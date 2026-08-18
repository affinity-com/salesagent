"""TMP Provider package sync service.

Pushes package definitions from the Sales Agent to all active TMP Providers
for a tenant whenever a media buy is created or updated.

Per the AdCP TMP spec (Package Sync section):
  "Package metadata is synced from seller agents to TMP providers at media buy
   creation time and whenever the media buy materially changes."

Each synced AvailablePackage includes a seller_agent reference so the TMP
Provider can attribute offers back to the originating seller agent.

Design principles (AdCP Pattern compliance):
- Triggered from **every transport** (MCP, A2A, REST) via ``fire_tmp_sync()``,
  which spawns a daemon thread so the caller is never blocked.
- Never called from _impl functions (which must remain transport-agnostic).
- Reads packages and provider endpoints via **repositories** (UoW pattern) —
  no raw get_db_session() / select() calls.
- HTTP calls are made **after** the DB session is closed — no open transaction
  during network I/O.
- Failures are **logged with full context** and re-raised as warnings so the
  background task runner records them.  The media buy operation itself is
  unaffected (fire-and-forget at the transport boundary).

beads: salesagent-tmp-sync
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

import httpx
from adcp.types import AvailablePackage, SellerAgentReference

from src.core.database.models import MediaPackage, TMPProvider
from src.core.database.repositories.uow import MediaBuyUoW, TenantConfigUoW, TMPProviderUoW
from src.core.schemas._base import (
    CreateMediaBuyResult,
    CreateMediaBuySuccess,
    UpdateMediaBuyResult,
    UpdateMediaBuySubmitted,
    UpdateMediaBuySuccess,
)
from src.core.security.url_validator import is_local_host, sanitize_for_log
from src.core.thread_registry import ThreadRegistry
from src.services._provider_http import bearer_headers, provider_client_kwargs, provider_url

if TYPE_CHECKING:
    from src.core.resolved_identity import ResolvedIdentity

logger = logging.getLogger(__name__)

# Log-sanitization rule across the TMP surfaces (this module,
# tmp_health_scheduler, the admin blueprint, the discovery route): a value goes
# through ``sanitize_for_log`` (CWE-117) when it enters the process from outside
# — operator form input (provider ``endpoint``/``name``), env (``ADCP_AGENT_URL``),
# or a request path (the discovery route's ``tenant_id``). Values that are only
# ever DB-resolved inside the process are logged raw; here that is ``tenant_id``
# and ``media_buy_id``, which reach this module from ResolvedIdentity and the
# media_buys table, never from a caller-controlled string.


#: In-flight package syncs, keyed by ``media_buy_id``.
#:
#: The shared seam (``src/core/thread_registry.py``, #1264) rather than a bare
#: ``threading.Thread(...).start()``: five services had hand-rolled the dict +
#: lock + reaper before it existed, and this was the sixth site to do so
#: (#1197 review).  What the registry buys here is ordering — see
#: :func:`fire_tmp_sync` — plus a deterministic completion signal
#: (``_active_syncs.get(media_buy_id).join()``) for callers and tests, which is
#: what replaces patching ``threading.Thread``.
_active_syncs = ThreadRegistry()

#: How long a sync waits for the previous sync of the SAME media buy.
#: A cap, not a correctness knob: exceeding it means the predecessor is wedged
#: (a hung provider connection), and the newer package data is more valuable
#: than strict ordering against a thread that may never finish.
_PREDECESSOR_JOIN_TIMEOUT_SECONDS = 60


def fire_tmp_sync(
    response: CreateMediaBuyResult | UpdateMediaBuyResult | UpdateMediaBuySubmitted | None,
    identity: ResolvedIdentity | None,
) -> None:
    """Spawn a daemon thread to sync TMP packages after a successful media buy operation.

    Transport-agnostic entry point shared by MCP, A2A, and REST transports —
    the sole trigger for ``sync_packages_for_media_buy``.  There is deliberately
    no route-layer trigger: adding one (e.g. FastAPI ``BackgroundTasks`` in
    ``api_v1.py``) would double-fire the sync on REST, since REST already reaches
    this function through the ``_raw`` wrapper.

    ``response`` is whatever the two ``_impl`` functions return:
    ``CreateMediaBuyResult`` (create path) or
    ``UpdateMediaBuyResult | UpdateMediaBuySubmitted`` (update path).  The id is
    read by ``_extract_media_buy_id`` as a typed attribute after narrowing the
    union, never by attribute name — see that function.

    Keep this union in step with those two return annotations: it is what caught
    the update path's type change when the pin moved to adcp 6.6.0 / spec 3.1.1
    (``UpdateMediaBuySuccess | UpdateMediaBuyError`` became
    ``UpdateMediaBuyResult | UpdateMediaBuySubmitted``), which ``response: Any``
    would have swallowed.

    ``identity`` is a ``ResolvedIdentity`` — ``tenant_id`` is extracted here so
    callers don't need to repeat ``identity.tenant_id if identity else None`` at
    every call site (four transport wrappers).

    Two rapid operations on the SAME media buy are **serialized**, not raced:
    each sync joins the one already in flight for that media_buy_id before
    reading the database, so the last operation to fire is the last to POST and
    "every provider holds current package data" is a property of the code rather
    than of thread scheduling.  Superseding (dropping the second sync) would
    publish the older package set; racing publishes whichever thread happens to
    finish last.  ``_active_syncs`` holds the newest thread per media buy, which
    is therefore also the last to finish — so ``get(media_buy_id).join()`` is a
    complete completion signal (#1197 review).

    Boundedness (a pool rather than a thread per fire) remains the separately
    accepted follow-up; ordering is what the registry makes expressible.

    No-ops when ``media_buy_id`` or ``tenant_id`` is absent (e.g. on error or
    submitted responses, which carry no ID); every no-op is logged.
    """
    tenant_id = identity.tenant_id if identity is not None else None

    media_buy_id = _extract_media_buy_id(response)

    if not media_buy_id or not tenant_id:
        if media_buy_id and not tenant_id:
            logger.warning(
                "[TMP sync] Skipping sync for media_buy=%s — no tenant on the resolved identity",
                media_buy_id,
            )
        return

    # Read the predecessor BEFORE registering ourselves: `add` is
    # last-writer-wins, so registering first would make us our own predecessor.
    predecessor = _active_syncs.get(media_buy_id)
    t = threading.Thread(
        target=_run_sync,
        args=(tenant_id, media_buy_id, predecessor),
        daemon=True,
        name=f"tmp-sync-{media_buy_id}",
    )
    _active_syncs.add(media_buy_id, t)
    t.start()


def _run_sync(tenant_id: str, media_buy_id: str, predecessor: threading.Thread | None) -> None:
    """Registry-managed body of one sync: serialize behind *predecessor*, then sync.

    Joining the predecessor here (rather than in :func:`fire_tmp_sync`) keeps the
    caller non-blocking — the transport wrapper returns as soon as the thread is
    spawned, exactly as before.
    """
    try:
        if predecessor is not None and predecessor.is_alive():
            predecessor.join(timeout=_PREDECESSOR_JOIN_TIMEOUT_SECONDS)
            if predecessor.is_alive():
                logger.warning(
                    "[TMP sync] Previous sync for media_buy=%s still running after %ds — "
                    "proceeding, provider ordering for this media buy is not guaranteed",
                    media_buy_id,
                    _PREDECESSOR_JOIN_TIMEOUT_SECONDS,
                )
        sync_packages_for_media_buy(tenant_id, media_buy_id)
    finally:
        # Only drop the entry if it is still ours: a newer fire may already have
        # replaced it, and that thread is the one callers must be able to join.
        if _active_syncs.get(media_buy_id) is threading.current_thread():
            _active_syncs.remove(media_buy_id)


def join_active_syncs(timeout: float = 30.0) -> list[str]:
    """Wait for every in-flight package sync to finish; return the keys still running.

    The feature's observation seam.  ``fire_tmp_sync`` is fire-and-forget by
    design, so without this a caller (an operator draining a worker, a test
    asserting on what a provider received) has nothing to wait on and has to
    reach for ``patch("threading.Thread")`` — an in-process artifact no
    transport observes, which is what every test tier ended up doing
    independently (#1197 review).

    Because ``_active_syncs`` holds the NEWEST thread per media buy and each
    sync serializes behind its predecessor, joining the registered threads joins
    the whole chain.

    Returns the media_buy_ids whose sync was still running when *timeout*
    expired — empty on a clean drain.
    """
    stragglers: list[str] = []
    for key in _active_syncs.list_active():
        thread = _active_syncs.get(key)
        if thread is None:
            continue
        thread.join(timeout=timeout)
        if thread.is_alive():
            stragglers.append(key)
    return stragglers


def _extract_media_buy_id(
    response: CreateMediaBuyResult | UpdateMediaBuyResult | UpdateMediaBuySubmitted | None,
) -> str | None:
    """Read ``media_buy_id`` off a media-buy result as a *typed* attribute.

    The union is narrowed with ``isinstance`` and the id is read from the
    concrete member, so renaming ``media_buy_id`` on any member is a type-check
    error here.  The previous ``getattr(response, "media_buy_id", None)`` dodged
    the union entirely: a rename would have switched TMP sync off on all four
    transports with no error and no log line — the exact regression the union
    annotation exists to prevent (#1197 review).

    Only the ``*Success`` members carry an id.  ``*Error`` and ``*Submitted``
    have no ``media_buy_id`` field at all, so "no id" is the correct, expected
    outcome there — logged at DEBUG.  An unrecognised type is a contract drift
    rather than an expected shape, so it is logged at WARNING instead of
    vanishing.

    ``CreateMediaBuyResult`` / ``UpdateMediaBuyResult`` are ``TaskResultEnvelope``
    shapes: they serialize flat but store the domain response in ``.response``,
    so the id lives on the inner model, not the envelope.
    """
    if response is None:
        return None

    if isinstance(response, CreateMediaBuyResult | UpdateMediaBuyResult):
        inner = response.response
        if isinstance(inner, CreateMediaBuySuccess | UpdateMediaBuySuccess):
            return inner.media_buy_id
        logger.debug(
            "[TMP sync] No media_buy_id on %s.response (%s) — skipping sync",
            type(response).__name__,
            type(inner).__name__,
        )
        return None

    if isinstance(response, UpdateMediaBuySubmitted):
        logger.debug("[TMP sync] Update submitted for async completion — no media_buy_id yet, skipping sync")
        return None

    logger.warning(
        "[TMP sync] Unrecognised media-buy result type %s — skipping sync. "
        "Add it to fire_tmp_sync's union and to _extract_media_buy_id.",
        type(response).__name__,
    )
    return None


def _resolve_seller_agent_url(tenant_id: str) -> str | None:
    """Resolve the seller agent URL for the AvailablePackage.seller_agent field.

    Per ``adcp/_schemas/3.1/core/seller-agent-ref.json``, ``agent_url``
    MUST use the ``https://`` scheme.  Returns ``None`` when no valid https URL
    can be resolved so the caller can skip the sync rather than emit a
    spec-invalid binding.

    Resolution order:
      1. ADCP_AGENT_URL env var (explicit override for non-standard deployments)
         — validated to use https:// like the virtual_host path; a non-https
         override is rejected (logged, falls through) rather than emitted.
      2. Tenant virtual_host (the public domain, e.g. "tenant.salesagent.example.com")
         — local hosts (localhost / *.localhost / 127.0.0.1) are skipped because
         they cannot produce a valid https URL.
      3. Returns None — caller logs and skips sync.

    IMPORTANT: this opens its own UoW/session. Callers MUST NOT invoke this
    function from inside another open UoW block (e.g. MediaBuyUoW) — nesting
    two UoWs means the inner UoW's __exit__ closes/removes the scoped session
    the outer block is still using (get_db_session() is a scoped session).
    sync_packages_for_media_buy() resolves the seller_agent URL before
    opening the MediaBuyUoW block for exactly this reason.
    """
    override = os.environ.get("ADCP_AGENT_URL")
    if override:
        override = override.rstrip("/")
        if override.startswith("https://"):
            return override
        logger.error(
            "[TMP sync] ADCP_AGENT_URL=%s does not use https:// — ignoring override "
            "(adcp/_schemas/3.1/core/seller-agent-ref.json requires https for agent_url). "
            "Falling back to tenant virtual_host resolution.",
            sanitize_for_log(override),
        )

    # Load tenant to resolve virtual_host.
    # Uses TenantConfigUoW for architecture compliance (no raw get_db_session).
    try:
        with TenantConfigUoW(tenant_id) as uow:
            assert uow.tenant_config is not None
            tenant = uow.tenant_config.get_tenant()
            if tenant and tenant.virtual_host:
                host = tenant.virtual_host
                if not is_local_host(host):
                    return f"https://{host}/mcp"
    except Exception:
        logger.warning(
            "[TMP sync] Failed to load tenant %s for seller_agent URL",
            tenant_id,
            exc_info=True,
        )

    # No valid https URL available — the spec requires https for agent_url.
    # Log an error and return None so the caller skips the sync rather than
    # emitting a spec-invalid binding that providers will reject.
    logger.error(
        "[TMP sync] Cannot resolve a valid https seller_agent URL for tenant=%s "
        "(ADCP_AGENT_URL not set and no public virtual_host configured). "
        "Set ADCP_AGENT_URL to the public https MCP endpoint to enable TMP sync.",
        tenant_id,
    )
    return None


def _build_package_payload(
    media_buy_id: str,
    pkg_row: MediaPackage,
    seller_agent_url: str,
) -> dict[str, Any]:
    """Build the POST /packages/sync payload from a MediaPackage DB row.

    The body is produced by the pinned SDK's ``AvailablePackage`` — the codegen
    of ``adcp/_schemas/3.1/trusted-match/available-package.json`` — rather than
    a hand-written TypedDict copy.  The schema is closed
    (``additionalProperties: false``) and requires exactly ``package_id``,
    ``media_buy_id``, ``seller_agent``; ``format_ids`` and ``catalogs`` are the
    optional members.  Constructing the model validates the shape here, and a
    spec bump that renames or adds a required field becomes a construction
    error instead of silently shipping the old body (#1197 review).

    ``seller_agent`` is ``SellerAgentReference``
    (``adcp/_schemas/3.1/core/seller-agent-ref.json``): ``{"agent_url": ...}``,
    whose ``agent_url`` MUST use the ``https://`` scheme.  Callers must resolve a
    valid https URL before calling this (see ``_resolve_seller_agent_url``); an
    invalid one raises out of the model rather than reaching a provider.

    ``mode="json"`` renders ``AnyUrl`` as a plain string so the result is
    directly JSON-serializable; ``exclude_none=True`` drops the optional
    members (and ``seller_agent.id``, reserved and unpopulated per the spec)
    so the emitted object stays inside the closed key set.
    """
    return AvailablePackage(
        package_id=pkg_row.package_id,
        media_buy_id=media_buy_id,
        # seller_agent is required by the schema; agent_url MUST be https.
        seller_agent=SellerAgentReference(agent_url=seller_agent_url),
    ).model_dump(mode="json", exclude_none=True)


def _post_packages_sync(endpoint: str, payloads: list[dict[str, Any]], auth_credentials: str = "") -> None:
    """POST /packages/sync to a single TMP Provider endpoint.

    Sends the full list as a JSON array.  The TMP Provider's handler accepts
    both a single object and an array (see handlers_packages.go).

    Auth: Bearer token — when auth_credentials is set, sends
    ``Authorization: Bearer <credentials>``.  The TMP Provider resolves
    the tenant server-side from the credential.

    ``follow_redirects=False`` prevents SSRF via open-redirect on the POST
    side (matching the GET-side guard in the health probe).

    Raises httpx.HTTPError on non-2xx responses so the caller can log and
    continue to the next provider.
    """
    url = provider_url(endpoint, "/packages/sync")
    headers = bearer_headers(auth_credentials)
    with httpx.Client(**provider_client_kwargs()) as client:
        resp = client.post(url, json=payloads, headers=headers)
        resp.raise_for_status()
    # Sync fires on every media-buy create/update; keep at DEBUG (failures stay
    # at WARNING in sync_packages_for_media_buy's fan-out loop below).
    logger.debug(
        "[TMP sync] POST %s → %d (%d package(s), auth=%s)",
        sanitize_for_log(url),
        resp.status_code,
        len(payloads),
        "bearer" if auth_credentials else "none",
    )


def _readable_providers(provider_rows: list[TMPProvider], tenant_id: str) -> list[tuple[str, str, str]]:
    """Materialise ``(name, endpoint, credential)`` per provider, skipping unreadable ones.

    The unit of work is one provider, so the failure handling is per provider:
    :attr:`TMPProvider.auth_credentials` decrypts on read and raises
    ``AdCPConfigurationError`` on a ciphertext the current key cannot open — a
    key-rotation state, not a corrupt database.  A list comprehension over the
    whole set turned that single row into "no providers synced for this tenant",
    logged as a repository failure, so one provider's rotated credential silently
    stopped the sync for every other provider the tenant had registered
    (#1197 review).

    Runs inside the caller's UoW block: the attribute reads (and the decrypt)
    need a live session.
    """
    providers: list[tuple[str, str, str]] = []
    for p in provider_rows:
        try:
            credential = p.auth_credentials or ""
        except Exception:
            # Named per provider, and at WARNING like the fan-out failures — an
            # operator reading this line must be able to tell WHICH registration
            # to re-enter the credential for.
            logger.warning(
                "[TMP sync] Skipping provider '%s' (%s) for tenant=%s — its stored auth credential "
                "could not be read (re-enter it in the admin UI); other providers are unaffected",
                sanitize_for_log(p.name),
                sanitize_for_log(p.endpoint),
                tenant_id,
                exc_info=True,
            )
            continue
        providers.append((p.name, p.endpoint, credential))
    return providers


def sync_packages_for_media_buy(tenant_id: str, media_buy_id: str) -> None:
    """Background task: push all packages for a media buy to active TMP providers.

    Called from the four transport entry points (MCP create/update wrappers and
    A2A+REST ``_raw`` wrappers) via ``fire_tmp_sync()``, which spawns a daemon
    thread so the caller is never blocked.

    Steps:
      1. Resolve seller_agent URL from tenant config (its own UoW, opened and
         closed BEFORE the MediaBuyUoW block — see note below).
      2. Load packages from media_packages table via MediaBuyRepository.
      3. Load active/draining TMP provider endpoints via TMPProviderRepository,
         materialised into plain tuples before the UoW block closes.
      4. POST /packages/sync to each provider (best-effort, errors logged).

    Args:
        tenant_id:    Tenant scope — used for both repository queries.
        media_buy_id: The media buy whose packages should be synced.
    """
    # --- Step 1: resolve seller_agent URL BEFORE opening MediaBuyUoW ---
    # _resolve_seller_agent_url() opens its own TenantConfigUoW. get_db_session()
    # is a scoped session, so nesting it inside another open UoW block means the
    # inner UoW's __exit__ closes/removes the session the outer block still
    # needs — the subsequent row access and outer commit then run against a
    # removed session. Resolving it here, before MediaBuyUoW opens, avoids the
    # nesting entirely.
    #
    # Returns None when no valid https URL is available (spec requires https for
    # seller_agent.agent_url). Skip sync rather than emit a spec-invalid binding.
    seller_agent_url = _resolve_seller_agent_url(tenant_id)
    if seller_agent_url is None:
        logger.warning(
            "[TMP sync] Skipping sync for media_buy=%s tenant=%s — no valid https seller_agent URL. "
            "Set ADCP_AGENT_URL to enable TMP sync.",
            media_buy_id,
            tenant_id,
        )
        return

    # --- Step 2: load packages and build payloads (inside session scope) ---
    # Payloads are built while the session is still open so that ORM attribute
    # access (pkg_row.package_id) does not hit a detached instance.
    # HTTP calls happen after this block — no open transaction during network I/O.
    try:
        with MediaBuyUoW(tenant_id) as uow:
            assert uow.media_buys is not None
            pkg_rows = uow.media_buys.get_packages(media_buy_id)

            if not pkg_rows:
                logger.debug(
                    "[TMP sync] No packages found for media_buy_id=%s — skipping sync",
                    media_buy_id,
                )
                return

            payloads = [_build_package_payload(media_buy_id, row, seller_agent_url) for row in pkg_rows]
    except Exception:
        logger.exception(
            "[TMP sync] Failed to load packages for media_buy_id=%s tenant=%s",
            media_buy_id,
            tenant_id,
        )
        return

    # Sync fires on every media-buy create/update — DEBUG matches the poll-path
    # per-cycle summaries (tmp_health_scheduler); failures below stay at WARNING.
    logger.debug(
        "[TMP sync] Built %d package payload(s) for media_buy=%s seller_agent=%s",
        len(payloads),
        media_buy_id,
        sanitize_for_log(seller_agent_url),
    )

    # --- Step 3: load active + draining TMP provider endpoints ---
    # Draining providers still serve in-flight requests and need current package data.
    # The router stops sending NEW requests to draining providers, but packages must
    # stay up-to-date for requests already in the pipeline.
    #
    # Materialise into plain tuples INSIDE the UoW block — provider.endpoint /
    # provider.auth_credentials / provider.name are ORM attributes that expire
    # on commit (default expire_on_commit=True). Reading them after the `with`
    # block closes hits a detached session and raises DetachedInstanceError,
    # which then repeats in the except-handler's own attribute reads below.
    try:
        with TMPProviderUoW(tenant_id) as uow:
            assert uow.tmp_providers is not None
            providers = _readable_providers(uow.tmp_providers.list_syncable(), tenant_id)
    except Exception:
        logger.exception(
            "[TMP sync] Failed to load TMP providers for tenant=%s",
            tenant_id,
        )
        return

    if not providers:
        logger.debug(
            "[TMP sync] No active TMP providers for tenant=%s — skipping sync",
            tenant_id,
        )
        return

    # --- Step 4: fan out to each provider (best-effort) ---
    for provider_name, provider_endpoint, provider_auth_credentials in providers:
        try:
            _post_packages_sync(provider_endpoint, payloads, provider_auth_credentials)
        except Exception:
            # Log with full context but do NOT re-raise — one provider failure
            # must not block the others.  The media buy is already committed.
            logger.warning(
                "[TMP sync] Failed to sync %d package(s) to provider '%s' (%s) for tenant=%s media_buy=%s",
                len(payloads),
                sanitize_for_log(provider_name),
                sanitize_for_log(provider_endpoint),
                tenant_id,
                media_buy_id,
                exc_info=True,
            )
