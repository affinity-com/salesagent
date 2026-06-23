"""Unit tests for _processing.py private helpers.

Covers:
- _find_format(): composite (agent_url, id) matching with URL normalization
- _build_generative_manifest(): AdCP-compliant creative_manifest construction
- Static-path creative_manifest: format_id is full FormatId object, assets always present
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_fmt(agent_url: str, fmt_id: str, output_format_ids: list | None = None) -> Any:
    """Build a minimal format object matching the shape _find_format() expects."""
    fmt = SimpleNamespace()
    fmt.format_id = SimpleNamespace(agent_url=agent_url, id=fmt_id)
    fmt.agent_url = agent_url
    fmt.output_format_ids = output_format_ids
    return fmt


def _make_creative_format(agent_url: str, fmt_id: str) -> Any:
    """Build a minimal FormatId object matching the shape creative.format_id has."""
    return SimpleNamespace(agent_url=agent_url, id=fmt_id)


# ── _find_format tests ────────────────────────────────────────────────────────


class TestFindFormat:
    """Tests for _find_format() — composite (normalized agent_url, id) matching."""

    def _call(self, all_formats, creative_format):
        from src.core.tools.creatives._processing import _find_format

        return _find_format(all_formats, creative_format)

    def test_exact_match_returns_format(self):
        """Exact agent_url + id match returns the correct format object."""
        fmt = _make_fmt("https://creative.example.com", "display_300x250")
        result = self._call([fmt], _make_creative_format("https://creative.example.com", "display_300x250"))
        assert result is fmt

    def test_trailing_slash_on_creative_format_matches(self):
        """Trailing slash on creative_format.agent_url is normalized away."""
        fmt = _make_fmt("https://creative.example.com", "banner")
        result = self._call([fmt], _make_creative_format("https://creative.example.com/", "banner"))
        assert result is fmt

    def test_trailing_slash_on_registry_format_matches(self):
        """Trailing slash on registry format's agent_url is normalized away."""
        fmt = _make_fmt("https://creative.example.com/", "banner")
        result = self._call([fmt], _make_creative_format("https://creative.example.com", "banner"))
        assert result is fmt

    def test_mcp_suffix_normalized(self):
        """/mcp suffix on creative_format.agent_url is stripped before comparison."""
        fmt = _make_fmt("https://creative.example.com", "video_15s")
        result = self._call([fmt], _make_creative_format("https://creative.example.com/mcp", "video_15s"))
        assert result is fmt

    def test_a2a_suffix_normalized(self):
        """/a2a suffix on creative_format.agent_url is stripped before comparison."""
        fmt = _make_fmt("https://creative.example.com", "video_15s")
        result = self._call([fmt], _make_creative_format("https://creative.example.com/a2a", "video_15s"))
        assert result is fmt

    def test_same_id_different_agent_url_no_match(self):
        """Same format id but different agent_url → no match (composite key enforced)."""
        fmt_a = _make_fmt("https://agent-a.example.com", "text_ad")
        fmt_b = _make_fmt("https://agent-b.example.com", "text_ad")
        # Looking for agent-b but only agent-a is in the list
        result = self._call([fmt_a], _make_creative_format("https://agent-b.example.com", "text_ad"))
        assert result is None

    def test_correct_agent_selected_when_multiple_agents_share_id(self):
        """When two agents have the same format id, the correct one is returned."""
        fmt_a = _make_fmt("https://agent-a.example.com", "text_ad")
        fmt_b = _make_fmt("https://agent-b.example.com", "text_ad")
        result = self._call([fmt_a, fmt_b], _make_creative_format("https://agent-b.example.com", "text_ad"))
        assert result is fmt_b

    def test_no_match_returns_none(self):
        """No matching format → returns None."""
        fmt = _make_fmt("https://creative.example.com", "display_300x250")
        result = self._call([fmt], _make_creative_format("https://creative.example.com", "nonexistent_format"))
        assert result is None

    def test_empty_list_returns_none(self):
        """Empty all_formats list → returns None."""
        result = self._call([], _make_creative_format("https://creative.example.com", "banner"))
        assert result is None

    def test_first_match_returned_when_duplicates(self):
        """If duplicates exist (same agent_url + id), the first one is returned."""
        fmt1 = _make_fmt("https://creative.example.com", "banner")
        fmt2 = _make_fmt("https://creative.example.com", "banner")
        result = self._call([fmt1, fmt2], _make_creative_format("https://creative.example.com", "banner"))
        assert result is fmt1


# ── _build_generative_manifest tests ─────────────────────────────────────────


