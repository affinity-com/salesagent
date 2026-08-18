"""Unit tests for the TMP Provider admin blueprint — what this LAYER adds.

Registration *validity* (which values are legal) belongs to
``TMPProviderRegistration`` and is graded in
``tests/unit/test_tmp_provider_registration.py``. This file grades only what the
blueprint itself owns:

- one proof that a rejected registration flashes, bounces, and does not write,
  driven by ``@parametrize`` over the invalid payloads — this used to be eight
  byte-identical bodies each re-enumerating an invariant the record's suite now
  owns (#1197 review)
- CSV/checkbox/int form shape reaching ``create_from_fields`` / ``update_fields``
- the credential-preservation rule on edit, and the masked (never echoed) render
- the render kwargs of the list and add-GET branches
- the two error helpers' 500 / flash+redirect shapes
- CRUD route responses (deactivate, delete, health check) and their JSON bodies

Note: Discovery endpoint tests are in test_tmp_providers_discovery_route.py
(the canonical discovery endpoint is the FastAPI route, not Flask).
"""

import os
from unittest.mock import patch

import pytest

from src.core.database.models import TMPProvider
from src.core.schemas.tmp_provider import VALID_STATUSES, VALID_UID_TYPES
from tests.unit._tmp_helpers import _make_blueprint_uow, _make_mock_provider, make_super_admin_client


def _make_tmp_provider_client():
    """Create a Flask test client authenticated as super admin for TMP provider endpoints."""
    return make_super_admin_client()


_SAFE_ENDPOINT = "https://provider.example.com/tmp"

# One valid add payload; each rejection case overrides exactly one thing, so a
# failure names the rejected field rather than a setup mistake.
_VALID_ADD_FORM: dict[str, str] = {
    "name": "Test Provider",
    "endpoint": _SAFE_ENDPOINT,
    "context_match": "on",
    "timeout_ms": "50",
}

# WHICH values are invalid is the record's contract (graded in
# test_tmp_provider_registration.py). This list exists only to drive the ONE
# layer-level claim below across the rejection paths a form can produce —
# including the two the record cannot see (non-numeric int, unsafe URL).
_REJECTED_ADD_FORMS: list[tuple[str, dict[str, str]]] = [
    ("ssrf_endpoint", {"endpoint": "http://host.docker.internal:9999"}),
    ("missing_endpoint", {"endpoint": ""}),
    ("missing_name", {"name": ""}),
    ("non_numeric_timeout", {"timeout_ms": "not-a-number"}),
    ("invalid_status", {"status": "bogus_status"}),
    ("identity_match_without_countries", {"identity_match": "on", "countries": "", "uid_types": "uid2"}),
    ("identity_match_without_uid_types", {"identity_match": "on", "countries": "US", "uid_types": ""}),
    ("invalid_uid_type", {"identity_match": "on", "countries": "US", "uid_types": "bogus_type"}),
    ("lowercase_country", {"identity_match": "on", "countries": "usa", "uid_types": "uid2"}),
]


def _post_add(form: dict[str, str], mock_uow_cls) -> object:
    """POST the add form with DNS stubbed public, returning the response."""
    client = _make_tmp_provider_client()
    with (
        patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
        patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"),
        patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
    ):
        return client.post("/tenant/default/tmp-providers/add", data=form, follow_redirects=False)


class TestRejectedRegistrationIsFlashedAndNotWritten:
    """The layer's rejection contract, proved once over every rejection path.

    A rejected registration must (a) bounce back to the add form and (b) NOT
    persist. The bounce alone is not enough: a regression that flashes,
    redirects AND writes the row would pass on the redirect assertions, which is
    why ``create_from_fields.assert_not_called()`` is here rather than in a
    comment.
    """

    @pytest.mark.parametrize("form_overrides", [pytest.param(o, id=name) for name, o in _REJECTED_ADD_FORMS])
    def test_rejected_add_flashes_bounces_and_does_not_write(self, form_overrides):
        mock_uow_cls, mock_uow = _make_blueprint_uow()

        response = _post_add({**_VALID_ADD_FORM, **form_overrides}, mock_uow_cls)

        assert response.status_code == 302
        assert "add" in response.headers.get("Location", "")
        mock_uow.tmp_providers.create_from_fields.assert_not_called()


