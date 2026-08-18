"""Mock ``httpx`` clients for the outbound TMP-provider calls.

Both TMP services make outbound provider calls through ``httpx`` — the package
sync synchronously (``httpx.Client.post``) and the health scheduler
asynchronously (``httpx.AsyncClient.get``). Three test files had each written
their own builder for the same context-manager-shaped mock, so a change to the
client contract meant three edits and the copies had already drifted on whether
a >= 400 status raises (CLAUDE.md DRY invariant, #1197 review).

Tests that want the sync path graded end-to-end over a real socket should use
``tests.harness._mixins.TMPSyncMixin`` instead of a mock client; these builders
are for the unit-level tests of a single function's HTTP shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx


def make_mock_http_client(status_code: int = 200) -> MagicMock:
    """A mock ``httpx.Client`` context manager whose ``.post()`` returns *status_code*.

    ``raise_for_status`` behaves like the real one: it raises
    ``httpx.HTTPStatusError`` for 4xx/5xx and is a no-op otherwise, so a test
    passing a failure status exercises the caller's error path rather than a
    silently successful mock.
    """
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.raise_for_status = MagicMock(
        side_effect=(
            httpx.HTTPStatusError(
                f"Server error {status_code}",
                request=MagicMock(),
                response=MagicMock(status_code=status_code),
            )
            if status_code >= 400
            else None
        )
    )

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    return mock_client


def make_mock_async_http_client(
    *, get_return: MagicMock | None = None, get_side_effect: Exception | None = None
) -> AsyncMock:
    """A mock ``httpx.AsyncClient`` async context manager for the health probe.

    Exactly one of *get_return* or *get_side_effect* is meaningful per call.
    """
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=get_return, side_effect=get_side_effect)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client
