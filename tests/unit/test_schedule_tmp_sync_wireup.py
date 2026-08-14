"""Unit tests for fire_tmp_sync's no-fire behavior and internal extraction logic.

Scope boundary — read this before adding a test here:

**The positive "sync fired" case is NOT graded in this file.** That behavior is
graded end-to-end by ``TestFireTmpSyncDispatched`` and
``TestFireTmpSyncDispatchedOnUpdate`` in
``tests/integration/test_tmp_provider_integration.py``, which dispatch through
the real per-transport pipeline and assert on the outbound ``POST
/packages/sync`` body.  This file used to carry four "a thread was constructed"
positives; they asserted an in-process object no transport observes and merely
restated the dispatched tests more weakly, so they were dropped (#1197 review).

What remains, and why each is distinct:

- ``test_no_thread_when_*_raises`` — the *no-fire* contract: a failed create or
  update must not sync. A dispatched test cannot express this as sharply
  (a failed dispatch has no media_buy_id to assert absence against).
- ``TestFireTmpSyncInternals`` — the ``media_buy_id`` extraction across the
  members of the result union, and the ``if not media_buy_id or not tenant_id``
  guard. These are pure-function behaviors of ``fire_tmp_sync`` with no
  transport involved.  They construct REAL result models: extraction narrows
  the union with ``isinstance`` (so a rename is a type error rather than a
  silent switch-off, #1197 review), and a ``MagicMock`` stand-in would take the
  unrecognised-type branch instead of the one under test.

``fire_tmp_sync`` accepts a ``ResolvedIdentity`` (not a bare ``tenant_id``
string) — the tenant_id extraction is centralised inside the function.  The
internals tests below pass a mock identity to exercise that extraction path.

beads: salesagent-tmp-sync
"""

from __future__ import annotations

import logging
from unittest.mock import ANY, MagicMock, patch

from src.core.schemas import Error
from src.core.schemas._base import (
    CreateMediaBuyResult,
    CreateMediaBuySuccess,
    UpdateMediaBuyError,
    UpdateMediaBuyResult,
    UpdateMediaBuySuccess,
)


def _make_identity(tenant_id: str = "tenant-1") -> MagicMock:
    """Return a minimal ResolvedIdentity mock."""
    identity = MagicMock()
    identity.tenant_id = tenant_id
    return identity


def _make_response(media_buy_id: str = "mb-abc") -> MagicMock:
    """Return a mock response with a media_buy_id and model_dump returning a dict."""
    resp = MagicMock()
    resp.media_buy_id = media_buy_id
    resp.model_dump.return_value = {"media_buy_id": media_buy_id}
    return resp


