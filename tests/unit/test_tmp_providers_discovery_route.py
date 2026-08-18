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
- uow.tenant_config is None → 500 (not an assert)
- TMPProvider.to_discovery_dict() / to_admin_dict() serialize their own contracts,
  the discovery half graded against the pinned schema itself

Every request here goes through the PRODUCTION app (``src.app.app``) — its
router mount, its middleware stack and its ``AdCPError`` handler — rather than a
test-local ``FastAPI()`` with a re-declared handler.  A restated handler can only
detect key-name drift in its own copy; the production one is the thing the
contract is served by.  The credential gate is the one dependency overridden,
because resolving a credential needs a real tenant + principal row: the auth
matrix (missing / cross-tenant / principal / admin token) is graded for real
against the same app in ``tests/integration/test_tmp_provider_integration.py``
(#1197 review).
"""

from unittest.mock import MagicMock, patch

import pytest
from adcp.types.generated_poc.trusted_match.provider_registration import TmpProviderRegistration
from fastapi.testclient import TestClient
from sqlalchemy.orm.exc import DetachedInstanceError

from src.core.database.models import TMPProvider
from src.routes.tmp_providers import PROVIDER_REGISTRATION_SCHEMA
from tests.helpers.envelope_assertions import assert_envelope_shape
from tests.helpers.pinned_schema import validate_against_pinned_schema
from tests.unit._tmp_helpers import _make_provider, _make_tmp_uow, _mock_cm

# A property RID is `format: uuid` in the pinned schema, so test fixtures must
# use a real UUID rather than a readable placeholder.
_RID = "0192f3c4-5d6e-7f80-9a1b-2c3d4e5f6071"


def _make_tenant(tenant_id="si-host"):
    t = MagicMock()
    t.tenant_id = tenant_id
    t.name = "SI Host Tenant"
    return t


_STUB_PRINCIPAL = "tmp-router-principal"


@pytest.fixture
def client():
    """A TestClient over the PRODUCTION app with the credential gate stubbed.

    ``src.app.app`` brings its own router mount, middleware stack and
    ``AdCPError`` handler, so the envelopes these tests assert on are the ones
    the deployed endpoint emits — deleting the production handler breaks them.
    Only :func:`require_tenant_credential` is overridden: resolving a credential
    reads tenant + principal rows, which is an integration concern, and the auth
    matrix itself is graded against this same app with a real DB in
    ``tests/integration/test_tmp_provider_integration.py`` (#1197 review).
    """
    from src.app import app
    from src.routes.tmp_providers import require_tenant_credential

    app.dependency_overrides[require_tenant_credential] = lambda: _STUB_PRINCIPAL
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(require_tenant_credential, None)


class TestDiscoveryReturnsActiveProviders:
    """GET /tenant/{tenant_id}/tmp-providers/discovery returns active + draining providers."""

    def test_returns_two_active_providers(self, client):
        """Two active providers are returned in the response via repository.list_syncable()."""
        tenant = _make_tenant()
        providers = [
            _make_provider(provider_id="prov_1", name="Provider A", priority=0, countries=["US"]),
            _make_provider(provider_id="prov_2", name="Provider B", priority=1, uid_types=["uid2"]),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "si-host"
        assert len(data["providers"]) == 2
        assert data["providers"][0]["provider_id"] == "prov_1"
        assert data["providers"][0]["countries"] == ["US"]
        assert data["providers"][1]["provider_id"] == "prov_2"
        assert data["providers"][1]["uid_types"] == ["uid2"]
        mock_tmp_uow_cls.return_value.__enter__.return_value.tmp_providers.list_syncable.assert_called_once_with()

    def test_includes_draining_providers(self, client):
        """Draining providers are included (router stops new requests but in-flight complete)."""
        tenant = _make_tenant()
        providers = [
            _make_provider(provider_id="prov_1", status="active"),
            _make_provider(provider_id="prov_2", status="draining"),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
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
            _make_provider(provider_id="prov_a", name="Alpha", priority=0),
            _make_provider(provider_id="prov_b", name="Beta", priority=0),
            _make_provider(provider_id="prov_c", name="Gamma", priority=1),
        ]

        mock_tmp_uow_cls = _make_tmp_uow(providers, tenant=tenant)

        with patch("src.routes.tmp_providers.TMPProviderUoW", mock_tmp_uow_cls):
            response = client.get("/tenant/si-host/tmp-providers/discovery")

        assert response.status_code == 200
        provider_ids = [p["provider_id"] for p in response.json()["providers"]]
        assert provider_ids == ["prov_a", "prov_b", "prov_c"]


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
        p = _make_provider(countries=["US", "GB"], uid_types=["uid2"], properties=[_RID])
        result = p.to_discovery_dict()
        assert result["countries"] == ["US", "GB"]
        assert result["uid_types"] == ["uid2"]
        assert result["properties"] == [_RID]

    def test_discovery_dict_validates_against_the_pinned_schema(self):
        """A fully-populated entry validates against ``provider-registration.json`` itself.

        The grader is the pinned schema, not a hand-written restatement of it:
        the previous ``set(result) == {...}`` key list could only detect
        key-name drift, which is why a hyphenated ``provider_id`` violating
        ``^[A-Za-z0-9_]+$`` shipped on this wire past eighteen review rounds
        (#1197 review).  ``validate_against_pinned_schema`` reads the file named
        by the route module's own citation, so a citation pointing at a
        non-existent path fails here too.
        """
        p = _make_provider(
            provider_id="prov_test",
            name="Test Provider",
            endpoint="https://example.com",
            context_match=False,
            identity_match=True,
            countries=["DE"],
            uid_types=["id5"],
            properties=[_RID],
            timeout_ms=300,
            priority=2,
            status="draining",
        )
        result = p.to_discovery_dict()

        validate_against_pinned_schema(PROVIDER_REGISTRATION_SCHEMA, result)
        # Cross-check against the SDK codegen of the same schema: it is the type
        # a router built on the pinned SDK deserializes this entry into.
        TmpProviderRegistration.model_validate(result)

        # `name` is the one admin-side key that must never reach this wire; the
        # closed schema (additionalProperties: false) makes it an error, and
        # asserting it here names the specific regression.
        assert "name" not in result

    def test_discovery_dict_omitting_conditionals_still_validates(self):
        """A context-only provider — no countries/uid_types/properties — is schema-valid.

        The omit-don't-null rule and the schema's ``anyOf`` are graded together
        on the same instrument as the populated case.
        """
        p = _make_provider(
            endpoint="https://ctx.example.com/tmp",
            context_match=True,
            identity_match=False,
            countries=None,
            uid_types=None,
            properties=None,
        )
        result = p.to_discovery_dict()
        validate_against_pinned_schema(PROVIDER_REGISTRATION_SCHEMA, result)
        # https, unlike the module-level default: the SDK codegen adds an
        # https-only validator on top of the schema's `format: uri`, and this
        # record deliberately allows http://*.localhost for local development
        # (see src/core/schemas/tmp_provider.py). The schema is the authority;
        # the codegen is the cross-check, so the cross-check gets an https URL.
        TmpProviderRegistration.model_validate(result)

    def test_hyphenated_provider_id_is_rejected_by_the_schema(self):
        """The canonical-UUID form this column used to emit fails the schema.

        Pins the reason ``provider_id`` is ``uuid4().hex`` rather than a Postgres
        ``uuid``: reverting the column type puts a ``-`` back in the value, and
        ``^[A-Za-z0-9_]+$`` rejects it.  Without this, the migration looks like
        cosmetics.
        """
        p = _make_provider(provider_id="5f1c0e3a-9b7d-4e8f-a1c2-b3d4e5f60718")
        with pytest.raises(AssertionError):
            validate_against_pinned_schema(PROVIDER_REGISTRATION_SCHEMA, p.to_discovery_dict())

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
