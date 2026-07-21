"""Siteplug SSP Adapter.

Adapter for the Siteplug SSP Tech API supporting:
- CPC, CPM, and flat_rate pricing
- Keyword, RON, category, hybrid, and PLA campaign types
- Search, native, and display channels
- Inventory sync (Task 03)
- Keyword targeting (Task 07)
- HITL workflows (Task 08)
- Ad group creation with keyword targeting (Task 04)

Entity Mapping:
- AdCP Media Buy → Siteplug Campaign
- AdCP Package → Siteplug Ad Group (within campaign)
- AdCP Creative → Siteplug Creative
- AdCP Product → Siteplug Platform/Brand configuration
"""

import asyncio
import concurrent.futures
import logging
import re
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
from src.adapters.constants import REQUIRED_UPDATE_ACTIONS
from src.core.exceptions import AdCPValidationError
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AffectedPackage,
    AssetStatus,
    CheckMediaBuyStatusResponse,
    CreateMediaBuyError,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    CreateMediaBuySuccess,
    DeliveryTotals,
    Error,
    MediaPackage,
    PackagePerformance,
    Principal,
    ReportingPeriod,
    UpdateMediaBuyError,
    UpdateMediaBuyResponse,
    UpdateMediaBuySuccess,
)

logger = logging.getLogger(__name__)

