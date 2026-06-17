"""Siteplug campaign manager.

Handles entity provisioning and campaign lifecycle operations against the
Siteplug SSP API.

Provisioning strategy (Task 02-A + 02-B):
  Primary path  — POST /onboard (Phase 7, single 207 call)
  Fallback path — sequential POST per entity (Phase 2–6)

The primary path is attempted first. If the onboard endpoint is unavailable
(ENTITY_NOT_FOUND / connection error) the manager falls back to sequential
provisioning automatically.
"""

import logging
from typing import Any

from src.adapters.siteplug.client import SiteplugAPIError

logger = logging.getLogger(__name__)


class SiteplugCampaignManager:
    """Manages Siteplug entity provisioning and campaign CRUD operations."""

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the campaign manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    # =========================================================================
    # Public API
    # =========================================================================

    async def provision_entity_stack(
        self,
        *,
        media_buy_id: str,
        package_id: str,
        platform_name: str,
        rtb_flag: int,
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        campaign_type: str,
        sol_id: int,
        deal_type: str | None = None,
        budget_type: int | None = None,
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> int:
        """Provision the full entity stack and return the Siteplug campaign_id.

        Primary path: POST /onboard (Phase 7 — single 207 call).
        Fallback path: sequential POST per entity (Phase 2–6).

        Idempotency guard: if ``siteplug_campaign_id`` is already present in
        ``package_config`` the method returns it immediately without any API
        calls.

        All 5 entity IDs are persisted to ``package_config`` JSONB after a
        successful provisioning run.

        Args:
            media_buy_id: AdCP media buy ID (used to locate DB packages).
            package_id: AdCP package ID (the specific package to update).
            platform_name: Siteplug platform name (e.g. "CJ", "Awin").
            rtb_flag: 1 for RTB networks (agency created), 0 for non-RTB.
            brand_name: Advertiser brand name.
            brand_domain: Advertiser brand domain (e.g. "example.com").
            vertical: IAB vertical category.
            sub_category: IAB sub-category.
            campaign_type: Siteplug campaign type string ("SD", "SSS", "SDC").
            sol_id: Source of Lead ID (must exist in SSP DB).
            deal_type: Deal type ("CPC", "CPA", "VCPC") — required for non-RTB.
            budget_type: Budget type int (1–5) — required for non-RTB.
            tenant_id: Tenant ID for DB session.
            idempotency_key: Optional idempotency key forwarded to the SSP API.

        Returns:
            Siteplug campaign_id (int).

        Raises:
            SiteplugAPIError: On unrecoverable SSP API errors.
        """
        # ── Idempotency guard ─────────────────────────────────────────────
        existing_campaign_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_campaign_id", tenant_id
        )
        if existing_campaign_id:
            self._log(
                f"[siteplug] provision_entity_stack: campaign already provisioned "
                f"(siteplug_campaign_id={existing_campaign_id}), skipping."
            )
            return int(existing_campaign_id)

        # ── Primary path: onboarding (Phase 7) ───────────────────────────
        try:
            campaign_id = await self._provision_via_onboard(
                media_buy_id=media_buy_id,
                package_id=package_id,
                platform_name=platform_name,
                rtb_flag=rtb_flag,
                brand_name=brand_name,
                brand_domain=brand_domain,
                vertical=vertical,
                sub_category=sub_category,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            )
            return campaign_id

        except SiteplugAPIError as exc:
            # Fall back to sequential if the onboard route is not yet live
            # (ENTITY_NOT_FOUND = 404 means the route doesn't exist yet)
            if exc.status_code == 404 or exc.error_code == "ENTITY_NOT_FOUND":
                self._log(
                    "[siteplug] /onboard not available (Phase 7 not live) — "
                    "falling back to sequential provisioning."
                )
            else:
                raise

        # ── Fallback path: sequential provisioning (Phase 2–6) ───────────
        return await self._provision_sequential(
            media_buy_id=media_buy_id,
            package_id=package_id,
            platform_name=platform_name,
            rtb_flag=rtb_flag,
            brand_name=brand_name,
            brand_domain=brand_domain,
            vertical=vertical,
            sub_category=sub_category,
            campaign_type=campaign_type,
            sol_id=sol_id,
            deal_type=deal_type,
            budget_type=budget_type,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )

    # =========================================================================
    # Primary path — onboarding (Phase 7)
    # =========================================================================

    async def _provision_via_onboard(
        self,
        *,
        media_buy_id: str,
        package_id: str,
        platform_name: str,
        rtb_flag: int,
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        tenant_id: str,
        idempotency_key: str | None,
    ) -> int:
        """Call POST /onboard and parse the 207 response.

        The onboarding request must NOT include campaign_name, campaign_type,
        sol_id, deal_type, or budget_type — the SSP API injects these from its
        own config.

        Returns:
            Siteplug campaign_id.

        Raises:
            SiteplugAPIError: On 400/401/500 or per-brand failure.
        """
        payload: dict[str, Any] = {
            "platform_name": platform_name,
            "rtb_flag": rtb_flag,
            "brands": [
                {
                    "brand_name": brand_name,
                    "brand_domain": brand_domain,
                    "vertical": vertical,
                    "sub_category": sub_category,
                }
            ],
        }

        self._log(
            f"[siteplug] POST /onboard platform_name={platform_name!r} "
            f"rtb_flag={rtb_flag} brand={brand_name!r}"
        )

        body = await self.client.onboard(payload, idempotency_key=idempotency_key)

        # ── Parse 207 flat envelope ───────────────────────────────────────
        # Top-level keys: platform, agency, summary, results
        platform_id: int = body["platform"]["platform_id"]
        agency_id: int = body["agency"]["masteraccount_id"]  # 0 when rtb_flag=0

        result = body["results"][0]
        result_status: str = result.get("status", "")

        if result_status == "partial_failure":
            # Find the first failed step's error_code
            steps = result.get("steps", {})
            for step_name in ("brand", "advertiser", "campaign"):
                step = steps.get(step_name, {})
                if step.get("status") not in ("success", None):
                    error_code = step.get("error_code", "PARTIAL_FAILURE")
                    raise SiteplugAPIError(
                        f"Siteplug onboarding partial_failure at step '{step_name}': "
                        f"{step.get('message', '')}",
                        error_code=error_code,
                    )
            raise SiteplugAPIError(
                "Siteplug onboarding partial_failure (unknown step)",
                error_code="PARTIAL_FAILURE",
            )

        if result_status == "failed":
            steps = result.get("steps", {})
            brand_step = steps.get("brand", {})
            error_code = brand_step.get("error_code", "ONBOARD_FAILED")
            raise SiteplugAPIError(
                f"Siteplug onboarding failed at brand step: {brand_step.get('message', '')}",
                error_code=error_code,
            )

        steps = result["steps"]
        brand_id: int = steps["brand"]["brand_id"]
        advertiser_id: int = steps["advertiser"]["advertiser_id"]
        campaign_id: int = steps["campaign"]["campaign_id"]

        self._log(
            f"[siteplug] onboard success: platform_id={platform_id} "
            f"agency_id={agency_id} brand_id={brand_id} "
            f"advertiser_id={advertiser_id} campaign_id={campaign_id}"
        )

        self._persist_entity_ids(
            media_buy_id=media_buy_id,
            package_id=package_id,
            platform_id=platform_id,
            agency_id=agency_id,
            brand_id=brand_id,
            advertiser_id=advertiser_id,
            campaign_id=campaign_id,
            tenant_id=tenant_id,
        )

        return campaign_id

    # =========================================================================
    # Fallback path — sequential provisioning (Phase 2–6)
    # =========================================================================

    async def _provision_sequential(
        self,
        *,
        media_buy_id: str,
        package_id: str,
        platform_name: str,
        rtb_flag: int,
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        campaign_type: str,
        sol_id: int,
        deal_type: str | None,
        budget_type: int | None,
        tenant_id: str,
        idempotency_key: str | None,
    ) -> int:
        """Provision entities sequentially: Platform → Agency → Brand → Advertiser → Campaign.

        Each step checks ``package_config`` for an existing ID before calling
        the SSP API (per-step idempotency guard).

        Returns:
            Siteplug campaign_id.
        """
        # ── Step 1: Platform ──────────────────────────────────────────────
        platform_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_platform_id", tenant_id
        )
        if not platform_id:
            platform_id = await self._create_or_resolve_platform(
                platform_name=platform_name,
                idempotency_key=idempotency_key,
            )
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                platform_id=platform_id,
                tenant_id=tenant_id,
            )
        else:
            self._log(f"[siteplug] platform already provisioned (platform_id={platform_id}), skipping.")
        platform_id = int(platform_id)

        # ── Step 2: Agency (skip for non-RTB) ────────────────────────────
        agency_id_raw = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_agency_id", tenant_id
        )
        if agency_id_raw is None:
            if rtb_flag == 1:
                agency_data = await self.client.create_agency(
                    {"platform_id": platform_id, "agency_name": platform_name},
                    idempotency_key=idempotency_key,
                )
                agency_id = int(agency_data.get("masteraccount_id", 0))
                self._log(f"[siteplug] agency created: agency_id={agency_id}")
            else:
                agency_id = 0
                self._log("[siteplug] rtb_flag=0 — skipping agency creation, agency_id=0")
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                agency_id=agency_id,
                tenant_id=tenant_id,
            )
        else:
            agency_id = int(agency_id_raw)
            self._log(f"[siteplug] agency already provisioned (agency_id={agency_id}), skipping.")

        # ── Step 3: Brand ─────────────────────────────────────────────────
        brand_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_brand_id", tenant_id
        )
        if not brand_id:
            brand_data = await self.client.create_brand(
                {
                    "brand_name": brand_name,
                    "brand_domain": brand_domain,
                    "vertical": vertical,
                    "sub_category": sub_category,
                    "platform_id": platform_id,
                },
                idempotency_key=idempotency_key,
            )
            brand_id = int(brand_data.get("brand_id", 0))
            self._log(f"[siteplug] brand created: brand_id={brand_id}")
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                brand_id=brand_id,
                tenant_id=tenant_id,
            )
        else:
            brand_id = int(brand_id)
            self._log(f"[siteplug] brand already provisioned (brand_id={brand_id}), skipping.")

        # ── Step 4: Advertiser ────────────────────────────────────────────
        advertiser_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_advertiser_id", tenant_id
        )
        if not advertiser_id:
            advertiser_data = await self.client.create_advertiser(
                {
                    "advertiser_name": brand_name,
                    "platform_id": platform_id,
                    "brand_id": brand_id,
                },
                idempotency_key=idempotency_key,
            )
            advertiser_id = int(advertiser_data.get("advertiser_id", 0))
            self._log(f"[siteplug] advertiser created: advertiser_id={advertiser_id}")
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                advertiser_id=advertiser_id,
                tenant_id=tenant_id,
            )
        else:
            advertiser_id = int(advertiser_id)
            self._log(f"[siteplug] advertiser already provisioned (advertiser_id={advertiser_id}), skipping.")

        # ── Step 5: Campaign ──────────────────────────────────────────────
        campaign_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_campaign_id", tenant_id
        )
        if not campaign_id:
            campaign_payload: dict[str, Any] = {
                "advertiser_id": advertiser_id,
                "platform_id": platform_id,
                "brand_id": brand_id,
                "campaign_type": campaign_type,
                "sol_id": sol_id,
            }
            if deal_type is not None:
                campaign_payload["deal_type"] = deal_type
            if budget_type is not None:
                campaign_payload["budget_type"] = budget_type

            campaign_data = await self.client.create_campaign(
                campaign_payload,
                idempotency_key=idempotency_key,
            )
            campaign_id = int(campaign_data.get("campaign_id", 0))
            self._log(f"[siteplug] campaign created: campaign_id={campaign_id}")
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                campaign_id=campaign_id,
                tenant_id=tenant_id,
            )
        else:
            campaign_id = int(campaign_id)
            self._log(f"[siteplug] campaign already provisioned (campaign_id={campaign_id}), skipping.")

        return campaign_id

    # =========================================================================
    # Platform resolution helper
    # =========================================================================

    async def _create_or_resolve_platform(
        self,
        *,
        platform_name: str,
        idempotency_key: str | None,
    ) -> int:
        """Create a platform, resolving 409 ENTITY_ALREADY_EXISTS via GET /platforms?search=.

        In production, POST /platforms almost always returns 409 because the
        network already exists. This is the normal path — resolve and continue.

        Returns:
            Siteplug platform_id.
        """
        try:
            data = await self.client.create_platform(
                {"platform_name": platform_name},
                idempotency_key=idempotency_key,
            )
            platform_id = int(data.get("platform_id", 0))
            self._log(f"[siteplug] platform created: platform_id={platform_id}")
            return platform_id

        except SiteplugAPIError as exc:
            if exc.error_code != "ENTITY_ALREADY_EXISTS":
                raise

            self._log(
                f"[siteplug] platform '{platform_name}' already exists (409) — "
                "resolving via GET /platforms?search=..."
            )
            platforms = await self.client.list_platforms(search=platform_name)
            # list_platforms returns the data list directly
            if isinstance(platforms, list):
                items = platforms
            elif isinstance(platforms, dict):
                items = platforms.get("data", [])
            else:
                items = []

            for p in items:
                if p.get("platform_name", "").lower() == platform_name.lower():
                    platform_id = int(p["platform_id"])
                    self._log(f"[siteplug] resolved existing platform_id={platform_id}")
                    return platform_id

            raise SiteplugAPIError(
                f"Platform '{platform_name}' returned 409 but could not be resolved via search.",
                error_code="PLATFORM_LOOKUP_ERROR",
            )

    # =========================================================================
    # DB persistence helpers
    # =========================================================================

    def _read_package_config_field(
        self,
        media_buy_id: str,
        package_id: str,
        field: str,
        tenant_id: str,
    ) -> Any:
        """Read a single field from package_config JSONB.

        Returns None if the package or field does not exist.
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                pkg = repo.get_package(media_buy_id, package_id)
                if pkg is None:
                    return None
                return pkg.package_config.get(field)
        except Exception as exc:
            logger.warning(f"[siteplug] Could not read package_config.{field}: {exc}")
            return None

    def _persist_entity_ids(
        self,
        *,
        media_buy_id: str,
        package_id: str,
        tenant_id: str,
        platform_id: int | None = None,
        agency_id: int | None = None,
        brand_id: int | None = None,
        advertiser_id: int | None = None,
        campaign_id: int | None = None,
    ) -> None:
        """Persist Siteplug entity IDs to package_config JSONB.

        Only writes keys that are explicitly provided (not None). Uses
        ``sqlalchemy.orm.attributes.flag_modified`` to ensure SQLAlchemy
        detects the JSONB mutation.

        Args:
            media_buy_id: AdCP media buy ID.
            package_id: AdCP package ID.
            tenant_id: Tenant ID for DB session.
            platform_id: Siteplug platform_id (optional).
            agency_id: Siteplug masteraccount_id (optional).
            brand_id: Siteplug brand_id (optional).
            advertiser_id: Siteplug advertiser_id (optional).
            campaign_id: Siteplug campaign_id (optional).
        """
        from sqlalchemy.orm import attributes

        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        updates: dict[str, int] = {}
        if platform_id is not None:
            updates["siteplug_platform_id"] = platform_id
        if agency_id is not None:
            updates["siteplug_agency_id"] = agency_id
        if brand_id is not None:
            updates["siteplug_brand_id"] = brand_id
        if advertiser_id is not None:
            updates["siteplug_advertiser_id"] = advertiser_id
        if campaign_id is not None:
            updates["siteplug_campaign_id"] = campaign_id

        if not updates:
            return

        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                pkg = repo.get_package(media_buy_id, package_id)
                if pkg is None:
                    logger.error(
                        f"[siteplug] _persist_entity_ids: package {package_id} not found "
                        f"in media buy {media_buy_id}"
                    )
                    return

                for key, value in updates.items():
                    pkg.package_config[key] = value
                attributes.flag_modified(pkg, "package_config")
                session.commit()
                self._log(f"[siteplug] persisted to package_config: {updates}")
        except Exception as exc:
            logger.error(f"[siteplug] Failed to persist entity IDs: {exc}", exc_info=True)

    # =========================================================================
    # Legacy campaign CRUD (used by other tasks)
    # =========================================================================

    async def create_campaign(
        self,
        name: str,
        platform_id: int,
        brand_id: int,
        campaign_type: int = 1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a campaign in Siteplug via POST /campaigns.

        Args:
            name: Campaign name
            platform_id: Siteplug platform ID
            brand_id: Siteplug brand ID
            campaign_type: Campaign type (1=KW, 2=RON, 3=CAT, 4=HYBRID, 5=PLA)
            **kwargs: Additional campaign parameters (advertiser_id, sol_id, etc.)

        Returns:
            Created campaign data with campaign_id
        """
        payload: dict[str, Any] = {
            "campaign_name": name,
            "platform_id": platform_id,
            "brand_id": brand_id,
            "campaign_type": campaign_type,
            **kwargs,
        }
        return await self.client.create_campaign(payload)

    async def update_campaign(
        self,
        campaign_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a campaign in Siteplug via PUT /campaigns/{id}.

        Args:
            campaign_id: Siteplug campaign ID
            data: Fields to update

        Returns:
            Updated campaign data
        """
        return await self.client.update_campaign(campaign_id, data)

    async def get_campaign_status(self, campaign_id: int) -> str:
        """Get the status of a campaign via GET /campaigns/{id}.

        Args:
            campaign_id: Siteplug campaign ID

        Returns:
            Campaign status string (e.g. "active", "paused", "pending")
        """
        data = await self.client.get_campaign(campaign_id)
        return data.get("status", "pending_activation")
