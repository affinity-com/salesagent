"""Domain mixins — shared fluent API for integration and unit test environments.

Each mixin provides the domain-specific helper methods (set_*, call_*, get_*)
that are identical across integration and unit variants. Concrete env classes
inherit from both a base (BaseTestEnv or IntegrationEnv) and a mixin.

Mixins don't define ``__init__`` — concrete classes set up required state.
Mixins may call ``self._commit_factory_data()`` which is a no-op in unit mode.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from src.adapters.mock_ad_server import simulate_breakdowns
from src.core.schemas import (
    AdapterGetMediaBuyDeliveryResponse,
    AdapterPackageDelivery,
    DeliveryTotals,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsResponse,
    ReportingPeriod,
)
from src.core.schemas import GetProductsRequest as GetProductsRequestGenerated
from src.core.tools.media_buy_delivery import _get_media_buy_delivery_impl
from src.core.tools.products import _get_products_impl
from src.core.webhook_delivery import WebhookDelivery, deliver_webhook_with_retry
from src.services.webhook_delivery_service import (
    CircuitBreaker,
    WebhookDeliveryService,
)
from tests.harness._realize import e2e_unsupported, realize_e2e

# Patch target for send-time SSRF gate in CircuitBreakerEnv (unit + integration).
OUTBOUND_SSRF_VALIDATE_TARGET = "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url"
# Shared EXTERNAL_PATCHES fragment — both CircuitBreakerEnv variants merge this.
SSRF_EXTERNAL_PATCH: dict[str, str] = {"ssrf": OUTBOUND_SSRF_VALIDATE_TARGET}


def _persist_simulation_config(env: Any, resp: AdapterGetMediaBuyDeliveryResponse) -> Any:
    """E2E realization of a delivery-poll adapter response (#1418).

    Persists the same ``AdapterGetMediaBuyDeliveryResponse`` the in-process
    branch would inject on the MagicMock as a ``DeliverySimulationConfig`` row in
    the live server's DB, where the server's Mock adapter reads it. Uses the
    env's server-bound session (rebound to the server engine in e2e mode) via
    the tenant-scoped repository, then commits so the HTTP request sees it.

    The repository ``upsert`` writes only the simulation-config row (never a
    tenant row), so the seeded tenant from the discovery-path / Given step is
    left intact.
    """
    from src.core.database.repositories.delivery_simulation import (
        DeliverySimulationConfigRepository,
    )

    repo = DeliverySimulationConfigRepository(env._session, env._tenant_id)
    row = repo.upsert(resp.media_buy_id, resp.model_dump(mode="json"))
    env._commit_factory_data()
    return row


def make_adapter_update_side_effect() -> Any:
    """Return a side_effect for a mocked ``adapter.update_media_buy``.

    Produces an ``UpdateMediaBuySuccess`` echoing the media_buy_id from the
    call and a resolved ``implementation_date``, mirroring the mock adapter's
    own ``update_media_buy`` return (mock_ad_server.update_media_buy). Used by
    MediaBuyDualEnv to wire the update-path adapter mock.
    """
    from src.core.schemas._base import UpdateMediaBuySuccess

    def _update_response(*args: Any, **kwargs: Any) -> UpdateMediaBuySuccess:
        media_buy_id = kwargs.get("media_buy_id") or (args[0] if args else "")
        today = kwargs.get("today") or datetime.now(UTC)
        return UpdateMediaBuySuccess(
            media_buy_id=media_buy_id,
            affected_packages=[],
            implementation_date=today,
        )

    return _update_response


class DeliveryPollMixin:
    """Shared fluent API for delivery poll testing.

    Requires concrete class to set ``self._adapter_responses: dict`` in __init__.
    """

    _adapter_responses: dict[str, AdapterGetMediaBuyDeliveryResponse]

    def _configure_adapter_mock(self) -> None:
        """Wire adapter mock with side_effect lookup. Call from _configure_mocks."""
        mock_adapter = MagicMock()
        mock_adapter.get_media_buy_delivery.side_effect = self._adapter_lookup
        self.mock["adapter"].return_value = mock_adapter  # type: ignore[attr-defined]

    def _adapter_lookup(self, *args: Any, **kwargs: Any) -> AdapterGetMediaBuyDeliveryResponse:
        """Look up configured adapter response by media_buy_id.

        Raises KeyError for unregistered IDs when other IDs are registered,
        preventing tests from silently succeeding with wrong data.
        """
        mb_id = kwargs.get("media_buy_id") or (args[0] if args else None)
        if mb_id and mb_id in self._adapter_responses:
            return self._adapter_responses[mb_id]
        if self._adapter_responses:
            raise KeyError(
                f"No adapter response registered for media_buy_id={mb_id!r}. "
                f"Registered: {list(self._adapter_responses.keys())}. "
                f"Call env.set_adapter_response({mb_id!r}, ...) first."
            )
        return self._make_default_adapter_response()

    @staticmethod
    def _build_adapter_delivery(
        media_buy_id: str,
        impressions: int,
        spend: float,
        package_id: str,
        clicks: int | None,
        packages: list[dict[str, Any]] | None,
        conversions: float | None = None,
        conversion_value: float | None = None,
    ) -> AdapterGetMediaBuyDeliveryResponse:
        """Normalize set_adapter_response params into the delivery intent.

        Shared by both transports: the in-process branch injects this object on
        the MagicMock; the e2e branch persists its wire dump. Single source of
        the packages-list-vs-scalars + totals-auto-sum logic.
        """
        if packages is not None:
            by_package = [
                AdapterPackageDelivery(
                    package_id=p["package_id"],
                    impressions=p.get("impressions", 0),
                    spend=p.get("spend", 0.0),
                )
                for p in packages
            ]
            total_impressions = float(sum(p.get("impressions", 0) for p in packages))
            total_spend = float(sum(p.get("spend", 0.0) for p in packages))
            totals = DeliveryTotals(impressions=total_impressions, spend=total_spend)
        else:
            simulated_geo, simulated_device_type = simulate_breakdowns(float(impressions), float(spend))
            by_package = [
                AdapterPackageDelivery(
                    package_id=package_id,
                    impressions=impressions,
                    spend=spend,
                    by_geo=simulated_geo,
                    by_device_type=simulated_device_type,
                )
            ]
            totals = DeliveryTotals(impressions=float(impressions), spend=spend)

        if clicks is not None:
            totals.clicks = float(clicks)
        if conversions is not None:
            totals.conversions = float(conversions)
        if conversion_value is not None:
            totals.conversion_value = float(conversion_value)

        return AdapterGetMediaBuyDeliveryResponse(
            media_buy_id=media_buy_id,
            reporting_period=ReportingPeriod(
                start=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2025, 12, 31, tzinfo=UTC),
            ),
            totals=totals,
            by_package=by_package,
            currency="USD",
        )

    def set_adapter_response(
        self,
        media_buy_id: str = "mb_001",
        impressions: int = 5000,
        spend: float = 250.0,
        package_id: str = "pkg_001",
        clicks: int | None = None,
        packages: list[dict[str, Any]] | None = None,
        conversions: float | None = None,
        conversion_value: float | None = None,
    ) -> None:
        """Configure adapter to return specific delivery data for a media buy.

        For single-package responses, use the scalar parameters (backward compatible).
        For multi-package responses, pass ``packages`` — a list of dicts with
        ``package_id``, ``impressions``, and ``spend`` keys. Totals are auto-computed
        as the sum of per-package values. ``conversions`` / ``conversion_value``
        are totals-level (spec-optional metrics; omitted when None).

        In-process: injects the response on the adapter MagicMock. E2E: persists
        a ``DeliverySimulationConfig`` row the live server's Mock adapter reads.
        """
        resp = self._build_adapter_delivery(
            media_buy_id, impressions, spend, package_id, clicks, packages, conversions, conversion_value
        )
        self._realize_adapter_response(resp)

    @realize_e2e(_persist_simulation_config)
    def _realize_adapter_response(self, resp: AdapterGetMediaBuyDeliveryResponse) -> None:
        """In-process realization: register the response on the adapter mock."""
        self._adapter_responses[resp.media_buy_id] = resp

    @realize_e2e(
        e2e_unsupported(
            "adapter fault-injection has no server surface; needs an ADCP_TESTING fault-injection control (#1418)"
        )
    )
    def set_adapter_error(self, exception: Exception) -> None:
        """Make the adapter raise the given exception on get_media_buy_delivery."""
        self.mock["adapter"].return_value.get_media_buy_delivery.side_effect = exception  # type: ignore[attr-defined]

    def call_impl(
        self,
        media_buy_ids: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status_filter: list[str] | None = None,
        **extra: Any,
    ) -> GetMediaBuyDeliveryResponse:
        """Call _get_media_buy_delivery_impl with the given parameters."""
        self._commit_factory_data()  # type: ignore[attr-defined]

        # Pop identity — it's injected by call_via for transport dispatch
        # but is not a GetMediaBuyDeliveryRequest field.
        # Use sentinel to distinguish "not provided" from "explicitly None".
        _no_identity = object()
        raw_identity = extra.pop("identity", _no_identity)
        identity = self.identity if raw_identity is _no_identity else raw_identity  # type: ignore[attr-defined]

        kwargs: dict[str, Any] = {}
        if media_buy_ids is not None:
            kwargs["media_buy_ids"] = media_buy_ids
        if start_date is not None:
            kwargs["start_date"] = start_date
        if end_date is not None:
            kwargs["end_date"] = end_date
        if status_filter is not None:
            kwargs["status_filter"] = status_filter
        kwargs.update(extra)

        req = GetMediaBuyDeliveryRequest(**kwargs)
        return _get_media_buy_delivery_impl(req, identity)

    @staticmethod
    def _make_default_adapter_response() -> AdapterGetMediaBuyDeliveryResponse:
        return AdapterGetMediaBuyDeliveryResponse(
            media_buy_id="mb_001",
            reporting_period=ReportingPeriod(
                start=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2025, 12, 31, tzinfo=UTC),
            ),
            totals=DeliveryTotals(impressions=5000.0, spend=250.0),
            by_package=[AdapterPackageDelivery(package_id="pkg_001", impressions=5000, spend=250.0)],
            currency="USD",
        )


class WebhookMixin:
    """Shared fluent API for webhook delivery testing."""

    _seq_counter: dict[str, int]

    def set_http_status(self, code: int, text: str = "") -> None:
        """Configure requests.post to return a single response with the given status."""
        mock_response = MagicMock()
        mock_response.status_code = code
        mock_response.text = text or f"Status {code}"
        self.mock["post"].return_value = mock_response  # type: ignore[attr-defined]
        self.mock["post"].side_effect = None  # type: ignore[attr-defined]

    def set_http_sequence(self, responses: list[tuple[int, str]]) -> None:
        """Configure requests.post to return a sequence of responses.

        Args:
            responses: List of (status_code, text) tuples.
        """
        mocks = []
        for code, text in responses:
            r = MagicMock()
            r.status_code = code
            r.text = text
            mocks.append(r)
        self.mock["post"].side_effect = mocks  # type: ignore[attr-defined]

    def set_http_error(self, exception: Exception) -> None:
        """Make requests.post raise the given exception."""
        self.mock["post"].side_effect = exception  # type: ignore[attr-defined]

    def set_url_invalid(self, error_msg: str = "Invalid URL") -> None:
        """Make URL validation fail, short-circuiting delivery."""
        self.mock["validate"].return_value = (False, error_msg)  # type: ignore[attr-defined]

    def call_deliver(
        self,
        webhook_url: str = "https://example.com/webhook",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        signing_secret: str | None = None,
        max_retries: int = 3,
        timeout: int = 10,
        event_type: str | None = None,
        tenant_id: str | None = None,
        object_id: str | None = None,
        media_buy_id: str | None = None,
        notification_type: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Call deliver_webhook_with_retry with the given parameters.

        When ``payload`` is omitted, a structured default payload is built that
        includes ``media_buy_id``, a monotonically increasing ``sequence_number``
        (per media_buy_id), ``reporting_period``, and optionally
        ``notification_type`` / ``next_expected_at``.  This mirrors the payload
        shape that ``WebhookDeliveryService`` produces so that BDD Then steps can
        assert on payload fields without requiring the full service stack.
        """
        self._commit_factory_data()  # type: ignore[attr-defined]
        mb = media_buy_id or "mb_001"

        # Per-media-buy sequence counter (simulates WebhookDeliveryService behaviour)
        if not hasattr(self, "_seq_counter"):
            self._seq_counter = {}  # type: ignore[assignment]
        self._seq_counter[mb] = self._seq_counter.get(mb, 0) + 1  # type: ignore[index]
        seq: int = self._seq_counter[mb]  # type: ignore[index]

        if payload is None:
            payload = {
                "event": "delivery.update",
                "media_buy_id": mb,
                "sequence_number": seq,
                "reporting_period": {
                    "start": "2025-01-01T00:00:00+00:00",
                    "end": "2025-12-31T23:59:59+00:00",
                },
            }
            if notification_type is not None:
                payload["notification_type"] = notification_type
                if notification_type != "final":
                    payload["next_expected_at"] = "2025-01-08T00:00:00+00:00"
        if headers is None:
            headers = {"Content-Type": "application/json"}
        delivery = WebhookDelivery(
            webhook_url=webhook_url,
            payload=payload,
            headers=headers,
            signing_secret=signing_secret,
            max_retries=max_retries,
            timeout=timeout,
            event_type=event_type,
            tenant_id=tenant_id,
            object_id=object_id,
        )
        return deliver_webhook_with_retry(delivery)

    def call_impl(self, **kwargs: Any) -> Any:
        """Alias for call_deliver to satisfy BaseTestEnv interface."""
        return self.call_deliver(**kwargs)


class CircuitBreakerMixin:
    """Shared fluent API for circuit breaker / webhook delivery service testing."""

    _service: WebhookDeliveryService | None

    def get_service(self) -> WebhookDeliveryService:
        """Return a WebhookDeliveryService instance (cached per env)."""
        if self._service is None:
            self._service = WebhookDeliveryService()
        return self._service

    def get_breaker(self, **kwargs: Any) -> CircuitBreaker:
        """Return a fresh CircuitBreaker instance with the given params."""
        return CircuitBreaker(**kwargs)

    def set_http_response(self, status_code: int) -> None:
        """Configure the httpx Client mock to return the given status code."""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        self.mock["client"].return_value.__enter__.return_value.post.return_value = mock_response  # type: ignore[attr-defined]

    def set_http_status(self, code: int, text: str = "") -> None:
        """Alias for set_http_response — BDD steps use this name consistently."""
        self.set_http_response(code)

    def set_http_sequence(self, responses: list[tuple[int, str]]) -> None:
        """Configure httpx Client to return a sequence of responses."""
        mocks = []
        for code, text in responses:
            r = MagicMock()
            r.status_code = code
            r.text = text
            mocks.append(r)
        self.mock["client"].return_value.__enter__.return_value.post.side_effect = mocks  # type: ignore[attr-defined]

    def set_url_invalid(self, error_msg: str = "Invalid URL") -> None:
        """Make send-time SSRF validation fail (skip delivery / record failure).

        Default harness config passes the SSRF mock so fixture hostnames do not
        NXDOMAIN-fail; scenarios that grade the outbound reject branch must call
        this hook explicitly.
        """
        self.mock["ssrf"].return_value = (False, error_msg)  # type: ignore[attr-defined]

    def set_url_valid(self) -> None:
        """Allow fixture hostnames through send-time SSRF (default harness path)."""
        self.mock["ssrf"].return_value = (True, "")  # type: ignore[attr-defined]

    def _configure_ssrf_default(self) -> None:
        """Default: allow fixture hostnames through send-time SSRF (DNS covered elsewhere).

        Scenarios that grade the reject branch call set_url_invalid(). Both
        CircuitBreakerEnv variants must call this from ``_configure_mocks``.
        """
        self.set_url_valid()

    def call_send(
        self,
        media_buy_id: str = "mb_001",
        tenant_id: str | None = None,
        principal_id: str | None = None,
        reporting_period_start: datetime | None = None,
        reporting_period_end: datetime | None = None,
        impressions: float = 1000.0,
        spend: float = 100.0,
        **extra: Any,
    ) -> bool:
        """Call service.send_delivery_webhook with sensible defaults."""
        self._commit_factory_data()  # type: ignore[attr-defined]
        service = self.get_service()
        return service.send_delivery_webhook(
            media_buy_id=media_buy_id,
            tenant_id=tenant_id or self._tenant_id,  # type: ignore[attr-defined]
            principal_id=principal_id or self._principal_id,  # type: ignore[attr-defined]
            reporting_period_start=reporting_period_start or datetime(2025, 1, 1, tzinfo=UTC),
            reporting_period_end=reporting_period_end or datetime(2025, 12, 31, tzinfo=UTC),
            impressions=impressions,
            spend=spend,
            **extra,
        )

    def call_deliver(
        self,
        media_buy_id: str = "mb_001",
        notification_type: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, dict[str, Any]]:
        """Deliver via the production WebhookDeliveryService.

        BDD scenarios that exercise webhook authentication (HMAC, bearer) and
        retry/backoff timing must use the real production code path —
        ``WebhookDeliveryService.send_delivery_webhook`` — because
        ``deliver_webhook_with_retry`` (the legacy path used by
        :class:`tests.harness.delivery_webhook.WebhookEnv`) emits a different
        signature header name and has different retry timing.

        ``notification_type`` is translated to the production flags:

        * ``"final"``    -> ``is_final=True``
        * ``"adjusted"`` -> ``is_adjusted=True``
        * any other value (``None``, ``"scheduled"``, ``"delayed"``) leaves
          both flags False, which yields a ``"scheduled"`` payload from
          production. ``"delayed"`` is a spec-defined value that production
          does not yet emit; tests that assert on it document a production
          gap rather than a harness gap.

        Returns ``(success, info_dict)`` to keep the call shape compatible
        with :meth:`WebhookMixin.call_deliver`.
        """
        is_final = notification_type == "final"
        is_adjusted = notification_type == "adjusted"
        # Set a non-zero interval so production includes ``next_expected_at``
        # in the payload for non-final notifications. The exact value does not
        # matter — assertions check presence, not the value.
        next_expected_interval_seconds = None if is_final else 86400.0

        success = self.call_send(
            media_buy_id=media_buy_id,
            is_final=is_final,
            is_adjusted=is_adjusted,
            next_expected_interval_seconds=next_expected_interval_seconds,
            **kwargs,
        )
        return success, {"success": success}

    def call_impl(self, **kwargs: Any) -> bool:
        """Alias for call_send to satisfy BaseTestEnv interface."""
        return self.call_send(**kwargs)

    def get_breaker_state(self) -> str:
        """Return circuit breaker state for this tenant's endpoints.

        Scans all circuit breakers keyed to this tenant and returns the
        worst observed state: 'open' > 'half_open' > 'closed'.

        Returns:
            State string: 'closed', 'open', or 'half_open'
        """
        from src.services.webhook_delivery_service import CircuitState

        service = self.get_service()
        tenant_prefix = f"{self._tenant_id}:"  # type: ignore[attr-defined]
        worst = CircuitState.CLOSED
        for key, cb in service._circuit_breakers.items():
            if key.startswith(tenant_prefix):
                if cb.state == CircuitState.OPEN:
                    return CircuitState.OPEN.value
                if cb.state == CircuitState.HALF_OPEN:
                    worst = CircuitState.HALF_OPEN
        return worst.value


class ProductMixin:
    """Shared fluent API for _get_products_impl testing.

    Requires concrete class to define EXTERNAL_PATCHES with these keys:
        "policy_service", "dynamic_variants", "ranking_factory",
        "dynamic_pricing", "resolve_property_list"

    And ASYNC_PATCHES containing at least:
        {"dynamic_variants", "resolve_property_list"}

    Fluent API:
        set_policy_approved()            -- policy check returns approved
        set_policy_blocked(reason)       -- policy check returns blocked
        set_dynamic_variants(variants)   -- configure dynamic variant generation
        set_property_list(ids)           -- configure property list resolver
        set_ranking_disabled()           -- disable AI ranking
        call_impl(brief, **kw)           -- call _get_products_impl
    """

    def set_policy_approved(self) -> None:
        """Configure PolicyCheckService to approve the brief.

        Note: Policy checks are only invoked when the tenant dict has
        ``advertising_policy.enabled = True`` AND ``gemini_api_key`` set.
        By default the harness identity has neither, so this is a no-op
        unless the test explicitly configures the tenant.
        """
        from unittest.mock import AsyncMock

        mock_result = MagicMock(status="approved", reason=None, restrictions=[])
        mock_instance = MagicMock()
        mock_instance.check_brief_compliance = AsyncMock(return_value=mock_result)
        self.mock["policy_service"].return_value = mock_instance  # type: ignore[attr-defined]

    def set_policy_blocked(self, reason: str = "Policy violation") -> None:
        """Configure PolicyCheckService to block the brief."""
        from unittest.mock import AsyncMock

        from src.services.policy_check_service import PolicyStatus

        mock_result = MagicMock(status=PolicyStatus.BLOCKED, reason=reason, restrictions=[])
        mock_instance = MagicMock()
        mock_instance.check_brief_compliance = AsyncMock(return_value=mock_result)
        self.mock["policy_service"].return_value = mock_instance  # type: ignore[attr-defined]

    def set_dynamic_variants(self, variants: list[Any] | None = None) -> None:
        """Configure generate_variants_for_brief to return specific variants.

        Args:
            variants: List of Product model instances to return. Defaults to [].
        """
        self.mock["dynamic_variants"].return_value = variants or []  # type: ignore[attr-defined]

    def set_property_list(self, property_ids: list[str] | None = None) -> None:
        """Configure resolve_property_list to return specific property IDs.

        Args:
            property_ids: List of property identifier strings. Defaults to [].
        """
        self.mock["resolve_property_list"].return_value = property_ids or []  # type: ignore[attr-defined]

    def set_ranking_disabled(self) -> None:
        """Disable AI ranking by making the factory report AI as not enabled."""
        mock_factory = MagicMock()
        mock_factory.is_ai_enabled.return_value = False
        self.mock["ranking_factory"].return_value = mock_factory  # type: ignore[attr-defined]

    def _configure_product_mocks(self) -> None:
        """Wire default happy-path mocks for product testing.

        Call from _configure_mocks() in concrete classes.

        Defaults:
        - PolicyCheckService: not invoked (no gemini_api_key in tenant dict)
        - Dynamic variants: returns [] (already AsyncMock via ASYNC_PATCHES)
        - DynamicPricingService: pass-through in unit mode, real in integration mode
        - Property list resolver: returns [] (already AsyncMock via ASYNC_PATCHES)
        - Ranking factory: AI not enabled
        """
        # Dynamic variants: returns empty list (AsyncMock from ASYNC_PATCHES)
        self.mock["dynamic_variants"].return_value = []  # type: ignore[attr-defined]

        # DynamicPricingService: configure pass-through mock in unit mode only.
        # In integration mode (ProductEnv from product.py), dynamic_pricing is NOT
        # in EXTERNAL_PATCHES, so self.mock won't have it — runs against real DB.
        if "dynamic_pricing" in self.mock:  # type: ignore[attr-defined]
            mock_pricing_instance = MagicMock()
            mock_pricing_instance.enrich_products_with_pricing.side_effect = lambda products, **kw: products
            self.mock["dynamic_pricing"].return_value = mock_pricing_instance  # type: ignore[attr-defined]

        # Ranking factory: AI not enabled
        self.set_ranking_disabled()

        # Property list resolver: returns [] (AsyncMock from ASYNC_PATCHES)
        self.mock["resolve_property_list"].return_value = []  # type: ignore[attr-defined]

    async def call_impl(  # type: ignore[override]
        self,
        brief: str = "test brief",
        brand: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        property_list: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **extra: Any,
    ) -> GetProductsResponse:
        """Call _get_products_impl with the given parameters.

        Args:
            brief: Search brief text.
            brand: Brand reference dict (defaults to {"domain": "test.com"}).
            filters: ProductFilters dict.
            property_list: PropertyListReference dict.
            context: ContextObject dict.
            **extra: Additional kwargs forwarded to request construction.

        Returns:
            GetProductsResponse from the impl function.
        """
        self._commit_factory_data()  # type: ignore[attr-defined]

        # Pop identity — injected by call_via for transport dispatch
        # but not a GetProductsRequest field.
        identity = extra.pop("identity", None) or self.identity  # type: ignore[attr-defined]

        if brand is None:
            brand = {"domain": "test.com"}

        req = GetProductsRequestGenerated(
            brief=brief,
            brand=brand,
            filters=filters,
            property_list=property_list,
            context=context,
            **extra,
        )
        return await _get_products_impl(req, identity)


class TMPSyncMixin:
    """The TMP package-sync observable, owned by the env instead of by each test.

    Package sync is transport-blind buyer-triggered behavior: *a buyer creates or
    updates a media buy, and every registered active/draining provider holds
    current package data*. Before this mixin, that observable had no seam, so
    each tier invented one — a process-wide ``httpx.Client`` patch plus a
    ``threading.Thread.start`` patch in the integration file, a second
    independent seed→dispatch→collect→assert implementation over a socket in the
    e2e file, and a third file asserting the thread was *constructed*. Three
    incompatible observables, two grading implementations that disagreed on which
    transports were covered (#1197 review).

    One implementation serves every transport because nothing here depends on
    which process the sync thread runs in:

    * **The collector is a real HTTP receiver.** No client stubbing, so the URL
      construction, the auth header and the JSON body are production's, and the
      arrival IS the observable whether the thread ran in this process
      (a2a/mcp/rest) or in the server container (e2e_rest).
    * **Completion is the production registry**, via
      :func:`src.services.tmp_provider_sync.join_active_syncs` in-process and by
      polling the collector out-of-process — never by patching a stdlib
      constructor.

    Envs mixing this in get the seam whether or not a scenario uses it: with no
    provider registered the sync short-circuits, and ``__exit__`` still drains
    the threads, so no unrelated media-buy scenario leaves an unjoined daemon
    opening DB sessions after its own teardown.
    """

    # Set on first register_tmp_provider(); None means "this scenario never
    # registered a provider", which is the no-op case.
    _tmp_collector: dict[str, Any] | None = None
    _tmp_collector_ctx: Any = None

    #: The public host planted on the tenant so ``_resolve_seller_agent_url``
    #: yields a spec-valid https ``seller_agent.agent_url``. A constant so the
    #: Then-step can assert the exact URL production emitted.
    TMP_SELLER_AGENT_HOST = "tmp-sync-seller.publisher.example.com"

    @property
    def tmp_seller_agent_url(self) -> str:
        """The ``seller_agent.agent_url`` production must put on every package."""
        return f"https://{self.TMP_SELLER_AGENT_HOST}/mcp"

    def register_tmp_provider(self, *, auth_credentials: str | None = None, **fields: Any) -> str:
        """Register one active TMP provider pointed at this env's collector.

        Starts the collector on first call and replaces any provider rows the
        tenant already had, so the fan-out reaches exactly this endpoint.
        Returns the registered endpoint.

        ``auth_credentials`` is a first-class parameter because the credential is
        half of the cross-transport claim: a scenario registers a credentialed
        provider and asserts the ``Authorization`` header arrives, or registers an
        uncredentialed one and asserts it does not. It is written through the
        model's encrypting property, so the row is what production would store.
        """
        from tests.factories import plant_seller_agent_host, replace_tmp_providers

        collector = self._ensure_tmp_collector()
        tenant_id = self._tenant_id  # type: ignore[attr-defined]

        plant_seller_agent_host(self, tenant_id, self.TMP_SELLER_AGENT_HOST)
        fields.setdefault("name", "Package Sync Collector")
        fields.setdefault("endpoint", collector["endpoint"])
        fields.setdefault("timeout_ms", 2000)
        if auth_credentials is not None:
            # `auth_credentials` is the encrypting property; the factory writes
            # columns, so set it after construction to exercise the real path.
            fields.setdefault("auth_type", "bearer")
        provider = replace_tmp_providers(self, tenant_id, **fields)
        if auth_credentials is not None:
            provider.auth_credentials = auth_credentials
            self.get_session().commit()  # type: ignore[attr-defined]
        return str(fields["endpoint"])

    def tmp_sync_deliveries(self) -> list[dict[str, Any]]:
        """Every ``POST /packages/sync`` the collector has received, in order.

        Entries are ``{"path", "body"}``. The path is carried because "the server
        POSTed *something*" and "the server POSTed to /packages/sync" are
        different claims, and only the second one grades ``provider_url()``.
        """
        if self._tmp_collector is None:
            return []
        return [e for e in self._tmp_collector["received"] if str(e["path"]).endswith("/packages/sync")]

    #: How long to keep watching AFTER the expected deliveries arrive, before a
    #: Then step asserts the exact count. Without a settle window, "exactly one"
    #: is unfalsifiable: a second, duplicate delivery in flight would simply not
    #: have landed yet (#1197 review).
    TMP_SYNC_SETTLE_SECONDS = 0.75

    def await_tmp_sync(self, count: int = 1, timeout: float = 30.0) -> dict[str, Any]:
        """Block until *count* package-sync deliveries have arrived; return the *count*-th.

        This is the LIVENESS signal, so it waits for "at least count" — the
        correctness signal is the Then step's exact ``len(...) == count``, which is
        what makes a double-fire fail. Reusing a ``>=`` wait as the assertion let a
        duplicate delivery pass green on every transport, including the REST
        double-fire that finding 5's placement argument exists to prevent.

        In-process, the production registry gives an exact completion signal, so
        the poll below normally returns on its first iteration. Out-of-process the
        thread is in the server container and polling is the only observation —
        hence one method with both, rather than a per-tier waiter.
        """
        import time

        if self._tmp_collector is None:
            raise AssertionError(
                "await_tmp_sync() called before register_tmp_provider() — there is no collector to wait on."
            )

        if not self.is_e2e:  # type: ignore[attr-defined]
            self.join_tmp_syncs(timeout=timeout)

        deadline = time.monotonic() + timeout
        while True:
            deliveries = self.tmp_sync_deliveries()
            if len(deliveries) >= count:
                # Let any duplicate that is already in flight land, so the caller's
                # exact-count assertion can see it.
                time.sleep(self.TMP_SYNC_SETTLE_SECONDS)
                return self.tmp_sync_deliveries()[count - 1]
            if time.monotonic() >= deadline:
                paths = [e["path"] for e in self._tmp_collector["received"]]
                raise AssertionError(
                    f"Expected {count} POST /packages/sync delivery(ies) within {timeout}s, "
                    f"got {len(deliveries)}. Captured paths: {paths}"
                )
            time.sleep(0.1)

    def settle_tmp_sync(self) -> None:
        """Wait out the settle window with no delivery expected.

        The counterpart to :meth:`await_tmp_sync` for a scenario asserting that
        NOTHING arrives: there is no arrival to wait for, so without a bounded wait
        "no delivery" would pass merely because the request had not landed yet.
        """
        import time

        time.sleep(self.TMP_SYNC_SETTLE_SECONDS)

    def join_tmp_syncs(self, timeout: float = 30.0) -> None:
        """Drain in-flight in-process syncs. No-op out-of-process (nothing local to join).

        Best-effort by design: this is the cleanup half of the seam, so a wedged
        thread is reported, not raised. The assertion belongs to
        :meth:`await_tmp_sync`, where a missing delivery is the actual failure —
        raising here would turn an unrelated media-buy scenario's slow teardown
        into that scenario's failure.
        """
        if self.is_e2e:  # type: ignore[attr-defined]
            return
        from src.services.tmp_provider_sync import join_active_syncs

        stragglers = join_active_syncs(timeout=timeout)
        if stragglers:
            logging.getLogger(__name__).warning(
                "TMP sync threads still running after %.0fs at env teardown: %s", timeout, stragglers
            )

    def _ensure_tmp_collector(self) -> dict[str, Any]:
        """Start the stub-provider HTTP receiver once per env."""
        if self._tmp_collector is not None:
            return self._tmp_collector

        from tests.e2e._webhook_capture import WebhookCaptureHandler, run_webhook_capture_server

        class _PackageSyncCollector(WebhookCaptureHandler):
            """Stub TMP provider recording the whole REQUEST it received.

            Method, path, headers and body — because that is what a provider
            receives, and "identical across transports" is a claim about all four.
            Recording only path+body left ``Authorization: Bearer <credential>``
            — the one credential this feature transmits to a third party — graded
            solely by ``mock_client.post.assert_called_once_with(headers=...)``
            under a patched ``httpx.Client``, the instrument this PR replaced
            everywhere else for being unable to see the wire (#1197 review).
            """

            received_webhooks: list[Any] = []

            def record(self, payload: Any) -> dict[str, Any]:
                return {
                    "method": self.command,
                    "path": self.path,
                    # Header names are case-insensitive on the wire; normalize so a
                    # step asserts on one spelling.
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": payload,
                }

        # Loopback is enough in-process; the e2e server runs in a container and
        # reaches the host via ADCP_WEBHOOK_HOST (in-network) or
        # host.docker.internal (host path). The TMP sync does not rewrite
        # "localhost" the way the webhook service does, so the registered URL has
        # to be container-reachable as written.
        host = None if self.is_e2e else "127.0.0.1"  # type: ignore[attr-defined]
        if self.is_e2e and not os.getenv("ADCP_WEBHOOK_HOST"):  # type: ignore[attr-defined]
            host = "host.docker.internal"

        ctx = run_webhook_capture_server(_PackageSyncCollector, _PackageSyncCollector.received_webhooks, host=host)
        info = ctx.__enter__()
        self._tmp_collector_ctx = ctx
        self._tmp_collector = {
            "endpoint": f"http://{info['host']}:{info['port']}/tmp",
            "received": info["received"],
        }
        return self._tmp_collector

    def _teardown_tmp_sync(self) -> None:
        """Join in-flight syncs, then stop the collector and drop the provider rows.

        Ordering matters: joining first means no thread is still POSTing when the
        receiver socket closes (which surfaces as a connection error in the sync's
        fan-out log), and dropping the rows last stops a later scenario sharing the
        e2e database from fanning out to a port that no longer listens.
        """
        try:
            self.join_tmp_syncs(timeout=30.0)
        finally:
            if self._tmp_collector is not None:
                from tests.factories import delete_tmp_providers

                try:
                    delete_tmp_providers(self, self._tenant_id)  # type: ignore[attr-defined]
                finally:
                    ctx, self._tmp_collector_ctx = self._tmp_collector_ctx, None
                    self._tmp_collector = None
                    if ctx is not None:
                        ctx.__exit__(None, None, None)
