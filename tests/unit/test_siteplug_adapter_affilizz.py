"""Unit tests for SiteplugAdapter — Affilizz wiring (Tasks 02 & 03).

Covers:
- create_media_buy() text_ad_search gate:
    - All packages text_ad_search → synthetic sp_text_* ID, no SSP provisioning
    - Mixed packages (text_ad + other) → normal provision_entity_stack path
    - Dry-run → synthetic sp_* ID, no SSP provisioning
- add_creative_assets() delegation to SiteplugCreativeManager
- SiteplugCreativeManager initialised in __init__ with config + client
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from src.adapters.siteplug.adapter import SiteplugAdapter
from src.adapters.siteplug.managers.creative import SiteplugCreativeManager
from src.core.schemas import (
    AssetStatus,
    CreateMediaBuyRequest,
    CreateMediaBuySuccess,
    MediaPackage,
    Principal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_principal(name: str = "Test Advertiser") -> Principal:
    p = MagicMock(spec=Principal)
    p.principal_id = "principal-001"
    p.name = name
    p.platform_mappings = {}
    p.get_adapter_id = MagicMock(return_value=None)
    return p


def _make_format_id(id: str = "text_ad_search", agent_url: str = "siteplug://t1") -> MagicMock:
    fmt = MagicMock()
    fmt.id = id
    fmt.agent_url = agent_url
    return fmt


def _make_package(
    package_id: str = "pkg-001",
    format_ids: list | None = None,
    implementation_config: dict | None = None,
) -> MediaPackage:
    pkg = MagicMock(spec=MediaPackage)
    pkg.package_id = package_id
    pkg.name = "Test Package"
    pkg.format_ids = format_ids if format_ids is not None else [_make_format_id()]
    pkg.implementation_config = implementation_config
    return pkg


def _make_request(po_number: str = "PO-001") -> CreateMediaBuyRequest:
    req = MagicMock(spec=CreateMediaBuyRequest)
    req.po_number = po_number
    req.idempotency_key = "idem-001"
    return req


def _make_adapter(
    dry_run: bool = False,
    affilizz_internal_url: str = "https://api.affilizz.com",
    affilizz_api_key: str = "test-key",
) -> SiteplugAdapter:
    """Build a SiteplugAdapter with all external dependencies mocked."""
    config = {
        "base_url": "https://api.siteplug.com/ssp/v1",
        "api_key": "siteplug-key",
        "affilizz_internal_url": affilizz_internal_url,
        "affilizz_api_key": affilizz_api_key,
    }
    principal = _make_principal()

    with (
        patch("src.adapters.siteplug.adapter.SiteplugClient"),
        patch("src.adapters.siteplug.adapter.SiteplugCampaignManager"),
        patch("src.adapters.siteplug.adapter.SiteplugInventoryManager"),
        patch("src.adapters.siteplug.adapter.SiteplugReportingManager"),
        patch("src.adapters.siteplug.adapter.SiteplugTargetingManager"),
        patch("src.adapters.siteplug.adapter.SiteplugWorkflowManager"),
        patch("src.adapters.base.get_audit_logger"),
    ):
        adapter = SiteplugAdapter(
            config=config,
            principal=principal,
            dry_run=dry_run,
            tenant_id="tenant-001",
        )
    return adapter


# ---------------------------------------------------------------------------
# SiteplugAdapter.__init__ — creative_manager wiring
# ---------------------------------------------------------------------------


class TestSiteplugAdapterInit:
    def test_creative_manager_is_initialised(self):
        adapter = _make_adapter()
        assert hasattr(adapter, "creative_manager")
        assert isinstance(adapter.creative_manager, SiteplugCreativeManager)

    def test_creative_manager_receives_connection_config(self):
        adapter = _make_adapter(
            affilizz_internal_url="https://api.affilizz.com",
            affilizz_api_key="secret",
        )
        assert adapter.creative_manager._config is adapter.connection_config

    def test_affilizz_credentials_stored_in_connection_config(self):
        adapter = _make_adapter(
            affilizz_internal_url="https://api.affilizz.com",
            affilizz_api_key="my-key",
        )
        assert adapter.connection_config.affilizz_internal_url == "https://api.affilizz.com"
        assert adapter.connection_config.affilizz_api_key == "my-key"


# ---------------------------------------------------------------------------
# create_media_buy() — text_ad_search gate
# ---------------------------------------------------------------------------


class TestCreateMediaBuyTextAdGate:
    def _call(
        self,
        adapter: SiteplugAdapter,
        packages: list,
        po_number: str = "PO-001",
    ) -> CreateMediaBuySuccess:
        request = _make_request(po_number=po_number)
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 12, 31, tzinfo=UTC)
        return adapter.create_media_buy(request, packages, start, end)

    def test_text_ad_only_returns_sp_text_prefix_id(self):
        """All packages are text_ad_search → synthetic sp_text_* media_buy_id."""
        adapter = _make_adapter()
        packages = [_make_package(format_ids=[_make_format_id("text_ad_search")])]

        result = self._call(adapter, packages, po_number="PO-123")

        assert isinstance(result, CreateMediaBuySuccess)
        assert result.media_buy_id.startswith("sp_text_")

    def test_text_ad_only_uses_po_number_in_id(self):
        """sp_text_{po_number} when po_number is set."""
        adapter = _make_adapter()
        packages = [_make_package(format_ids=[_make_format_id("text_ad_search")])]

        result = self._call(adapter, packages, po_number="PO-999")

        assert result.media_buy_id == "sp_text_PO-999"

    def test_text_ad_only_does_not_call_provision_entity_stack(self):
        """No SSP provisioning calls for text_ad_search-only media buys."""
        adapter = _make_adapter()
        packages = [_make_package(format_ids=[_make_format_id("text_ad_search")])]

        self._call(adapter, packages)

        adapter.campaign_manager.provision_entity_stack.assert_not_called()

    def test_multiple_text_ad_packages_all_skips_provisioning(self):
        """Multiple packages all text_ad_search → still skips provisioning."""
        adapter = _make_adapter()
        packages = [
            _make_package("pkg-001", format_ids=[_make_format_id("text_ad_search")]),
            _make_package("pkg-002", format_ids=[_make_format_id("text_ad_search")]),
        ]

        result = self._call(adapter, packages)

        assert result.media_buy_id.startswith("sp_text_")
        adapter.campaign_manager.provision_entity_stack.assert_not_called()

    def test_mixed_packages_does_not_skip_provisioning(self):
        """Mixed formats (text_ad_search + other) → provision_entity_stack is called."""
        adapter = _make_adapter()
        # First package carries the required implementation_config (platform_name)
        packages = [
            _make_package(
                "pkg-001",
                format_ids=[_make_format_id("text_ad_search")],
                implementation_config={"platform_name": "TestPlatform"},
            ),
            _make_package("pkg-002", format_ids=[_make_format_id("siteplug_native_display")]),
        ]

        # The mixed path will try to provision — bypass the thread executor entirely
        # so we can verify the text_ad gate did NOT fire (no sp_text_ prefix).
        with patch("src.adapters.siteplug.adapter.concurrent.futures.ThreadPoolExecutor") as mock_pool:
            mock_executor = MagicMock()
            mock_pool.return_value.__enter__ = MagicMock(return_value=mock_executor)
            mock_pool.return_value.__exit__ = MagicMock(return_value=False)
            mock_future = MagicMock()
            mock_future.result.return_value = 42  # campaign_id
            mock_executor.submit.return_value = mock_future

            result = self._call(adapter, packages)

        # Mixed buy → sp_{campaign_id} not sp_text_
        assert result.media_buy_id == "sp_42"
        assert not result.media_buy_id.startswith("sp_text_")

    def test_empty_format_ids_does_not_trigger_text_ad_gate(self):
        """Packages with empty format_ids list → gate condition is False → normal path."""
        adapter = _make_adapter()
        packages = [_make_package(format_ids=[])]

        with patch("src.adapters.siteplug.adapter.concurrent.futures.ThreadPoolExecutor") as mock_pool:
            mock_executor = MagicMock()
            mock_pool.return_value.__enter__ = MagicMock(return_value=mock_executor)
            mock_pool.return_value.__exit__ = MagicMock(return_value=False)
            mock_future = MagicMock()
            mock_future.result.return_value = 99
            mock_executor.submit.return_value = mock_future

            # Needs platform_name in impl_config to avoid AdCPValidationError
            packages[0].implementation_config = {"platform_name": "TestPlatform"}
            result = self._call(adapter, packages)

        assert result.media_buy_id == "sp_99"

    def test_dry_run_returns_sp_prefix_id(self):
        """Dry-run always returns synthetic sp_{po_number} without SSP calls."""
        adapter = _make_adapter(dry_run=True)
        packages = [_make_package(format_ids=[_make_format_id("text_ad_search")])]

        result = self._call(adapter, packages, po_number="PO-DRY")

        assert result.media_buy_id.startswith("sp_")
        adapter.campaign_manager.provision_entity_stack.assert_not_called()

    def test_text_ad_gate_result_is_create_media_buy_success(self):
        """Return type is CreateMediaBuySuccess (not Error)."""
        adapter = _make_adapter()
        packages = [_make_package(format_ids=[_make_format_id("text_ad_search")])]

        result = self._call(adapter, packages)

        assert isinstance(result, CreateMediaBuySuccess)
        assert result.packages is not None


# ---------------------------------------------------------------------------
# add_creative_assets() — delegation to SiteplugCreativeManager
# ---------------------------------------------------------------------------


class TestAddCreativeAssetsDelegation:
    def test_delegates_to_creative_manager(self):
        """add_creative_assets must call creative_manager.add_creative_assets."""
        adapter = _make_adapter()
        expected = [AssetStatus(creative_id="c-001", status="ok")]
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=expected)

        assets = [{"creative_id": "c-001", "format_id": "text_ad_search"}]
        today = datetime(2025, 6, 1, tzinfo=UTC)

        result = adapter.add_creative_assets("mb-001", assets, today)

        adapter.creative_manager.add_creative_assets.assert_called_once_with("mb-001", assets, today)
        assert result == expected

    def test_passes_media_buy_id_to_creative_manager(self):
        adapter = _make_adapter()
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=[])

        adapter.add_creative_assets("sp_text_PO-123", [], datetime.now(UTC))

        call_args = adapter.creative_manager.add_creative_assets.call_args
        assert call_args.args[0] == "sp_text_PO-123"

    def test_passes_assets_list_to_creative_manager(self):
        adapter = _make_adapter()
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=[])

        assets = [
            {"creative_id": "c-001", "format_id": "text_ad_search"},
            {"creative_id": "c-002", "format_id": "text_ad_search"},
        ]
        adapter.add_creative_assets("mb-001", assets, datetime.now(UTC))

        call_args = adapter.creative_manager.add_creative_assets.call_args
        assert call_args.args[1] == assets

    def test_passes_today_to_creative_manager(self):
        adapter = _make_adapter()
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=[])

        today = datetime(2025, 6, 23, tzinfo=UTC)
        adapter.add_creative_assets("mb-001", [], today)

        call_args = adapter.creative_manager.add_creative_assets.call_args
        assert call_args.args[2] == today

    def test_returns_creative_manager_result_unchanged(self):
        adapter = _make_adapter()
        statuses = [
            AssetStatus(creative_id="c-001", status="ok"),
            AssetStatus(creative_id="c-002", status="skipped", message="sandbox"),
        ]
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=statuses)

        result = adapter.add_creative_assets("mb-001", [], datetime.now(UTC))

        assert result is statuses

    def test_empty_assets_returns_empty_list(self):
        adapter = _make_adapter()
        adapter.creative_manager.add_creative_assets = MagicMock(return_value=[])

        result = adapter.add_creative_assets("mb-001", [], datetime.now(UTC))

        assert result == []
