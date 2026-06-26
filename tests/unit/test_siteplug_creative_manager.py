"""Unit tests for SiteplugCreativeManager.

Covers:
- _get_format_id() — plain string / dict / missing
- _get_affilizz_client() — lazy init, config fields, env var fallback, missing creds
- add_creative_assets() — text_ad_search routing, unknown format stub, exception handling
- _sync_text_ad_to_affilizz() — sandbox gate, config guard, domain guard,
  shop validation (success / 404 / error), upsert success, 409 guard, re-raise on other errors
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.siteplug.affilizz_client import AffilizzAPIError, AffilizzClient, ShopInfo
from src.adapters.siteplug.managers.creative import SiteplugCreativeManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    affilizz_internal_url: str = "https://api.affilizz.com",
    affilizz_api_key: str = "test-api-key",
) -> MagicMock:
    cfg = MagicMock()
    cfg.affilizz_internal_url = affilizz_internal_url
    cfg.affilizz_api_key = affilizz_api_key
    return cfg


def _make_manager(
    affilizz_internal_url: str = "https://api.affilizz.com",
    affilizz_api_key: str = "test-api-key",
) -> SiteplugCreativeManager:
    config = _make_config(affilizz_internal_url, affilizz_api_key)
    siteplug_client = MagicMock()
    return SiteplugCreativeManager(config=config, siteplug_client=siteplug_client)


def _make_text_ad_asset(
    creative_id: str = "creative-001",
    format_id: str | dict = "text_ad_search",
    domain: str = "example.com",
    title: str = "Buy Now",
    description: str = "Great deal",
    click_url: str = "https://example.com/buy",
    country: str = "DE",
) -> dict:
    return {
        "creative_id": creative_id,
        "format_id": format_id,
        "brand": {"domain": domain},
        "assets": {
            "title": {"content": title},
            "description": {"content": description},
            "click_url": {"url": click_url},
            "country": {"content": country},
        },
    }


def _make_shop_info(
    shop_id: str = "shop-123",
    shop_name: str = "Test Shop",
    shop_domain: str = "example.com",
    country_codes: list[str] | None = None,
) -> ShopInfo:
    return ShopInfo(
        shop_id=shop_id,
        shop_name=shop_name,
        shop_domain=shop_domain,
        country_codes=country_codes or ["DE"],
    )


# ---------------------------------------------------------------------------
# _get_format_id()
# ---------------------------------------------------------------------------


class TestGetFormatId:
    def _call(self, asset: dict) -> str:
        mgr = _make_manager()
        return mgr._get_format_id(asset)

    def test_plain_string_format_id(self):
        assert self._call({"format_id": "text_ad_search"}) == "text_ad_search"

    def test_dict_format_id_extracts_id_key(self):
        assert self._call({"format_id": {"id": "text_ad_search", "agent_url": "siteplug://t1"}}) == "text_ad_search"

    def test_missing_format_id_returns_empty_string(self):
        assert self._call({}) == ""

    def test_dict_without_id_key_returns_empty_string(self):
        assert self._call({"format_id": {"agent_url": "siteplug://t1"}}) == ""

    def test_non_string_non_dict_coerced_to_string(self):
        assert self._call({"format_id": 42}) == "42"


# ---------------------------------------------------------------------------
# _get_affilizz_client()
# ---------------------------------------------------------------------------


class TestGetAffilizzClient:
    def test_returns_client_when_config_has_credentials(self):
        mgr = _make_manager(
            affilizz_internal_url="https://api.affilizz.com",
            affilizz_api_key="secret",
        )
        client = mgr._get_affilizz_client()
        assert isinstance(client, AffilizzClient)

    def test_returns_none_when_url_missing(self):
        mgr = _make_manager(affilizz_internal_url="", affilizz_api_key="secret")
        # Ensure env vars don't interfere
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFFILIZZ_INTERNAL_URL", None)
            result = mgr._get_affilizz_client()
        assert result is None

    def test_returns_none_when_api_key_missing(self):
        mgr = _make_manager(affilizz_internal_url="https://api.affilizz.com", affilizz_api_key="")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFFILIZZ_API_KEY", None)
            result = mgr._get_affilizz_client()
        assert result is None

    def test_falls_back_to_env_vars_when_config_empty(self):
        mgr = _make_manager(affilizz_internal_url="", affilizz_api_key="")
        with patch.dict(
            os.environ,
            {"AFFILIZZ_INTERNAL_URL": "https://env.affilizz.com", "AFFILIZZ_API_KEY": "env-key"},
        ):
            client = mgr._get_affilizz_client()
        assert isinstance(client, AffilizzClient)

    def test_client_is_cached_on_second_call(self):
        mgr = _make_manager()
        client1 = mgr._get_affilizz_client()
        client2 = mgr._get_affilizz_client()
        assert client1 is client2


# ---------------------------------------------------------------------------
# add_creative_assets() — routing
# ---------------------------------------------------------------------------


class TestAddCreativeAssetsRouting:
    def test_unknown_format_returns_not_implemented_status(self):
        mgr = _make_manager()
        asset = {"creative_id": "c-001", "format_id": "siteplug_native_display"}

        results = mgr.add_creative_assets("mb-001", [asset], today=None)

        assert len(results) == 1
        assert results[0].creative_id == "c-001"
        assert results[0].status == "not_implemented"

    def test_multiple_assets_processed_independently(self):
        mgr = _make_manager()
        assets = [
            {"creative_id": "c-001", "format_id": "siteplug_native_display"},
            {"creative_id": "c-002", "format_id": "unknown_format"},
        ]

        results = mgr.add_creative_assets("mb-001", assets, today=None)

        assert len(results) == 2
        assert all(r.status == "not_implemented" for r in results)

    def test_empty_assets_list_returns_empty_results(self):
        mgr = _make_manager()
        results = mgr.add_creative_assets("mb-001", [], today=None)
        assert results == []

    def test_text_ad_search_format_as_dict_is_routed_to_affilizz(self):
        """format_id as dict with id='text_ad_search' must be routed to Affilizz."""
        mgr = _make_manager()
        asset = _make_text_ad_asset(format_id={"id": "text_ad_search", "agent_url": "siteplug://t1"})

        # Patch _sync_text_ad_to_affilizz to avoid real async execution
        with patch.object(
            mgr,
            "_sync_text_ad_to_affilizz",
            new=AsyncMock(return_value={"status": "ok", "id": "ad-123"}),
        ):
            results = mgr.add_creative_assets("mb-001", [asset], today=None)

        assert len(results) == 1
        assert results[0].status == "ok"

    def test_exception_in_text_ad_sync_returns_failed_status(self):
        """Exceptions from _sync_text_ad_to_affilizz are caught and returned as failed."""
        mgr = _make_manager()
        asset = _make_text_ad_asset()

        with patch.object(
            mgr,
            "_sync_text_ad_to_affilizz",
            new=AsyncMock(side_effect=RuntimeError("unexpected error")),
        ):
            results = mgr.add_creative_assets("mb-001", [asset], today=None)

        assert len(results) == 1
        assert results[0].status == "failed"
        assert "unexpected error" in results[0].message


# ---------------------------------------------------------------------------
# _sync_text_ad_to_affilizz() — individual gates
# ---------------------------------------------------------------------------


class TestSyncTextAdToAffilizz:
    """Tests for the async _sync_text_ad_to_affilizz method."""

    @pytest.mark.asyncio
    async def test_sandbox_gate_skips_sync(self):
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        account = MagicMock()
        account.sandbox = True

        result = await mgr._sync_text_ad_to_affilizz(asset, account=account)

        assert result["status"] == "skipped"
        assert result["reason"] == "sandbox"

    @pytest.mark.asyncio
    async def test_no_config_returns_skipped(self):
        """When Affilizz credentials are not configured, sync is skipped gracefully."""
        mgr = _make_manager(affilizz_internal_url="", affilizz_api_key="")
        asset = _make_text_ad_asset()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFFILIZZ_INTERNAL_URL", None)
            os.environ.pop("AFFILIZZ_API_KEY", None)
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "skipped"
        assert result["reason"] == "no_config"

    @pytest.mark.asyncio
    async def test_missing_domain_returns_skipped(self):
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        asset["brand"] = {}  # no domain

        mock_client = AsyncMock(spec=AffilizzClient)
        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "skipped"
        assert result["reason"] == "no_domain"

    @pytest.mark.asyncio
    async def test_shop_validation_error_returns_skipped(self):
        mgr = _make_manager()
        asset = _make_text_ad_asset()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(side_effect=AffilizzAPIError("API error", status_code=500))

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "skipped"
        assert result["reason"] == "shop_validation_error"

    @pytest.mark.asyncio
    async def test_shop_not_found_returns_skipped(self):
        mgr = _make_manager()
        asset = _make_text_ad_asset()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=None)  # 404

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "skipped"
        assert result["reason"] == "shop_not_found"

    @pytest.mark.asyncio
    async def test_successful_upsert_returns_ok(self):
        mgr = _make_manager()
        asset = _make_text_ad_asset(creative_id="c-001")
        shop = _make_shop_info()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(return_value={"id": "affilizz-ad-001"})

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "ok"
        assert result["affilizz_id"] == "affilizz-ad-001"
        assert result["action"] == "upserted"
        assert result["creative_id"] == "c-001"

    @pytest.mark.asyncio
    async def test_409_conflict_returns_skipped_updated_manually(self):
        """HTTP 409 from upsert_text_ad is caught and returns skipped/updated_manually."""
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        shop = _make_shop_info()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(side_effect=AffilizzAPIError("Conflict", status_code=409))

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset)

        assert result["status"] == "skipped"
        assert result["reason"] == "updated_manually"

    @pytest.mark.asyncio
    async def test_non_409_api_error_is_re_raised(self):
        """Non-409 AffilizzAPIError from upsert_text_ad propagates to caller."""
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        shop = _make_shop_info()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(side_effect=AffilizzAPIError("Server Error", status_code=500))

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            with pytest.raises(AffilizzAPIError) as exc_info:
                await mgr._sync_text_ad_to_affilizz(asset)

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_sandbox_false_does_not_skip(self):
        """account.sandbox = False must NOT trigger the sandbox gate."""
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        account = MagicMock()
        account.sandbox = False
        shop = _make_shop_info()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(return_value={"id": "ad-001"})

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset, account=account)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_no_account_does_not_trigger_sandbox_gate(self):
        """account=None must not trigger the sandbox gate."""
        mgr = _make_manager()
        asset = _make_text_ad_asset()
        shop = _make_shop_info()

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(return_value={"id": "ad-001"})

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            result = await mgr._sync_text_ad_to_affilizz(asset, account=None)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_validate_shop_called_with_brand_domain(self):
        """validate_shop must be called with the brand.domain from the asset."""
        mgr = _make_manager()
        asset = _make_text_ad_asset(domain="myshop.com")
        shop = _make_shop_info(shop_domain="myshop.com")

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(return_value={"id": "ad-001"})

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            await mgr._sync_text_ad_to_affilizz(asset)

        mock_client.validate_shop.assert_awaited_once_with("myshop.com")

    @pytest.mark.asyncio
    async def test_upsert_called_with_built_payload(self):
        """upsert_text_ad must receive a payload built from the asset + shop info."""
        mgr = _make_manager()
        asset = _make_text_ad_asset(creative_id="c-999", title="Special Offer")
        shop = _make_shop_info(shop_id="shop-xyz")

        mock_client = AsyncMock(spec=AffilizzClient)
        mock_client.validate_shop = AsyncMock(return_value=shop)
        mock_client.upsert_text_ad = AsyncMock(return_value={"id": "ad-001"})

        with patch.object(mgr, "_get_affilizz_client", return_value=mock_client):
            await mgr._sync_text_ad_to_affilizz(asset)

        upsert_payload = mock_client.upsert_text_ad.call_args.args[0]
        assert upsert_payload["externalId"] == "c-999"
        assert upsert_payload["title"] == "Special Offer"
        assert upsert_payload["shopId"] == "shop-xyz"
        assert upsert_payload["createdBy"] == "agent-siteplug"
