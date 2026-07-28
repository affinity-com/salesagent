"""Unit tests for the transport-agnostic TMP provider registration record.

``src/core/schemas/tmp_provider.py`` owns the AdCP provider-registration
invariants that used to live in the Flask admin blueprint.  These tests grade
the invariants where they now live, so any future write surface (MCP/A2A/REST
tool, bulk import) inherits graded rules rather than re-deriving them.

Covers:
- ``TMPProviderFields`` and ``TMPProviderRegistration`` cannot drift apart
- ``VALID_UID_TYPES`` / ``VALID_STATUSES`` track the pinned SDK enums *behaviourally*
  (every SDK value accepted, unknown values rejected) — not by re-asserting a literal
- Each invariant rejects with the operator-facing message the admin UI flashes
- ``to_fields`` / ``to_update_fields`` produce the repository's write record
- The shared rules agree with the SDK's own ``TmpProviderRegistration`` model
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from adcp.types.generated_poc.enums.uid_type import UidType
from adcp.types.generated_poc.trusted_match.provider_registration import Status as ProviderStatus
from pydantic import ValidationError

from src.core.schemas.tmp_provider import (
    VALID_STATUSES,
    VALID_UID_TYPES,
    TMPProviderFields,
    TMPProviderRegistration,
    TMPProviderValidationError,
)


def _rejection_message(**overrides) -> str:
    """Return the operator-facing message for a registration that must be rejected.

    Fails the test (rather than returning None) when the registration is
    *accepted* — an invariant that silently stopped firing must not read as a
    passing assertion on a message that was never produced.
    """
    with pytest.raises(TMPProviderValidationError) as excinfo:
        TMPProviderRegistration.parse(_fields(**overrides))
    return str(excinfo.value)


# Every test builds on a valid registration and mutates one thing, so a failure
# names the invariant under test rather than a setup mistake.
_VALID: TMPProviderFields = {
    "name": "Test Provider",
    "endpoint": "https://provider.example.com/tmp",
    "context_match": True,
    "identity_match": False,
    "countries": None,
    "uid_types": None,
    "properties": None,
    "timeout_ms": 50,
    "priority": 0,
    "status": "active",
    "auth_type": None,
    "auth_credentials": None,
}


def _fields(**overrides) -> TMPProviderFields:
    return {**_VALID, **overrides}  # type: ignore[typeddict-item]


@pytest.fixture(autouse=True)
def _resolvable_dns():
    """Make the SSRF check's DNS lookup deterministic (public IP) for every test.

    Patched at the ``url_validator`` module so production's real ``check_url_ssrf``
    runs — only the resolution result is stubbed.  Tests that need a *rejected*
    endpoint use a hostname the validator blocks before resolving.
    """
    with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
        yield


class TestFieldContract:
    """TMPProviderFields is the static mirror of the model's field set."""

    def test_typeddict_keys_match_model_fields(self):
        """Mutation this pins: adding a field to one and not the other.

        ``TMPProviderFields`` is what the repository write methods are typed
        against (``**Unpack[TMPProviderFields]``); if it drifts from the model,
        ``to_fields()`` starts emitting a key the repository's type rejects.
        """
        assert set(TMPProviderFields.__annotations__) == set(TMPProviderRegistration.model_fields)

    def test_to_fields_round_trips_through_the_model(self):
        assert TMPProviderRegistration.parse(_fields()).to_fields() == _VALID

    def test_to_update_fields_omits_credentials_when_not_included(self):
        """Leaving the credential field blank must not overwrite the stored value."""
        registration = TMPProviderRegistration.parse(_fields(auth_credentials="secret-token"))

        preserved = registration.to_update_fields(include_credentials=False)
        rotated = registration.to_update_fields(include_credentials=True)

        assert "auth_credentials" not in preserved
        assert rotated["auth_credentials"] == "secret-token"
        # Only that one key differs — nothing else is dropped by the omission.
        assert set(rotated) - set(preserved) == {"auth_credentials"}


