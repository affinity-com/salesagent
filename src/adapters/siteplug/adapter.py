"""Siteplug SSP Adapter.

Adapter for the Siteplug SSP Tech API supporting:
- CPC, CPM, and flat_rate pricing
- Keyword, RON, category, hybrid, and PLA campaign types
- Search, native, and display channels
- Inventory sync (Task 03)
- Keyword targeting (Task 07)
- HITL workflows (Task 08)

Entity Mapping:
- AdCP Media Buy → Siteplug Campaign
- AdCP Package → Siteplug Ad Group (within campaign)
- AdCP Creative → Siteplug Creative
- AdCP Product → Siteplug Platform/Brand configuration
"""

import logging
from datetime import UTC, datetime
from typing import Any

from src.adapters.base import (
    AdapterCapabilities,
    AdServerAdapter,
    CreativeEngineAdapter,
    TargetingCapabilities,
)
from src.adapters.siteplug.client import SiteplugClient
from src.adapters.siteplug.config_schema import SiteplugConnectionConfig
from src.adapters.siteplug.managers import (
    SiteplugCampaignManager,
    SiteplugCreativeManager,
    SiteplugInventoryManager,
    SiteplugReportingManager,
    SiteplugTargetingManager,
    SiteplugWorkflowManager,
)
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AssetStatus,
    CheckMediaBuyStatusResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    CreateMediaBuySuccess,
    DeliveryTotals,
    MediaPackage,
    PackagePerformance,
    Principal,
    ReportingPeriod,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
    AffectedPackage,
)

logger = logging.getLogger(__name__)


