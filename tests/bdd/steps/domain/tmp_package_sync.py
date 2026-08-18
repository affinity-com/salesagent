"""BDD step definitions for TMP package sync (local feature).

The obligation these steps grade — *a buyer creates or updates a media buy, and
every registered active/draining provider holds current package data* — is
transport-blind, so the steps themselves are: setup goes through the env's
:class:`~tests.harness._mixins.TMPSyncMixin` seam, dispatch goes through
``dispatch_request`` → ``call_via``, and the assertion reads what the stub
provider actually received over a socket. Nothing here knows which transport it
is running on, which is why one scenario covers a2a/mcp/rest and e2e_rest
(#1197 review).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pytest_bdd import given, then, when

from tests.bdd.steps.generic._dispatch import dispatch_request
from tests.bdd.steps.generic.given_media_buy import _pricing_option_id
from tests.helpers.adcp_factories import create_test_package_request_dict


def _future(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _create_media_buy(ctx: dict) -> str:
    """Dispatch a real create through ctx['transport'] and return its media_buy_id."""
    env = ctx["env"]
    product = ctx["default_product"]
    dispatch_request(
        ctx,
        brand={"domain": "tmp-package-sync.example.com"},
        packages=[
            create_test_package_request_dict(
                product_id=product.product_id,
                pricing_option_id=_pricing_option_id(ctx["default_pricing_option"]),
                budget=5000.0,
            )
        ],
        start_time=_future(1),
        end_time=_future(30),
    )
    result = ctx["result"]
    assert result.is_success, f"create_media_buy failed on {ctx['transport']}: {result.error}"
    media_buy_id = result.payload.response.media_buy_id
    assert media_buy_id, "create_media_buy returned no media_buy_id"
    ctx["tmp_media_buy_id"] = media_buy_id
    # The seam owns the wait; without it the assertion races the fire-and-forget
    # thread (in-process) or the server's own thread (e2e_rest).
    env.await_tmp_sync(count=ctx["tmp_expected_deliveries"])
    return str(media_buy_id)


@given("a TMP provider is registered for the tenant")
def given_tmp_provider_registered(ctx: dict) -> None:
    """Register one active provider pointed at the env's collector."""
    ctx["env"].register_tmp_provider()
    ctx["tmp_expected_deliveries"] = 1


@given("the Buyer Agent created a media buy whose packages were delivered")
def given_created_media_buy_already_synced(ctx: dict) -> None:
    """Seed the update scenario with a create whose own sync already landed.

    Awaiting the create's delivery here is what makes the update assertion
    falsifiable: without it, a wrapper that dropped ``fire_tmp_sync`` from the
    update path would still find one delivery waiting and pass.
    """
    _create_media_buy(ctx)


@when("the Buyer Agent creates a media buy")
def when_buyer_creates_media_buy(ctx: dict) -> None:
    _create_media_buy(ctx)


@when("the Buyer Agent updates that media buy")
def when_buyer_updates_media_buy(ctx: dict) -> None:
    from src.core.schemas import UpdateMediaBuyRequest

    ctx["tmp_expected_deliveries"] = 2
    dispatch_request(ctx, req=UpdateMediaBuyRequest(media_buy_id=ctx["tmp_media_buy_id"], budget=7500.0))
    result = ctx["result"]
    assert result.is_success, f"update_media_buy failed on {ctx['transport']}: {result.error}"
    ctx["env"].await_tmp_sync(count=2)


def _assert_delivery(ctx: dict, index: int) -> None:
    """Assert the *index*-th (1-based) delivery carries this media buy's packages.

    Asserts the closed key set of the pinned ``available-package.json`` rather
    than "the expected keys are present": the schema is
    ``additionalProperties: false``, so an added key is a violation, and the
    exact-set form is what fails when the payload drifts.
    """
    env = ctx["env"]
    deliveries = env.tmp_sync_deliveries()
    assert len(deliveries) >= index, f"expected at least {index} delivery(ies), got {len(deliveries)}"
    entry = deliveries[index - 1]

    assert entry["path"] == "/tmp/packages/sync", f"provider_url() built the wrong path: {entry['path']!r}"

    body: Any = entry["body"]
    assert isinstance(body, list), f"packages sync body must be a JSON array, got {type(body).__name__}"
    assert body, "packages sync body was an empty array"

    for package in body:
        assert set(package) == {"package_id", "media_buy_id", "seller_agent"}, (
            f"delivered body diverged from available-package.json: {sorted(package)}"
        )
        assert package["media_buy_id"] == ctx["tmp_media_buy_id"]
        assert package["seller_agent"] == {"agent_url": env.tmp_seller_agent_url}


@then("the provider receives the packages for that media buy")
def then_provider_received_packages(ctx: dict) -> None:
    _assert_delivery(ctx, 1)


@then("the provider receives the packages for that media buy a second time")
def then_provider_received_packages_again(ctx: dict) -> None:
    _assert_delivery(ctx, 2)
