"""Shared HTTP helpers for outbound TMP Provider calls.

Both the health-check scheduler (``tmp_health_scheduler.py``) and the package
sync service (``tmp_provider_sync.py``) make HTTP calls to TMP Provider
endpoints.  Centralising the URL-building and auth-header helpers here ensures
every outbound call inherits the same hardening (trailing-slash normalisation,
``follow_redirects=False``) rather than each call site re-implementing
it independently.
"""

from __future__ import annotations

from typing import TypedDict

# Default timeout for synchronous package-sync calls (seconds).
# Kept short — TMP Provider is an internal service on the same network.
# Named with the *_SECONDS suffix (matching HEALTH_CHECK_TIMEOUT_SECONDS,
# HEALTH_CHECK_INTERVAL_SECONDS, STATUS_CHECK_INTERVAL_SECONDS) so a grep for
# *_SECONDS finds every duration constant this feature touches.
_DEFAULT_SYNC_TIMEOUT_SECONDS = 5.0


def provider_url(endpoint: str, path: str) -> str:
    """Build a full URL for a TMP Provider path.

    Strips any trailing slash from *endpoint* before joining so callers
    don't need to remember to normalise the stored value.

    Args:
        endpoint: Base endpoint URL as stored in the DB (e.g. ``"http://tmp:3000/"``).
        path: Path to append (e.g. ``"/packages/sync"`` or ``"/health"``).
    """
    return endpoint.rstrip("/") + path


#: The auth schemes an outbound TMP Provider call can actually make.
#:
#: One entry, and that is the point: the registration's ``auth_type`` used to be
#: an unconstrained ``str`` whose admin form offered "API Key" while this module
#: always emitted ``Authorization: Bearer`` regardless — selecting a scheme
#: changed nothing (#1197 review). The vocabulary now lives where the behaviour
#: is, ``TMPProviderRegistration.auth_type`` is typed from it, and
#: :func:`provider_auth_headers` dispatches on it, so adding a scheme means adding
#: a branch here rather than an option to a template.
PROVIDER_AUTH_SCHEMES: frozenset[str] = frozenset({"bearer"})


def provider_auth_headers(auth_type: str | None, auth_credentials: str) -> dict[str, str]:
    """Build the auth headers for one outbound TMP Provider request.

    Returns an empty dict when the provider has no credential — an
    unauthenticated provider is a supported registration. A credential with no
    explicit ``auth_type`` is sent as Bearer: that is the only scheme implemented,
    and it is what every previously-stored registration already got.

    An ``auth_type`` outside :data:`PROVIDER_AUTH_SCHEMES` cannot reach here from
    any write surface (the record types the field), so it is a programming error
    rather than operator input — hence a raise, not a silent fallback that would
    reintroduce "the selected scheme is ignored".
    """
    if not auth_credentials:
        return {}
    scheme = auth_type or "bearer"
    if scheme not in PROVIDER_AUTH_SCHEMES:
        raise ValueError(
            f"Unsupported TMP provider auth scheme {scheme!r}; expected one of {sorted(PROVIDER_AUTH_SCHEMES)}"
        )
    return {"Authorization": f"Bearer {auth_credentials}"}


class ProviderClientKwargs(TypedDict):
    """The exact ``httpx`` client kwargs every outbound TMP Provider call sets.

    A closed two-key contract, so the ``**``-unpack into ``httpx.Client(...)``
    is checked: ``dict[str, Any]`` gave the call sites nothing to check against
    and left the key set documented only in prose (#1197 review).
    """

    timeout: float
    follow_redirects: bool


def provider_client_kwargs(timeout: float = _DEFAULT_SYNC_TIMEOUT_SECONDS) -> ProviderClientKwargs:
    """Return shared ``httpx.Client`` / ``httpx.AsyncClient`` constructor kwargs.

    Centralises the two flags that every outbound TMP Provider call must set:

    - ``follow_redirects=False`` — prevents SSRF via open-redirect on both the
      GET (health probe) and POST (package sync) sides.  This flag was forgotten
      once on the POST side (round 7) and must never be omitted again.
    - ``timeout`` — callers may override for async health probes (which use a
      different constant) but the default matches the sync package-sync timeout.

    Usage::

        import httpx
        from src.services._provider_http import provider_client_kwargs

        # Sync (package sync):
        with httpx.Client(**provider_client_kwargs()) as client:
            resp = client.post(url, json=payloads, headers=headers)

        # Async (health scheduler) — override timeout:
        async with httpx.AsyncClient(**provider_client_kwargs(timeout=5)) as client:
            resp = await client.get(health_url)
    """
    return ProviderClientKwargs(timeout=timeout, follow_redirects=False)
