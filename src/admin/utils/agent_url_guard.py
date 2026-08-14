"""Shared SSRF gate for admin-submitted agent URLs.

Creative agents and signals agents are both *dial sites*: the URL an admin
stores becomes the target of outbound calls (``build_creative`` /
``preview_creative`` / format fetches; signals discovery). Each blueprint gates
add, edit, and test-connection — six parallel gates in total — and every one of
them must reject identically. This module owns that rejection contract once, so
the log format, flash wording, and HTTP status cannot drift between them.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Response, flash, jsonify, redirect, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from src.core.logging_config import log_safe
from src.core.security.url_validator import check_url_ssrf, url_for_log

logger = logging.getLogger(__name__)


def reject_if_unsafe_agent_url(
    agent_url: str | None,
    *,
    agent_kind: str,
    action: str,
    redirect_endpoint: str,
    as_json: bool = False,
    **redirect_kwargs: Any,
) -> WerkzeugResponse | tuple[Response, int] | None:
    """Return a rejection response if ``agent_url`` fails the SSRF check, else ``None``.

    Args:
        agent_url: The admin-submitted (or stored) agent URL to validate.
        agent_kind: Human label for the agent family, e.g. ``"Creative agent"``.
        action: The handler being gated, e.g. ``"add"``, ``"edit"``,
            ``"test-connection"``. Used in the ``[SECURITY]`` WARNING only.
        redirect_endpoint: Flask endpoint to redirect to on rejection (HTML
            handlers). Ignored when ``as_json`` is True.
        as_json: True for the XHR test-connection handlers, which answer with
            ``({"success": False, "error": ...}, 400)`` instead of a redirect.
        **redirect_kwargs: View args forwarded to ``url_for(redirect_endpoint, ...)``.

    Returns:
        ``None`` when the URL is safe (caller proceeds), otherwise a ready-to-return
        Flask response.
    """
    is_safe, ssrf_error = check_url_ssrf(agent_url or "")
    if is_safe:
        return None

    # Both interpolated values derive from unvalidated request data: the URL is
    # rendered structure-only + percent-encoded, and the reason has its control
    # characters escaped, so neither can forge a log record.
    logger.warning(
        "[SECURITY] %s %s rejected unsafe URL %s: %s",
        agent_kind,
        action,
        url_for_log(agent_url),
        log_safe(ssrf_error),
    )
    message = f"Agent URL is not allowed: {ssrf_error}"
    if as_json:
        return jsonify({"success": False, "error": message}), 400
    flash(message, "error")
    return redirect(url_for(redirect_endpoint, **redirect_kwargs))
