"""TMP Provider management blueprint for admin UI.

Trusted Match Protocol providers are buyer-side agents that the TMP Router
fans out to during ad selection. Each provider evaluates context signals
and/or identity signals against their synced package set and returns scored
offers. The TMP Router calls all active providers in parallel within the
configured latency budget, then merges results for the publisher-side join.

The Sales Agent exposes active registrations via the FastAPI discovery
endpoint (``GET /tenant/{tenant_id}/tmp-providers/discovery``) so the
router can poll for discovery — it never reads the DB directly.

The Flask blueprint here handles the **admin CRUD UI** only.  The
machine-to-machine discovery endpoint lives in ``src/routes/tmp_providers.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from src.admin.utils import require_tenant_access
from src.admin.utils.audit_decorator import log_admin_action
from src.core.database.repositories.uow import TMPProviderUoW
from src.core.schemas.tmp_provider import TMPProviderFields, TMPProviderRegistration, TMPProviderValidationError

logger = logging.getLogger(__name__)

# Create Blueprint
tmp_providers_bp = Blueprint("tmp_providers", __name__)


# ---------------------------------------------------------------------------
# Guard helpers (DRY: used by multiple route handlers)
# ---------------------------------------------------------------------------


def _log_and_500(action: str, exc: Exception):
    """Log *exc* at ERROR level and return a JSON 500 response.

    Collapses the copy-pasted ``except Exception as e: logger.error(...)``
    blocks in JSON-returning route handlers into one call site.  The *action*
    string appears in the log message and the JSON error body so operators can
    identify which handler failed.

    ``[TMP admin]`` prefix matches the sync/health/discovery modules' tags
    (``[TMP sync]``, ``[TMP health]``, ``[TMP discovery]``) so an operator
    grepping ``[TMP`` for this feature's logs sees the admin-side lines too.

    Usage::

        except Exception as e:
            return _log_and_500("deactivating TMP provider", e)
    """
    logger.error("[TMP admin] Error %s: %s", action, exc, exc_info=True)
    return jsonify({"error": f"Error {action}"}), 500


def _log_flash_and_redirect(action: str, exc: Exception, redirect_response):
    """Log *exc* at ERROR level, flash an error message, and return *redirect_response*.

    Collapses the copy-pasted ``except Exception as e: logger.error(...); flash(...);
    return redirect(...)`` blocks in flash-based route handlers into one call site.

    ``[TMP admin]`` prefix matches the sync/health/discovery modules' tags —
    see ``_log_and_500`` above.

    Usage::

        except Exception as e:
            return _log_flash_and_redirect(
                "loading TMP providers", e,
                redirect(url_for("tenants.tenant_settings", tenant_id=tenant_id)),
            )
    """
    logger.error("[TMP admin] Error %s: %s", action, exc, exc_info=True)
    flash(f"Error {action}", "error")
    return redirect_response


def _tenant_not_found_redirect():
    """Flash an error and redirect to the index when a tenant cannot be found.

    Returns the redirect response so callers can ``return`` it directly::

        tenant = uow.tenant_config.get_tenant()
        if not tenant:
            return _tenant_not_found_redirect()
    """
    flash("Tenant not found", "error")
    return redirect(url_for("core.index"))


def _provider_not_found_json():
    """Return a JSON 404 response when a TMP provider cannot be found.

    Returns the response so callers can ``return`` it directly::

        provider = uow.tmp_providers.get_by_id(provider_id)
        if not provider:
            return _provider_not_found_json()
    """
    return jsonify({"error": "TMP provider not found"}), 404


def _provider_not_found_redirect(tenant_id: str):
    """Flash an error and redirect to the provider list when a TMP provider cannot be found.

    Returns the redirect response so callers can ``return`` it directly::

        provider = uow.tmp_providers.get_by_id(provider_id)
        if not provider:
            return _provider_not_found_redirect(tenant_id)
    """
    flash("TMP provider not found", "error")
    return redirect(url_for("tmp_providers.list_tmp_providers", tenant_id=tenant_id))


# ---------------------------------------------------------------------------
# Shared form validation helper (DRY: used by both add and edit routes)
# ---------------------------------------------------------------------------


def _parse_int_field(form: Mapping[str, str], field: str, default: str, label: str) -> int:
    """Parse *field* as an int, raising ``TMPProviderValidationError`` on bad input.

    Collapses the two identical ``try: int(form.get(...)) except (ValueError,
    TypeError)`` blocks (timeout_ms, priority) into one call site.  Raises the
    same domain error the registration model raises, so the caller has one
    rejection path rather than two.
    """
    try:
        return int(form.get(field, default))
    except (ValueError, TypeError) as exc:
        raise TMPProviderValidationError(f"{label} must be a numeric value") from exc


def _split_csv(raw: str, *, upper: bool = False) -> list[str] | None:
    """Split a comma-separated form field into a list, or ``None`` when empty.

    ``None`` (not ``[]``) is the "accepts all" sentinel used by the DB columns
    and the discovery contract, so an empty field must not become an empty
    list.  *upper* uppercases each entry (ISO 3166-1 alpha-2 country codes).
    """
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if upper:
        items = [item.upper() for item in items]
    return items or None


def _validate_provider_form(form: Mapping[str, str]) -> TMPProviderRegistration:
    """Parse TMP provider form data into a validated :class:`TMPProviderRegistration`.

    This function owns **form shape only** — CSV splitting, checkbox ``"on"``,
    int parsing, whitespace stripping.  Registration *validity* (the uid-type
    and status enums, the at-least-one-match-mode rule, the
    ``identity_match ⇒ countries + uid_types`` rule, the SSRF check) belongs to
    ``TMPProviderRegistration`` in ``src/core/schemas/tmp_provider.py`` so that
    a future programmatic write surface enforces the same rules without
    reimplementing them (#1197 review).

    ``form`` is typed as ``Mapping[str, str]`` — callers pass
    ``request.form``, a Werkzeug ``ImmutableMultiDict``, not a plain ``dict``.
    Only ``.get()`` is used here, so the narrower ``Mapping`` contract is
    what's actually relied on.

    Raises:
        TMPProviderValidationError: on any form-shape or registration-validity
            rejection; ``str(exc)`` is the operator-facing message to flash.
    """
    return TMPProviderRegistration.parse(
        TMPProviderFields(
            name=form.get("name", "").strip(),
            endpoint=form.get("endpoint", "").strip(),
            context_match=form.get("context_match") == "on",
            identity_match=form.get("identity_match") == "on",
            countries=_split_csv(form.get("countries", "").strip(), upper=True),
            uid_types=_split_csv(form.get("uid_types", "").strip()),
            properties=_split_csv(form.get("properties", "").strip()),
            timeout_ms=_parse_int_field(form, "timeout_ms", "50", "Timeout (ms)"),
            priority=_parse_int_field(form, "priority", "0", "Priority"),
            status=form.get("status", "active").strip(),
            auth_type=form.get("auth_type", "").strip() or None,
            auth_credentials=form.get("auth_credentials", "").strip() or None,
        )
    )


@tmp_providers_bp.route("/")
@require_tenant_access()
def list_tmp_providers(tenant_id):
    """List all TMP providers for a tenant."""
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tenant_config is not None
            assert uow.tmp_providers is not None
            tenant = uow.tenant_config.get_tenant()
            if not tenant:
                return _tenant_not_found_redirect()

            providers = uow.tmp_providers.list_all()

            providers_list = []
            for p in providers:
                # include_conditional=False: always emit countries/uid_types/properties
                # (None means "accepts all") — same contract as the edit path and the
                # discovery endpoint. Avoids manually overwriting the same three fields
                # that to_dict(include_conditional=False) already handles.
                entry = p.to_dict(include_conditional=False)
                entry["created_at"] = p.created_at
                providers_list.append(entry)

            return render_template(
                "tmp_providers.html",
                tenant=tenant,
                tenant_id=tenant_id,
                tenant_name=tenant.name,
                providers=providers_list,
            )

    except Exception as e:
        return _log_flash_and_redirect(
            "loading TMP providers",
            e,
            redirect(url_for("tenants.tenant_settings", tenant_id=tenant_id)),
        )


@tmp_providers_bp.route("/add", methods=["GET", "POST"])
@log_admin_action("add_tmp_provider")
@require_tenant_access()
def add_tmp_provider(tenant_id):
    """Add a new TMP provider."""
    if request.method == "GET":
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tenant_config is not None
            tenant = uow.tenant_config.get_tenant()
            if not tenant:
                return _tenant_not_found_redirect()

            return render_template(
                "tmp_provider_form.html",
                tenant=tenant,
                tenant_id=tenant_id,
                tenant_name=tenant.name,
                provider=None,
            )

    # POST — create new TMP provider
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tenant_config is not None
            assert uow.tmp_providers is not None
            tenant = uow.tenant_config.get_tenant()
            if not tenant:
                return _tenant_not_found_redirect()

            # Caught here, not by the handler's generic `except Exception` below:
            # a validation rejection must flash its own operator-facing message,
            # not the generic "Error adding TMP provider".
            try:
                registration = _validate_provider_form(request.form)
            except TMPProviderValidationError as exc:
                flash(str(exc), "error")
                return redirect(url_for("tmp_providers.add_tmp_provider", tenant_id=tenant_id))

            # create_from_fields is symmetric with update_fields used in the edit path:
            # both take the same TMPProviderFields record without inline ORM construction.
            uow.tmp_providers.create_from_fields(**registration.to_fields())

            flash(f"TMP provider '{registration.name}' added successfully", "success")
            return redirect(url_for("tmp_providers.list_tmp_providers", tenant_id=tenant_id))

    except Exception as e:
        return _log_flash_and_redirect(
            "adding TMP provider",
            e,
            redirect(url_for("tmp_providers.add_tmp_provider", tenant_id=tenant_id)),
        )


@tmp_providers_bp.route("/<provider_id>/edit", methods=["GET", "POST"])
@log_admin_action("edit_tmp_provider")
@require_tenant_access()
def edit_tmp_provider(tenant_id, provider_id):
    """Edit an existing TMP provider."""
    if request.method == "GET":
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tenant_config is not None
            assert uow.tmp_providers is not None
            tenant = uow.tenant_config.get_tenant()
            if not tenant:
                return _tenant_not_found_redirect()

            provider = uow.tmp_providers.get_by_id(provider_id)
            if not provider:
                return _provider_not_found_redirect(tenant_id)

            provider_dict = provider.to_dict(include_conditional=False)
            # Form fields need comma-separated strings, not lists
            provider_dict["countries"] = ",".join(provider.countries or [])
            provider_dict["uid_types"] = ",".join(provider.uid_types or [])
            provider_dict["properties"] = ",".join(provider.properties or [])
            # Auth fields are not in to_dict() (sensitive / not part of TMP Router contract).
            # Render a placeholder instead of the plaintext credential so it is never
            # echoed back to the browser — the POST side preserves the existing value
            # when the field is left empty.
            provider_dict["auth_type"] = provider.auth_type
            provider_dict["auth_credentials"] = "••••••••" if provider._auth_credentials else ""

            return render_template(
                "tmp_provider_form.html",
                tenant=tenant,
                tenant_id=tenant_id,
                tenant_name=tenant.name,
                provider=provider_dict,
            )

    # POST — update TMP provider
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            provider = uow.tmp_providers.get_by_id(provider_id)
            if not provider:
                return _provider_not_found_redirect(tenant_id)

            # See the add handler: caught before the generic `except Exception`
            # so the specific rejection message reaches the operator.
            try:
                registration = _validate_provider_form(request.form)
            except TMPProviderValidationError as exc:
                flash(str(exc), "error")
                return redirect(
                    url_for("tmp_providers.edit_tmp_provider", tenant_id=tenant_id, provider_id=provider_id)
                )

            # auth_credentials is written only when a new non-empty value was
            # submitted — otherwise the stored (encrypted) value is preserved.
            # to_update_fields() owns that rule so the edit handler no longer
            # hand-copies eleven field names to express it.
            uow.tmp_providers.update_fields(
                provider_id,
                **registration.to_update_fields(include_credentials=bool(registration.auth_credentials)),
            )

            flash(f"TMP provider '{registration.name}' updated successfully", "success")
            return redirect(url_for("tmp_providers.list_tmp_providers", tenant_id=tenant_id))

    except Exception as e:
        return _log_flash_and_redirect(
            "updating TMP provider",
            e,
            redirect(url_for("tmp_providers.edit_tmp_provider", tenant_id=tenant_id, provider_id=provider_id)),
        )


@tmp_providers_bp.route("/<provider_id>/deactivate", methods=["POST"])
@log_admin_action("deactivate_tmp_provider")
@require_tenant_access()
def deactivate_tmp_provider(tenant_id, provider_id):
    """Soft-deactivate a TMP provider (set status='inactive')."""
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            provider = uow.tmp_providers.deactivate(provider_id)
            if not provider:
                return _provider_not_found_json()

            return jsonify({"success": True, "message": f"TMP provider '{provider.name}' deactivated"})

    except Exception as e:
        return _log_and_500("deactivating TMP provider", e)


@tmp_providers_bp.route("/<provider_id>/delete", methods=["DELETE"])
@log_admin_action("delete_tmp_provider")
@require_tenant_access()
def delete_tmp_provider(tenant_id, provider_id):
    """Hard-delete a TMP provider."""
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            # Get name before deleting for the response message
            provider = uow.tmp_providers.get_by_id(provider_id)
            if not provider:
                return _provider_not_found_json()

            provider_name = provider.name
            uow.tmp_providers.delete(provider_id)

            return jsonify({"success": True, "message": f"TMP provider '{provider_name}' deleted successfully"})

    except Exception as e:
        return _log_and_500("deleting TMP provider", e)


@tmp_providers_bp.route("/<provider_id>/health", methods=["GET"])
@log_admin_action("health_check_tmp_provider")
@require_tenant_access()
def health_check_tmp_provider(tenant_id, provider_id):
    """Return the last health-check result from the background scheduler.

    The TMP health scheduler polls each provider's ``/health`` endpoint
    every 60 s and writes the result to ``health_status`` /
    ``last_health_checked_at``.  This route reads from the DB — no live
    HTTP call, no worker starvation risk.
    """
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            provider = uow.tmp_providers.get_by_id(provider_id)
            if not provider:
                return _provider_not_found_json()

            if provider.health_status is None:
                return jsonify(
                    {
                        "success": True,
                        "status": "pending",
                        "provider": provider.name,
                        "message": "Health check has not run yet",
                    }
                )

            return jsonify(
                {
                    "success": provider.health_status == "healthy",
                    "status": provider.health_status,
                    "provider": provider.name,
                    "last_checked": (
                        provider.last_health_checked_at.isoformat() if provider.last_health_checked_at else None
                    ),
                }
            )

    except Exception as e:
        return _log_and_500("checking TMP provider health", e)
