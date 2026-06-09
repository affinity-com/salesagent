"""Shared test helpers for TMP provider unit tests.

Extracted from test_tmp_providers_discovery_route.py to avoid duplicating the
UoW mock factories across the four TMP test files (CLAUDE.md DRY invariant).

Usage::

    from tests.unit._tmp_helpers import _make_tenant_uow, _make_tmp_uow, _make_provider

    mock_tenant_uow_cls = _make_tenant_uow(tenant)
    mock_tmp_uow_cls = _make_tmp_uow(providers)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.core.database.models import TMPProvider


def _make_provider(
    provider_id: str = "uuid-1",
    name: str = "Provider A",
    endpoint: str = "http://si-agent.localhost:3003",
    context_match: bool = True,
    identity_match: bool = True,
    countries: list[str] | None = None,
    uid_types: list[str] | None = None,
    properties: list[str] | None = None,
    timeout_ms: int = 200,
    priority: int = 0,
    status: str = "active",
) -> TMPProvider:
    """Create a real TMPProvider ORM instance (no DB session required).

    Uses the real model so that to_dict() is exercised against the production
    implementation rather than a MagicMock reimplementation that can silently
    diverge (e.g. the missing-properties regression that was caught in review).
    """
    p = TMPProvider()
    p.provider_id = provider_id
    p.name = name
    p.endpoint = endpoint
    p.context_match = context_match
    p.identity_match = identity_match
    p.countries = countries
    p.uid_types = uid_types
    p.properties = properties
    p.timeout_ms = timeout_ms
    p.priority = priority
    p.status = status
    return p


def _make_tenant_uow(tenant: MagicMock | None) -> MagicMock:
    """Return a mock TenantConfigUoW context manager.

    The yielded UoW has ``.tenant_config.get_tenant()`` returning *tenant*.
    Pass ``None`` to simulate an unknown tenant (404 path).
    """
    mock_uow = MagicMock()
    mock_uow.tenant_config = MagicMock()
    mock_uow.tenant_config.get_tenant.return_value = tenant
    mock_uow_cls = MagicMock()
    mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)
    return mock_uow_cls


def _make_tmp_uow(providers: list[TMPProvider]) -> MagicMock:
    """Return a mock TMPProviderUoW context manager.

    The yielded UoW has ``.tmp_providers.list_syncable()`` returning *providers*.
    """
    mock_uow = MagicMock()
    mock_uow.tmp_providers = MagicMock()
    mock_uow.tmp_providers.list_syncable.return_value = providers
    mock_uow_cls = MagicMock()
    mock_uow_cls.return_value.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow_cls.return_value.__exit__ = MagicMock(return_value=False)
    return mock_uow_cls
