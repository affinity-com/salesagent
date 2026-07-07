# src/adapters/siteplug/brand_agent_client.py
"""Thin sync HTTP client for the brand-agent REST API.

Used by the Siteplug adapter at provisioning time to fetch a brand's owned
website domains (T10 brand.json enrichment) so they can be included in the
king domain whitelist for SiteDiscover (SDC) campaigns.

Only the fields needed for king domain extraction are consumed:
    GET /api/brands/resolve?domain=
    → BrandResponse.properties: list[str | dict]
      where dict entries have {type: "website", identifier: "nike.fr", ...}

The resolve endpoint checks the local brand-agent DB first (by domain), then
falls back to the upstream AdCP registry — no slug derivation needed.

Non-blocking contract: all methods return empty results on any error so that
a brand-agent outage never blocks campaign provisioning.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def fetch_brand_related_domains(
    *,
    brand_agent_url: str,
    brand_agent_api_key: str,
    brand_agent_tenant_id: str,
    domain: str,
    timeout: int = 10,
) -> list[str]:
    """Fetch owned website domains for a brand from the brand-agent REST API.

    Calls ``GET {brand_agent_url}/resolve?domain={domain}`` and extracts all
    ``properties`` entries where ``type == "website"`` and the relationship
    is not ``"delegated"`` or ``"ad_network"``.

    Uses the resolve endpoint (domain-based lookup) so no brand_id slug
    derivation is needed — the salesagent passes ``brand_domain`` directly.

    Args:
        brand_agent_url: Base URL of the brand-agent API
            (e.g. ``"https://brand-agent.internal/api"``).
        brand_agent_api_key: X-API-Key header value for brand-agent auth.
        brand_agent_tenant_id: Tenant scope header (X-Tenant-Id).
        domain: Brand domain (e.g. ``"nike.com"``).
        timeout: HTTP request timeout in seconds (default 10).

    Returns:
        Deduplicated list of domain strings (e.g. ``["nike.fr", "nike.com.au"]``).
        Returns an empty list on any error (non-fatal).
    """
    if not brand_agent_url or not domain:
        return []

    try:
        import httpx
    except ImportError:
        logger.warning(
            "[siteplug] brand_agent_client: httpx not available — skipping related_domains lookup"
        )
        return []

    url = f"{brand_agent_url.rstrip('/')}/resolve"
    headers: dict[str, str] = {}
    if brand_agent_api_key:
        headers["X-API-Key"] = brand_agent_api_key
    if brand_agent_tenant_id:
        headers["X-Tenant-Id"] = brand_agent_tenant_id

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers, params={"domain": domain})
            response.raise_for_status()
            data: dict[str, Any] = response.json()
    except Exception as exc:
        logger.warning(
            "[siteplug] brand_agent_client: GET %s?domain=%s failed (non-fatal, skipping related_domains): %s",
            url,
            domain,
            exc,
        )
        return []

    properties = data.get("properties") or []
    if not isinstance(properties, list):
        return []

    domains: list[str] = []
    seen: set[str] = set()

    for prop in properties:
        if isinstance(prop, str):
            # Plain domain string — include as-is
            d = prop.strip().lower()
            if d and d not in seen:
                seen.add(d)
                domains.append(d)
        elif isinstance(prop, dict):
            if prop.get("type") != "website":
                continue
            # Skip delegated / ad_network relationships
            rel = prop.get("relationship", "owned")
            if rel in ("delegated", "ad_network"):
                continue
            identifier = prop.get("identifier") or prop.get("name")
            if not identifier or not isinstance(identifier, str):
                continue
            d = identifier.strip().lower()
            if d and d not in seen:
                seen.add(d)
                domains.append(d)

    if domains:
        logger.info(
            "[siteplug] brand_agent_client: fetched %d related domain(s) for domain=%r: %s",
            len(domains),
            domain,
            ", ".join(domains),
        )

    return domains
