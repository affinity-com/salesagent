"""Siteplug creative manager.

Routes creative assets by format_id:
- ``siteplug_text_ad_search`` → Affilizz Internal Text Ads API
- All other formats → stub returning ``{"status": "not_implemented"}``

Real SSP creative upload (Task 06) will extend this manager.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from typing import Any

from src.adapters.siteplug.affilizz_client import (
    AffilizzAPIError,
    AffilizzClient,
    build_text_ad_payload,
)
from src.core.schemas._base import AssetStatus

logger = logging.getLogger(__name__)

TEXT_AD_FORMAT_ID = "text_ad_search"


class SiteplugCreativeManager:
    """Manages Siteplug creative upload and association.

    Routes ``siteplug_text_ad_search`` creatives to the Affilizz Internal
    Text Ads API via :class:`~src.adapters.siteplug.affilizz_client.AffilizzClient`.
    All other formats return a ``not_implemented`` stub until Task 06.

    Attributes:
        _config: :class:`~src.adapters.siteplug.config_schema.SiteplugConnectionConfig`
            instance (may carry ``affilizz_internal_url`` / ``affilizz_api_key``).
        _client: :class:`~src.adapters.siteplug.client.SiteplugClient` for the
            Siteplug SSP API path (used by future Task 06 formats).
        _affilizz_client: Lazy-initialised :class:`AffilizzClient` singleton.
    """

    def __init__(self, config: Any, siteplug_client: Any) -> None:
        """Initialise the creative manager.

        Args:
            config: ``SiteplugConnectionConfig`` instance.
            siteplug_client: ``SiteplugClient`` instance (SSP API path).
        """
        self._config = config
        self._client = siteplug_client
        self._affilizz_client: AffilizzClient | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add_creative_assets(
        self,
        media_buy_id: str,
        assets: list[dict[str, Any]],
        today: Any,
    ) -> list[AssetStatus]:
        """Route each asset to the appropriate handler based on ``format_id``.

        Args:
            media_buy_id: Media buy identifier (for logging).
            assets: List of AdCP creative asset dicts.
            today: Current date (unused here; forwarded for interface compatibility).

        Returns:
            List of :class:`AssetStatus` objects, one per asset.
        """
        results: list[AssetStatus] = []

        for asset in assets:
            format_id = self._get_format_id(asset)
            creative_id = asset.get("creative_id", "<unknown>")

            if format_id == TEXT_AD_FORMAT_ID:
                try:
                    # asyncio.run() cannot be called from a running event loop (MCP server
                    # is already async). Run the coroutine in a fresh thread that owns its
                    # own event loop instead.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        raw = pool.submit(asyncio.run, self._sync_text_ad_to_affilizz(asset)).result()
                    results.append(
                        AssetStatus(
                            creative_id=creative_id,
                            status=raw.get("status", "active"),
                            message=raw.get("error") or raw.get("reason"),
                        )
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to sync text ad %s to Affilizz: %s",
                        creative_id,
                        exc,
                        exc_info=True,
                    )
                    results.append(
                        AssetStatus(
                            creative_id=creative_id,
                            status="failed",
                            message=str(exc),
                        )
                    )
            else:
                logger.debug(
                    "add_creative_assets: format '%s' not yet implemented (creative_id=%s)",
                    format_id,
                    creative_id,
                )
                results.append(
                    AssetStatus(
                        creative_id=creative_id,
                        status="not_implemented",
                        message=f"Format '{format_id}' not yet implemented",
                    )
                )

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_format_id(self, asset: dict[str, Any]) -> str:
        """Extract the format ID string from an asset dict.

        The ``format_id`` field may be either a plain string or a dict with
        an ``"id"`` key (e.g. ``{"id": "siteplug_text_ad_search", "agent_url": "…"}``).

        Args:
            asset: AdCP creative asset dict.

        Returns:
            Format ID string, or empty string if not present.
        """
        raw = asset.get("format_id", "")
        if isinstance(raw, dict):
            return raw.get("id", "")
        return str(raw)

    def _get_affilizz_client(self) -> AffilizzClient | None:
        """Return the lazy-singleton :class:`AffilizzClient`.

        Reads ``affilizz_internal_url`` and ``affilizz_api_key`` from config
        first, then falls back to ``AFFILIZZ_INTERNAL_URL`` / ``AFFILIZZ_API_KEY``
        environment variables (for local development).

        Returns:
            :class:`AffilizzClient` instance, or ``None`` if credentials are
            not configured (graceful degradation).
        """
        if self._affilizz_client is None:
            base_url: str = getattr(self._config, "affilizz_internal_url", "") or os.environ.get(
                "AFFILIZZ_INTERNAL_URL", ""
            )
            api_key: str = getattr(self._config, "affilizz_api_key", "") or os.environ.get("AFFILIZZ_API_KEY", "")
            if not base_url or not api_key:
                return None  # Graceful degradation — caller logs warning
            self._affilizz_client = AffilizzClient(base_url=base_url, api_key=api_key)
        return self._affilizz_client

    async def _sync_text_ad_to_affilizz(
        self,
        asset: dict[str, Any],
        account: Any = None,
    ) -> dict[str, Any]:
        """Upsert a ``siteplug_text_ad_search`` creative to the Affilizz API.

        Steps:
        1. Sandbox gate — skip if ``account.sandbox`` is ``True``.
        2. Config guard — skip if Affilizz credentials are not configured.
        3. Domain guard — skip if ``brand.domain`` is missing.
        4. Shop validation — ``GET /internal/text-ads/_validate-shop?domain=…``.
        5. Build payload via :func:`build_text_ad_payload`.
        6. Upsert via :meth:`AffilizzClient.upsert_text_ad` (resolve-before-write).
        7. Handle ``updatedManually`` 409 gracefully.

        Args:
            asset: AdCP creative asset dict.
            account: Optional account object (checked for ``sandbox`` flag).

        Returns:
            Result dict with ``status`` key.
        """
        creative_id: str = asset.get("creative_id", "<unknown>")

        # 1. Sandbox gate
        if account and getattr(account, "sandbox", False):
            logger.info(
                "Skipping Affilizz sync for creative %s — sandbox account",
                creative_id,
            )
            return {
                "status": "skipped",
                "reason": "sandbox",
                "creative_id": creative_id,
            }

        # 2. Config guard
        client = self._get_affilizz_client()
        if client is None:
            logger.warning(
                "Affilizz credentials not configured "
                "(AFFILIZZ_INTERNAL_URL / AFFILIZZ_API_KEY). "
                "Skipping text ad sync for creative %s.",
                creative_id,
            )
            return {
                "status": "skipped",
                "reason": "no_config",
                "creative_id": creative_id,
            }

        # 3. Domain guard
        domain: str = asset.get("brand", {}).get("domain", "")
        if not domain:
            logger.warning(
                "Missing brand.domain for creative %s — cannot validate shop. Skipping text ad sync.",
                creative_id,
            )
            return {
                "status": "skipped",
                "reason": "no_domain",
                "creative_id": creative_id,
            }

        # 4. Shop validation
        try:
            shop_info = await client.validate_shop(domain)
        except AffilizzAPIError as exc:
            logger.warning(
                "Shop validation failed for domain '%s' (creative_id=%s): %s",
                domain,
                creative_id,
                exc,
            )
            return {
                "status": "skipped",
                "reason": "shop_validation_error",
                "creative_id": creative_id,
            }

        if shop_info is None:
            logger.warning(
                "Shop not found in Affilizz catalog for domain '%s' "
                "(creative_id=%s). Skipping text ad write. "
                "Shop must be created in Affilizz before text ads can be synced.",
                domain,
                creative_id,
            )
            return {
                "status": "skipped",
                "reason": "shop_not_found",
                "domain": domain,
                "creative_id": creative_id,
            }

        # 5. Build payload
        payload = build_text_ad_payload(asset, shop_info, agent_id="agent-siteplug")

        # 6. Upsert with 409 guard
        try:
            result = await client.upsert_text_ad(payload)
            logger.info(
                "Upserted text ad for creative %s → Affilizz id=%s",
                creative_id,
                result.get("id"),
            )
            return {
                "status": "ok",
                "affilizz_id": result["id"],
                "action": "upserted",
                "creative_id": creative_id,
            }
        except AffilizzAPIError as exc:
            if exc.status_code == 409:
                logger.warning(
                    "Text ad %s was manually updated in Affilizz "
                    "and cannot be modified by agent (updatedManually=true). Skipping.",
                    creative_id,
                )
                return {
                    "status": "skipped",
                    "reason": "updated_manually",
                    "creative_id": creative_id,
                }
            raise
