"""TMP Provider package sync service.

Pushes media buy packages to all active/draining TMP Providers registered
for a tenant whenever a media buy is created or updated.

Design principles:
- Fire-and-forget: callers use asyncio.create_task() — never awaited directly
- Non-fatal: all exceptions are caught and logged as warnings; never raised
- Non-blocking: the API response is never delayed by TMP sync
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


async def sync_packages_to_tmp_provider(
    media_buy_id: str,
    tenant_id: str,
    session: "Session",
) -> None:
    """Push packages for a media buy to all active TMP Providers for the tenant.

    Queries the tmp_providers table for active/draining providers, builds the
    package payload from media_packages rows, and POSTs to each provider's
    /packages/sync endpoint.

    This function is designed to be called as a fire-and-forget task via
    asyncio.create_task(). It never raises — all exceptions are caught and
    logged as warnings so the caller's response is never delayed or blocked.

    Args:
        media_buy_id: The media buy whose packages should be synced.
        tenant_id: The tenant that owns the media buy.
        session: SQLAlchemy session (used for DB reads only; already committed).
    """
    import httpx
    from sqlalchemy import select

    try:
        from src.core.database.models import MediaBuy, MediaPackage, TMPProvider

        # ── 1. Discover active/draining TMP providers for this tenant ──────────
        stmt = select(TMPProvider).where(
            TMPProvider.tenant_id == tenant_id,
            TMPProvider.status.in_(["active", "draining"]),
        )
        providers = list(session.scalars(stmt).all())

        if not providers:
            logger.debug(
                "[TMP sync] No active TMP providers for tenant=%s — skipping sync",
                tenant_id,
            )
            return

        # ── 2. Load the media buy ───────────────────────────────────────────────
        media_buy = session.scalar(
            select(MediaBuy).where(
                MediaBuy.media_buy_id == media_buy_id,
                MediaBuy.tenant_id == tenant_id,
            )
        )
        if not media_buy:
            logger.warning(
                "[TMP sync] Media buy %s not found for tenant=%s — skipping sync",
                media_buy_id,
                tenant_id,
            )
            return

        # ── 3. Load packages ────────────────────────────────────────────────────
        pkg_stmt = select(MediaPackage).where(
            MediaPackage.media_buy_id == media_buy_id,
        )
        db_packages = list(session.scalars(pkg_stmt).all())

        if not db_packages:
            logger.debug(
                "[TMP sync] No packages found for media_buy=%s — skipping sync",
                media_buy_id,
            )
            return

        # ── 4. Build package payloads ───────────────────────────────────────────
        payloads: list[dict[str, Any]] = []
        for db_pkg in db_packages:
            cfg: dict[str, Any] = db_pkg.package_config or {}

            # Derive offering_id: prefer raw_request field, fall back to product_id
            offering_id: str = cfg.get("product_id") or ""

            # Brand info from media buy raw_request if available
            raw_req: dict[str, Any] = media_buy.raw_request or {}
            brand_raw = raw_req.get("brand") or {}
            brand_domain: str = brand_raw.get("domain") or ""
            brand_id: str = brand_raw.get("brand_id") or ""

            # Price info from package pricing_info
            pricing: dict[str, Any] = cfg.get("pricing_info") or {}
            price_amount: float = float(pricing.get("rate") or 0.0)
            price_currency: str = pricing.get("currency") or "USD"
            price_model: str = pricing.get("pricing_model") or "cpm"

            # Expiry from media buy end_time
            expires_at: str | None = (
                media_buy.end_time.isoformat() if media_buy.end_time else None
            )

            payload: dict[str, Any] = {
                "package_id": db_pkg.package_id,
                "media_buy_id": media_buy_id,
                "offering_id": offering_id,
                "brand": {
                    "domain": brand_domain,
                    "brand_id": brand_id,
                },
                "keywords": [],
                "topics": [],
                "summary": raw_req.get("buyer_ref") or media_buy_id,
                "creative_manifest": {},
                "price": {
                    "amount": price_amount,
                    "currency": price_currency,
                    "model": price_model,
                },
                "macros": {},
                "is_active": True,
                "expires_at": expires_at,
            }
            payloads.append(payload)

        # ── 5. POST to each provider ────────────────────────────────────────────
        async with httpx.AsyncClient(timeout=5.0) as client:
            for provider in providers:
                endpoint = (provider.endpoint or "").rstrip("/")
                url = f"{endpoint}/packages/sync"
                try:
                    resp = await client.post(url, json={"packages": payloads})
                    if resp.is_success:
                        logger.info(
                            "[TMP sync] Synced %d package(s) for media_buy=%s to provider=%s (%s) → %s",
                            len(payloads),
                            media_buy_id,
                            provider.name,
                            url,
                            resp.status_code,
                        )
                    else:
                        logger.warning(
                            "[TMP sync] Provider %s returned %s for media_buy=%s: %s",
                            provider.name,
                            resp.status_code,
                            media_buy_id,
                            resp.text[:200],
                        )
                except Exception as provider_err:
                    logger.warning(
                        "[TMP sync] Failed to sync to provider %s (%s) for media_buy=%s: %s",
                        provider.name,
                        url,
                        media_buy_id,
                        provider_err,
                    )

    except Exception as err:
        # Non-fatal: log and swallow so the caller's response is never affected
        logger.warning(
            "[TMP sync] Unexpected error syncing media_buy=%s tenant=%s: %s",
            media_buy_id,
            tenant_id,
            err,
        )
