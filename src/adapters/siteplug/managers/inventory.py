"""Siteplug inventory manager stub.

Handles inventory zone sync and lookup against the Siteplug SSP API.
All methods are stubs — real implementation in Task 03.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugInventoryManager:
    """Manages Siteplug inventory zone sync and lookup.

    Stub implementation — wired to real API in Task 03.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the inventory manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def sync_inventory(self) -> list[dict[str, Any]]:
        """Sync available inventory zones from Siteplug.

        Stub — wired in Task 03.

        Returns:
            List of inventory zone records
        """
        self._log("[STUB] SiteplugInventoryManager.sync_inventory")
        return []

    async def get_zones(self, **filters: Any) -> list[dict[str, Any]]:
        """Get available inventory zones, optionally filtered.

        Stub — wired in Task 03.

        Args:
            **filters: Optional filter parameters (e.g. platform_id, category)

        Returns:
            List of zone records
        """
        self._log("[STUB] SiteplugInventoryManager.get_zones")
        return []

    async def get_zone_stats(self, zone_id: int) -> dict[str, Any]:
        """Get statistics for a specific inventory zone.

        Stub — wired in Task 03.

        Args:
            zone_id: Siteplug zone ID

        Returns:
            Zone statistics (impressions, fill rate, etc.)
        """
        self._log(f"[STUB] SiteplugInventoryManager.get_zone_stats: zone_id={zone_id}")
        return {}
