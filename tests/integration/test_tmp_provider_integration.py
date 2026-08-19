"""Integration tests for TMP Provider feature.

End-to-end scenarios exercised against a real PostgreSQL database:

1. test_discovery_returns_active_providers / TestDiscoveryAuth
   The discovery contract (GET /tenant/{id}/tmp-providers/discovery) served by
   the PRODUCTION app: returns active/draining providers and excludes inactive
   ones, every entry validated against the pinned provider-registration schema,
   and the credential gate graded for real — no credential, another tenant's
   credential, this tenant's principal token, this tenant's admin token. The
   401 envelope is produced by ``src.app.app``'s own handler, not a re-declared
   copy (#1197 review).

2. test_sync_packages_posts_to_providers
   sync_packages_for_media_buy fans out to all syncable providers; outbound
   HTTP is stubbed at the httpx.Client level (not at _post_packages_sync) so
   the full sync path — URL construction, auth header, body shape, AND the
   resolve-before-MediaBuyUoW seller_agent lookup — is graded.

3. test_health_scheduler_tick_persists_status
   TMPHealthScheduler.tick() probes providers (HTTP stubbed) and persists the
   resulting health_status to the DB.

The buyer-triggered half — "a create or update on any transport delivers the
packages to every registered provider" — is NOT here. It is one BDD scenario
(``tests/bdd/features/local-tmp-package-sync.feature``) fanned out by the
harness over a2a/mcp/rest and e2e_rest through the env-owned seam
(``tests.harness._mixins.TMPSyncMixin``). This file used to carry a hand-written
``_DISPATCHED_TRANSPORTS`` list plus a process-wide ``threading.Thread.start`` /
``httpx.Client`` patch, and ``tests/e2e/test_tmp_provider_sync_e2e.py`` carried a
second, independent implementation of the same seed→dispatch→collect→assert for
one transport; both are replaced by that scenario (#1197 review).

beads: salesagent-tmp-sync
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from src.routes.tmp_providers import PROVIDER_REGISTRATION_SCHEMA
from tests.factories import MediaBuyFactory, MediaPackageFactory, PrincipalFactory, TenantFactory, TMPProviderFactory
from tests.harness._base import IntegrationEnv
from tests.helpers.admin_client import make_super_admin_client
from tests.helpers.envelope_assertions import assert_envelope_shape
from tests.helpers.pinned_schema import validate_against_pinned_schema
from tests.helpers.tmp_provider_http import make_mock_http_client

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Shared integration env — no external patches (we patch inline per test)
# ---------------------------------------------------------------------------


class _TMPEnv(IntegrationEnv):
    """Bare integration env for TMP tests — external patches applied inline."""

    EXTERNAL_PATCHES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# 1. Discovery endpoint returns active providers
# ---------------------------------------------------------------------------


def _discovery_client() -> TestClient:
    """A client over the production app — its router mount, middleware and handlers."""
    from src.app import app

    return TestClient(app, raise_server_exceptions=False)


def _get_discovery(tenant_id: str, token: str | None) -> object:
    """GET the discovery contract as *token*'s owner (or unauthenticated)."""
    headers = {"x-adcp-auth": token} if token else {}
    return _discovery_client().get(f"/tenant/{tenant_id}/tmp-providers/discovery", headers=headers)


class TestDiscoveryReturnsActiveProviders:
    """GET /tenant/{id}/tmp-providers/discovery returns active+draining, excludes inactive."""

    def test_discovery_returns_active_providers(self, integration_db):
        """Active and draining providers appear in the discovery response; inactive do not.

        Also grades every returned entry against the pinned
        provider-registration schema. That per-entry assertion is what a
        hand-maintained key list could not do: it failed on ``provider_id``
        (a hyphenated UUID against ``^[A-Za-z0-9_]+$``) until the column became
        charset-safe, so every entry this endpoint published was rejected by the
        schema it declares conformance to (#1197 review).
        """
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_int_disc_t1")
            principal = PrincipalFactory(tenant=tenant)
            active = TMPProviderFactory(tenant=tenant, name="Active Provider", status="active")
            draining = TMPProviderFactory(tenant=tenant, name="Draining Provider", status="draining")
            inactive = TMPProviderFactory(tenant=tenant, name="Inactive Provider", status="inactive")
            env._commit_factory_data()
            # Read the ids while the session is open — the wire identifies
            # providers by provider_id, not by the admin-only `name` (absent
            # from the closed provider-registration.json key set, #1197 review).
            token = principal.access_token
            active_id, draining_id, inactive_id = (
                active.provider_id,
                draining.provider_id,
                inactive.provider_id,
            )

        response = _get_discovery("tmp_int_disc_t1", token)

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tenant_id"] == "tmp_int_disc_t1"
        returned_ids = {p["provider_id"] for p in data["providers"]}
        assert active_id in returned_ids
        assert draining_id in returned_ids
        assert inactive_id not in returned_ids

        for entry in data["providers"]:
            validate_against_pinned_schema(PROVIDER_REGISTRATION_SCHEMA, entry)


