"""Siteplug targeting manager stub.

Handles keyword targeting and validation for Siteplug campaigns.
All methods are stubs — real implementation in Task 07.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugTargetingManager:
    """Manages Siteplug keyword targeting configuration.

    Stub implementation — replaced with real implementation in Task 07.
    """

    def __init__(self, client: Any, log_func: Any = None) -> None:
        """Initialize the targeting manager.

        Args:
            client: SiteplugClient instance
            log_func: Optional logging function from the adapter
        """
        self.client = client
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def build_targeting(
        self,
        keywords: list[str],
        match_types: list[str] | None = None,
        geo_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build a targeting configuration for a Siteplug campaign.

        Stub — replaced in Task 07.

        Args:
            keywords: List of keyword strings
            match_types: Optional list of match types (broad, phrase, exact)
            geo_targets: Optional list of geo target codes

        Returns:
            Targeting configuration dict suitable for campaign creation
        """
        self._log(
            f"[STUB] SiteplugTargetingManager.build_targeting: "
            f"{len(keywords)} keywords, match_types={match_types}"
        )
        return {
            "keywords": keywords,
            "match_types": match_types or ["broad"],
            "geo_targets": geo_targets or [],
        }

    async def validate_targeting(
        self,
        targeting: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        """Validate a targeting configuration.

        Stub — replaced in Task 07.

        Args:
            targeting: Targeting configuration dict

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        self._log("[STUB] SiteplugTargetingManager.validate_targeting")
        return True, []