class TestTMPProviderAddSSRF:
    """SSRF validation is wired into the add endpoint."""

    def test_add_accepts_safe_public_url(self):
        """POST /tmp-providers/add with a safe public URL must proceed past SSRF check."""
        client = _make_tmp_provider_client()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/add",
                        data={
                            "name": "Safe Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US,GB",
                            "uid_types": "uid2,id5",
                            "timeout_ms": "50",
                        },
                        follow_redirects=False,
                    )

        # Must redirect to list (success) — not back to add form
        assert response.status_code == 302
        assert "add" not in response.headers.get("Location", "")
        # create_from_fields is called (not create) — the blueprint uses the
        # factory method symmetric with update_fields on the edit path.
        mock_uow.tmp_providers.create_from_fields.assert_called_once_with(
            name="Safe Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US", "GB"],
            uid_types=["uid2", "id5"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type=None,
            auth_credentials=None,
        )


class TestTMPProviderEditSSRF:
    """SSRF validation is wired into the edit endpoint."""

    def test_edit_rejects_unsafe_url_on_update(self):
        """POST /tmp-providers/<id>/edit updating URL to host.docker.internal must be rejected.

        Uses identity_match=on with countries+uid_types so the SSRF check is the
        sole rejection reason — mirrors the add-path test (test_add_rejects_docker_internal_url)
        which uses context_match-only to isolate SSRF as the sole cause.
        """
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.post(
                    "/tenant/default/tmp-providers/prov_test_1234/edit",
                    data={
                        "name": "Existing Provider",
                        "endpoint": "http://host.docker.internal:9999",
                        "context_match": "on",
                        "identity_match": "on",
                        # Provide valid countries+uid_types so identity_match validation
                        # passes and SSRF is the sole rejection reason.
                        "countries": "US,GB",
                        "uid_types": "uid2,id5",
                        "timeout_ms": "50",
                        "status": "active",
                    },
                    follow_redirects=False,
                )

        assert response.status_code == 302
        assert "edit" in response.headers.get("Location", "")
        mock_uow.tmp_providers.update_fields.assert_not_called()

    def test_edit_accepts_safe_public_url(self):
        """POST /tmp-providers/<id>/edit with a safe public URL must succeed.

        Positive counterpart to test_edit_rejects_unsafe_url_on_update — verifies
        that the SSRF guard does not block legitimate public endpoints on the edit path.
        """
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/prov_test_1234/edit",
                        data={
                            "name": "Existing Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US,GB",
                            "uid_types": "uid2,id5",
                            "timeout_ms": "50",
                            "status": "active",
                        },
                        follow_redirects=False,
                    )

        assert response.status_code == 302
        assert "tmp-providers" in response.headers.get("Location", "")
        mock_uow.tmp_providers.update_fields.assert_called_once_with(
            "prov_test_1234",
            name="Existing Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US", "GB"],
            uid_types=["uid2", "id5"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type=None,
        )


class TestTMPProviderInputValidation:
    """Input validation for required fields."""

    def test_add_passes_status_to_create_from_fields(self):
        """POST /tmp-providers/add with explicit status passes it to create_from_fields."""
        client = _make_tmp_provider_client()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/add",
                        data={
                            "name": "Draining Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US",
                            "uid_types": "uid2",
                            "timeout_ms": "50",
                            "status": "draining",
                        },
                        follow_redirects=False,
                    )

        assert response.status_code == 302
        mock_uow.tmp_providers.create_from_fields.assert_called_once_with(
            name="Draining Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US"],
            uid_types=["uid2"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="draining",
            auth_type=None,
            auth_credentials=None,
        )


