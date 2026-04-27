"""TMP Provider discovery and registration endpoints.

Exposes:
    GET  /tenant/{tenant_id}/tmp-providers/discovery
         Polled by the TMP Router every 30 s to discover active providers.
         Unauthenticated — internal network only.

    POST /tenant/{tenant_id}/tmp-providers
         Register (or idempotently upsert) a TMP provider for a tenant.
         Used by seed-local.sh and CI tooling instead of raw SQL.
         Unauthenticated — internal network only.

Response schema for GET /discovery (mirrors the plan's discovery format):
{
  "tenant_id": "si-host",
  "providers": [
    {
      "provider_id": "<uuid>",
      "name": "si-agent-demo",
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

Only providers whose status is 'active' or 'draining' are returned by /discovery.
Providers with status 'inactive' are excluded entirely.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from src.core.database.database_session import get_db_session
from src.core.database.models import TMPProvider, Tenant
from src.core.database.repositories.tmp_provider import TMPProviderRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tmp-providers"])

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

_VALID_UID_TYPES = frozenset([
    "uid2", "rampid", "id5", "euid", "pairid",
    "maid", "hashed_email", "publisher_first_party", "other",
])

_VALID_STATUSES = frozenset(["active", "draining", "inactive"])


# ---------------------------------------------------------------------------
# Request schema for POST /tenant/{tenant_id}/tmp-providers
# ---------------------------------------------------------------------------


class TMPProviderRegisterRequest(BaseModel):
    """Body for POST /tenant/{tenant_id}/tmp-providers."""

    name: str
    endpoint: str
    context_match: bool = True
    identity_match: bool = True
    countries: list[str] = ["US"]
    uid_types: list[str] = ["publisher_first_party", "uid2", "hashed_email"]
    timeout_ms: int = 200
    priority: int = 0
    status: str = "active"


# ---------------------------------------------------------------------------
# POST /tenant/{tenant_id}/tmp-providers — register / upsert a provider
# ---------------------------------------------------------------------------


@router.post("/tenant/{tenant_id}/tmp-providers", status_code=201)
async def register_tmp_provider(
    tenant_id: str,
    body: TMPProviderRegisterRequest,
) -> JSONResponse:
    """Register (or idempotently upsert) a TMP provider for a tenant.

    Designed for use by seed-local.sh and CI tooling so that no raw SQL is
    needed to register a provider.  The endpoint is unauthenticated and
    intended for internal network use only.

    Idempotency: if a provider with the same ``name`` already exists for the
    tenant, its mutable fields are updated in-place and the existing
    ``provider_id`` is returned.  This makes the call safe to repeat on every
    ``make local-seed`` run.

    Returns:
        201 with ``{"provider_id": "...", "created": true}`` on creation.
        200 with ``{"provider_id": "...", "created": false}`` on update.
    """
    # Basic validation
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    if not body.endpoint.strip():
        raise HTTPException(status_code=422, detail="endpoint is required")
    if body.status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}",
        )
    invalid_uid_types = [u for u in body.uid_types if u not in _VALID_UID_TYPES]
    if invalid_uid_types:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid uid_type(s): {invalid_uid_types}. "
                f"Valid values: {sorted(_VALID_UID_TYPES)}"
            ),
        )

    with get_db_session() as session:
        # Verify tenant exists
        tenant_row = session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))
        if tenant_row is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")

        repo = TMPProviderRepository(session, tenant_id)

        # Idempotency: look up by name within the tenant
        existing = session.scalar(
            select(TMPProvider).where(
                TMPProvider.tenant_id == tenant_id,
                TMPProvider.name == body.name,
            )
        )

        if existing is not None:
            # Update mutable fields in-place
            repo.update_fields(
                existing.provider_id,
                endpoint=body.endpoint,
                context_match=body.context_match,
                identity_match=body.identity_match,
                countries=body.countries,
                uid_types=body.uid_types,
                timeout_ms=body.timeout_ms,
                priority=body.priority,
                status=body.status,
            )
            session.commit()
            logger.info(
                "[TMP register] Updated provider name=%s provider_id=%s tenant=%s",
                body.name,
                existing.provider_id,
                tenant_id,
            )
            return JSONResponse(
                status_code=200,
                content={"provider_id": existing.provider_id, "created": False},
            )

        # Create new provider
        provider = TMPProvider(
            tenant_id=tenant_id,
            name=body.name,
            endpoint=body.endpoint,
            context_match=body.context_match,
            identity_match=body.identity_match,
            countries=body.countries,
            uid_types=body.uid_types,
            timeout_ms=body.timeout_ms,
            priority=body.priority,
            status=body.status,
        )
        repo.create(provider)
        session.commit()

        logger.info(
            "[TMP register] Created provider name=%s provider_id=%s tenant=%s",
            body.name,
            provider.provider_id,
            tenant_id,
        )
        return JSONResponse(
            status_code=201,
            content={"provider_id": provider.provider_id, "created": True},
        )


# ---------------------------------------------------------------------------
# GET /tenant/{tenant_id}/tmp-providers/discovery — polled by TMP Router
# ---------------------------------------------------------------------------


@router.get("/tenant/{tenant_id}/tmp-providers/discovery")
async def tmp_providers_discovery(tenant_id: str) -> JSONResponse:
    """Return the active TMP provider set for a tenant.

    Polled by the TMP Router every 30 s.  Internal network only — no auth.

    Lifecycle filtering:
      active   -> included
      draining -> included (router stops sending new requests but in-flight complete)
      inactive -> excluded
    """
    with get_db_session() as session:
        # Verify tenant exists — return 404 for unknown tenants so the router
        # can distinguish "no providers" from "wrong tenant_id".
        tenant_row = session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))
        if tenant_row is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")

        stmt = (
            select(TMPProvider)
            .where(
                TMPProvider.tenant_id == tenant_id,
                # Exclude inactive providers; active + draining are forwarded.
                TMPProvider.status.in_(["active", "draining"]),
            )
            .order_by(TMPProvider.priority.asc(), TMPProvider.name.asc())
        )
        providers = session.scalars(stmt).all()

    provider_list = []
    for p in providers:
        provider_list.append(
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "endpoint": p.endpoint,
                "context_match": p.context_match,
                "identity_match": p.identity_match,
                # countries / uid_types may be None for legacy rows that pre-date
                # the 20260421000000 migration.  The router treats None as
                # "accepts all" for backward compatibility.
                "countries": p.countries,
                "uid_types": p.uid_types,
                "timeout_ms": p.timeout_ms,
                "priority": p.priority,
                "status": p.status,
            }
        )

    logger.debug(
        "[TMP discovery] tenant=%s returned %d provider(s)",
        tenant_id,
        len(provider_list),
    )

    return JSONResponse(
        content={
            "tenant_id": tenant_id,
            "providers": provider_list,
        }
    )
