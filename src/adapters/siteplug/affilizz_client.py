"""Affilizz Internal Text Ads API HTTP client.

Handles authentication and HTTP requests to the Affilizz Internal Text Ads API.
Auth: ApiKey header (NOT X-API-Key — requires INTERNAL_API-scoped token).

Used by SiteplugCreativeManager to sync siteplug_text_ad_search creatives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ShopInfo:
    """Affilizz shop metadata returned by the validate-shop endpoint."""

    shop_id: str
    shop_name: str
    shop_domain: str
    country_codes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AffilizzAPIError(Exception):
    """Exception raised for Affilizz Internal API errors."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AffilizzClient:
    """Client for the Affilizz Internal Text Ads API.

    Attributes:
        _base_url: API base URL (trailing slash stripped).
        _api_key: INTERNAL_API-scoped token sent as ``ApiKey`` header.
        _agent_id: Identifier written into ``createdBy`` / ``updatedBy`` fields.
        _timeout: HTTP request timeout in seconds.
        _shop_cache: In-memory cache mapping domain → ShopInfo | None.
        _http: Shared async httpx client.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        agent_id: str = "agent-siteplug",
        timeout: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._agent_id = agent_id
        self._timeout = timeout
        self._shop_cache: dict[str, ShopInfo | None] = {}
        self._http = httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """Return default request headers."""
        return {"ApiKey": self._api_key}

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Execute an authenticated HTTP request.

        Args:
            method: HTTP verb (GET, POST, PATCH, …).
            path: Path relative to base_url (must start with ``/``).
            **kwargs: Forwarded to ``httpx.AsyncClient.request``.

        Returns:
            Raw httpx.Response (caller decides how to interpret status codes).

        Raises:
            AffilizzAPIError: On network / transport errors.
        """
        url = f"{self._base_url}{path}"
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = await self._http.request(
                method=method,
                url=url,
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise AffilizzAPIError(
                f"Network error calling {method} {url}: {exc}",
                status_code=0,
            ) from exc
        return response

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def validate_shop(self, domain: str) -> ShopInfo | None:
        """Validate a shop domain and return its metadata.

        Results are cached in ``self._shop_cache`` so the API is called at
        most once per domain per client lifetime.

        Args:
            domain: Shop domain to validate (e.g. ``"example.com"``).

        Returns:
            :class:`ShopInfo` on success, ``None`` if the domain is unknown.

        Raises:
            AffilizzAPIError: On any non-200 / non-404 response.
        """
        if domain in self._shop_cache:
            return self._shop_cache[domain]

        response = await self._request(
            "GET",
            "/internal/text-ads/_validate-shop",
            params={"domain": domain},
        )

        if response.status_code == 200:
            data = response.json()
            shop = ShopInfo(
                shop_id=data["id"],
                shop_name=data["name"],
                shop_domain=data["domain"],
                country_codes=data.get("countryCodes", []),
            )
            self._shop_cache[domain] = shop
            return shop

        if response.status_code == 404:
            self._shop_cache[domain] = None
            return None

        raise AffilizzAPIError(
            f"validate_shop({domain!r}) failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )

    async def resolve_text_ad(self, external_id: str) -> dict | None:
        """Resolve a text ad by its external ID.

        Args:
            external_id: The ``externalId`` value set when the ad was created.

        Returns:
            Parsed JSON dict on success, ``None`` if not found.

        Raises:
            AffilizzAPIError: On any non-200 / non-404 response.
        """
        response = await self._request(
            "GET",
            "/internal/text-ads/_resolve",
            params={"externalId": external_id},
        )

        if response.status_code == 200:
            return response.json()

        if response.status_code == 404:
            return None

        raise AffilizzAPIError(
            f"resolve_text_ad({external_id!r}) failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )

    async def create_text_ad(self, payload: dict) -> dict:
        """Create a new text ad.

        Args:
            payload: Full text ad payload (must include ``externalId``,
                ``createdBy``, ``title``, ``description``, ``link``, etc.).

        Returns:
            Parsed JSON response from the API.

        Raises:
            AffilizzAPIError: On any non-2xx response (including 409 Conflict).
        """
        response = await self._request(
            "POST",
            "/internal/text-ads",
            json=payload,
        )

        if response.is_success:
            return response.json()

        raise AffilizzAPIError(
            f"create_text_ad failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )

    async def patch_text_ad(self, affilizz_id: str, payload: dict) -> dict:
        """Partially update an existing text ad.

        Args:
            affilizz_id: Affilizz-assigned text ad ID.
            payload: Fields to update (should include ``updatedBy``; must NOT
                include ``createdBy``).

        Returns:
            Parsed JSON response from the API.

        Raises:
            AffilizzAPIError: On any non-2xx response.
        """
        response = await self._request(
            "PATCH",
            f"/internal/text-ads/{affilizz_id}",
            json=payload,
        )

        if response.is_success:
            return response.json()

        raise AffilizzAPIError(
            f"patch_text_ad({affilizz_id!r}) failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )

    async def get_text_ad(self, affilizz_id: str) -> dict | None:
        """Fetch a text ad by its Affilizz ID.

        Args:
            affilizz_id: Affilizz-assigned text ad ID.

        Returns:
            Parsed JSON dict on success, ``None`` if not found (404).

        Raises:
            AffilizzAPIError: On any non-200 / non-404 response.
        """
        response = await self._request(
            "GET",
            f"/internal/text-ads/{affilizz_id}",
        )

        if response.status_code == 200:
            return response.json()

        if response.status_code == 404:
            return None

        raise AffilizzAPIError(
            f"get_text_ad({affilizz_id!r}) failed with HTTP {response.status_code}: {response.text}",
            status_code=response.status_code,
        )

    async def upsert_text_ad(self, payload: dict) -> dict:
        """Create or update a text ad using resolve-before-write semantics.

        Calls :meth:`resolve_text_ad` with ``payload["externalId"]``:

        - If found → PATCH the existing ad (``updatedBy`` = ``self._agent_id``,
          ``createdBy`` excluded from PATCH payload).
        - If not found → POST the full payload as a new ad.

        Args:
            payload: Full text ad payload including ``externalId`` and
                ``createdBy``.

        Returns:
            Parsed JSON response from the create or update call.

        Raises:
            AffilizzAPIError: On any API error during resolve, create, or update.
        """
        external_id = payload["externalId"]
        existing = await self.resolve_text_ad(external_id)

        if existing is not None:
            affilizz_id = existing["id"]
            patch_payload = {k: v for k, v in payload.items() if k != "createdBy"}
            patch_payload["updatedBy"] = self._agent_id
            logger.debug(
                "upsert_text_ad: patching existing ad %s (externalId=%s)",
                affilizz_id,
                external_id,
            )
            return await self.patch_text_ad(affilizz_id, patch_payload)

        logger.debug("upsert_text_ad: creating new ad (externalId=%s)", external_id)
        return await self.create_text_ad(payload)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def build_text_ad_payload(
    creative: dict,
    shop_info: ShopInfo,
    agent_id: str,
) -> dict:
    """Map an AdCP creative dict to an Affilizz text ad payload.

    The server always forces ``creationChannel`` to ``"internal-api"`` so it
    is intentionally omitted from the payload.

    Args:
        creative: AdCP creative dict (must contain ``creative_id`` and
            ``assets``).
        shop_info: Validated shop metadata from :meth:`AffilizzClient.validate_shop`.
        agent_id: Agent identifier written into ``createdBy``.

    Returns:
        Dict ready to pass to :meth:`AffilizzClient.create_text_ad` or
        :meth:`AffilizzClient.upsert_text_ad`.
    """
    assets: dict = creative.get("assets", {})

    country: str = assets.get("country", {}).get("content", "")
    content_source: str = assets.get("content_source", {}).get("content", "")

    payload: dict = {
        "externalId": creative["creative_id"],
        "createdBy": agent_id,
        "title": assets.get("title", {}).get("content", ""),
        "description": assets.get("description", {}).get("content", ""),
        "link": assets.get("click_url", {}).get("url", ""),
        "displayLink": assets.get("display_url", {}).get("content") or None,
        "shopId": shop_info.shop_id,
        "shopName": shop_info.shop_name,
        "shopDomain": shop_info.shop_domain,
        "countryCodes": [country] if country else shop_info.country_codes,
        "externalMetadata": {"contentSource": content_source} if content_source else None,
    }

    return payload
