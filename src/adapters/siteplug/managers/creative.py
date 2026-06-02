"""Siteplug creative manager stub.

Handles creative upload and association operations against the Siteplug SSP API.
All methods are stubs — real implementation in Task 06.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugCreativeManager:
    """Manages Siteplug creative upload and association.

    Stub implementation — wired to real API in Task 06.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the creative manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def upload_creative(
        self,
        creative_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Upload a creative to Siteplug.

        Stub — wired in Task 06.

        Args:
            creative_data: Creative asset data (title, description, click_url, etc.)

        Returns:
            Created creative data with creative_id
        """
        self._log("[STUB] SiteplugCreativeManager.upload_creative")
        return {"creative_id": 0}

    async def associate_creative(
        self,
        campaign_id: int,
        creative_id: int,
    ) -> dict[str, Any]:
        """Associate a creative with a campaign.

        Stub — wired in Task 06.

        Args:
            campaign_id: Siteplug campaign ID
            creative_id: Siteplug creative ID

        Returns:
            Association result
        """
        self._log(
            f"[STUB] SiteplugCreativeManager.associate_creative: "
            f"campaign_id={campaign_id}, creative_id={creative_id}"
        )
        return {}
