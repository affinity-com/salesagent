"""Unit tests for Task 02 — SiteplugClient wiring + entity provisioning.

Covers:
- _handle_response: 207 returns full body, 2xx extracts data, error codes mapped
- _request: 429 rate-limit sleep + retry, Idempotency-Key forwarding
- SiteplugClient method routing (platforms, agencies, brands, advertisers, campaigns, onboard)
- provision_entity_stack: onboarding primary path (207 parse + idempotency guard)
- provision_entity_stack: sequential fallback (per-step guards, 409 platform resolution)
- provision_entity_stack: agency always skipped (non-RTB, agency_id=0 unconditionally)
- SiteplugAdapter.create_media_buy: dry-run, missing platform_name, provisioning error
"""

from __future__ import annotations

import json
import time
import unittest.mock
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.adapters.siteplug.client import SiteplugAPIError, SiteplugClient
from src.adapters.siteplug.config_schema import SiteplugConnectionConfig
from src.adapters.siteplug.managers.campaign import SiteplugCampaignManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(base_url: str = "https://ssp.example.com/ssp/v1", api_key: str = "test-key") -> SiteplugConnectionConfig:
    return SiteplugConnectionConfig(base_url=base_url, api_key=api_key, timeout=5, max_retries=1)


def _make_response(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    content = json.dumps(body).encode() if body is not None else b""
    return httpx.Response(status_code=status, content=content, headers=headers or {})


def _make_client() -> SiteplugClient:
    return SiteplugClient(_make_config())


# ---------------------------------------------------------------------------
# _handle_response
# ---------------------------------------------------------------------------

class TestHandleResponse:
    def test_207_returns_full_body(self):
        client = _make_client()
        body = {
            "platform": {"platform_id": 10},
            "agency": {"masteraccount_id": 0},
            "summary": {},
            "results": [{"status": "success", "steps": {}}],
        }
        resp = _make_response(207, body)
        result = client._handle_response(resp)
        assert result["platform"]["platform_id"] == 10
        assert "data" not in result  # NOT body["data"]

    def test_2xx_extracts_data(self):
        client = _make_client()
        body = {"status": "success", "data": {"platform_id": 42}, "meta": {}}
        resp = _make_response(200, body)
        result = client._handle_response(resp)
        assert result == {"platform_id": 42}

    def test_2xx_no_data_key_returns_body(self):
        client = _make_client()
        body = {"status": "ok"}
        resp = _make_response(200, body)
        result = client._handle_response(resp)
        assert result == {"status": "ok"}

    @pytest.mark.parametrize("status,expected_code", [
        (400, "VALIDATION_ERROR"),
        (401, "API_KEY_INVALID"),
        (404, "ENTITY_NOT_FOUND"),
        (409, "ENTITY_ALREADY_EXISTS"),
        (422, "VALIDATION_ERROR"),
        (429, "RATE_LIMITED"),
        (500, "INTERNAL_ERROR"),
    ])
    def test_error_codes_mapped(self, status, expected_code):
        client = _make_client()
        resp = _make_response(status, {"message": "err"})
        with pytest.raises(SiteplugAPIError) as exc_info:
            client._handle_response(resp)
        assert exc_info.value.error_code == expected_code
        assert exc_info.value.status_code == status

    def test_500_onboarding_prefers_body_error_code(self):
        client = _make_client()
        body = {"error": {"code": "PLATFORM_ERROR", "message": "SP failed"}}
        resp = _make_response(500, body)
        with pytest.raises(SiteplugAPIError) as exc_info:
            client._handle_response(resp)
        assert exc_info.value.error_code == "PLATFORM_ERROR"

    def test_500_falls_back_to_internal_error_when_no_body_code(self):
        client = _make_client()
        resp = _make_response(500, {"message": "oops"})
        with pytest.raises(SiteplugAPIError) as exc_info:
            client._handle_response(resp)
        assert exc_info.value.error_code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# _request: Idempotency-Key forwarding
# ---------------------------------------------------------------------------

class TestRequestIdempotencyKey:
    @pytest.mark.asyncio
    async def test_idempotency_key_forwarded_on_post(self):
        client = _make_client()
        captured_headers = {}

        # httpx.AsyncClient.request is called with keyword args (method=, url=, headers=, ...)
        async def fake_request(self_inner, **kwargs):
            captured_headers.update(kwargs.get("headers") or {})
            return _make_response(200, {"data": {"platform_id": 1}})

        with patch("httpx.AsyncClient.request", new=fake_request):
            await client._request("POST", "/platforms", json={"platform_name": "CJ"}, idempotency_key="idem-123")

        assert captured_headers.get("Idempotency-Key") == "idem-123"

    @pytest.mark.asyncio
    async def test_idempotency_key_not_forwarded_on_get(self):
        client = _make_client()
        captured_headers = {}

        async def fake_request(self_inner, **kwargs):
            captured_headers.update(kwargs.get("headers") or {})
            return _make_response(200, {"data": []})

        with patch("httpx.AsyncClient.request", new=fake_request):
            await client._request("GET", "/platforms", idempotency_key="idem-123")

        assert "Idempotency-Key" not in captured_headers

    @pytest.mark.asyncio
    async def test_no_idempotency_key_when_none(self):
        client = _make_client()
        captured_headers = {}

        async def fake_request(self_inner, **kwargs):
            captured_headers.update(kwargs.get("headers") or {})
            return _make_response(200, {"data": {"platform_id": 1}})

        with patch("httpx.AsyncClient.request", new=fake_request):
            await client._request("POST", "/platforms", json={})

        assert "Idempotency-Key" not in captured_headers


# ---------------------------------------------------------------------------
# _request: 429 rate-limit retry
# ---------------------------------------------------------------------------

class TestRequestRateLimit:
    @pytest.mark.asyncio
    async def test_429_sleeps_until_reset_and_retries(self):
        client = _make_client()
        reset_ts = time.time() + 2.0
        call_count = 0

        async def fake_request(self_inner, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(429, {"message": "rate limited"}, {"X-RateLimit-Reset": str(reset_ts)})
            return _make_response(200, {"data": {"platform_id": 5}})

        slept = []
        async def fake_sleep(secs):
            slept.append(secs)

        with patch("httpx.AsyncClient.request", new=fake_request), \
             patch("asyncio.sleep", new=fake_sleep):
            result = await client._request("POST", "/platforms", json={})

        assert call_count == 2
        assert len(slept) == 1
        assert slept[0] <= 60.0
        assert result == {"platform_id": 5}

    @pytest.mark.asyncio
    async def test_429_sleep_capped_at_60(self):
        client = _make_client()
        reset_ts = time.time() + 9999.0  # far future
        call_count = 0

        async def fake_request(self_inner, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(429, {}, {"X-RateLimit-Reset": str(reset_ts)})
            return _make_response(200, {"data": {}})

        slept = []
        async def fake_sleep(secs):
            slept.append(secs)

        with patch("httpx.AsyncClient.request", new=fake_request), \
             patch("asyncio.sleep", new=fake_sleep):
            await client._request("POST", "/platforms", json={})

        assert slept[0] == 60.0


# ---------------------------------------------------------------------------
# Client method routing
# ---------------------------------------------------------------------------

class TestClientMethodRouting:
    @pytest.mark.asyncio
    async def test_health_uses_no_auth_header(self):
        client = _make_client()
        captured = {}

        # httpx.AsyncClient.get is an instance method; patch receives self as first arg
        async def fake_get(self_inner, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b'{"status":"ok"}'
            resp.json.return_value = {"status": "ok"}
            return resp

        with patch("httpx.AsyncClient.get", new=fake_get):
            await client.health()

        assert "X-API-Key" not in captured["headers"]
        assert captured["url"].endswith("/health")

    @pytest.mark.asyncio
    async def test_list_campaigns_hits_campaigns_list(self):
        client = _make_client()
        captured_path = []

        async def fake_request(self_inner, **kwargs):
            captured_path.append(kwargs.get("url", ""))
            return _make_response(200, {"data": []})

        with patch("httpx.AsyncClient.request", new=fake_request):
            await client.list_campaigns()

        assert captured_path[0].endswith("/campaigns/list")

    @pytest.mark.asyncio
    async def test_onboard_returns_full_207_body(self):
        client = _make_client()
        body_207 = {
            "platform": {"platform_id": 1},
            "agency": {"masteraccount_id": 0},
            "summary": {"total": 1},
            "results": [{"status": "success", "steps": {
                "brand": {"brand_id": 10},
                "advertiser": {"advertiser_id": 20},
                "campaign": {"campaign_id": 30},
            }}],
        }

        async def fake_request(self_inner, **kwargs):
            return _make_response(207, body_207)

        with patch("httpx.AsyncClient.request", new=fake_request):
            result = await client.onboard({"platform_name": "CJ", "rtb_flag": 0, "brands": []})

        assert result["platform"]["platform_id"] == 1
        assert result["results"][0]["steps"]["campaign"]["campaign_id"] == 30


# ---------------------------------------------------------------------------
# provision_entity_stack — onboarding primary path
# ---------------------------------------------------------------------------

class TestProvisionEntityStackOnboarding:
    def _make_manager(self, client=None):
        if client is None:
            client = MagicMock()
        return SiteplugCampaignManager(client=client, log_func=lambda msg, **kw: None)

    def _make_207_body(self, campaign_id=99):
        return {
            "platform": {"platform_id": 1},
            "agency": {"masteraccount_id": 5},
            "summary": {},
            "results": [{
                "status": "success",
                "steps": {
                    "brand": {"brand_id": 10},
                    "advertiser": {"advertiser_id": 20},
                    "campaign": {"campaign_id": campaign_id},
                },
            }],
        }

    @pytest.mark.asyncio
    async def test_onboarding_happy_path_returns_campaign_id(self):
        client = MagicMock()
        client.onboard = AsyncMock(return_value=self._make_207_body(99))
        manager = self._make_manager(client)

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_persist_entity_ids") as mock_persist:
            result = await manager.provision_entity_stack(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                tenant_id="test-tenant",
            )

        assert result == 99
        mock_persist.assert_called_once_with(
            media_buy_id=unittest.mock.ANY,
            package_id=unittest.mock.ANY,
            platform_id=1,
            agency_id=5,
            brand_id=10,
            advertiser_id=20,
            campaign_id=99,
            tenant_id=unittest.mock.ANY,
        )

    @pytest.mark.asyncio
    async def test_idempotency_guard_skips_onboard_when_campaign_id_exists(self):
        client = MagicMock()
        client.onboard = AsyncMock()
        manager = self._make_manager(client)

        with patch.object(manager, "_read_package_config_field", return_value=77):
            result = await manager.provision_entity_stack(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                tenant_id="test-tenant",
            )

        assert result == 77
        client.onboard.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_failure_raises_siteplug_api_error(self):
        body = {
            "platform": {"platform_id": 1},
            "agency": {"masteraccount_id": 0},
            "summary": {},
            "results": [{
                "status": "partial_failure",
                "steps": {
                    "brand": {"brand_id": 10, "status": "success"},
                    "advertiser": {"status": "failed", "error_code": "ADVERTISER_ERROR", "message": "SP failed"},
                    "campaign": {},
                },
            }],
        }
        client = MagicMock()
        client.onboard = AsyncMock(return_value=body)
        manager = self._make_manager(client)

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             pytest.raises(SiteplugAPIError) as exc_info:
            await manager.provision_entity_stack(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                tenant_id="test-tenant",
            )

        assert exc_info.value.error_code == "ADVERTISER_ERROR"

    @pytest.mark.asyncio
    async def test_onboard_payload_excludes_campaign_fields(self):
        """Onboarding request must NOT include campaign_name, campaign_type, sol_id, deal_type, budget_type."""
        client = MagicMock()
        client.onboard = AsyncMock(return_value=self._make_207_body())
        manager = self._make_manager(client)

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_persist_entity_ids"):
            await manager.provision_entity_stack(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                deal_type="CPC",
                budget_type=2,
                tenant_id="test-tenant",
            )

        payload = client.onboard.call_args[0][0]
        assert "campaign_name" not in payload
        assert "campaign_type" not in payload
        assert "sol_id" not in payload
        assert "deal_type" not in payload
        assert "budget_type" not in payload
        assert payload["platform_name"] == "CJ"
        assert payload["rtb_flag"] == 0  # always 0 — all AdCP campaigns are non-RTB
        assert len(payload["brands"]) == 1
        assert payload["brands"][0]["brand_name"] == "Acme"

    @pytest.mark.asyncio
    async def test_falls_back_to_sequential_when_onboard_404(self):
        """If /onboard returns 404 (Phase 7 not live), fall back to sequential."""
        client = MagicMock()
        client.onboard = AsyncMock(side_effect=SiteplugAPIError("not found", status_code=404, error_code="ENTITY_NOT_FOUND"))
        manager = self._make_manager(client)

        sequential_called = []

        async def fake_sequential(**kwargs):
            sequential_called.append(kwargs)
            return 55

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_provision_sequential", new=fake_sequential):
            result = await manager.provision_entity_stack(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                tenant_id="test-tenant",
            )

        assert result == 55
        assert len(sequential_called) == 1


# ---------------------------------------------------------------------------
# provision_entity_stack — sequential fallback
# ---------------------------------------------------------------------------

class TestProvisionEntityStackSequential:
    def _make_manager(self):
        client = MagicMock()
        client.list_platforms = AsyncMock(return_value=[])  # empty → triggers POST
        client.create_platform = AsyncMock(return_value={"platform_id": 1})
        client.create_agency = AsyncMock(return_value={"masteraccount_id": 5})
        client.list_advertisers = AsyncMock(return_value=[])  # empty → triggers POST
        client.create_brand = AsyncMock(return_value={"brand_id": 10})
        client.create_advertiser = AsyncMock(return_value={"advertiser_id": 20})
        client.create_campaign = AsyncMock(return_value={"campaign_id": 30})
        return SiteplugCampaignManager(client=client, log_func=lambda msg, **kw: None), client

    @pytest.mark.asyncio
    async def test_sequential_happy_path(self):
        """Agency is always skipped; platform resolved via GET then POST; advertiser via POST."""
        manager, client = self._make_manager()

        persisted = []
        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_persist_entity_ids", side_effect=lambda **kw: persisted.append(kw)):
            result = await manager._provision_sequential(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                is_product=0,
                deal_type=None,
                budget_type=None,
                tenant_id="test-tenant",
                idempotency_key=None,
            )

        assert result == 30
        client.create_platform.assert_called_once_with(
            {"platform": "CJ"}, idempotency_key=unittest.mock.ANY
        )
        client.create_agency.assert_not_called()  # C1: agency always skipped
        client.create_brand.assert_called_once_with(
            unittest.mock.ANY, idempotency_key=unittest.mock.ANY
        )
        client.create_advertiser.assert_called_once_with(
            unittest.mock.ANY, idempotency_key=unittest.mock.ANY
        )
        client.create_campaign.assert_called_once_with(
            unittest.mock.ANY, idempotency_key=unittest.mock.ANY
        )

    @pytest.mark.asyncio
    async def test_sequential_always_skips_agency(self):
        """Agency step is unconditionally skipped — agency_id=0 always (C1)."""
        manager, client = self._make_manager()

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_persist_entity_ids"):
            await manager._provision_sequential(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                is_product=0,
                deal_type=None,
                budget_type=None,
                tenant_id="test-tenant",
                idempotency_key=None,
            )

        client.create_agency.assert_not_called()

    @pytest.mark.asyncio
    async def test_sequential_resolves_existing_platform_via_search(self):
        """Platform is resolved via GET /platforms?search= first; POST only if not found."""
        manager, client = self._make_manager()
        # GET returns the existing platform — POST should NOT be called
        client.list_platforms = AsyncMock(return_value=[{"platform": "CJ", "platform_id": 7}])

        with patch.object(manager, "_read_package_config_field", return_value=None), \
             patch.object(manager, "_persist_entity_ids"):
            result = await manager._provision_sequential(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                is_product=0,
                deal_type=None,
                budget_type=None,
                tenant_id="test-tenant",
                idempotency_key=None,
            )

        assert result == 30  # campaign_id from mock
        client.list_platforms.assert_called_once_with(search="CJ")
        client.create_platform.assert_not_called()  # resolved via GET, no POST needed

    @pytest.mark.asyncio
    async def test_sequential_per_step_idempotency_skips_existing(self):
        """If platform_id already in package_config, skip platform resolution entirely."""
        manager, client = self._make_manager()

        def read_field(media_buy_id, package_id, field, tenant_id):
            if field == "siteplug_platform_id":
                return 99  # already exists
            return None

        with patch.object(manager, "_read_package_config_field", side_effect=read_field), \
             patch.object(manager, "_persist_entity_ids"):
            await manager._provision_sequential(
                media_buy_id="sp_123",
                package_id="pkg-1",
                platform_name="CJ",
                brand_name="Acme",
                brand_domain="acme.com",
                vertical="Retail",
                sub_category="Fashion",
                campaign_type="SDC",
                sol_id=1,
                is_product=0,
                deal_type=None,
                budget_type=None,
                tenant_id="test-tenant",
                idempotency_key=None,
            )

        client.create_platform.assert_not_called()
        client.list_platforms.assert_not_called()  # skipped entirely when ID already in config
        client.create_brand.assert_called_once_with(
            unittest.mock.ANY, idempotency_key=unittest.mock.ANY
        )  # brand still created


# ---------------------------------------------------------------------------
# SiteplugAdapter.create_media_buy
# ---------------------------------------------------------------------------

class TestAdapterCreateMediaBuy:
    def _make_adapter(self, dry_run=False):
        from src.adapters.siteplug.adapter import SiteplugAdapter
        config = {
            "base_url": "https://ssp.example.com/ssp/v1",
            "api_key": "test-key",
        }
        principal = MagicMock()
        principal.name = "Test Advertiser"
        return SiteplugAdapter(config, principal, dry_run=dry_run, tenant_id="test-tenant")

    def _make_request(self, po_number="PO-001", idempotency_key=None):
        req = MagicMock()
        req.po_number = po_number
        req.idempotency_key = idempotency_key
        return req

    def _make_package(self, impl_config=None):
        pkg = MagicMock()
        pkg.package_id = "pkg-1"
        if impl_config is None:
            pkg.implementation_config = {
                "platform_name": "CJ",
                "brand_name": "Acme",
                "brand_domain": "acme.com",
                "vertical": "Retail",
                "sub_category": "Fashion",
                "campaign_type": "SDC",
                "sol_id": 1,
                "is_product": 0,
            }
        else:
            pkg.implementation_config = impl_config
        return pkg

    def test_dry_run_returns_synthetic_id(self):
        adapter = self._make_adapter(dry_run=True)
        req = self._make_request()
        pkg = self._make_package()

        result = adapter.create_media_buy(req, [pkg], MagicMock(), MagicMock())

        assert result.media_buy_id.startswith("sp_")

    def test_missing_platform_name_raises_validation_error(self):
        from src.core.exceptions import AdCPValidationError
        adapter = self._make_adapter(dry_run=False)
        req = self._make_request()
        pkg = self._make_package(impl_config={})  # no platform_name

        with pytest.raises(AdCPValidationError, match="platform_name"):
            adapter.create_media_buy(req, [pkg], MagicMock(), MagicMock())

    def test_no_packages_raises_validation_error(self):
        from src.core.exceptions import AdCPValidationError
        adapter = self._make_adapter(dry_run=False)
        req = self._make_request()

        with pytest.raises(AdCPValidationError, match="No packages"):
            adapter.create_media_buy(req, [], MagicMock(), MagicMock())

    def test_provisioning_success_returns_sp_campaign_id(self):
        from src.core.schemas import CreateMediaBuySuccess
        adapter = self._make_adapter(dry_run=False)
        req = self._make_request()
        pkg = self._make_package()

        with patch.object(adapter.campaign_manager, "provision_entity_stack", new=AsyncMock(return_value=42)):
            result = adapter.create_media_buy(req, [pkg], MagicMock(), MagicMock())

        assert isinstance(result, CreateMediaBuySuccess)
        assert result.media_buy_id == "sp_42"

    def test_provisioning_error_raises_validation_error(self):
        from src.core.exceptions import AdCPValidationError
        adapter = self._make_adapter(dry_run=False)
        req = self._make_request()
        pkg = self._make_package()

        with patch.object(
            adapter.campaign_manager,
            "provision_entity_stack",
            new=AsyncMock(side_effect=SiteplugAPIError("SP failed", error_code="PLATFORM_ERROR")),
        ):
            with pytest.raises(AdCPValidationError, match="provisioning failed"):
                adapter.create_media_buy(req, [pkg], MagicMock(), MagicMock())
