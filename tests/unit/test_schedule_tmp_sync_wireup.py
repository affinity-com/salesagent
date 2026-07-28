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
- ``TestFireTmpSyncInternals`` — the ``media_buy_id`` extraction (direct vs
  inner ``.response``) and the ``if not media_buy_id or not tenant_id`` guard,
  which is a *silent* no-op on either falsy value. These are pure-function
  behaviors of ``fire_tmp_sync`` with no transport involved.

``fire_tmp_sync`` accepts a ``ResolvedIdentity`` (not a bare ``tenant_id``
string) — the tenant_id extraction is centralised inside the function.  The
internals tests below pass a mock identity to exercise that extraction path.

beads: salesagent-tmp-sync
"""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch


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
    """Unit tests for fire_tmp_sync media_buy_id extraction and thread-spawn logic.

    The function must handle two response shapes:
    - ``CreateMediaBuyResult`` wrapper: media_buy_id is on ``.response.media_buy_id``
    - ``UpdateMediaBuySuccess | UpdateMediaBuyError``: media_buy_id is directly on the object

    ``fire_tmp_sync`` accepts a ``ResolvedIdentity`` (not a bare tenant_id string).
    The identity mock's ``.tenant_id`` attribute is used for thread args.
    """

    def test_spawns_thread_for_direct_media_buy_id(self):
        """Update path: media_buy_id directly on response — thread is spawned."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        resp = MagicMock()
        resp.media_buy_id = "mb-direct-001"
        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(resp, identity)

        mock_thread_cls.assert_called_once_with(
            target=ANY,
            args=("tenant-1", "mb-direct-001"),
            daemon=True,
            name=ANY,
        )
        mock_thread.start.assert_called_once_with()

    def test_spawns_thread_for_inner_response_media_buy_id(self):
        """Create path: media_buy_id on .response.media_buy_id — thread is spawned.

        CreateMediaBuyResult has no direct media_buy_id — it wraps a
        CreateMediaBuySuccess in its .response field.
        """
        from src.services.tmp_provider_sync import fire_tmp_sync

        inner = MagicMock()
        inner.media_buy_id = "mb-inner-001"

        class _WrapperWithNoMediaBuyId:
            """Minimal stand-in for CreateMediaBuyResult (no media_buy_id attribute)."""

            response = inner

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(_WrapperWithNoMediaBuyId(), identity)

        mock_thread_cls.assert_called_once_with(
            target=ANY,
            args=("tenant-1", "mb-inner-001"),
            daemon=True,
            name=ANY,
        )
        mock_thread.start.assert_called_once_with()

    def test_no_thread_when_media_buy_id_absent(self):
        """No thread spawned when neither direct nor inner response has media_buy_id."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        class _NoIdResponse:
            """Response with no media_buy_id anywhere."""

        identity = _make_identity("tenant-1")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(_NoIdResponse(), identity)

        mock_thread_cls.assert_not_called()

    def test_no_thread_when_identity_absent(self):
        """No thread spawned when identity is None (tenant_id cannot be resolved)."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        resp = MagicMock()
        resp.media_buy_id = "mb-001"

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(resp, None)

        mock_thread_cls.assert_not_called()

    def test_no_thread_when_tenant_id_is_none_on_identity(self):
        """No thread spawned when identity.tenant_id is None."""
        from src.services.tmp_provider_sync import fire_tmp_sync

        resp = MagicMock()
        resp.media_buy_id = "mb-001"
        identity = MagicMock()
        identity.tenant_id = None

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            fire_tmp_sync(resp, identity)

        mock_thread_cls.assert_not_called()

    def test_thread_targets_sync_packages_for_media_buy(self):
        """Thread is created with sync_packages_for_media_buy as target and correct args."""
        from src.services.tmp_provider_sync import fire_tmp_sync, sync_packages_for_media_buy

        resp = MagicMock()
        resp.media_buy_id = "mb-xyz"
        identity = _make_identity("tenant-99")

        with patch("src.services.tmp_provider_sync.threading.Thread") as mock_thread_cls:
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread
            fire_tmp_sync(resp, identity)

        mock_thread_cls.assert_called_once_with(
            target=sync_packages_for_media_buy,
            args=("tenant-99", "mb-xyz"),
            daemon=True,
            name="tmp-sync-mb-xyz",
        )
        mock_thread.start.assert_called_once_with()