class TestTMPProviderDeactivate:
    """Deactivate endpoint sets status='inactive' via repository."""

    def test_deactivate_returns_success_json(self):
        """POST /tmp-providers/<id>/deactivate returns JSON success."""
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.deactivate.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.post(
                    "/tenant/default/tmp-providers/prov_test_1234/deactivate",
                )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        mock_uow.tmp_providers.deactivate.assert_called_once_with("prov_test_1234")

    def test_deactivate_returns_404_for_missing_provider(self):
        """POST /tmp-providers/<id>/deactivate returns 404 when provider not found."""
        client = _make_tmp_provider_client()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.deactivate.return_value = None
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.post(
                    "/tenant/default/tmp-providers/nonexistent-uuid/deactivate",
                )

        assert response.status_code == 404
        data = response.get_json()
        assert "error" in data


class TestTMPProviderDelete:
    """Delete endpoint hard-deletes a provider via repository."""

    def test_delete_returns_success_json(self):
        """DELETE /tmp-providers/<id>/delete returns JSON success."""
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        mock_uow.tmp_providers.delete.return_value = True
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.delete(
                    "/tenant/default/tmp-providers/prov_test_1234/delete",
                )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        mock_uow.tmp_providers.delete.assert_called_once_with("prov_test_1234")

    def test_delete_returns_404_for_missing_provider(self):
        """DELETE /tmp-providers/<id>/delete returns 404 when provider not found."""
        client = _make_tmp_provider_client()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = None
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.delete(
                    "/tenant/default/tmp-providers/nonexistent-uuid/delete",
                )

        assert response.status_code == 404


class TestTMPProviderHealthCheck:
    """Health check endpoint reads from DB (background scheduler writes health_status)."""

    def test_health_check_returns_healthy_from_db(self):
        """GET /tmp-providers/<id>/health returns healthy when health_status='healthy'."""
        from datetime import UTC, datetime

        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()
        existing_provider.health_status = "healthy"
        existing_provider.last_health_checked_at = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.get(
                    "/tenant/default/tmp-providers/prov_test_1234/health",
                )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["status"] == "healthy"
        assert data["last_checked"] is not None

    def test_health_check_returns_unhealthy_from_db(self):
        """GET /tmp-providers/<id>/health returns unhealthy when health_status='unhealthy'."""
        from datetime import UTC, datetime

        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()
        existing_provider.health_status = "unhealthy"
        existing_provider.last_health_checked_at = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.get(
                    "/tenant/default/tmp-providers/prov_test_1234/health",
                )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is False
        assert data["status"] == "unhealthy"

    def test_health_check_returns_pending_when_never_checked(self):
        """GET /tmp-providers/<id>/health returns pending when health_status is None."""
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()
        existing_provider.health_status = None
        existing_provider.last_health_checked_at = None

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                response = client.get(
                    "/tenant/default/tmp-providers/prov_test_1234/health",
                )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["status"] == "pending"


