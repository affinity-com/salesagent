"""Behavioral tests for create_media_buy transport boundary serialization.

Covers the push_notification_config serialization obligations: both MCP and A2A
wrappers must use model_dump(mode='json') so that Pydantic v2 AnyUrl fields and
enum instances are converted to plain Python strings before reaching _impl and
SQLAlchemy String columns.

Also covers brand propagation (Change 5): to_brand_reference() must convert
plain brand strings to AdCP BrandRef-shaped dicts (bare hostname, no scheme/path).

The media_buy_brand propagation obligation (that _create_media_buy_impl forwards
req.brand to process_and_upload_package_creatives) lives in the integration
sibling, tests/integration/test_create_media_buy_behavioral.py, where the
MediaBuyCreateEnv harness drives the real pipeline instead of hand-rolled mocks.

Obligation IDs:
  UC-002-TRANSPORT-PNC-SERIALIZATION-01  (MCP wrapper)
  UC-002-TRANSPORT-PNC-SERIALIZATION-02  (A2A wrapper)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from adcp.types import BrandReference

from src.core.schema_helpers import to_brand_reference
from tests.helpers.create_media_buy_capture import capture_a2a_forwarded_pnc, capture_mcp_forwarded_pnc


class TestMCPWrapperPncJsonSerialization:
    """MCP wrapper must serialize PushNotificationConfig with mode='json'.

    Regression: plain model_dump() preserves AnyUrl objects that SQLAlchemy
    String columns cannot coerce, raising StatementError at flush.
    """

    @pytest.mark.asyncio
    async def test_mcp_wrapper_url_is_plain_str_not_anyurl(self):
        """Covers: UC-002-TRANSPORT-PNC-SERIALIZATION-01

        When the MCP wrapper serializes PushNotificationConfig to a dict,
        the url field must be a plain str (not a Pydantic AnyUrl object) so
        that SQLAlchemy String columns can persist it without StatementError.
        """
        from adcp import PushNotificationConfig

        pnc = PushNotificationConfig(
            url="https://buyer.example.com/webhook",
            authentication={"credentials": "a" * 32, "schemes": ["Bearer"]},
        )
        forwarded = await capture_mcp_forwarded_pnc(pnc)

        assert forwarded is not None, "MCP wrapper did not forward push_notification_config to _impl"
        assert isinstance(forwarded, dict), f"push_notification_config must be a dict, got {type(forwarded).__name__}"

        url = forwarded.get("url")
        assert isinstance(url, str), (
            f"url must be a plain str after model_dump(mode='json'), got {type(url).__name__!r}. "
            "This indicates model_dump() was used instead of model_dump(mode='json'), "
            "which preserves AnyUrl objects and causes SQLAlchemy StatementError."
        )
        assert url == "https://buyer.example.com/webhook", f"url value mismatch: {url!r}"

    @pytest.mark.asyncio
    async def test_mcp_wrapper_enum_schemes_are_plain_strings(self):
        """Covers: UC-002-TRANSPORT-PNC-SERIALIZATION-01

        When the MCP wrapper serializes PushNotificationConfig, enum fields
        such as authentication.schemes must be plain strings, not enum instances,
        so SQLAlchemy can persist them without coercion errors.
        """
        from adcp import PushNotificationConfig

        pnc = PushNotificationConfig(
            url="https://buyer.example.com/webhook",
            authentication={"credentials": "a" * 32, "schemes": ["Bearer"]},
        )
        forwarded = await capture_mcp_forwarded_pnc(pnc)
        assert forwarded is not None

        auth = forwarded.get("authentication", {})
        schemes = auth.get("schemes", [])
        for scheme in schemes:
            assert isinstance(scheme, str), (
                f"authentication.schemes entries must be plain str after model_dump(mode='json'), "
                f"got {type(scheme).__name__!r} — enum instances cause SQLAlchemy coercion errors."
            )


class TestA2AWrapperPncJsonSerialization:
    """A2A wrapper must serialize PushNotificationConfig with mode='json'.

    Regression: plain model_dump() preserves AnyUrl objects that SQLAlchemy
    String columns cannot coerce, raising StatementError at flush.
    """

    @pytest.mark.asyncio
    async def test_a2a_wrapper_url_is_plain_str_not_anyurl(self):
        """Covers: UC-002-TRANSPORT-PNC-SERIALIZATION-02

        When the A2A wrapper (create_media_buy_raw) receives a PushNotificationConfig
        model instance and serializes it to a dict, the url field must be a plain str
        (not a Pydantic AnyUrl object) so SQLAlchemy String columns can persist it.
        """
        from adcp import PushNotificationConfig

        pnc = PushNotificationConfig(
            url="https://buyer.example.com/webhook",
            authentication={"credentials": "a" * 32, "schemes": ["Bearer"]},
        )
        forwarded = await capture_a2a_forwarded_pnc(pnc)

        assert forwarded is not None, "A2A wrapper did not forward push_notification_config to _impl"
        assert isinstance(forwarded, dict), f"push_notification_config must be a dict, got {type(forwarded).__name__}"

        url = forwarded.get("url")
        assert isinstance(url, str), (
            f"url must be a plain str after model_dump(mode='json'), got {type(url).__name__!r}. "
            "This indicates model_dump() was used instead of model_dump(mode='json'), "
            "which preserves AnyUrl objects and causes SQLAlchemy StatementError."
        )
        assert url == "https://buyer.example.com/webhook", f"url value mismatch: {url!r}"

    @pytest.mark.asyncio
    async def test_a2a_wrapper_passthrough_dict_unchanged(self):
        """Covers: UC-002-TRANSPORT-PNC-SERIALIZATION-02

        When the A2A wrapper receives push_notification_config already as a plain
        dict (the normal A2A JSON path), it must pass it through unchanged without
        re-serializing it.
        """
        pnc_dict = {
            "url": "https://buyer.example.com/webhook",
            "authentication": {"credentials": "a" * 32, "schemes": ["Bearer"]},
        }
        forwarded = await capture_a2a_forwarded_pnc(pnc_dict)

        assert forwarded is not None
        assert isinstance(forwarded, dict)
        assert forwarded["url"] == "https://buyer.example.com/webhook"
        assert forwarded["authentication"]["schemes"] == ["Bearer"]

    @pytest.mark.asyncio
    async def test_a2a_wrapper_enum_schemes_are_plain_strings(self):
        """Covers: UC-002-TRANSPORT-PNC-SERIALIZATION-02

        When the A2A wrapper serializes a PushNotificationConfig model, enum
        fields such as authentication.schemes must be plain strings.
        """
        from adcp import PushNotificationConfig

        pnc = PushNotificationConfig(
            url="https://buyer.example.com/webhook",
            authentication={"credentials": "a" * 32, "schemes": ["Bearer"]},
        )
        forwarded = await capture_a2a_forwarded_pnc(pnc)
        assert forwarded is not None

        auth = forwarded.get("authentication", {})
        schemes = auth.get("schemes", [])
        for scheme in schemes:
            assert isinstance(scheme, str), (
                f"authentication.schemes entries must be plain str after model_dump(mode='json'), "
                f"got {type(scheme).__name__!r} — enum instances cause SQLAlchemy coercion errors."
            )


class TestToBrandReference:
    """``to_brand_reference`` is the ONE str/dict/model → BrandReference converter.

    One home for the converter's contract, because there is one converter: the
    creative-build path and ``create_media_buy``'s request builder both route
    through it (``media_buy_create._build_create_media_buy_request`` no longer
    constructs ``BrandReference(domain=brand)`` raw), so scheme-bearing/uppercase
    shorthand is accepted identically on both.

    ``brand-ref.json @ 3.1.1`` requires ``domain`` to be a bare hostname — no
    scheme, no path, no query, no fragment — so the converter strips every URL
    component and lowercases the host. It returns a TYPED ``BrandReference``, not
    a loose dict: the brand stays typed end-to-end inside the application and is
    serialized only at the DB/SDK boundary.
    """

    @pytest.mark.parametrize(
        "raw,expected_domain",
        [
            pytest.param("example.com", "example.com", id="bare-domain"),
            pytest.param("https://example.com", "example.com", id="https-scheme"),
            pytest.param("http://example.com", "example.com", id="http-scheme"),
            pytest.param("https://example.com/path/to/page", "example.com", id="path"),
            pytest.param("https://example.com/path?q=1&foo=bar", "example.com", id="query"),
            pytest.param("https://example.com/page#section", "example.com", id="fragment"),
            pytest.param("https://example.com/path?q=1#anchor", "example.com", id="all-components"),
            pytest.param("https://Example.COM/Path", "example.com", id="uppercase-host"),
            pytest.param("https://ads.example.com/campaign", "ads.example.com", id="subdomain-preserved"),
            pytest.param({"domain": "acme.com"}, "acme.com", id="dict-input"),
            pytest.param(BrandReference(domain="acme.com"), "acme.com", id="model-input"),
        ],
    )
    def test_normalizes_to_bare_lowercase_domain(self, raw, expected_domain):
        result = to_brand_reference(raw)

        assert isinstance(result, BrandReference), "the converter returns a typed BrandReference, not a dict"
        assert result.domain == expected_domain

    def test_invalid_dict_raises_typed_correctable_error(self):
        """A malformed dict brand raises AdCPValidationError (correctable), not a raw
        pydantic ValidationError crash.
        """
        from src.core.exceptions import AdCPValidationError

        with pytest.raises(AdCPValidationError) as exc_info:
            to_brand_reference({"domain": 12345})  # wrong type — not coercible to str

        assert exc_info.value.recovery == "correctable"

    def test_media_buy_create_raw_construction_uses_same_converter(self):
        """media_buy_create._build_create_media_buy_request routes brand through
        to_brand_reference(), matching the creative-build path's normalization —
        pins the "one converter" invariant against regressing to a raw
        BrandReference(domain=brand) construction.
        """
        from src.core.tools.media_buy_create import _build_create_media_buy_request

        req = _build_create_media_buy_request(
            brand="https://Example.COM/path",
            packages=None,
            start_time="asap",
            end_time=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            po_number=None,
            reporting_webhook=None,
            context=None,
            ext=None,
            account=None,
            idempotency_key="test-idempotency-key-0001",
            paused=None,
        )
        assert req.brand is not None
        assert req.brand.domain == "example.com"
