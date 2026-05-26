"""Unit tests for the TMP health-check background scheduler.

Tests the scheduler in src/services/tmp_health_scheduler.py which polls
each active/draining TMP provider's /health endpoint and writes the result
to health_status / last_health_checked_at columns.

Covers:
- _check_provider_health: healthy on 200, unhealthy on non-200, error on exception
- _check_all_providers: multi-provider fan-out, skip when no providers, error isolation
- Scheduler lifecycle: start/stop, singleton pattern
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.services.tmp_health_scheduler import (
    _check_provider_health,
    get_tmp_health_scheduler,
)


class TestCheckProviderHealth:
    """_check_provider_health probes a single provider's /health endpoint."""

    def test_returns_healthy_on_200(self):
        """200 response → 'healthy'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("src.services.tmp_health_scheduler.requests.get", return_value=mock_resp) as mock_get:
            result = _check_provider_health("https://provider.example.com/tmp")

        assert result == "healthy"
        mock_get.assert_called_once_with(
            "https://provider.example.com/tmp/health",
            timeout=5,
            allow_redirects=False,
        )

    def test_returns_unhealthy_on_non_200(self):
        """Non-200 response → 'unhealthy'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch("src.services.tmp_health_scheduler.requests.get", return_value=mock_resp):
            result = _check_provider_health("https://provider.example.com/tmp")

        assert result == "unhealthy"

    def test_returns_error_on_connection_failure(self):
        """ConnectionError → 'error'."""
        with patch(
            "src.services.tmp_health_scheduler.requests.get",
            side_effect=requests.ConnectionError("Connection refused"),
        ):
            result = _check_provider_health("https://provider.example.com/tmp")

        assert result == "error"

    def test_returns_error_on_timeout(self):
        """Timeout → 'error'."""
        with patch(
            "src.services.tmp_health_scheduler.requests.get",
            side_effect=requests.Timeout("Read timed out"),
        ):
            result = _check_provider_health("https://provider.example.com/tmp")

        assert result == "error"

    def test_strips_trailing_slash_from_endpoint(self):
        """Trailing slash on endpoint is stripped before appending /health."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("src.services.tmp_health_scheduler.requests.get", return_value=mock_resp) as mock_get:
            _check_provider_health("https://provider.example.com/tmp/")

        mock_get.assert_called_once_with(
            "https://provider.example.com/tmp/health",
            timeout=5,
            allow_redirects=False,
        )

    def test_allow_redirects_false_prevents_ssrf(self):
        """allow_redirects=False is always passed to prevent SSRF via open-redirect."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("src.services.tmp_health_scheduler.requests.get", return_value=mock_resp) as mock_get:
            _check_provider_health("https://provider.example.com")

        _, kwargs = mock_get.call_args
        assert kwargs["allow_redirects"] is False


class TestCheckAllProviders:
    """_check_all_providers polls every active/draining provider and persists results."""

    @pytest.mark.asyncio
    async def test_updates_health_status_for_each_provider(self):
        """Each provider gets its health_status updated via repository."""
        provider_a = MagicMock()
        provider_a.provider_id = "uuid-a"
        provider_a.tenant_id = "tenant-1"
        provider_a.endpoint = "https://a.example.com"

        provider_b = MagicMock()
        provider_b.provider_id = "uuid-b"
        provider_b.tenant_id = "tenant-1"
        provider_b.endpoint = "https://b.example.com"

        mock_session = MagicMock()

        with (
            patch("src.services.tmp_health_scheduler.get_db_session") as mock_get_db,
            patch("src.services.tmp_health_scheduler.TMPProviderRepository") as mock_repo_cls,
            patch("src.services.tmp_health_scheduler._check_provider_health") as mock_check,
        ):
            mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
            mock_repo_cls.get_all_active.return_value = [provider_a, provider_b]
            mock_check.side_effect = ["healthy", "unhealthy"]

            mock_repo_instance = MagicMock()
            mock_repo_cls.return_value = mock_repo_instance

            scheduler = get_tmp_health_scheduler()
            await scheduler._check_all_providers()

        assert mock_check.call_count == 2
        assert mock_repo_instance.update_health_status.call_count == 2
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_providers(self):
        """No active providers → no HTTP calls, no commit."""
        mock_session = MagicMock()

        with (
            patch("src.services.tmp_health_scheduler.get_db_session") as mock_get_db,
            patch("src.services.tmp_health_scheduler.TMPProviderRepository") as mock_repo_cls,
            patch("src.services.tmp_health_scheduler._check_provider_health") as mock_check,
        ):
            mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
            mock_repo_cls.get_all_active.return_value = []

            scheduler = get_tmp_health_scheduler()
            await scheduler._check_all_providers()

        mock_check.assert_not_called()
        mock_session.commit.assert_not_called()


class TestSchedulerLifecycle:
    """Scheduler start/stop and singleton pattern."""

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        """start() sets is_running and creates an asyncio task."""
        scheduler = get_tmp_health_scheduler()
        scheduler.is_running = False
        scheduler._task = None

        with patch.object(scheduler, "_run_scheduler", return_value=None):
            await scheduler.start()

        assert scheduler.is_running is True
        assert scheduler._task is not None

        # Clean up
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        """stop() sets is_running=False and cancels the task."""
        scheduler = get_tmp_health_scheduler()
        scheduler.is_running = False
        scheduler._task = None

        with patch.object(scheduler, "_run_scheduler", return_value=None):
            await scheduler.start()
            await scheduler.stop()

        assert scheduler.is_running is False

    def test_singleton_returns_same_instance(self):
        """get_tmp_health_scheduler() returns the same instance on repeated calls."""
        s1 = get_tmp_health_scheduler()
        s2 = get_tmp_health_scheduler()
        assert s1 is s2
