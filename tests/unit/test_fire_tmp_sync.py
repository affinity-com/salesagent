"""Unit tests for ``fire_tmp_sync``'s no-fire contract and its extraction helper.

Scope boundary — read this before adding a test here:

**The positive "sync fired and the provider got the packages" case is NOT here.**
That is one BDD scenario (``tests/bdd/features/local-tmp-package-sync.feature``)
fanned out over a2a/mcp/rest and e2e_rest through the env-owned seam
(``tests.harness._mixins.TMPSyncMixin``). This file previously carried "a
``threading.Thread`` was constructed with these kwargs" positives: an in-process
artifact no transport observes, and a strictly weaker restatement of the
dispatched tests on the same code path (#1197 review).

What remains, and why each is distinct:

- ``TestWrapperDoesNotFireOnFailure`` — the *no-fire* contract: a create or
  update that raised must not sync. A dispatched scenario cannot express this as
  sharply (a failed dispatch has no media_buy_id to assert absence against).
- ``TestExtractMediaBuyId`` — ``_extract_media_buy_id`` graded as the pure
  function it is: its return value per member of the result union. That pins the
  rename regression the typed extraction exists to prevent far more directly than
  a constructor call — a member renaming ``media_buy_id`` fails on the returned
  value, not on mock call kwargs.
- ``TestFireGuard`` — the ``if not media_buy_id or not tenant_id`` guard, observed
  on the production registry (``_active_syncs``): "no sync was registered" is a
  seam the feature owns, unlike ``threading.Thread``.

beads: salesagent-tmp-sync
"""

from __future__ import annotations

import logging

import pytest

from src.core.schemas import Error
from src.core.schemas._base import (
    CreateMediaBuyError,
    CreateMediaBuyResult,
    CreateMediaBuySuccess,
    UpdateMediaBuyError,
    UpdateMediaBuyResult,
    UpdateMediaBuySubmitted,
    UpdateMediaBuySuccess,
)
from src.services.tmp_provider_sync import _active_syncs, _extract_media_buy_id, fire_tmp_sync
from tests.harness import make_identity


def _create_result(media_buy_id: str) -> CreateMediaBuyResult:
    """A successful create envelope carrying *media_buy_id* on its inner response."""
    return CreateMediaBuyResult(
        status="completed",
        response=CreateMediaBuySuccess(media_buy_id=media_buy_id, packages=[]),
    )


def _update_result(media_buy_id: str) -> UpdateMediaBuyResult:
    """A successful update envelope carrying *media_buy_id* on its inner response."""
    return UpdateMediaBuyResult(
        status="completed",
        response=UpdateMediaBuySuccess(media_buy_id=media_buy_id, affected_packages=[]),
    )


