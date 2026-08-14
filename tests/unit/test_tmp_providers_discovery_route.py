"""Unit tests for the FastAPI TMP provider discovery route and TMPProvider model.

Tests the endpoint:
    GET /tenant/{tenant_id}/tmp-providers/discovery

This is the FastAPI route in src/routes/tmp_providers.py — the canonical
machine-to-machine discovery endpoint polled by the TMP Router every 30 s.

Covers:
- Returns active + draining providers via repository.list_syncable()
- Returns 404 for unknown tenant
- Returns empty list when tenant has no active providers
- Response shape matches TMP Router contract
- Providers ordered by priority ASC, name ASC
- Handles legacy rows with null countries/uid_types
- Fail-closed auth: unset/empty TMP_DISCOVERY_API_KEYS → 500 (CONFIGURATION_ERROR)
- Explicit opt-out: TMP_DISCOVERY_API_KEYS=OPEN disables auth
- uow.tenant_config is None → 500 (not an assert)
- TMPProvider.to_discovery_dict() / to_admin_dict() serialize their own contracts
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm.exc import DetachedInstanceError

from src.core.database.models import TMPProvider
from tests.helpers.envelope_assertions import assert_envelope_shape
from tests.unit._tmp_helpers import _make_provider, _make_tmp_uow, _mock_cm


def _make_tenant(tenant_id="si-host"):
    t = MagicMock()
    t.tenant_id = tenant_id
    t.name = "SI Host Tenant"
    return t


@pytest.fixture
def client():
    """Create a FastAPI TestClient with the tmp_providers router and AdCPError handler mounted.

    The handler mirrors the production handler in src/app.py exactly:
    ``build_two_layer_error_envelope(exc)`` is returned at the top level (no
    ``"detail"`` wrapper).  Tests assert via ``assert_envelope_shape()`` so
    that deleting this handler from the production app would break the tests.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from src.core.exceptions import AdCPError, build_two_layer_error_envelope
    from src.routes.tmp_providers import router

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AdCPError)
    async def adcp_error_handler(request: Request, exc: AdCPError) -> JSONResponse:
        # Matches src/app.py adcp_error_handler exactly — envelope at top level.
        return JSONResponse(
            status_code=exc.status_code,
            content=build_two_layer_error_envelope(exc),
        )

    return TestClient(app, raise_server_exceptions=False)


