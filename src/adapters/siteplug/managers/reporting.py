"""Siteplug Reporting Manager.

Handles delivery reporting and campaign snapshot operations for the Siteplug
SSP adapter (Task 05).

SSP API endpoints (wired in SiteplugClient — stubs until API is deployed):
  GET /ssp/v1/campaigns/{id}/delivery          → get_delivery()
  GET /ssp/v1/campaigns/{id}/delivery/snapshot → get_snapshot()

Field mapping (SSP API → AdCP):
  totals.impressions          → delivery.impressions
  totals.clicks               → delivery.clicks
  totals.spend                → delivery.spend
  totals.ctr                  → delivery.ctr
  ad_groups[].ad_group_id     → packages[].package_id  (via package_config)
  ad_groups[].impressions     → packages[].impressions
  ad_groups[].clicks          → packages[].clicks
  ad_groups[].spend           → packages[].spend
  dimensions.geo              → reporting_dimensions.geo
  dimensions.device_type      → reporting_dimensions.device_type
  data_freshness              → data_freshness (passed through)

NOTE (K4): AdCP 3.0 does not include a 'keyword' dimension in
get_media_buy_delivery. Keyword-level reporting is not surfaced via the AdCP
protocol. Buyers needing keyword-level data must use the Siteplug advertiser
dashboard directly.

Ad group → package_id mapping:
  Task 04 stores siteplug_adgroup_id in package_config for each package.
  This manager reads all packages for a media buy from the DB and builds the
  reverse mapping (adgroup_id → package_id) at query time.  If Task 04 has
  not yet run (adgroup IDs not yet stored), by_package degrades gracefully
  to an empty list.

Data freshness tiers (from SSP API data_freshness.latency_tier):
  "realtime" → data is seconds old (Redis)
  "daily"    → data is hours old (MySQL aggregation)
  "delayed"  → data is days old (Hadoop/Impala)
"""

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugReportingManager:
    """Manages Siteplug delivery reporting and snapshots.

    Reads campaign delivery data from the SSP API and maps it to AdCP
    delivery schema objects.  The SSP API client methods are stubs until
    the delivery endpoint is deployed; the mapping logic is fully implemented
    so that switching to the real API requires only a client-layer change.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialise the reporting manager.

        Args:
            client: SiteplugClient instance.
            log_func: Optional logging function from the adapter.
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    # =========================================================================
    # Public API
    # =========================================================================

    async def get_delivery(
        self,
        campaign_id: int,
        media_buy_id: str,
        tenant_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        dimensions: list[str] | None = None,
        geo_level: str | None = None,
    ) -> dict[str, Any]:
        """Fetch delivery stats and map to AdCP delivery format.

        Calls the SSP API delivery endpoint (stub until deployed) and maps
        the response to a dict that ``SiteplugAdapter.get_media_buy_delivery``
        can use to build an ``AdapterGetMediaBuyDeliveryResponse``.

        Args:
            campaign_id: Siteplug campaign ID (from package_config).
            media_buy_id: AdCP media buy ID (used to look up package mappings).
            tenant_id: Tenant ID for DB access.
            start_date: Optional start date filter (YYYY-MM-DD).
            end_date: Optional end date filter (YYYY-MM-DD).
            dimensions: Optional AdCP dimension keys (geo, device_type, placement).
            geo_level: Required when "geo" dimension requested.

        Returns:
            Dict with keys: impressions, clicks, spend, ctr, by_package,
            data_freshness, reporting_dimensions.
        """
        self._log(
            f"[siteplug] ReportingManager.get_delivery: "
            f"campaign_id={campaign_id} start={start_date} end={end_date} "
            f"dimensions={dimensions}"
        )

        # ── Call SSP API (stub returns {} until API is deployed) ──────────
        raw = await self.client.get_campaign_delivery(
            campaign_id,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            geo_level=geo_level,
        )

        # ── Map campaign-level totals ─────────────────────────────────────
        totals: dict[str, Any] = raw.get("totals", {})
        impressions: float = float(totals.get("impressions", 0))
        clicks: float | None = _optional_float(totals.get("clicks"))
        spend: float = float(totals.get("spend", 0.0))
        ctr: float | None = _optional_float(totals.get("ctr"))

        # ── Build ad_group → package_id reverse mapping ───────────────────
        # Task 04 stores siteplug_adgroup_id in package_config for each
        # package.  We read all packages for this media buy and build the
        # reverse map.  Degrades gracefully to {} if Task 04 hasn't run yet.
        adgroup_to_package = self._build_adgroup_package_map(media_buy_id, tenant_id)

        # ── Map per-ad-group breakdown → AdCP by_package ─────────────────
        by_package: list[dict[str, Any]] = []
        for ag in raw.get("ad_groups", []):
            ag_id = ag.get("ad_group_id")
            package_id = adgroup_to_package.get(ag_id)
            if package_id is None:
                # Ad group not yet mapped to a package (Task 04 pending)
                logger.debug(
                    "[siteplug] get_delivery: ad_group_id=%s has no package mapping, skipping",
                    ag_id,
                )
                continue
            by_package.append(
                {
                    "package_id": package_id,
                    "impressions": int(ag.get("impressions", 0)),
                    "spend": float(ag.get("spend", 0.0)),
                    "clicks": _optional_int(ag.get("clicks")),
                }
            )

        # ── Map reporting dimensions ──────────────────────────────────────
        # AdCP uses "geo", "device_type", "device_platform", "audience",
        # "placement" — NOT "by_geo", "by_device_type", etc.
        reporting_dimensions: dict[str, Any] = {}
        raw_dims: dict[str, Any] = raw.get("dimensions", {})
        if "geo" in raw_dims:
            reporting_dimensions["geo"] = raw_dims["geo"]
        if "device_type" in raw_dims:
            reporting_dimensions["device_type"] = raw_dims["device_type"]
        if "device_platform" in raw_dims:
            reporting_dimensions["device_platform"] = raw_dims["device_platform"]
        if "audience" in raw_dims:
            reporting_dimensions["audience"] = raw_dims["audience"]
        if "placement" in raw_dims:
            reporting_dimensions["placement"] = raw_dims["placement"]

        # ── Pass through data freshness ───────────────────────────────────
        data_freshness: dict[str, Any] | None = raw.get("data_freshness")

        return {
            "impressions": impressions,
            "clicks": clicks,
            "spend": spend,
            "ctr": ctr,
            "by_package": by_package,
            "data_freshness": data_freshness,
            "reporting_dimensions": reporting_dimensions,
        }

    async def get_snapshot(
        self,
        campaign_id: int,
        media_buy_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        """Fetch a point-in-time campaign snapshot and map to AdCP format.

        Calls the SSP API snapshot endpoint (stub until deployed) and maps
        the response to a dict that ``SiteplugAdapter.get_packages_snapshot``
        can use to build per-package ``Snapshot`` objects.

        SSP API → AdCP snapshot mapping:
          campaign_budget → total_budget
          campaign_spend  → total_spend
          pacing_index    → pacing_index
          ad_groups[]     → packages[] (via adgroup → package_id mapping)

        Args:
            campaign_id: Siteplug campaign ID (from package_config).
            media_buy_id: AdCP media buy ID (used to look up package mappings).
            tenant_id: Tenant ID for DB access.

        Returns:
            Dict with keys: total_budget, total_spend, pacing_index, packages.
            ``packages`` is a list of dicts with package_id, impressions,
            spend, pacing_index, delivery_status.
        """
        self._log(
            f"[siteplug] ReportingManager.get_snapshot: campaign_id={campaign_id}"
        )

        # ── Call SSP API (stub returns {} until API is deployed) ──────────
        raw = await self.client.get_campaign_snapshot(campaign_id)

        # ── Map campaign-level snapshot fields ────────────────────────────
        total_budget: float = float(raw.get("campaign_budget", 0.0))
        total_spend: float = float(raw.get("campaign_spend", 0.0))
        pacing_index: float | None = _optional_float(raw.get("pacing_index"))

        # ── Build ad_group → package_id reverse mapping ───────────────────
        adgroup_to_package = self._build_adgroup_package_map(media_buy_id, tenant_id)

        # ── Map per-ad-group snapshot → AdCP packages ─────────────────────
        packages: list[dict[str, Any]] = []
        as_of = datetime.now(UTC)

        for ag in raw.get("ad_groups", []):
            ag_id = ag.get("ad_group_id")
            package_id = adgroup_to_package.get(ag_id)
            if package_id is None:
                logger.debug(
                    "[siteplug] get_snapshot: ad_group_id=%s has no package mapping, skipping",
                    ag_id,
                )
                continue
            packages.append(
                {
                    "package_id": package_id,
                    "impressions": float(ag.get("impressions", 0)),
                    "spend": float(ag.get("spend", 0.0)),
                    "clicks": _optional_float(ag.get("clicks")),
                    "pacing_index": _optional_float(ag.get("pacing_index")),
                    "delivery_status": ag.get("delivery_status"),
                    "as_of": as_of,
                }
            )

        return {
            "total_budget": total_budget,
            "total_spend": total_spend,
            "pacing_index": pacing_index,
            "packages": packages,
            "as_of": as_of,
        }

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _build_adgroup_package_map(
        self, media_buy_id: str, tenant_id: str
    ) -> dict[int, str]:
        """Build a reverse mapping from Siteplug ad_group_id → AdCP package_id.

        Reads all packages for the given media buy from the DB and extracts
        the ``siteplug_adgroup_id`` field written by Task 04.

        Returns an empty dict if Task 04 has not yet run (no adgroup IDs
        stored) — callers degrade gracefully by skipping per-package breakdown.

        Args:
            media_buy_id: AdCP media buy ID.
            tenant_id: Tenant ID for DB session.

        Returns:
            Dict mapping adgroup_id (int) → package_id (str).
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories.media_buy import MediaBuyRepository

        mapping: dict[int, str] = {}
        try:
            with get_db_session() as session:
                repo = MediaBuyRepository(session, tenant_id)
                packages = repo.get_packages(media_buy_id)
                for pkg in packages:
                    cfg = pkg.package_config or {}
                    adgroup_id = cfg.get("siteplug_adgroup_id")
                    if adgroup_id is not None:
                        mapping[int(adgroup_id)] = pkg.package_id
        except Exception as exc:
            logger.warning(
                "[siteplug] _build_adgroup_package_map: could not read packages "
                "for media_buy_id=%s: %s",
                media_buy_id,
                exc,
            )
        return mapping


# =========================================================================
# Module-level helpers
# =========================================================================


def _optional_float(value: Any) -> float | None:
    """Return float(value) or None if value is None/falsy-zero-safe."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    """Return int(value) or None if value is None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
