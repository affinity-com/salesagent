"""Transport-agnostic TMP provider registration record.

Owns the AdCP provider-registration invariants so that *every* write surface
enforces them, not just the Flask admin form.  Before this module existed, the
uid-type enum, the status enum, the "at least one match mode" rule and the
``identity_match ⇒ countries + uid_types`` rule all lived in
``src/admin/blueprints/tmp_providers.py`` while the repository write path took
``**kwargs: object`` and checked only that attribute names existed — so the
first programmatic write surface (an MCP/A2A/REST tool, a bulk import) would
have forked or silently dropped every one of them (#1197 review).

Layering:
  - **This module** owns *validity*: which values are legal for a registration.
  - **The blueprint** owns *form shape*: CSV splitting, checkbox ``"on"``,
    int parsing, and turning a rejection into a flash message.
  - **The repository** owns *persistence*, typed against
    :class:`TMPProviderFields` instead of a runtime ``hasattr`` guard.

Spec grounding (pin: ``adcp==6.6.0``, AdCP spec 3.1.1):
  - ``dist/schemas/3.1.1/trusted-match/provider-registration.json`` — the
    ``anyOf`` requiring ``context_match`` or ``identity_match``, and the
    ``identity_match ⇒ countries/uid_types non-empty`` rule (mirrored by the
    SDK's own ``_require_identity_match_dimensions`` model validator).
  - ``dist/schemas/3.1.1/enums/uid-type.json`` → ``adcp.types.UidType``.
  - The ``status`` enum → the SDK's ``provider_registration.Status``.

Both enums are derived from the pinned SDK rather than hand-maintained
frozensets, so a spec bump can only widen them by upgrading the pin.

Why this is not a subclass of the SDK's ``TmpProviderRegistration``
(CLAUDE.md Pattern #1 asks for inheritance where a library counterpart exists):
that type is a ``RootModel`` union over two closed (``extra="forbid"``)
variants describing the **wire** registration a router consumes.  It requires
``provider_id`` (assigned by us at INSERT, absent from the form), forbids the
three fields this record must carry (``name``, ``auth_type``,
``auth_credentials``), types ``properties`` as UUIDs, and rejects non-https
endpoints — which would break local development against
``http://…​.localhost`` providers.  Inheriting a closed RootModel union also
yields no field inheritance.  The shared rules are instead grounded on the same
SDK enums and pinned against the library model by
``tests/unit/test_tmp_provider_registration.py``.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from adcp.types.generated_poc.enums.uid_type import UidType
from adcp.types.generated_poc.trusted_match.provider_registration import Status as ProviderStatus
from pydantic import ValidationError, model_validator

from src.core.schemas._base import SalesAgentBaseModel
from src.core.security.url_validator import check_url_ssrf, sanitize_for_log

logger = logging.getLogger(__name__)

# ``src/core/schemas/__init__.py`` star-imports this module. Without __all__ the
# star would also re-export this module's own imports (``logger``, ``logging``,
# ``check_url_ssrf``, ``ValidationError``, …) into ``src.core.schemas``, where
# generic names like ``logger`` can shadow a sibling module's.
__all__ = [
    "VALID_STATUSES",
    "VALID_UID_TYPES",
    "TMPDiscoveryResponse",
    "TMPProviderDiscoveryDict",
    "TMPProviderFields",
    "TMPProviderRegistration",
    "TMPProviderValidationError",
]

# Derived from the pinned SDK enums — never hand-maintained.  A spec bump that
# adds a uid type or a lifecycle status widens these automatically.
VALID_UID_TYPES: frozenset[str] = frozenset(t.value for t in UidType)
VALID_STATUSES: frozenset[str] = frozenset(s.value for s in ProviderStatus)


class TMPProviderValidationError(ValueError):
    """A TMP provider registration was rejected by a domain invariant.

    Carries the operator-facing message as its only argument, so a write surface
    can surface it directly (``flash(str(exc))``) without reaching into pydantic
    error structures.  Subclasses ``ValueError`` so callers that already handle
    bad input generically keep working.
    """


class TMPProviderFields(TypedDict, total=False):
    """The twelve persisted TMP provider fields, as a static kwargs contract.

    ``total=False`` because the update path writes a subset (``auth_credentials``
    is only included when the operator submitted a new value).  Used via
    ``**Unpack[TMPProviderFields]`` on the repository write methods so a typo
    (``timout_ms``, ``contry``) is a type error at the call site rather than a
    ``ValueError`` raised when the write finally runs.

    Mirrors :class:`TMPProviderRegistration`'s field set; the two are pinned
    equal by ``test_tmp_provider_registration.py`` so they cannot drift.
    """

    name: str
    endpoint: str
    context_match: bool
    identity_match: bool
    countries: list[str] | None
    uid_types: list[str] | None
    properties: list[str] | None
    timeout_ms: int
    priority: int
    status: str
    auth_type: str | None
    auth_credentials: str | None


class _TMPProviderDiscoveryRequired(TypedDict):
    """The always-emitted half of :class:`TMPProviderDiscoveryDict` (see there)."""

    provider_id: str
    endpoint: str
    context_match: bool
    identity_match: bool
    timeout_ms: int
    priority: int
    status: str


class TMPProviderDiscoveryDict(_TMPProviderDiscoveryRequired, total=False):
    """One provider entry on the discovery wire, typed against the closed schema.

    ``dist/schemas/3.1.1/trusted-match/provider-registration.json`` is a closed
    object (``additionalProperties: false``), so this is the exact key set the
    discovery endpoint may emit — not a partial view.  The three keys declared
    here are ``total=False`` because the schema types each as ``array`` with
    ``minItems: 1``: an absent value must be **omitted**, never sent as ``null``
    (``null`` is a type violation a strictly-validating router rejects).

    ``name`` is deliberately absent: it is not in the closed schema.  It stays
    on the admin serialization (:meth:`TMPProvider.to_admin_dict`), which feeds
    Jinja templates rather than the machine wire (#1197 review).

    ``tmpx_macros`` — the schema's remaining optional property, added by 3.1.1
    for provider-namespaced ad-server macro names — is not carried: there is no
    column, admin field, or router consumer for it yet, and omitting an optional
    property is conformant.
    """

    countries: list[str]
    uid_types: list[str]
    properties: list[str]


class TMPDiscoveryResponse(SalesAgentBaseModel):
    """Body of ``GET /tenant/{tenant_id}/tmp-providers/discovery``.

    Used as the route's ``response_model`` so FastAPI publishes an OpenAPI
    schema for the discovery contract and validates the outgoing keys, instead
    of the route hand-building an unvalidated ``JSONResponse`` (#1197 review).
    """

    tenant_id: str
    providers: list[TMPProviderDiscoveryDict]


class TMPProviderRegistration(SalesAgentBaseModel):
    """A validated TMP provider registration, independent of how it was submitted.

    Construct it with :meth:`parse` (which narrows the failure to a single
    operator-facing :class:`TMPProviderValidationError`) or directly with
    ``TMPProviderRegistration(...)`` when the full pydantic ``ValidationError``
    — every failing field, not just the first — is the more useful failure mode.
    """

    name: str
    endpoint: str
    context_match: bool = False
    identity_match: bool = False
    countries: list[str] | None = None
    uid_types: list[str] | None = None
    properties: list[str] | None = None
    timeout_ms: int = 50
    priority: int = 0
    status: str = "active"
    auth_type: str | None = None
    auth_credentials: str | None = None

    @model_validator(mode="after")
    def _check_registration_invariants(self) -> TMPProviderRegistration:
        """Enforce every provider-registration invariant in one pass.

        Ordered so the cheapest presence checks run before the SSRF check, which
        resolves DNS.  Messages are the operator-facing strings the admin UI
        flashes verbatim.
        """
        if not self.name.strip():
            raise ValueError("Provider name is required")
        if not self.endpoint.strip():
            raise ValueError("Endpoint URL is required")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status '{self.status}'. Valid values: {', '.join(sorted(VALID_STATUSES))}")

        is_safe, ssrf_error = check_url_ssrf(self.endpoint)
        if not is_safe:
            # Tagged `[TMP …]` so an operator grepping `[TMP` for this feature's
            # logs sees SSRF rejections from every write surface, not just the
            # admin form the check used to live behind.
            logger.warning(
                "[TMP registration][SECURITY] Provider rejected unsafe URL %s: %s",
                sanitize_for_log(self.endpoint),
                sanitize_for_log(ssrf_error),
            )
            raise ValueError(f"Endpoint URL is not allowed: {ssrf_error}")

        if not self.context_match and not self.identity_match:
            raise ValueError("Provider must support at least one of context_match or identity_match")

        if self.identity_match:
            if not self.countries:
                raise ValueError("Countries are required when identity_match is enabled (ISO 3166-1 alpha-2 codes)")
            if not self.uid_types:
                raise ValueError(
                    "UID types are required when identity_match is enabled (e.g. uid2, publisher_first_party)"
                )
            invalid_types = [u for u in self.uid_types if u not in VALID_UID_TYPES]
            if invalid_types:
                raise ValueError(
                    f"Invalid uid_type(s): {', '.join(invalid_types)}. "
                    f"Valid values: {', '.join(sorted(VALID_UID_TYPES))}"
                )
        return self

    @classmethod
    def parse(cls, fields: TMPProviderFields) -> TMPProviderRegistration:
        """Validate *fields*, raising :class:`TMPProviderValidationError` on rejection.

        Equivalent to constructing the model directly, except the pydantic
        ``ValidationError`` is narrowed to one operator-facing message — the
        single string the admin UI flashes.  Raising (rather than returning
        ``(model | None, message | None)``) keeps the success type non-optional,
        so callers never carry an ``Optional`` past the guard.
        """
        try:
            return cls(**fields)
        except ValidationError as exc:
            raise TMPProviderValidationError(_first_error_message(exc)) from exc

    def to_fields(self) -> TMPProviderFields:
        """Return the persisted field set for ``create_from_fields(**…)``."""
        return TMPProviderFields(
            name=self.name,
            endpoint=self.endpoint,
            context_match=self.context_match,
            identity_match=self.identity_match,
            countries=self.countries,
            uid_types=self.uid_types,
            properties=self.properties,
            timeout_ms=self.timeout_ms,
            priority=self.priority,
            status=self.status,
            auth_type=self.auth_type,
            auth_credentials=self.auth_credentials,
        )

    def to_update_fields(self, *, include_credentials: bool) -> TMPProviderFields:
        """Return the field set for ``update_fields(provider_id, **…)``.

        ``auth_credentials`` is omitted unless *include_credentials* is true, so
        an edit that leaves the credential field blank preserves the stored
        (encrypted) value rather than overwriting it with ``None``.
        """
        fields = self.to_fields()
        if not include_credentials:
            fields.pop("auth_credentials", None)
        return fields


def _first_error_message(exc: ValidationError) -> str:
    """Extract the operator-facing message from a ``ValidationError``.

    Pydantic prefixes messages raised by a validator with ``"Value error, "``;
    strip it so the admin UI flashes the message this module wrote.
    """
    first = exc.errors()[0]
    return str(first.get("msg", "Invalid TMP provider registration")).removeprefix("Value error, ")
