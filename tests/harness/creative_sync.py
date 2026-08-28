"""CreativeSyncEnv — integration test environment for _sync_creatives_impl.

Patches: creative agent registry, run_async_in_sync_context, notifications, audit, config.
Real: get_db_session, CreativeRepository, all validation/processing (all hit real DB).

Requires: integration_db fixture (creates test PostgreSQL DB).

Usage::

    @pytest.mark.requires_db
    def test_something(self, integration_db):
        with CreativeSyncEnv() as env:
            tenant = TenantFactory(tenant_id="t1")
            principal = PrincipalFactory(tenant=tenant, principal_id="p1")

            response = env.call_impl(creatives=[{
                "creative_id": "c1",
                "name": "Test Creative",
                "format_id": {"id": "display_300x250", "agent_url": "..."},
                "media_url": "https://example.com/img.png",
            }])
            assert len(response.results) == 1

Generative creative usage::

    with CreativeSyncEnv() as env:
        env.setup_default_data()
        fmt = env.setup_generative_build(
            format_id="gen_banner",
            build_result={"status": "draft", "context_id": "ctx-1", "creative_output": {}},
        )
        result = env.call_via(transport, creatives=[{
            "creative_id": "c1",
            "name": "Gen Creative",
            "format_id": fmt,
            "assets": build_assets(
                text_spec("message", content="Build me a banner")
            ),
        }])

Available mocks via env.mock:
    "registry"           -- get_creative_agent_registry (lazy import in _sync.py)
    "run_async"          -- run_async_in_sync_context (module-level import in _sync.py)
    "send_notifications" -- _send_creative_notifications (from _workflow)
    "audit_log"          -- _audit_log_sync (from _workflow)
    "config"             -- get_config (lazy import in _processing.py)
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.core.creative_agent_registry import GenerativeBuildResult
from src.core.schemas import SyncCreativesResponse
from tests.factories.format import make_generative_format
from tests.harness._base import IntegrationEnv
from tests.harness._realize import E2EUnsupportedSetup, realize_e2e

# Sink for the production error mapper's log calls in set_build_creative_sdk_error.
_harness_logger = logging.getLogger(__name__)


# A generative format the PINNED reference agent serves (present in
# tests/fixtures/creative_formats/reference_formats.json with a non-empty
# output_format_ids). Defaulting to a real catalog format is what makes the
# generative setup realizable over e2e instead of an impl-only escape hatch:
# under ADCP_TESTING the live server resolves formats from the same fixture.
REFERENCE_GENERATIVE_FORMAT_ID = "display_300x250_generative"

# What the in-process double returns from build_creative. Mirrors the shape the
# real registry produces (status / context_id / creative_output).
DEFAULT_GENERATIVE_BUILD_RESULT: dict[str, Any] = {
    "status": "draft",
    "context_id": "ctx-test-123",
    "creative_output": {
        "assets": {"headline": {"asset_type": "text", "content": "Generated headline"}},
        "output_format": {"url": "https://generated.example.com/creative.html"},
    },
}


def _realize_generative_build(
    env: Any,
    format_id: str = REFERENCE_GENERATIVE_FORMAT_ID,
    agent_url: str | None = None,
    build_result: dict[str, Any] | None = None,
    gemini_api_key: str = "test-gemini-key",
) -> dict[str, str]:
    """E2E realization of setup_generative_build: validate against the live catalog.

    Twin of ``CreativeFormatsEnv._validate_registry_formats``: the live stack
    serves the reference catalog by construction (``ADCP_TESTING`` reads the same
    fixture), and its registry serves a derived ``build_creative`` result under
    the same flag — so there is no per-scenario server registry to write, only an
    intent to validate.

    - a generative format from the reference catalog -> no-op: the server already
      serves it, and its own ``ADCP_TESTING`` build branch answers the call.
    - a format the catalog does not serve as generative -> unrealizable: name it
      and point at the fixture-refresh path.
    - a scenario-specific ``build_result`` -> unrealizable: the live server serves
      the result IT derives, so a canned response cannot be injected.
    """
    from src.core.format_cache import load_reference_formats

    if build_result is not None:
        raise E2EUnsupportedSetup(
            "a scenario-specific build_result cannot be injected over e2e: the live server serves the "
            "build result its own ADCP_TESTING branch derives. Assert on that result instead of pinning one."
        )

    generative_ids = {fmt.format_id.id for fmt in load_reference_formats() if getattr(fmt, "output_format_ids", None)}
    if format_id not in generative_ids:
        raise E2EUnsupportedSetup(
            f"{format_id!r} is not a generative format in the reference catalog "
            f"(generative formats: {sorted(generative_ids)}). Register it with the creative agent "
            "and refresh the fixture (`make creative-formats-refresh`)."
        )

    env.mock["config"].return_value.gemini_api_key = gemini_api_key
    return {"agent_url": agent_url or env.DEFAULT_AGENT_URL, "id": format_id}


class CreativeSyncEnv(IntegrationEnv):
    """Integration test environment for _sync_creatives_impl.

    Only mocks external services (creative agent registry, async runner,
    notifications, audit logging). Everything else is real:
    - Real get_db_session -> real DB queries
    - Real CreativeRepository -> real DB writes
    - Real validation/processing -> real business logic
    """

    EXTERNAL_PATCHES = {
        "registry": "src.core.creative_agent_registry.get_creative_agent_registry",
        "run_async": "src.core.tools.creatives._sync.run_async_in_sync_context",
        "send_notifications": "src.core.tools.creatives._sync._send_creative_notifications",
        "audit_log": "src.core.tools.creatives._sync._audit_log_sync",
        "config": "src.core.config.get_config",
    }
    DEFAULT_AGENT_URL = "https://creative.test.example.com"
    REST_ENDPOINT = "/api/v1/creatives/sync"

    def _configure_mocks(self) -> None:
        """Set up happy-path defaults for external mocks."""
        # Registry: return a mock that supports list_all_formats() + get_format()
        mock_registry = MagicMock()
        mock_registry.list_all_formats.return_value = []
        # get_format must return a coroutine (consumed by run_async_in_sync_context
        # in _validation.py). Return a truthy value to pass format existence check.
        mock_registry.get_format = AsyncMock(return_value={"id": "display_300x250", "name": "Display 300x250"})
        # build_creative and preview_creative must be AsyncMock because
        # _processing.py uses the REAL run_async_in_sync_context (not patched there).
        # build_creative's production return type is GenerativeBuildResult | None;
        # None is the real registry's "agent returned no payload" value.
        mock_registry.build_creative = AsyncMock(return_value=None)
        mock_registry.preview_creative = AsyncMock(return_value={})
        self.mock["registry"].return_value = mock_registry

        # run_async: execute the coroutine synchronously (return empty list)
        self.mock["run_async"].side_effect = lambda coro: []

        # Notifications: no-op
        self.mock["send_notifications"].return_value = None

        # Audit log: no-op
        self.mock["audit_log"].return_value = None

        # Config: default with no gemini key (safe for static creatives)
        mock_config = MagicMock()
        mock_config.gemini_api_key = None
        self.mock["config"].return_value = mock_config

    @realize_e2e(_realize_generative_build)
    def setup_generative_build(
        self,
        format_id: str = REFERENCE_GENERATIVE_FORMAT_ID,
        agent_url: str | None = None,
        build_result: dict[str, Any] | None = None,
        gemini_api_key: str = "test-gemini-key",
    ) -> dict[str, str]:
        """Configure harness for generative creative testing.

        Sets up:
        - The reference catalog's generative format (real ``Format``, non-empty
          ``output_format_ids`` — production routes on that attribute, and a
          ``Mock`` would auto-create it as truthy)
        - ``build_creative`` returning the typed ``GenerativeBuildResult`` the real
          registry returns
        - gemini_api_key on the config mock
        - run_async to return the generative format list

        Returns a format_id dict for use in creative payloads::

            fmt = env.setup_generative_build()
            creative = {"creative_id": "c1", "name": "Test", "format_id": fmt, ...}

        The default format is a format the PINNED reference agent actually serves
        (see :data:`REFERENCE_GENERATIVE_FORMAT_ID`), so the same setup is
        realizable over e2e — the live stack runs with ``ADCP_TESTING=true``, under
        which the registry serves the same reference catalog and a derived
        ``build_creative`` result. See :func:`_realize_generative_build`.
        """
        agent = agent_url or self.DEFAULT_AGENT_URL

        generative_format = make_generative_format(format_id, agent_url=agent)

        # Configure run_async to return this format for list_all_formats
        self.set_run_async_result([generative_format])

        registry = self.mock["registry"].return_value
        self.set_build_creative_result(build_result or DEFAULT_GENERATIVE_BUILD_RESULT)

        # Also configure get_format to return this format for validation
        registry.get_format = AsyncMock(return_value=generative_format)

        # Set gemini API key
        self.mock["config"].return_value.gemini_api_key = gemini_api_key

        return {"agent_url": agent, "id": format_id}

    def set_build_creative_result(self, build_result: dict[str, Any] | GenerativeBuildResult | None) -> None:
        """Make ``build_creative`` return *build_result*, as the real registry would.

        The production ``CreativeAgentRegistry.build_creative`` returns a
        ``GenerativeBuildResult`` model (or ``None`` when the agent returned no
        payload), so a double that returns a raw dict lets a test pass against a
        contract production does not have. A dict is validated into the model
        here — the convenience of writing the response as a literal without the
        double drifting from the type the caller actually receives.
        """
        result = GenerativeBuildResult.model_validate(build_result) if isinstance(build_result, dict) else build_result
        self.mock["registry"].return_value.build_creative = AsyncMock(return_value=result)

    def set_build_creative_side_effect(self, side_effect: Any) -> None:
        """Make ``build_creative`` run *side_effect* — an exception or a callable.

        The one place the double's failure behaviour is configured, so
        :meth:`set_build_creative_error` and the callable-based configurators
        (e.g. one that renders the manifest through production so the raised
        error is the REAL one) share a single seam.
        """
        self.mock["registry"].return_value.build_creative = AsyncMock(side_effect=side_effect)

    def set_build_creative_error(self, error: BaseException) -> None:
        """Make ``build_creative`` raise *error* on the generative path.

        Use after :meth:`setup_generative_build`. The real ``CreativeAgentRegistry``
        raises the *internal* typed ``AdCPError`` taxonomy from ``build_creative``
        (it translates the SDK's ``ADCPError`` via ``raise_mapped_adcp_error``), so
        pass an internal ``AdCPError`` to exercise recovery classification, or a
        bare ``Exception`` for the unknown-failure fallback.
        """
        self.set_build_creative_side_effect(error)

    def set_build_creative_sdk_error(self, sdk_error: Any) -> None:
        """Make ``build_creative`` fail exactly as the real registry does for an SDK error.

        Routes *sdk_error* (an ``adcp.exceptions.ADCPError``) through the production
        ``raise_mapped_adcp_error`` — the same call the real ``build_creative``
        makes in its ``except ADCPError`` arm — so a test pins the whole
        SDK-error → internal-typed-error → wire-recovery chain rather than only the
        half after the mapping.
        """
        from src.core.helpers.adapter_helpers import raise_mapped_adcp_error

        def _raise_mapped(*_args: Any, **_kwargs: Any) -> None:
            raise_mapped_adcp_error(sdk_error, agent_label="creative agent test", logger=_harness_logger)

        self.set_build_creative_side_effect(_raise_mapped)

    def set_run_async_result(self, formats: list[Any]) -> None:
        """Configure run_async_in_sync_context to return *formats*.

        Unlike CreativeFormatsEnv.set_registry_formats (which patches
        registry.list_all_formats directly), this patches the sync bridge
        that wraps the async call in _sync.py.
        """
        self.mock["run_async"].side_effect = lambda coro: formats

    def call_impl(self, **kwargs: Any) -> SyncCreativesResponse:
        """Call _sync_creatives_impl with real DB.

        Accepts all _sync_creatives_impl kwargs. The 'identity' kwarg
        defaults to self.identity if not provided.

        If 'account' is present, resolves it via enrich_identity_with_account
        (same as the transport wrappers do) before calling _impl.
        """
        from src.core.tools.creatives._sync import _sync_creatives_impl

        self._commit_factory_data()
        kwargs.setdefault("identity", self.identity)
        kwargs.setdefault("creatives", [])

        # Handle account kwarg — resolve at boundary, same as wrappers
        account = kwargs.pop("account", None)
        if account is not None:
            from src.core.transport_helpers import enrich_identity_with_account

            kwargs["identity"] = enrich_identity_with_account(kwargs["identity"], account)

        return _sync_creatives_impl(**kwargs)

    def call_internal_impl(self, **kwargs: Any) -> SyncCreativesResponse:
        """Call ``_sync_creatives_impl`` including its orchestration-only inputs.

        Same impl as :meth:`call_impl`; this entry point exists to name the intent
        of a test that passes a keyword-only, orchestration-only input (currently
        ``media_buy_brand``, supplied in production by
        ``process_and_upload_package_creatives``). Those inputs are not on the wire
        contract, so there is no transport equivalent to compare against.
        """
        return self.call_impl(**kwargs)

    def call_a2a(self, **kwargs: Any) -> SyncCreativesResponse:
        """Dispatch through the REAL A2A handler (message → skill → Task/Artifact).

        Not ``sync_creatives_raw``: the ``_raw`` wrapper skips
        ``AdCPRequestHandler.on_message_send``, so the A2A cases produced no wire
        bytes at all — ``_run_a2a_handler`` is the only writer of
        ``_last_wire_response``, which the A2A dispatcher surfaces as
        ``result.wire_response``. Every A2A assertion on the serialized response
        was therefore vacuous while looking identical to the graded REST/MCP ones.
        """
        kwargs.setdefault("creatives", [])
        return self._run_a2a_handler("sync_creatives", SyncCreativesResponse, **kwargs)

    def call_mcp(self, **kwargs: Any) -> SyncCreativesResponse:
        """Call sync_creatives via Client(mcp) — full pipeline dispatch.

        No enum coercion needed — FastMCP's TypeAdapter handles it automatically.
        """
        kwargs.setdefault("creatives", [])
        return self._run_mcp_client("sync_creatives", SyncCreativesResponse, **kwargs)

    def build_rest_body(self, **kwargs: Any) -> dict[str, Any]:
        """Convert kwargs to SyncCreativesBody shape for REST POST."""
        # The REST body expects 'creatives' as list[dict], matching SyncCreativesBody
        body: dict[str, Any] = {}
        if "creatives" in kwargs:
            creatives = kwargs["creatives"]
            # Convert Pydantic models to dicts if needed
            body["creatives"] = [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in creatives]
        if "assignments" in kwargs and kwargs["assignments"] is not None:
            body["assignments"] = kwargs["assignments"]
        if "creative_ids" in kwargs and kwargs["creative_ids"] is not None:
            body["creative_ids"] = kwargs["creative_ids"]
        if "delete_missing" in kwargs:
            body["delete_missing"] = kwargs["delete_missing"]
        if "dry_run" in kwargs:
            body["dry_run"] = kwargs["dry_run"]
        if "validation_mode" in kwargs:
            body["validation_mode"] = kwargs["validation_mode"]
        if "account" in kwargs and kwargs["account"] is not None:
            account = kwargs["account"]
            body["account"] = account.model_dump(mode="json") if hasattr(account, "model_dump") else account
        if "push_notification_config" in kwargs and kwargs["push_notification_config"] is not None:
            pnc = kwargs["push_notification_config"]
            body["push_notification_config"] = pnc.model_dump(mode="json") if hasattr(pnc, "model_dump") else pnc
        return body

    def parse_rest_response(self, data: dict[str, Any]) -> SyncCreativesResponse:
        """Parse REST JSON into SyncCreativesResponse."""
        return SyncCreativesResponse(**data)
