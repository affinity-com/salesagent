"""Unit tests for the creative-agent seam in ``_processing.py`` (Change 1 & 2).

Covers:
- ``_find_format`` / ``_resolve_agent_format``: canonical composite
  (agent_url, id) key lookup per AdCP URL canonicalization (spec MUST —
  ``core/format-id.json`` / ``reference/url-canonicalization.mdx``): lowercased
  host, default ports stripped, trailing slash stripped. Transport-suffix paths
  (``/mcp``, ``/a2a``) are NOT stripped — canonicalization must preserve the
  path, so a reference carrying such a suffix is a genuinely different agent_url.
- ``_render_creative_manifest`` (the registry's single manifest renderer):
  AdCP-compliant ``creative_manifest`` structure (``format_id`` as an object,
  ``assets`` always present, no ``creative_id``/``name``).

The format fixtures are REAL ``Format`` models, not ``Mock``s: production
branches on ``format_obj.output_format_ids`` (generative vs static), and a
``Mock`` auto-creates that attribute as truthy, which silently routes a
static-format test down the generative path. The legacy shape (bare-string
``format_id`` with a top-level ``agent_url``) cannot be expressed as a
``Format``, so it gets an explicit stand-in that carries exactly those two
attributes and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.core.creative_agent_registry import _render_creative_manifest
from src.core.exceptions import AdCPValidationError
from src.core.schemas import Format, FormatId
from src.core.tools.creatives._processing import _find_format, _resolve_agent_format
from tests.factories.creative_asset import build_assets, image_spec

AGENT = "https://creative.example.com"


def _structured_format(agent_url: str, format_id: str) -> Format:
    """A real SDK-shaped ``Format`` (``format_id`` is a ``FormatId`` object)."""
    return Format(format_id=FormatId(agent_url=agent_url, id=format_id), name="Test Format")


@dataclass(frozen=True)
class _LegacyFormat:
    """The pre-federation format shape: bare-string id + top-level agent_url.

    Still reachable from stored/adapter-provided formats, which is why
    ``_get_format_agent_url`` keeps its runtime shape discrimination. Spelled as
    a two-field object (not a ``Mock``) so an attribute production reads but
    this shape does not define is an ``AttributeError``, not a truthy default.
    """

    agent_url: str
    format_id: str


def _legacy_format(agent_url: str, format_id: str) -> _LegacyFormat:
    return _LegacyFormat(agent_url=agent_url, format_id=format_id)


# The stored side under both shapes. A real ``FormatId`` pre-normalizes host
# case, the default port and the trailing slash at construction, so those rows
# exercise canonicalization end-to-end only for the legacy shape — where the
# agent_url is an unnormalized plain string. Running the same table over both
# shapes is the point: the outcome must not depend on which shape is stored.
FORMAT_SHAPES = [
    pytest.param(_structured_format, id="structured"),
    pytest.param(_legacy_format, id="legacy"),
]

# (stored agent_url, requested agent_url, stored id, requested id, matches)
LOOKUP_CASES = [
    pytest.param(AGENT, AGENT, "display_300x250", "display_300x250", True, id="exact"),
    pytest.param(AGENT, AGENT + "/", "display_300x250", "display_300x250", True, id="requested-trailing-slash"),
    pytest.param(AGENT + "/", AGENT, "display_300x250", "display_300x250", True, id="stored-trailing-slash"),
    pytest.param(AGENT + "/", AGENT + "/", "display_300x250", "display_300x250", True, id="both-trailing-slash"),
    pytest.param(
        "https://Creative.Example.com", AGENT, "display_300x250", "display_300x250", True, id="stored-host-case"
    ),
    pytest.param(AGENT + ":443", AGENT, "display_300x250", "display_300x250", True, id="stored-default-port"),
    pytest.param(AGENT, AGENT + "/mcp", "display_300x250", "display_300x250", False, id="transport-suffix"),
    pytest.param(AGENT, AGENT, "display_300x250", "display_728x90", False, id="id-mismatch"),
    pytest.param(AGENT, "https://other.example.com", "display_300x250", "display_300x250", False, id="host-mismatch"),
]


class TestFindFormat:
    """_find_format matches on the canonical composite (agent_url, id) key.

    AdCP URL canonicalization (RFC 3986 §6.2.2/§6.2.3): URLs differing only by
    trailing slash, host case, or default port must compare equal; a differing
    path (``/mcp``) must not.
    """

    @pytest.mark.parametrize("make_format", FORMAT_SHAPES)
    @pytest.mark.parametrize("stored_url,requested_url,stored_id,requested_id,matches", LOOKUP_CASES)
    def test_lookup_table(self, make_format, stored_url, requested_url, stored_id, requested_id, matches):
        fmt = make_format(stored_url, stored_id)

        result = _find_format([fmt], FormatId(agent_url=requested_url, id=requested_id))

        assert result is (fmt if matches else None), (
            f"stored {stored_url!r}/{stored_id!r} vs requested {requested_url!r}/{requested_id!r}: "
            f"expected {'a match' if matches else 'no match'}"
        )

    def test_empty_list_returns_none(self):
        """Empty format list returns None."""
        assert _find_format([], FormatId(agent_url=AGENT, id="display_300x250")) is None

    def test_first_matching_format_returned(self):
        """When multiple formats match, the first one is returned."""
        fmt_a = _structured_format(AGENT, "display_300x250")
        fmt_b = _structured_format(AGENT, "display_300x250")

        result = _find_format([fmt_a, fmt_b], FormatId(agent_url=AGENT, id="display_300x250"))

        assert result is fmt_a

    def test_selects_correct_format_from_multiple(self):
        """Correct format is selected when multiple formats are present."""
        fmt_a = _structured_format(AGENT, "display_300x250")
        fmt_b = _structured_format(AGENT, "display_728x90")

        result = _find_format([fmt_a, fmt_b], FormatId(agent_url=AGENT, id="display_728x90"))

        assert result is fmt_b


class TestResolveAgentFormat:
    """_resolve_agent_format returns the format plus the ONE identity to dial with.

    Both agent calls (``preview_creative`` / ``build_creative``) are addressed
    with this single ``FormatId``, so a request cannot carry two spellings of
    the same agent_url.
    """

    def test_returns_format_and_canonical_identity(self):
        fmt = _structured_format(AGENT, "display_300x250")

        resolved = _resolve_agent_format([fmt], FormatId(agent_url=AGENT + "/", id="display_300x250"))

        assert resolved is not None
        format_obj, agent_format = resolved
        assert format_obj is fmt
        assert agent_format.id == "display_300x250"
        assert str(agent_format.agent_url).rstrip("/") == AGENT

    def test_unresolvable_reference_returns_none(self):
        """A reference to a format the agent list does not carry resolves to None."""
        fmt = _structured_format(AGENT, "display_300x250")

        assert _resolve_agent_format([fmt], FormatId(agent_url=AGENT, id="display_728x90")) is None

    def test_format_without_agent_url_returns_none(self):
        """A format carrying no agent_url has no agent to dial, so it does not resolve."""

        @dataclass(frozen=True)
        class _NoAgentUrlFormat:
            format_id: str

        assert (
            _resolve_agent_format(
                [_NoAgentUrlFormat("display_300x250")], FormatId(agent_url=AGENT, id="display_300x250")
            )
            is None
        )


class TestRenderCreativeManifest:
    """_render_creative_manifest produces the AdCP-compliant creative_manifest.

    AdCP 3.1 requires ``format_id`` as a structured object (never a bare
    string), ``assets`` always present, and no ``creative_id``/``name`` at the
    top level. Rendering lives in the registry (the adapter that owns the wire
    contract), which is why this is graded against the serialized payload.
    """

    @pytest.fixture
    def manifest(self) -> dict:
        return _render_creative_manifest(
            FormatId(agent_url=AGENT, id="display_300x250"),
            build_assets(image_spec("banner")),
        ).model_dump(mode="json", exclude_none=True)

    def test_format_id_is_structured_object(self, manifest):
        assert isinstance(manifest["format_id"], dict), (
            "format_id must be a structured object (dict), not a bare string"
        )
        assert manifest["format_id"]["id"] == "display_300x250"
        assert str(manifest["format_id"]["agent_url"]).rstrip("/") == AGENT

    def test_assets_are_carried(self, manifest):
        assert manifest["assets"]["banner"]["asset_type"] == "image"

    def test_no_creative_id_or_name(self, manifest):
        assert "creative_id" not in manifest, "AdCP 3.1 removed creative_id from the manifest"
        assert "name" not in manifest, "AdCP 3.1 removed name from the manifest"

    def test_no_url_key_without_one(self, manifest):
        """``url`` is a static-preview extra — absent unless a media URL is passed."""
        assert "url" not in manifest

    def test_assets_empty_dict_when_no_assets(self):
        """A generative build with no buyer assets still sends ``assets`` as ``{}``."""
        manifest = _render_creative_manifest(FormatId(agent_url=AGENT, id="gen"), None).model_dump(
            mode="json", exclude_none=True
        )

        assert manifest["assets"] == {}, "assets must be {} when the creative has none, not None or missing"

    def test_static_preview_url_rides_through(self):
        """The static path's existing media URL reaches the agent as a manifest extra."""
        manifest = _render_creative_manifest(
            FormatId(agent_url=AGENT, id="display_300x250"),
            build_assets(image_spec("banner")),
            url="https://cdn.example.com/banner.png",
        ).model_dump(mode="json", exclude_none=True)

        assert manifest["url"] == "https://cdn.example.com/banner.png"

    def test_malformed_asset_is_rejected_before_the_request_goes_out(self):
        """model_validate (not model_construct) — a bad asset fails here, not at the agent.

        Typed as ``VALIDATION_ERROR`` / ``correctable``: the assets are buyer input,
        so the rejection must not reach the buyer as a retryable agent outage.
        """
        with pytest.raises(AdCPValidationError):
            _render_creative_manifest(
                FormatId(agent_url=AGENT, id="display_300x250"),
                {"banner": {"asset_type": "image"}},  # image asset with no url/width/height
            )
