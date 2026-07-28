"""Integration tests for TMP Provider feature.

End-to-end scenarios exercised against a real PostgreSQL database:

1. test_discovery_returns_active_providers
   Discovery endpoint (GET /tenant/{id}/tmp-providers/discovery) returns
   active/draining providers and excludes inactive ones.

2. test_sync_packages_posts_to_providers
   sync_packages_for_media_buy fans out to all syncable providers; outbound
   HTTP is stubbed at the httpx.Client level (not at _post_packages_sync) so
   the full sync path — URL construction, auth header, body shape, AND the
   resolve-before-MediaBuyUoW seller_agent lookup — is graded.

3. test_health_scheduler_tick_persists_status
   TMPHealthScheduler.tick() probes providers (HTTP stubbed) and persists the
   resulting health_status to the DB.

4. TestFireTmpSyncDispatched / TestFireTmpSyncDispatchedOnUpdate
   (both parametrized over MCP/A2A/REST)
   Dispatch create_media_buy and update_media_buy through the REAL
   per-transport pipeline (dispatch → wrapper → _impl → fire_tmp_sync), not a
   hand-built MagicMock response, so a regression in the wiring on any
   transport fails these tests. Only httpx.Client.post is stubbed; thread
   spawn, URL construction, auth header, and body shape run against real
   production code. The E2E_REST transport is graded in
   tests/e2e/test_tmp_provider_sync_e2e.py, where the sync runs server-side
   and in-process patching cannot reach it.

beads: salesagent-tmp-sync
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.factories import MediaBuyFactory, MediaPackageFactory, TenantFactory, TMPProviderFactory
from tests.harness._base import IntegrationEnv
from tests.harness.media_buy_create import MediaBuyCreateEnv
from tests.harness.transport import Transport
from tests.helpers.adcp_factories import create_test_package_request_dict

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Shared integration env — no external patches (we patch inline per test)
# ---------------------------------------------------------------------------


class _TMPEnv(IntegrationEnv):
    """Bare integration env for TMP tests — external patches applied inline."""

    EXTERNAL_PATCHES: dict[str, str] = {}


def _make_mock_http_client(status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Client context manager whose .post() returns *status_code*.

    Used by SF-3 and SF-4 tests to stub outbound HTTP at the httpx.Client level
    rather than at _post_packages_sync, so the full sync path (URL construction,
    auth header, body serialisation) is exercised against real production code.
    """
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.raise_for_status = MagicMock(return_value=None)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    return mock_client


# ---------------------------------------------------------------------------
# 1. Discovery endpoint returns active providers
# ---------------------------------------------------------------------------


