"""Factory_boy factory for TMPProvider model."""

from __future__ import annotations

import uuid
from typing import Any

import factory
from factory import LazyAttribute, Sequence, SubFactory

from src.core.database.models import TMPProvider
from tests.factories.core import TenantFactory


class TMPProviderFactory(factory.alchemy.SQLAlchemyModelFactory):
    """Factory for TMPProvider ORM instances.

    Creates active providers by default.  Override ``status``, ``priority``,
    ``tenant_id`` etc. as needed for specific test scenarios.

    Note: ``provider_id`` uses a server-default (gen_random_uuid()) in
    production, so we generate a UUID client-side here to avoid a round-trip.
    """

    class Meta:
        model = TMPProvider
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"
        exclude = ["tenant"]

    tenant = SubFactory(TenantFactory)

    provider_id = factory.LazyFunction(lambda: str(uuid.uuid4()))
    tenant_id = LazyAttribute(lambda o: o.tenant.tenant_id)
    name = Sequence(lambda n: f"Provider {n:04d}")
    endpoint = LazyAttribute(lambda o: f"https://{o.name.lower().replace(' ', '-')}.example.com/tmp")
    context_match = True
    identity_match = False
    countries = None
    uid_types = None
    properties = None
    timeout_ms = 200
    priority = 0
    status = "active"
    auth_type = None
    # _auth_credentials is the raw column; leave None (no encryption in factory)
    _auth_credentials = None
    health_status = None
    last_health_checked_at = None


def replace_tmp_providers(env: Any, tenant_id: str, **fields: Any) -> TMPProvider:
    """Make *tenant_id* have exactly one TMP provider, built by the factory.

    The e2e analogue of ``set_adapter_test_behavior`` (``tests/factories/core.py``):
    a shared factory-backed helper so out-of-process tests configure the live DB
    through :class:`TMPProviderFactory` instead of hand-constructing the ORM row
    (CLAUDE.md Pattern #8 — no ``session.add()`` in test bodies).

    Existing providers for the tenant are deleted first. That is the point of
    "replace": the sync fans out to *every* syncable provider, so a row left by
    an earlier run would add unrelated POSTs — and unresolvable-host errors — to
    this tenant's fan-out.

    Args:
        env: Harness environment exposing ``get_session()`` (real-DB envs).
        tenant_id: Tenant the provider is registered under.
        **fields: Factory field overrides (``endpoint``, ``name``, ``status``, …).

    Returns the created provider.
    """
    from sqlalchemy import select

    session = env.get_session()
    for stale in session.scalars(select(TMPProvider).filter_by(tenant_id=tenant_id)).all():
        session.delete(stale)
    session.commit()

    TMPProviderFactory._meta.sqlalchemy_session = session
    try:
        return TMPProviderFactory(tenant_id=tenant_id, tenant=None, **fields)
    finally:
        TMPProviderFactory._meta.sqlalchemy_session = None


def delete_tmp_providers(env: Any, tenant_id: str) -> None:
    """Remove every TMP provider row for *tenant_id* (teardown counterpart)."""
    from sqlalchemy import select

    session = env.get_session()
    for row in session.scalars(select(TMPProvider).filter_by(tenant_id=tenant_id)).all():
        session.delete(row)
    session.commit()
