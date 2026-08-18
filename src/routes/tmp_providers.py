"""TMP Provider discovery endpoint.

Exposes:
    GET /tenant/{tenant_id}/tmp-providers/discovery

This endpoint is polled by the TMP Router every 30 s to discover which
provider endpoints to fan out context and identity match requests to.

Authentication reuses the codebase's one credential gate, resolved **inside the
path's tenant**: :func:`src.core.auth_utils.get_principal_from_token` is called
with the ``{tenant_id}`` from the URL, and its docstring is the guarantee this
route needs — "If tenant_id specified, ONLY look in that tenant."  A credential
issued to tenant A therefore resolves to nothing on tenant B's path, so a
cross-tenant read is *inexpressible* rather than merely rejected: there is no
``resolved_tenant == tenant_id`` comparison to get wrong, and no second
authentication scheme (no ``TMP_DISCOVERY_API_KEYS``, no "OPEN" mode, no
per-route header list) to keep in step with the first (#1197 review).

That also satisfies the pinned spec's authentication MUST for this surface —
AdCP 3.1.1 ``trusted-match/specification.mdx`` §"Router Requirements": routers
exposing dynamic registration MUST authenticate callers, and static API keys
are conformant only alongside IP allow-listing.  A per-tenant Sales Agent
credential is not a static process-global key.

Token extraction is ``UnifiedAuthMiddleware``'s job (``x-adcp-auth``, else
``Authorization: Bearer``), the same for this route as for every other REST
endpoint.

Response schema — ``TMPDiscoveryResponse``.  Each provider entry is the closed
key set of the pinned ``provider-registration.json``
(:data:`PROVIDER_REGISTRATION_SCHEMA`):
{
  "tenant_id": "si-host",
  "providers": [
    {
      "provider_id": "5f1c0e3a9b7d4e8fa1c2b3d4e5f60718",
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

from fastapi import APIRouter, Depends

from src.core.auth_context import AuthContext, get_auth_context
from src.core.auth_utils import get_principal_from_token
from src.core.database.repositories.uow import TMPProviderUoW
from src.core.exceptions import (
    AdCPAccountNotFoundError,
    AdCPAuthRequiredError,
    AdCPServiceUnavailableError,
)
from src.core.schemas.tmp_provider import TMPDiscoveryResponse
from src.core.security.url_validator import sanitize_for_log

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tmp-providers"])

#: The discovery wire's authority, as a path that resolves in this tree.
#:
#: The single declaration of the pinned schema this endpoint's provider entries
#: conform to.  Every other site that needs to name it — the ORM model, the
#: migration, the sync service's operator log line, the tests — references this
#: constant instead of re-typing a path, so the citation cannot drift out of
#: step with the file it points at (#1197 review).  ``tests/helpers/pinned_schema``
#: reads exactly this relative path out of the installed SDK, and
#: ``test_tmp_providers_discovery_route.py`` loads it, so a citation pointing at
#: a non-existent file fails a test rather than misleading a router operator.
#:
#: Upstream: https://github.com/adcontextprotocol/adcp/blob/main/static/schemas/v1/trusted-match/provider-registration.json
PROVIDER_REGISTRATION_SCHEMA = "trusted-match/provider-registration.json"

#: The one cross-service path this feature publishes, declared once.
#: The model, the admin blueprint and the migration reference this rather than
#: restating a path in prose (#1197 review).
DISCOVERY_ROUTE = "/tenant/{tenant_id}/tmp-providers/discovery"


async def require_tenant_credential(tenant_id: str, auth_ctx: AuthContext = get_auth_context) -> str:
    """Resolve the caller's credential **within** *tenant_id*, or raise 401.

    Tenant isolation is a property of the resolution, not a check layered on top
    of it: ``get_principal_from_token(token, tenant_id)`` only ever searches the
    named tenant (its own docstring: "If tenant_id specified, ONLY look in that
    tenant"), so there is no state in which a credential from another tenant
    yields a principal here.  Accepted credentials are a tenant's principal
    access tokens and its admin token — the same set every other Sales Agent
    surface accepts, which is why this route no longer owns an authentication
    scheme of its own (#1197 review).

    Returns the resolved principal id so the route can log *who* polled;
    unauthenticated callers never reach the route body.
    """
    token = (auth_ctx.auth_token or "").strip()
    principal_id = get_principal_from_token(token, tenant_id)[0] if token else None
    if not principal_id:
        raise AdCPAuthRequiredError(
            "Authentication required.",
            suggestion=(
                f"Provide a valid access token for tenant '{tenant_id}' via x-adcp-auth "
                "or Authorization: Bearer <token>."
            ),
        )
    return principal_id


@router.get("/tenant/{tenant_id}/tmp-providers/discovery", response_model=TMPDiscoveryResponse)
async def tmp_providers_discovery(
    tenant_id: str, principal_id: str = Depends(require_tenant_credential)
) -> TMPDiscoveryResponse:
    """Return the active TMP provider set for a tenant.

    Polled by the TMP Router every 30 s.  Requires a credential issued by the
    tenant in the path — see :func:`require_tenant_credential`, where the
    tenant scoping lives.

    Returns the typed :class:`TMPDiscoveryResponse` rather than a hand-built
    ``JSONResponse``: FastAPI then publishes an OpenAPI schema for this
    versioned contract and validates the outgoing keys against
    :data:`PROVIDER_REGISTRATION_SCHEMA`'s closed key set, which an unvalidated
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
    # to_discovery_dict() is also called INSIDE this block — TMPProvider
    # attributes expire on commit (default expire_on_commit=True), so
    # serializing after the `with` block closes hits a detached session and
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
        "[TMP discovery] tenant=%s principal=%s returned %d provider(s)",
        sanitize_for_log(tenant_id),
        sanitize_for_log(principal_id),
        len(provider_list),
    )

    return TMPDiscoveryResponse(tenant_id=tenant_id, providers=provider_list)
