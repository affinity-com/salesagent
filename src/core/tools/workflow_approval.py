"""Generic workflow step approval executor.

Provides :func:`execute_approved_workflow_step` — the adapter-agnostic
post-approval hook called by the admin blueprint when a human approves a
workflow step that requires an adapter-side action (e.g. activating a paused
Siteplug campaign).

The blueprint stays adapter-agnostic: it calls this function, which loads the
correct adapter via :func:`~src.core.helpers.adapter_helpers.get_adapter` and
dispatches to ``adapter.execute_workflow_step_approval(step_id)`` if the
adapter supports it.

Pattern mirrors :func:`~src.core.tools.media_buy_create.execute_approved_media_buy`.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def execute_approved_workflow_step(
    step_id: str,
    tenant_id: str,
) -> tuple[bool, str | None]:
    """Execute any adapter-side action required after a workflow step is approved.

    Loads the workflow step from the database, resolves the principal and
    tenant, instantiates the correct adapter, and calls
    ``adapter.execute_workflow_step_approval(step_id)`` if the adapter
    implements it.

    Adapters that do not implement ``execute_workflow_step_approval`` are
    silently skipped (the step is already marked ``approved`` by the caller).

    Args:
        step_id: The workflow step ID that was just approved.
        tenant_id: Tenant scope for DB and adapter resolution.

    Returns:
        ``(True, None)`` on success or when the adapter has no post-approval
        action.  ``(False, error_message)`` when the adapter action fails.
    """
    from sqlalchemy import select

    from src.core.auth import get_principal_object
    from src.core.config_loader import get_tenant_by_id, set_current_tenant
    from src.core.database.database_session import get_db_session
    from src.core.database.models import ObjectWorkflowMapping, Tenant, WorkflowStep
    from src.core.helpers.adapter_helpers import get_adapter

    logger.info(f"[WF_APPROVAL] execute_approved_workflow_step: step_id={step_id}, tenant_id={tenant_id}")

    try:
        # ── Load step + resolve principal ─────────────────────────────────
        with get_db_session() as db:
            step = db.get(WorkflowStep, step_id)
            if step is None:
                msg = f"WorkflowStep '{step_id}' not found"
                logger.error(f"[WF_APPROVAL] {msg}")
                return False, msg

            tool_name: str = step.tool_name or ""

            # Resolve principal via ObjectWorkflowMapping → media_buy_id
            stmt = (
                select(ObjectWorkflowMapping)
                .where(ObjectWorkflowMapping.step_id == step_id)
                .order_by(ObjectWorkflowMapping.created_at.desc())
            )
            mapping = db.scalars(stmt).first()

            # Load tenant ORM object
            tenant_obj = db.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
            if tenant_obj is None:
                msg = f"Tenant '{tenant_id}' not found"
                logger.error(f"[WF_APPROVAL] {msg}")
                return False, msg

            # Resolve principal_id from the workflow context
            context = step.context
            principal_id: str | None = context.principal_id if context else None

        if not principal_id:
            msg = f"Cannot resolve principal for workflow step '{step_id}'"
            logger.error(f"[WF_APPROVAL] {msg}")
            return False, msg

        principal = get_principal_object(principal_id, tenant_id=tenant_id)
        if principal is None:
            msg = f"Principal '{principal_id}' not found for tenant '{tenant_id}'"
            logger.error(f"[WF_APPROVAL] {msg}")
            return False, msg

        # ── Set tenant context (required by adapter helpers) ──────────────
        tenant_config = get_tenant_by_id(tenant_id)
        if tenant_config:
            set_current_tenant(tenant_config)

        # ── Instantiate adapter ───────────────────────────────────────────
        adapter = get_adapter(principal, dry_run=False, tenant=tenant_obj)

        # ── Dispatch to adapter if it supports post-approval actions ──────
        if not hasattr(adapter, "execute_workflow_step_approval"):
            logger.info(
                f"[WF_APPROVAL] Adapter {type(adapter).__name__} does not implement "
                f"execute_workflow_step_approval — no post-approval action needed for step '{step_id}'"
            )
            return True, None

        logger.info(
            f"[WF_APPROVAL] Dispatching to {type(adapter).__name__}.execute_workflow_step_approval "
            f"for step '{step_id}' (tool_name='{tool_name}')"
        )
        success: bool = adapter.execute_workflow_step_approval(step_id)

        if success:
            logger.info(f"[WF_APPROVAL] Post-approval action succeeded for step '{step_id}'")
            return True, None
        else:
            msg = (
                f"Adapter post-approval action failed for step '{step_id}' "
                f"(tool_name='{tool_name}'). Check adapter logs for details."
            )
            logger.error(f"[WF_APPROVAL] {msg}")
            return False, msg

    except Exception as exc:
        msg = f"execute_approved_workflow_step failed for step '{step_id}': {exc}"
        logger.error(f"[WF_APPROVAL] {msg}", exc_info=True)
        return False, msg
