"""Background health-check scheduler for TMP providers.

Polls each active/draining TMP provider's ``/health`` endpoint on a fixed
cadence and writes the result (``healthy``, ``unhealthy``, ``error``) to the
``health_status`` / ``last_health_checked_at`` columns.  The admin UI reads
from these columns instead of making a live HTTP call in the request cycle,
which avoids blocking workers for up to 5 s per provider.

The scheduler follows the same singleton + asyncio.create_task pattern used
by :mod:`src.services.delivery_webhook_scheduler` and
:mod:`src.services.media_buy_status_scheduler`.
"""

from __future__ import annotations

import asyncio
import logging

import requests

from src.core.database.database_session import get_db_session
from src.core.database.repositories.tmp_provider import TMPProviderRepository

logger = logging.getLogger(__name__)

# Poll every 60 seconds — frequent enough for the admin UI to show
# near-real-time status, infrequent enough to avoid hammering providers.
HEALTH_CHECK_INTERVAL_SECONDS = 60

# Per-provider HTTP timeout.  Shorter than the old inline 5 s because
# the scheduler can afford to mark a slow provider as unhealthy and
# retry on the next cycle.
HEALTH_CHECK_TIMEOUT_SECONDS = 5


def _check_provider_health(endpoint: str) -> str:
    """Probe a single provider's /health endpoint.

    Returns one of: ``"healthy"``, ``"unhealthy"``, ``"error"``.

    Uses ``allow_redirects=False`` to prevent SSRF via open-redirect even
    though the base URL was validated at registration time.
    """
    health_url = endpoint.rstrip("/") + "/health"
    try:
        resp = requests.get(
            health_url,
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        return "healthy" if resp.status_code == 200 else "unhealthy"
    except requests.RequestException:
        return "error"


class TMPHealthScheduler:
    """Background scheduler that polls TMP provider health endpoints."""

    def __init__(self) -> None:
        self.is_running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler background task."""
        async with self._lock:
            if self.is_running:
                logger.warning("TMP health scheduler is already running")
                return

            self.is_running = True
            self._task = asyncio.create_task(self._run_scheduler())
            logger.info(
                "TMP health scheduler started (checking every %ds)",
                HEALTH_CHECK_INTERVAL_SECONDS,
            )

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        async with self._lock:
            if not self.is_running:
                return
            self.is_running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("TMP health scheduler stopped")

    async def _run_scheduler(self) -> None:
        """Main scheduler loop — runs on a fixed cadence."""
        while self.is_running:
            try:
                await self._check_all_providers()
            except Exception as e:
                logger.error("Error in TMP health scheduler: %s", e, exc_info=True)
            finally:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

    async def _check_all_providers(self) -> None:
        """Poll every active/draining provider and persist the result."""
        loop = asyncio.get_running_loop()

        with get_db_session() as session:
            providers = TMPProviderRepository.get_all_active(session)
            if not providers:
                return

            for provider in providers:
                # Run the blocking HTTP call in a thread so we don't stall
                # the event loop.
                status = await loop.run_in_executor(
                    None,
                    _check_provider_health,
                    provider.endpoint,
                )

                repo = TMPProviderRepository(session, provider.tenant_id)
                repo.update_health_status(provider.provider_id, status)

            session.commit()

        logger.debug(
            "TMP health check complete: %d provider(s) checked",
            len(providers),
        )


# ---------------------------------------------------------------------------
# Global singleton (same pattern as delivery_webhook_scheduler)
# ---------------------------------------------------------------------------

_scheduler: TMPHealthScheduler | None = None


def get_tmp_health_scheduler() -> TMPHealthScheduler:
    """Get or create the global TMP health scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TMPHealthScheduler()
    return _scheduler


async def start_tmp_health_scheduler() -> None:
    """Start the global TMP health scheduler."""
    scheduler = get_tmp_health_scheduler()
    await scheduler.start()


async def stop_tmp_health_scheduler() -> None:
    """Stop the global TMP health scheduler."""
    scheduler = get_tmp_health_scheduler()
    await scheduler.stop()
