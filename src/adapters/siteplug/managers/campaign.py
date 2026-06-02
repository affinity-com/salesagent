"""Siteplug campaign manager stub.

Handles campaign lifecycle operations against the Siteplug SSP API.
All methods are stubs — real implementation in Task 02.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugCampaignManager:
    """Manages Siteplug campaign CRUD operations.

    Stub implementation — wired to real API in Task 02.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the campaign manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def create_campaign(
        self,
        name: str,
        platform_id: int,
        brand_id: int,
        campaign_type: int = 1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a campaign in Siteplug.

        Stub — wired in Task 02.

        Args:
            name: Campaign name
            platform_id: Siteplug platform ID
            brand_id: Siteplug brand ID
            campaign_type: Campaign type (1=KW, 2=RON, 3=CAT, 4=HYBRID, 5=PLA)
            **kwargs: Additional campaign parameters

        Returns:
            Created campaign data with campaign_id
        """
        self._log(f"[STUB] SiteplugCampaignManager.create_campaign: name={name}")
        return {"campaign_id": 0}

    async def update_campaign(
        self,
        campaign_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a campaign in Siteplug.

        Stub — wired in Task 02.

        Args:
            campaign_id: Siteplug campaign ID
            data: Fields to update

        Returns:
            Updated campaign data
        """
        self._log(f"[STUB] SiteplugCampaignManager.update_campaign: campaign_id={campaign_id}")
        return {}

    async def get_campaign_status(self, campaign_id: int) -> str:
        """Get the status of a campaign.

        Stub — wired in Task 02.

        Args:
            campaign_id: Siteplug campaign ID

        Returns:
            Campaign status string (e.g. "active", "paused", "pending")
        """
        self._log(f"[STUB] SiteplugCampaignManager.get_campaign_status: campaign_id={campaign_id}")
        return "pending_activation"
