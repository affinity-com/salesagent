"""Siteplug campaign manager.

Handles entity provisioning and campaign lifecycle operations against the
Siteplug SSP API.

Provisioning strategy (Task 02-A + 02-B):
  Primary path  — POST /onboard (Phase 7, single 207 call)
  Fallback path — sequential POST per entity (Phase 2–6)

The primary path is attempted first. If the onboard endpoint is unavailable
(ENTITY_NOT_FOUND / connection error) the manager falls back to sequential
provisioning automatically.

Fixes applied (2026-06-18, post MR !12 review):
  Fix 1: POST /platforms field is "platform" not "platform_name"
  Fix 2: POST /brands removes platform_id (not in schema); adds is_product
  Fix 3: POST /advertisers field is "adv_name" not "advertiser_name";
         naming convention "{platform_name} – {brand_name}" (em-dash)
  Fix 4: POST /campaigns always includes campaign_name, deal_type, budget_type
  C1:    Agency step removed — agency_id auto-detected server-side from
         platform.masteraccount_id; all AdCP platforms are non-RTB → direct flow
  C3:    Advertiser resolved by name before creation to avoid duplicates
"""

import logging
from typing import Any

from src.adapters.siteplug.client import SiteplugAPIError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task 11 — Starting bid derivation (D17)
# ---------------------------------------------------------------------------

# Geo tier country sets (ISO 3166-1 alpha-2, upper-case)
GEO_TIER_1: frozenset[str] = frozenset({"US", "CA", "GB", "DE", "FR", "IT", "ES"})
GEO_TIER_2: frozenset[str] = frozenset(
    {"AU", "AT", "BE", "CH", "DK", "FI", "NL", "NO", "PL", "PT", "SE"}
)

