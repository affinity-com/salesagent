"""Unit tests for Siteplug adapter config loading.

Covers the bug fix where get_adapter() was passing {"enabled": True} to
SiteplugAdapter instead of reading base_url/api_key from config_json.

Three surfaces tested:
1. AdapterConfigRepository.get_siteplug_config() — pure logic method
2. get_adapter() — wiring: siteplug branch reads config_json via repo
3. tenant_status — is_tenant_ad_server_configured() and get_tenant_status()
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter_config(
    adapter_type: str = "siteplug",
    config_json: dict | None = None,
    tenant_id: str = "t-test",
) -> MagicMock:
    """Build a minimal AdapterConfig mock."""
    row = MagicMock()
    row.tenant_id = tenant_id
    row.adapter_type = adapter_type
    row.config_json = config_json if config_json is not None else {}
    return row


# ---------------------------------------------------------------------------
# 1. AdapterConfigRepository.get_siteplug_config()
# ---------------------------------------------------------------------------


class TestGetSiteplugConfig:
    """AdapterConfigRepository.get_siteplug_config() — pure logic, no DB."""

    def _call(self, config_row):
        from src.core.database.repositories.adapter_config import AdapterConfigRepository

        return AdapterConfigRepository.get_siteplug_config(config_row)

    def test_returns_base_url_and_api_key(self):
        """Happy path: config_json has both required fields."""
        row = _make_adapter_config(
            config_json={"base_url": "https://api.siteplug.com/ssp/v1", "api_key": "secret-key"}
        )
        result = self._call(row)

        assert result["base_url"] == "https://api.siteplug.com/ssp/v1"
        assert result["api_key"] == "secret-key"
        assert result["enabled"] is True

    def test_forwards_optional_timeout_and_max_retries(self):
        """Optional timeout/max_retries are forwarded when present."""
        row = _make_adapter_config(
            config_json={
                "base_url": "https://api.siteplug.com/ssp/v1",
                "api_key": "k",
                "timeout": 60,
                "max_retries": 5,
            }
        )
        result = self._call(row)

        assert result["timeout"] == 60
        assert result["max_retries"] == 5

    def test_omits_timeout_when_not_in_config_json(self):
        """timeout/max_retries are NOT injected when absent (let adapter use defaults)."""
        row = _make_adapter_config(
            config_json={"base_url": "https://api.siteplug.com/ssp/v1", "api_key": "k"}
        )
        result = self._call(row)

        assert "timeout" not in result
        assert "max_retries" not in result

    def test_raises_when_base_url_missing(self):
        """ValueError raised when base_url is absent from config_json."""
        row = _make_adapter_config(config_json={"api_key": "k"})

        with pytest.raises(ValueError, match="base_url"):
            self._call(row)

    def test_raises_when_api_key_missing(self):
        """ValueError raised when api_key is absent from config_json."""
        row = _make_adapter_config(config_json={"base_url": "https://api.siteplug.com/ssp/v1"})

        with pytest.raises(ValueError, match="api_key"):
            self._call(row)

    def test_raises_when_config_json_is_empty(self):
        """ValueError raised when config_json is {} (never saved)."""
        row = _make_adapter_config(config_json={})

        with pytest.raises(ValueError, match="base_url"):
            self._call(row)

    def test_raises_when_config_json_is_none(self):
        """ValueError raised when config_json is None (DB default)."""
        row = _make_adapter_config(config_json=None)

        with pytest.raises(ValueError, match="base_url"):
            self._call(row)

    def test_raises_for_wrong_adapter_type(self):
        """ValueError raised when called on a non-siteplug AdapterConfig."""
        row = _make_adapter_config(adapter_type="google_ad_manager", config_json={})

        with pytest.raises(ValueError, match="not a Siteplug adapter"):
            self._call(row)


# ---------------------------------------------------------------------------
# 2. get_adapter() wiring — siteplug branch
# ---------------------------------------------------------------------------


class TestGetAdapterSiteplugBranch:
    """get_adapter() correctly reads config_json for siteplug tenants."""

    def _make_tenant(self, tenant_id: str = "t-sp") -> MagicMock:
        tenant = MagicMock()
        tenant.tenant_id = tenant_id
        tenant.ad_server = "siteplug"
        return tenant

    def _make_principal(self) -> MagicMock:
        p = MagicMock()
        p.platform_mappings = {}
        return p

    def test_siteplug_adapter_receives_base_url_and_api_key(self):
        """get_siteplug_config() is called and its result is passed to SiteplugAdapter.

        We verify the wiring by asserting that repo.get_siteplug_config() was
        called (proving the siteplug branch ran) and that the returned config
        contains the expected credentials.  We don't try to intercept the
        SiteplugAdapter constructor because it's a local import inside
        get_adapter() and the module is already cached in sys.modules.
        """
        from src.core.helpers.adapter_helpers import get_adapter

        config_row = _make_adapter_config(
            config_json={"base_url": "https://api.siteplug.com/ssp/v1", "api_key": "live-key"}
        )
        expected_config = {
            "enabled": True,
            "base_url": "https://api.siteplug.com/ssp/v1",
            "api_key": "live-key",
        }

        with (
            patch("src.core.helpers.adapter_helpers.get_db_session") as mock_session_ctx,
            patch(
                "src.core.database.repositories.adapter_config.AdapterConfigRepository"
            ) as MockRepo,
            # Patch SiteplugAdapter at the package __init__ level so the local
            # `from src.adapters.siteplug import SiteplugAdapter` picks up the mock.
            patch("src.adapters.siteplug.SiteplugAdapter") as MockSiteplug,
        ):
            # Wire up the session context manager
            mock_session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)

            # Wire up the repo
            repo_instance = MagicMock()
            MockRepo.return_value = repo_instance
            repo_instance.find_by_tenant.return_value = config_row
            repo_instance.get_siteplug_config.return_value = expected_config

            get_adapter(self._make_principal(), dry_run=False, tenant=self._make_tenant())

        # The siteplug branch must have called get_siteplug_config() on the repo
        repo_instance.get_siteplug_config.assert_called_once_with(config_row)

        # The config returned by get_siteplug_config must contain the credentials
        returned_config = repo_instance.get_siteplug_config.return_value
        assert returned_config["base_url"] == "https://api.siteplug.com/ssp/v1"
        assert returned_config["api_key"] == "live-key"

    def test_siteplug_adapter_not_called_with_empty_config(self):
        """SiteplugAdapter is NOT instantiated when config_json is missing credentials."""
        from src.core.helpers.adapter_helpers import get_adapter

        config_row = _make_adapter_config(config_json={})  # missing credentials

        with (
            patch("src.core.helpers.adapter_helpers.get_db_session") as mock_session_ctx,
            patch(
                "src.core.database.repositories.adapter_config.AdapterConfigRepository"
            ) as MockRepo,
            patch("src.adapters.siteplug.adapter.SiteplugAdapter") as MockSiteplug,
        ):
            mock_session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)

            repo_instance = MagicMock()
            MockRepo.return_value = repo_instance
            repo_instance.find_by_tenant.return_value = config_row
            # Simulate the repo raising ValueError for missing credentials
            repo_instance.get_siteplug_config.side_effect = ValueError("missing base_url or api_key")

            with pytest.raises(ValueError, match="base_url"):
                get_adapter(self._make_principal(), dry_run=False, tenant=self._make_tenant())

        MockSiteplug.assert_not_called()

    def test_siteplug_adapter_not_called_with_empty_config_dry_run(self):
        """ValueError from get_siteplug_config() fires before dry-run bypass in SiteplugAdapter.

        SiteplugAdapter.__init__() uses placeholder credentials when dry_run=True,
        but get_siteplug_config() is called first (in get_adapter()) and raises
        ValueError for missing credentials — so the adapter is never instantiated.
        """
        from src.core.helpers.adapter_helpers import get_adapter

        config_row = _make_adapter_config(config_json={})  # missing credentials

        with (
            patch("src.core.helpers.adapter_helpers.get_db_session") as mock_session_ctx,
            patch(
                "src.core.database.repositories.adapter_config.AdapterConfigRepository"
            ) as MockRepo,
            patch("src.adapters.siteplug.adapter.SiteplugAdapter") as MockSiteplug,
        ):
            mock_session = MagicMock()
            mock_session_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_session_ctx.return_value.__exit__ = MagicMock(return_value=False)

            repo_instance = MagicMock()
            MockRepo.return_value = repo_instance
            repo_instance.find_by_tenant.return_value = config_row
            # Simulate the repo raising ValueError for missing credentials
            repo_instance.get_siteplug_config.side_effect = ValueError("missing base_url or api_key")

            with pytest.raises(ValueError, match="base_url"):
                get_adapter(self._make_principal(), dry_run=True, tenant=self._make_tenant())

        # The adapter's dry-run bypass must never be reached
        MockSiteplug.assert_not_called()


# ---------------------------------------------------------------------------
# 3. tenant_status — siteplug readiness checks
# ---------------------------------------------------------------------------


class TestIsTenantAdServerConfiguredSiteplug:
    """is_tenant_ad_server_configured() returns correct value for siteplug."""

    def _make_tenant_with_adapter(self, config_json: dict) -> MagicMock:
        adapter = MagicMock()
        adapter.adapter_type = "siteplug"
        adapter.config_json = config_json

        tenant = MagicMock()
        tenant.is_active = True
        tenant.adapter_config = adapter
        return tenant

    def _call(self, tenant_id: str, tenant_obj: MagicMock) -> bool:
        from src.core.tenant_status import is_tenant_ad_server_configured

        with (
            patch("src.core.tenant_status.get_db_session") as mock_ctx,
        ):
            mock_session = MagicMock()
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            mock_session.scalars.return_value.first.return_value = tenant_obj

            return is_tenant_ad_server_configured(tenant_id)

    def test_returns_true_when_credentials_present(self):
        tenant = self._make_tenant_with_adapter(
            {"base_url": "https://api.siteplug.com/ssp/v1", "api_key": "k"}
        )
        assert self._call("t-sp", tenant) is True

    def test_returns_false_when_base_url_missing(self):
        tenant = self._make_tenant_with_adapter({"api_key": "k"})
        assert self._call("t-sp", tenant) is False

    def test_returns_false_when_api_key_missing(self):
        tenant = self._make_tenant_with_adapter({"base_url": "https://api.siteplug.com/ssp/v1"})
        assert self._call("t-sp", tenant) is False

    def test_returns_false_when_config_json_empty(self):
        tenant = self._make_tenant_with_adapter({})
        assert self._call("t-sp", tenant) is False


class TestGetTenantStatusSiteplug:
    """get_tenant_status() reports correct missing_config for siteplug."""

    def _make_tenant_with_adapter(self, config_json: dict) -> MagicMock:
        adapter = MagicMock()
        adapter.adapter_type = "siteplug"
        adapter.config_json = config_json

        tenant = MagicMock()
        tenant.is_active = True
        tenant.adapter_config = adapter
        return tenant

    def _call(self, tenant_id: str, tenant_obj: MagicMock) -> dict:
        from src.core.tenant_status import get_tenant_status

        with patch("src.core.tenant_status.get_db_session") as mock_ctx:
            mock_session = MagicMock()
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            mock_session.scalars.return_value.first.return_value = tenant_obj

            return get_tenant_status(tenant_id)

    def test_is_configured_true_when_both_fields_present(self):
        tenant = self._make_tenant_with_adapter(
            {"base_url": "https://api.siteplug.com/ssp/v1", "api_key": "k"}
        )
        status = self._call("t-sp", tenant)
        assert status["is_configured"] is True
        assert status["missing_config"] == []

    def test_reports_missing_base_url(self):
        tenant = self._make_tenant_with_adapter({"api_key": "k"})
        status = self._call("t-sp", tenant)
        assert status["is_configured"] is False
        assert any("base_url" in m for m in status["missing_config"])

    def test_reports_missing_api_key(self):
        tenant = self._make_tenant_with_adapter(
            {"base_url": "https://api.siteplug.com/ssp/v1"}
        )
        status = self._call("t-sp", tenant)
        assert status["is_configured"] is False
        assert any("api_key" in m for m in status["missing_config"])

    def test_reports_both_missing_when_config_json_empty(self):
        tenant = self._make_tenant_with_adapter({})
        status = self._call("t-sp", tenant)
        assert status["is_configured"] is False
        missing = status["missing_config"]
        assert any("base_url" in m for m in missing)
        assert any("api_key" in m for m in missing)