class TestFireTmpSyncOnCreate:
    """create_media_buy_raw must NOT sync when the create failed.

    ``threading.Thread`` is observed rather than ``fire_tmp_sync`` itself
    because ``fire_tmp_sync`` is imported inside the function body, so the
    deferred import site is fragile to patch.  The positive counterpart lives in
    ``tests/integration/test_tmp_provider_integration.py`` (see module docstring).
    """

    def test_no_thread_when_create_raises(self):
        """No thread is spawned when _create_media_buy_impl raises."""
        import asyncio

        import pytest

        from src.core.tools.media_buy_create import create_media_buy_raw

        identity = _make_identity(tenant_id="tenant-1")

        mock_req = MagicMock()
        mock_req.account = None

        with (
            patch(
                "src.core.tools.media_buy_create._create_media_buy_impl",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "src.core.tools.media_buy_create._build_create_media_buy_request",
                return_value=mock_req,
            ),
            patch(
                "src.core.transport_helpers.enrich_identity_with_account",
                return_value=identity,
            ),
            patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(create_media_buy_raw(identity=identity))

        mock_thread_cls.assert_not_called()


class TestFireTmpSyncOnUpdate:
    """update_media_buy_raw must NOT sync when the update failed.

    Same rationale as TestFireTmpSyncOnCreate — we observe threading.Thread.
    """

    def test_no_thread_when_update_raises(self):
        """No thread is spawned when _update_media_buy_impl raises."""
        import pytest

        from src.core.tools.media_buy_update import update_media_buy_raw

        identity = _make_identity(tenant_id="tenant-2")

        with (
            patch(
                "src.core.tools.media_buy_update._update_media_buy_impl",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "src.core.tools.media_buy_update._build_update_request",
                return_value=MagicMock(),
            ),
            patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                update_media_buy_raw(media_buy_id="mb-update-1", identity=identity)

        mock_thread_cls.assert_not_called()


class TestFireTmpSyncInternals:
    """media_buy_id extraction across the result union, plus the tenant guard.

    ``fire_tmp_sync`` accepts ``CreateMediaBuyResult | UpdateMediaBuyResult |
    UpdateMediaBuySubmitted``.  Both envelope shapes hold the domain response in
    ``.response``, and only the ``*Success`` members carry a ``media_buy_id`` —
    so these tests build real models rather than attribute-carrying mocks.

    ``fire_tmp_sync`` accepts a ``ResolvedIdentity`` (not a bare tenant_id
    string). The identity mock's ``.tenant_id`` attribute is used for thread args.
    """

    @staticmethod
    def _create_result(media_buy_id: str) -> CreateMediaBuyResult:
        """A successful create envelope carrying *media_buy_id* on its inner response."""
        return CreateMediaBuyResult(
            status="completed",
            response=CreateMediaBuySuccess(media_buy_id=media_buy_id, packages=[]),
        )

    @staticmethod
    def _update_result(media_buy_id: str) -> UpdateMediaBuyResult:
        """A successful update envelope carrying *media_buy_id* on its inner response."""
        return UpdateMediaBuyResult(
            status="completed",
            response=UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[]),
        )

    def test_spawns_thread_for_update_result(self):
        """Update path: media_buy_id on the UpdateMediaBuySuccess inner response."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(self._update_result("mb-direct-001"), identity)

        mock_thread_cls.assert_called_once_with(
            target=ANY,
            args=("tenant-1", "mb-direct-001"),
            daemon=True,
            name=ANY,
        )
        mock_thread.start.assert_called_once_with()

    def test_spawns_thread_for_create_result(self):
        """Create path: media_buy_id on the CreateMediaBuySuccess inner response.

        CreateMediaBuyResult has no media_buy_id of its own — it wraps a
        CreateMediaBuySuccess in its .response field.
        """
        from src.services.tmp_provider_sync import fire_tmp_sync

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(self._create_result("mb-inner-001"), identity)

        mock_thread_cls.assert_called_once_with(
            target=ANY,
            args=("tenant-1", "mb-inner-001"),
            daemon=True,
            name=ANY,
        )
        mock_thread.start.assert_called_once_with()

    def test_no_thread_for_error_response(self):
        """An error inner response carries no media_buy_id, so nothing is synced."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        result = UpdateMediaBuyResult(
            status="failed",
            response=UpdateMediaBuyError(errors=[Error(code="update_failed", message="nope")]),
        )
        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(result, identity)

        mock_thread_cls.assert_not_called()

    def test_no_thread_when_response_is_none(self):
        """A None response (no result to read) syncs nothing."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(None, identity)

        mock_thread_cls.assert_not_called()

    def test_no_thread_when_identity_absent(self):
        """No thread spawned when identity is None (tenant_id cannot be resolved)."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(self._create_result("mb-001"), None)

        mock_thread_cls.assert_not_called()

    def test_no_thread_when_tenant_id_is_none_on_identity(self):
        """No thread spawned when identity.tenant_id is None."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        identity = MagicMock()
        identity.tenant_id = None

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(self._create_result("mb-001"), identity)

        mock_thread_cls.assert_not_called()

    def test_unrecognised_result_type_is_logged_not_silent(self, caplog):
        """A result type outside the union warns instead of vanishing.

        This is the regression the typed extraction exists to surface: if a
        member is renamed or a new result type reaches fire_tmp_sync, sync stops
        — and the operator gets a log line naming the type rather than silence
        (#1197 review).
        """
        from src.services.tmp_provider_sync import fire_tmp_sync

        class _NotAResult:
            pass

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            with caplog.at_level(logging.WARNING, logger="src.services.tmp_provider_sync"):
                fire_tmp_sync(_NotAResult(), identity)  # type: ignore[arg-type]

        mock_thread_cls.assert_not_called()
        assert "_NotAResult" in caplog.text

    def test_thread_targets_sync_packages_for_media_buy(self):
        """Thread is created with sync_packages_for_media_buy as target and correct args."""
        from src.services.tmp_provider_sync import fire_tmp_sync, sync_packages_for_media_buy

        identity = _make_identity("tenant-99")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(self._update_result("mb-xyz"), identity)

        mock_thread_cls.assert_called_once_with(
            target=sync_packages_for_media_buy,
            args=("tenant-99", "mb-xyz"),
            daemon=True,
            name="tmp-sync-mb-xyz",
        )
        mock_thread.start.assert_called_once_with()
