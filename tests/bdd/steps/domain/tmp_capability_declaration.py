"""BDD step definitions for the TMP capability declaration (local feature).

The obligation — *a seller declares ``trusted_match.core`` exactly when it has a
provider the surface actually serves* — is transport-blind, so these steps are:
setup registers the state that makes the surface real (a row, via the factory),
dispatch goes through ``dispatch_request`` → ``call_via``, and the assertion
reads the field off the real serialized body via ``wire_field``.

This replaced a hand-written ``@parametrize("transport", [MCP, A2A, REST])`` in
``tests/integration/test_tmp_provider_integration.py`` that rolled its own
envelope extraction and structurally could not include ``e2e_rest`` (#1197
review).
"""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from src.core.tools.capabilities import TRUSTED_MATCH_FEATURE_ID
from tests.bdd.steps._outcome_helpers import wire_dict
from tests.bdd.steps.generic._dispatch import dispatch_request


@given(parsers.parse('a TMP provider is registered for the tenant with status "{status}"'))
def given_provider_with_status(ctx: dict, status: str) -> None:
    """Register exactly one provider in *status* for the scenario's tenant."""
    from tests.factories.tmp_provider import TMPProviderFactory

    TMPProviderFactory(
        tenant=ctx["tenant"],
        name=f"Capability Declaration Provider ({status})",
        status=status,
    )
    ctx["env"]._commit_factory_data()


@given("the tenant has no TMP provider registered")
def given_no_provider(ctx: dict) -> None:
    """The falsifiable half: a hardcoded constant would fail the Then below."""
    ctx["env"]._commit_factory_data()


@when("the Buyer Agent asks for the seller's capabilities")
def when_buyer_asks_for_capabilities(ctx: dict) -> None:
    dispatch_request(ctx)
    # `dispatch_request` sets ctx["result"] only when the dispatch RETURNED; a
    # dispatch that raised leaves only ctx["error"]. Reading ctx["result"] blindly
    # turned that into a bare KeyError that hid the actual failure.
    if "result" not in ctx:
        raise AssertionError(f"get_adcp_capabilities did not dispatch on {ctx['transport']}: {ctx.get('error')!r}")
    result = ctx["result"]
    assert result.is_success, f"get_adcp_capabilities failed on {ctx['transport']}: {result.error}"


def _declared_features(ctx: dict) -> list[str]:
    """``experimental_features`` as the buyer sees it, absent field included.

    Read through ``wire_dict`` rather than ``wire_field`` because absence is the
    assertion in half these scenarios and the field is omitted, not emitted as
    ``[]`` — indexing it would raise where the obligation says "not declared".
    """
    return list(wire_dict(ctx).get("experimental_features") or [])


@then(parsers.parse('experimental_features includes "{feature_id}"'))
def then_features_include(ctx: dict, feature_id: str) -> None:
    declared = _declared_features(ctx)
    assert feature_id in declared, f"{ctx['transport']}: expected {feature_id} in {declared}"
    # The id the production constant emits is the id the spec pattern grades.
    assert feature_id == TRUSTED_MATCH_FEATURE_ID


@then(parsers.parse('experimental_features does not include "{feature_id}"'))
def then_features_exclude(ctx: dict, feature_id: str) -> None:
    declared = _declared_features(ctx)
    assert feature_id not in declared, f"{ctx['transport']}: unexpected {feature_id} in {declared}"
