"""Flask admin test client, authenticated as super admin.

Lives in ``tests/helpers/`` because it has nothing to do with any one feature.
It previously sat in ``tests/unit/_tmp_helpers.py`` — a TMP-private module — and
was imported from ``tests/unit/test_ssrf_url_validator.py``, so a non-TMP suite
depended on a feature's private helpers while roughly ten other sites hand-rolled
the same app-creation + session-setup block and would never have found it there
(#1197 review).
"""

from __future__ import annotations

from typing import Any


def make_super_admin_client() -> Any:
    """Create a Flask test client authenticated as super admin.

    The one implementation of the app-creation + session-setup block that admin
    blueprint tests need.
    """
    from src.admin.app import create_app

    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False})
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["test_user"] = "test_super_admin@example.com"
        sess["test_user_role"] = "super_admin"
        sess["authenticated"] = True
    return client
