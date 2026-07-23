"""Siteplug SSP Tech API HTTP client.

Handles authentication and HTTP requests to the Siteplug SSP API.
Auth: X-API-Key header.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from src.adapters.siteplug.config_schema import SiteplugConnectionConfig

logger = logging.getLogger(__name__)

# HTTP status → SiteplugAPIError.error_code mapping
_HTTP_ERROR_CODES: dict[int, str] = {
    400: "VALIDATION_ERROR",
    401: "API_KEY_INVALID",
    404: "ENTITY_NOT_FOUND",
    409: "ENTITY_ALREADY_EXISTS",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
}


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
        *,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated HTTP request with retry logic.

        On HTTP 429, reads X-RateLimit-Reset (Unix timestamp) and sleeps
        until that time (capped at 60 s) before retrying.

        On POST/PUT, forwards ``idempotency_key`` as the ``Idempotency-Key``
        header when provided.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            path: API endpoint path (e.g. "/platforms")
            idempotency_key: Optional idempotency key forwarded on POST/PUT.
            **kwargs: Additional arguments passed to httpx.AsyncClient.request

        Returns:
            Parsed JSON response body (or full body for 207)

        Raises:
            SiteplugAPIError: If the request fails or returns an error status
        """
        url = f"{self.base_url}{path}"
        headers = {**self._headers, **kwargs.pop("headers", {})}

        # Forward idempotency key on mutating requests
        if idempotency_key and method.upper() in ("POST", "PUT"):
            headers["Idempotency-Key"] = idempotency_key

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

                # Handle 429 with rate-limit sleep before retry
                if response.status_code == 429:
                    reset_header = response.headers.get("X-RateLimit-Reset")
                    if reset_header:
                        try:
                            reset_ts = float(reset_header)
                            sleep_secs = min(reset_ts - time.time(), 60.0)
                            if sleep_secs > 0:
                                logger.warning(
                                    f"Siteplug rate limited. Sleeping {sleep_secs:.1f}s "
                                    f"(X-RateLimit-Reset={reset_header})"
                                )
                                await asyncio.sleep(sleep_secs)
                        except (ValueError, TypeError):
                            await asyncio.sleep(1)
                    else:
                        await asyncio.sleep(1)
                    # Let _handle_response raise so the outer except re-raises
                    # only if we've exhausted retries; otherwise continue loop
                    if attempt < self.config.max_retries:
                        continue

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
        """Parse the SSP API response envelope and raise on errors.

        - HTTP 207: return the full body (flat envelope with platform/agency/
          summary/results keys — NOT body["data"]).
        - HTTP 2xx: extract and return body["data"].
        - HTTP 4xx/5xx: raise SiteplugAPIError with the mapped error_code.
          For 500 onboarding errors, prefer body["error"]["code"] over the
          HTTP-mapped code.

        Args:
            response: httpx Response object

        Returns:
            Parsed response data

        Raises:
            SiteplugAPIError: On 4xx/5xx responses
        """
        try:
            body = response.json() if response.content else None
        except Exception:
            body = response.text

        status = response.status_code

        # ── Error responses ────────────────────────────────────────────────
        if status in _HTTP_ERROR_CODES or status >= 400:
            mapped_code = _HTTP_ERROR_CODES.get(status, "INTERNAL_ERROR")

            # For 500 errors, prefer the API's own error code.
            # The staging API returns the code in body["code"] (flat envelope),
            # e.g. {"status":"error","code":"SP_ERROR","message":"..."}.
            # Some endpoints nest it under body["error"]["code"].
            if status == 500 and isinstance(body, dict):
                api_code = (
                    body.get("error", {}).get("code")
                    if isinstance(body.get("error"), dict)
                    else None
                ) or body.get("code")
                error_code = api_code or mapped_code
            else:
                # For other errors, try to extract a more specific code from body
                error_code = mapped_code
                if isinstance(body, dict):
                    api_code = (
                        body.get("error", {}).get("code")
                        if isinstance(body.get("error"), dict)
                        else body.get("error_code") or body.get("code")
                    )
                    if api_code and api_code not in ("SUCCESS", "success"):
                        error_code = api_code

            message = f"Siteplug API error (HTTP {status})"
            if isinstance(body, dict):
                msg = (
                    body.get("message")
                    or (body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else None)
                    or str(body)
                )
                message = f"{message}: {msg}"
            elif body:
                message = f"{message}: {body}"

            raise SiteplugAPIError(message, status_code=status, error_code=error_code)

        # ── 207 Multi-Status (onboarding) — return full body ──────────────
        if status == 207:
            return body

        # ── 2xx success — extract body["data"] ────────────────────────────
        if isinstance(body, dict) and "data" in body:
            return body["data"]

        return body

    # =========================================================================
    # Health
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Check SSP API health. Public route — no auth header sent."""
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.get(
                f"{self.base_url}/health",
                headers={"Accept": "application/json"},
            )
        return self._handle_response(response)

    # =========================================================================
    # Platform Operations
    # =========================================================================

    async def create_platform(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create a platform. POST /platforms."""
        return await self._request("POST", "/platforms", json=data, idempotency_key=idempotency_key)

    async def list_platforms(self, **filters: Any) -> list[dict[str, Any]]:
        """List platforms. GET /platforms."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._request("GET", "/platforms", params=params)

    async def get_platform(self, platform_id: int) -> dict[str, Any]:
        """Get a platform by ID. GET /platforms/{id}."""
        return await self._request("GET", f"/platforms/{platform_id}")

    # =========================================================================
    # Agency Operations
    # =========================================================================

    async def create_agency(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create an agency (master account). POST /agencies."""
        return await self._request("POST", "/agencies", json=data, idempotency_key=idempotency_key)

    async def list_agencies(self, **filters: Any) -> list[dict[str, Any]]:
        """List agencies. GET /agencies."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._request("GET", "/agencies", params=params)

    async def get_agency(self, agency_id: int) -> dict[str, Any]:
        """Get an agency by ID. GET /agencies/{id}."""
        return await self._request("GET", f"/agencies/{agency_id}")

    # =========================================================================
    # Brand Operations
    # =========================================================================

    async def create_brand(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create a brand. POST /brands."""
        return await self._request("POST", "/brands", json=data, idempotency_key=idempotency_key)

    async def list_brands(self, **filters: Any) -> list[dict[str, Any]]:
        """List brands. GET /brands."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._request("GET", "/brands", params=params)

    async def get_brand(self, brand_id: int) -> dict[str, Any]:
        """Get a brand by ID. GET /brands/{id}."""
        return await self._request("GET", f"/brands/{brand_id}")

    async def update_brand_king_domains(
        self, brand_id: int, domains: list[str]
    ) -> dict[str, Any]:
        """Whitelist king domains for a brand. PUT /brands/{id}.

        Required for SiteDiscover (SDC) campaigns so the SD traffic matching
        engine can associate publisher traffic with the correct brand.

        Args:
            brand_id: Siteplug brand_id.
            domains: List of domain strings (e.g. ["nike.com", "nike.fr"]).

        Returns:
            Updated brand data.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        payload = {
            "king_domain_slogans": [
                {"domain": domain, "slogan": ""} for domain in domains
            ]
        }
        return await self._request("PUT", f"/brands/{brand_id}", json=payload)

    # =========================================================================
    # Advertiser Operations
    # =========================================================================

    async def create_advertiser(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create an advertiser. POST /advertisers."""
        return await self._request("POST", "/advertisers", json=data, idempotency_key=idempotency_key)

    async def list_advertisers(self, **filters: Any) -> list[dict[str, Any]]:
        """List advertisers. GET /advertisers."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._request("GET", "/advertisers", params=params)

    async def get_advertiser(self, advertiser_id: int) -> dict[str, Any]:
        """Get an advertiser by ID. GET /advertisers/{id}."""
        return await self._request("GET", f"/advertisers/{advertiser_id}")

    # =========================================================================
    # Campaign Operations
    # =========================================================================

    async def create_campaign(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create a campaign. POST /campaigns."""
        return await self._request("POST", "/campaigns", json=data, idempotency_key=idempotency_key)

    async def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        """Get a campaign by ID. GET /campaigns/{id}."""
        return await self._request("GET", f"/campaigns/{campaign_id}")

    async def update_campaign(
        self, campaign_id: int, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Update a campaign. PUT /campaigns/{id}."""
        return await self._request(
            "PUT", f"/campaigns/{campaign_id}", json=data, idempotency_key=idempotency_key
        )

    async def list_campaigns(self, **filters: Any) -> list[dict[str, Any]]:
        """List campaigns. GET /campaigns with optional filter params."""
        params = {k: v for k, v in filters.items() if v is not None}
        return await self._request("GET", "/campaigns", params=params)

    async def onboard(
        self, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """POST /onboard — Phase 7 onboarding orchestration.

        Returns the full 207 response body on success (flat envelope with
        top-level ``platform``, ``agency``, ``summary``, ``results`` keys).

        Raises:
            SiteplugAPIError: On 400/401/500 responses.
        """
        return await self._request("POST", "/onboard", json=data, idempotency_key=idempotency_key)

    # =========================================================================
    # Inventory Operations  (Task 03)
    # =========================================================================

    async def list_inventory(
        self,
        page: int = 1,
        limit: int = 200,
        status: int | None = None,
        publisher_id: int | None = None,
        implementation_type: str | None = None,
        source_type: str | None = None,
        transparency: str | None = None,
        search: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        """List available inventory zones from GET /inventory.

        Queries IC only (no AX cross-join). Returns the full paginated
        response envelope so the caller can iterate pages.

        Args:
            page: Page number (1-based).
            limit: Records per page (max 200 per SSP API contract).
            status: Filter by zone status — 0=inactive, 1=active. Omit for all.
            publisher_id: Filter by publisher/customer ID.
            implementation_type: Exact match on implementation type name.
            source_type: Exact match on source type name.
            transparency: ``transparent`` or ``non_transparent``.
            search: LIKE search on zone name and domain.

        Returns:
            Parsed response body containing ``data`` (list of zone dicts) and
            ``pagination`` metadata.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if status is not None:
            params["status"] = status
        if publisher_id is not None:
            params["publisher_id"] = publisher_id
        if implementation_type is not None:
            params["implementation_type"] = implementation_type
        if source_type is not None:
            params["source_type"] = source_type
        if transparency is not None:
            params["transparency"] = transparency
        if search is not None:
            params["search"] = search

        return await self._request("GET", "/inventory", params=params)

    async def get_inventory_zone(self, zone_id: int) -> dict[str, Any]:
        """Get a single inventory zone with delivery stats from GET /inventory/{id}.

        Queries IC for zone metadata and AX for 7-day / 30-day delivery stats.

        Args:
            zone_id: Positive integer zone ID (IC ``site_id``).

        Returns:
            Parsed response body containing the zone object with a nested
            ``stats`` dict (impressions_7d, clicks_7d, ctr_7d,
            avg_daily_impressions, last_updated).

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses (including 404 when
                the zone does not exist in IC).
        """
        return await self._request("GET", f"/inventory/{zone_id}")

    # =========================================================================
    # Delivery / Reporting Operations  (Task 05)
    # =========================================================================

    async def get_campaign_delivery(
        self,
        campaign_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        dimensions: list[str] | None = None,
        geo_level: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any]:
        """Get campaign delivery stats.

        Calls ``GET /campaigns/{id}/delivery`` when the SSP API delivery
        endpoint is deployed.  Until then, returns a mock response that
        exercises the full mapping path in ``SiteplugReportingManager``.

        To wire the real endpoint, replace the ``return _MOCK_DELIVERY_RESPONSE``
        line below with:

            params: dict[str, Any] = {}
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            if dimensions:
                params["dimensions"] = ",".join(dimensions)
            if geo_level:
                params["geo_level"] = geo_level
            return await self._request(
                "GET", f"/campaigns/{campaign_id}/delivery", params=params
            )

        Args:
            campaign_id: Siteplug campaign ID.
            start_date: Optional start date filter (YYYY-MM-DD).
            end_date: Optional end date filter (YYYY-MM-DD).
            dimensions: Optional list of dimension keys (e.g. ["geo", "device_type"]).
            geo_level: Required when "geo" dimension requested (country/region/metro/postal_area).
        """
        # STUB: delivery API not yet deployed — return empty response so the
        # mapping layer produces all-zero AdCP metrics.  Replace with the real
        # _request call above when GET /campaigns/{id}/delivery is live.
        logger.debug(
            "[siteplug] get_campaign_delivery: delivery API not yet deployed, "
            "returning empty stub for campaign_id=%s", campaign_id
        )
        return {}

    async def get_campaign_snapshot(self, campaign_id: int) -> dict[str, Any]:
        """Get a point-in-time campaign snapshot.

        Calls ``GET /campaigns/{id}/delivery/snapshot`` when the SSP API
        delivery endpoint is deployed.  Until then, returns an empty dict.

        To wire the real endpoint, replace the ``return {}`` line below with:

            return await self._request(
                "GET", f"/campaigns/{campaign_id}/delivery/snapshot"
            )

        Args:
            campaign_id: Siteplug campaign ID.
        """
        # STUB: delivery API not yet deployed — return empty response so the
        # mapping layer produces an all-zero AdCP snapshot.  Replace with the
        # real _request call above when GET /campaigns/{id}/delivery/snapshot
        # is live.
        logger.debug(
            "[siteplug] get_campaign_snapshot: delivery API not yet deployed, "
            "returning empty stub for campaign_id=%s", campaign_id
        )
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
    # Ad Group / Keyword Operations  (Task 04)
    # =========================================================================

    async def create_adgroup(
        self,
        campaign_id: int,
        data: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create an ad group under a campaign.

        POST /ssp/v1/campaigns/{id}/adgroups

        Required fields in ``data``:
            bid_amount (float): CPC/CPM bid value (maps to ``max_cpc``).
            bid_type (str): ``"cpc"`` or ``"cpm"``.

        Optional fields in ``data``:
            name (str): Ad group name. Auto-generated by SSP if omitted.
                Must match ``/^[a-zA-Z0-9][a-zA-Z0-9 \\-\\_]*$/``.
            status (int): 0=paused, 1=active (default 1).

        Args:
            campaign_id: Siteplug campaign ID.
            data: Request body dict.
            idempotency_key: Optional idempotency key forwarded on POST.

        Returns:
            Created ad group data including ``ad_group_id``.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        return await self._request(
            "POST",
            f"/campaigns/{campaign_id}/adgroups",
            json=data,
            idempotency_key=idempotency_key,
        )

    async def list_adgroups(
        self,
        campaign_id: int,
        *,
        status: str | None = None,
        page: int = 1,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """List ad groups for a campaign.

        GET /ssp/v1/campaigns/{id}/adgroups

        Args:
            campaign_id: Siteplug campaign ID.
            status: Optional filter — ``"active"`` or ``"paused"``.
            page: Page number (1-based).
            limit: Records per page (max 200).

        Returns:
            List of ad group dicts.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if status is not None:
            params["status"] = status
        result = await self._request(
            "GET", f"/campaigns/{campaign_id}/adgroups", params=params
        )
        # The endpoint returns a paginated envelope; extract the items list.
        if isinstance(result, dict):
            return result.get("items", result.get("data", []))
        if isinstance(result, list):
            return result
        return []

    async def add_keywords(
        self,
        adgroup_id: int,
        data: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Add positive and/or negative keywords to an ad group.

        POST /ssp/v1/adgroups/{id}/keywords

        ``data`` must contain at least one of:
            keywords (list): Positive keyword dicts with keys:
                text (str): Keyword text.
                match_type (str): ``"broad"``, ``"phrase"``, or ``"exact"``.
                kw_max_cpc (float, optional): Per-keyword max CPC override.
            negative_keywords (list): Negative keyword dicts with keys:
                text (str): Keyword text.
                match_type (str): ``"broad"`` or ``"exact"`` (no phrase).

        Args:
            adgroup_id: Siteplug ad group ID.
            data: Request body dict.
            idempotency_key: Optional idempotency key forwarded on POST.

        Returns:
            Response data with ``ad_group_id``, ``keywords``, ``total``.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        return await self._request(
            "POST",
            f"/adgroups/{adgroup_id}/keywords",
            json=data,
            idempotency_key=idempotency_key,
        )

    async def update_adgroup(
        self, adgroup_id: int, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Update an ad group. PUT /adgroups/{id}."""
        return await self._request(
            "PUT", f"/adgroups/{adgroup_id}", json=data, idempotency_key=idempotency_key
        )

    async def update_adgroup_status(
        self, adgroup_id: int, status: int, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Update ad group status. PUT /adgroups/{id}/status.

        Args:
            adgroup_id: Siteplug ad group ID.
            status: 0 = paused, 1 = active.
            idempotency_key: Optional idempotency key.

        Returns:
            Updated ad group data.
        """
        return await self._request(
            "PUT",
            f"/adgroups/{adgroup_id}/status",
            json={"status": status},
            idempotency_key=idempotency_key,
        )

    async def remove_keywords(
        self,
        adgroup_id: int,
        data: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Remove positive and/or negative keywords from an ad group.

        DELETE /ssp/v1/adgroups/{id}/keywords

        ``data`` must contain at least one of:
            keywords (list): Positive keyword dicts with keys:
                text (str): Keyword text.
                match_type (str): ``"broad"``, ``"phrase"``, or ``"exact"``.
            negative_keywords (list): Negative keyword dicts with keys:
                text (str): Keyword text.
                match_type (str): ``"broad"`` or ``"exact"``.

        Keywords are matched by ``(text, match_type)`` tuple — no ``keyword_id``
        required. Non-matching entries are treated as no-ops by the SSP API.

        Args:
            adgroup_id: Siteplug ad group ID.
            data: Request body dict.
            idempotency_key: Unused (DELETE does not support idempotency keys),
                kept for API symmetry.

        Returns:
            Response data with ``ad_group_id``, ``keywords``, ``total``.

        Raises:
            SiteplugAPIError: On HTTP 4xx/5xx responses.
        """
        return await self._request(
            "DELETE",
            f"/adgroups/{adgroup_id}/keywords",
            json=data,
        )