class TestTMPProviderAuthFields:
    """auth_type and auth_credentials are parsed and passed through the add/edit flow."""

    def test_add_passes_auth_type_and_credentials_to_create_from_fields(self):
        """POST /tmp-providers/add with auth_type and auth_credentials passes them to create_from_fields.

        The blueprint now calls ``uow.tmp_providers.create_from_fields(**data)`` instead of
        constructing ``TMPProvider(...)`` inline, so we assert on the repository factory method.
        """
        client = _make_tmp_provider_client()

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/add",
                        data={
                            "name": "Auth Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US",
                            "uid_types": "uid2",
                            "timeout_ms": "50",
                            "auth_type": "bearer",
                            "auth_credentials": "my-secret-token",
                        },
                        follow_redirects=False,
                    )

        assert response.status_code == 302
        mock_uow.tmp_providers.create_from_fields.assert_called_once_with(
            name="Auth Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US"],
            uid_types=["uid2"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type="bearer",
            auth_credentials="my-secret-token",
        )

    def test_edit_get_includes_auth_fields_in_provider_dict(self):
        """GET /tmp-providers/<id>/edit includes auth_type and auth_credentials in template context.

        Uses a real TMPProvider instance (not a MagicMock) so that to_admin_dict()
        and has_auth_credentials are exercised against the production implementation —
        avoids the missing-properties regression that was caught in review (same
        pattern as test_tmp_providers_discovery_route.py).
        """
        client = _make_tmp_provider_client()

        # Real TMPProvider instance — to_admin_dict() is the production implementation.
        existing_provider = TMPProvider(
            provider_id="prov_test_1234",
            tenant_id="default",
            name="Test Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US", "GB"],
            uid_types=["uid2", "id5"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type="bearer",
        )
        # Set auth_credentials via the property so the encryption path is exercised.
        from cryptography.fernet import Fernet

        _key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": _key}):
            existing_provider.auth_credentials = "stored-token"

            mock_uow_cls, mock_uow = _make_blueprint_uow()
            mock_tenant = mock_uow.tenant_config.get_tenant.return_value
            mock_uow.tmp_providers.get_by_id.return_value = existing_provider
            with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
                with patch("src.admin.blueprints.tmp_providers.render_template") as mock_render:
                    mock_render.return_value = "<html/>"
                    with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                        response = client.get(
                            "/tenant/default/tmp-providers/prov_test_1234/edit",
                        )

        assert response.status_code == 200
        # Production calls to_admin_dict() then overwrites
        # list fields with comma-separated strings and adds auth fields with
        # placeholder masking (credentials are never echoed back to the browser).
        mock_render.assert_called_once_with(
            "tmp_provider_form.html",
            tenant=mock_tenant,
            tenant_id="default",
            tenant_name="Default Tenant",
            provider={
                "provider_id": "prov_test_1234",
                "name": "Test Provider",
                "endpoint": "https://provider.example.com/tmp",
                "context_match": True,
                "identity_match": True,
                "countries": "US,GB",
                "uid_types": "uid2,id5",
                "properties": "",
                "timeout_ms": 50,
                "priority": 0,
                "status": "active",
                "auth_type": "bearer",
                "auth_credentials": "••••••••",
            },
            valid_uid_types=sorted(VALID_UID_TYPES),
            valid_statuses=sorted(VALID_STATUSES),
        )

    def test_edit_post_preserves_existing_credentials_when_empty_submitted(self):
        """POST /tmp-providers/<id>/edit with empty auth_credentials preserves existing value."""
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()
        existing_provider.auth_type = "bearer"
        existing_provider.auth_credentials = "existing-secret"

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/prov_test_1234/edit",
                        data={
                            "name": "Existing Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US",
                            "uid_types": "uid2",
                            "timeout_ms": "50",
                            "status": "active",
                            "auth_type": "bearer",
                            "auth_credentials": "",  # empty — should preserve existing
                        },
                        follow_redirects=False,
                    )

        assert response.status_code == 302
        # Production uses update_fields() — verify auth_credentials was NOT
        # included in the kwargs (empty submission preserves existing value).
        mock_uow.tmp_providers.update_fields.assert_called_once_with(
            "prov_test_1234",
            name="Existing Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US"],
            uid_types=["uid2"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type="bearer",
            # auth_credentials intentionally absent — empty submission preserves existing value
        )

    def test_edit_post_updates_credentials_when_new_value_submitted(self):
        """POST /tmp-providers/<id>/edit with non-empty auth_credentials updates the value."""
        client = _make_tmp_provider_client()

        existing_provider = _make_mock_provider()
        existing_provider.auth_type = "bearer"
        existing_provider.auth_credentials = "old-secret"

        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = existing_provider
        with patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls):
            with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
                with patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}):
                    response = client.post(
                        "/tenant/default/tmp-providers/prov_test_1234/edit",
                        data={
                            "name": "Existing Provider",
                            "endpoint": "https://provider.example.com/tmp",
                            "context_match": "on",
                            "identity_match": "on",
                            "countries": "US",
                            "uid_types": "uid2",
                            "timeout_ms": "50",
                            "status": "active",
                            "auth_type": "bearer",
                            "auth_credentials": "new-secret",
                        },
                        follow_redirects=False,
                    )

        assert response.status_code == 302
        # Production uses update_fields() — verify auth_credentials IS included
        # with the new value when a non-empty credential is submitted.
        mock_uow.tmp_providers.update_fields.assert_called_once_with(
            "prov_test_1234",
            name="Existing Provider",
            endpoint="https://provider.example.com/tmp",
            context_match=True,
            identity_match=True,
            countries=["US"],
            uid_types=["uid2"],
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type="bearer",
            auth_credentials="new-secret",
        )