class TestEnumsTrackThePinnedSdk:
    """The uid-type and status vocabularies come from the SDK, not a literal.

    Asserted behaviourally: a hand-written frozenset would pass a set-equality
    check against itself, so these drive the validator with each SDK value.
    """

    @pytest.mark.parametrize("uid_type", [t.value for t in UidType])
    def test_every_sdk_uid_type_is_accepted(self, uid_type: str):
        registration = TMPProviderRegistration.parse(
            _fields(identity_match=True, countries=["US"], uid_types=[uid_type])
        )

        assert registration.uid_types == [uid_type]

    def test_unknown_uid_type_is_rejected(self):
        message = _rejection_message(identity_match=True, countries=["US"], uid_types=["uid2", "not_a_uid_type"])

        assert message.startswith("Invalid uid_type(s):")
        assert "not_a_uid_type" in message

    @pytest.mark.parametrize("status", [s.value for s in ProviderStatus])
    def test_every_sdk_status_is_accepted(self, status: str):
        assert TMPProviderRegistration.parse(_fields(status=status)).status == status

    def test_unknown_status_is_rejected(self):
        assert (
            _rejection_message(status="paused") == "Invalid status 'paused'. Valid values: active, draining, inactive"
        )

    def test_vocabularies_are_derived_not_literal(self):
        """The module constants equal the SDK enums exactly (no local additions)."""
        assert VALID_UID_TYPES == frozenset(t.value for t in UidType)
        assert VALID_STATUSES == frozenset(s.value for s in ProviderStatus)


class TestRegistrationInvariants:
    """Each rule rejects with the operator-facing message the admin UI flashes."""

    def test_requires_a_name(self):
        assert _rejection_message(name="   ") == "Provider name is required"

    def test_requires_an_endpoint(self):
        assert _rejection_message(endpoint="") == "Endpoint URL is required"

    def test_requires_at_least_one_match_mode(self):
        assert (
            _rejection_message(context_match=False, identity_match=False)
            == "Provider must support at least one of context_match or identity_match"
        )

    def test_identity_match_requires_countries(self):
        message = _rejection_message(context_match=False, identity_match=True, countries=None, uid_types=["uid2"])
        assert message == "Countries are required when identity_match is enabled (ISO 3166-1 alpha-2 codes)"

    def test_identity_match_requires_uid_types(self):
        message = _rejection_message(context_match=False, identity_match=True, countries=["US"], uid_types=None)
        assert message == "UID types are required when identity_match is enabled (e.g. uid2, publisher_first_party)"

    def test_context_match_only_provider_needs_no_identity_dimensions(self):
        """The identity rules must not fire for a context-only provider."""
        registration = TMPProviderRegistration.parse(
            _fields(context_match=True, identity_match=False, countries=None, uid_types=None)
        )
        assert (registration.context_match, registration.identity_match) == (True, False)

    def test_ssrf_unsafe_endpoint_is_rejected(self):
        """The SSRF check runs inside the record, not only behind the admin form.

        Mutation this pins: dropping ``check_url_ssrf`` from the model would let
        an internal-network endpoint register from *any* write surface.
        """
        message = _rejection_message(endpoint="http://host.docker.internal:9999/tmp")

        assert message.startswith("Endpoint URL is not allowed:")
        assert "host.docker.internal" in message

    def test_direct_construction_raises_validation_error(self):
        """Programmatic write surfaces get an exception, not a message tuple."""
        with pytest.raises(ValidationError, match="at least one of context_match or identity_match"):
            TMPProviderRegistration(**_fields(context_match=False, identity_match=False))


class TestAgreesWithTheSdkModel:
    """The rules shared with the SDK's own registration model agree with it.

    ``TMPProviderRegistration`` deliberately does not subclass the SDK's closed
    ``RootModel`` union (see the module docstring), so these cases pin that the
    two do not diverge on the rules they *do* share.
    """

    @staticmethod
    def _sdk_accepts(**overrides) -> bool:
        from adcp.types.generated_poc.trusted_match.provider_registration import TmpProviderRegistration

        payload = {
            "provider_id": "test_provider",
            "endpoint": "https://provider.example.com/tmp",
            "context_match": True,
            **overrides,
        }
        try:
            TmpProviderRegistration.model_validate(payload)
        except ValidationError:
            return False
        return True

    def test_identity_match_without_countries_rejected_by_both(self):
        assert not self._sdk_accepts(context_match=False, identity_match=True, uid_types=["uid2"])

        assert _rejection_message(context_match=False, identity_match=True, countries=None, uid_types=["uid2"])

    def test_identity_match_without_uid_types_rejected_by_both(self):
        assert not self._sdk_accepts(context_match=False, identity_match=True, countries=["US"])

        assert _rejection_message(context_match=False, identity_match=True, countries=["US"], uid_types=None)

    def test_no_match_mode_rejected_by_both(self):
        assert not self._sdk_accepts(context_match=False)

        assert _rejection_message(context_match=False, identity_match=False)

    def test_fully_specified_identity_provider_accepted_by_both(self):
        assert self._sdk_accepts(identity_match=True, countries=["US"], uid_types=["uid2"])

        registration = TMPProviderRegistration.parse(_fields(identity_match=True, countries=["US"], uid_types=["uid2"]))
        assert registration.uid_types == ["uid2"]