class TestAdminWriteReachesTheDiscoveryContract:
    """The round trip: an operator registers a provider, the contract publishes it.

    Invariant 2 spans two layers, and until now nothing owned the seam between
    them. The admin-write tests assert on the kwargs handed to
    ``create_from_fields`` with the UoW mocked, and the discovery tests validate
    the entry from factory-built rows with the UoW mocked — so the actual write and
    the actual read of what was written were mocked on both sides, and their
    agreement was a coincidence maintained by two mocks agreeing on a signature.
    That is the gap through which a hyphenated ``provider_id`` shipped every entry
    in a schema-rejecting shape for eighteen rounds (#1197 review).

    Real Flask client, real form POST, real Postgres, real discovery request, every
    published entry validated against the pinned schema.
    """

    _FORM = {
        "name": "Round Trip Provider",
        "endpoint": "https://roundtrip.example.com/tmp",
        "context_match": "on",
        "identity_match": "on",
        "countries": "US, DE",
        "uid_types": "uid2,id5",
        "timeout_ms": "250",
        "priority": "3",
        "status": "active",
    }

    def test_form_registration_is_published_and_schema_valid(self, integration_db):
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_roundtrip")
            principal = PrincipalFactory(tenant=tenant)
            env._commit_factory_data()
            token = principal.access_token

        admin = make_super_admin_client()
        with (
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
            # The record's SSRF check resolves DNS; stub the resolution (not the
            # check) so production's real guard still runs, against a public IP.
            patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"),
        ):
            posted = admin.post("/tenant/tmp_roundtrip/tmp-providers/add", data=self._FORM, follow_redirects=False)
        # A rejected form redirects back to /add; a successful one to the list.
        assert posted.status_code == 302, posted.data
        assert "/add" not in posted.headers["Location"], "the form rejected a valid registration"

        response = _get_discovery("tmp_roundtrip", token)

        assert response.status_code == 200, response.text
        providers = response.json()["providers"]
        assert len(providers) == 1, f"expected the registered provider on the wire, got {providers}"
        entry = providers[0]

        # The whole point: what the admin layer wrote is what the contract can
        # publish, graded by the schema itself.
        validate_against_pinned_schema(PROVIDER_REGISTRATION_SCHEMA, entry)

        # Values the form owns, as they must appear on the machine wire. Whitespace
        # around the CSV entries is form shape (the blueprint strips it); the
        # ``^[A-Z]{2}$`` charset is the record's rule, which is why the input here
        # is already uppercase — dropping the blueprint's `upper=True` moved
        # normalization out of the form, so a lowercase code is now REJECTED rather
        # than silently corrected (asserted below).
        assert entry["countries"] == ["US", "DE"]
        assert entry["uid_types"] == ["uid2", "id5"]
        assert entry["timeout_ms"] == 250
        assert entry["priority"] == 3
        assert entry["identity_match"] is True
        # `name` is admin-only and must never cross to the machine wire.
        assert "name" not in entry

    def test_lowercase_country_is_rejected_not_normalized(self, integration_db):
        """The blueprint no longer uppercases, so the record's charset rule is what applies.

        Round 18 moved the ``^[A-Z]{2}$`` rule onto the record and dropped the
        form helper's ``upper=True``, which was the only thing anywhere moving a
        country code toward the pattern — a domain rule stranded in a form helper
        that the second write surface inherited nothing of. The consequence is
        visible here: a lowercase code is rejected with a message naming the field,
        the index and the value, rather than silently corrected.
        """
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_roundtrip_lower")
            PrincipalFactory(tenant=tenant)
            env._commit_factory_data()

        admin = make_super_admin_client()
        with (
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
            patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"),
        ):
            posted = admin.post(
                "/tenant/tmp_roundtrip_lower/tmp-providers/add",
                data={**self._FORM, "countries": "us, de"},
                follow_redirects=True,
            )

        assert posted.status_code == 200
        assert b"countries[0]" in posted.data, "the flash must name which CSV entry was rejected"

    def test_form_rejection_publishes_nothing(self, integration_db):
        """The falsifiable half: a rejected form leaves the contract empty.

        Without this, "the write reaches the wire" could pass on a fixture row
        rather than on the row the form actually created.
        """
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_roundtrip_bad")
            principal = PrincipalFactory(tenant=tenant)
            env._commit_factory_data()
            token = principal.access_token

        admin = make_super_admin_client()
        with (
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
            patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"),
        ):
            # identity_match with no countries — rejected by the record.
            posted = admin.post(
                "/tenant/tmp_roundtrip_bad/tmp-providers/add",
                data={**self._FORM, "countries": ""},
                follow_redirects=False,
            )
        assert posted.status_code == 302
        assert "/add" in posted.headers["Location"], "an invalid registration must bounce back to the form"

        response = _get_discovery("tmp_roundtrip_bad", token)

        assert response.status_code == 200, response.text
        assert response.json()["providers"] == []