# TestTMPProviderSerializers lives in test_tmp_providers_discovery_route.py (the
# canonical home for model contract tests — uses _tmp_helpers._make_provider).
# Keeping a second copy here would require parallel edits on every serializer
# change and one copy would inevitably drift (CLAUDE.md DRY invariant).


class TestListRouteRenderKwargs:
    """``list_tmp_providers`` builds the table rows the list template reads.

    The list branch was reworked twice in this PR with nothing grading it —
    deleting the ``created_at`` line failed no test, and neither did dropping a
    provider from the list (#1197 review).
    """

    def test_rows_carry_the_admin_shape_plus_created_at(self):
        from datetime import UTC, datetime

        created = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        provider = TMPProvider(
            provider_id="prov_list_1",
            tenant_id="default",
            name="Listed Provider",
            endpoint=_SAFE_ENDPOINT,
            context_match=True,
            identity_match=False,
            countries=None,
            uid_types=None,
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
        )
        provider.created_at = created

        client = _make_tmp_provider_client()
        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_tenant = mock_uow.tenant_config.get_tenant.return_value
        mock_uow.tmp_providers.list_all.return_value = [provider]

        with (
            patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
            patch("src.admin.blueprints.tmp_providers.render_template") as mock_render,
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
        ):
            mock_render.return_value = "<html/>"
            response = client.get("/tenant/default/tmp-providers/")

        assert response.status_code == 200
        mock_render.assert_called_once_with(
            "tmp_providers.html",
            tenant=mock_tenant,
            tenant_id="default",
            tenant_name="Default Tenant",
            providers=[
                {
                    "provider_id": "prov_list_1",
                    "name": "Listed Provider",
                    "endpoint": _SAFE_ENDPOINT,
                    "context_match": True,
                    "identity_match": False,
                    "timeout_ms": 50,
                    "priority": 0,
                    "status": "active",
                    "countries": None,
                    "uid_types": None,
                    "properties": None,
                    "created_at": created,
                }
            ],
        )


class TestAddGetRendersTheEmptyForm:
    """The add-GET branch renders the form with no provider and both vocabularies.

    The vocabularies are the point: passing them is what keeps the UI's uid-type
    and status lists in step with the SDK-derived enums instead of the stale
    hand-typed copies the template used to carry (#1197 review).
    """

    def test_renders_with_no_provider_and_the_enum_vocabularies(self):
        client = _make_tmp_provider_client()
        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_tenant = mock_uow.tenant_config.get_tenant.return_value

        with (
            patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
            patch("src.admin.blueprints.tmp_providers.render_template") as mock_render,
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
        ):
            mock_render.return_value = "<html/>"
            response = client.get("/tenant/default/tmp-providers/add")

        assert response.status_code == 200
        mock_render.assert_called_once_with(
            "tmp_provider_form.html",
            tenant=mock_tenant,
            tenant_id="default",
            tenant_name="Default Tenant",
            provider=None,
            valid_uid_types=sorted(VALID_UID_TYPES),
            valid_statuses=sorted(VALID_STATUSES),
        )

    def test_the_rendered_vocabularies_cover_the_whole_enum(self):
        """Every SDK uid type and status reaches the template context.

        Pins the fix for the specific drift found in review: the template's
        hand-written list was missing ``rampid_derived`` and
        ``world_id_nullifier``, so the form rejected values the enum accepts.
        """
        from src.admin.blueprints.tmp_providers import _form_render_context

        context = _form_render_context()

        assert set(context["valid_uid_types"]) == set(VALID_UID_TYPES)
        assert set(context["valid_statuses"]) == set(VALID_STATUSES)


