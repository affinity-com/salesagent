"""TMP Provider discovery endpoint.

Exposes:
    GET /tenant/{tenant_id}/tmp-providers/discovery

This endpoint is polled by the TMP Router every 30 s to discover which
provider endpoints to fan out context and identity match requests to.

Authentication is **fail-closed**: the endpoint is locked by default.

Set ``TMP_DISCOVERY_API_KEYS`` to a comma-separated list of accepted keys to
grant access.  To explicitly disable authentication for internal-network-only
deployments, set ``TMP_DISCOVERY_API_KEYS=OPEN``.  Leaving the variable unset
or empty returns HTTP 500 so that misconfigured deployments fail loudly rather
than silently exposing tenant topology.

Accepted auth headers (any one is sufficient):
  - ``x-adcp-auth: <key>``
  - ``X-API-Key: <key>``
  - ``Authorization: Bearer <key>``

Response schema — ``TMPDiscoveryResponse``.  Each provider entry is the closed
key set of ``dist/schemas/3.1.1/trusted-match/provider-registration.json``:
{
  "tenant_id": "si-host",
  "providers": [
    {
      "provider_id": "<uuid>",
      "endpoint": "http://si-agent.localhost:3003",
      "context_match": true,
      "identity_match": true,
      "countries": ["US"],
      "uid_types": ["publisher_first_party", "uid2", "hashed_email"],
      "timeout_ms": 200,
      "priority": 0,
      "status": "active"
    }
  ]
}

``countries`` / ``uid_types`` / ``properties`` are omitted — not ``null`` — when
the provider restricts nothing: the schema types all three ``array`` with
``minItems: 1``.  ``name`` is not on this wire at all (it is not in the closed
schema); it lives on the admin serialization.  See
``TMPProvider.to_discovery_dict``.

Only providers whose status is 'active' or 'draining' are returned.
Providers with status 'inactive' are excluded entirely.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Depends, Request

from src.core.database.repositories.uow import TMPProviderUoW
from src.core.exceptions import (
    AdCPAccountNotFoundError,
    AdCPAuthRequiredError,
    AdCPConfigurationError,
    AdCPServiceUnavailableError,
)
from src.core.http_utils import parse_bearer_token as _parse_bearer_token
from src.core.schemas.tmp_provider import TMPDiscoveryResponse
from src.core.security.url_validator import sanitize_for_log

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tmp-providers"])


async def require_api_key(request: Request) -> None:
    """Require API key for the TMP discovery endpoint.

    Fail-closed: the endpoint is locked unless ``TMP_DISCOVERY_API_KEYS`` is
    explicitly configured.

    - ``TMP_DISCOVERY_API_KEYS=key1,key2`` — accept those keys only.
    - ``TMP_DISCOVERY_API_KEYS=OPEN`` — disable auth (internal-network-only
      deployments where the operator has made a deliberate choice).
    - Unset or empty — raise ``AdCPConfigurationError`` (500, terminal) so
      misconfigured deployments fail loudly instead of silently exposing tenant
      topology.  The operator must act; the buyer cannot recover this.

    Accepted headers (first non-empty value wins):
      - ``x-adcp-auth``
      - ``X-API-Key``
      - ``Authorization: Bearer <key>``

    Bearer parsing uses the shared ``parse_bearer_token()`` helper
    (``src.core.http_utils``) — the single canonical implementation across
    all four Bearer-parsing sites in the codebase.
    """
    raw = os.environ.get("TMP_DISCOVERY_API_KEYS", "").strip()

    if raw.upper() == "OPEN":
        logger.warning("[TMP discovery] API key auth disabled — TMP_DISCOVERY_API_KEYS=OPEN")
        return

    allowed = [k.strip() for k in raw.split(",") if k.strip()]
    if not allowed:
        raise AdCPConfigurationError(
            "TMP_DISCOVERY_API_KEYS is not configured.",
            suggestion=(
                "Ask the sales agent operator to set TMP_DISCOVERY_API_KEYS to a "
                "comma-separated list of API keys, or to 'OPEN' to disable authentication."
            ),
        )

    api_key = (
        request.headers.get("x-adcp-auth", "")
        or request.headers.get("X-API-Key", "")
        or _parse_bearer_token(request.headers.get("authorization", ""))
        or ""
    )
    # Compare on bytes: Starlette decodes header bytes as latin-1, so any byte
    # > 0x7F yields a non-ASCII str.  secrets.compare_digest raises TypeError
    # for non-ASCII strings — encode both sides to bytes so a malformed header
    # returns a clean 401 instead of a 500.
    try:
        api_key_bytes = api_key.encode("utf-8", "surrogatepass")
        if not any(secrets.compare_digest(api_key_bytes, k.encode("utf-8", "surrogatepass")) for k in allowed):
            raise AdCPAuthRequiredError(
                "Authentication required.",
                suggestion="Provide a valid API key via x-adcp-auth, X-API-Key, or Authorization: Bearer <key>.",
            )
    except (UnicodeEncodeError, TypeError):
        raise AdCPAuthRequiredError(
            "Authentication required.",
            suggestion="Provide a valid API key via x-adcp-auth, X-API-Key, or Authorization: Bearer <key>.",
        )


@router.get("/tenant/{tenant_id}/tmp-providers/discovery", response_model=TMPDiscoveryResponse)
async def tmp_providers_discovery(tenant_id: str, _: None = Depends(require_api_key)) -> TMPDiscoveryResponse:
    """Return the active TMP provider set for a tenant.

    Polled by the TMP Router every 30 s.  Requires API key authentication
    via ``TMP_DISCOVERY_API_KEYS`` (fail-closed: returns 500 when unset).

    Returns the typed :class:`TMPDiscoveryResponse` rather than a hand-built
    ``JSONResponse``: FastAPI then publishes an OpenAPI schema for this
    versioned contract and validates the outgoing keys against
    ``provider-registration.json``'s closed key set, which an unvalidated
    ``JSONResponse`` did not (#1197 review).

    Lifecycle filtering:
      active   → included
      draining → included (router stops sending new requests but in-flight complete)
      inactive → excluded
    """
    # Single TMPProviderUoW block: it already exposes both tmp_providers and
    # tenant_config repositories, so the tenant-existence check and the
    # provider read run as ONE transaction rather than two separate ones.
    #
    # provider.to_dict(...) is also called INSIDE this block — TMPProvider
    # attributes expire on commit (default expire_on_commit=True), so calling
    # to_dict() after the `with` block closes hits a detached session and
    # raises DetachedInstanceError.
    with TMPProviderUoW(tenant_id) as uow:
        # Both repository guards raise the same typed error, never `assert`:
        # `python -O` strips asserts, and an AssertionError escapes as an
        # un-enveloped 500 instead of the typed AdCP envelope this endpoint's
        # contract promises.  Every raise on this route carries `suggestion=`
        # so the buyer-facing envelope always has a next step (#1197 review).
        if uow.tenant_config is None:
            raise AdCPServiceUnavailableError(
                "Tenant config repository unavailable.",
                suggestion="Retry shortly; the sales agent could not open a tenant configuration session.",
            )
        if uow.tenant_config.get_tenant() is None:
            raise AdCPAccountNotFoundError(
                f"Tenant '{tenant_id}' not found.",
                suggestion="Provide a valid tenant ID.",
            )
        if uow.tmp_providers is None:
            raise AdCPServiceUnavailableError(
                "TMP provider repository unavailable.",
                suggestion="Retry shortly; the sales agent could not open a TMP provider session.",
            )

        providers = uow.tmp_providers.list_syncable()

        # to_discovery_dict() is the machine-wire serializer: the closed key set
        # of provider-registration.json, with absent conditional arrays omitted
        # rather than nulled. The admin views use to_admin_dict() instead.
        provider_list = [p.to_discovery_dict() for p in providers]

    logger.debug(
        "[TMP discovery] tenant=%s returned %d provider(s)",
        sanitize_for_log(tenant_id),
        len(provider_list),
    )

    return TMPDiscoveryResponse(tenant_id=tenant_id, providers=provider_list)