class SiteplugAdapter(AdServerAdapter):
    """Adapter for interacting with the Siteplug SSP Tech API.

    Siteplug is a search/native/display SSP supporting keyword-targeted
    campaigns with CPC, CPM, and flat-rate pricing.

    All abstract methods are stubbed with safe defaults in Task 01.
    Real implementations are wired in Tasks 02–08.
    """

    adapter_name = "siteplug"

    # Siteplug supports search, native, and display advertising
    default_channels = ["search", "native", "display"]

    # Delivery measurement is provided by Siteplug
    delivery_measurement_provider = "siteplug"
    default_delivery_measurement = {"provider": "siteplug"}

    # Schema and capabilities
    connection_config_class = SiteplugConnectionConfig
    capabilities = AdapterCapabilities(
        supports_inventory_sync=True,
        supports_inventory_profiles=True,
        inventory_entity_label="Zones",
        supports_custom_targeting=True,
        supports_geo_targeting=True,
        supports_dynamic_products=False,
        supported_pricing_models=["cpc", "cpm", "flat_rate"],
        supports_webhooks=False,
        supports_realtime_reporting=True,
    )

    def __init__(
        self,
        config: dict[str, Any],
        principal: Principal,
        dry_run: bool = False,
        creative_engine: CreativeEngineAdapter | None = None,
        tenant_id: str | None = None,
    ):
        """Initialize the Siteplug adapter.

        Args:
            config: Adapter configuration dict (base_url, api_key, timeout, etc.)
            principal: Principal (advertiser) making the request
            dry_run: Whether to simulate operations without making API calls
            creative_engine: Optional creative processing engine
            tenant_id: Tenant ID for multi-tenant context
        """
        super().__init__(config, principal, dry_run, creative_engine, tenant_id)

        # Build validated connection config from raw dict
        if self.dry_run:
            self.log("Running in dry-run mode — Siteplug API calls will be simulated", dry_run_prefix=False)
            # Use placeholder config for dry-run
            self.connection_config = SiteplugConnectionConfig(
                base_url=config.get("base_url", "https://api.siteplug.com/ssp/v1"),
                api_key=config.get("api_key", "dry-run-key"),
                timeout=config.get("timeout", 30),
                max_retries=config.get("max_retries", 3),
            )
        else:
            base_url = config.get("base_url", "")
            api_key = config.get("api_key", "")
            if not base_url or not api_key:
                raise ValueError(
                    "Siteplug adapter config is missing 'base_url' or 'api_key'"
                )
            self.connection_config = SiteplugConnectionConfig(
                base_url=base_url,
                api_key=api_key,
                timeout=config.get("timeout", 30),
                max_retries=config.get("max_retries", 3),
            )

        # Initialize HTTP client
        self.client = SiteplugClient(self.connection_config)

        # Initialize managers
        self.campaign_manager = SiteplugCampaignManager(
            client=self.client,
            log_func=self.log,
        )
        self.creative_manager = SiteplugCreativeManager(
            client=self.client,
            log_func=self.log,
        )
        self.inventory_manager = SiteplugInventoryManager(
            client=self.client,
            log_func=self.log,
            tenant_id=tenant_id or "",
        )
        self.reporting_manager = SiteplugReportingManager(
            client=self.client,
            log_func=self.log,
        )
        self.targeting_manager = SiteplugTargetingManager(
            client=self.client,
            log_func=self.log,
        )
        self.workflow_manager = SiteplugWorkflowManager(
            log_func=self.log,
        )

    # =========================================================================
    # Abstract method implementations — all stubs returning safe defaults
    # Real implementations wired in Tasks 02–08
    # =========================================================================

    def create_media_buy(
        self,
        request: CreateMediaBuyRequest,
        packages: list[MediaPackage],
        start_time: datetime,
        end_time: datetime,
        package_pricing_info: dict[str, dict] | None = None,
    ) -> CreateMediaBuyResponse:
        """Create a new media buy (campaign) in Siteplug.

        Stub — wired in Task 02.

        Returns:
            CreateMediaBuySuccess with pending_activation status
        """
        self.log(
            f"Siteplug.create_media_buy [STUB] for principal '{self.principal.name}'",
            dry_run_prefix=False,
        )

        media_buy_id = f"sp_{request.po_number or int(datetime.now(UTC).timestamp())}"

        return self._build_create_success(
            request,
            media_buy_id,
            packages,
        )

    def update_media_buy(
        self,
        media_buy_id: str,
        buyer_ref: str,
        action: str,
        package_id: str | None,
        budget: int | None,
        today: datetime,
    ) -> UpdateMediaBuyResponse:
        """Update a media buy with a specific action.

        Stub — wired in Task 02.

        Returns:
            UpdateMediaBuySuccess with current status unchanged
        """
        self.log(
            f"Siteplug.update_media_buy [STUB] for '{media_buy_id}' action='{action}'",
            dry_run_prefix=False,
        )

        affected = []
        if package_id:
            affected.append(
                AffectedPackage(
                    package_id=package_id,
                    buyer_ref=buyer_ref,
                    paused=action in ("pause_media_buy", "pause_package"),
                    changes_applied=None,
                    buyer_package_ref=None,
                )
            )

        return UpdateMediaBuySuccess(
            media_buy_id=media_buy_id,
            buyer_ref=buyer_ref,
            affected_packages=affected,
            implementation_date=today,
        )

    def check_media_buy_status(
        self,
        media_buy_id: str,
        today: datetime,
    ) -> CheckMediaBuyStatusResponse:
        """Check the status of a media buy.

        Stub — wired in Task 02.

        Returns:
            CheckMediaBuyStatusResponse with pending_activation status
        """
        self.log(
            f"Siteplug.check_media_buy_status [STUB] for '{media_buy_id}'",
            dry_run_prefix=False,
        )
        return CheckMediaBuyStatusResponse(
            media_buy_id=media_buy_id,
            buyer_ref=media_buy_id,
            status="pending_activation",
        )

    def get_media_buy_delivery(
        self,
        media_buy_id: str,
        date_range: ReportingPeriod,
        today: datetime,
    ) -> AdapterGetMediaBuyDeliveryResponse:
        """Get delivery data for a media buy.

        Stub — wired in Task 05.

        Returns:
            Empty delivery report
        """
        self.log(
            f"Siteplug.get_media_buy_delivery [STUB] for '{media_buy_id}'",
            dry_run_prefix=False,
        )
        return AdapterGetMediaBuyDeliveryResponse(
            media_buy_id=media_buy_id,
            reporting_period=date_range,
            totals=DeliveryTotals(
                impressions=0,
                spend=0,
                clicks=0,
                ctr=0.0,
                video_completions=0,
                completion_rate=0.0,
            ),
            by_package=[],
            currency="USD",
        )

    def get_packages_snapshot(self, media_buy_id: str) -> dict[str, Any]:
        """Get a snapshot of package performance.

        Stub — wired in Task 05.

        Returns:
            Empty snapshot dict
        """
        self.log(
            f"Siteplug.get_packages_snapshot [STUB] for '{media_buy_id}'",
            dry_run_prefix=False,
        )
        return {}

    def add_creative_assets(
        self,
        media_buy_id: str,
        assets: list[dict[str, Any]],
        today: datetime,
    ) -> list[AssetStatus]:
        """Add creative assets to a media buy.

        Stub — wired in Task 06.

        Returns:
            Empty list of asset statuses
        """
        self.log(
            f"Siteplug.add_creative_assets [STUB] for '{media_buy_id}'",
            dry_run_prefix=False,
        )
        return []

    def associate_creatives(
        self,
        line_item_ids: list[str],
        platform_creative_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Associate already-uploaded creatives with line items.

        Stub — wired in Task 06.

        Returns:
            Empty list of association results
        """
        self.log(
            f"Siteplug.associate_creatives [STUB]: "
            f"{len(platform_creative_ids)} creatives to {len(line_item_ids)} line items",
            dry_run_prefix=False,
        )
        return []

    def get_creative_formats(self) -> list[dict[str, Any]]:
        """Return Siteplug creative formats.

        Returns the Siteplug-specific format list from specs.
        """
        agent_url = f"siteplug://{self.tenant_id or 'default'}"
        return [
            {
                "format_id": {"id": "siteplug_text_ad_search", "agent_url": agent_url},
                "name": "Siteplug Text Ad (Search)",
                "type": "search",
                "description": "Text ad for Siteplug search placements (title + description + click URL)",
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "title",
                        "asset_type": "text",
                        "required": True,
                        "name": "Title",
                    },
                    {
                        "item_type": "individual",
                        "asset_id": "description",
                        "asset_type": "text",
                        "required": True,
                        "name": "Description",
                    },
                    {
                        "item_type": "individual",
                        "asset_id": "click_url",
                        "asset_type": "url",
                        "required": True,
                        "name": "Click URL",
                    },
                ],
                "is_standard": False,
            },
            {
                "format_id": {"id": "siteplug_native_display", "agent_url": agent_url},
                "name": "Siteplug Native Display",
                "type": "native",
                "description": "Native display ad for Siteplug native placements",
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "headline",
                        "asset_type": "text",
                        "required": True,
                        "name": "Headline",
                    },
                    {
                        "item_type": "individual",
                        "asset_id": "image",
                        "asset_type": "image",
                        "required": True,
                        "name": "Image",
                    },
                    {
                        "item_type": "individual",
                        "asset_id": "click_url",
                        "asset_type": "url",
                        "required": True,
                        "name": "Click URL",
                    },
                ],
                "is_standard": False,
            },
        ]

    def get_supported_pricing_models(self) -> set[str]:
        """Return supported pricing models.

        Siteplug supports CPC, CPM, and flat rate pricing.
        """
        return {"cpc", "cpm", "flat_rate"}

    def get_targeting_capabilities(self) -> TargetingCapabilities:
        """Return targeting capabilities.

        Siteplug supports keyword targeting with broad/phrase/exact match types,
        plus geographic targeting.
        """
        return TargetingCapabilities(
            geo_countries=True,
            geo_regions=True,
            nielsen_dma=False,
            eurostat_nuts2=False,
            us_zip=False,
            us_zip_plus_four=False,
            ca_fsa=False,
            ca_full=False,
            gb_outward=False,
            gb_full=False,
            de_plz=False,
            fr_code_postal=False,
            au_postcode=False,
        )

    def get_adcp_capabilities(self) -> AdapterCapabilities:
        """Return full AdapterCapabilities for this adapter."""
        return self.capabilities

    def update_media_buy_performance_index(
        self,
        media_buy_id: str,
        package_performance: list[PackagePerformance],
    ) -> bool:
        """Update performance index for packages.

        Stub — no-op for now.

        Returns:
            True (always succeeds as no-op)
        """
        self.log(
            f"Siteplug.update_media_buy_performance_index [STUB] for '{media_buy_id}'",
            dry_run_prefix=False,
        )
        return True

    async def get_available_inventory(self) -> dict[str, Any]:
        """Fetch available inventory zones from Siteplug.

        Delegates to :class:`~src.adapters.siteplug.managers.inventory.SiteplugInventoryManager`
        which warms its in-memory cache from ``GET /ssp/v1/inventory`` (IC-only,
        active zones, paginated) and returns the full zone list.

        Returns:
            Dict with ``zones`` list and ``properties`` metadata.
        """
        self.log("Siteplug.get_available_inventory", dry_run_prefix=False)
        # Warm the cache if empty (single-page fast path)
        if not self.inventory_manager._zone_cache:
            await self.inventory_manager._warm_cache()
        return self.inventory_manager.build_inventory_response()
