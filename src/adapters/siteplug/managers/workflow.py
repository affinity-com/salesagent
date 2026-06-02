"""Siteplug workflow manager stub.

Handles HITL workflow steps for Siteplug campaign operations.
All methods are stubs — real implementation in Task 08.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SiteplugWorkflowManager:
    """Manages Siteplug HITL workflow steps.

    Stub implementation — replaced with real implementation in Task 08.
    """

    def __init__(self, log_func: Any = None) -> None:
        """Initialize the workflow manager.

        Args:
            log_func: Optional logging function from the adapter
        """
        self._log = log_func or (lambda msg, **kw: logger.info(msg))

    async def create_workflow_step(
        self,
        step_type: str,
        media_buy_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Create a HITL workflow step for human review.

        Stub — replaced in Task 08.

        Args:
            step_type: Type of workflow step (e.g. "campaign_activation", "budget_approval")
            media_buy_id: Media buy ID this step is associated with
            payload: Optional additional data for the workflow step

        Returns:
            Workflow step ID
        """
        self._log(
            f"[STUB] SiteplugWorkflowManager.create_workflow_step: "
            f"step_type={step_type}, media_buy_id={media_buy_id}"
        )
        return f"wf_stub_{media_buy_id}"

    async def check_workflow_status(self, workflow_step_id: str) -> str:
        """Check the status of a workflow step.

        Stub — replaced in Task 08.

        Args:
            workflow_step_id: Workflow step ID

        Returns:
            Status string (e.g. "pending", "approved", "rejected")
        """
        self._log(
            f"[STUB] SiteplugWorkflowManager.check_workflow_status: "
            f"workflow_step_id={workflow_step_id}"
        )
        return "pending"
