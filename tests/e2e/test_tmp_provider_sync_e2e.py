"""E2E: TMP package sync crosses the process boundary on the live REST transport.

The in-process siblings (``TestFireTmpSyncDispatched`` /
``TestFireTmpSyncDispatchedOnUpdate`` in
``tests/integration/test_tmp_provider_integration.py``) cover MCP/A2A/REST by
stubbing ``httpx.Client`` and ``threading.Thread`` inside the test process.
Neither patch can reach the server process, so on the Docker/HTTP path the
daemon thread runs entirely server-side and a serialization or thread-lifetime
regression that only shows across the process boundary was uncaught (#1197
review).

This test closes that gap the only way the live path allows: it stands up a
real stub TMP provider endpoint, registers it in the live database, creates a
media buy over real HTTP (``POST /api/v1/media-buys``), and asserts on the
``POST /packages/sync`` request the server's own thread actually delivered —
real socket, real JSON, real thread lifetime.

Everything asserted here is produced by production code in the server
container: no ``httpx`` stub, no thread patch, no in-process seam.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tests.e2e._webhook_capture import WebhookCaptureHandler, run_webhook_capture_server
from tests.e2e.adcp_request_builder import build_adcp_media_buy_request, get_test_date_range
from tests.e2e.utils import live_db_env, wait_for_server_readiness
from tests.factories import delete_tmp_providers, replace_tmp_providers

pytestmark = pytest.mark.e2e

# Seller-agent host planted on the tenant so _resolve_seller_agent_url resolves a
# spec-valid https URL. ADCP_AGENT_URL is not set in the e2e stack, so the
# tenant.virtual_host branch is the one that runs — the same branch production
# uses when no override is configured.
_SELLER_AGENT_HOST = "tmp-e2e-seller.publisher.example.com"
_TENANT_SUBDOMAIN = "ci-test"


class PackageSyncReceiver(WebhookCaptureHandler):
    """Stub TMP provider: records the path and body of every POST it receives.

    Overrides :meth:`record` rather than ``do_POST`` so the HTTP framing stays
    in the shared receiver (``tests/e2e/_webhook_capture.py``). The path matters
    here — the assertion is that the server hit ``/packages/sync`` specifically,
    which a body-only capture could not distinguish from any other callback.
    """

    received_webhooks: list[Any] = []

    def record(self, payload):
        return {"path": self.path, "body": payload}


@pytest.fixture
def tmp_provider_endpoint():
    """Run a stub TMP provider and yield its container-reachable base URL."""
    with run_webhook_capture_server(PackageSyncReceiver, PackageSyncReceiver.received_webhooks) as info:
        port = info["url"].rsplit(":", 1)[1].split("/")[0]
        # The sales agent runs in a container and must reach this host process.
        # In-network runs set ADCP_WEBHOOK_HOST to the runner's compose alias; on
        # the host path the container reaches us via host.docker.internal
        # (docker-compose.e2e.yml maps it to host-gateway). Unlike the webhook
        # service, the TMP sync does not rewrite localhost, so the URL must be
        # container-reachable as written.
        host = os.getenv("ADCP_WEBHOOK_HOST") or "host.docker.internal"
        yield {"base_url": f"http://{host}:{port}/tmp", "received": info["received"]}


def _register_provider(live_server: dict, endpoint: str) -> str:
    """Register a TMP provider on the live DB and return its tenant_id.

    Also plants a public ``virtual_host`` on the tenant so the server can resolve
    a spec-valid https ``seller_agent.agent_url``; without it production
    correctly skips the sync and this test would fail for the wrong reason.
    """
    from src.core.database.models import Tenant

    with live_db_env(live_server) as env:
        session = env.get_session()
        tenant = session.scalars(select(Tenant).filter_by(subdomain=_TENANT_SUBDOMAIN)).first()
        if tenant is None:
            raise RuntimeError(
                f"Tenant with subdomain {_TENANT_SUBDOMAIN!r} not found in the live e2e DB — "
                "did the stack's init_database_ci.py seed run?"
            )
        tenant.virtual_host = _SELLER_AGENT_HOST
        session.commit()

        replace_tmp_providers(
            env,
            tenant.tenant_id,
            name="E2E Sync Collector",
            endpoint=endpoint,
            timeout_ms=2000,
        )
        return tenant.tenant_id


def _cleanup_provider(live_server: dict, tenant_id: str) -> None:
    """Remove the provider row so later e2e tests don't fan out to a dead port."""
    with live_db_env(live_server) as env:
        delete_tmp_providers(env, tenant_id)