# Ad group name validation regex (Siteplug requirement).
# Hoisted to module level so both create_media_buy and add_new_packages can use it.
_ADGROUP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _\-]*$")


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
                affilizz_internal_url=config.get("affilizz_internal_url", ""),
                affilizz_api_key=config.get("affilizz_api_key", ""),
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
                affilizz_internal_url=config.get("affilizz_internal_url", ""),
                affilizz_api_key=config.get("affilizz_api_key", ""),
            )

        # Initialize HTTP client
        self.client = SiteplugClient(self.connection_config)

        # Initialize managers
        self.campaign_manager = SiteplugCampaignManager(
            client=self.client,
            log_func=self.log,
        )
        self.creative_manager = SiteplugCreativeManager(
            config=self.connection_config,
            siteplug_client=self.client,
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
            tenant_id=tenant_id or "",
            principal=principal,
            audit_logger=self.audit_logger,
            log_func=self.log,
        )

    # =========================================================================
    # DB persistence helpers
    # =========================================================================

    def _persist_adgroup_id(
        self,
        *,
        media_buy_id: str,
        package_id: str,
        adgroup_id: int,
    ) -> None:
        """Persist ``siteplug_adgroup_id`` to ``package_config`` JSONB.

        Called after a successful ``POST /campaigns/{id}/adgroups`` to store
        the new ad group ID so subsequent update/keyword operations can look
        it up without an extra API call.

        Args:
            media_buy_id: AdCP media buy ID (used as the stable lookup key).
            package_id: AdCP package ID.
            adgroup_id: Siteplug ad group ID returned by the create endpoint.
        """
        from sqlalchemy.orm import attributes

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, self.tenant_id)
                pkg = repo.get_package(media_buy_id, package_id)
                if pkg is None:
                    logger.error(
                        f"[siteplug] _persist_adgroup_id: package '{package_id}' "
                        f"not found in media buy '{media_buy_id}'"
                    )
                    return
                pkg.package_config["siteplug_adgroup_id"] = adgroup_id
                attributes.flag_modified(pkg, "package_config")
                session.commit()
                self.log(
                    f"[siteplug] persisted siteplug_adgroup_id={adgroup_id} "
                    f"for package '{package_id}'"
                )
        except Exception as exc:
            logger.error(
                f"[siteplug] _persist_adgroup_id: failed to persist "
                f"adgroup_id={adgroup_id} for package '{package_id}': {exc}",
                exc_info=True,
            )

    # =========================================================================
    # Async → sync bridge helper
    # =========================================================================

    def _run_async(self, coro_func):
        """Run an async coroutine function synchronously in a new event loop.

        The sales agent core layer calls adapter methods synchronously, but
        the Siteplug client is async. This helper spins up a dedicated thread
        with its own event loop to avoid "event loop already running" errors.

        Args:
            coro_func: A zero-argument callable that returns a coroutine.

        Returns:
            The result of the coroutine.

        Raises:
            Any exception raised by the coroutine.
        """
        def _run_in_new_loop():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro_func())
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_new_loop)
            return future.result()

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

        Calls ``provision_entity_stack()`` on the first package to create the
        full entity stack (Platform → Brand → Advertiser → Campaign) and
        returns the resulting ``siteplug_campaign_id`` as the media buy ID.

        The ``implementation_config`` on the product (stored in the package's
        product config) must supply the Siteplug-specific fields:
        ``platform_name``, ``brand_name``, ``brand_domain``, ``vertical``,
        ``sub_category``, ``campaign_type``, ``sol_id``.
        Optional: ``deal_type``, ``budget_type``, ``is_product``.

        Fix 5: ``package_id`` is used as the stable idempotency lookup key
        (known before provisioning, never changes). The provisional
        ``sp_{po_number}`` key was causing the idempotency guard to miss on
        retry because IDs were stored under a different key than the final
        ``sp_{campaign_id}`` media_buy_id.

        Returns:
            CreateMediaBuySuccess with ``pending_activation`` status.
        """
        self.log(
            f"Siteplug.create_media_buy for principal '{self.principal.name}'",
            dry_run_prefix=False,
        )

        assert self.tenant_id is not None, "tenant_id required for Siteplug provisioning"

        # ── Validate targeting overlays (Task 07) ─────────────────────────
        # Reject unsupported capability-gated fields before any provisioning.
        # Silent ignore is non-conformant for named, capability-gated fields.
        targeting_errors: list[str] = []
        for pkg in packages:
            overlay = getattr(pkg, "targeting_overlay", None)
            if overlay is not None:
                overlay_dict = (
                    overlay.model_dump(exclude_none=True)
                    if hasattr(overlay, "model_dump")
                    else (overlay if isinstance(overlay, dict) else {})
                )
                pkg_errors = self.targeting_manager.validate_targeting(overlay_dict)
                if pkg_errors:
                    targeting_errors.extend(pkg_errors)

        if targeting_errors:
            error_msg = f"Targeting validation failed: {'; '.join(targeting_errors)}"
            self.log(f"[siteplug] {error_msg}")
            return CreateMediaBuyError(
                errors=[
                    Error(
                        code="UNSUPPORTED_FEATURE",
                        message=error_msg,
                        details=None,
                    )
                ]
            )

        # ── Dry-run: return a synthetic media_buy_id without API calls ────
        if self.dry_run:
            media_buy_id = f"sp_{request.po_number or int(datetime.now(UTC).timestamp())}"
            self.log(f"[dry-run] Would provision entity stack → media_buy_id={media_buy_id}")
            return self._build_create_success(request, media_buy_id, packages)

        # ── Text-ad-search gate (Task 04 / Affilizz) ─────────────────────
        # When ALL packages carry only the ``text_ad_search`` format AND no
        # SSP campaign_type is configured in implementation_config, the media
        # buy is fulfilled entirely via the Affilizz API (no Siteplug SSP
        # entity stack required).
        #
        # IMPORTANT: ``text_ad_search`` is the Affilizz *creative format* —
        # it describes how ad content is delivered, not whether a campaign
        # entity exists in the SSP.  SSS campaigns use ``text_ad_search``
        # creatives but still require SSP provisioning (AX/IC) with
        # campaign_type="SSS", kd_auto_status=1, sss_auto_status=1.
        #
        # Gate fires only when BOTH conditions are true:
        #   1. All packages are text_ad_search format (Affilizz creative)
        #   2. No campaign_type is set in implementation_config (truly
        #      Affilizz-only — no SSP campaign entity needed)
        def _all_text_ad_search(pkgs: list) -> bool:
            if not pkgs:
                return False
            for pkg in pkgs:
                raw = getattr(pkg, "format_ids", None)
                # Materialise to a plain list so MagicMock iterables (which
                # yield nothing) are treated as empty rather than truthy.
                fmt_ids: list = list(raw) if isinstance(raw, (list, tuple)) else []
                if not fmt_ids:
                    return False
                if not all(getattr(f, "id", None) == "text_ad_search" for f in fmt_ids):
                    return False
            return True

        def _has_ssp_campaign_type(pkgs: list) -> bool:
            """Return True if any package has campaign_type set in implementation_config.

            A positive campaign_type (e.g. "SSS", "SDC", "SD") means the product
            requires an SSP campaign entity in AX/IC regardless of creative format.
            """
            for pkg in pkgs:
                impl = getattr(pkg, "implementation_config", None) or {}
                if impl.get("campaign_type"):
                    return True
            return False

        if _all_text_ad_search(packages) and not _has_ssp_campaign_type(packages):
            media_buy_id = f"sp_text_{request.po_number or int(datetime.now(UTC).timestamp())}"
            self.log(
                f"[siteplug] text_ad_search gate: all packages are text-only with no "
                f"SSP campaign_type → synthetic media_buy_id={media_buy_id}, "
                f"skipping SSP provisioning"
            )
            return self._build_create_success(request, media_buy_id, packages)

        # ── Extract Siteplug config from the first package ────────────────
        # All packages in a single media buy share the same platform/brand
        # config (one media buy = one brand stack per the onboarding spec).
        first_package = packages[0] if packages else None
        if first_package is None:
            raise AdCPValidationError("No packages provided")

        # The product's implementation_config carries the Siteplug-specific
        # provisioning fields.  The core layer stores these on the package
        # object via the product lookup; fall back to an empty dict if absent.
        impl_config: dict[str, Any] = getattr(first_package, "implementation_config", None) or {}

        platform_name: str = impl_config.get("platform_name", "")
        brand_name: str = impl_config.get("brand_name", self.principal.name or "")
        brand_domain: str = impl_config.get("brand_domain", "")
        vertical: str = impl_config.get("vertical", "")
        sub_category: str = impl_config.get("sub_category", "")
        campaign_type: str = impl_config.get("campaign_type", "SDC")
        sol_id: int = int(impl_config.get("sol_id", 1))
        is_product: int = int(impl_config.get("is_product", 0))
        deal_type: str | None = impl_config.get("deal_type")
        budget_type: int | None = (
            int(impl_config.get("budget_type")) if impl_config.get("budget_type") is not None else None
        )
        # Task 02c Phase 2: king_domains resolved at get_products time and passed
        # back by the buyer-agent via package.ext.affinity.king_domains.
        #
        # Priority order:
        #   1. ext.affinity.king_domains — set by get_products enrichment hook;
        #      buyer-agent passes through unchanged (may review/modify before submit).
        #   2. Static impl_config["related_domains"] — ops-controlled fallback.
        #
        # For SDC campaigns: if neither source provides domains, reject with a
        # validation error (fail-closed) to prevent zero-impression campaigns.
        _first_pkg_ext = getattr(first_package, "ext", None)
        _ext_king_domains, related_domains = self._merge_king_domains(
            pkg_ext=_first_pkg_ext,
            impl_config=impl_config,
            brand_domain=brand_domain,
        )

        # Fail-closed for SDC: king_domains must be present (set by get_products
        # enrichment) so the Siteplug traffic matching engine can associate
        # publisher traffic with the correct brand.
        if campaign_type == "SDC" and not _ext_king_domains and not related_domains:
            raise AdCPValidationError(
                "SDC campaign requires king_domains to be set. "
                "Ensure get_products was called with a brand.domain so the seller-agent "
                "can resolve related domains and return them in product.ext.affinity.king_domains. "
                "The buyer-agent must pass package.ext through to create_media_buy unchanged."
            )

        # platform_id (numeric) may be pre-provisioned by ops via Admin UI.
        # When present it takes precedence over platform_name — the platform
        # already exists in AX and does not need to be resolved/created.
        platform_id_from_config: int | None = (
            int(impl_config["platform_id"])
            if impl_config.get("platform_id") is not None
            else None
        )
        brand_id_from_config: int | None = (
            int(impl_config["brand_id"])
            if impl_config.get("brand_id") is not None
            else None
        )

        # platform_name is required only when platform_id is not pre-provisioned.
        if not platform_name and platform_id_from_config is None:
            raise AdCPValidationError(
                "Siteplug product implementation_config is missing 'platform_name' "
                "and 'platform_id'. Configure the product with either:\n"
                "  • platform_name: the Siteplug network name (e.g. 'CJ', 'Awin') "
                "to resolve/create the platform via POST /onboard, OR\n"
                "  • platform_id: the numeric ID of an existing platform in AX "
                "(set via Admin UI → Siteplug Campaign Configuration)."
            )

        # ── Branch on automation_mode BEFORE provisioning ─────────────────
        # manual mode: the adapter must NOT create the campaign automatically.
        # A provisional media_buy_id is generated from the package_id so the
        # workflow step and DB records have a stable, unique identifier.
        automation_mode: str = impl_config.get("automation_mode", "")

        if automation_mode == "manual":
            # Use package_id as the provisional media_buy_id — it is stable,
            # known before any API call, and unique per media buy.
            media_buy_id = f"sp_manual_{first_package.package_id}"
            self.log(
                f"[siteplug] manual mode: skipping provisioning, "
                f"provisional media_buy_id={media_buy_id}"
            )
            campaign_data = {
                "brand_name": brand_name,
                "budget": float(request.get_total_budget()) if hasattr(request, "get_total_budget") else 0.0,
                "campaign_type": campaign_type,
                "platform_name": platform_name,
                "vertical": vertical,
                "sub_category": sub_category,
            }
            workflow_step_id = self.workflow_manager.create_manual_workflow_step(
                media_buy_id=media_buy_id,
                campaign_data=campaign_data,
            )
            self.log(
                f"[siteplug] manual mode: created workflow step {workflow_step_id} for '{media_buy_id}'"
            )
            return self._build_create_success(
                request, media_buy_id, packages, workflow_step_id=workflow_step_id
            )

        # ── Provision entity stack (async → sync bridge) ──────────────────
        # provision_entity_stack is async; create_media_buy is called
        # synchronously by the core layer (which itself runs inside an async
        # event loop).  asyncio.run() would raise "event loop already running",
        # so we spin up a dedicated thread with its own event loop instead.
        #
        # Fix 5: Use first_package.package_id as the stable media_buy_id for
        # the idempotency guard. This key is known before provisioning and
        # never changes, so retries correctly find the already-stored IDs.
        stable_lookup_id = first_package.package_id

        # ── Pre-seed pre-provisioned platform_id / brand_id ──────────────
        # If ops has set platform_id and/or brand_id on the product via Admin
        # UI, inject them into package_config BEFORE calling provision_entity_stack
        # so the sequential path's per-step idempotency guards skip those
        # creation steps and go straight to POST /advertisers + POST /campaigns.
        #
        # Finny review note: validate that pre-seeded IDs are non-zero to catch
        # stale Admin UI values early (a wrong platform_id would cause a cryptic
        # foreign-key error from the SSP API further down).
        if platform_id_from_config is not None and platform_id_from_config > 0:
            self.log(
                f"[siteplug] pre-seeding platform_id={platform_id_from_config} "
                f"from impl_config into package_config (skipping POST /platforms)"
            )
            self.campaign_manager._persist_entity_ids(
                media_buy_id=stable_lookup_id,
                package_id=first_package.package_id,
                platform_id=platform_id_from_config,
                tenant_id=self.tenant_id,
            )
        elif platform_id_from_config is not None and platform_id_from_config <= 0:
            raise AdCPValidationError(
                f"Siteplug product implementation_config has invalid platform_id="
                f"{platform_id_from_config}. "
                "Set a valid numeric platform ID via Admin UI → Siteplug Campaign Configuration."
            )

        if brand_id_from_config is not None and brand_id_from_config > 0:
            self.log(
                f"[siteplug] pre-seeding brand_id={brand_id_from_config} "
                f"from impl_config into package_config (skipping POST /brands)"
            )
            self.campaign_manager._persist_entity_ids(
                media_buy_id=stable_lookup_id,
                package_id=first_package.package_id,
                brand_id=brand_id_from_config,
                tenant_id=self.tenant_id,
            )
        elif brand_id_from_config is not None and brand_id_from_config <= 0:
            raise AdCPValidationError(
                f"Siteplug product implementation_config has invalid brand_id="
                f"{brand_id_from_config}. "
                "Set a valid numeric brand ID via Admin UI → Siteplug Campaign Configuration."
            )

        async def _run_provision() -> int:
            return await self.campaign_manager.provision_entity_stack(
                media_buy_id=stable_lookup_id,
                package_id=first_package.package_id,
                platform_name=platform_name,
                brand_name=brand_name,
                brand_domain=brand_domain,
                vertical=vertical,
                sub_category=sub_category,
                campaign_type=campaign_type,
                sol_id=sol_id,
                is_product=is_product,
                deal_type=deal_type,
                budget_type=budget_type,
                tenant_id=self.tenant_id,
                idempotency_key=request.idempotency_key,
                related_domains=related_domains,
            )

        try:
            campaign_id: int = self._run_async(_run_provision)
        except Exception as exc:
            logger.error(f"Siteplug.create_media_buy: provisioning failed: {exc}", exc_info=True)
            raise AdCPValidationError(f"Siteplug entity provisioning failed: {exc}") from exc

        media_buy_id = f"sp_{campaign_id}"
        self.log(f"Siteplug.create_media_buy: provisioned campaign_id={campaign_id} → media_buy_id={media_buy_id}")

        # ── Create ad groups for each package (Task 04) ───────────────────
        # Each AdCP package maps to one Siteplug ad group within the campaign.
        async def _create_adgroups_for_packages() -> None:
            for pkg in packages:
                bid_type: str = "cpm"

                # Check package_pricing_info for an explicit bid/type first.
                # If an explicit rate is present it is forwarded directly;
                # otherwise _derive_starting_bid() is called (Task 11 / D17).
                explicit_rate: float | None = None
                if package_pricing_info and pkg.package_id in package_pricing_info:
                    pricing = package_pricing_info[pkg.package_id]
                    pricing_model = pricing.get("pricing_model", "cpm").lower()
                    if pricing_model in ("cpc", "cpm"):
                        bid_type = pricing_model
                    rate = pricing.get("rate") or pricing.get("bid_price")
                    if rate is not None:
                        explicit_rate = float(rate)

                if explicit_rate is not None:
                    bid_amount: float = explicit_rate
                else:
                    # Task 11 (D17): derive geo-tier × product-type starting bid.
                    # Passes product_config so configurable thresholds are respected.
                    _product_config = getattr(pkg, "product_config", None)
                    bid_amount = self.campaign_manager._derive_starting_bid(
                        request, pkg, campaign_type, product_config=_product_config
                    )

                # Build ad group name from package name; validate against regex
                adgroup_name: str | None = pkg.name or None
                if adgroup_name and not _ADGROUP_NAME_RE.match(adgroup_name):
                    # Sanitise: strip leading non-alphanumeric chars, replace
                    # disallowed chars with underscores, truncate to 64 chars
                    sanitised = re.sub(r"[^a-zA-Z0-9 _\-]", "_", adgroup_name)
                    sanitised = re.sub(r"^[^a-zA-Z0-9]+", "", sanitised)
                    adgroup_name = sanitised[:64] if sanitised else None
                    self.log(
                        f"[siteplug] ad group name sanitised for package "
                        f"'{pkg.package_id}': {adgroup_name!r}"
                    )

                adgroup_payload: dict[str, Any] = {
                    "bid_amount": bid_amount,
                    "bid_type": bid_type,
                }
                if adgroup_name:
                    adgroup_payload["name"] = adgroup_name

                try:
                    adgroup_data = await self.client.create_adgroup(
                        campaign_id,
                        adgroup_payload,
                        idempotency_key=request.idempotency_key,
                    )
                    adgroup_id: int = int(
                        adgroup_data.get("ad_group_id")
                        or adgroup_data.get("adgroup_id")
                        or 0
                    )
                    self.log(
                        f"[siteplug] created adgroup_id={adgroup_id} "
                        f"for package '{pkg.package_id}'"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[siteplug] create_adgroup failed for package "
                        f"'{pkg.package_id}': {exc} — skipping ad group creation"
                    )
                    continue

                if adgroup_id <= 0:
                    logger.warning(
                        f"[siteplug] create_adgroup returned invalid adgroup_id={adgroup_id} "
                        f"for package '{pkg.package_id}' — skipping keyword wiring"
                    )
                    continue

                # Persist adgroup_id to package_config
                self.campaign_manager._persist_entity_ids(
                    media_buy_id=stable_lookup_id,
                    package_id=pkg.package_id,
                    tenant_id=self.tenant_id,
                )
                # Persist adgroup_id separately (not a standard entity field)
                self._persist_adgroup_id(
                    media_buy_id=stable_lookup_id,
                    package_id=pkg.package_id,
                    adgroup_id=adgroup_id,
                )

                # ── Keyword targeting ─────────────────────────────────────
                # Build keyword payload from targeting_overlay if present
                overlay = pkg.targeting_overlay
                if overlay is None:
                    continue

                # Apply geo/device targeting parameters to the campaign
                # (Task 07: country_codes, device_targeting)
                targeting_params = self.targeting_manager.build_targeting(
                    overlay.model_dump(exclude_none=True) if hasattr(overlay, "model_dump") else (overlay or {})
                )
                if targeting_params:
                    try:
                        async def _apply_targeting(cid=campaign_id, params=targeting_params) -> None:
                            await self.client.update_campaign(cid, params)

                        await _apply_targeting()
                        self.log(
                            f"[siteplug] applied targeting to campaign_id={campaign_id}: "
                            f"{list(targeting_params.keys())}"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[siteplug] apply targeting failed for campaign_id={campaign_id}: "
                            f"{exc} — targeting not applied"
                        )

                kw_payload = self._build_keyword_payload(overlay)

                if kw_payload:
                    try:
                        await self.client.add_keywords(
                            adgroup_id,
                            kw_payload,
                            idempotency_key=request.idempotency_key,
                        )
                        self.log(
                            f"[siteplug] added keywords to adgroup_id={adgroup_id} "
                            f"for package '{pkg.package_id}': "
                            f"{len(kw_payload.get('keywords', []))} positive, "
                            f"{len(kw_payload.get('negative_keywords', []))} negative"
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[siteplug] add_keywords failed for adgroup_id={adgroup_id} "
                            f"package '{pkg.package_id}': {exc} — keywords not added"
                        )

        try:
            self._run_async(_create_adgroups_for_packages)
        except Exception as exc:
            # Ad group creation is non-fatal — campaign is already provisioned.
            # Log and continue so the media buy is still returned successfully.
            logger.warning(
                f"[siteplug] create_media_buy: ad group creation failed "
                f"(non-fatal): {exc}"
            )

        if automation_mode == "confirmation_required":
            # Pause the campaign immediately so it does not serve ads before
            # the human reviewer approves it.  Non-fatal: if the pause call
            # fails we still create the workflow step so the human can review.
            try:
                async def _pause_campaign() -> None:
                    await self.client.update_campaign(campaign_id, {"status": 0})

                self._run_async(_pause_campaign)
                self.log(
                    f"[siteplug] confirmation_required: paused campaign_id={campaign_id} (status=0)"
                )
            except Exception as _pause_exc:
                logger.warning(
                    f"[siteplug] confirmation_required: failed to pause campaign_id={campaign_id}: "
                    f"{_pause_exc} — workflow step will still be created"
                )

            # Adapter created the campaign paused (status=0); human approves activation
            campaign_data = {
                "brand_name": brand_name,
                "budget": float(request.get_total_budget()) if hasattr(request, "get_total_budget") else 0.0,
                "campaign_type": campaign_type,
                "platform_name": platform_name,
                "vertical": vertical,
                "sub_category": sub_category,
                "siteplug_campaign_id": campaign_id,
            }
            workflow_step_id = self.workflow_manager.create_confirmation_workflow_step(
                media_buy_id=media_buy_id,
                campaign_id=campaign_id,
                campaign_data=campaign_data,
            )
            self.log(
                f"[siteplug] confirmation_required mode: created workflow step "
                f"{workflow_step_id} for '{media_buy_id}'"
            )
            return self._build_create_success(
                request, media_buy_id, packages, workflow_step_id=workflow_step_id
            )

        # Default: no workflow step — return normally
        return self._build_create_success(request, media_buy_id, packages)

    def update_media_buy(
        self,
        media_buy_id: str,
        buyer_ref: str,
        action: str,
        package_id: str | None,
        budget: int | None,
        today: datetime,
    ) -> UpdateMediaBuyResponse:
        """Update a Siteplug campaign / ad group.

        Supported actions and their SSP API mappings:

        | AdCP action              | SSP API call                                      |
        |--------------------------|---------------------------------------------------|
        | pause_media_buy          | PUT /campaigns/{id}  status=0                     |
        | resume_media_buy         | PUT /campaigns/{id}  status=1                     |
        | cancel_media_buy         | PUT /campaigns/{id}  status=0  (irreversible)     |
        | pause_package            | PUT /adgroups/{id}/status  status=0               |
        | resume_package           | PUT /adgroups/{id}/status  status=1               |
        | update_package_budget    | PUT /adgroups/{id}  bid/budget fields             |

        All mutating calls forward ``request.idempotency_key`` when present.
        State is read from ``package_config`` JSONB (persisted by create_media_buy).

        Args:
            media_buy_id: AdCP media buy ID (format: "sp_{campaign_id}").
            buyer_ref: Buyer reference string.
            action: One of the REQUIRED_UPDATE_ACTIONS.
            package_id: AdCP package ID (required for package-level actions).
            budget: New budget in cents (for update_package_budget).
            today: Current datetime.

        Returns:
            UpdateMediaBuySuccess or UpdateMediaBuyError.
        """
        from sqlalchemy.orm import attributes

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        self.log(
            f"Siteplug.update_media_buy for '{media_buy_id}' action='{action}'",
            dry_run_prefix=False,
        )

        if action not in REQUIRED_UPDATE_ACTIONS:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="UNSUPPORTED_FEATURE",
                        message=(
                            f"Action '{action}' not supported by Siteplug adapter. "
                            f"Supported: {sorted(REQUIRED_UPDATE_ACTIONS)}"
                        ),
                        details=None,
                    )
                ]
            )

        assert self.tenant_id is not None, "tenant_id required for Siteplug update_media_buy"

        # ── Dry-run: return success without API calls ─────────────────────
        if self.dry_run:
            is_pause = action in ("pause_media_buy", "pause_package")
            affected = []
            if package_id:
                affected.append(
                    AffectedPackage(
                        package_id=package_id,
                        buyer_ref=buyer_ref,
                        paused=is_pause,
                        changes_applied={"budget": budget} if budget is not None else None,
                        buyer_package_ref=None,
                    )
                )
            self.log(f"[dry-run] Would execute action='{action}' on '{media_buy_id}'")
            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                affected_packages=affected,
                implementation_date=today,
            )

        # ── Extract Siteplug campaign_id from media_buy_id ────────────────
        # Format: "sp_{campaign_id}" — strip the prefix.
        if media_buy_id.startswith("sp_"):
            try:
                campaign_id = int(media_buy_id[3:])
            except ValueError:
                return UpdateMediaBuyError(
                    errors=[
                        Error(
                            code="VALIDATION_ERROR",
                            message=f"Cannot parse campaign_id from media_buy_id '{media_buy_id}'",
                            details=None,
                        )
                    ]
                )
        else:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="VALIDATION_ERROR",
                        message=f"Unexpected media_buy_id format: '{media_buy_id}' (expected 'sp_<id>')",
                        details=None,
                    )
                ]
            )

        # ── Campaign-level pause / resume ─────────────────────────────────
        if action in ("pause_media_buy", "resume_media_buy"):
            sp_status = 0 if action == "pause_media_buy" else 1
            is_pause = sp_status == 0

            async def _update_campaign() -> None:
                await self.client.update_campaign(campaign_id, {"status": sp_status})

            self._run_async(_update_campaign)

            with get_db_session() as session:
                repo = MediaBuyRepository(session, self.tenant_id)
                db_packages = repo.get_packages(media_buy_id)

            affected = [
                AffectedPackage(
                    package_id=pkg.package_id,
                    buyer_ref=buyer_ref,
                    paused=is_pause,
                    changes_applied=None,
                    buyer_package_ref=None,
                )
                for pkg in db_packages
            ]
            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                affected_packages=affected,
                implementation_date=today,
            )

        # ── Package-level actions — require package_id ────────────────────
        if not package_id:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="VALIDATION_ERROR",
                        message=f"package_id is required for action '{action}'",
                        details=None,
                    )
                ]
            )

        # Read adgroup_id from package_config
        with get_db_session() as session:
            repo = MediaBuyRepository(session, self.tenant_id)
            db_package = repo.get_package(media_buy_id, package_id)

        if db_package is None:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="PACKAGE_NOT_FOUND",
                        message=f"Package '{package_id}' not found in media buy '{media_buy_id}'",
                        details=None,
                    )
                ]
            )

        adgroup_id: int | None = db_package.package_config.get("siteplug_adgroup_id")

        # ── Package pause / resume ────────────────────────────────────────
        if action in ("pause_package", "resume_package"):
            sp_status = 0 if action == "pause_package" else 1
            is_pause = sp_status == 0

            if adgroup_id is None:
                # Ad group not yet created (e.g. ad group API not yet live) — no-op
                self.log(
                    f"[siteplug] update_media_buy: no adgroup_id for package '{package_id}' "
                    f"— skipping {action} (ad group API may not be live yet)"
                )
            else:
                async def _update_adgroup_status() -> None:
                    await self.client.update_adgroup_status(adgroup_id, sp_status)

                self._run_async(_update_adgroup_status)

            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                affected_packages=[
                    AffectedPackage(
                        package_id=package_id,
                        buyer_ref=buyer_ref,
                        paused=is_pause,
                        changes_applied=None,
                        buyer_package_ref=None,
                    )
                ],
                implementation_date=today,
            )

        # ── Package budget update ─────────────────────────────────────────
        if action == "update_package_budget":
            if budget is None:
                return UpdateMediaBuyError(
                    errors=[
                        Error(
                            code="VALIDATION_ERROR",
                            message="budget is required for update_package_budget action",
                            details=None,
                        )
                    ]
                )

            budget_float = float(budget) / 100.0  # cents → dollars

            if adgroup_id is not None:
                async def _update_adgroup_budget() -> None:
                    await self.client.update_adgroup(adgroup_id, {"budget": budget_float})

                self._run_async(_update_adgroup_budget)
            else:
                self.log(
                    f"[siteplug] update_media_buy: no adgroup_id for package '{package_id}' "
                    "— persisting budget to package_config only"
                )

            # Always persist to package_config regardless of API availability
            with get_db_session() as session:
                repo = MediaBuyRepository(session, self.tenant_id)
                pkg = repo.get_package(media_buy_id, package_id)
                if pkg is not None:
                    pkg.package_config["budget"] = budget_float
                    attributes.flag_modified(pkg, "package_config")
                    session.commit()

            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                affected_packages=[
                    AffectedPackage(
                        package_id=package_id,
                        buyer_ref=buyer_ref,
                        paused=False,
                        changes_applied={"budget": budget},
                        buyer_package_ref=None,
                    )
                ],
                implementation_date=today,
            )

        # ── Impressions update ────────────────────────────────────────────
        if action == "update_package_impressions":
            if budget is None:
                return UpdateMediaBuyError(
                    errors=[
                        Error(
                            code="VALIDATION_ERROR",
                            message="budget (impressions) is required for update_package_impressions action",
                            details=None,
                        )
                    ]
                )

            with get_db_session() as session:
                repo = MediaBuyRepository(session, self.tenant_id)
                pkg = repo.get_package(media_buy_id, package_id)
                if pkg is not None:
                    pkg.package_config["impressions"] = budget
                    attributes.flag_modified(pkg, "package_config")
                    session.commit()

            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=buyer_ref,
                affected_packages=[
                    AffectedPackage(
                        package_id=package_id,
                        buyer_ref=buyer_ref,
                        paused=False,
                        changes_applied={"impressions": budget},
                        buyer_package_ref=None,
                    )
                ],
                implementation_date=today,
            )

        # Fallback — should not reach here for valid actions
        return UpdateMediaBuySuccess(
            media_buy_id=media_buy_id,
            buyer_ref=buyer_ref,
            affected_packages=[],
            implementation_date=today,
        )

    def check_media_buy_status(
        self,
        media_buy_id: str,
        today: datetime,
    ) -> CheckMediaBuyStatusResponse:
        """Check the status of a Siteplug campaign.

        Reads ``siteplug_campaign_id`` from ``package_config``, calls
        ``GET /campaigns/{id}``, and maps the Siteplug status integer to an
        AdCP status string.

        Siteplug → AdCP status mapping:
            0 (Off/Paused)   → "paused"
            1 (Active)       → "active"
            2 (Date-paused)  → "paused"
            3 (Incomplete)   → "pending_activation"

        Args:
            media_buy_id: AdCP media buy ID (format: "sp_{campaign_id}").
            today: Current datetime (unused but required by interface).

        Returns:
            CheckMediaBuyStatusResponse with the mapped AdCP status.
        """
        self.log(
            f"Siteplug.check_media_buy_status for '{media_buy_id}'",
            dry_run_prefix=False,
        )

        # ── Dry-run: return pending_activation without API call ───────────
        if self.dry_run:
            return CheckMediaBuyStatusResponse(
                media_buy_id=media_buy_id,
                buyer_ref=media_buy_id,
                status="pending_activation",
            )

        # ── Extract campaign_id from media_buy_id ─────────────────────────
        if media_buy_id.startswith("sp_"):
            try:
                campaign_id = int(media_buy_id[3:])
            except ValueError:
                logger.warning(
                    f"[siteplug] check_media_buy_status: cannot parse campaign_id "
                    f"from '{media_buy_id}' — returning pending_activation"
                )
                return CheckMediaBuyStatusResponse(
                    media_buy_id=media_buy_id,
                    buyer_ref=media_buy_id,
                    status="pending_activation",
                )
        else:
            logger.warning(
                f"[siteplug] check_media_buy_status: unexpected media_buy_id format "
                f"'{media_buy_id}' — returning pending_activation"
            )
            return CheckMediaBuyStatusResponse(
                media_buy_id=media_buy_id,
                buyer_ref=media_buy_id,
                status="pending_activation",
            )

        # ── Fetch campaign from SSP API ───────────────────────────────────
        _SP_STATUS_MAP: dict[int, str] = {
            0: "paused",
            1: "active",
            2: "paused",
            3: "pending_activation",
        }

        async def _get_campaign() -> dict:
            return await self.client.get_campaign(campaign_id)

        try:
            campaign_data = self._run_async(_get_campaign)
            sp_status: int = int(campaign_data.get("status", 3))
            adcp_status = _SP_STATUS_MAP.get(sp_status, "pending_activation")
        except Exception as exc:
            logger.warning(
                f"[siteplug] check_media_buy_status: GET /campaigns/{campaign_id} failed: {exc} "
                "— returning pending_activation"
            )
            adcp_status = "pending_activation"

        self.log(f"[siteplug] campaign_id={campaign_id} status={adcp_status}")

        # ── Check for pending workflow step ───────────────────────────────
        # If there's a pending HITL step, override status to pending_activation
        # and surface the workflow details in the response packages field.
        pending_step = self.workflow_manager.get_pending_workflow_step(media_buy_id)
        if pending_step is not None:
            self.log(
                f"[siteplug] pending workflow step {pending_step['step_id']} "
                f"found for '{media_buy_id}' — returning pending_activation"
            )
            return CheckMediaBuyStatusResponse(
                media_buy_id=media_buy_id,
                buyer_ref=media_buy_id,
                status="pending_activation",
                packages=[
                    {
                        "workflow_step_id": pending_step["step_id"],
                        "workflow_status": pending_step["status"],
                        "workflow_step_type": pending_step["step_type"],
                        "workflow_tool": pending_step["tool_name"],
                        "workflow_created_at": pending_step["created_at"],
                        "automation_mode": pending_step["action_details"].get(
                            "automation_mode", "unknown"
                        ),
                    }
                ],
            )

        return CheckMediaBuyStatusResponse(
            media_buy_id=media_buy_id,
            buyer_ref=media_buy_id,
            status=adcp_status,
        )

    def get_media_buy_delivery(
        self,
        media_buy_id: str,
        date_range: ReportingPeriod,
        today: datetime,
    ) -> AdapterGetMediaBuyDeliveryResponse:
        """Get delivery data for a media buy from the Siteplug SSP API.

        Reads ``siteplug_campaign_id`` from the first package's ``package_config``
        in the DB, calls the SSP API delivery endpoint via
        ``SiteplugReportingManager.get_delivery()``, and maps the response to
        an ``AdapterGetMediaBuyDeliveryResponse``.

        The SSP API client is a stub until the delivery endpoint is deployed;
        the mapping logic is fully implemented so switching to the real API
        requires only a change in ``SiteplugClient.get_campaign_delivery()``.

        Args:
            media_buy_id: AdCP media buy ID (``sp_{campaign_id}`` format).
            date_range: Reporting period with start/end datetimes.
            today: Current datetime (unused — date_range is authoritative).

        Returns:
            AdapterGetMediaBuyDeliveryResponse with delivery metrics.
        """
        self.log(
            f"Siteplug.get_media_buy_delivery for '{media_buy_id}'",
            dry_run_prefix=False,
        )

        assert self.tenant_id is not None, "tenant_id required for Siteplug delivery reporting"

        # ── Dry-run: return empty report without DB/API calls ─────────────
        if self.dry_run:
            self.log(f"[dry-run] Would fetch delivery for media_buy_id={media_buy_id}")
            return AdapterGetMediaBuyDeliveryResponse(
                media_buy_id=media_buy_id,
                reporting_period=date_range,
                totals=DeliveryTotals(
                    impressions=0,
                    spend=0.0,
                    clicks=0,
                    ctr=0.0,
                    video_completions=0,
                    completion_rate=0.0,
                ),
                by_package=[],
                currency="USD",
            )

        # ── Read siteplug_campaign_id from DB ─────────────────────────────
        campaign_id = self._read_campaign_id(media_buy_id)
        if campaign_id is None:
            self.log(
                f"[siteplug] get_media_buy_delivery: no siteplug_campaign_id found "
                f"for media_buy_id={media_buy_id}, returning empty report"
            )
            return AdapterGetMediaBuyDeliveryResponse(
                media_buy_id=media_buy_id,
                reporting_period=date_range,
                totals=DeliveryTotals(
                    impressions=0,
                    spend=0.0,
                    clicks=0,
                    ctr=0.0,
                    video_completions=0,
                    completion_rate=0.0,
                ),
                by_package=[],
                currency="USD",
            )

        # ── Build date params from ReportingPeriod ────────────────────────
        start_date: str | None = None
        end_date: str | None = None
        if date_range.start:
            start_date = date_range.start.strftime("%Y-%m-%d")
        if date_range.end:
            end_date = date_range.end.strftime("%Y-%m-%d")

        # ── Call reporting manager (async → sync bridge) ──────────────────
        async def _run_delivery() -> dict:
            return await self.reporting_manager.get_delivery(
                campaign_id=campaign_id,
                media_buy_id=media_buy_id,
                tenant_id=self.tenant_id,
                start_date=start_date,
                end_date=end_date,
            )

        delivery_data = self._run_async(_run_delivery)

        # ── Map to AdapterGetMediaBuyDeliveryResponse ─────────────────────
        from src.core.schemas import AdapterPackageDelivery

        by_package = [
            AdapterPackageDelivery(
                package_id=pkg["package_id"],
                impressions=int(pkg.get("impressions", 0)),
                spend=float(pkg.get("spend", 0.0)),
            )
            for pkg in delivery_data.get("by_package", [])
        ]

        return AdapterGetMediaBuyDeliveryResponse(
            media_buy_id=media_buy_id,
            reporting_period=date_range,
            totals=DeliveryTotals(
                impressions=delivery_data.get("impressions", 0),
                spend=delivery_data.get("spend", 0.0),
                clicks=delivery_data.get("clicks"),
                ctr=delivery_data.get("ctr"),
                video_completions=None,
                completion_rate=None,
            ),
            by_package=by_package,
            currency="USD",
        )

    def get_packages_snapshot(self, media_buy_id: str) -> dict[str, Any]:
        """Get a point-in-time snapshot of package performance.

        Reads ``siteplug_campaign_id`` from the first package's ``package_config``
        in the DB, calls the SSP API snapshot endpoint via
        ``SiteplugReportingManager.get_snapshot()``, and returns a dict of
        per-package ``Snapshot`` objects keyed by ``package_id``.

        The SSP API client is a stub until the delivery endpoint is deployed;
        the mapping logic is fully implemented so switching to the real API
        requires only a change in ``SiteplugClient.get_campaign_snapshot()``.

        Args:
            media_buy_id: AdCP media buy ID (``sp_{campaign_id}`` format).

        Returns:
            Dict mapping package_id → Snapshot (or None if unavailable).
            Returns empty dict if campaign_id not found or API not yet deployed.
        """
        self.log(
            f"Siteplug.get_packages_snapshot for '{media_buy_id}'",
            dry_run_prefix=False,
        )

        assert self.tenant_id is not None, "tenant_id required for Siteplug snapshot"

        # ── Dry-run: return empty snapshot without DB/API calls ───────────
        if self.dry_run:
            self.log(f"[dry-run] Would fetch snapshot for media_buy_id={media_buy_id}")
            return {}

        # ── Read siteplug_campaign_id from DB ─────────────────────────────
        campaign_id = self._read_campaign_id(media_buy_id)
        if campaign_id is None:
            self.log(
                f"[siteplug] get_packages_snapshot: no siteplug_campaign_id found "
                f"for media_buy_id={media_buy_id}, returning empty snapshot"
            )
            return {}

        # ── Call reporting manager (async → sync bridge) ──────────────────
        async def _run_snapshot() -> dict:
            return await self.reporting_manager.get_snapshot(
                campaign_id=campaign_id,
                media_buy_id=media_buy_id,
                tenant_id=self.tenant_id,
            )

        snapshot_data = self._run_async(_run_snapshot)

        # ── Map to per-package Snapshot objects ───────────────────────────
        from src.core.schemas import DeliveryStatus, Snapshot

        result: dict[str, Snapshot | None] = {}
        as_of = snapshot_data.get("as_of") or datetime.now(UTC)

        # Determine staleness from data_freshness latency tier (if available)
        # realtime → 60s, daily → 3600s, delayed → 86400s
        staleness_seconds = 3600  # default: daily aggregation

        for pkg in snapshot_data.get("packages", []):
            package_id = pkg.get("package_id")
            if not package_id:
                continue

            # Map delivery_status string to DeliveryStatus enum
            raw_status = pkg.get("delivery_status")
            delivery_status: DeliveryStatus | None = None
            if raw_status:
                try:
                    delivery_status = DeliveryStatus(raw_status)
                except ValueError:
                    logger.debug(
                        "[siteplug] Unknown delivery_status value: %s", raw_status
                    )

            result[package_id] = Snapshot(
                as_of=as_of,
                impressions=float(pkg.get("impressions", 0)),
                spend=float(pkg.get("spend", 0.0)),
                clicks=pkg.get("clicks"),
                pacing_index=pkg.get("pacing_index"),
                delivery_status=delivery_status,
                staleness_seconds=staleness_seconds,
                currency="USD",
            )

        return result

    def add_creative_assets(
        self,
        media_buy_id: str,
        assets: list[dict[str, Any]],
        today: datetime,
    ) -> list[AssetStatus]:
        """Add creative assets to a media buy.

        Delegates to :class:`SiteplugCreativeManager` which handles
        Affilizz text-ad upserts and any future SSP creative uploads.

        Returns:
            List of :class:`AssetStatus` objects from the creative manager.
        """
        self.log(
            f"Siteplug.add_creative_assets for '{media_buy_id}': {len(assets)} asset(s)",
            dry_run_prefix=False,
        )
        return self.creative_manager.add_creative_assets(media_buy_id, assets, today)

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

    def enrich_products(
        self,
        products: list,
        brand_domain: str | None,
    ) -> list:
        """Enrich SDC products with king_domains resolved from the brand-agent.

        Implements the Phase 2 AdCP pattern: seller-resolved configuration via
        ``ext.affinity.king_domains`` on the ``get_products`` response.

        For each SDC product (``impl_config.campaign_type == "SDC"``):
        - Calls ``GET /api/brands/resolve?domain={brand_domain}`` on the brand-agent
        - Merges the result with any static ``impl_config["related_domains"]``
        - Sets ``product.ext = {"affinity": {"king_domains": [...]}}``

        Fail-open per BR-RULE-079: any exception is caught and logged; products
        still return (with empty ``king_domains``) rather than blocking get_products.

        Args:
            products: List of Product objects after filtering/ranking.
            brand_domain: Brand domain from req.brand.domain (may be None).

        Returns:
            The same list with ext.affinity.king_domains set on SDC products.
        """
        if not brand_domain or not products:
            return products

        try:
            from src.adapters.siteplug.brand_agent_client import fetch_brand_related_domains

            _king_domains: list[str] = []
            if self.connection_config.brand_agent_url:
                _king_domains = fetch_brand_related_domains(
                    brand_agent_url=self.connection_config.brand_agent_url,
                    brand_agent_api_key=self.connection_config.brand_agent_api_key,
                    brand_agent_tenant_id=self.connection_config.brand_agent_tenant_id,
                    domain=brand_domain,
                )

            # Always include the brand domain itself (deduplicated)
            _all_domains: list[str] = list(dict.fromkeys([brand_domain] + _king_domains))

            for product in products:
                _impl = getattr(product, "implementation_config", None) or {}
                _camp_type: str = str(_impl.get("campaign_type", "")).upper()
                if _camp_type == "SDC":
                    # Merge brand-agent domains with static related_domains from impl_config
                    # using the shared helper so the deduplication logic stays in one place.
                    _fake_ext = {"affinity": {"king_domains": _all_domains}}
                    _, _merged_or_none = self._merge_king_domains(
                        pkg_ext=_fake_ext,
                        impl_config=_impl,
                        brand_domain=brand_domain,
                    )
                    _merged_domains: list[str] = _merged_or_none or _all_domains
                    # Set ext.affinity.king_domains — buyer passes this back in create_media_buy
                    _existing_ext: dict = dict(getattr(product, "ext", None) or {})
                    _existing_affinity: dict = dict(_existing_ext.get("affinity", {}))
                    _existing_affinity["king_domains"] = _merged_domains
                    _existing_ext["affinity"] = _existing_affinity
                    product.ext = _existing_ext  # type: ignore[assignment]
                    self.log(
                        f"[siteplug] enrich_products: set ext.affinity.king_domains="
                        f"{_merged_domains} on product {product.product_id}",
                        dry_run_prefix=False,
                    )
        except (ImportError, RuntimeError, OSError, ValueError) as exc:
            logger.warning(
                "[siteplug] enrich_products: king_domains enrichment failed "
                "(fail-open, continuing): %s", exc
            )

        return products

    def get_targeting_capabilities(self) -> TargetingCapabilities:
        """Return targeting capabilities.

        Task 07: geo_countries enabled; geo_regions set to False (blocked on
        SSP API enhancement — Task 13).

        Keyword capabilities (K2 + K3 — AdCP 3.0 rc.3 compliance) are
        unchanged from Task 01 skeleton and will be updated in Task 12:
        - keyword_targets: broad/phrase/exact match types supported via adgroup_kw_mapping
        - negative_keywords: broad/exact match types supported (no negative phrase in Siteplug)
        Both are declared as list[str] here and serialized to KeywordTargets/NegativeKeywords
        objects with supported_match_types arrays in capabilities.py (not booleans).
        """
        return TargetingCapabilities(
            geo_countries=True,
            geo_regions=False,   # Task 13 — blocked on SSP API geo region enhancement
            # Keyword targeting: all three match types supported (Task 12)
            keyword_targets=["broad", "phrase", "exact"],
            # Negative keywords: broad and exact only (Siteplug type IDs 4 and 7; no phrase)
            negative_keywords=["broad", "exact"],
        )

    def get_adcp_capabilities(self) -> AdapterCapabilities:
        """Return full AdapterCapabilities for this adapter."""
        return self.capabilities

    def execute_workflow_step_approval(self, step_id: str) -> bool:
        """Execute the adapter-side action for an approved workflow step.

        Called by the generic
        :func:`~src.core.tools.workflow_approval.execute_approved_workflow_step`
        after a human approves a HITL workflow step.

        Dispatches based on the step's ``tool_name``:

        - ``"activate_siteplug_campaign"`` → calls
          :meth:`~src.adapters.siteplug.managers.workflow.SiteplugWorkflowManager.activate_campaign_from_step`
          which calls ``PUT /campaigns/{id}`` with ``status=1``.

        Args:
            step_id: The workflow step ID that was approved.

        Returns:
            ``True`` if the action succeeded, ``False`` otherwise.
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.models import WorkflowStep

        try:
            with get_db_session() as db:
                step = db.get(WorkflowStep, step_id)
                tool_name = step.tool_name if step else None
        except Exception as exc:
            logger.error(f"[siteplug] execute_workflow_step_approval: DB error reading step '{step_id}': {exc}")
            return False

        if tool_name == "activate_siteplug_campaign":
            logger.info(
                f"[siteplug] execute_workflow_step_approval: activating campaign for step '{step_id}'"
            )
            return self.workflow_manager.activate_campaign_from_step(step_id)

        logger.info(
            f"[siteplug] execute_workflow_step_approval: no action for tool_name='{tool_name}' "
            f"on step '{step_id}'"
        )
        return True

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

    def add_new_packages(
        self,
        media_buy_id: str,
        new_packages: list,
        *,
        idempotency_key: str | None = None,
        today: datetime,
    ) -> "UpdateMediaBuyResponse":
        """Create new ad groups for packages added mid-flight to an existing campaign.

        Reads ``siteplug_campaign_id`` from the media buy's ``package_config``,
        then calls ``POST /ssp/v1/campaigns/{id}/adgroups`` for each new package.
        Keywords from ``targeting_overlay`` are wired via ``add_keywords``.

        Args:
            media_buy_id: AdCP media buy ID (format: ``"sp_{campaign_id}"``).
            new_packages: List of ``PackageRequest`` objects to add.
            idempotency_key: Optional idempotency key forwarded on POST.
            today: Current datetime (required by interface).

        Returns:
            ``UpdateMediaBuySuccess`` or ``UpdateMediaBuyError``.
        """
        self.log(
            f"Siteplug.add_new_packages for '{media_buy_id}' "
            f"({len(new_packages)} package(s))",
            dry_run_prefix=False,
        )

        # ── Dry-run ───────────────────────────────────────────────────────────
        if self.dry_run:
            self.log(
                f"[dry-run] Would create {len(new_packages)} new ad group(s) "
                f"for '{media_buy_id}'"
            )
            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=media_buy_id,
                affected_packages=[
                    AffectedPackage(
                        package_id=getattr(pkg, "package_id", str(i)),
                        buyer_ref=media_buy_id,
                        paused=False,
                        changes_applied={"adgroup_created": True},
                        buyer_package_ref=None,
                    )
                    for i, pkg in enumerate(new_packages)
                ],
                implementation_date=today,
            )

        # ── Extract campaign_id from media_buy_id ─────────────────────────────
        if not media_buy_id.startswith("sp_"):
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="VALIDATION_ERROR",
                        message=f"Unexpected media_buy_id format: '{media_buy_id}' (expected 'sp_<id>')",
                        details=None,
                    )
                ]
            )
        try:
            campaign_id = int(media_buy_id[3:])
        except ValueError:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="VALIDATION_ERROR",
                        message=f"Cannot parse campaign_id from media_buy_id '{media_buy_id}'",
                        details=None,
                    )
                ]
            )

        assert self.tenant_id is not None, "tenant_id required for add_new_packages"

        created: list[AffectedPackage] = []

        async def _create_new_adgroups() -> None:
            for pkg in new_packages:
                pkg_id: str = getattr(pkg, "package_id", "") or ""
                bid_amount: float = float(getattr(pkg, "bid_price", None) or getattr(pkg, "cpm", None) or 0.0)
                bid_type: str = "cpm"

                # Build name
                adgroup_name: str | None = getattr(pkg, "name", None)
                if adgroup_name and not _ADGROUP_NAME_RE.match(adgroup_name):
                    sanitised = re.sub(r"[^a-zA-Z0-9 _\-]", "_", adgroup_name)
                    sanitised = re.sub(r"^[^a-zA-Z0-9]+", "", sanitised)
                    adgroup_name = sanitised[:64] if sanitised else None

                adgroup_payload: dict[str, Any] = {
                    "bid_amount": bid_amount,
                    "bid_type": bid_type,
                }
                if adgroup_name:
                    adgroup_payload["name"] = adgroup_name
                if getattr(pkg, "paused", False):
                    adgroup_payload["status"] = 0

                try:
                    adgroup_data = await self.client.create_adgroup(
                        campaign_id,
                        adgroup_payload,
                        idempotency_key=idempotency_key,
                    )
                    adgroup_id: int = int(
                        adgroup_data.get("ad_group_id")
                        or adgroup_data.get("adgroup_id")
                        or 0
                    )
                    self.log(
                        f"[siteplug] add_new_packages: created adgroup_id={adgroup_id} "
                        f"for package '{pkg_id}'"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[siteplug] add_new_packages: create_adgroup failed for "
                        f"package '{pkg_id}': {exc} — skipping"
                    )
                    continue

                if adgroup_id > 0 and pkg_id:
                    self._persist_adgroup_id(
                        media_buy_id=media_buy_id,
                        package_id=pkg_id,
                        adgroup_id=adgroup_id,
                    )

                # Wire keywords from targeting_overlay
                overlay = getattr(pkg, "targeting_overlay", None)
                if overlay is not None and adgroup_id > 0:
                    kw_payload = self._build_keyword_payload(overlay)
                    if kw_payload:
                        try:
                            await self.client.add_keywords(
                                adgroup_id, kw_payload, idempotency_key=idempotency_key
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[siteplug] add_new_packages: add_keywords failed for "
                                f"adgroup_id={adgroup_id}: {exc}"
                            )

                created.append(
                    AffectedPackage(
                        package_id=pkg_id,
                        buyer_ref=media_buy_id,
                        paused=bool(getattr(pkg, "paused", False)),
                        changes_applied={"adgroup_id": adgroup_id},
                        buyer_package_ref=None,
                    )
                )

        try:
            self._run_async(_create_new_adgroups)
        except Exception as exc:
            logger.error(
                f"[siteplug] add_new_packages: failed for '{media_buy_id}': {exc}",
                exc_info=True,
            )
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="INTERNAL_ERROR",
                        message=f"Failed to create new ad groups for '{media_buy_id}': {exc}",
                        details=None,
                    )
                ]
            )

        return UpdateMediaBuySuccess(
            media_buy_id=media_buy_id,
            buyer_ref=media_buy_id,
            affected_packages=created,
            implementation_date=today,
        )

    def update_media_buy_keywords(
        self,
        media_buy_id: str,
        package_id: str,
        *,
        keyword_targets_add: list | None = None,
        keyword_targets_remove: list | None = None,
        negative_keywords_add: list | None = None,
        negative_keywords_remove: list | None = None,
        today: datetime,
    ) -> "UpdateMediaBuyResponse":
        """Add or remove keyword targets / negative keywords on a Siteplug ad group.

        Reads ``siteplug_adgroup_id`` from ``package_config``, then calls
        ``POST /adgroups/{id}/keywords`` (add) and/or
        ``DELETE /adgroups/{id}/keywords`` (remove) as appropriate.

        Per-keyword ``bid_price`` is forwarded as ``kw_max_cpc`` on add (K1).
        Negative keywords do not support ``bid_price``.

        Args:
            media_buy_id: AdCP media buy ID (format: ``"sp_{campaign_id}"``).
            package_id: AdCP package ID.
            keyword_targets_add: List of ``KeywordTargetsAddItem`` objects to add.
            keyword_targets_remove: List of ``KeywordTargetsRemoveItem`` objects to remove.
            negative_keywords_add: List of ``NegativeKeywordsAddItem`` objects to add.
            negative_keywords_remove: List of ``NegativeKeywordsRemoveItem`` objects to remove.
            today: Current datetime (required by interface).

        Returns:
            ``UpdateMediaBuySuccess`` or ``UpdateMediaBuyError``.
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        self.log(
            f"Siteplug.update_media_buy_keywords for '{media_buy_id}' "
            f"package='{package_id}'",
            dry_run_prefix=False,
        )

        # ── Dry-run: return success without API calls ─────────────────────
        if self.dry_run:
            self.log(
                f"[dry-run] Would update keywords on '{media_buy_id}' "
                f"package='{package_id}'"
            )
            return UpdateMediaBuySuccess(
                media_buy_id=media_buy_id,
                buyer_ref=media_buy_id,
                affected_packages=[
                    AffectedPackage(
                        package_id=package_id,
                        buyer_ref=media_buy_id,
                        paused=False,
                        changes_applied={"keywords_updated": True},
                        buyer_package_ref=None,
                    )
                ],
                implementation_date=today,
            )

        # ── Read adgroup_id from package_config ───────────────────────────
        assert self.tenant_id is not None, "tenant_id required for keyword update"

        with get_db_session() as session:
            repo = MediaBuyRepository(session, self.tenant_id)
            db_package = repo.get_package(media_buy_id, package_id)

        if db_package is None:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="PACKAGE_NOT_FOUND",
                        message=f"Package '{package_id}' not found in media buy '{media_buy_id}'",
                        details=None,
                    )
                ]
            )

        adgroup_id: int | None = db_package.package_config.get("siteplug_adgroup_id")
        if adgroup_id is None:
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="UNSUPPORTED_FEATURE",
                        message=(
                            f"Package '{package_id}' has no Siteplug ad group ID — "
                            "ad group may not have been created yet"
                        ),
                        details=None,
                    )
                ]
            )

        # ── Build add payload ─────────────────────────────────────────────
        add_payload: dict[str, Any] = {}

        if keyword_targets_add:
            kw_list = [self._kw_entry(kw, include_bid=True) for kw in keyword_targets_add]
            if kw_list:
                add_payload["keywords"] = kw_list

        if negative_keywords_add:
            neg_list = [self._kw_entry(nkw, include_bid=False) for nkw in negative_keywords_add]
            if neg_list:
                add_payload["negative_keywords"] = neg_list

        # ── Build remove payload ──────────────────────────────────────────
        remove_payload: dict[str, Any] = {}

        if keyword_targets_remove:
            kw_remove_list = [self._kw_entry(kw, include_bid=False) for kw in keyword_targets_remove]
            if kw_remove_list:
                remove_payload["keywords"] = kw_remove_list

        if negative_keywords_remove:
            neg_remove_list = [self._kw_entry(nkw, include_bid=False) for nkw in negative_keywords_remove]
            if neg_remove_list:
                remove_payload["negative_keywords"] = neg_remove_list

        # ── Execute API calls ─────────────────────────────────────────────
        try:
            if add_payload:
                async def _add_kw() -> None:
                    await self.client.add_keywords(adgroup_id, add_payload)

                self._run_async(_add_kw)
                self.log(
                    f"[siteplug] added keywords to adgroup_id={adgroup_id}: "
                    f"{len(add_payload.get('keywords', []))} positive, "
                    f"{len(add_payload.get('negative_keywords', []))} negative"
                )

            if remove_payload:
                async def _remove_kw() -> None:
                    await self.client.remove_keywords(adgroup_id, remove_payload)

                self._run_async(_remove_kw)
                self.log(
                    f"[siteplug] removed keywords from adgroup_id={adgroup_id}: "
                    f"{len(remove_payload.get('keywords', []))} positive, "
                    f"{len(remove_payload.get('negative_keywords', []))} negative"
                )

        except Exception as exc:
            logger.error(
                f"[siteplug] update_media_buy_keywords: API call failed "
                f"for adgroup_id={adgroup_id}: {exc}",
                exc_info=True,
            )
            return UpdateMediaBuyError(
                errors=[
                    Error(
                        code="INTERNAL_ERROR",
                        message=f"Keyword update failed for ad group {adgroup_id}: {exc}",
                        details=None,
                    )
                ]
            )

        return UpdateMediaBuySuccess(
            media_buy_id=media_buy_id,
            buyer_ref=media_buy_id,
            affected_packages=[
                AffectedPackage(
                    package_id=package_id,
                    buyer_ref=media_buy_id,
                    paused=False,
                    changes_applied={
                        "keywords_added": len(add_payload.get("keywords", [])),
                        "negative_keywords_added": len(add_payload.get("negative_keywords", [])),
                        "keywords_removed": len(remove_payload.get("keywords", [])),
                        "negative_keywords_removed": len(remove_payload.get("negative_keywords", [])),
                    },
                    buyer_package_ref=None,
                )
            ],
            implementation_date=today,
        )

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

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _merge_king_domains(
        self,
        *,
        pkg_ext: dict | None,
        impl_config: dict,
        brand_domain: str | None,
    ) -> tuple[list[str], list[str] | None]:
        """Merge ext.affinity.king_domains with static impl_config related_domains.

        Single source of truth for the king_domains merge used by both
        ``create_media_buy`` and ``enrich_products``.

        Priority order:
          1. ``pkg_ext["affinity"]["king_domains"]`` — set by get_products enrichment.
          2. ``impl_config["related_domains"]`` — ops-controlled static fallback.

        ``brand_domain`` is excluded from the merged list (it is always added
        separately by the Siteplug whitelist endpoint).

        Args:
            pkg_ext: The ``ext`` dict from the package / product (may be None).
            impl_config: The product's ``implementation_config`` dict.
            brand_domain: The brand domain string (excluded from merged list).

        Returns:
            A 2-tuple of:
              - ``ext_king_domains``: the raw list from ext (may be empty).
              - ``merged``: deduplicated merged list, or None if empty.
        """
        ext_king_domains: list[str] = []
        if isinstance(pkg_ext, dict):
            _affinity = pkg_ext.get("affinity") or {}
            if isinstance(_affinity, dict):
                ext_king_domains = list(_affinity.get("king_domains") or [])

        static_related: list[str] = list(impl_config.get("related_domains") or [])

        # Deduplicate: ext domains first (buyer-reviewed), then static fallback.
        # brand_domain is excluded here — the Siteplug whitelist endpoint adds it.
        seen: set[str] = {brand_domain} if brand_domain else set()
        merged: list[str] = []
        for domain in ext_king_domains + static_related:
            if domain and domain not in seen:
                seen.add(domain)
                merged.append(domain)

        return ext_king_domains, merged or None

    def _kw_entry(self, kw: Any, *, include_bid: bool) -> dict[str, Any]:
        """Build a single keyword dict for the Siteplug keywords API.

        Args:
            kw: A keyword object with ``keyword``/``text``, ``match_type``,
                and optionally ``bid_price`` attributes.
            include_bid: When True, forward ``bid_price`` as ``kw_max_cpc`` (K1).

        Returns:
            Dict with ``text``, ``match_type``, and optionally ``kw_max_cpc``.
        """
        kw_text = getattr(kw, "keyword", None) or getattr(kw, "text", None)
        kw_match = getattr(kw, "match_type", "broad")
        # Resolve enum to string if needed
        if hasattr(kw_match, "value"):
            kw_match = kw_match.value
        entry: dict[str, Any] = {
            "text": str(kw_text),
            "match_type": str(kw_match),
        }
        if include_bid:
            bid_price = getattr(kw, "bid_price", None)
            if bid_price is not None:
                entry["kw_max_cpc"] = float(bid_price)
        return entry

    def _build_keyword_payload(self, overlay: Any) -> dict[str, Any]:
        """Build the keyword payload dict from a targeting overlay object.

        Handles both positive keyword targets (with optional per-keyword bid)
        and negative keywords (no bid support).

        Args:
            overlay: A targeting overlay object with optional ``keyword_targets``
                     and ``negative_keywords`` attributes.

        Returns:
            Dict with ``keywords`` and/or ``negative_keywords`` lists,
            or an empty dict if the overlay has no keywords.
        """
        payload: dict[str, Any] = {}

        keyword_targets = getattr(overlay, "keyword_targets", None)
        if keyword_targets:
            kw_list = [self._kw_entry(kw, include_bid=True) for kw in keyword_targets]
            if kw_list:
                payload["keywords"] = kw_list

        negative_keywords = getattr(overlay, "negative_keywords", None)
        if negative_keywords:
            neg_list = [self._kw_entry(nkw, include_bid=False) for nkw in negative_keywords]
            if neg_list:
                payload["negative_keywords"] = neg_list

        return payload

    def _read_campaign_id(self, media_buy_id: str) -> int | None:
        """Read ``siteplug_campaign_id`` from the first package's package_config.

        The campaign ID is stored by ``SiteplugCampaignManager.provision_entity_stack``
        (Task 02) under the key ``siteplug_campaign_id`` in the JSONB
        ``package_config`` column of the first package for this media buy.

        Args:
            media_buy_id: AdCP media buy ID (``sp_{campaign_id}`` format).

        Returns:
            Siteplug campaign ID as int, or None if not found.
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, self.tenant_id)
                packages = repo.get_packages(media_buy_id)
                for pkg in packages:
                    cfg = pkg.package_config or {}
                    campaign_id = cfg.get("siteplug_campaign_id")
                    if campaign_id is not None:
                        return int(campaign_id)
        except Exception as exc:
            logger.error(
                "[siteplug] _read_campaign_id: failed to read campaign_id for "
                "media_buy_id=%s: %s",
                media_buy_id,
                exc,
                exc_info=True,
            )
        return None

    def _run_async(self, coro_factory) -> Any:
        """Run an async coroutine factory synchronously in a dedicated thread.

        The sales agent core calls adapter methods synchronously from within
        an async event loop.  ``asyncio.run()`` would raise
        "event loop already running", so we spin up a dedicated thread with
        its own event loop instead — the same pattern used in
        ``create_media_buy``.

        Args:
            coro_factory: Zero-argument callable that returns a coroutine.

        Returns:
            The coroutine's return value.

        Raises:
            Any exception raised by the coroutine.
        """
        def _run_in_new_loop():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro_factory())
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_new_loop)
            return future.result()
