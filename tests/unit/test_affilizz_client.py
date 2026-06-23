"""Unit tests for AffilizzClient and build_text_ad_payload.

Covers:
- AffilizzClient._headers() — correct ApiKey header
- AffilizzClient._request() — URL construction, header merging, network error wrapping
- AffilizzClient.validate_shop() — 200 / 404 / error / caching
- AffilizzClient.resolve_text_ad() — 200 / 404 / error
- AffilizzClient.create_text_ad() — 2xx success / non-2xx error
- AffilizzClient.patch_text_ad() — 2xx success / non-2xx error
- AffilizzClient.get_text_ad() — 200 / 404 / error
- AffilizzClient.upsert_text_ad() — create path / update path
- build_text_ad_payload() — field mapping, country fallback, optional fields
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.adapters.siteplug.affilizz_client import (
    AffilizzAPIError,
    AffilizzClient,
    ShopInfo,
    build_text_ad_payload,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    base_url: str = "https://api.affilizz.com",
    api_key: str = "test-key",
    agent_id: str = "agent-siteplug",
) -> AffilizzClient:
    """Return an AffilizzClient with a mocked httpx.AsyncClient."""
    client = AffilizzClient(base_url=base_url, api_key=api_key, agent_id=agent_id)
    client._http = AsyncMock()
    return client


def _make_response(status_code: int, json_data: dict | None = None, text: str = "") -> MagicMock:
    """Build a minimal httpx.Response mock."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.is_success = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


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
# ShopInfo dataclass
# ---------------------------------------------------------------------------


class TestShopInfo:
    def test_fields_stored_correctly(self):
        shop = ShopInfo(
            shop_id="s1",
            shop_name="My Shop",
            shop_domain="myshop.com",
            country_codes=["DE", "AT"],
        )
        assert shop.shop_id == "s1"
        assert shop.shop_name == "My Shop"
        assert shop.shop_domain == "myshop.com"
        assert shop.country_codes == ["DE", "AT"]

    def test_country_codes_defaults_to_empty_list(self):
        shop = ShopInfo(shop_id="s1", shop_name="S", shop_domain="s.com")
        assert shop.country_codes == []


# ---------------------------------------------------------------------------
# AffilizzAPIError
# ---------------------------------------------------------------------------


class TestAffilizzAPIError:
    def test_stores_status_code_and_message(self):
        err = AffilizzAPIError("something went wrong", status_code=422)
        assert err.status_code == 422
        assert err.message == "something went wrong"
        assert str(err) == "something went wrong"

    def test_default_status_code_is_zero(self):
        err = AffilizzAPIError("oops")
        assert err.status_code == 0


# ---------------------------------------------------------------------------
# AffilizzClient — construction
# ---------------------------------------------------------------------------


class TestAffilizzClientConstruction:
    def test_trailing_slash_stripped_from_base_url(self):
        client = AffilizzClient(base_url="https://api.affilizz.com/", api_key="k")
        assert client._base_url == "https://api.affilizz.com"

    def test_default_agent_id(self):
        client = AffilizzClient(base_url="https://api.affilizz.com", api_key="k")
        assert client._agent_id == "agent-siteplug"

    def test_custom_agent_id(self):
        client = AffilizzClient(base_url="https://api.affilizz.com", api_key="k", agent_id="my-agent")
        assert client._agent_id == "my-agent"

    def test_shop_cache_starts_empty(self):
        client = AffilizzClient(base_url="https://api.affilizz.com", api_key="k")
        assert client._shop_cache == {}


# ---------------------------------------------------------------------------
# AffilizzClient._headers()
# ---------------------------------------------------------------------------


class TestAffilizzClientHeaders:
    def test_returns_api_key_header(self):
        client = _make_client(api_key="my-secret-key")
        headers = client._headers()
        assert headers == {"ApiKey": "my-secret-key"}


# ---------------------------------------------------------------------------
# AffilizzClient._request()
# ---------------------------------------------------------------------------