# Base bid table: (tier, campaign_type) → USD bid
BASE_BIDS: dict[tuple[int, str], float] = {
    (1, "SD"): 0.10, (1, "SSS"): 0.05, (1, "SDC"): 0.10,
    (2, "SD"): 0.05, (2, "SSS"): 0.05, (2, "SDC"): 0.05,
    (3, "SD"): 0.01, (3, "SSS"): 0.01, (3, "SDC"): 0.01,
}


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
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        campaign_type: str,
        sol_id: int,
        is_product: int = 0,
        deal_type: str | None = None,
        budget_type: int | None = None,
        account_manager_name: str | None = None,
        sales_manager_name: str | None = None,
        bdam_name: str | None = None,
        tenant_id: str,
        idempotency_key: str | None = None,
        related_domains: list[str] | None = None,
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
            brand_name: Advertiser brand name.
            brand_domain: Advertiser brand domain (e.g. "example.com").
            vertical: IAB vertical category.
            sub_category: IAB sub-category.
            campaign_type: Siteplug campaign type string ("SD", "SSS", "SDC").
            sol_id: Source of Lead ID (must exist in SSP DB).
            is_product: 0 = home brand (default), 1 = product brand.
            deal_type: Deal type ("CPC", "CPA", "VCPC") — required for non-RTB.
            budget_type: Budget type int (1–5) — required for non-RTB.
            tenant_id: Tenant ID for DB session.
            idempotency_key: Optional idempotency key forwarded to the SSP API.
            related_domains: Optional additional TLD/geo domains from brand agent
                enrichment (e.g. ["nike.fr", "nike.com.au"]). Merged with
                brand_domain when whitelisting king domains for SDC campaigns.

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
                brand_name=brand_name,
                brand_domain=brand_domain,
                vertical=vertical,
                sub_category=sub_category,
                campaign_type=campaign_type,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                related_domains=related_domains,
            )
            return campaign_id

        except SiteplugAPIError as exc:
            # Fall back to sequential if the onboard route is not yet live
            # (ENTITY_NOT_FOUND = 404 means the route doesn't exist yet),
            # or if the brand already exists in staging (BRAND_ALREADY_EXISTS).
            if exc.status_code == 404 or exc.error_code in ("ENTITY_NOT_FOUND", "BRAND_ALREADY_EXISTS"):
                self._log(
                    f"[siteplug] /onboard fallback to sequential: {exc.error_code} — {exc}"
                )
            else:
                raise

        # ── Fallback path: sequential provisioning (Phase 2–6) ───────────
        return await self._provision_sequential(
            media_buy_id=media_buy_id,
            package_id=package_id,
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
            account_manager_name=account_manager_name,
            sales_manager_name=sales_manager_name,
            bdam_name=bdam_name,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            related_domains=related_domains,
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
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        campaign_type: str = "SDC",
        tenant_id: str,
        idempotency_key: str | None,
        related_domains: list[str] | None = None,
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
            "rtb_flag": 0,  # All AdCP campaigns are non-RTB
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
            f"brand={brand_name!r}"
        )

        body = await self.client.onboard(payload, idempotency_key=idempotency_key)

        # ── Parse 207 flat envelope ───────────────────────────────────────
        # Top-level keys: platform, agency, summary, results
        platform_id: int = body["platform"]["platform_id"]
        agency_id: int = body["agency"]["masteraccount_id"]  # 0 for non-RTB

        result = body["results"][0]
        result_status: str = result.get("status", "")

        if result_status in ("partial_failure", "failed"):
            # Find the first failed step's error_code
            steps = result.get("steps", {})
            for step_name in ("brand", "advertiser", "campaign"):
                step = steps.get(step_name, {})
                step_status = step.get("status", "")
                if step_status not in ("success", "skipped", None, ""):
                    error_code = step.get("error_code", "PARTIAL_FAILURE")
                    error_msg = step.get("error_message", step.get("message", ""))
                    # BRAND_ERROR "Brand already exists" — check if campaign was still created.
                    # When brand already exists, the onboard SP may still create the campaign
                    # using the existing brand. If campaign_id is present in the steps, use it.
                    if error_code == "BRAND_ERROR" and "already exists" in error_msg.lower():
                        campaign_step = steps.get("campaign", {})
                        campaign_id_from_onboard = campaign_step.get("campaign_id")
                        if campaign_id_from_onboard:
                            self._log(
                                f"[siteplug] brand already exists but campaign created: "
                                f"campaign_id={campaign_id_from_onboard} — using onboard result"
                            )
                            brand_id_from_onboard = steps.get("brand", {}).get("brand_id") or 0
                            advertiser_id_from_onboard = steps.get("advertiser", {}).get("advertiser_id") or 0
                            self._persist_entity_ids(
                                media_buy_id=media_buy_id,
                                package_id=package_id,
                                platform_id=platform_id,
                                agency_id=agency_id,
                                brand_id=brand_id_from_onboard,
                                advertiser_id=advertiser_id_from_onboard,
                                campaign_id=int(campaign_id_from_onboard),
                                tenant_id=tenant_id,
                            )
                            await self._mark_primary_campaign(
                                brand_id=brand_id_from_onboard,
                                new_campaign_id=int(campaign_id_from_onboard),
                                new_campaign_type=campaign_type,
                            )
                            return int(campaign_id_from_onboard)
                        # No campaign_id → fall back to sequential
                        raise SiteplugAPIError(
                            f"Brand already exists — falling back to sequential provisioning",
                            error_code="BRAND_ALREADY_EXISTS",
                        )
                    # CAMPAIGN_ERROR — staging AX bug: SP4/SP5 may return a non-zero error
                    # code even when the campaign record was committed to the DB. The brand
                    # and advertiser steps succeeded, so we have their IDs. Recover by
                    # fetching the most recently created campaign under the advertiser.
                    if step_name == "campaign" and error_code == "CAMPAIGN_ERROR":
                        brand_id_from_onboard = steps.get("brand", {}).get("brand_id") or 0
                        advertiser_id_from_onboard = steps.get("advertiser", {}).get("advertiser_id") or 0
                        self._log(
                            f"[siteplug] onboard CAMPAIGN_ERROR — attempting recovery via "
                            f"GET /campaigns?advertiser_id={advertiser_id_from_onboard}..."
                        )
                        try:
                            campaigns = await self.client.list_campaigns(
                                advertiser_id=advertiser_id_from_onboard
                            )
                            campaign_list = campaigns if isinstance(campaigns, list) else []
                            if campaign_list:
                                # Take the most recently created campaign (highest campaign_id)
                                recovered = max(campaign_list, key=lambda c: int(c.get("campaign_id", 0)))
                                recovered_id = int(recovered["campaign_id"])
                                self._log(
                                    f"[siteplug] CAMPAIGN_ERROR recovery: found campaign_id={recovered_id} "
                                    f"(name={recovered.get('campaign_name')!r}) — campaign was created despite CAMPAIGN_ERROR"
                                )
                                self._persist_entity_ids(
                                    media_buy_id=media_buy_id,
                                    package_id=package_id,
                                    platform_id=platform_id,
                                    agency_id=agency_id,
                                    brand_id=brand_id_from_onboard,
                                    advertiser_id=advertiser_id_from_onboard,
                                    campaign_id=recovered_id,
                                    tenant_id=tenant_id,
                                )
                                await self._mark_primary_campaign(
                                    brand_id=brand_id_from_onboard,
                                    new_campaign_id=recovered_id,
                                    new_campaign_type=campaign_type,
                                )
                                return recovered_id
                            self._log(
                                f"[siteplug] CAMPAIGN_ERROR recovery: no campaigns found for "
                                f"advertiser {advertiser_id_from_onboard} — re-raising"
                            )
                        except SiteplugAPIError:
                            self._log(
                                f"[siteplug] CAMPAIGN_ERROR recovery: list_campaigns failed — re-raising"
                            )
                    raise SiteplugAPIError(
                        f"Siteplug onboarding partial_failure at step '{step_name}': "
                        f"{error_msg} | full_step={step}",
                        error_code=error_code,
                    )
            raise SiteplugAPIError(
                f"Siteplug onboarding partial_failure (unknown step) | result={result}",
                error_code="PARTIAL_FAILURE",
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

        # Task 02c: whitelist king domains for SDC campaigns after brand creation.
        if campaign_type == "SDC" and brand_domain:
            await self._whitelist_king_domains(
                brand_id=brand_id,
                brand_domain=brand_domain,
                related_domains=related_domains,
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

        # Task 04 (D15): evaluate and set primary campaign for this brand.
        await self._mark_primary_campaign(
            brand_id=brand_id,
            new_campaign_id=campaign_id,
            new_campaign_type=campaign_type,
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
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        campaign_type: str,
        sol_id: int,
        is_product: int,
        deal_type: str | None,
        budget_type: int | None,
        account_manager_name: str | None = None,
        sales_manager_name: str | None = None,
        bdam_name: str | None = None,
        tenant_id: str,
        idempotency_key: str | None,
        related_domains: list[str] | None = None,
    ) -> int:
        """Provision entities sequentially: Platform → Brand → Advertiser → Campaign.

        Agency step is skipped unconditionally — agency_id is auto-detected
        server-side from platform.masteraccount_id. All AdCP platforms are
        non-RTB (is_external_rtb=0) so masteraccount_id=0 → direct flow always.

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

        # ── Step 2: Agency — SKIPPED (C1) ────────────────────────────────
        # agency_id is auto-detected server-side from platform.masteraccount_id.
        # All AdCP platforms are non-RTB → masteraccount_id=0 → direct flow.
        # We persist 0 so the idempotency guard recognises this step as done.
        agency_id_raw = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_agency_id", tenant_id
        )
        if agency_id_raw is None:
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                agency_id=0,
                tenant_id=tenant_id,
            )
        self._log("[siteplug] agency step skipped (non-RTB, agency_id=0)")

        # ── Step 3: Brand ─────────────────────────────────────────────────
        brand_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_brand_id", tenant_id
        )
        if not brand_id:
            brand_id = await self._resolve_or_create_brand(
                brand_name=brand_name,
                brand_domain=brand_domain,
                vertical=vertical,
                sub_category=sub_category,
                is_product=is_product,
                idempotency_key=idempotency_key,
            )
            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                brand_id=brand_id,
                tenant_id=tenant_id,
            )

            # Task 02c: whitelist king domains for SiteDiscover (SDC) campaigns.
            # Required so the SD traffic matching engine can associate publisher
            # traffic with the correct brand. Non-fatal — ops can whitelist manually.
            if campaign_type == "SDC" and brand_domain:
                await self._whitelist_king_domains(
                    brand_id=brand_id,
                    brand_domain=brand_domain,
                    related_domains=related_domains,
                )
        else:
            brand_id = int(brand_id)
            self._log(f"[siteplug] brand already provisioned (brand_id={brand_id}), skipping.")

        # ── Step 4: Advertiser (resolve by name before creating) ──────────
        advertiser_id = self._read_package_config_field(
            media_buy_id, package_id, "siteplug_advertiser_id", tenant_id
        )
        if not advertiser_id:
            advertiser_id = await self._resolve_or_create_advertiser(
                platform_id=platform_id,
                brand_id=brand_id,
                platform_name=platform_name,
                brand_name=brand_name,
                idempotency_key=idempotency_key,
            )
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
            # deal_type and budget_type are always required for non-RTB platforms.
            # Use media_buy_id suffix to guarantee uniqueness — avoids 409 on retry
            # since each AdCP media buy maps to exactly one Siteplug campaign.
            import uuid as _uuid
            campaign_name = (
                f"{platform_name} {brand_name} {campaign_type} "
                f"{media_buy_id[-8:] if len(media_buy_id) >= 8 else _uuid.uuid4().hex[:8]}"
            )
            campaign_payload: dict[str, Any] = {
                "advertiser_id": advertiser_id,
                "platform_id": platform_id,
                "brand_id": brand_id,
                "campaign_type": campaign_type,
                "sol_id": sol_id,
                "campaign_name": campaign_name,
                "deal_type": deal_type or "CPC",
                "budget_type": int(budget_type) if budget_type is not None else 1,
            }
            # SM/BDAM/AM names: only include when explicitly set in impl_config.
            # When omitted, the SP uses its hardcoded default IDs (SM_ID, BDAM_ID, ADMIN_ID
            # from config('constants.direct_campaign')). Passing a name that doesn't exist
            # in ss_sales_manager_master causes a 422 pre-flight error.
            if account_manager_name:
                campaign_payload["account_manager_name"] = account_manager_name
            if sales_manager_name:
                campaign_payload["sales_manager_name"] = sales_manager_name
            if bdam_name:
                campaign_payload["bdam_name"] = bdam_name
            self._log(f"[siteplug] POST /campaigns payload: {campaign_payload}")

            try:
                campaign_data = await self.client.create_campaign(
                    campaign_payload,
                    idempotency_key=idempotency_key,
                )
                campaign_id = int(campaign_data.get("campaign_id", 0))
                self._log(f"[siteplug] campaign created: campaign_id={campaign_id}")
            except SiteplugAPIError as exc:
                # Staging AX bug: SP4/SP5 may return SP_ERROR (HTTP 500) even when
                # the campaign record was committed to the DB. Recover by searching
                # for the campaign by name under the advertiser.
                if exc.error_code != "SP_ERROR":
                    raise
                self._log(
                    f"[siteplug] POST /campaigns returned SP_ERROR — "
                    f"attempting recovery via GET /campaigns?advertiser_id={advertiser_id}..."
                )
                try:
                    campaigns = await self.client.list_campaigns(advertiser_id=advertiser_id)
                    recovered_id: int | None = None
                    for c in (campaigns if isinstance(campaigns, list) else []):
                        if c.get("campaign_name", "") == campaign_name:
                            recovered_id = int(c["campaign_id"])
                            break
                    if recovered_id:
                        self._log(
                            f"[siteplug] SP_ERROR recovery: found campaign_id={recovered_id} "
                            f"by name '{campaign_name}' — campaign was created despite SP_ERROR"
                        )
                        campaign_id = recovered_id
                    else:
                        # Campaign not found — re-raise the original error
                        self._log(
                            f"[siteplug] SP_ERROR recovery: campaign '{campaign_name}' "
                            f"not found in advertiser {advertiser_id} — re-raising"
                        )
                        raise
                except SiteplugAPIError:
                    raise
                except Exception as search_exc:
                    self._log(
                        f"[siteplug] SP_ERROR recovery: search failed ({search_exc}) — re-raising original"
                    )
                    raise exc

            self._persist_entity_ids(
                media_buy_id=media_buy_id,
                package_id=package_id,
                campaign_id=campaign_id,
                tenant_id=tenant_id,
            )
        else:
            campaign_id = int(campaign_id)
            self._log(f"[siteplug] campaign already provisioned (campaign_id={campaign_id}), skipping.")

        # Task 04 (D15): evaluate and set primary campaign for this brand.
        await self._mark_primary_campaign(
            brand_id=brand_id,
            new_campaign_id=campaign_id,
            new_campaign_type=campaign_type,
        )

        return campaign_id

    # =========================================================================
    # Task 02c — King domain whitelisting helper
    # =========================================================================

    async def _whitelist_king_domains(
        self,
        *,
        brand_id: int,
        brand_domain: str,
        related_domains: list[str] | None = None,
    ) -> None:
        """Whitelist king domains for a brand via PUT /brands/{id}.

        Called after brand creation for SDC campaigns only. Non-fatal — a
        failure is logged but does not block campaign provisioning. Ops can
        whitelist manually via the Siteplug admin panel if needed.

        Args:
            brand_id: Siteplug brand_id.
            brand_domain: Primary brand domain (e.g. "nike.com").
            related_domains: Optional additional TLD/geo domains from brand agent
                enrichment (e.g. ["nike.fr", "nike.com.au"]). Deduplicated and
                merged with brand_domain before sending to the SSP API.
        """
        # Deduplicate: primary domain first, then any related domains not already present
        seen: set[str] = {brand_domain}
        domains: list[str] = [brand_domain]
        for d in (related_domains or []):
            if d and d not in seen:
                seen.add(d)
                domains.append(d)
        self._log(
            f"[siteplug] Task 02c: whitelisting king domains for brand_id={brand_id}: {domains}"
        )
        try:
            await self.client.update_brand_king_domains(brand_id=brand_id, domains=domains)
            self._log(f"[siteplug] king domain whitelist updated for brand_id={brand_id}")
        except Exception as exc:
            # Non-fatal: log and continue — ops can whitelist manually
            logger.warning(
                f"[siteplug] king domain whitelist failed for brand_id={brand_id} "
                f"(non-fatal, ops can whitelist manually): {exc}"
            )

    # =========================================================================
    # Task 04 (D15) — Primary campaign marking helper
    # =========================================================================

    async def _mark_primary_campaign(
        self,
        *,
        brand_id: int,
        new_campaign_id: int,
        new_campaign_type: str,
    ) -> None:
        """Evaluate and set the primary campaign for a brand after creation.

        Applies the deterministic hierarchy: SD > SDC > SSS > any.
        Calls ``PUT /campaigns/{id}`` with ``is_primary=true`` on the winner.
        Non-fatal — a failure is logged but does not block provisioning.

        Args:
            brand_id: Siteplug brand_id to list campaigns for.
            new_campaign_id: The campaign just created.
            new_campaign_type: Campaign type of the new campaign ("SD", "SDC", "SSS").
        """
        _TYPE_PRIORITY: dict[str, int] = {"SD": 3, "SDC": 2, "SSS": 1}

        try:
            campaigns: list[dict] = await self.client.list_campaigns(brand_id=brand_id)
        except Exception as exc:
            logger.warning(
                f"[siteplug] primary marking: failed to list campaigns for brand_id={brand_id} "
                f"(non-fatal): {exc}"
            )
            return

        if not campaigns:
            # Newly created campaign is the only one — mark it primary.
            primary_id = new_campaign_id
        else:
            # Find the campaign with the highest type priority.
            # Include the new campaign in the evaluation set.
            all_campaigns = campaigns if any(
                c.get("campaign_id") == new_campaign_id for c in campaigns
            ) else campaigns + [{"campaign_id": new_campaign_id, "campaign_type": new_campaign_type}]

            best: dict | None = None
            best_priority = -1
            for c in all_campaigns:
                ctype = c.get("campaign_type", "")
                priority = _TYPE_PRIORITY.get(ctype, 0)
                if priority > best_priority:
                    best_priority = priority
                    best = c

            primary_id = int(best["campaign_id"]) if best else new_campaign_id

        self._log(
            f"[siteplug] primary marking: brand_id={brand_id} → "
            f"primary_campaign_id={primary_id} (new_campaign_id={new_campaign_id})"
        )
        try:
            await self.client.update_campaign(primary_id, {"is_primary": True})
            self._log(f"[siteplug] campaign_id={primary_id} marked as primary.")
        except Exception as exc:
            logger.warning(
                f"[siteplug] primary marking: PUT /campaigns/{primary_id} failed "
                f"(non-fatal): {exc}"
            )

    # =========================================================================
    # Platform resolution helper
    # =========================================================================

    async def _create_or_resolve_platform(
        self,
        *,
        platform_name: str,
        idempotency_key: str | None,
    ) -> int:
        """Resolve an existing platform by name, or create it if not found.

        In production, the platform almost always already exists in IC.
        Strategy: GET /platforms?search= first; only POST if not found.
        On 409 from POST, fall back to GET /platforms?search= again.

        Fix 1: POST /platforms uses field "platform" (not "platform_name").

        Returns:
            Siteplug platform_id.
        """
        # Try to resolve existing platform first (normal path — platform exists)
        self._log(f"[siteplug] resolving platform '{platform_name}' via GET /platforms?search=...")
        platforms = await self.client.list_platforms(search=platform_name)
        items: list[dict[str, Any]] = (
            platforms if isinstance(platforms, list)
            else platforms.get("data", []) if isinstance(platforms, dict)
            else []
        )
        for p in items:
            if p.get("platform", "").lower() == platform_name.lower():
                platform_id = int(p["platform_id"])
                self._log(f"[siteplug] resolved existing platform_id={platform_id}")
                return platform_id

        # Not found — create it (rare: new network)
        self._log(f"[siteplug] platform '{platform_name}' not found — creating via POST /platforms...")
        try:
            data = await self.client.create_platform(
                {"platform": platform_name},  # Fix 1: "platform" not "platform_name"
                idempotency_key=idempotency_key,
            )
            platform_id = int(data.get("platform_id", 0))
            self._log(f"[siteplug] platform created: platform_id={platform_id}")
            return platform_id

        except SiteplugAPIError as exc:
            if exc.error_code != "ENTITY_ALREADY_EXISTS":
                raise

            # 409 race condition — resolve again
            self._log(
                f"[siteplug] platform '{platform_name}' 409 on create — "
                "resolving via GET /platforms?search= (race condition)..."
            )
            platforms = await self.client.list_platforms(search=platform_name)
            items = (
                platforms if isinstance(platforms, list)
                else platforms.get("data", []) if isinstance(platforms, dict)
                else []
            )
            for p in items:
                if p.get("platform", "").lower() == platform_name.lower():
                    platform_id = int(p["platform_id"])
                    self._log(f"[siteplug] resolved existing platform_id={platform_id}")
                    return platform_id

            raise SiteplugAPIError(
                f"Platform '{platform_name}' returned 409 but could not be resolved via search.",
                error_code="PLATFORM_LOOKUP_ERROR",
            )

    # =========================================================================
    # Brand resolution helper
    # =========================================================================

    async def _resolve_or_create_brand(
        self,
        *,
        brand_name: str,
        brand_domain: str,
        vertical: str,
        sub_category: str,
        is_product: int,
        idempotency_key: str | None,
    ) -> int:
        """Create a brand, or resolve the existing one if it already exists.

        Strategy:
          1. POST /brands — if successful, return brand_id.
          2. If the API returns "Brand already exists" (BRAND_ERROR), fall back
             to GET /brands?search=<brand_name> and match by name.
          3. If still not found, re-raise the original error.

        Returns:
            Siteplug brand_id (int).
        """
        try:
            brand_data = await self.client.create_brand(
                {
                    "brand_name": brand_name,
                    "brand_domain": brand_domain,
                    "vertical": vertical,
                    "sub_category": sub_category,
                    "is_product": is_product,
                },
                idempotency_key=idempotency_key,
            )
            brand_id = int(brand_data.get("brand_id", 0))
            self._log(f"[siteplug] brand created: brand_id={brand_id}")
            return brand_id

        except SiteplugAPIError as exc:
            err_msg = str(exc).lower()
            if "already exists" not in err_msg and exc.error_code not in (
                "BRAND_ERROR",
                "ENTITY_ALREADY_EXISTS",
            ):
                raise

            # Brand already exists — resolve by name
            self._log(
                f"[siteplug] brand '{brand_name}' already exists — "
                "resolving via GET /brands?search=..."
            )
            brands = await self.client.list_brands(search=brand_name)
            items: list[dict[str, Any]] = (
                brands if isinstance(brands, list)
                else brands.get("data", []) if isinstance(brands, dict)
                else []
            )
            for b in items:
                if b.get("brand_name", "").lower() == brand_name.lower():
                    brand_id = int(b["brand_id"])
                    self._log(f"[siteplug] resolved existing brand_id={brand_id}")
                    return brand_id

            raise SiteplugAPIError(
                f"Brand '{brand_name}' already exists but could not be resolved via search.",
                error_code="BRAND_LOOKUP_ERROR",
            )

    # =========================================================================
    # Advertiser resolution helper (C3)
    # =========================================================================

    async def _resolve_or_create_advertiser(
        self,
        *,
        platform_id: int,
        brand_id: int,
        platform_name: str,
        brand_name: str,
        idempotency_key: str | None,
    ) -> int:
        """Resolve an existing advertiser by name convention, or create it.

        Naming convention (IC standard, confirmed by Milan):
            "{platform_name} – {brand_name}"  (em-dash)

        Strategy:
          1. GET /advertisers?search="{platform_name} – {brand_name}"
          2. If found and platform_id matches → reuse existing advertiser_id
          3. If not found → POST /advertisers with adv_name using convention
          4. On 409 → resolve via GET /advertisers?search= again

        Fix 3: field is "adv_name" (not "advertiser_name").

        Returns:
            Siteplug advertiser_id.
        """
        adv_name = f"{platform_name} \u2013 {brand_name}"  # em-dash (–)

        # Try to resolve existing advertiser first
        self._log(f"[siteplug] resolving advertiser '{adv_name}' via GET /advertisers?search=...")
        try:
            advertisers = await self.client.list_advertisers(search=adv_name)
            items: list[dict[str, Any]] = (
                advertisers if isinstance(advertisers, list)
                else advertisers.get("data", []) if isinstance(advertisers, dict)
                else []
            )
            self._log(f"[siteplug] advertiser search returned {len(items)} items: {[a.get('advertiser_id') for a in items]}")
            for a in items:
                self._log(
                    f"[siteplug] advertiser item: id={a.get('advertiser_id')} "
                    f"adv_name={a.get('adv_name')!r} platform_id={a.get('platform_id')} "
                    f"brand_id={a.get('brand_id')}"
                )
                # Strict match: name + platform_id both present and match
                name_match = a.get("adv_name", "").lower() == adv_name.lower()
                plat_id_val = a.get("platform_id")
                platform_match = plat_id_val is not None and int(plat_id_val) == platform_id
                if name_match and platform_match:
                    advertiser_id = int(a["advertiser_id"])
                    self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (strict match)")
                    return advertiser_id

            # Relaxed match: staging API may omit platform_id/brand_id in list response
            # and use a different name format. If only 1 result returned, trust it.
            if len(items) == 1:
                advertiser_id = int(items[0]["advertiser_id"])
                self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (single result fallback)")
                return advertiser_id
        except SiteplugAPIError:
            pass  # Search failure is non-fatal — proceed to create

        # Not found — create it
        self._log(f"[siteplug] advertiser '{adv_name}' not found — creating via POST /advertisers...")
        try:
            advertiser_data = await self.client.create_advertiser(
                {
                    "platform_id": platform_id,
                    "brand_id": brand_id,
                    "adv_name": adv_name,  # Fix 3: "adv_name" not "advertiser_name"
                },
                idempotency_key=idempotency_key,
            )
            advertiser_id = int(advertiser_data.get("advertiser_id", 0))
            self._log(f"[siteplug] advertiser created: advertiser_id={advertiser_id}")
            return advertiser_id

        except SiteplugAPIError as exc:
            # Catch any "already exists" condition: 409 status, ENTITY_ALREADY_EXISTS code,
            # or "email already in use" message (staging AX API variant).
            is_conflict = (
                exc.status_code == 409
                or exc.error_code == "ENTITY_ALREADY_EXISTS"
                or "already" in str(exc).lower()
            )
            if not is_conflict:
                raise

            self._log(
                f"[siteplug] advertiser '{adv_name}' conflict on create ({exc.error_code}) — "
                "resolving via multiple search strategies..."
            )

            def _extract_items(resp: Any) -> list[dict[str, Any]]:
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    return resp.get("data", [])
                return []

            # Strategy 1: search by brand_id filter
            try:
                resp = await self.client.list_advertisers(brand_id=brand_id)
                for a in _extract_items(resp):
                    if int(a.get("platform_id", 0)) == platform_id:
                        advertiser_id = int(a["advertiser_id"])
                        self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (by brand_id filter)")
                        return advertiser_id
            except SiteplugAPIError:
                pass

            # Strategy 2: search by platform_id filter
            try:
                resp = await self.client.list_advertisers(platform_id=platform_id)
                for a in _extract_items(resp):
                    if int(a.get("brand_id", 0)) == brand_id:
                        advertiser_id = int(a["advertiser_id"])
                        self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (by platform_id filter)")
                        return advertiser_id
            except SiteplugAPIError:
                pass

            # Strategy 3: search by name (em-dash variant)
            try:
                resp = await self.client.list_advertisers(search=adv_name)
                items = _extract_items(resp)
                for a in items:
                    if int(a.get("platform_id", 0)) == platform_id:
                        advertiser_id = int(a["advertiser_id"])
                        self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (by name search)")
                        return advertiser_id
                # Fallback: any item matching brand_id
                for a in items:
                    if int(a.get("brand_id", 0)) == brand_id:
                        advertiser_id = int(a["advertiser_id"])
                        self._log(f"[siteplug] resolved existing advertiser_id={advertiser_id} (by brand_id in name search)")
                        return advertiser_id
            except SiteplugAPIError:
                pass

            raise SiteplugAPIError(
                f"Advertiser '{adv_name}' conflict but could not be resolved via search.",
                error_code="ADVERTISER_LOOKUP_ERROR",
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
    # Task 11 — Starting bid derivation (D17)
    # =========================================================================

    def _derive_starting_bid(
        self,
        request: Any,
        package: Any,
        campaign_type: str,
        product_config: Any = None,
    ) -> float:
        """Derive a starting bid from geo tier and product type.

        Called during ad group creation when the buyer has not supplied an
        explicit ``bid_price``.  An explicit ``package.bid_price`` always
        takes precedence over the derived value.

        Geo tier classification:
            Tier 1 — US, CA, GB, DE, FR, IT, ES
            Tier 2 — AU, AT, BE, CH, DK, FI, NL, NO, PL, PT, SE
            Tier 3 — IN, BR, MX (and all other countries)

        Multi-country targeting uses the highest tier (lowest number) present.
        No geo targeting defaults to Tier 2.

        Volume adjustment (from ProductInventoryMapping zone stats):
            Low  (< bid_volume_low_threshold)  → ×1.5  (bid aggressively)
            High (> bid_volume_high_threshold) → ×0.7  (bid conservatively)
            Normal / no data                   → ×1.0

        Args:
            request: AdCP ``create_media_buy`` request object.
            package: AdCP package (line item) object.
            campaign_type: Siteplug campaign type string ("SD" | "SSS" | "SDC").
            product_config: Optional product config object with
                ``bid_volume_low_threshold`` / ``bid_volume_high_threshold``
                fields (falls back to defaults when None or absent).

        Returns:
            Bid amount in USD, rounded to 4 decimal places.
        """
        # Explicit bid always wins
        explicit_bid = getattr(package, "bid_price", None)
        if explicit_bid is not None:
            return float(explicit_bid)

        # ── Geo tier ─────────────────────────────────────────────────────────
        geo = getattr(request, "targeting", None)
        geo_obj = getattr(geo, "geo", None) if geo else None
        raw_countries = getattr(geo_obj, "countries", None) if geo_obj else None
        countries: frozenset[str] = frozenset(
            c.upper() for c in (raw_countries or []) if c
        )

        if countries & GEO_TIER_1:
            tier = 1
        elif countries & GEO_TIER_2:
            tier = 2
        else:
            # No geo specified → Tier 2 default; any other country → Tier 3
            tier = 3 if countries else 2

        base = BASE_BIDS.get((tier, campaign_type), 0.05)

        # ── Volume adjustment ─────────────────────────────────────────────────
        product_id: str | None = getattr(request, "product_id", None)
        volume = self._get_zone_volume(product_id) if product_id else None

        low_thresh: int = (
            getattr(product_config, "bid_volume_low_threshold", None) or 10_000
        )
        high_thresh: int = (
            getattr(product_config, "bid_volume_high_threshold", None) or 1_000_000
        )

        if volume is not None and volume < low_thresh:
            multiplier = 1.5
        elif volume is not None and volume > high_thresh:
            multiplier = 0.7
        else:
            multiplier = 1.0

        bid = round(base * multiplier, 4)
        self._log(
            f"[siteplug] _derive_starting_bid: tier={tier} campaign_type={campaign_type!r} "
            f"base={base} volume={volume} multiplier={multiplier} → bid={bid}"
        )
        return bid

    def _get_zone_volume(self, product_id: str) -> int | None:
        """Return total query volume for the product's zones from ProductInventoryMapping.

        Reads ``zone_stats.query_volume`` from each mapping row.  Returns
        ``None`` when no mappings exist or when no row carries zone_stats
        (the column is not yet present on the model — volume adjustment will
        be skipped and the ×1.0 multiplier applied).

        Args:
            product_id: AdCP product ID to look up.

        Returns:
            Total query volume (int) or None if unavailable.
        """
        try:
            from src.core.database.database_session import get_db_session
            from src.core.database.models import ProductInventoryMapping

            with get_db_session() as session:
                mappings = (
                    session.query(ProductInventoryMapping)
                    .filter(ProductInventoryMapping.product_id == product_id)
                    .all()
                )
            if not mappings:
                return None
            total = sum(
                (m.zone_stats or {}).get("query_volume", 0)
                for m in mappings
                if hasattr(m, "zone_stats")
            )
            return total if total > 0 else None
        except Exception as exc:
            logger.debug(
                f"[siteplug] _get_zone_volume: could not read zone stats for "
                f"product_id={product_id!r}: {exc}"
            )
            return None

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