def _first_product(base_url: str, token: str) -> tuple[str, str]:
    """Fetch a ``(product_id, pricing_option_id)`` pair over the live REST surface.

    Discovering the product from the server (rather than hardcoding a seeded id)
    keeps this test working if the CI seed catalog changes, and exercises the
    same auth path the media-buy call uses.
    """
    response = httpx.post(
        f"{base_url}/api/v1/products",
        json={"brand": {"domain": "tmp-e2e-brand.example.com"}, "brief": "display advertising"},
        headers=_auth_headers(token),
        timeout=30,
    )
    assert response.status_code == 200, f"get_products failed: {response.status_code} {response.text}"
    products = response.json().get("products") or []
    assert products, "live stack returned no products — cannot build a media buy"
    product = products[0]
    pricing_options = product.get("pricing_options") or []
    assert pricing_options, f"product {product['product_id']!r} has no pricing_options — cannot build a media buy"
    return product["product_id"], pricing_options[0]["pricing_option_id"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"x-adcp-auth": token, "x-adcp-tenant": _TENANT_SUBDOMAIN}


def _wait_for_sync(received: list, timeout: float = 30.0) -> dict:
    """Poll until the server's sync thread delivers a ``/packages/sync`` POST.

    The sync is fire-and-forget server-side, so there is no response to await —
    the arrival at this collector IS the observable. Fails with the captured
    paths so a wrong-path delivery is distinguishable from no delivery at all.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in received:
            if entry["path"].endswith("/packages/sync"):
                return entry
        time.sleep(0.5)
    raise AssertionError(
        f"No POST /packages/sync arrived within {timeout}s. "
        f"Captured requests: {json.dumps([e['path'] for e in received])}"
    )


class TestTmpSyncOverLiveRest:
    """create_media_buy over real HTTP delivers packages to a real provider endpoint."""

    def test_package_sync_reaches_provider_over_the_wire(
        self,
        live_server,
        test_auth_token,
        auto_approval_adapter,
        tmp_provider_endpoint,
    ):
        """The live server POSTs /packages/sync to the registered provider after a REST create.

        Grades what the in-process tests structurally cannot: the sync thread
        surviving the request/response cycle in the server process, and the
        payload surviving real JSON serialization across a socket.
        """
        base_url = live_server["mcp"]
        wait_for_server_readiness(base_url)

        tenant_id = _register_provider(live_server, tmp_provider_endpoint["base_url"])
        try:
            product_id, pricing_option_id = _first_product(base_url, test_auth_token)
            start_time, end_time = get_test_date_range()

            # Built by the shared e2e request builder so this test tracks the
            # AdCP package shape (plain-number budget, required pricing_option_id)
            # with every other e2e create instead of hand-rolling its own.
            request_body = build_adcp_media_buy_request(
                product_ids=[product_id],
                total_budget=5000.0,
                start_time=start_time,
                end_time=end_time,
                brand={"domain": "tmp-e2e-brand.example.com"},
                pricing_option_id=pricing_option_id,
            )
            response = httpx.post(
                f"{base_url}/api/v1/media-buys",
                json=request_body,
                headers=_auth_headers(test_auth_token),
                timeout=60,
            )
            assert response.status_code == 200, f"create_media_buy failed: {response.status_code} {response.text}"
            media_buy_id = response.json().get("media_buy_id")
            assert media_buy_id, f"create_media_buy returned no media_buy_id: {response.text}"

            entry = _wait_for_sync(tmp_provider_endpoint["received"])
        finally:
            _cleanup_provider(live_server, tenant_id)

        assert entry["path"] == "/tmp/packages/sync", (
            f"provider_url() built the wrong path on the live wire: {entry['path']!r}"
        )

        body = entry["body"]
        assert isinstance(body, list), f"packages sync body must be a JSON array, got {type(body).__name__}"
        assert body, "packages sync body was an empty array"

        package = body[0]
        # available-package.json is additionalProperties: false with exactly
        # these three required keys — assert the closed shape survived the wire,
        # not just that the fields are present.
        assert set(package) == {"package_id", "media_buy_id", "seller_agent"}, (
            f"live wire body diverged from available-package.json: {sorted(package)}"
        )
        assert package["media_buy_id"] == media_buy_id
        assert package["seller_agent"] == {"agent_url": f"https://{_SELLER_AGENT_HOST}/mcp"}
