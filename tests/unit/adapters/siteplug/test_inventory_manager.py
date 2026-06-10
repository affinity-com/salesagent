"""Unit tests for SiteplugInventoryManager.

Covers:
- sync_inventory() happy path (AC: zones upserted, SyncJob created/completed)
- Pagination: multiple pages consumed until has_next=False
- transparency_flag stored/returned as string enum (not integer)
- customer_type (publisher_type) raw varchar pass-through
- get_zones() returns zones from in-memory cache
- get_zone_stats() returns stats from get-by-id endpoint
- get_zone_stats() returns zeroed stats on API failure (non-blocking)
- Stale zones deactivated (deleted from ProductInventoryMapping)
- SiteplugZone.to_dict() shape
- BaseInventoryManager interface compliance
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.siteplug.managers.inventory import (
    SiteplugInventoryManager,
    SiteplugZone,
    _zone_from_api,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ZONE_1 = {
    "zone_id": 1042,
    "zone_name": "ExampleSite - Homepage",
    "domain": "example.com",
    "publisher_id": 87,
    "publisher_name": "Example Publisher Inc.",
    "publisher_type": "standard",
    "implementation_type": "SS API",
    "source_type": "SuperSearch",
    "transparency": "transparent",
    "status": 1,
    "created_at": "2024-03-15T10:30:00+05:30",
}

_ZONE_2 = {
    "zone_id": 1043,
    "zone_name": "ExampleSite - Mobile",
    "domain": "m.example.com",
    "publisher_id": 87,
    "publisher_name": "Example Publisher Inc.",
    "publisher_type": "SSS",
    "implementation_type": "SS API",
    "source_type": "SuperSearch",
    "transparency": "non_transparent",
    "status": 1,
    "created_at": "2024-03-15T10:35:00+05:30",
}

_ZONE_3 = {
    "zone_id": 2001,
    "zone_name": "SiteDiscover Zone",
    "domain": None,
    "publisher_id": 99,
    "publisher_name": "SiteDiscover Publisher",
    "publisher_type": "sitediscover",
    "implementation_type": None,
    "source_type": None,
    "transparency": "non_transparent",
    "status": 1,
    "created_at": None,
}


def _single_page_response(zones: list[dict]) -> dict:
    """Build a single-page SSP API envelope."""
    return {
        "status": "success",
        "data": zones,
        "pagination": {
            "page": 1,
            "limit": 200,
            "total": len(zones),
            "total_pages": 1,
            "has_next": False,
            "has_prev": False,
        },
    }


def _make_manager(client: MagicMock, tenant_id: str = "tenant_sp") -> SiteplugInventoryManager:
    return SiteplugInventoryManager(
        client=client,
        log_func=lambda msg: None,
        tenant_id=tenant_id,
    )


def _make_db_session() -> MagicMock:
    """Return a minimal SQLAlchemy session mock."""
    session = MagicMock()
    # scalars().all() returns empty list by default (no pre-existing rows)
    session.scalars.return_value.all.return_value = []
    return session


# ---------------------------------------------------------------------------
# SiteplugZone / _zone_from_api
# ---------------------------------------------------------------------------


class TestSiteplugZone:
    def test_to_dict_shape(self):
        zone = _zone_from_api(_ZONE_1)
        d = zone.to_dict()
        assert d["zone_id"] == 1042
        assert d["zone_name"] == "ExampleSite - Homepage"
        assert d["domain"] == "example.com"
        assert d["publisher_id"] == 87
        assert d["publisher_name"] == "Example Publisher Inc."
        assert d["publisher_type"] == "standard"
        assert d["implementation_type"] == "SS API"
        assert d["source_type"] == "SuperSearch"
        assert d["transparency"] == "transparent"
        assert d["status"] == 1
        assert d["created_at"] == "2024-03-15T10:30:00+05:30"

    def test_transparency_is_string_not_integer(self):
        """transparency must be the string enum, never a raw integer."""
        zone = _zone_from_api(_ZONE_1)
        assert zone.transparency == "transparent"
        assert isinstance(zone.transparency, str)

        zone2 = _zone_from_api(_ZONE_2)
        assert zone2.transparency == "non_transparent"
        assert isinstance(zone2.transparency, str)

    def test_publisher_type_raw_passthrough(self):
        """customer_type / publisher_type must be returned as-is (varchar pass-through)."""
        assert _zone_from_api(_ZONE_1).publisher_type == "standard"
        assert _zone_from_api(_ZONE_2).publisher_type == "SSS"
        assert _zone_from_api(_ZONE_3).publisher_type == "sitediscover"

    def test_nullable_fields(self):
        """domain, implementation_type, source_type, created_at may be None."""
        zone = _zone_from_api(_ZONE_3)
        assert zone.domain is None
        assert zone.implementation_type is None
        assert zone.source_type is None
        assert zone.created_at is None

    def test_stats_absent_by_default(self):
        zone = _zone_from_api(_ZONE_1)
        assert zone.stats == {}

    def test_stats_included_in_to_dict_when_present(self):
        data = dict(_ZONE_1)
        data["stats"] = {"impressions_7d": 1000, "clicks_7d": 50, "ctr_7d": 0.05,
                         "avg_daily_impressions": 142, "last_updated": "2026-05-13T00:00:00+05:30"}
        zone = _zone_from_api(data)
        d = zone.to_dict()
        assert "stats" in d
        assert d["stats"]["impressions_7d"] == 1000

    def test_inherits_inventory_item(self):
        from src.adapters.base_inventory import InventoryItem
        zone = _zone_from_api(_ZONE_1)
        assert isinstance(zone, InventoryItem)
        assert zone.item_id == "1042"
        assert zone.name == "ExampleSite - Homepage"


# ---------------------------------------------------------------------------
# sync_inventory — happy path
# ---------------------------------------------------------------------------


class TestSyncInventoryHappyPath:
    """AC: sync_inventory() fetches all active zones, upserts ProductInventoryMapping,
    creates a SyncJob record with completed status and correct counts."""

    @pytest.mark.asyncio
    async def test_sync_creates_completed_sync_job(self):
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1, _ZONE_2])
        )
        manager = _make_manager(client)
        db = _make_db_session()

        result = await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        assert result["status"] == "completed"
        assert result["sync_id"].startswith("sync_tenant_sp_inventory_")
        summary = result["summary"]
        assert summary["zones_fetched"] == 2
        assert summary["zones_added"] == 2
        assert summary["zones_updated"] == 0
        assert summary["zones_deactivated"] == 0

    @pytest.mark.asyncio
    async def test_sync_calls_list_inventory_with_status_1(self):
        """list endpoint must be called with status=1 (active only, IC-only)."""
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        manager = _make_manager(client)
        db = _make_db_session()

        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        client.list_inventory.assert_called_once_with(
            page=1, limit=200, status=1
        )

    @pytest.mark.asyncio
    async def test_sync_populates_zone_cache(self):
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1, _ZONE_2, _ZONE_3])
        )
        manager = _make_manager(client)
        db = _make_db_session()

        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        assert 1042 in manager._zone_cache
        assert 1043 in manager._zone_cache
        assert 2001 in manager._zone_cache

    @pytest.mark.asyncio
    async def test_sync_upserts_product_inventory_mapping_rows(self):
        """Each zone must be added as a ProductInventoryMapping row.

        db.add() is called once for the SyncJob + once per new zone.
        """
        from src.core.database.models import ProductInventoryMapping, SyncJob

        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1, _ZONE_2])
        )
        manager = _make_manager(client)

        added_objects: list = []
        db = _make_db_session()
        db.add.side_effect = lambda obj: added_objects.append(obj)

        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        mapping_rows = [o for o in added_objects if isinstance(o, ProductInventoryMapping)]
        sync_jobs = [o for o in added_objects if isinstance(o, SyncJob)]

        # One ProductInventoryMapping row per zone
        assert len(mapping_rows) == 2
        # Exactly one SyncJob created
        assert len(sync_jobs) == 1
        # db.commit() called at least twice (sync_job creation + final)
        assert db.commit.call_count >= 2

    @pytest.mark.asyncio
    async def test_sync_marks_sync_job_running_then_completed(self):
        """SyncJob status must transition: pending → running → completed."""
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        manager = _make_manager(client)

        # Capture the SyncJob object passed to db.add()
        added_objects: list = []
        db = _make_db_session()
        db.add.side_effect = lambda obj: added_objects.append(obj)

        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        # First added object is the SyncJob
        from src.core.database.models import SyncJob
        sync_jobs = [o for o in added_objects if isinstance(o, SyncJob)]
        assert len(sync_jobs) == 1
        sync_job = sync_jobs[0]
        assert sync_job.adapter_type == "siteplug"
        assert sync_job.sync_type == "inventory"
        # After sync completes the status is set to "completed"
        assert sync_job.status == "completed"
        assert sync_job.completed_at is not None
        summary = json.loads(sync_job.summary)
        assert summary["zones_fetched"] == 1

    @pytest.mark.asyncio
    async def test_sync_deactivates_stale_zones(self):
        """Zones present in DB but absent from API response must be deleted."""
        from src.core.database.models import ProductInventoryMapping

        client = MagicMock()
        # API returns only zone 1042 — zone 9999 is stale
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        manager = _make_manager(client)
        db = _make_db_session()

        # Simulate an existing stale row for zone 9999
        stale_row = MagicMock(spec=ProductInventoryMapping)
        stale_row.inventory_id = "9999"
        db.scalars.return_value.all.return_value = [stale_row]

        result = await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        # Stale row must be deleted
        db.delete.assert_called_once_with(stale_row)
        assert result["summary"]["zones_deactivated"] == 1

    @pytest.mark.asyncio
    async def test_sync_counts_updated_for_existing_rows(self):
        """Zones already in DB count as 'updated', not 'added'."""
        from src.core.database.models import ProductInventoryMapping

        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        manager = _make_manager(client)
        db = _make_db_session()

        # Simulate zone 1042 already in DB
        existing_row = MagicMock(spec=ProductInventoryMapping)
        existing_row.inventory_id = "1042"
        db.scalars.return_value.all.return_value = [existing_row]

        result = await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        assert result["summary"]["zones_added"] == 0
        assert result["summary"]["zones_updated"] == 1
        assert result["summary"]["zones_deactivated"] == 0

    @pytest.mark.asyncio
    async def test_sync_marks_sync_job_failed_on_api_error(self):
        """If the API call raises, SyncJob must be marked failed and exception re-raised."""
        client = MagicMock()
        client.list_inventory = AsyncMock(side_effect=RuntimeError("API down"))
        manager = _make_manager(client)

        added_objects: list = []
        db = _make_db_session()
        db.add.side_effect = lambda obj: added_objects.append(obj)

        with pytest.raises(RuntimeError, match="API down"):
            await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        from src.core.database.models import SyncJob
        sync_jobs = [o for o in added_objects if isinstance(o, SyncJob)]
        assert sync_jobs[0].status == "failed"
        assert "API down" in sync_jobs[0].error_message


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    @pytest.mark.asyncio
    async def test_sync_iterates_all_pages(self):
        """sync_inventory must consume all pages until has_next=False."""
        page1 = {
            "status": "success",
            "data": [_ZONE_1],
            "pagination": {"page": 1, "limit": 200, "total": 2,
                           "total_pages": 2, "has_next": True, "has_prev": False},
        }
        page2 = {
            "status": "success",
            "data": [_ZONE_2],
            "pagination": {"page": 2, "limit": 200, "total": 2,
                           "total_pages": 2, "has_next": False, "has_prev": True},
        }
        client = MagicMock()
        client.list_inventory = AsyncMock(side_effect=[page1, page2])
        manager = _make_manager(client)
        db = _make_db_session()

        result = await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        assert client.list_inventory.call_count == 2
        assert result["summary"]["zones_fetched"] == 2
        assert 1042 in manager._zone_cache
        assert 1043 in manager._zone_cache

    @pytest.mark.asyncio
    async def test_sync_stops_when_data_empty(self):
        """If a page returns empty data, pagination must stop even if has_next=True."""
        page1 = {
            "status": "success",
            "data": [_ZONE_1],
            "pagination": {"page": 1, "limit": 200, "total": 1,
                           "total_pages": 1, "has_next": True, "has_prev": False},
        }
        page2 = {
            "status": "success",
            "data": [],
            "pagination": {"page": 2, "limit": 200, "total": 1,
                           "total_pages": 1, "has_next": True, "has_prev": True},
        }
        client = MagicMock()
        client.list_inventory = AsyncMock(side_effect=[page1, page2])
        manager = _make_manager(client)
        db = _make_db_session()

        result = await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        assert client.list_inventory.call_count == 2
        assert result["summary"]["zones_fetched"] == 1


# ---------------------------------------------------------------------------
# get_zones
# ---------------------------------------------------------------------------


class TestGetZones:
    @pytest.mark.asyncio
    async def test_get_zones_returns_all_cached_zones(self):
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1, _ZONE_2])
        )
        manager = _make_manager(client)
        db = _make_db_session()
        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        zones = await manager.get_zones()

        assert len(zones) == 2
        zone_ids = {z["zone_id"] for z in zones}
        assert 1042 in zone_ids
        assert 1043 in zone_ids

    @pytest.mark.asyncio
    async def test_get_zones_warms_cache_if_empty(self):
        """get_zones() must call the API if the cache is empty."""
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        manager = _make_manager(client)

        zones = await manager.get_zones()

        client.list_inventory.assert_called_once_with(page=1, limit=200, status=1)
        assert len(zones) == 1

    @pytest.mark.asyncio
    async def test_get_zones_scoped_to_product(self):
        """When product_id is given, only zones mapped to that product are returned."""
        from src.core.database.models import ProductInventoryMapping

        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1, _ZONE_2])
        )
        manager = _make_manager(client)
        db = _make_db_session()
        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        # Simulate DB returning only zone 1042 for product "prod_abc"
        mapping_row = MagicMock(spec=ProductInventoryMapping)
        mapping_row.inventory_id = "1042"
        db.scalars.return_value.all.return_value = [mapping_row]

        zones = await manager.get_zones(product_id="prod_abc", db_session=db)

        assert len(zones) == 1
        assert zones[0]["zone_id"] == 1042


# ---------------------------------------------------------------------------
# get_zone_stats
# ---------------------------------------------------------------------------


class TestGetZoneStats:
    @pytest.mark.asyncio
    async def test_get_zone_stats_returns_stats(self):
        stats = {
            "impressions_7d": 1250000,
            "clicks_7d": 37500,
            "ctr_7d": 0.03,
            "avg_daily_impressions": 185000,
            "last_updated": "2026-05-13T00:00:00+05:30",
        }
        client = MagicMock()
        client.get_inventory_zone = AsyncMock(
            return_value={"data": {**_ZONE_1, "stats": stats}}
        )
        manager = _make_manager(client)

        result = await manager.get_zone_stats(1042)

        assert result["impressions_7d"] == 1250000
        assert result["clicks_7d"] == 37500
        assert result["ctr_7d"] == 0.03
        assert result["avg_daily_impressions"] == 185000
        assert result["last_updated"] == "2026-05-13T00:00:00+05:30"

    @pytest.mark.asyncio
    async def test_get_zone_stats_returns_zeroed_on_api_failure(self):
        """Stats failures are non-blocking — zeroed stats returned, no exception raised."""
        client = MagicMock()
        client.get_inventory_zone = AsyncMock(side_effect=RuntimeError("AX down"))
        manager = _make_manager(client)

        result = await manager.get_zone_stats(1042)

        assert result["impressions_7d"] == 0
        assert result["clicks_7d"] == 0
        assert result["ctr_7d"] == 0.0
        assert result["avg_daily_impressions"] == 0
        assert result["last_updated"] is None

    @pytest.mark.asyncio
    async def test_get_zone_stats_no_stats_key_returns_zeroed(self):
        """If the API response has no 'stats' key, return zeroed stats."""
        client = MagicMock()
        client.get_inventory_zone = AsyncMock(
            return_value={"data": _ZONE_1}  # no 'stats' key
        )
        manager = _make_manager(client)

        result = await manager.get_zone_stats(1042)

        assert result["impressions_7d"] == 0
        assert result["last_updated"] is None

    @pytest.mark.asyncio
    async def test_get_zone_stats_updates_cache(self):
        """Stats fetched via get_zone_stats must be stored in the zone cache."""
        stats = {"impressions_7d": 500, "clicks_7d": 10, "ctr_7d": 0.02,
                 "avg_daily_impressions": 71, "last_updated": None}
        client = MagicMock()
        client.list_inventory = AsyncMock(
            return_value=_single_page_response([_ZONE_1])
        )
        client.get_inventory_zone = AsyncMock(
            return_value={"data": {**_ZONE_1, "stats": stats}}
        )
        manager = _make_manager(client)
        db = _make_db_session()
        await manager.sync_inventory(db_session=db, tenant_id="tenant_sp")

        await manager.get_zone_stats(1042)

        assert manager._zone_cache[1042].stats["impressions_7d"] == 500


# ---------------------------------------------------------------------------
# BaseInventoryManager interface compliance
# ---------------------------------------------------------------------------


class TestBaseInventoryManagerInterface:
    def test_is_subclass_of_base_inventory_manager(self):
        from src.adapters.base_inventory import BaseInventoryManager
        assert issubclass(SiteplugInventoryManager, BaseInventoryManager)

    def test_discover_inventory_returns_cached_zones(self):
        client = MagicMock()
        manager = _make_manager(client)
        # Manually populate cache
        zone = _zone_from_api(_ZONE_1)
        manager._zone_cache[zone.zone_id] = zone

        result = manager.discover_inventory()

        assert len(result) == 1
        assert result[0].zone_id == 1042

    def test_validate_inventory_ids_valid(self):
        client = MagicMock()
        manager = _make_manager(client)
        zone = _zone_from_api(_ZONE_1)
        manager._zone_cache[zone.zone_id] = zone

        valid, invalid = manager.validate_inventory_ids(["1042", "9999"])

        assert "1042" in valid
        assert "9999" in invalid

    def test_validate_inventory_ids_non_integer(self):
        client = MagicMock()
        manager = _make_manager(client)

        valid, invalid = manager.validate_inventory_ids(["not_an_int"])

        assert "not_an_int" in invalid
        assert valid == []

    def test_suggest_products_groups_by_publisher(self):
        client = MagicMock()
        manager = _make_manager(client)
        for raw in [_ZONE_1, _ZONE_2]:  # both publisher_id=87
            z = _zone_from_api(raw)
            manager._zone_cache[z.zone_id] = z
        z3 = _zone_from_api(_ZONE_3)  # publisher_id=99
        manager._zone_cache[z3.zone_id] = z3

        suggestions = manager.suggest_products()

        assert len(suggestions) == 2  # two publishers
        pub_ids = {s["implementation_config"]["publisher_id"] for s in suggestions}
        assert 87 in pub_ids
        assert 99 in pub_ids

    def test_build_inventory_response_shape(self):
        client = MagicMock()
        manager = _make_manager(client)
        zone = _zone_from_api(_ZONE_1)
        manager._zone_cache[zone.zone_id] = zone

        response = manager.build_inventory_response()

        assert "zones" in response
        assert "properties" in response
        assert response["properties"]["adapter"] == "siteplug"
        assert response["properties"]["total_zones"] == 1
        assert len(response["zones"]) == 1