class TestAffilizzClientRequest:
    @pytest.mark.asyncio
    async def test_constructs_correct_url(self):
        client = _make_client(base_url="https://api.affilizz.com")
        client._http.request = AsyncMock(return_value=_make_response(200, {}))

        await client._request("GET", "/internal/text-ads/_validate-shop")

        client._http.request.assert_called_once()
        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["url"] == "https://api.affilizz.com/internal/text-ads/_validate-shop"

    @pytest.mark.asyncio
    async def test_merges_api_key_header_with_extra_headers(self):
        client = _make_client(api_key="secret")
        client._http.request = AsyncMock(return_value=_make_response(200, {}))

        await client._request("GET", "/path", headers={"X-Custom": "value"})

        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["headers"]["ApiKey"] == "secret"
        assert call_kwargs.kwargs["headers"]["X-Custom"] == "value"

    @pytest.mark.asyncio
    async def test_wraps_network_error_as_affilizz_api_error(self):
        client = _make_client()
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client._request("GET", "/path")

        assert exc_info.value.status_code == 0
        assert "Network error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_forwards_query_params(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(200, {}))

        await client._request("GET", "/path", params={"domain": "example.com"})

        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["params"] == {"domain": "example.com"}


# ---------------------------------------------------------------------------
# AffilizzClient.validate_shop()
# ---------------------------------------------------------------------------


