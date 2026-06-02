# Bug: Webhooks not delivered on auto-approve path — missing ObjectWorkflowMapping

**Affects:** `src/core/tools/media_buy_create.py` — `_create_media_buy_impl()` auto-approve path  
**Introduced by:** affinity-main commit `63c35c93` ("fix: adcopy-gen e2e fixes")  
**Status:** Fixed on `affinity-main`, **not yet ported to `feat/tmp-integration`**

---

## Problem

When a media buy is auto-approved (no human review step), `_create_media_buy_impl` calls
`ctx_manager.update_workflow_step(step.step_id, status="completed")` to mark the step done.
This triggers `_send_push_notifications` internally.

`_send_push_notifications` queries `ObjectWorkflowMapping` to find the webhook URL for the
media buy:

```python
# src/core/context_manager.py — _send_push_notifications
mappings = session.scalars(
    select(ObjectWorkflowMapping).filter_by(step_id=step_id)
).all()
if not mappings:
    logger.debug("No object mappings found — skipping push notifications")
    return   # ← exits here silently
```

On the auto-approve path, no `ObjectWorkflowMapping` row is created before
`update_workflow_step` is called, so the function returns early and the webhook is never sent.

On the **manual-approval path** the mapping is created by the workflow machinery before the
step is completed, so it works correctly there.

## Symptom

Media buy creation succeeds and returns HTTP 200, but the buyer's push notification webhook
is never called on the auto-approve path. No error is logged — the failure is silent.

## Fix (already on affinity-main)

Insert an `ObjectWorkflowMapping` row immediately before calling `update_workflow_step` on
the auto-approve path:

```python
# Link workflow step to media buy for webhook delivery (auto-approve path)
try:
    with MediaBuyUoW(tenant["tenant_id"]) as wf_uow:
        # FIXME(salesagent-9f2): workflow mapping should use a repository method
        assert wf_uow.session is not None
        from src.core.database.models import ObjectWorkflowMapping

        mapping = ObjectWorkflowMapping(
            object_type="media_buy",
            object_id=response.media_buy_id,
            step_id=step.step_id,
            action="create",
        )
        wf_uow.session.add(mapping)
        # UoW auto-commits on clean exit
        logger.info(
            f"✅ Linked workflow step {step.step_id} to media buy "
            f"{response.media_buy_id} (auto-approve path)"
        )
except Exception as e:
    logger.warning(
        f"Failed to create ObjectWorkflowMapping for auto-approve path: {e}"
    )

# Mark workflow step as completed on success (triggers _send_push_notifications)
ctx_manager.update_workflow_step(step.step_id, status="completed")
```

## Known technical debt

The fix uses `wf_uow.session.add(mapping)` directly instead of going through a repository
method. This is tracked as `FIXME(salesagent-9f2)` — a `WorkflowMappingRepository` or
equivalent should be created to encapsulate this insert.

## Proposed solution

1. Cherry-pick or manually apply the `ObjectWorkflowMapping` insert block from affinity-main
   commit `63c35c93` into `feat/tmp-integration`.
2. Follow up with a separate PR to extract a `WorkflowMappingRepository.create_for_object()`
   method and remove the `FIXME(salesagent-9f2)` comment.

## Reproduction

1. Configure a principal with a push notification webhook URL.
2. Create a media buy for a product that auto-approves (no manual review workflow).
3. Observe that the webhook endpoint receives no POST request despite the media buy
   returning `status: active`.