class TestWrapperDoesNotFireOnFailure:
    """A create/update that raised must not trigger a package sync.

    ``fire_tmp_sync`` is observed at its module attribute: the wrappers import it
    inside the function body, so the deferred import resolves the patched
    attribute at call time.
    """

    def test_create_wrapper_does_not_fire_when_impl_raises(self):
        import asyncio
        from unittest.mock import MagicMock, patch

        from src.core.tools.media_buy_create import create_media_buy_raw

        identity = make_identity(tenant_id="tenant_1")
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
            patch("src.core.transport_helpers.enrich_identity_with_account", return_value=identity),
            patch("src.services.tmp_provider_sync.fire_tmp_sync") as mock_fire,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(create_media_buy_raw(identity=identity))

        mock_fire.assert_not_called()

    def test_update_wrapper_does_not_fire_when_impl_raises(self):
        from unittest.mock import MagicMock, patch

        from src.core.tools.media_buy_update import update_media_buy_raw

        identity = make_identity(tenant_id="tenant_2")

        with (
            patch(
                "src.core.tools.media_buy_update._update_media_buy_impl",
                side_effect=RuntimeError("boom"),
            ),
            patch("src.core.tools.media_buy_update._build_update_request", return_value=MagicMock()),
            patch("src.services.tmp_provider_sync.fire_tmp_sync") as mock_fire,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                update_media_buy_raw(media_buy_id="mb-update-1", identity=identity)

        mock_fire.assert_not_called()


class TestExtractMediaBuyId:
    """``_extract_media_buy_id`` returns the id per union member, or None.

    The union is narrowed with ``isinstance`` and the id read off the concrete
    member, so a rename is a type error rather than a silently disabled sync.
    Real models (never attribute-carrying mocks) — a mock would take the
    unrecognised-type branch instead of the one under test.
    """

    def test_create_success_returns_the_inner_id(self):
        assert _extract_media_buy_id(_create_result("mb_inner_001")) == "mb_inner_001"

    def test_update_success_returns_the_inner_id(self):
        assert _extract_media_buy_id(_update_result("mb_direct_001")) == "mb_direct_001"

    def test_none_response_returns_none(self):
        assert _extract_media_buy_id(None) is None

    def test_create_error_returns_none(self):
        result = CreateMediaBuyResult(
            status="failed",
            response=CreateMediaBuyError(errors=[Error(code="create_failed", message="nope")]),
        )
        assert _extract_media_buy_id(result) is None

    def test_update_error_returns_none(self):
        result = UpdateMediaBuyResult(
            status="failed",
            response=UpdateMediaBuyError(errors=[Error(code="update_failed", message="nope")]),
        )
        assert _extract_media_buy_id(result) is None

    def test_submitted_update_returns_none(self):
        """A submitted update has no media_buy_id yet — "no id" is the correct outcome."""
        assert _extract_media_buy_id(UpdateMediaBuySubmitted(status="submitted", task_id="task_1")) is None

    def test_unrecognised_type_warns_instead_of_vanishing(self, caplog):
        """A result type outside the union warns and returns None.

        The regression the typed extraction exists to surface: a new or renamed
        result type stops the sync, and the operator gets a line naming the type
        rather than silence (#1197 review).
        """

        class _NotAResult:
            pass

        with caplog.at_level(logging.WARNING, logger="src.services.tmp_provider_sync"):
            assert _extract_media_buy_id(_NotAResult()) is None  # type: ignore[arg-type]

        assert "_NotAResult" in caplog.text


class TestFireGuard:
    """``fire_tmp_sync`` registers a sync only when it has both ids.

    Observed on ``_active_syncs`` — the production registry — rather than on a
    patched ``threading.Thread``: "no sync is in flight for this media buy" is
    what the guard means, and the registry is where the feature says it.
    """

    @staticmethod
    def _registered_keys() -> set[str]:
        return set(_active_syncs.list_active())

    def test_no_sync_registered_when_response_is_none(self):
        before = self._registered_keys()
        fire_tmp_sync(None, make_identity(tenant_id="tenant_1"))
        assert self._registered_keys() == before

    def test_no_sync_registered_when_identity_absent(self):
        before = self._registered_keys()
        fire_tmp_sync(_create_result("mb_guard_1"), None)
        assert self._registered_keys() == before
        assert "mb_guard_1" not in self._registered_keys()

    def test_no_sync_registered_when_tenant_id_is_none(self, caplog):
        """An identity with no tenant is logged, not silently dropped."""
        before = self._registered_keys()
        with caplog.at_level(logging.WARNING, logger="src.services.tmp_provider_sync"):
            fire_tmp_sync(_create_result("mb_guard_2"), make_identity(tenant_id=None))

        assert self._registered_keys() == before
        assert "mb_guard_2" in caplog.text

    def test_no_sync_registered_for_an_error_response(self):
        result = UpdateMediaBuyResult(
            status="failed",
            response=UpdateMediaBuyError(errors=[Error(code="update_failed", message="nope")]),
        )
        before = self._registered_keys()
        fire_tmp_sync(result, make_identity(tenant_id="tenant_1"))
        assert self._registered_keys() == before
