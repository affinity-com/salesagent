"""Siteplug inventory manager.

Syncs traffic sources (zones) from the Siteplug SSP API into the sales
agent's ``ProductInventoryMapping`` table and exposes zone lookup helpers
used by ``manage_placements`` and ``discover_products``.

Architecture notes
------------------
* All zone data comes from the SSP API — the adapter never touches Siteplug
  databases directly (design decision D1 from siteplug-index.md).
* The list endpoint (``GET /ssp/v1/inventory``) queries IC only — no AX
  cross-join.  Status comes from IC ``site_status``.
* The get-by-id endpoint (``GET /ssp/v1/inventory/{id}``) adds AX delivery
  stats (7-day impressions/clicks/CTR, 30-day avg daily impressions).
* ``transparency_flag`` is stored and returned as the string enum
  ``transparent`` / ``non_transparent`` — never as an integer.
* ``customer_type`` (``publisher_type`` in the API response) is a raw
  varchar pass-through — not normalised or enumerated.
* Sync follows the GAMSyncManager pattern: create a ``SyncJob`` record,
  mark it running, iterate all pages, upsert ``ProductInventoryMapping``
  rows, deactivate stale rows, then mark the job completed/failed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from src.adapters.base_inventory import BaseInventoryManager, InventoryItem
from src.core.database.models import ProductInventoryMapping, SyncJob
from src.core.database.repositories.product_inventory_mapping import (
    ProductInventoryMappingRepository,
)

if TYPE_CHECKING:
    from src.adapters.siteplug.client import SiteplugClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_TYPE = "siteplug"
_INVENTORY_TYPE = "zone"
_PAGE_SIZE = 200  # max allowed by SSP API contract


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


class SiteplugZone(InventoryItem):
    """Represents a single Siteplug traffic source (zone).

    Field names match the SSP API response contract defined in
    ``specs/inventory_specs/api-contract.md``.
    """

    def __init__(
        self,
        zone_id: int,
        zone_name: str,
        domain: str | None,
        publisher_id: int,
        publisher_name: str,
        publisher_type: str,
        implementation_type: str | None,
        source_type: str | None,
        transparency: str,
        status: int,
        created_at: str | None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(item_id=str(zone_id), name=zone_name)
        self.zone_id = zone_id
        self.zone_name = zone_name
        self.domain = domain
        self.publisher_id = publisher_id
        self.publisher_name = publisher_name
        # Raw varchar pass-through — not normalised (spec: customer_type)
        self.publisher_type = publisher_type
        self.implementation_type = implementation_type
        self.source_type = source_type
        # String enum: "transparent" | "non_transparent" (spec: transparency_flag)
        self.transparency = transparency
        self.status = status
        self.created_at = created_at
        self.stats = stats or {}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "domain": self.domain,
            "publisher_id": self.publisher_id,
            "publisher_name": self.publisher_name,
            "publisher_type": self.publisher_type,
            "implementation_type": self.implementation_type,
            "source_type": self.source_type,
            "transparency": self.transparency,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.stats:
            d["stats"] = self.stats
        return d


def _zone_from_api(data: dict[str, Any]) -> SiteplugZone:
    """Construct a :class:`SiteplugZone` from a raw SSP API zone dict."""
    return SiteplugZone(
        zone_id=int(data["zone_id"]),
        zone_name=data["zone_name"],
        domain=data.get("domain"),
        publisher_id=int(data["publisher_id"]),
        publisher_name=data["publisher_name"],
        # Pass-through — already a string in the API response
        publisher_type=data.get("publisher_type", ""),
        implementation_type=data.get("implementation_type"),
        source_type=data.get("source_type"),
        # String enum returned by the SSP API (transparency_flag normalised server-side)
        transparency=data.get("transparency", "non_transparent"),
        status=int(data.get("status", 0)),
        created_at=data.get("created_at"),
        stats=data.get("stats"),
    )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SiteplugInventoryManager(BaseInventoryManager):
    """Manages Siteplug inventory zone sync and lookup.

    Implements :class:`~src.adapters.base_inventory.BaseInventoryManager` and
    follows the ``GAMSyncManager`` pattern for ``SyncJob`` tracking.

    Usage (from the adapter)::

        zones = await self.inventory_manager.sync_inventory(
            db_session=db, tenant_id=self.tenant_id
        )
    """

    def __init__(
        self,
        client: SiteplugClient,
        log_func: Callable[[str], None] | None = None,
        tenant_id: str = "",
    ) -> None:
        """Initialise the inventory manager.

        Args:
            client: Authenticated :class:`~src.adapters.siteplug.client.SiteplugClient`.
            log_func: Optional structured logging callable from the adapter.
            tenant_id: Tenant identifier used for ``SyncJob`` records.
        """
        super().__init__(
            client=client,
            identifier=tenant_id,
            dry_run=False,
            log_func=log_func,
        )
        self.tenant_id = tenant_id
        # In-memory zone cache populated by sync_inventory / discover_inventory
        self._zone_cache: dict[int, SiteplugZone] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync_inventory(
        self,
        db_session: Session,
        tenant_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fetch all active zones from the SSP API and upsert into the DB.

        Follows the GAMSyncManager pattern:

        1. Create a ``SyncJob`` record (status=``running``).
        2. Paginate through ``GET /ssp/v1/inventory?status=1`` until exhausted.
        3. Upsert each zone into ``ProductInventoryMapping``
           (``inventory_type="zone"``, ``inventory_id=str(zone_id)``).
        4. Mark zones absent from the API response as inactive by removing
           their ``ProductInventoryMapping`` rows.
        5. Update the ``SyncJob`` to ``completed`` (or ``failed`` on error).

        Args:
            db_session: Active SQLAlchemy session.
            tenant_id: Override the tenant ID set at construction time.
            force: Reserved for future use (currently always syncs).

        Returns:
            Dict with ``sync_id``, ``status``, and ``summary`` keys.
        """
        effective_tenant = tenant_id or self.tenant_id
        sync_job = self._create_sync_job(db_session, effective_tenant)

        try:
            sync_job.status = "running"
            db_session.commit()

            zones = await self._fetch_all_zones()

            # Populate in-memory cache
            self._zone_cache = {z.zone_id: z for z in zones}

            # Upsert into ProductInventoryMapping
            counts = self._upsert_zones(db_session, effective_tenant, zones)

            summary = {
                "tenant_id": effective_tenant,
                "sync_time": datetime.now(UTC).isoformat(),
                "zones_fetched": len(zones),
                "zones_added": counts["added"],
                "zones_updated": counts["updated"],
                "zones_deactivated": counts["deactivated"],
            }

            sync_job.status = "completed"
            sync_job.completed_at = datetime.now(UTC)
            sync_job.summary = json.dumps(summary)
            db_session.commit()

            self.log(
                f"[SiteplugInventoryManager] sync_inventory completed: "
                f"{len(zones)} zones fetched, "
                f"{counts['added']} added, {counts['updated']} updated, "
                f"{counts['deactivated']} deactivated"
            )
            return {"sync_id": sync_job.sync_id, "status": "completed", "summary": summary}

        except Exception as exc:
            logger.error(
                "SiteplugInventoryManager.sync_inventory failed: %s", exc, exc_info=True
            )
            sync_job.status = "failed"
            sync_job.completed_at = datetime.now(UTC)
            sync_job.error_message = str(exc)
            db_session.commit()
            raise

    async def get_zones(
        self,
        product_id: str | None = None,
        db_session: Session | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Return zones, optionally scoped to a product.

        If ``product_id`` and ``db_session`` are provided, queries
        ``ProductInventoryMapping`` for the product's zone IDs and returns
        only those zones from the in-memory cache.

        If the cache is empty, falls back to a live API call (status=1,
        page 1 only) to populate it.

        Args:
            product_id: Optional AdCP product ID to scope the result.
            db_session: Required when ``product_id`` is set.
            **filters: Additional keyword filters (ignored; reserved for
                future use).

        Returns:
            List of zone dicts (same shape as :meth:`SiteplugZone.to_dict`).
        """
        if not self._zone_cache:
            await self._warm_cache()

        if product_id and db_session is not None:
            repo = ProductInventoryMappingRepository(db_session, self.tenant_id)
            rows = repo.list_by_product(product_id, _INVENTORY_TYPE)
            zone_ids = {int(r.inventory_id) for r in rows}
            return [
                z.to_dict()
                for zid, z in self._zone_cache.items()
                if zid in zone_ids
            ]

        return [z.to_dict() for z in self._zone_cache.values()]

    async def get_zone_stats(self, zone_id: int) -> dict[str, Any]:
        """Fetch delivery stats for a single zone via GET /ssp/v1/inventory/{id}.

        The SSP API returns 7-day impressions/clicks/CTR and 30-day average
        daily impressions sourced from AX aggregation tables.

        Args:
            zone_id: Positive integer Siteplug zone ID.

        Returns:
            Stats dict with keys: ``impressions_7d``, ``clicks_7d``,
            ``ctr_7d``, ``avg_daily_impressions``, ``last_updated``.
            Returns zeroed stats if the API call fails (non-blocking).
        """
        _zeroed: dict[str, Any] = {
            "impressions_7d": 0,
            "clicks_7d": 0,
            "ctr_7d": 0.0,
            "avg_daily_impressions": 0,
            "last_updated": None,
        }
        try:
            response = await self.client.get_inventory_zone(zone_id)
            # SSP API wraps the zone in {"data": {...}}
            zone_data: dict[str, Any] = response.get("data", response)
            stats = zone_data.get("stats", {})
            if not stats:
                return _zeroed
            # Update cache entry if present
            if zone_id in self._zone_cache:
                self._zone_cache[zone_id].stats = stats
            return {
                "impressions_7d": int(stats.get("impressions_7d", 0)),
                "clicks_7d": int(stats.get("clicks_7d", 0)),
                "ctr_7d": float(stats.get("ctr_7d", 0.0)),
                "avg_daily_impressions": int(stats.get("avg_daily_impressions", 0)),
                "last_updated": stats.get("last_updated"),
            }
        except Exception as exc:
            logger.warning(
                "SiteplugInventoryManager.get_zone_stats failed for zone %d: %s",
                zone_id,
                exc,
            )
            return _zeroed

    def build_inventory_response(self) -> dict[str, Any]:
        """Build the inventory response dict for ``get_available_inventory``.

        Returns:
            Dict with ``zones`` list and ``properties`` metadata.
        """
        zones = list(self._zone_cache.values())
        return {
            "zones": [z.to_dict() for z in zones],
            "properties": {
                "adapter": _ADAPTER_TYPE,
                "inventory_entity_label": "Zones",
                "total_zones": len(zones),
            },
        }

    # ------------------------------------------------------------------
    # BaseInventoryManager abstract method implementations
    # ------------------------------------------------------------------

    def discover_inventory(self, refresh: bool = False) -> list[SiteplugZone]:
        """Return cached zones (synchronous shim for the base class interface).

        For a full async sync use :meth:`sync_inventory` instead.
        """
        return list(self._zone_cache.values())

    def validate_inventory_ids(
        self, inventory_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Validate that zone IDs exist in the in-memory cache.

        Args:
            inventory_ids: String zone IDs to validate.

        Returns:
            ``(valid_ids, invalid_ids)`` tuple.
        """
        valid: list[str] = []
        invalid: list[str] = []
        for sid in inventory_ids:
            try:
                if int(sid) in self._zone_cache:
                    valid.append(sid)
                else:
                    invalid.append(sid)
            except (ValueError, TypeError):
                invalid.append(sid)
        return valid, invalid

    def suggest_products(self) -> list[dict[str, Any]]:
        """Generate product configuration suggestions from available zones.

        Groups zones by publisher and returns one suggestion per publisher.
        """
        by_publisher: dict[int, list[SiteplugZone]] = {}
        for zone in self._zone_cache.values():
            by_publisher.setdefault(zone.publisher_id, []).append(zone)

        suggestions = []
        for pub_id, zones in by_publisher.items():
            pub_name = zones[0].publisher_name if zones else f"Publisher {pub_id}"
            suggestions.append(
                {
                    "name": f"Siteplug — {pub_name}",
                    "description": (
                        f"Inventory across {len(zones)} zone(s) from {pub_name}"
                    ),
                    "implementation_config": {
                        "targeted_zone_ids": [z.zone_id for z in zones],
                        "publisher_id": pub_id,
                    },
                }
            )
        return suggestions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_all_zones(self) -> list[SiteplugZone]:
        """Paginate through GET /ssp/v1/inventory?status=1 and return all zones.

        Uses ``status=1`` (active only) as required by the task spec.
        Iterates pages until ``has_next`` is False or the page returns no data.
        """
        zones: list[SiteplugZone] = []
        page = 1

        while True:
            self.log(
                f"[SiteplugInventoryManager] fetching inventory page {page} "
                f"(limit={_PAGE_SIZE})"
            )
            response = await self.client.list_inventory(
                page=page,
                limit=_PAGE_SIZE,
                status=1,
            )

            # SSP API envelope: {"data": [...], "pagination": {...}}
            data: list[dict[str, Any]] = response.get("data", [])
            pagination: dict[str, Any] = response.get("pagination", {})

            for item in data:
                zones.append(_zone_from_api(item))

            has_next: bool = bool(pagination.get("has_next", False))
            if not has_next or not data:
                break

            page += 1

        self.log(
            f"[SiteplugInventoryManager] fetched {len(zones)} active zones "
            f"across {page} page(s)"
        )
        return zones

    async def _warm_cache(self) -> None:
        """Populate the in-memory cache with a single-page live API call.

        Used as a lightweight fallback when ``get_zones()`` is called without
        a prior ``sync_inventory()``.  Only fetches page 1 — callers that need
        the full inventory should call ``sync_inventory()`` first.
        """
        try:
            response = await self.client.list_inventory(
                page=1, limit=_PAGE_SIZE, status=1
            )
            data: list[dict[str, Any]] = response.get("data", [])
            for item in data:
                zone = _zone_from_api(item)
                self._zone_cache[zone.zone_id] = zone
        except Exception as exc:
            logger.warning(
                "SiteplugInventoryManager._warm_cache failed: %s", exc
            )

    def _upsert_zones(
        self,
        db_session: Session,
        tenant_id: str,
        zones: list[SiteplugZone],
    ) -> dict[str, int]:
        """Upsert zones into ``ProductInventoryMapping`` and deactivate stale rows.

        The ``ProductInventoryMapping`` table stores one row per
        (tenant_id, product_id, inventory_type, inventory_id) tuple.
        For inventory sync we use ``product_id="__siteplug_zone_catalog__"``
        as a sentinel product that represents the full zone catalogue — this
        mirrors the GAM pattern where inventory is synced independently of
        products and products reference zones by ID.

        Stale rows (zones no longer returned by the API) are deleted.

        Args:
            db_session: Active SQLAlchemy session.
            tenant_id: Tenant identifier.
            zones: Zones returned by the SSP API.

        Returns:
            Dict with ``added``, ``updated``, ``deactivated`` counts.
        """
        _CATALOG_PRODUCT = "__siteplug_zone_catalog__"
        live_ids = {str(z.zone_id) for z in zones}

        # Fetch existing rows for this tenant's zone catalogue
        repo = ProductInventoryMappingRepository(db_session, tenant_id)
        existing_rows = {
            row.inventory_id: row
            for row in repo.list_by_catalog_product(_CATALOG_PRODUCT, _INVENTORY_TYPE)
        }

        added = 0
        updated = 0

        for zone in zones:
            zone_id_str = str(zone.zone_id)
            if zone_id_str in existing_rows:
                # Row already exists — nothing to update on this sparse schema
                updated += 1
            else:
                new_row = ProductInventoryMapping(
                    tenant_id=tenant_id,
                    product_id=_CATALOG_PRODUCT,
                    inventory_type=_INVENTORY_TYPE,
                    inventory_id=zone_id_str,
                    is_primary=False,
                )
                db_session.add(new_row)
                added += 1

        # Deactivate (delete) rows for zones no longer in the API response
        stale_ids = set(existing_rows.keys()) - live_ids
        deactivated = 0
        for stale_id in stale_ids:
            db_session.delete(existing_rows[stale_id])
            deactivated += 1

        db_session.flush()
        return {"added": added, "updated": updated, "deactivated": deactivated}

    def _create_sync_job(self, db_session: Session, tenant_id: str) -> SyncJob:
        """Create and persist a new ``SyncJob`` record (status=``pending``).

        Follows the ``GAMSyncManager._create_sync_job`` pattern.
        """
        sync_id = (
            f"sync_{tenant_id}_inventory_{int(datetime.now(UTC).timestamp())}"
        )
        sync_job = SyncJob(
            sync_id=sync_id,
            tenant_id=tenant_id,
            adapter_type=_ADAPTER_TYPE,
            sync_type="inventory",
            status="pending",
            started_at=datetime.now(UTC),
            triggered_by="api",
            triggered_by_id="siteplug_inventory_sync",
        )
        db_session.add(sync_job)
        db_session.commit()
        logger.info(
            "Created SyncJob %s for tenant %s (siteplug inventory)",
            sync_id,
            tenant_id,
        )
        return sync_job