class TestValidateShop:
    @pytest.mark.asyncio
    async def test_returns_shop_info_on_200(self):
        client = _make_client()
        client._http.request = AsyncMock(
            return_value=_make_response(
                200,
                {
                    "id": "shop-abc",
                    "name": "My Shop",
                    "domain": "myshop.com",
                    "countryCodes": ["DE", "AT"],
                },
            )
        )

        result = await client.validate_shop("myshop.com")

        assert isinstance(result, ShopInfo)
        assert result.shop_id == "shop-abc"
        assert result.shop_name == "My Shop"
        assert result.shop_domain == "myshop.com"
        assert result.country_codes == ["DE", "AT"]

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(404))

        result = await client.validate_shop("unknown.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_non_200_non_404(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(500, text="Internal Server Error"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client.validate_shop("example.com")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_caches_successful_result(self):
        client = _make_client()
        client._http.request = AsyncMock(
            return_value=_make_response(200, {"id": "s1", "name": "S", "domain": "s.com", "countryCodes": []})
        )

        result1 = await client.validate_shop("s.com")
        result2 = await client.validate_shop("s.com")

        # HTTP called only once — second call served from cache
        assert client._http.request.call_count == 1
        assert result1 is result2

    @pytest.mark.asyncio
    async def test_caches_none_on_404(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(404))

        await client.validate_shop("missing.com")
        await client.validate_shop("missing.com")

        assert client._http.request.call_count == 1
        assert client._shop_cache["missing.com"] is None

    @pytest.mark.asyncio
    async def test_country_codes_defaults_to_empty_list_when_absent(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(200, {"id": "s1", "name": "S", "domain": "s.com"}))

        result = await client.validate_shop("s.com")

        assert result.country_codes == []


# ---------------------------------------------------------------------------
# AffilizzClient.resolve_text_ad()
# ---------------------------------------------------------------------------


class TestResolveTextAd:
    @pytest.mark.asyncio
    async def test_returns_dict_on_200(self):
        client = _make_client()
        payload = {"id": "ad-xyz", "externalId": "creative-001"}
        client._http.request = AsyncMock(return_value=_make_response(200, payload))

        result = await client.resolve_text_ad("creative-001")

        assert result == payload

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(404))

        result = await client.resolve_text_ad("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_error_status(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(503, text="Service Unavailable"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client.resolve_text_ad("creative-001")

        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# AffilizzClient.create_text_ad()
# ---------------------------------------------------------------------------


class TestCreateTextAd:
    @pytest.mark.asyncio
    async def test_returns_json_on_success(self):
        client = _make_client()
        created = {"id": "ad-new", "externalId": "c-001"}
        client._http.request = AsyncMock(return_value=_make_response(201, created))

        result = await client.create_text_ad({"externalId": "c-001", "title": "T"})

        assert result == created

    @pytest.mark.asyncio
    async def test_raises_on_non_2xx(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(409, text="Conflict"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client.create_text_ad({"externalId": "c-001"})

        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_posts_to_correct_path(self):
        client = _make_client(base_url="https://api.affilizz.com")
        client._http.request = AsyncMock(return_value=_make_response(201, {"id": "x"}))

        await client.create_text_ad({"externalId": "c-001"})

        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["url"] == "https://api.affilizz.com/internal/text-ads"
        assert call_kwargs.kwargs["method"] == "POST"


# ---------------------------------------------------------------------------
# AffilizzClient.patch_text_ad()
# ---------------------------------------------------------------------------


class TestPatchTextAd:
    @pytest.mark.asyncio
    async def test_returns_json_on_success(self):
        client = _make_client()
        updated = {"id": "ad-123", "title": "Updated"}
        client._http.request = AsyncMock(return_value=_make_response(200, updated))

        result = await client.patch_text_ad("ad-123", {"title": "Updated"})

        assert result == updated

    @pytest.mark.asyncio
    async def test_patches_correct_url(self):
        client = _make_client(base_url="https://api.affilizz.com")
        client._http.request = AsyncMock(return_value=_make_response(200, {"id": "ad-123"}))

        await client.patch_text_ad("ad-123", {"title": "T"})

        call_kwargs = client._http.request.call_args
        assert call_kwargs.kwargs["url"] == "https://api.affilizz.com/internal/text-ads/ad-123"
        assert call_kwargs.kwargs["method"] == "PATCH"

    @pytest.mark.asyncio
    async def test_raises_on_non_2xx(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(404, text="Not Found"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client.patch_text_ad("ad-missing", {})

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# AffilizzClient.get_text_ad()
# ---------------------------------------------------------------------------


class TestGetTextAd:
    @pytest.mark.asyncio
    async def test_returns_dict_on_200(self):
        client = _make_client()
        ad_data = {"id": "ad-abc", "title": "Hello"}
        client._http.request = AsyncMock(return_value=_make_response(200, ad_data))

        result = await client.get_text_ad("ad-abc")

        assert result == ad_data

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(404))

        result = await client.get_text_ad("ad-missing")

        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_error_status(self):
        client = _make_client()
        client._http.request = AsyncMock(return_value=_make_response(500, text="Error"))

        with pytest.raises(AffilizzAPIError) as exc_info:
            await client.get_text_ad("ad-abc")

        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# AffilizzClient.upsert_text_ad()
# ---------------------------------------------------------------------------


class TestUpsertTextAd:
    @pytest.mark.asyncio
    async def test_creates_new_ad_when_not_found(self):
        """resolve returns None → create_text_ad is called."""
        client = _make_client()
        created = {"id": "ad-new"}

        client.resolve_text_ad = AsyncMock(return_value=None)
        client.create_text_ad = AsyncMock(return_value=created)
        client.patch_text_ad = AsyncMock()

        payload = {"externalId": "c-001", "createdBy": "agent-siteplug", "title": "T"}
        result = await client.upsert_text_ad(payload)

        client.resolve_text_ad.assert_awaited_once_with("c-001")
        client.create_text_ad.assert_awaited_once_with(payload)
        client.patch_text_ad.assert_not_awaited()
        assert result == created

    @pytest.mark.asyncio
    async def test_patches_existing_ad_when_found(self):
        """resolve returns existing ad → patch_text_ad is called, createdBy excluded."""
        client = _make_client(agent_id="agent-siteplug")
        existing = {"id": "ad-existing", "externalId": "c-001"}
        patched = {"id": "ad-existing", "title": "Updated"}

        client.resolve_text_ad = AsyncMock(return_value=existing)
        client.create_text_ad = AsyncMock()
        client.patch_text_ad = AsyncMock(return_value=patched)

        payload = {
            "externalId": "c-001",
            "createdBy": "agent-siteplug",
            "title": "Updated",
            "description": "Desc",
        }
        result = await client.upsert_text_ad(payload)

        client.create_text_ad.assert_not_awaited()
        patch_call = client.patch_text_ad.call_args
        patch_payload = patch_call.args[1]

        # createdBy must be excluded from PATCH
        assert "createdBy" not in patch_payload
        # updatedBy must be set to agent_id
        assert patch_payload["updatedBy"] == "agent-siteplug"
        assert result == patched

    @pytest.mark.asyncio
    async def test_patch_payload_retains_other_fields(self):
        """All fields except createdBy are forwarded to patch_text_ad."""
        client = _make_client(agent_id="agent-siteplug")
        existing = {"id": "ad-existing"}

        client.resolve_text_ad = AsyncMock(return_value=existing)
        client.patch_text_ad = AsyncMock(return_value={"id": "ad-existing"})

        payload = {
            "externalId": "c-001",
            "createdBy": "agent-siteplug",
            "title": "T",
            "description": "D",
            "link": "https://example.com",
        }
        await client.upsert_text_ad(payload)

        patch_payload = client.patch_text_ad.call_args.args[1]
        assert patch_payload["externalId"] == "c-001"
        assert patch_payload["title"] == "T"
        assert patch_payload["description"] == "D"
        assert patch_payload["link"] == "https://example.com"


# ---------------------------------------------------------------------------
# build_text_ad_payload()
# ---------------------------------------------------------------------------


class TestBuildTextAdPayload:
    def _make_creative(
        self,
        creative_id: str = "creative-001",
        title: str = "Buy Now",
        description: str = "Great deal",
        click_url: str = "https://example.com/buy",
        display_url: str | None = "example.com",
        country: str = "DE",
        content_source: str = "affilizz",
    ) -> dict:
        assets: dict = {
            "title": {"content": title},
            "description": {"content": description},
            "click_url": {"url": click_url},
            "country": {"content": country},
            "content_source": {"content": content_source},
        }
        if display_url is not None:
            assets["display_url"] = {"content": display_url}
        return {"creative_id": creative_id, "assets": assets}

    def test_maps_all_required_fields(self):
        creative = self._make_creative()
        shop = _make_shop_info(shop_id="s1", shop_name="My Shop", shop_domain="example.com")

        payload = build_text_ad_payload(creative, shop, agent_id="agent-siteplug")

        assert payload["externalId"] == "creative-001"
        assert payload["createdBy"] == "agent-siteplug"
        assert payload["title"] == "Buy Now"
        assert payload["description"] == "Great deal"
        assert payload["link"] == "https://example.com/buy"
        assert payload["shopId"] == "s1"
        assert payload["shopName"] == "My Shop"
        assert payload["shopDomain"] == "example.com"

    def test_country_from_asset_takes_precedence(self):
        creative = self._make_creative(country="FR")
        shop = _make_shop_info(country_codes=["DE"])

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["countryCodes"] == ["FR"]

    def test_falls_back_to_shop_country_codes_when_asset_country_empty(self):
        creative = self._make_creative(country="")
        shop = _make_shop_info(country_codes=["DE", "AT"])

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["countryCodes"] == ["DE", "AT"]

    def test_display_url_included_when_present(self):
        creative = self._make_creative(display_url="example.com/shop")
        shop = _make_shop_info()

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["displayLink"] == "example.com/shop"

    def test_display_url_is_none_when_absent(self):
        creative = self._make_creative(display_url=None)
        shop = _make_shop_info()

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["displayLink"] is None

    def test_external_metadata_set_when_content_source_present(self):
        creative = self._make_creative(content_source="affilizz")
        shop = _make_shop_info()

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["externalMetadata"] == {"contentSource": "affilizz"}

    def test_external_metadata_is_none_when_content_source_empty(self):
        creative = self._make_creative(content_source="")
        shop = _make_shop_info()

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["externalMetadata"] is None

    def test_handles_missing_assets_gracefully(self):
        creative = {"creative_id": "c-001", "assets": {}}
        shop = _make_shop_info(country_codes=["DE"])

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert payload["title"] == ""
        assert payload["description"] == ""
        assert payload["link"] == ""
        assert payload["displayLink"] is None
        assert payload["countryCodes"] == ["DE"]
        assert payload["externalMetadata"] is None

    def test_creation_channel_not_in_payload(self):
        """creationChannel is server-forced and must NOT be sent by the client."""
        creative = self._make_creative()
        shop = _make_shop_info()

        payload = build_text_ad_payload(creative, shop, agent_id="agent")

        assert "creationChannel" not in payload
