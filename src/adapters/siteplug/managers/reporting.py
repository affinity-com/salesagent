"""Siteplug reporting manager stub.

Handles delivery reporting and campaign snapshot operations.
All methods are stubs — real implementation in Task 05.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugReportingManager:
    """Manages Siteplug delivery reporting and snapshots.

    Stub implementation — wired to real API in Task 05.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the reporting manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def get_delivery(
        self,
        campaign_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Get delivery stats for a campaign.

        Stub — wired in Task 05.

        Args:
            campaign_id: Siteplug campaign ID
            start_date: Optional start date (ISO 8601)
            end_date: Optional end date (ISO 8601)

        Returns:
            Delivery stats (impressions, clicks, spend, etc.)
        """
        self._log(f"[STUB] SiteplugReportingManager.get_delivery: campaign_id={campaign_id}")
        return {}

    async def get_snapshot(self, campaign_id: int) -> dict[str, Any]:
        """Get a point-in-time snapshot of campaign performance.

        Stub — wired in Task 05.

        Args:
            campaign_id: Siteplug campaign ID

        Returns:
            Campaign snapshot data
        """
        self._log(f"[STUB] SiteplugReportingManager.get_snapshot: campaign_id={campaign_id}")
        return {}