class TestDiscoveryReturnsActiveProviders:
    """GET /tenant/{tenant_id}/tmp-providers/discovery returns active + draining providers."""

    def test_returns_two_active_providers(self, client):
        """Two active providers are returned in the response via repository.list_syncable()."""
        tenant = _make_tenant()
        providers = [
            _make_provider(provider_id="uuid-1", name="Provider A", priority=0, countries=["US"]),
            _make_provider(provider_id="uuid-2", name="Provider B", priority=1, uid_types=["uid2"]),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "si-host"
        assert len(data["providers"]) == 2
        assert data["providers"][0]["provider_id"] == "uuid-1"
        assert data["providers"][0]["countries"] == ["US"]
        assert data["providers"][1]["provider_id"] == "uuid-2"
        assert data["providers"][1]["uid_types"] == ["uid2"]
        mock_tmp_uow_cls.return_value.__enter__.return_value.tmp_providers.list_syncable.assert_called_once_with()

    def test_includes_draining_providers(self, client):
        """Draining providers are included (router stops new requests but in-flight complete)."""
        tenant = _make_tenant()
        providers = [
            _make_provider(provider_id="uuid-1", status="active"),
            _make_provider(provider_id="uuid-2", status="draining"),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert len(data["providers"]) == 2
        statuses = {p["status"] for p in data["providers"]}
        assert statuses == {"active", "draining"}


class TestDiscoveryTenantNotFound:
    """GET /tenant/{tenant_id}/tmp-providers/discovery returns 404 for unknown tenant."""

    def test_returns_404_for_unknown_tenant(self, client):
        """Unknown tenant_id returns 404 so the router can distinguish from 'no providers'."""
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=None)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/nonexistent/tmp-providers/discovery")

        assert response.status_code == 404
        envelope = response.json()
        assert_envelope_shape(envelope, "ACCOUNT_NOT_FOUND", recovery="terminal", message_substr="not found")
        assert envelope["errors"][0]["suggestion"] == "Provide a valid tenant ID."


class TestDiscoveryEmptyProviders:
    """GET /tenant/{tenant_id}/tmp-providers/discovery returns empty list when no providers."""

    def test_returns_empty_providers_list(self, client):
        """Valid tenant with no active providers returns empty providers array."""
        tenant = _make_tenant()

        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "si-host"
        assert data["providers"] == []


class TestDiscoveryResponseShape:
    """Response shape matches the TMP Router contract."""

    def test_response_contains_all_required_fields(self, client):
        """Each provider entry contains all fields the TMP Router expects."""
        tenant = _make_tenant()
        providers = [
            _make_provider(
                countries=["US", "GB"],
                uid_types=["publisher_first_party", "uid2"],
            ),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        entry = response.json()["providers"][0]

        # The closed key set of provider-registration.json — asserted as
        # EQUALITY, not a subset: additionalProperties is false, so an extra key
        # (e.g. re-adding the admin-only `name`) is a schema violation a subset
        # check would wave through.
        assert set(entry) == {
            "provider_id",
            "endpoint",
            "context_match",
            "identity_match",
            "countries",
            "uid_types",
            "timeout_ms",
            "priority",
            "status",
        }

    def test_name_is_not_on_the_machine_wire(self, client):
        """`name` is not in the closed schema, so the discovery wire must not carry it.

        It stays on the admin serialization (``to_admin_dict``) — see
        ``TestTMPProviderSerializers``.  The TMP Router uses ``name`` only as a
        fallback identifier when ``provider_id`` is empty, and this endpoint
        always emits ``provider_id``.
        """
        tenant = _make_tenant()
        providers = [_make_provider(name="Admin Only Label")]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        assert "name" not in response.json()["providers"][0]

    def test_absent_countries_uid_types_are_omitted_not_null(self, client):
        """Rows that restrict nothing omit the conditional arrays — `null` is a type violation.

        ``provider-registration.json`` types ``countries``/``uid_types``/
        ``properties`` as ``array`` with ``minItems: 1``, so ``null`` is not a
        permitted value and a strictly-validating router rejects the body.
        Omission is how the schema spells "no restriction" (#1197 review).
        """
        tenant = _make_tenant()
        providers = [
            _make_provider(countries=None, uid_types=None, properties=None),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        entry = response.json()["providers"][0]
        assert "countries" not in entry
        assert "uid_types" not in entry
        assert "properties" not in entry


class TestDiscoveryOrdering:
    """Providers are ordered by priority ASC, name ASC."""

    def test_providers_ordered_by_priority_then_name(self, client):
        """The repository's priority ASC, name ASC order survives to the wire.

        Asserted on ``provider_id`` rather than ``name``: the ordering key is
        the repository's, but ``name`` is admin-only and not on this wire, so
        each row's id stands in for it (ids are assigned to match the expected
        name order).
        """
        tenant = _make_tenant()
        # Simulate DB returning in correct order (priority 0 before 1, alpha within same priority)
        providers = [
            _make_provider(provider_id="uuid-a", name="Alpha", priority=0),
            _make_provider(provider_id="uuid-b", name="Beta", priority=0),
            _make_provider(provider_id="uuid-c", name="Gamma", priority=1),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        provider_ids = [p["provider_id"] for p in response.json()["providers"]]
        assert provider_ids == ["uuid-a", "uuid-b", "uuid-c"]


# ---------------------------------------------------------------------------
# TMP_DISCOVERY_API_KEYS gating tests
# ---------------------------------------------------------------------------


class TestDiscoveryApiKeyAuth:
    """GET /tenant/{tenant_id}/tmp-providers/discovery enforces TMP_DISCOVERY_API_KEYS."""

    def test_returns_500_when_tmp_discovery_api_keys_not_set(self, client):
        """When TMP_DISCOVERY_API_KEYS is unset the endpoint returns 500 (fail-closed, operator must act).

        AdCPConfigurationError is the right error here: the operator has to configure
        the env var; the buyer cannot recover this themselves.  On the wire this maps
        to code=CONFIGURATION_ERROR with recovery="terminal", which is self-consistent
        against the pinned ``enums/error-code.json``: 3.1.1 classifies
        CONFIGURATION_ERROR as terminal (SERVICE_UNAVAILABLE is the transient code).

        This pairing was previously SERVICE_UNAVAILABLE + terminal — flagged in review
        as self-inconsistent and deferred as a follow-up. Moving to the adcp 6.6.0 /
        spec 3.1.1 pin resolved it: the SDK now emits CONFIGURATION_ERROR for this
        exception, so code and recovery agree with the enum and no follow-up remains.
        """
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("TMP_DISCOVERY_API_KEYS", None)
            response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 500
        # AdCPConfigurationError maps to CONFIGURATION_ERROR + terminal under the
        # adcp 6.6.0 / spec 3.1.1 pin — code and recovery agree with
        # enums/error-code.json (see docstring for the pre-3.1.1 history).
        envelope = response.json()
        assert_envelope_shape(envelope, "CONFIGURATION_ERROR", recovery="terminal")
        # Every raise on this route carries an actionable suggestion; the operator
        # is the actor here, so the hint names the env var they must set.
        assert "TMP_DISCOVERY_API_KEYS" in envelope["errors"][0]["suggestion"]

    def test_returns_500_when_tmp_discovery_api_keys_is_empty_string(self, client):
        """When TMP_DISCOVERY_API_KEYS is set to empty string the endpoint returns 500 (fail-closed).

        Same as unset: AdCPConfigurationError (500, terminal) — operator must act.
        """
        with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": ""}):
            response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 500
        # Same wire shape as the unset case: CONFIGURATION_ERROR + terminal under the
        # 3.1.1 pin — see test_returns_500_when_tmp_discovery_api_keys_not_set.
        assert_envelope_shape(response.json(), "CONFIGURATION_ERROR", recovery="terminal")

    def test_open_when_tmp_discovery_api_keys_is_open(self, client):
        """When TMP_DISCOVERY_API_KEYS=OPEN the endpoint is accessible without a key."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200

    def test_open_mode_is_case_insensitive(self, client):
        """TMP_DISCOVERY_API_KEYS=open (lowercase) also disables auth."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "open"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200

    def test_returns_401_when_no_key_provided_and_keys_configured(self, client):
        """When TMP_DISCOVERY_API_KEYS is set and no key is sent, returns 401 with suggestion."""
        with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "secret-key-1,secret-key-2"}):
            response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 401
        envelope = response.json()
        # AUTH_REQUIRED + correctable is the spec-grounded pairing under the
        # adcp 6.6.0 / spec 3.1.1 pin: enums/error-code.json enumMetadata gives
        # AUTH_REQUIRED {"recovery": "correctable"} ("provide credentials when
        # missing"). The previous assertion pinned AUTH_TOKEN_INVALID + terminal
        # and was deferred in review as an agreed follow-up; 3.1.1 removed
        # AUTH_TOKEN_INVALID from the vocabulary entirely, so the bump closed it.
        assert_envelope_shape(envelope, "AUTH_REQUIRED", recovery="correctable")
        assert (
            envelope["errors"][0]["suggestion"]
            == "Provide a valid API key via x-adcp-auth, X-API-Key, or Authorization: Bearer <key>."
        )

    def test_returns_401_when_wrong_key_provided(self, client):
        """When TMP_DISCOVERY_API_KEYS is set and a wrong key is sent, returns 401 with suggestion."""
        with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "correct-key"}):
            response = client.get(
                "/tenant/si-host/tmp-providers/discovery",
                headers={"x-adcp-auth": "wrong-key"},
            )

        assert response.status_code == 401
        envelope = response.json()
        # Same pairing as the missing-key case — see
        # test_returns_401_when_no_key_provided_and_keys_configured.
        #
        # Worth noting for the follow-up: 3.1.1 also added AUTH_MISSING
        # (correctable) and AUTH_INVALID (terminal, "credentials were rejected,
        # do NOT auto-retry"), which distinguish this case from the one above.
        # Both 401 paths currently emit AUTH_REQUIRED because they share
        # AdCPAuthenticationError with every other auth boundary in the app;
        # splitting them is an app-wide error-code change, not a TMP one.
        assert_envelope_shape(envelope, "AUTH_REQUIRED", recovery="correctable")
        assert (
            envelope["errors"][0]["suggestion"]
            == "Provide a valid API key via x-adcp-auth, X-API-Key, or Authorization: Bearer <key>."
        )

    def test_accepts_valid_key_via_x_adcp_auth_header(self, client):
        """Valid key in x-adcp-auth header is accepted."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "valid-key"}):
                response = client.get(
                    "/tenant/si-host/tmp-providers/discovery",
                    headers={"x-adcp-auth": "valid-key"},
                )

        assert response.status_code == 200

    def test_accepts_valid_key_via_x_api_key_header(self, client):
        """Valid key in X-API-Key header is accepted."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "valid-key"}):
                response = client.get(
                    "/tenant/si-host/tmp-providers/discovery",
                    headers={"X-API-Key": "valid-key"},
                )

        assert response.status_code == 200

    def test_accepts_valid_key_via_authorization_bearer_header(self, client):
        """Valid key in Authorization: Bearer header is accepted."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "valid-key"}):
                response = client.get(
                    "/tenant/si-host/tmp-providers/discovery",
                    headers={"Authorization": "Bearer valid-key"},
                )

        assert response.status_code == 200

    def test_accepts_one_of_multiple_configured_keys(self, client):
        """Any key from the comma-separated TMP_DISCOVERY_API_KEYS list is accepted."""
        tenant = _make_tenant()
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "key-a,key-b,key-c"}):
                response = client.get(
                    "/tenant/si-host/tmp-providers/discovery",
                    headers={"x-adcp-auth": "key-b"},
                )

        assert response.status_code == 200

    def test_returns_401_for_non_ascii_key(self):
        """A header value with non-ASCII bytes returns 401, not 500.

        Starlette decodes header bytes as latin-1, so any byte > 0x7F yields a
        non-ASCII str.  secrets.compare_digest raises TypeError for non-ASCII
        strings — the endpoint must catch this and return a clean 401 instead
        of a 500 that mislabels an auth failure as a server error.

        The httpx2 test client rejects non-ASCII header values before they
        reach the server, so we call require_api_key() directly with a mock
        Request that carries the non-ASCII header — this exercises the exact
        production code path that Starlette would trigger in production.
        """
        import asyncio

        import pytest

        from src.core.exceptions import AdCPAuthRequiredError
        from src.routes.tmp_providers import require_api_key

        # Starlette decodes header bytes as latin-1; byte 0xFF → '\xff' in str
        mock_request = MagicMock()
        mock_request.headers.get = lambda key, default="": "caf\xff" if key == "x-adcp-auth" else default

        with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "valid-key"}):
            with pytest.raises(AdCPAuthRequiredError):
                asyncio.run(require_api_key(mock_request))


# ---------------------------------------------------------------------------
# uow.tenant_config is None guard (replaces the old assert)
# ---------------------------------------------------------------------------


class TestDiscoveryRepositoryUnavailable:
    """Both UoW repository guards emit the typed 503 envelope — never a bare ``assert``.

    ``assert uow.<repo> is not None`` is stripped by ``python -O``, and when it
    does fire it raises ``AssertionError`` → an un-enveloped 500 rather than the
    typed AdCP envelope this endpoint's contract promises.  These two tests are
    siblings on purpose: the ``tenant_config`` guard had a test, the parallel
    ``tmp_providers`` guard eight lines down did not (#1197 review).
    """

    @staticmethod
    def _uow_cls_with(*, tenant_config, tmp_providers) -> MagicMock:
        """A UoW class whose yielded UoW exposes the two repositories as given.

        Unlike ``_make_tmp_uow``, either repository may be ``None`` — that is
        the condition under test. The context-manager plumbing itself is
        ``_mock_cm``'s job, not this factory's.
        """
        mock_uow = MagicMock()
        mock_uow.tenant_config = tenant_config
        mock_uow.tmp_providers = tmp_providers
        return _mock_cm(mock_uow)

    def test_returns_503_when_tenant_config_is_none(self, client):
        """If TMPProviderUoW yields uow.tenant_config=None the endpoint returns 503 (service unavailable).

        AdCPServiceUnavailableError (503, transient) is the right error here: the
        repository layer is temporarily unavailable; the buyer should retry.
        """
        mock_uow_cls = self._uow_cls_with(tenant_config=None, tmp_providers=MagicMock())

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 503
        # AdCPServiceUnavailableError: recovery=transient (buyer should retry)
        envelope = response.json()
        assert_envelope_shape(
            envelope,
            "SERVICE_UNAVAILABLE",
            recovery="transient",
            message_substr="Tenant config repository unavailable",
        )
        assert "Retry shortly" in envelope["errors"][0]["suggestion"]

    def test_returns_503_when_tmp_providers_is_none(self, client):
        """If TMPProviderUoW yields uow.tmp_providers=None the endpoint returns the same typed 503.

        Mutation this pins: reverting the guard to ``assert uow.tmp_providers is
        not None`` produces an ``AssertionError`` → status 500 with no AdCP
        envelope, failing both the status and the envelope assertion below.
        The tenant_config repo is present so this test isolates the second guard.
        """
        tenant_config = MagicMock()
        tenant_config.get_tenant.return_value = _make_tenant()
        mock_uow_cls = self._uow_cls_with(tenant_config=tenant_config, tmp_providers=None)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 503
        envelope = response.json()
        assert_envelope_shape(
            envelope,
            "SERVICE_UNAVAILABLE",
            recovery="transient",
            message_substr="TMP provider repository unavailable",
        )
        assert "Retry shortly" in envelope["errors"][0]["suggestion"]


# ---------------------------------------------------------------------------
# Single-transaction + no-DetachedInstance regression tests
# ---------------------------------------------------------------------------


class TestDiscoverySingleTransactionAndNoDetachedInstance:
    """Regression tests proving the route uses ONE UoW and calls to_dict() inside it.

    Round 11 review fix: the route was refactored from two separate UoW blocks
    (TenantConfigUoW then TMPProviderUoW) to a single TMPProviderUoW block.
    These tests prove:
    1. TMPProviderUoW is constructed exactly once (not twice).
    2. provider.to_dict() is called BEFORE the UoW exits — calling it after
       would raise DetachedInstanceError under real SQLAlchemy
       (expire_on_commit=True is the default).
    """

    class _DetachAfterCloseProvider:
        """Fake provider whose to_discovery_dict() raises DetachedInstanceError once the UoW closed."""

        def __init__(self, closed_flag: list[bool]):
            self._closed_flag = closed_flag

        def _check(self):
            if self._closed_flag[0]:
                raise DetachedInstanceError("Instance is not bound to a Session; attribute access failed")

        def to_discovery_dict(self) -> dict:
            self._check()
            return {
                "provider_id": "fake-uuid",
                "endpoint": "http://fake:3000",
                "context_match": True,
                "identity_match": True,
                "timeout_ms": 200,
                "priority": 0,
                "status": "active",
            }

    def test_tmp_provider_uow_constructed_exactly_once(self, client):
        """TMPProviderUoW is instantiated exactly once — not twice (no separate TenantConfigUoW)."""
        mock_tmp_uow_cls = _make_tmp_uow([], tenant=_make_tenant())

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        # The class must have been called (constructed) exactly once.
        mock_tmp_uow_cls.assert_called_once_with("si-host")

    def test_to_dict_called_before_uow_exits(self, client):
        """provider.to_discovery_dict() is called inside the UoW block, not after it closes.

        Uses a fake provider whose to_discovery_dict() raises
        DetachedInstanceError once the UoW __exit__ sets a closed_flag. If the
        route serializes after the block exits, the request would 500; if it
        serializes inside, it succeeds.
        """
        closed_flag = [False]
        provider = self._DetachAfterCloseProvider(closed_flag)

        mock_uow = MagicMock()
        mock_uow.tmp_providers = MagicMock()
        mock_uow.tmp_providers.list_syncable.return_value = [provider]
        mock_uow.tenant_config = MagicMock()
        mock_uow.tenant_config.get_tenant.return_value = _make_tenant()

        def _mark_closed(*_args):
            closed_flag[0] = True
            return False

        mock_uow_cls = _mock_cm(mock_uow, on_exit=_mark_closed)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                # Would raise DetachedInstanceError (→ 500) if to_dict() ran after __exit__.
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert len(data["providers"]) == 1
        assert data["providers"][0]["provider_id"] == "fake-uuid"


# ---------------------------------------------------------------------------
# TMPProvider serializer unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestTMPProviderSerializers:
    """The two serializers carry two different contracts and must not converge.

    ``to_discovery_dict()`` is the machine wire — the closed key set of
    ``provider-registration.json``, absent conditionals omitted, no ``name``.
    ``to_admin_dict()`` is the Jinja-facing shape — ``name`` plus all three
    conditional keys always present (``None`` meaning "no restriction").

    These tests use real TMPProvider instances (no DB session) to ensure the
    production serialization contract is tested directly — not a MagicMock
    reimplementation that can silently diverge.
    """

    def test_discovery_dict_omits_absent_conditionals(self):
        """Absent countries/uid_types/properties are omitted — never emitted as null."""
        p = _make_provider(countries=None, uid_types=None, properties=None)
        result = p.to_discovery_dict()
        assert "countries" not in result
        assert "uid_types" not in result
        assert "properties" not in result

    def test_discovery_dict_includes_populated_conditionals(self):
        """Populated countries/uid_types/properties are carried through verbatim."""
        p = _make_provider(countries=["US", "GB"], uid_types=["uid2"], properties=["rid-1"])
        result = p.to_discovery_dict()
        assert result["countries"] == ["US", "GB"]
        assert result["uid_types"] == ["uid2"]
        assert result["properties"] == ["rid-1"]

    def test_discovery_dict_key_set_is_closed(self):
        """The emitted key set stays inside the closed schema — and excludes `name`."""
        p = _make_provider(
            provider_id="test-uuid",
            name="Test Provider",
            endpoint="http://example.com",
            context_match=False,
            identity_match=True,
            countries=["DE"],
            uid_types=["id5"],
            properties=["rid-2"],
            timeout_ms=300,
            priority=2,
            status="draining",
        )
        result = p.to_discovery_dict()
        assert set(result) == {
            "provider_id",
            "endpoint",
            "context_match",
            "identity_match",
            "countries",
            "uid_types",
            "properties",
            "timeout_ms",
            "priority",
            "status",
        }
        assert result["provider_id"] == "test-uuid"
        assert result["endpoint"] == "http://example.com"
        assert result["context_match"] is False
        assert result["identity_match"] is True
        assert result["timeout_ms"] == 300
        assert result["priority"] == 2
        assert result["status"] == "draining"

    def test_admin_dict_keeps_name_and_null_conditionals(self):
        """The admin shape carries `name` and all three conditional keys, `None` included.

        The edit template renders those three fields unconditionally, so the
        admin serialization must not adopt the wire's omission rule.
        """
        p = _make_provider(name="Test Provider", countries=None, uid_types=None, properties=None)
        result = p.to_admin_dict()
        assert result["name"] == "Test Provider"
        assert result["countries"] is None
        assert result["uid_types"] is None
        assert result["properties"] is None

    def test_discovery_endpoint_uses_the_wire_serializer(self, client):
        """The route serializes with to_discovery_dict(), not the admin shape."""
        tenant = _make_tenant()
        providers = [_make_provider(name="Admin Only Label", countries=None, uid_types=None, properties=None)]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            with patch.dict("os.environ", {"TMP_DISCOVERY_API_KEYS": "OPEN"}):
                response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        entry = response.json()["providers"][0]
        assert "name" not in entry
        assert "countries" not in entry
        assert "uid_types" not in entry
        assert "properties" not in entry


# ---------------------------------------------------------------------------
# TMPProvider.auth_credentials encryption round-trip and error contract
# ---------------------------------------------------------------------------


class TestTMPProviderAuthCredentials:
    """TMPProvider.auth_credentials encrypts on write and decrypts on read.

    The property must raise AdCPConfigurationError (not silently return
    plaintext) when decryption fails — a corrupted ciphertext, a key rotation,
    or a tampered row must surface as a hard error so the admin can act.
    """

    def test_round_trip_encrypt_decrypt(self):
        """Setting auth_credentials encrypts; reading it back decrypts to the original value."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with patch.dict("os.environ", {"ENCRYPTION_KEY": key}):
            p = TMPProvider()
            p.provider_id = "test-provider-id"
            p.auth_credentials = "super-secret-token"

            # The raw column must NOT be the plaintext value
            assert p._auth_credentials != "super-secret-token"
            assert p._auth_credentials is not None

            # Reading back through the property must return the original value
            assert p.auth_credentials == "super-secret-token"

    def test_none_value_returns_none(self):
        """Setting auth_credentials to None stores None and reads back as None."""
        p = TMPProvider()
        p.provider_id = "test-provider-id"
        p.auth_credentials = None
        assert p._auth_credentials is None
        assert p.auth_credentials is None

    def test_corrupted_ciphertext_raises_adcp_configuration_error(self):
        """A corrupted ciphertext raises AdCPConfigurationError, not a silent plaintext fallback."""
        from cryptography.fernet import Fernet

        from src.core.exceptions import AdCPConfigurationError

        key = Fernet.generate_key().decode()
        with patch.dict("os.environ", {"ENCRYPTION_KEY": key}):
            p = TMPProvider()
            p.provider_id = "test-provider-id"
            # Inject a corrupted ciphertext directly into the backing column
            p._auth_credentials = "not-a-valid-fernet-token"

            with pytest.raises(AdCPConfigurationError) as exc_info:
                _ = p.auth_credentials

        assert "test-provider-id" in str(exc_info.value)

    def test_empty_string_stores_none(self):
        """Setting auth_credentials to empty string stores None (treated as absent)."""
        p = TMPProvider()
        p.provider_id = "test-provider-id"
        p.auth_credentials = ""
        assert p._auth_credentials is None
        assert p.auth_credentials is None