class TestBuildGenerativeManifest:
    """Tests for _build_generative_manifest() — AdCP creative_manifest construction."""

    def _call(self, format_id_str: str, agent_url: str, assets: dict | None) -> dict:
        from src.core.tools.creatives._processing import _build_generative_manifest

        return _build_generative_manifest(format_id_str, agent_url, assets)

    def test_returns_full_format_id_object(self):
        """format_id in output is a full FormatId dict with agent_url and id."""
        result = self._call("display_300x250", "https://creative.example.com", None)
        assert isinstance(result["format_id"], dict)
        assert result["format_id"]["id"] == "display_300x250"
        assert result["format_id"]["agent_url"] == "https://creative.example.com"

    def test_assets_always_present(self):
        """assets key is always present, even when input assets is None."""
        result = self._call("banner", "https://creative.example.com", None)
        assert "assets" in result
        assert result["assets"] == {}

    def test_assets_empty_dict_when_none(self):
        """None assets → empty dict (not missing key, not None)."""
        result = self._call("banner", "https://creative.example.com", None)
        assert result["assets"] == {}

    def test_assets_passed_through(self):
        """Provided assets dict is included in output."""
        assets = {"banner": {"url": "https://example.com/banner.png"}}
        result = self._call("display_300x250", "https://creative.example.com", assets)
        assert result["assets"] == assets

    def test_assets_copied_not_mutated(self):
        """Output assets is a copy — mutating it does not affect the original."""
        assets = {"banner": {"url": "https://example.com/banner.png"}}
        result = self._call("display_300x250", "https://creative.example.com", assets)
        result["assets"]["extra"] = "injected"
        assert "extra" not in assets

    def test_no_extra_top_level_keys(self):
        """Output only contains format_id and assets (no creative_id, name, etc.)."""
        result = self._call("text_ad", "https://creative.example.com", None)
        assert set(result.keys()) == {"format_id", "assets"}

    def test_format_id_only_has_id_and_agent_url(self):
        """format_id dict contains exactly id and agent_url (no extra fields)."""
        result = self._call("text_ad", "https://creative.example.com", None)
        assert set(result["format_id"].keys()) == {"id", "agent_url"}


# ── Static-path creative_manifest shape tests ─────────────────────────────────


