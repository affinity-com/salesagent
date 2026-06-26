"""Siteplug Workflow Manager — Human-in-the-Loop Workflow Management.

Handles HITL workflow steps for Siteplug campaign operations.

Two modes are supported:
- ``manual``: human creates the campaign in the Siteplug admin UI, then marks
  the workflow step as complete.
- ``confirmation_required``: adapter creates the campaign in paused state,
  human approves activation via Slack or the sales-agent UI.

Extends :class:`~src.adapters.base_workflow.BaseWorkflowManager` with
Siteplug-specific workflow logic.
"""

import logging
from typing import Any

from src.adapters.base_workflow import BaseWorkflowManager

logger = logging.getLogger(__name__)


class SiteplugWorkflowManager(BaseWorkflowManager):
    """Manages Human-in-the-Loop workflows for Siteplug campaign operations.

    Extends :class:`~src.adapters.base_workflow.BaseWorkflowManager` with
    Siteplug-specific workflow logic for two HITL modes:

    - **manual** — human creates the campaign in the Siteplug admin UI.
    - **confirmation_required** — adapter creates a paused campaign; human
      approves activation.
    """

    platform_name = "Siteplug"
    platform_url_base = "https://console.siteplug.com"

    def __init__(
        self,
        tenant_id: str,
        principal=None,
        audit_logger=None,
        log_func=None,
    ) -> None:
        """Initialize the Siteplug workflow manager.

        Args:
            tenant_id: Tenant identifier for configuration and DB scoping.
            principal: Principal object used for context creation.
            audit_logger: Audit logging instance for structured audit trails.
            log_func: Logging function for console output (defaults to
                ``logger.info``).
        """
        super().__init__(tenant_id, principal, audit_logger, log_func)

    # =========================================================================
    # Public HITL step creators
    # =========================================================================

    def create_manual_workflow_step(
        self,
        media_buy_id: str,
        campaign_data: dict[str, Any],
    ) -> str | None:
        """Create a workflow step for manual campaign creation in Siteplug UI.

        Used when ``automation_mode == "manual"``.  The adapter does NOT call
        the Siteplug API; instead a human operator creates the campaign in the
        Siteplug admin console and then marks this step as complete.

        Args:
            media_buy_id: AdCP media buy ID (format: ``sp_{campaign_id}`` or
                a provisional ID before the campaign exists).
            campaign_data: Dict with campaign details to surface in the Slack
                notification (brand name, budget, targeting summary, etc.).

        Returns:
            The workflow step ID if created successfully, ``None`` otherwise.
        """
        brand_name: str = campaign_data.get("brand_name", "Unknown Brand")
        budget: float = campaign_data.get("budget", 0.0)
        campaign_type: str = campaign_data.get("campaign_type", "")
        platform_name: str = campaign_data.get("platform_name", "")

        action_details: dict[str, Any] = {
            "action_type": "create_siteplug_campaign",
            "media_buy_id": media_buy_id,
            "platform": self.platform_name,
            "automation_mode": "manual",
            "brand_name": brand_name,
            "budget": budget,
            "campaign_type": campaign_type,
            "platform_name": platform_name,
            "siteplug_console_url": self.platform_url_base,
            "instructions": [
                f"Log in to the Siteplug admin console at {self.platform_url_base}",
                f"Create a new campaign for brand: {brand_name}",
                f"Set campaign type to: {campaign_type}" if campaign_type else "Configure campaign type as required",
                f"Set total budget to: ${budget:,.2f}",
                "Configure targeting, ad groups, and creatives per the campaign brief",
                "Once created, update this workflow step with the Siteplug campaign ID",
            ],
            "campaign_data": campaign_data,
            "next_action_after_creation": "campaign_id_update_required",
        }

        return self.create_workflow_step(
            step_type="creation",
            tool_name="create_siteplug_campaign",
            action_details=action_details,
            object_type="media_buy",
            object_id=media_buy_id,
            object_action="create",
            step_prefix="m",
            owner="publisher",
            status="approval",
            assigned_to=None,
            transaction_details={"media_buy_id": media_buy_id, "brand_name": brand_name},
        )

    def create_confirmation_workflow_step(
        self,
        media_buy_id: str,
        campaign_id: int | str,
        campaign_data: dict[str, Any],
    ) -> str | None:
        """Create a workflow step for human approval of a paused Siteplug campaign.

        Used when ``automation_mode == "confirmation_required"``.  The adapter
        has already created the campaign in Siteplug with ``status=0`` (paused).
        A human must review and approve activation.

        Args:
            media_buy_id: AdCP media buy ID (format: ``sp_{campaign_id}``).
            campaign_id: Siteplug campaign ID (integer or string).
            campaign_data: Dict with campaign details to surface in the Slack
                notification (brand name, budget, targeting summary, etc.).

        Returns:
            The workflow step ID if created successfully, ``None`` otherwise.
        """
        brand_name: str = campaign_data.get("brand_name", "Unknown Brand")
        budget: float = campaign_data.get("budget", 0.0)
        campaign_type: str = campaign_data.get("campaign_type", "")

        campaign_url = f"{self.platform_url_base}/campaigns/{campaign_id}"

        action_details: dict[str, Any] = {
            "action_type": "activate_siteplug_campaign",
            "media_buy_id": media_buy_id,
            "siteplug_campaign_id": str(campaign_id),
            "platform": self.platform_name,
            "automation_mode": "confirmation_required",
            "brand_name": brand_name,
            "budget": budget,
            "campaign_type": campaign_type,
            "campaign_url": campaign_url,
            "instructions": [
                f"Review the Siteplug campaign at: {campaign_url}",
                f"Verify campaign settings for brand: {brand_name}",
                f"Confirm budget: ${budget:,.2f} and campaign type: {campaign_type}",
                "Check targeting, ad groups, and creative configuration",
                "Approve this workflow step to activate the campaign (status → 1)",
                "Reject this workflow step to keep the campaign paused",
            ],
            "campaign_data": campaign_data,
            "next_action_after_approval": "automatic_activation",
        }

        return self.create_workflow_step(
            step_type="approval",
            tool_name="activate_siteplug_campaign",
            action_details=action_details,
            object_type="media_buy",
            object_id=media_buy_id,
            object_action="activate",
            step_prefix="r",
            owner="publisher",
            status="approval",
            assigned_to=None,
            transaction_details={
                "media_buy_id": media_buy_id,
                "siteplug_campaign_id": str(campaign_id),
                "brand_name": brand_name,
            },
        )

    # =========================================================================
    # Approval action — activate paused campaign
    # =========================================================================

    def activate_campaign_from_step(self, step_id: str) -> bool:
        """Activate a paused Siteplug campaign when a confirmation_required step is approved.

        Reads ``siteplug_campaign_id`` from the :class:`WorkflowStep` ``request_data``
        and calls ``PUT /campaigns/{id}`` with ``status=1`` (active).

        Args:
            step_id: The workflow step ID (prefix ``r``).

        Returns:
            ``True`` if the campaign was activated successfully, ``False`` otherwise.
        """
        import asyncio
        import concurrent.futures

        from src.core.database.database_session import get_db_session
        from src.core.database.models import WorkflowStep

        # ── Read campaign_id from the workflow step ───────────────────────
        try:
            with get_db_session() as db_session:
                step = db_session.get(WorkflowStep, step_id)
                if step is None:
                    self.log(f"[red]activate_campaign_from_step: step '{step_id}' not found[/red]")
                    return False
                request_data: dict = step.request_data or {}
                campaign_id_raw = request_data.get("siteplug_campaign_id")
        except Exception as exc:
            self.log(f"[red]activate_campaign_from_step: DB error reading step '{step_id}': {exc}[/red]")
            return False

        if not campaign_id_raw:
            self.log(
                f"[red]activate_campaign_from_step: no siteplug_campaign_id in step '{step_id}' "
                f"request_data — cannot activate[/red]"
            )
            return False

        try:
            campaign_id = int(campaign_id_raw)
        except (TypeError, ValueError):
            self.log(
                f"[red]activate_campaign_from_step: invalid siteplug_campaign_id "
                f"'{campaign_id_raw}' in step '{step_id}'[/red]"
            )
            return False

        # ── Build a Siteplug client from AdapterConfig table ─────────────
        try:
            from src.core.database.repositories.adapter_config import AdapterConfigRepository

            with get_db_session() as _db:
                repo = AdapterConfigRepository(_db, self.tenant_id)
                config_row = repo.find_by_tenant()
                if config_row is None:
                    self.log(
                        f"[red]activate_campaign_from_step: no AdapterConfig for "
                        f"tenant '{self.tenant_id}'[/red]"
                    )
                    return False
                sp_cfg = repo.get_siteplug_config(config_row)

            from src.adapters.siteplug.client import SiteplugClient
            from src.adapters.siteplug.config_schema import SiteplugConnectionConfig

            connection_config = SiteplugConnectionConfig(
                base_url=sp_cfg["base_url"],
                api_key=sp_cfg["api_key"],
                timeout=sp_cfg.get("timeout", 30),
                max_retries=sp_cfg.get("max_retries", 3),
            )
            client = SiteplugClient(connection_config)
        except Exception as exc:
            self.log(f"[red]activate_campaign_from_step: failed to build client: {exc}[/red]")
            return False

        # ── Call PUT /campaigns/{id} with status=1 ────────────────────────
        try:
            async def _activate() -> None:
                await client.update_campaign(campaign_id, {"status": 1})

            def _run_in_new_loop() -> None:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_activate())
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(_run_in_new_loop).result()

            self.log(
                f"[siteplug] activate_campaign_from_step: campaign_id={campaign_id} "
                f"activated (status=1) for step '{step_id}'"
            )
            if self.audit_logger:
                self.audit_logger.log_success(
                    f"Siteplug campaign {campaign_id} activated via workflow step {step_id}"
                )
            return True

        except Exception as exc:
            self.log(
                f"[red]activate_campaign_from_step: PUT /campaigns/{campaign_id} failed: {exc}[/red]"
            )
            if self.audit_logger:
                self.audit_logger.log_warning(
                    f"Failed to activate Siteplug campaign {campaign_id} for step {step_id}: {exc}"
                )
            return False

    # =========================================================================
    # Notification styling override
    # =========================================================================

    def _get_notification_details(
        self, step_id: str, action_details: dict[str, Any]
    ) -> dict[str, str]:
        """Return Siteplug-specific Slack notification styling.

        Args:
            step_id: The workflow step ID.
            action_details: Details about the workflow step.

        Returns:
            Dict with ``title``, ``description``, and ``color``.
        """
        action_type: str = action_details.get("action_type", "")
        brand_name: str = action_details.get("brand_name", "")
        budget: float = action_details.get("budget", 0.0)

        description_suffix = ""
        if brand_name:
            description_suffix += f" | Brand: {brand_name}"
        if budget:
            description_suffix += f" | Budget: ${budget:,.2f}"

        if action_type == "create_siteplug_campaign":
            return {
                "title": "New Siteplug Campaign — Manual Creation Required",
                "description": (
                    f"Manual mode activated — human intervention needed to create "
                    f"Siteplug campaign{description_suffix}"
                ),
                "color": "#FF9500",  # Orange
            }
        elif action_type == "activate_siteplug_campaign":
            return {
                "title": "Siteplug Campaign Ready for Review",
                "description": (
                    f"Campaign created in paused state — approval needed for "
                    f"activation{description_suffix}"
                ),
                "color": "#FFD700",  # Gold
            }
        else:
            # Fall back to base class behaviour
            return super()._get_notification_details(step_id, action_details)