class TestDiscoveryAuth:
    """The contract authenticates a TENANT's credential, not a process.

    ``_require_tenant_credential`` resolves the caller's token *inside* the
    tenant in the path, so a cross-tenant read is inexpressible rather than
    rejected by a comparison. Every case here goes through ``src.app.app``, so
    the 401 body is the one the deployed endpoint emits — the previous suite ran
    its denial cases against a test-local ``FastAPI()`` with a copied error
    handler and only ever hit the production app on the 200 path (#1197 review).
    """

    @staticmethod
    def _two_tenants(env) -> tuple[str, str, str, str]:
        """Seed two tenants, each with a principal. Returns both (tenant_id, token) pairs."""
        own = TenantFactory(tenant_id="tmp_int_auth_own")
        other = TenantFactory(tenant_id="tmp_int_auth_other")
        own_principal = PrincipalFactory(tenant=own)
        other_principal = PrincipalFactory(tenant=other)
        TMPProviderFactory(tenant=own, name="Own Provider", status="active")
        env._commit_factory_data()
        return own.tenant_id, own_principal.access_token, other.tenant_id, other_principal.access_token

    def test_no_credential_is_rejected(self, integration_db):
        """An unauthenticated poll gets the production AUTH_REQUIRED envelope."""
        with _TMPEnv() as env:
            own_id, _own_token, _other_id, _other_token = self._two_tenants(env)

        response = _get_discovery(own_id, None)

        assert response.status_code == 401
        assert_envelope_shape(response.json(), "AUTH_REQUIRED", recovery="correctable")

    def test_another_tenants_credential_cannot_read_this_tenant(self, integration_db):
        """The isolation case: a valid credential from tenant B is nothing on tenant A's path.

        This is the finding the process-global API key made unexpressible — one
        key authorized reads of every tenant's provider topology.
        """
        with _TMPEnv() as env:
            own_id, _own_token, _other_id, other_token = self._two_tenants(env)

        response = _get_discovery(own_id, other_token)

        assert response.status_code == 401, response.text
        assert_envelope_shape(response.json(), "AUTH_REQUIRED", recovery="correctable")

    def test_unknown_token_is_rejected(self, integration_db):
        """A token that belongs to nobody is rejected, not treated as anonymous access."""
        with _TMPEnv() as env:
            own_id, _own_token, _other_id, _other_token = self._two_tenants(env)

        response = _get_discovery(own_id, "not-a-real-token")

        assert response.status_code == 401
        assert_envelope_shape(response.json(), "AUTH_REQUIRED", recovery="correctable")

    def test_tenants_own_principal_token_is_accepted(self, integration_db):
        """The credential the operator issues the router is the one that works."""
        with _TMPEnv() as env:
            own_id, own_token, _other_id, _other_token = self._two_tenants(env)

        response = _get_discovery(own_id, own_token)

        assert response.status_code == 200, response.text
        assert len(response.json()["providers"]) == 1

    def test_tenants_admin_token_is_accepted(self, integration_db):
        """Bearer transport + the tenant's admin token — the other credential shape.

        Covers both halves the route no longer implements itself: token
        extraction is ``UnifiedAuthMiddleware``'s (``Authorization: Bearer``),
        and the accepted credential set is ``get_principal_from_token``'s.
        """
        with _TMPEnv() as env:
            tenant = TenantFactory(tenant_id="tmp_int_auth_admin", admin_token="tmp-admin-token-1")
            TMPProviderFactory(tenant=tenant, name="Admin-visible Provider", status="active")
            env._commit_factory_data()
            tenant_id = tenant.tenant_id

        response = _discovery_client().get(
            f"/tenant/{tenant_id}/tmp-providers/discovery",
            headers={"Authorization": "Bearer tmp-admin-token-1"},
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["providers"]) == 1

    def test_unknown_tenant_is_not_found_for_an_authenticated_caller(self, integration_db):
        """A credential cannot resolve in a tenant that does not exist → 401, never 404.

        Pins the ordering: the gate runs before the route body, so a caller
        cannot probe which tenant ids exist by reading the difference between
        401 and 404.
        """
        with _TMPEnv() as env:
            _own_id, own_token, _other_id, _other_token = self._two_tenants(env)

        response = _get_discovery("tmp_int_auth_missing", own_token)

        assert response.status_code == 401
        assert_envelope_shape(response.json(), "AUTH_REQUIRED", recovery="correctable")


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

        mock_client = make_mock_http_client(200)
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
# 4. The agent declares the experimental surface it implements
# ---------------------------------------------------------------------------
#
# Moved to a BDD scenario: tests/bdd/features/local-tmp-capability-declaration.feature,
# bound by tests/bdd/test_tmp_capability_declaration.py.
#
# What lived here was a @parametrize("transport", [MCP, A2A, REST]) with its own
# envelope extraction (`wire.get("experimental_features")`). That hand-written
# transport list could not include e2e_rest — the fan-out invariant names that
# shape specifically — and it restated an extraction the wire helpers own. The
# scenario grades the same obligation plus the two the parametrize could not
# express (draining counts, inactive-only does not), on every transport the
# harness fans out to (#1197 review).