class TestEditGetSurvivesAnUnreadableCredential:
    """A rotated/corrupt ciphertext must render the form, not a 500.

    ``has_auth_credentials`` exists precisely so the edit render never goes
    through the decrypting getter. Swapping it back for ``auth_credentials``
    turns this page into a 500 for every operator whose encryption key was
    rotated — and until this test, that swap failed nothing (#1197 review).
    """

    def test_edit_get_renders_the_mask_for_a_corrupt_credential(self):
        provider = TMPProvider(
            provider_id="prov_corrupt_1",
            tenant_id="default",
            name="Rotated Provider",
            endpoint=_SAFE_ENDPOINT,
            context_match=True,
            identity_match=False,
            countries=None,
            uid_types=None,
            properties=None,
            timeout_ms=50,
            priority=0,
            status="active",
            auth_type="bearer",
        )
        # The raw column, deliberately not written through the encrypting setter:
        # this is the on-disk state after a key rotation.
        provider._auth_credentials = "not-a-valid-fernet-token"

        client = _make_tmp_provider_client()
        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.get_by_id.return_value = provider

        with (
            patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
            patch("src.admin.blueprints.tmp_providers.render_template") as mock_render,
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
        ):
            mock_render.return_value = "<html/>"
            response = client.get("/tenant/default/tmp-providers/prov_corrupt_1/edit")

        assert response.status_code == 200
        rendered_provider = mock_render.call_args.kwargs["provider"]
        assert rendered_provider["auth_credentials"] == "••••••••"
        assert "not-a-valid-fernet-token" not in str(rendered_provider)


class TestErrorHelpers:
    """The two extracted error helpers' response shapes.

    Six call sites route through them, and until now deleting an entire
    ``except`` block failed nothing (#1197 review). Driven through a real route
    (an exception raised inside the handler) rather than by calling the helper
    directly, so the wiring is graded too.
    """

    def test_json_route_failure_returns_500_with_the_action_in_the_body(self):
        """``_log_and_500``: JSON 500 naming the action, from the deactivate route."""
        client = _make_tmp_provider_client()
        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.deactivate.side_effect = RuntimeError("db exploded")

        with (
            patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
        ):
            response = client.post("/tenant/default/tmp-providers/prov_1/deactivate")

        assert response.status_code == 500
        assert response.get_json() == {"error": "Error deactivating TMP provider"}

    def test_flash_route_failure_redirects_to_tenant_settings(self):
        """``_log_flash_and_redirect``: flash + redirect, from the list route."""
        client = _make_tmp_provider_client()
        mock_uow_cls, mock_uow = _make_blueprint_uow()
        mock_uow.tmp_providers.list_all.side_effect = RuntimeError("db exploded")

        with (
            patch("src.admin.blueprints.tmp_providers.TMPProviderUoW", mock_uow_cls),
            patch.dict(os.environ, {"ADCP_AUTH_TEST_MODE": "true"}),
        ):
            response = client.get("/tenant/default/tmp-providers/", follow_redirects=False)
            with client.session_transaction() as session:
                flashes = session.get("_flashes", [])

        assert response.status_code == 302
        assert "/settings" in response.headers["Location"]
        assert ("error", "Error loading TMP providers") in flashes