class TestStaticManifestShape:
    """Verify that the static-path (preview_creative) creative_manifest passed to
    the registry has a full FormatId object and always includes the assets key.

    These tests call _update_existing_creative / _create_new_creative with a
    static (non-generative) format and inspect the creative_manifest kwarg
    captured by the mock registry.
    """

    def _make_static_format(self, agent_url: str = "https://creative.example.com", fmt_id: str = "display_300x250"):
        """Return a format object that looks static (no output_format_ids)."""
        return _make_fmt(agent_url, fmt_id, output_format_ids=None)

    def _make_creative_format_obj(self, agent_url: str = "https://creative.example.com", fmt_id: str = "display_300x250"):
        return _make_creative_format(agent_url, fmt_id)

    # ── _create_new_creative ──────────────────────────────────────────────────

    def test_create_static_manifest_format_id_is_dict(self):
        """Static create: creative_manifest.format_id is a dict with agent_url + id."""
        from src.core.tools.creatives._processing import _create_new_creative

        agent_url = "https://creative.example.com"
        fmt_id = "display_300x250"
        static_fmt = self._make_static_format(agent_url, fmt_id)
        creative_format = self._make_creative_format_obj(agent_url, fmt_id)

        mock_creative = MagicMock()
        mock_creative.creative_id = "c_test_1"
        mock_creative.name = "Test Creative"
        mock_creative.format_id = creative_format
        mock_creative.assets = {"banner": {"url": "https://example.com/banner.png"}}
        mock_creative.inputs = None
        mock_creative.context_id = None
        mock_creative.approved = False
        mock_creative.brand = None

        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(creative_id="c_test_1", status="pending_review")

        captured_manifest = {}

        async def fake_preview(**kwargs):
            captured_manifest.update(kwargs.get("creative_manifest", {}))
            return {"previews": [{"renders": [{"preview_url": "https://example.com/preview.png", "dimensions": {}}]}]}

        mock_registry = MagicMock()
        mock_registry.preview_creative = fake_preview

        with (
            patch("src.core.tools.creatives._processing._extract_format_info") as mock_fmt_info,
            patch("src.core.tools.creatives._processing.run_async_in_sync_context") as mock_run_async,
            patch("src.core.tools.creatives._processing._extract_url_from_assets", return_value=None),
            patch("src.core.tools.creatives._processing._build_creative_data", return_value={}),
        ):
            mock_fmt_info.return_value = {"agent_url": agent_url, "format_id": fmt_id, "parameters": None}
            # Capture the coroutine passed to run_async_in_sync_context
            preview_manifest_holder = {}

            def capture_run_async(coro):
                # We can't easily await here; inspect the call args on the registry mock instead
                return {"previews": [{"renders": [{"preview_url": "https://p.example.com/img.png", "dimensions": {}}]}]}

            mock_run_async.side_effect = capture_run_async

            mock_registry_instance = MagicMock()
            mock_registry_instance.preview_creative = MagicMock(return_value=MagicMock())

            _create_new_creative(
                creative=mock_creative,
                creative_repo=mock_repo,
                format_value=creative_format,
                approval_mode="auto-approve",
                tenant={"tenant_id": "t1"},
                webhook_url=None,
                context=None,
                all_formats=[static_fmt],
                registry=mock_registry_instance,
                principal_id="p1",
            )

            # Inspect the creative_manifest kwarg passed to preview_creative
            call_kwargs = mock_registry_instance.preview_creative.call_args.kwargs
            manifest = call_kwargs["creative_manifest"]

        assert isinstance(manifest["format_id"], dict), "format_id must be a dict (FormatId object)"
        assert manifest["format_id"]["id"] == fmt_id
        assert manifest["format_id"]["agent_url"] == agent_url
        assert "assets" in manifest, "assets key must always be present"

    def test_create_static_manifest_assets_present_when_no_assets(self):
        """Static create with no assets: creative_manifest still has assets key (empty dict)."""
        from src.core.tools.creatives._processing import _create_new_creative

        agent_url = "https://creative.example.com"
        fmt_id = "display_300x250"
        static_fmt = self._make_static_format(agent_url, fmt_id)
        creative_format = self._make_creative_format_obj(agent_url, fmt_id)

        mock_creative = MagicMock()
        mock_creative.creative_id = "c_test_2"
        mock_creative.name = "Test Creative"
        mock_creative.format_id = creative_format
        mock_creative.assets = None  # No assets
        mock_creative.inputs = None
        mock_creative.context_id = None
        mock_creative.approved = False
        mock_creative.brand = None

        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(creative_id="c_test_2", status="pending_review")

        with (
            patch("src.core.tools.creatives._processing._extract_format_info") as mock_fmt_info,
            patch("src.core.tools.creatives._processing.run_async_in_sync_context") as mock_run_async,
            patch("src.core.tools.creatives._processing._extract_url_from_assets", return_value="https://example.com/img.png"),
            patch("src.core.tools.creatives._processing._build_creative_data", return_value={"url": "https://example.com/img.png"}),
        ):
            mock_fmt_info.return_value = {"agent_url": agent_url, "format_id": fmt_id, "parameters": None}
            mock_run_async.return_value = {
                "previews": [{"renders": [{"preview_url": "https://p.example.com/img.png", "dimensions": {}}]}]
            }

            mock_registry_instance = MagicMock()
            mock_registry_instance.preview_creative = MagicMock(return_value=MagicMock())

            _create_new_creative(
                creative=mock_creative,
                creative_repo=mock_repo,
                format_value=creative_format,
                approval_mode="auto-approve",
                tenant={"tenant_id": "t1"},
                webhook_url=None,
                context=None,
                all_formats=[static_fmt],
                registry=mock_registry_instance,
                principal_id="p1",
            )

            call_kwargs = mock_registry_instance.preview_creative.call_args.kwargs
            manifest = call_kwargs["creative_manifest"]

        assert "assets" in manifest
        assert manifest["assets"] == {}

    # ── _update_existing_creative ─────────────────────────────────────────────

    def test_update_static_manifest_format_id_is_dict(self):
        """Static update: creative_manifest.format_id is a dict with agent_url + id."""
        from src.core.tools.creatives._processing import _update_existing_creative

        agent_url = "https://creative.example.com"
        fmt_id = "display_300x250"
        static_fmt = self._make_static_format(agent_url, fmt_id)
        creative_format = self._make_creative_format_obj(agent_url, fmt_id)

        mock_creative = MagicMock()
        mock_creative.name = "Updated Creative"
        mock_creative.format_id = creative_format
        mock_creative.assets = {"banner": {"url": "https://example.com/banner.png"}}
        mock_creative.inputs = None
        mock_creative.context_id = None
        mock_creative.approved = False
        mock_creative.brand = None

        mock_existing = MagicMock()
        mock_existing.creative_id = "c_existing_1"
        mock_existing.name = "Original Creative"
        mock_existing.agent_url = agent_url
        mock_existing.format = fmt_id
        mock_existing.format_parameters = None
        mock_existing.data = {}

        mock_repo = MagicMock()

        with (
            patch("src.core.tools.creatives._processing._extract_format_info") as mock_fmt_info,
            patch("src.core.tools.creatives._processing.run_async_in_sync_context") as mock_run_async,
            patch("src.core.tools.creatives._processing._extract_url_from_assets", return_value=None),
            patch("src.core.tools.creatives._processing._build_creative_data", return_value={}),
        ):
            mock_fmt_info.return_value = {"agent_url": agent_url, "format_id": fmt_id, "parameters": None}
            mock_run_async.return_value = {
                "previews": [{"renders": [{"preview_url": "https://p.example.com/img.png", "dimensions": {}}]}]
            }

            mock_registry_instance = MagicMock()
            mock_registry_instance.preview_creative = MagicMock(return_value=MagicMock())

            _update_existing_creative(
                creative=mock_creative,
                existing_creative=mock_existing,
                creative_repo=mock_repo,
                format_value=creative_format,
                approval_mode="auto-approve",
                tenant={"tenant_id": "t1"},
                webhook_url=None,
                context=None,
                all_formats=[static_fmt],
                registry=mock_registry_instance,
                principal_id="p1",
            )

            call_kwargs = mock_registry_instance.preview_creative.call_args.kwargs
            manifest = call_kwargs["creative_manifest"]

        assert isinstance(manifest["format_id"], dict), "format_id must be a dict (FormatId object)"
        assert manifest["format_id"]["id"] == fmt_id
        assert manifest["format_id"]["agent_url"] == agent_url
        assert "assets" in manifest
