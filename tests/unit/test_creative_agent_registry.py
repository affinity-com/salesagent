"""Unit tests for Creative Agent Registry adcp library integration."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import AnyUrl

from src.core.creative_agent_registry import (
    _KNOWN_ASSET_TYPES,
    CreativeAgent,
    CreativeAgentRegistry,
    GenerativeBuildResult,
)
from src.core.exceptions import (
    AdCPAdapterError,
    AdCPAuthenticationError,
    AdCPServiceUnavailableError,
    AdCPValidationError,
)
from src.core.schemas import FormatId
from tests.factories.creative_asset import build_assets, image_spec

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_agent_client(*, result: object = None, side_effect: BaseException | None = None, capture: list | None = None):
    """Return a mock ``ADCPMultiAgentClient`` for the ``build_creative`` path.

    ONE double for the whole call chain the registry uses
    (``_build_adcp_client(...)`` → ``.agent(name)`` → ``.build_creative(request)``).
    Every test configures it through this helper, so a change to that chain — the
    kind this PR just made — is a single edit here instead of eight hand-rolled
    ``Mock`` / ``AsyncMock`` / ``agent()`` trios failing separately.

    Args:
        result: what ``build_creative`` returns (a model or a dict).
        side_effect: raise this instead of returning.
        capture: if given, every request is appended to it (and ``result`` is
            returned).
    """
    if capture is not None:

        async def _capture(request):
            capture.append(request)
            return result if result is not None else {"status": "draft"}

        agent_client = Mock()
        agent_client.build_creative = _capture
    else:
        agent_client = Mock()
        agent_client.build_creative = AsyncMock(
            return_value=result if result is not None else {"status": "draft"}, side_effect=side_effect
        )

    adcp_client = Mock()
    adcp_client.agent = Mock(return_value=agent_client)
    return adcp_client


def _make_capture_client():
    """``(captured_requests, mock_adcp_client)`` — the capture flavour of :func:`_make_agent_client`."""
    captured: list = []
    return captured, _make_agent_client(capture=captured)


GENERATIVE_FORMAT = FormatId(agent_url="https://creative.example.com", id="display_300x250_generative")


@pytest.fixture
def dials_the_agent(monkeypatch):
    """Turn OFF testing mode so build_creative actually dials the client.

    ``ADCP_TESTING=true`` (set for the whole test session by ``tests/conftest.py``)
    makes every registry method serve checked-in/derived data instead of making an
    external call — which is the point in CI, and exactly what the tests below must
    NOT exercise: they grade the request the registry BUILDS and the errors it
    translates. Without this the assertions pass against a branch that never
    constructs a ``BuildCreativeRequest``.
    """
    monkeypatch.setenv("ADCP_TESTING", "false")


class TestCacheKeyAcceptsAnyUrl:
    """Regression tests for #1106: _cache_key must accept Pydantic AnyUrl.

    FormatId.agent_url is AnyUrl (not a str subclass in Pydantic v2).
    When GAM line item creation resolves formats, the AnyUrl flows through
    format_resolver → creative_agent_registry._cache_key → yarl.URL().
    yarl.URL() rejects non-str input with TypeError.
    """

    def test_cache_key_accepts_pydantic_anyurl(self):
        """_cache_key must not crash when given AnyUrl instead of str."""
        registry = CreativeAgentRegistry()
        agent_url = AnyUrl("https://creative.adcontextprotocol.org/")
        result = registry._cache_key(agent_url)
        assert result == "https://creative.adcontextprotocol.org"

    def test_cache_key_normalizes_anyurl_same_as_str(self):
        """AnyUrl and equivalent str must produce the same cache key."""
        registry = CreativeAgentRegistry()
        str_key = registry._cache_key("https://creative.adcontextprotocol.org/")
        anyurl_key = registry._cache_key(AnyUrl("https://creative.adcontextprotocol.org/"))
        assert str_key == anyurl_key

    @pytest.mark.asyncio
    async def test_get_format_accepts_anyurl_agent_url(self, monkeypatch):
        """get_format must not crash when agent_url is AnyUrl (GAM line item path)."""
        monkeypatch.delenv("ADCP_TESTING", raising=False)
        registry = CreativeAgentRegistry()

        # Patch _fetch to avoid real HTTP — we only test the cache_key path
        async def mock_fetch(*args, **kwargs):
            return []

        monkeypatch.setattr(registry, "_fetch_formats_from_agent", mock_fetch)

        result = await registry.get_format(AnyUrl("https://creative.adcontextprotocol.org/"), "display_300x250_image")
        assert result is None  # Not found, but no TypeError


class TestCreativeAgentRegistry:
    """Test suite for Creative Agent Registry adcp integration."""

    def test_build_adcp_client_with_custom_auth_header(self):
        """Test _build_adcp_client correctly maps custom auth headers."""
        registry = CreativeAgentRegistry()

        # Test agent with custom auth header
        test_agents = [
            CreativeAgent(
                agent_url="https://test-agent.example.com/mcp",
                name="Test Agent",
                enabled=True,
                priority=1,
                auth={"type": "bearer", "credentials": "test-token-123"},
                auth_header="Authorization",  # Custom header
            )
        ]

        client = registry._build_adcp_client(test_agents)

        # Verify client was created
        assert client is not None

        # Verify agent config is correct (check via client._agents if accessible)
        # Note: We can't easily verify internal AgentConfig without accessing private attrs
        # But we can verify the method doesn't raise and returns a client
        assert hasattr(client, "agent")

    def test_build_adcp_client_with_default_auth_header(self):
        """Test _build_adcp_client uses default x-adcp-auth when no custom header."""
        registry = CreativeAgentRegistry()

        test_agents = [
            CreativeAgent(
                agent_url="https://default-agent.example.com/mcp",
                name="Default Agent",
                enabled=True,
                priority=1,
                auth={"type": "token", "credentials": "token-456"},
                auth_header=None,  # No custom header
            )
        ]

        client = registry._build_adcp_client(test_agents)

        assert client is not None
        assert hasattr(client, "agent")

    def test_build_adcp_client_with_no_auth(self):
        """Test _build_adcp_client handles agents without auth."""
        registry = CreativeAgentRegistry()

        test_agents = [
            CreativeAgent(
                agent_url="https://public-agent.example.com/mcp",
                name="Public Agent",
                enabled=True,
                priority=1,
                auth=None,
                auth_header=None,
            )
        ]

        client = registry._build_adcp_client(test_agents)

        assert client is not None

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_with_adcp_success(self):
        """Test _fetch_formats_from_agent with successful adcp response."""
        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Mock ADCPMultiAgentClient
        mock_client = Mock()
        mock_agent_client = Mock()

        # Mock format data as dicts (as returned by adcp library)
        # Using spec-compliant renders array for dimensions (not top-level dimensions field)
        mock_formats = [
            {
                "format_id": {"agent_url": "https://test-agent.example.com/mcp", "id": "display_300x250"},
                "name": "Display 300x250",
                "type": "display",
                "renders": [{"role": "primary", "dimensions": {"width": 300, "height": 250, "unit": "px"}}],
            },
            {
                "format_id": {"agent_url": "https://test-agent.example.com/mcp", "id": "display_728x90"},
                "name": "Display 728x90",
                "type": "display",
                "renders": [{"role": "primary", "dimensions": {"width": 728, "height": 90, "unit": "px"}}],
            },
        ]

        mock_result = Mock()
        mock_result.status = "completed"
        mock_result.data = Mock()
        mock_result.data.formats = mock_formats

        mock_agent_client.list_creative_formats = AsyncMock(return_value=mock_result)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Call the method
        formats = await registry._fetch_formats_from_agent(mock_client, test_agent, max_width=1920, max_height=1080)

        # Verify results
        assert len(formats) == 2
        assert formats[0].format_id.id == "display_300x250"
        assert formats[1].format_id.id == "display_728x90"

        # Verify agent_url was set
        # Note: Can't directly check since Format is constructed, but method should set it

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_with_async_submission(self):
        """Test _fetch_formats_from_agent handles async webhook submission."""
        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Mock async submission response
        mock_client = Mock()
        mock_agent_client = Mock()

        mock_result = Mock()
        mock_result.status = "submitted"
        mock_result.submitted = Mock()
        mock_result.submitted.webhook_url = "https://webhook.example.com/callback"

        mock_agent_client.list_creative_formats = AsyncMock(return_value=mock_result)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Submitted status is anomalous for list_creative_formats — must raise
        # Fix for : silent return [] masked failures as 'no formats'
        with pytest.raises(AdCPAdapterError, match="Unexpected submitted status"):
            await registry._fetch_formats_from_agent(mock_client, test_agent)

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_handles_auth_error(self):
        """Test _fetch_formats_from_agent handles authentication errors."""
        from adcp.exceptions import ADCPAuthenticationError

        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Mock authentication error
        mock_client = Mock()
        mock_agent_client = Mock()

        auth_error = ADCPAuthenticationError("Invalid credentials")
        mock_agent_client.list_creative_formats = AsyncMock(side_effect=auth_error)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Should re-raise as typed src.core.AdCPAuthenticationError (wrapped)
        with pytest.raises(AdCPAuthenticationError, match="Authentication failed"):
            await registry._fetch_formats_from_agent(mock_client, test_agent)

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_handles_timeout_error(self):
        """Test _fetch_formats_from_agent handles timeout errors."""
        from adcp.exceptions import ADCPTimeoutError

        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Mock timeout error
        mock_client = Mock()
        mock_agent_client = Mock()

        timeout_error = ADCPTimeoutError(
            message="Request timed out",
            agent_id="Test Agent",
            agent_uri="https://test-agent.example.com/mcp",
            timeout=30.0,
        )
        mock_agent_client.list_creative_formats = AsyncMock(side_effect=timeout_error)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Should raise typed AdCPServiceUnavailableError with timeout message
        with pytest.raises(AdCPServiceUnavailableError, match="Request timed out"):
            await registry._fetch_formats_from_agent(mock_client, test_agent)

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_handles_connection_error(self):
        """Test _fetch_formats_from_agent handles connection errors."""
        from adcp.exceptions import ADCPConnectionError

        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Mock connection error
        mock_client = Mock()
        mock_agent_client = Mock()

        conn_error = ADCPConnectionError("Connection refused")
        mock_agent_client.list_creative_formats = AsyncMock(side_effect=conn_error)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Should raise typed AdCPServiceUnavailableError
        with pytest.raises(AdCPServiceUnavailableError, match="Connection failed"):
            await registry._fetch_formats_from_agent(mock_client, test_agent)

    @pytest.mark.asyncio
    async def test_fetch_formats_from_agent_handles_library_format(self):
        """Test _fetch_formats_from_agent converts library Format to local Format via model_validate."""
        from adcp.types import Format as LibraryFormat

        registry = CreativeAgentRegistry()

        test_agent = CreativeAgent(
            agent_url="https://test-agent.example.com/mcp",
            name="Test Agent",
            enabled=True,
            priority=1,
        )

        # Use a real library Format object (as returned by adcp client)
        mock_client = Mock()
        mock_agent_client = Mock()

        library_format = LibraryFormat(
            format_id={"agent_url": "https://test-agent.example.com/mcp", "id": "display_300x250"},
            name="Display 300x250",
            type="display",
            renders=[{"role": "primary", "dimensions": {"width": 300, "height": 250}}],
        )

        mock_result = Mock()
        mock_result.status = "completed"
        mock_result.data = Mock()
        mock_result.data.formats = [library_format]

        mock_agent_client.list_creative_formats = AsyncMock(return_value=mock_result)
        mock_client.agent = Mock(return_value=mock_agent_client)

        # Call the method
        formats = await registry._fetch_formats_from_agent(mock_client, test_agent)

        # Verify format was constructed as our local Format subclass
        assert len(formats) == 1
        assert formats[0].format_id.id == "display_300x250"


class TestKnownAssetTypes:
    """_KNOWN_ASSET_TYPES includes 'url' (Change 4).

    AdCP 3.1 adds 'url' as a valid asset type for text_ad_search formats.
    The tolerant ingestion must not reject formats that use 'url' assets.
    """

    def test_url_in_known_asset_types(self):
        """'url' must be in _KNOWN_ASSET_TYPES after Change 4."""
        assert "url" in _KNOWN_ASSET_TYPES, (
            "'url' must be in _KNOWN_ASSET_TYPES so formats with url assets "
            "are not rejected by the tolerant ingestion path"
        )

    def test_known_asset_types_is_frozenset(self):
        """_KNOWN_ASSET_TYPES must be a frozenset (immutable, hashable)."""
        assert isinstance(_KNOWN_ASSET_TYPES, frozenset), (
            "_KNOWN_ASSET_TYPES must be a frozenset so it cannot be mutated at runtime"
        )

    def test_known_asset_types_covers_every_enum_member(self):
        """No AssetContentType member may be missing from _KNOWN_ASSET_TYPES.

        A member left out would make the tolerant-ingestion path treat formats
        using it as "unknown additive" and silently DROP them, even though the
        pinned SDK models them.

        The enum is iterated, never listed: an annotation-walk over Format.assets
        collects nothing under the Annotated[…, Discriminator] shape the SDK uses,
        so production derives from AssetContentType — and this test derives from it
        too, so neither needs an edit when AdCP adds an asset type. It still catches
        the regression that matters: replacing the derivation with a partial
        hand-written list.
        """
        from adcp.types import AssetContentType

        missing = {member.value for member in AssetContentType} - set(_KNOWN_ASSET_TYPES)
        assert not missing, (
            f"_KNOWN_ASSET_TYPES is missing AssetContentType member(s) {sorted(missing)} — "
            f"formats using them would be dropped as unknown-additive by _validate_formats_tolerant"
        )

    def test_zip_in_known_asset_types(self):
        """'zip' must be in _KNOWN_ASSET_TYPES.

        'zip' is a valid asset_type Literal on the SDK's individual-asset shapes
        (Assets32/Assets43 in the generated union) but is absent from the
        AssetContentType response enum, so deriving _KNOWN_ASSET_TYPES from the
        enum alone silently drops it.
        """
        assert "zip" in _KNOWN_ASSET_TYPES, (
            "'zip' must be in _KNOWN_ASSET_TYPES — it's a real SDK asset_type Literal "
            "not covered by the AssetContentType enum"
        )

    def test_card_in_known_asset_types(self):
        """'card' must be in _KNOWN_ASSET_TYPES.

        'card' is the asset_type discriminator for RepeatableAssetGroup member
        assets (CardAsset) but is absent from the AssetContentType response enum.
        """
        assert "card" in _KNOWN_ASSET_TYPES, (
            "'card' must be in _KNOWN_ASSET_TYPES — it's a real SDK asset_type Literal "
            "not covered by the AssetContentType enum"
        )

    def test_zip_and_card_union_is_still_necessary(self):
        """The explicit zip/card union must still be earning its place.

        Production unions ``{"zip", "card"}`` on top of the enum precisely because
        AssetContentType omits them. If a pin bump adds either to the enum, that
        union becomes dead code — fail here so it is removed rather than lingering
        as a stale special case.

        (Replaces a hardcoded 17-name snapshot of the whole set: the snapshot
        duplicated the enum listing in the creative schema-compliance obligations
        test, needed an edit on every pin bump, and its stated claim about the
        Format.assets union was wrong — the union discriminates
        ``repeatable_group`` on ``item_type``, not ``asset_type``.)
        """
        from adcp.types import AssetContentType

        enum_values = {member.value for member in AssetContentType}
        overlap = enum_values & {"zip", "card"}
        assert not overlap, (
            f"AssetContentType now includes {sorted(overlap)} — drop it from the explicit "
            f"union in _known_asset_types(), which exists only to cover the enum's omissions"
        )


class TestBuildCreativeUsesADCPClient:
    """build_creative uses ADCPMultiAgentClient + BuildCreativeRequest (Change 3).

    Verifies that:
    - gemini_api_key is NOT a parameter (removed in Change 3)
    - ADCPMultiAgentClient is used for the call
    - BuildCreativeRequest is constructed with target_format_id and idempotency_key
    - brand string is converted to BrandRef dict before the request
    - the response crosses the boundary as a typed GenerativeBuildResult
    """

    @pytest.mark.asyncio
    async def test_build_creative_no_gemini_api_key_param(self):
        """build_creative must NOT accept gemini_api_key parameter (Change 3)."""
        import inspect

        registry = CreativeAgentRegistry()
        sig = inspect.signature(registry.build_creative)
        assert "gemini_api_key" not in sig.parameters, (
            "build_creative must not accept gemini_api_key — "
            "Change 3 removed this dependency in favour of ADCPMultiAgentClient"
        )

    @pytest.mark.asyncio
    async def test_build_creative_takes_domain_values_only(self):
        """The signature carries domain values, not pre-built wire objects or dead inputs.

        ``creative_manifest`` is rendered by the registry from ``format_id`` +
        ``assets`` (protocol framing belongs to this adapter), and the pre-3.1
        ``promoted_offerings`` / ``context_id`` arguments are gone: neither has a
        home in ``media-buy/build-creative-request.json @ 3.1.1``, and both were
        accepted-and-ignored, which silently dropped buyer input (#2143).
        """
        import inspect

        params = set(inspect.signature(CreativeAgentRegistry.build_creative).parameters)

        assert {"format_id", "message", "assets", "brand"} <= params
        assert not params & {"creative_manifest", "promoted_offerings", "context_id"}, (
            "build_creative must not accept wire objects or parameters its body never reads"
        )

    @pytest.mark.asyncio
    async def test_build_creative_uses_adcp_multi_agent_client(self, dials_the_agent):
        """build_creative must use ADCPMultiAgentClient, not raw MCP client."""
        from unittest.mock import ANY

        registry = CreativeAgentRegistry()
        adcp_client = _make_agent_client(result={"status": "draft", "context_id": "ctx-1"})

        with patch.object(registry, "_build_adcp_client", return_value=adcp_client) as mock_build:
            result = await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        # _build_adcp_client must have been called with a list of CreativeAgent objects
        mock_build.assert_called_once_with(ANY)
        # build_creative on the agent client must have been called with a BuildCreativeRequest
        adcp_client.agent.return_value.build_creative.assert_called_once_with(ANY)
        assert result is not None
        assert result.context_id == "ctx-1"

    @pytest.mark.asyncio
    async def test_build_creative_passes_idempotency_key(self, dials_the_agent):
        """build_creative must pass idempotency_key in the BuildCreativeRequest."""
        registry = CreativeAgentRegistry()
        captured_request, mock_adcp_client = _make_capture_client()

        with patch.object(registry, "_build_adcp_client", return_value=mock_adcp_client):
            await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        assert len(captured_request) == 1
        req = captured_request[0]
        assert req.idempotency_key is not None, (
            "BuildCreativeRequest must include idempotency_key (required by AdCP 3.1)"
        )
        assert len(req.idempotency_key) > 0

    @pytest.mark.asyncio
    async def test_build_creative_brand_str_converted_to_ref(self, dials_the_agent):
        """build_creative converts brand string to typed BrandReference before the request."""
        from adcp.types import BrandReference

        registry = CreativeAgentRegistry()
        captured_request, mock_adcp_client = _make_capture_client()

        with patch.object(registry, "_build_adcp_client", return_value=mock_adcp_client):
            await registry.build_creative(
                format_id=GENERATIVE_FORMAT,
                message="Build a banner ad",
                brand="https://advertiser.example.com/brand",
            )

        assert len(captured_request) == 1
        req = captured_request[0]
        # brand must be a typed BrandReference, not a raw string or dict
        assert req.brand is not None, "brand must be forwarded to BuildCreativeRequest"
        assert isinstance(req.brand, BrandReference), "brand must be a typed BrandReference (not a raw string or dict)"
        assert req.brand.domain == "advertiser.example.com"

    @pytest.mark.asyncio
    async def test_request_identity_and_manifest_identity_are_one_value(self, dials_the_agent):
        """``target_format_id`` and the manifest's ``format_id`` must serialize identically.

        Both are rendered from the single ``format_id`` argument. A hand-built
        canonical string next to a pydantic-serialized ``AnyUrl`` (which adds the
        trailing slash for a path-less URL) put two spellings of one agent_url in
        one request — the drift ``core/format-id.json``'s canonicalization MUST
        exists to stop.
        """
        registry = CreativeAgentRegistry()
        captured_request, mock_adcp_client = _make_capture_client()

        with patch.object(registry, "_build_adcp_client", return_value=mock_adcp_client):
            await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        req = captured_request[0]
        wire = req.model_dump(mode="json")
        assert wire["creative_manifest"]["format_id"] == wire["target_format_id"]

    @pytest.mark.asyncio
    async def test_build_creative_returns_typed_result(self, dials_the_agent):
        """build_creative returns a typed GenerativeBuildResult, not an untyped dict.

        Callers read ``result.status`` / ``result.creative_output`` — a dict would
        put them back on the stringly-typed ``.get()`` chains the typed-SDK
        migration exists to remove.
        """
        registry = CreativeAgentRegistry()
        # A REAL pydantic model, not a Mock with a model_dump attribute: the
        # registry discriminates with isinstance(result, BaseModel) precisely so a
        # duck-typed stand-in cannot stand in for an SDK response model.
        sdk_result = GenerativeBuildResult(status="draft", context_id="ctx-abc")
        adcp_client = _make_agent_client(result=sdk_result)

        with patch.object(registry, "_build_adcp_client", return_value=adcp_client):
            result = await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        assert isinstance(result, GenerativeBuildResult)
        assert result.status == "draft"
        assert result.context_id == "ctx-abc"

    @pytest.mark.asyncio
    async def test_empty_response_is_none(self, dials_the_agent):
        """An agent that returns no payload yields None — "nothing to store", not a default build."""
        registry = CreativeAgentRegistry()
        adcp_client = _make_agent_client(result={})

        with patch.object(registry, "_build_adcp_client", return_value=adcp_client):
            result = await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sdk_error_name,sdk_kwargs,expected_type,expected_code,expected_recovery",
        [
            pytest.param(
                "ADCPAuthenticationError",
                {"agent_id": "agent-1"},
                AdCPAuthenticationError,
                "AUTH_REQUIRED",
                "correctable",
                id="auth",
            ),
            pytest.param(
                "ADCPTimeoutError",
                {"agent_id": "agent-1"},
                AdCPServiceUnavailableError,
                "SERVICE_UNAVAILABLE",
                "transient",
                id="timeout",
            ),
        ],
    )
    async def test_sdk_error_translated_to_internal_typed_error(
        self, dials_the_agent, sdk_error_name, sdk_kwargs, expected_type, expected_code, expected_recovery
    ):
        """SDK exceptions become internal typed errors carrying their own code + recovery.

        Mirrors ``_fetch_formats_from_agent``'s translation via
        ``raise_mapped_adcp_error``: without it a blanket ``except`` would
        classify every agent failure as a generic retryable outage. Recovery
        follows the pinned enum (``enums/error-code.json @ 3.1.1``:
        ``AUTH_REQUIRED`` → correctable, ``SERVICE_UNAVAILABLE`` → transient), so
        the buyer is told to fix credentials rather than to retry them.
        """
        import adcp.exceptions

        sdk_error = getattr(adcp.exceptions, sdk_error_name)("agent failed", **sdk_kwargs)

        registry = CreativeAgentRegistry()
        adcp_client = _make_agent_client(side_effect=sdk_error)

        with patch.object(registry, "_build_adcp_client", return_value=adcp_client):
            with pytest.raises(expected_type) as exc_info:
                await registry.build_creative(format_id=GENERATIVE_FORMAT, message="Build a banner ad")

        assert exc_info.value.error_code == expected_code
        assert exc_info.value.recovery == expected_recovery


class TestBuildCreativeManifestValidation:
    """The registry renders the manifest via model_validate (not model_construct).

    Covers the review's "untested strictness" gap: realistic complete assets must
    reach the request as a typed manifest, and realistic partial/malformed assets
    must raise rather than silently forwarding a broken manifest to the agent.
    """

    @pytest.mark.asyncio
    async def test_realistic_complete_assets_forwarded_as_typed_manifest(self, dials_the_agent):
        """Valid assets are rendered into a typed CreativeManifest on the request."""
        from adcp.types.generated_poc.core.creative_manifest import CreativeManifest

        registry = CreativeAgentRegistry()
        captured_request, mock_adcp_client = _make_capture_client()

        with patch.object(registry, "_build_adcp_client", return_value=mock_adcp_client):
            await registry.build_creative(
                format_id=FormatId(agent_url="https://creative.example.com", id="display_300x250"),
                message="Build a banner ad",
                assets=build_assets(image_spec("main_image")),
            )

        assert len(captured_request) == 1
        req = captured_request[0]
        assert req.creative_manifest is not None
        assert isinstance(req.creative_manifest, CreativeManifest), (
            "creative_manifest must be a typed CreativeManifest (validated, not constructed unchecked)"
        )
        assert req.creative_manifest.model_dump(mode="json")["assets"]["main_image"]["asset_type"] == "image"

    @pytest.mark.asyncio
    async def test_realistic_partial_assets_raise(self, dials_the_agent):
        """A partial asset (image missing required width/height) raises rather than
        silently forwarding a broken manifest to the creative agent.

        model_validate() (not model_construct()) enforces the asset schema's field
        validators — this pins that strictness against silent regression to a lenient
        construction path. The rejection is TYPED (``VALIDATION_ERROR`` /
        ``correctable``): the assets are buyer input, and a bare
        ``pydantic.ValidationError`` is not an ``AdCPError``, so the sync path would
        report it as "creative agent unreachable … retry recommended".
        """

        registry = CreativeAgentRegistry()
        adcp_client = _make_agent_client()

        with patch.object(registry, "_build_adcp_client", return_value=adcp_client):
            with pytest.raises(AdCPValidationError) as exc_info:
                await registry.build_creative(
                    format_id=FormatId(agent_url="https://creative.example.com", id="display_300x250"),
                    message="Build a banner ad",
                    # Missing required width/height on the image asset.
                    assets={"main_image": {"asset_type": "image", "url": "https://example.com/img.png"}},
                )

        assert exc_info.value.error_code == "VALIDATION_ERROR"
        assert exc_info.value.recovery == "correctable"