class TestDiscoveryReturnsActiveProviders:
    """GET /tenant/{id}/tmp-providers/discovery returns active+draining, excludes inactive."""

    def test_discovery_returns_active_providers(self, integration_db):
        """Active and draining providers appear in the discovery response; inactive do not."""
        from starlette.testclient import TestClient

        from src.app import app

        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_int_disc_t1")
            TMPProviderFactory(tenant=tenant, name="Active Provider", status="active")
            TMPProviderFactory(tenant=tenant, name="Draining Provider", status="draining")
            TMPProviderFactory(tenant=tenant, name="Inactive Provider", status="inactive")
            env._commit_factory_data()

        with patch.dict(os.environ, {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/tenant/tmp_int_disc_t1/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tmp_int_disc_t1"
        names = {p["name"] for p in data["providers"]}
        assert "Active Provider" in names
        assert "Draining Provider" in names
        assert "Inactive Provider" not in names


# ---------------------------------------------------------------------------
# 2. sync_packages_for_media_buy fans out to all syncable providers
# ---------------------------------------------------------------------------


class TestSyncPackagesPostsToProviders:
    """sync_packages_for_media_buy POSTs to every syncable provider (HTTP stubbed at httpx level)."""

    def test_sync_packages_posts_to_providers(self, integration_db):
        """With two active providers and one package, httpx.Client.post is called twice.

        Stubs httpx.Client (not _post_packages_sync) so the full sync path is
        graded: URL construction via provider_url(), auth header via bearer_headers(),
        and JSON body shape from _build_package_payload().

        Deliberately does NOT set ADCP_AGENT_URL: that env-var branch returns
        before _resolve_seller_agent_url ever opens TenantConfigUoW, which would
        mask the round-12 resolve-before-MediaBuyUoW scoped-session fix (a nested
        TenantConfigUoW.__exit__ inside an open MediaBuyUoW block removes the
        scoped session the outer block still needs). Giving the tenant a public
        virtual_host instead forces the tenant-lookup branch to run for real
        against Postgres, so a regression of that ordering fix fails this test.
        """
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_int_sync_t1", virtual_host="tmp-int-sync-t1.publisher.example.com")
            mb = MediaBuyFactory(tenant=tenant)
            MediaPackageFactory(
                media_buy=mb,
                package_config={
                    "product_id": "prod-001",
                    "name": "Test Package",
                    "is_active": True,
                },
            )
            TMPProviderFactory(
                tenant=tenant,
                name="Provider Alpha",
                endpoint="https://alpha.example.com/tmp",
                status="active",
            )
            TMPProviderFactory(
                tenant=tenant,
                name="Provider Beta",
                endpoint="https://beta.example.com/tmp",
                status="active",
            )
            env._commit_factory_data()
            media_buy_id = mb.media_buy_id
            tenant_id = tenant.tenant_id

        from src.services.tmp_provider_sync import sync_packages_for_media_buy

        mock_client = _make_mock_http_client(200)
        with (
            patch("src.services.tmp_provider_sync.httpx.Client", return_value=mock_client),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ADCP_AGENT_URL", None)
            sync_packages_for_media_buy(tenant_id, media_buy_id)

        expected_seller_agent_url = "https://tmp-int-sync-t1.publisher.example.com/mcp"

        # Both providers must have been called
        assert mock_client.post.call_count == 2

        # Assert the URLs hit — provider_url() appends /packages/sync
        called_urls = {call.args[0] for call in mock_client.post.call_args_list}
        assert called_urls == {
            "https://alpha.example.com/tmp/packages/sync",
            "https://beta.example.com/tmp/packages/sync",
        }

        # No auth credentials on either provider → no Authorization header
        for call in mock_client.post.call_args_list:
            headers = call.kwargs.get("headers", call.args[2] if len(call.args) > 2 else {})
            assert "Authorization" not in headers

        # Body must be a list of AvailablePackage dicts with required fields
        for call in mock_client.post.call_args_list:
            body = call.kwargs.get("json", call.args[1] if len(call.args) > 1 else None)
            assert isinstance(body, list)
            assert len(body) == 1
            pkg_payload = body[0]
            assert "package_id" in pkg_payload
            assert "media_buy_id" in pkg_payload
            assert pkg_payload["media_buy_id"] == media_buy_id
            assert "seller_agent" in pkg_payload
            # Resolved via the real TenantConfigUoW → tenant.virtual_host path,
            # not an env-var shortcut — proves the tenant lookup actually ran.
            assert pkg_payload["seller_agent"]["agent_url"] == expected_seller_agent_url


# ---------------------------------------------------------------------------
# 3. TMPHealthScheduler.tick() persists health_status to DB
# ---------------------------------------------------------------------------


class TestHealthSchedulerTickPersistsStatus:
    """TMPHealthScheduler.tick() writes health_status to the DB after probing."""

    def test_health_scheduler_tick_persists_status(self, integration_db):
        """After tick(), the provider's health_status column is updated in the DB."""
        import asyncio

        from src.core.database.repositories.uow import TMPProviderUoW
        from src.services.tmp_health_scheduler import TMPHealthScheduler

        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_int_health_t1")
            provider = TMPProviderFactory(
                tenant=tenant,
                name="Health Provider",
                endpoint="https://health.example.com/tmp",
                status="active",
            )
            env._commit_factory_data()
            provider_id = provider.provider_id
            tenant_id = tenant.tenant_id

        # Stub the HTTP probe so no real network call is made
        with patch(
            "src.services.tmp_health_scheduler._check_provider_health",
            new=AsyncMock(return_value="healthy"),
        ):
            scheduler = TMPHealthScheduler()
            asyncio.run(scheduler.tick())

        # Verify the health_status was persisted. Read back through the repository
        # (not get_db_session in a test body) per CLAUDE.md Pattern #8 — this also
        # grades that the scheduler's write is visible through the same repository
        # method the admin UI reads.
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            updated = uow.tmp_providers.get_by_id(provider_id)

            assert updated is not None
            assert updated.health_status == "healthy"
            assert updated.last_health_checked_at is not None


# ---------------------------------------------------------------------------
# 4. fire_tmp_sync dispatched transport-parameterized test
# ---------------------------------------------------------------------------


def _future(days: int) -> str:
    """Return an ISO 8601 datetime string N days in the future."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


# Transports that run the real wrapper chain in-process.  IMPL is excluded
# deliberately: it bypasses the wrappers, which is where fire_tmp_sync lives.
# E2E_REST is graded separately in tests/e2e/test_tmp_provider_sync_e2e.py —
# in-process patching cannot reach the server process.
_DISPATCHED_TRANSPORTS = [Transport.MCP, Transport.A2A, Transport.REST]

_SELLER_AGENT_URL = "https://salesagent.example.com/mcp"
_PROVIDER_ENDPOINT = "https://fire.example.com/tmp"


@contextmanager
def _captured_tmp_sync():
    """Stub outbound sync HTTP and join every ``fire_tmp_sync`` thread on exit.

    ``fire_tmp_sync`` is fire-and-forget, so the daemon thread it spawns races
    the assertions.  The join must happen INSIDE the patch context: the thread
    body reads ``ADCP_AGENT_URL`` and calls ``httpx.Client()``, so joining after
    the patches are torn down sends a real outbound connection instead of
    hitting the mock (observed as a real DNS/connect failure on REST).

    Yields the mock client so callers can assert on ``.post``.  Shared by the
    create and update siblings — the capture mechanics are identical and only
    the dispatched call differs (CLAUDE.md DRY invariant).
    """
    mock_client = _make_mock_http_client(200)
    spawned_threads: list[threading.Thread] = []
    original_start = threading.Thread.start

    def _track_start(self_thread: threading.Thread) -> None:
        spawned_threads.append(self_thread)
        original_start(self_thread)

    with (
        patch("src.services.tmp_provider_sync.httpx.Client", return_value=mock_client),
        patch.dict(os.environ, {"ADCP_AGENT_URL": _SELLER_AGENT_URL}),
        patch.object(threading.Thread, "start", _track_start),
    ):
        try:
            yield mock_client
        finally:
            for t in spawned_threads:
                t.join(timeout=10)


def _assert_synced_once(mock_client: MagicMock, *, media_buy_id: str, transport: Transport) -> None:
    """Assert exactly one ``POST /packages/sync`` carrying *media_buy_id*."""
    assert mock_client.post.call_count == 1, (
        f"Expected fire_tmp_sync to POST once via {transport.value}, got {mock_client.post.call_count} calls"
    )
    call = mock_client.post.call_args_list[0]
    assert call.args[0] == f"{_PROVIDER_ENDPOINT}/packages/sync"

    body = call.kwargs.get("json", call.args[1] if len(call.args) > 1 else None)
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["media_buy_id"] == media_buy_id
    assert body[0]["seller_agent"]["agent_url"] == _SELLER_AGENT_URL


class TestFireTmpSyncDispatched:
    """create_media_buy on each real transport reaches fire_tmp_sync and POSTs.

    Dispatches ``create_media_buy`` through the REAL per-transport pipeline
    (dispatch → wrapper → ``_create_media_buy_impl`` → ``fire_tmp_sync``) via
    ``MediaBuyCreateEnv.call_via``, not a hand-built ``MagicMock`` response —
    the seam that changed (dispatch → wrapper → ``fire_tmp_sync``) is exactly
    what a hand-built mock bypasses. Only ``httpx.Client.post`` is stubbed;
    thread spawn, URL construction, auth header, and body shape are graded
    against real production code, and the created ``media_buy_id`` is
    asserted on the outbound wire body.
    """

    @pytest.mark.parametrize("transport", _DISPATCHED_TRANSPORTS, ids=lambda t: t.value)
    def test_fire_tmp_sync_dispatched_posts_to_providers(self, integration_db, transport):
        """create_media_buy via *transport* fires a real POST to the TMP provider."""
        with MediaBuyCreateEnv() as env:
            tenant, _principal, product, _pricing_option = env.setup_media_buy_data()
            TMPProviderFactory(
                tenant=tenant,
                name="Fire Provider",
                endpoint=_PROVIDER_ENDPOINT,
                status="active",
            )
            env._commit_factory_data()

            with _captured_tmp_sync() as mock_client:
                result = env.call_via(
                    transport,
                    brand={"domain": "tmp-fire-dispatch.example.com"},
                    packages=[
                        create_test_package_request_dict(
                            product_id=product.product_id,
                            pricing_option_id="cpm_usd_fixed",
                            budget=5000.0,
                        )
                    ],
                    start_time=_future(1),
                    end_time=_future(30),
                )

        assert result.is_success, f"create_media_buy failed on {transport.value}: {result.error}"
        media_buy_id = result.payload.response.media_buy_id
        assert media_buy_id
        _assert_synced_once(mock_client, media_buy_id=media_buy_id, transport=transport)


class TestFireTmpSyncDispatchedOnUpdate:
    """update_media_buy on each real transport reaches fire_tmp_sync and POSTs.

    The update sibling of :class:`TestFireTmpSyncDispatched`.  Until this
    existed, the update half of the sync was graded only by unit tests that
    patched ``threading.Thread`` and asserted the thread was *constructed* — an
    in-process object no transport observes.  Deleting the ``fire_tmp_sync``
    call from an update wrapper failed nothing (#1197 review).

    Dispatch goes through ``MediaBuyDualEnv``, which is the update harness with
    real per-transport wrapper dispatch (A2A via the real ``on_message_send``,
    MCP via the real FastMCP ``Client``, REST via the route).  Note this is NOT
    ``MediaBuyUpdateEnv``: that is the *unit* env whose ``call_via`` routes every
    transport through ``call_impl``, skipping the wrappers where
    ``fire_tmp_sync`` lives.  Adding a second real-dispatch update path to it
    would duplicate what ``MediaBuyDualEnv`` already owns.
    """

    @pytest.mark.parametrize("transport", _DISPATCHED_TRANSPORTS, ids=lambda t: t.value)
    def test_fire_tmp_sync_dispatched_posts_on_update(self, integration_db, transport):
        """update_media_buy via *transport* fires a real POST to the TMP provider."""
        from src.core.schemas import UpdateMediaBuyRequest
        from tests.harness.media_buy_dual import MediaBuyDualEnv

        media_buy_id = f"mb_tmp_update_{transport.value}"

        with MediaBuyDualEnv() as env:
            tenant, principal, _product, _pricing_option = env.setup_media_buy_data()
            media_buy = MediaBuyFactory(
                tenant=tenant,
                principal=principal,
                media_buy_id=media_buy_id,
                status="active",
            )
            # The sync only POSTs when the media buy has packages — without one
            # sync_packages_for_media_buy short-circuits and the test would pass
            # vacuously whether or not the wrapper fired.
            MediaPackageFactory(
                media_buy=media_buy,
                package_config={"product_id": "prod-001", "name": "Update Package", "is_active": True},
            )
            TMPProviderFactory(
                tenant=tenant,
                name="Fire Provider",
                endpoint=_PROVIDER_ENDPOINT,
                status="active",
            )
            env._commit_factory_data()

            with _captured_tmp_sync() as mock_client:
                env.call_via(
                    transport,
                    req=UpdateMediaBuyRequest(media_buy_id=media_buy_id, budget=15000.0),
                )

        _assert_synced_once(mock_client, media_buy_id=media_buy_id, transport=transport)
