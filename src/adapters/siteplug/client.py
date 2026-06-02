"""Siteplug SSP Tech API HTTP client.

Handles authentication and HTTP requests to the Siteplug SSP API.
Auth: X-API-Key header.

All methods are stubs returning mock data. Real API calls are wired in Task 02+.
"""

import logging
from typing import Any

import httpx

from src.adapters.siteplug.config_schema import SiteplugConnectionConfig

logger = logging.getLogger(__name__)


class SiteplugAPIError(Exception):
    """Exception raised for Siteplug SSP API errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class SiteplugClient:
    """Client for interacting with the Siteplug SSP Tech API.

    All methods are stubs returning safe mock data.
    Real HTTP calls are wired in Task 02 (platform/brand/campaign CRUD),
    Task 03 (inventory), Task 04 (ad groups), Task 05 (delivery),
    and Task 06 (creatives).

    Attributes:
        config: Validated connection configuration
        base_url: API base URL (stripped of trailing slash)
        _headers: Default request headers including X-API-Key
    """

    def __init__(self, config: SiteplugConnectionConfig):
        """Initialize the Siteplug client.

        Args:
            config: Validated SiteplugConnectionConfig
        """
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._headers = {
            "X-API-Key": config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated HTTP request with retry logic.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            path: API endpoint path (e.g. "/platforms")
            **kwargs: Additional arguments passed to httpx.AsyncClient.request

        Returns:
            Parsed JSON response body

        Raises:
            SiteplugAPIError: If the request fails or returns an error status
        """
        url = f"{self.base_url}{path}"
        headers = {**self._headers, **kwargs.pop("headers", {})}

        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        **kwargs,
                    )
                return self._handle_response(response)
            except SiteplugAPIError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    logger.warning(
                        f"Siteplug request attempt {attempt + 1} failed: {exc}. Retrying..."
                    )
                continue

        raise SiteplugAPIError(
            f"Request to {url} failed after {self.config.max_retries + 1} attempts: {last_exc}"
        )

    def _handle_response(self, response: httpx.Response) -> Any:
        """Check status codes and raise SiteplugAPIError on failures.

        Args:
            response: httpx Response object

        Returns:
            Parsed JSON response body (or None for empty responses)

        Raises:
            SiteplugAPIError: On 4xx/5xx responses
        """
        try:
            body = response.json() if response.content else None
        except Exception:
            body = response.text

        if response.status_code == 401:
            raise SiteplugAPIError(
                "Siteplug API authentication failed (HTTP 401)",
                status_code=401,
                error_code="UNAUTHORIZED",
            )

        if response.status_code == 403:
            raise SiteplugAPIError(
                "Siteplug API access denied (HTTP 403)",
                status_code=403,
                error_code="FORBIDDEN",
            )

        if response.status_code == 404:
            raise SiteplugAPIError(
                "Resource not found (HTTP 404)",
                status_code=404,
                error_code="NOT_FOUND",
            )

        if response.status_code == 429:
            raise SiteplugAPIError(
                "Siteplug API rate limit exceeded (HTTP 429)",
                status_code=429,
                error_code="RATE_LIMITED",
            )

        if response.status_code >= 500:
            raise SiteplugAPIError(
                f"Siteplug API server error (HTTP {response.status_code})",
                status_code=response.status_code,
                error_code="SERVER_ERROR",
            )

        if response.status_code >= 400:
            error_code = None
            if isinstance(body, dict):
                error_code = body.get("error_code") or body.get("code")
            raise SiteplugAPIError(
                f"Siteplug API error (HTTP {response.status_code}): {body}",
                status_code=response.status_code,
                error_code=error_code,
            )

        return body

    # =========================================================================
    # Health / Platform Operations  (wired in Task 02)
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Check SSP API health. Stub — wired in Task 02."""
        return {"status": "ok"}

    async def create_platform(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a platform. Stub — wired in Task 02."""
        return {"platform_id": 0}

    async def list_platforms(self, **filters: Any) -> list[dict[str, Any]]:
        """List platforms. Stub — wired in Task 02."""
        return []

    async def create_agency(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create an agency (master account). Stub — wired in Task 02."""
        return {"masteraccount_id": 0}

    async def create_brand(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a brand. Stub — wired in Task 02."""
        return {"brand_id": 0}

    async def create_advertiser(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create an advertiser. Stub — wired in Task 02."""
        return {"advertiser_id": 0}

    # =========================================================================
    # Campaign Operations  (wired in Task 02)
    # =========================================================================

    async def create_campaign(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a campaign. Stub — wired in Task 02."""
        return {"campaign_id": 0}

    async def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        """Get a campaign by ID. Stub — wired in Task 02."""
        return {}

    async def update_campaign(self, campaign_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Update a campaign. Stub — wired in Task 02."""
        return {}

    async def list_campaigns(self, **filters: Any) -> list[dict[str, Any]]:
        """List campaigns. Stub — wired in Task 02."""
        return []

    async def onboard(self, data: dict[str, Any]) -> dict[str, Any]:
        """Onboard a new advertiser/campaign. Stub — wired in Task 02."""
        return {"results": []}

    # =========================================================================
    # Inventory Operations  (wired in Task 03)
    # =========================================================================

    async def list_inventory(self, **filters: Any) -> list[dict[str, Any]]:
        """List available inventory zones. Stub — wired in Task 03."""
        return []

    async def get_inventory_zone(self, zone_id: int) -> dict[str, Any]:
        """Get a specific inventory zone. Stub — wired in Task 03."""
        return {}

    # =========================================================================
    # Delivery / Reporting Operations  (wired in Task 05)
    # =========================================================================

    async def get_campaign_delivery(
        self, campaign_id: int, **params: Any
    ) -> dict[str, Any]:
        """Get campaign delivery stats. Stub — wired in Task 05."""
        return {}

    async def get_campaign_snapshot(self, campaign_id: int) -> dict[str, Any]:
        """Get campaign snapshot. Stub — wired in Task 05."""
        return {}

    # =========================================================================
    # Creative Operations  (wired in Task 06)
    # =========================================================================

    async def create_creative(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a creative. Stub — wired in Task 06."""
        return {"creative_id": 0}

    async def associate_creatives(
        self, campaign_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Associate creatives with a campaign. Stub — wired in Task 06."""
        return {}

    # =========================================================================
    # Ad Group / Keyword Operations  (wired in Task 04)
    # =========================================================================

    async def create_adgroup(
        self, campaign_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create an ad group. Stub — wired in Task 04."""
        return {"ad_group_id": 0}

    async def list_adgroups(self, campaign_id: int) -> list[dict[str, Any]]:
        """List ad groups for a campaign. Stub — wired in Task 04."""
        return []

    async def add_keywords(
        self, adgroup_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Add keywords to an ad group. Stub — wired in Task 04."""
        return {}

    async def remove_keywords(
        self, adgroup_id: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove keywords from an ad group. Stub — wired in Task 04."""
        return {}
