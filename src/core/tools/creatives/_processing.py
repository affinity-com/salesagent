"""Creative create/update logic: DB persistence, agent validation, preview extraction."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from adcp.types import CreativeAsset
from adcp.types import Error as AdCPErrorDetail
from pydantic import BaseModel

from src.core.helpers import _extract_format_info, _validate_creative_assets
from src.core.schemas import CreativeStatusEnum, SyncCreativeResult
from src.core.validation_helpers import run_async_in_sync_context

from ._assets import _build_creative_data, _extract_url_from_assets

if TYPE_CHECKING:
    from src.core.database.repositories.creative import CreativeRepository

logger = logging.getLogger(__name__)


def _find_format(
    all_formats: list[Any],
    creative_format: Any,  # FormatId object with .agent_url and .id
) -> Any | None:
    """Find a Format from all_formats matching the composite (agent_url, id) key.

    Uses normalize_agent_url() to handle trailing-slash variants consistently.
    Returns None if not found.
    """
    from src.core.validation import normalize_agent_url

    target_agent = normalize_agent_url(str(creative_format.agent_url))
    target_id = creative_format.id

    for fmt in all_formats:
        fmt_agent = normalize_agent_url(str(fmt.format_id.agent_url))
        if fmt_agent == target_agent and fmt.format_id.id == target_id:
            return fmt
    return None


def _build_generative_manifest(
    format_id_str: str,
    agent_url: str,
    assets: dict | None,
) -> dict[str, Any]:
    """Build a compliant AdCP creative_manifest for build_creative calls.

    Per AdCP creative-manifest.json schema:
    - format_id: required, full FormatId object {agent_url, id}
    - assets: required, dict of asset_id -> asset object
    """
    return {
        "format_id": {
            "id": format_id_str,
            "agent_url": agent_url,
        },
        "assets": dict(assets or {}),
    }


def _failed_sync_result(creative_id: str, error_msg: str) -> SyncCreativeResult:
    """Build a SyncCreativeResult for a failed creative sync operation."""
    return SyncCreativeResult(
        creative_id=creative_id,
        action="failed",
        status=None,
        platform_id=None,
        errors=[AdCPErrorDetail(code="SERVICE_UNAVAILABLE", message=error_msg)],
        review_feedback=None,
        assigned_to=None,
        assignment_errors=None,
    )


def _update_existing_creative(
    creative: CreativeAsset,
    existing_creative: Any,
    creative_repo: CreativeRepository,
    format_value: Any,
    approval_mode: str,
    tenant: dict[str, Any],
    webhook_url: str | None,
    context: dict[str, Any] | BaseModel | None,
    all_formats: list[Any],
    registry: Any,
    principal_id: str,
    media_buy_brand: dict | None = None,
) -> tuple[SyncCreativeResult, bool]:
    """Update an existing creative with upsert semantics (AdCP 2.5).

    Handles the full update path: field updates, approval mode logic,
    creative agent validation (generative and static), preview extraction,
    and data persistence.

    Args:
        creative: CreativeAsset model from the sync payload.
        existing_creative: Existing DBCreative model to update (mutated in-place).
        creative_repo: CreativeRepository for DB operations (flush, update_data).
        format_value: Validated FormatId from Creative schema.
        approval_mode: Tenant approval mode (auto-approve, ai-powered, require-human).
        tenant: Tenant dict with tenant_id, slack_webhook_url, etc.
        webhook_url: Push notification webhook URL for AI review callbacks.
        context: Application-level context per AdCP spec.
        all_formats: Pre-fetched creative formats from registry.
        registry: CreativeAgentRegistry instance.
        principal_id: Authenticated principal ID for AI review callbacks.

    Returns:
        Tuple of (SyncCreativeResult, needs_approval).
    """

    from typing import Literal

    # Update updated_at timestamp
    now = datetime.now(UTC)
    existing_creative.updated_at = now

    # Track changes for result
    changes: list[str] = []

    # Upsert mode: update provided fields
    if creative.name != existing_creative.name:
        name_value = creative.name
        if name_value is not None:
            existing_creative.name = str(name_value)
        changes.append("name")
    # Extract complete format info including parameters (AdCP 2.5)
    format_info = _extract_format_info(format_value)
    new_agent_url = format_info["agent_url"]
    new_format = format_info["format_id"]
    new_params = format_info["parameters"]
    if (
        new_agent_url != existing_creative.agent_url
        or new_format != existing_creative.format
        or new_params != existing_creative.format_parameters
    ):
        existing_creative.agent_url = new_agent_url
        existing_creative.format = new_format
        # Cast TypedDict to dict for SQLAlchemy column type
        existing_creative.format_parameters = cast(dict | None, new_params)
        changes.append("format")

    # Determine creative status based on approval mode
    creative_format = creative.format_id
    needs_approval = False
    if creative_format:  # Only update approval status if format is provided
        if approval_mode == "auto-approve":
            existing_creative.status = CreativeStatusEnum.approved.value
            needs_approval = False
        elif approval_mode == "ai-powered":
            # Submit to background AI review (async)

            from src.admin.blueprints.creatives import (
                _ai_review_executor,
                _ai_review_lock,
                _ai_review_tasks,
            )

            # Set status to pending_review for AI review
            existing_creative.status = CreativeStatusEnum.pending_review.value
            needs_approval = True

            # Submit background task
            task_id = f"ai_review_{existing_creative.creative_id}_{uuid.uuid4().hex[:8]}"

            # Need to flush to ensure creative_id is available
            creative_repo.flush()

            # Import the async function
            from src.admin.blueprints.creatives import _ai_review_creative_async

            future = _ai_review_executor.submit(
                _ai_review_creative_async,
                creative_id=existing_creative.creative_id,
                tenant_id=tenant["tenant_id"],
                webhook_url=webhook_url,
                slack_webhook_url=tenant.get("slack_webhook_url"),
                principal_name=principal_id,
            )

            # Track the task
            with _ai_review_lock:
                _ai_review_tasks[task_id] = {
                    "future": future,
                    "creative_id": existing_creative.creative_id,
                    "created_at": time.time(),
                }

            logger.info(f"[sync_creatives] Submitted AI review for {existing_creative.creative_id} (task: {task_id})")
        else:  # require-human
            existing_creative.status = CreativeStatusEnum.pending_review.value
            needs_approval = True

    # Store creative properties in data field
    # AdCP 2.5: Full upsert semantics (replace all data, not merge)
    url = _extract_url_from_assets(creative)
    data = _build_creative_data(creative, url, context)

    # ALWAYS validate updates with creative agent
    if creative_format:
        try:
            # Use pre-fetched formats (fetched outside transaction at function start)
            # This avoids async HTTP calls inside savepoint

            # Find matching format using composite (normalized agent_url, id) key
            format_obj = _find_format(all_formats, creative_format)

            if format_obj and format_obj.agent_url:
                # Check if format is generative (has output_format_ids)
                is_generative = bool(getattr(format_obj, "output_format_ids", None))

                if is_generative:
                    # Generative creative update - delegate to creative-agent
                    # creative-agent decides model internally — no format-specific key checks here
                    format_id_str = creative_format.id if hasattr(creative_format, "id") else str(creative_format)

                    logger.info(
                        f"[sync_creatives] Detected generative format update: {creative_format}"
                        ", delegating to creative-agent"
                    )

                    # Extract message/brief from assets or inputs
                    message = None
                    if creative.assets:
                        for role, asset in creative.assets.items():
                            if role in ["message", "brief", "prompt"]:
                                if isinstance(asset, dict):
                                    message = asset.get("content") or asset.get("text")
                                else:
                                    message = getattr(asset, "content", None) or getattr(asset, "text", None)
                                if message:
                                    break

                    if not message and creative.inputs:
                        inputs = creative.inputs or []
                        if inputs:
                            first_input = inputs[0]
                            if isinstance(first_input, dict):
                                message = first_input.get("context_description")
                            else:
                                message = getattr(first_input, "context_description", None)

                    # Extract promoted_offerings from assets if available
                    promoted_offerings = None
                    if creative.assets:
                        for role, asset in creative.assets.items():
                            if role == "promoted_offerings":
                                promoted_offerings = asset
                                break

                    # Get existing context_id for refinement
                    existing_context_id = None
                    if existing_creative.data:
                        existing_context_id = existing_creative.data.get("generative_context_id")

                    # Use provided context_id or existing one
                    context_id = getattr(creative, "context_id", None) or existing_context_id

                    # Only call build_creative if we have a message (refinement)
                    if message:
                        logger.info(
                            f"[sync_creatives] Calling build_creative for update: "
                            f"{existing_creative.creative_id} format {creative_format} "
                            f"from agent {format_obj.agent_url}, "
                            f"message_length={len(message) if message else 0}, "
                            f"context_id={context_id}"
                        )

                        format_id_str = creative_format.id if hasattr(creative_format, "id") else str(creative_format)
                        build_result = run_async_in_sync_context(
                            registry.build_creative(
                                agent_url=format_obj.agent_url,
                                format_id=format_id_str,
                                message=message,
                                promoted_offerings=promoted_offerings,
                                context_id=context_id,
                                finalize=getattr(creative, "approved", False),
                                creative_manifest=_build_generative_manifest(
                                    format_id_str=format_id_str,
                                    agent_url=str(format_obj.agent_url),
                                    assets=creative.assets,
                                ),
                                brand=getattr(creative, "brand", None),
                            )
                        )

                        # Store build result in data
                        if build_result:
                            data["generative_build_result"] = build_result
                            data["generative_status"] = build_result.get("status", "draft")
                            data["generative_context_id"] = build_result.get("context_id")
                            changes.append("generative_build_result")

                            # Extract creative output if available
                            creative_manifest_out = build_result.get("creative_manifest", {})
                            if creative_manifest_out.get("assets"):
                                data["assets"] = {**data.get("assets", {}), **creative_manifest_out["assets"]}
                                changes.append("assets")
                                logger.info("[sync_creatives] Using assets from generative output (update)")

                            # Gap B: fallback to creative_manifest assets if creative_output had none
                            if not data.get("assets"):
                                manifest_assets = build_result.get("creative_manifest", {}).get("assets")
                                if manifest_assets:
                                    data["assets"] = manifest_assets
                                    changes.append("assets")
                                    logger.info("[sync_creatives] Using assets from creative_manifest (update)")

                            if creative_manifest_out.get("output_format"):
                                output_format = creative_manifest_out["output_format"]
                                data["output_format"] = output_format
                                changes.append("output_format")

                                # Only use generative URL if user didn't provide one
                                if isinstance(output_format, dict) and output_format.get("url"):
                                    if not data.get("url"):
                                        data["url"] = output_format["url"]
                                        changes.append("url")
                                        logger.info(
                                            f"[sync_creatives] Got URL from generative output (update): "
                                            f"{data['url']}"
                                        )
                                    else:
                                        logger.info(
                                            "[sync_creatives] Preserving user-provided URL in update, "
                                            "not overwriting with generative output"
                                        )

                            # Extract url/width/height from top-level creative_manifest_out (unconditional)
                            # These are set by the creative-agent for rendered formats (e.g. text_ad_search → HTML preview)
                            if creative_manifest_out.get("url") and not data.get("url"):
                                data["url"] = creative_manifest_out["url"]
                                changes.append("url")
                                logger.info(
                                    f"[sync_creatives] Got URL from generative output manifest (update): {data['url']}"
                                )
                            if creative_manifest_out.get("width") and not data.get("width"):
                                data["width"] = creative_manifest_out["width"]
                                changes.append("width")
                            if creative_manifest_out.get("height") and not data.get("height"):
                                data["height"] = creative_manifest_out["height"]
                                changes.append("height")

                            # Persist brand from the media buy request so adapters can route by domain
                            # (e.g. Affilizz domain guard in SiteplugCreativeManager._sync_text_ad_to_affilizz).
                            # brand is buyer-supplied creative metadata — not a generation decision.
                            if not data.get("brand"):
                                if media_buy_brand:
                                    data["brand"] = media_buy_brand
                                    changes.append("brand")
                                elif getattr(creative, "brand", None):
                                    brand_val = creative.brand
                                    data["brand"] = brand_val if isinstance(brand_val, dict) else {"domain": str(brand_val)}
                                    changes.append("brand")

                            logger.info(
                                f"[sync_creatives] Generative creative updated: "
                                f"status={data.get('generative_status')}, "
                                f"context_id={data.get('generative_context_id')}"
                            )
                    else:
                        # No prompt → skip build, but preserve generative fields
                        # from existing data (data was rebuilt from scratch above)
                        if existing_creative.data:
                            for key in (
                                "generative_build_result",
                                "generative_status",
                                "generative_context_id",
                                "output_format",
                            ):
                                if key in existing_creative.data:
                                    data[key] = existing_creative.data[key]
                        logger.info("[sync_creatives] No message for generative update, keeping existing creative data")

                    # Skip preview_creative call since we already have the output
                    preview_result = None
                else:
                    # Static creative - use preview_creative
                    # Build AdCP-compliant creative manifest
                    format_id_str = creative_format.id
                    creative_manifest: dict[str, Any] = {
                        "format_id": {
                            "id": creative_format.id,
                            "agent_url": str(format_obj.agent_url),
                        },
                        "assets": _validate_creative_assets(creative.assets) if creative.assets else {},
                    }
                    # Optional extras (non-spec but harmless for agent context)
                    if creative.name or getattr(existing_creative, "name", None):
                        creative_manifest["name"] = creative.name or existing_creative.name
                    if data.get("url"):
                        creative_manifest["url"] = data.get("url")

                    # Call creative agent's preview_creative for validation + preview
                    logger.info(
                        f"[sync_creatives] Calling preview_creative for validation (update): "
                        f"{existing_creative.creative_id} format {format_id_str} "
                        f"from agent {format_obj.agent_url}, has_assets={bool(creative.assets)}, "
                        f"has_url={bool(data.get('url'))}"
                    )

                    preview_result = run_async_in_sync_context(
                        registry.preview_creative(
                            agent_url=format_obj.agent_url,
                            format_id=format_id_str,
                            creative_manifest=creative_manifest,
                        )
                    )

                # Extract preview data and store in data field
                if preview_result and preview_result.get("previews"):
                    # Store full preview response for UI (per AdCP PR #119)
                    # This preserves all variants and renders for UI display
                    data["preview_response"] = preview_result
                    changes.append("preview_response")

                    # Also extract primary preview URL for backward compatibility
                    first_preview = preview_result["previews"][0]
                    renders = first_preview.get("renders", [])
                    if renders:
                        first_render = renders[0]

                        # Store preview URL from render ONLY if we don't already have a URL from assets
                        # This preserves user-provided URLs in assets instead of overwriting with preview URLs
                        if first_render.get("preview_url") and not data.get("url"):
                            data["url"] = first_render["preview_url"]
                            changes.append("url")
                            logger.info(f"[sync_creatives] Got preview URL from creative agent: {data['url']}")
                        elif data.get("url"):
                            logger.info(
                                "[sync_creatives] Preserving user-provided URL from assets, "
                                "not overwriting with preview URL"
                            )

                        # Extract dimensions from dimensions object
                        # Only use preview dimensions if not already provided by user
                        dimensions = first_render.get("dimensions", {})
                        if dimensions.get("width") and not data.get("width"):
                            data["width"] = dimensions["width"]
                            changes.append("width")
                        if dimensions.get("height") and not data.get("height"):
                            data["height"] = dimensions["height"]
                            changes.append("height")
                        if dimensions.get("duration") and not data.get("duration"):
                            data["duration"] = dimensions["duration"]
                            changes.append("duration")

                logger.info(
                    f"[sync_creatives] Preview data populated for update: "
                    f"url={bool(data.get('url'))}, "
                    f"width={data.get('width')}, "
                    f"height={data.get('height')}, "
                    f"variants={len(preview_result.get('previews', []) if preview_result else [])}"
                )
            else:
                # Preview generation returned no previews
                # Only acceptable if creative has a media_url (direct URL to creative asset)
                has_media_url = bool(getattr(creative, "url", None) or data.get("url"))

                if has_media_url:
                    # Static creatives with media_url don't need previews
                    warning_msg = f"Preview generation returned no previews for {existing_creative.creative_id} (static creative with media_url)"
                    logger.warning(f"[sync_creatives] {warning_msg}")
                    # Continue with update - preview is optional for static creatives
                else:
                    # Creative agent should have generated previews but didn't
                    error_msg = f"Preview generation failed for {existing_creative.creative_id}: no previews returned and no media_url provided"
                    logger.error(f"[sync_creatives] {error_msg}")
                    return (_failed_sync_result(existing_creative.creative_id, error_msg), False)

        except Exception as validation_error:
            # Creative agent validation failed for update (network error, agent down, etc.)
            # Do NOT update the creative - it needs validation before acceptance
            error_msg = (
                f"Creative agent unreachable or validation error: {str(validation_error)}. "
                f"Retry recommended - creative agent may be temporarily unavailable."
            )
            logger.error(
                f"[sync_creatives] {error_msg} for update of {existing_creative.creative_id}",
                exc_info=True,
            )
            return (_failed_sync_result(existing_creative.creative_id, error_msg), False)

    # In full upsert, consider all fields as changed
    changes.extend(["url", "click_url", "width", "height", "duration"])

    creative_repo.update_data(existing_creative, data)

    # Record result for updated creative
    action: Literal["updated", "unchanged"] = "updated" if changes else "unchanged"

    return (
        SyncCreativeResult(
            creative_id=existing_creative.creative_id,
            action=action,
            status=existing_creative.status,
            platform_id=None,
            changes=changes,
            review_feedback=None,
            assigned_to=None,
            assignment_errors=None,
        ),
        needs_approval,
    )


def _create_new_creative(
    creative: CreativeAsset,
    creative_repo: CreativeRepository,
    format_value: Any,
    approval_mode: str,
    tenant: dict[str, Any],
    webhook_url: str | None,
    context: dict[str, Any] | BaseModel | None,
    all_formats: list[Any],
    registry: Any,
    principal_id: str,
    media_buy_brand: dict | None = None,
) -> tuple[SyncCreativeResult, bool]:
    """Create a new creative and persist it to the database (AdCP 2.5).

    Handles the full create path: URL extraction, data dict construction,
    creative agent validation (generative build or static preview),
    DB insertion, approval mode logic, and AI review submission.

    Mutates ``creative.creative_id`` in-place when the ID is server-generated.

    Returns:
        Tuple of (SyncCreativeResult, needs_approval).
    """

    # Extract creative_id for error reporting (must be defined before any validation)
    creative_id = creative.creative_id or "unknown"

    # Prepare data field with all creative properties
    url = _extract_url_from_assets(creative)
    data = _build_creative_data(creative, url, context)

    # Store user-provided assets for preservation check
    user_provided_assets = creative.assets

    # ALWAYS validate creatives with the creative agent (validation + preview generation)
    creative_format = creative.format_id
    if creative_format:
        try:
            # Use pre-fetched formats (fetched outside transaction at function start)
            # This avoids async HTTP calls inside savepoint

            # Find matching format using composite (normalized agent_url, id) key
            format_obj = _find_format(all_formats, creative_format)

            if format_obj and format_obj.agent_url:
                # Check if format is generative (has output_format_ids)
                is_generative = bool(getattr(format_obj, "output_format_ids", None))

                if is_generative:
                    # Generative creative - delegate to creative-agent
                    # creative-agent decides model internally — no format-specific key checks here
                    format_id_str = creative_format.id if hasattr(creative_format, "id") else str(creative_format)

                    logger.info(
                        f"[sync_creatives] Detected generative format: {creative_format}"
                        ", delegating to creative-agent"
                    )

                    # Extract message/brief from assets or inputs
                    message = None
                    if creative.assets:
                        for role, asset in creative.assets.items():
                            if role in ["message", "brief", "prompt"]:
                                if isinstance(asset, dict):
                                    message = asset.get("content") or asset.get("text")
                                else:
                                    message = getattr(asset, "content", None) or getattr(asset, "text", None)
                                if message:
                                    break

                    if not message and creative.inputs:
                        inputs = creative.inputs or []
                        if inputs:
                            first_input = inputs[0]
                            if isinstance(first_input, dict):
                                message = first_input.get("context_description")
                            else:
                                message = getattr(first_input, "context_description", None)

                    if not message:
                        message = f"Create a creative for: {creative.name}"
                        logger.warning(
                            "[sync_creatives] No message found in assets/inputs, using creative name as fallback"
                        )

                    # Extract promoted_offerings from assets if available
                    promoted_offerings = None
                    if creative.assets:
                        for role, asset in creative.assets.items():
                            if role == "promoted_offerings":
                                promoted_offerings = asset
                                break

                    # Call build_creative
                    # Extract string ID from FormatId object if needed
                    format_id_str = creative_format.id
                    logger.info(
                        f"[sync_creatives] Calling build_creative for generative format: "
                        f"{format_id_str} from agent {format_obj.agent_url}, "
                        f"message_length={len(message) if message else 0}"
                    )

                    build_result = run_async_in_sync_context(
                        registry.build_creative(
                            agent_url=format_obj.agent_url,
                            format_id=format_id_str,
                            message=message,
                            promoted_offerings=promoted_offerings,
                            context_id=getattr(creative, "context_id", None),
                            finalize=getattr(creative, "approved", False),
                            creative_manifest=_build_generative_manifest(
                                format_id_str=format_id_str,
                                agent_url=str(format_obj.agent_url),
                                assets=creative.assets,
                            ),
                            brand=getattr(creative, "brand", None),
                        )
                    )

                    # Store build result
                    if build_result:
                        data["generative_build_result"] = build_result
                        data["generative_status"] = build_result.get("status", "draft")
                        data["generative_context_id"] = build_result.get("context_id")

                        # Extract creative output
                        creative_manifest_out = build_result.get("creative_manifest", {})
                        if creative_manifest_out.get("assets"):
                            data["assets"] = {**data.get("assets", {}), **creative_manifest_out["assets"]}
                            logger.info("[sync_creatives] Using assets from generative output")

                        # Gap B: fallback to creative_manifest assets if creative_output had none
                        if not data.get("assets"):
                            manifest_assets = build_result.get("creative_manifest", {}).get("assets")
                            if manifest_assets:
                                data["assets"] = manifest_assets
                                logger.info("[sync_creatives] Using assets from creative_manifest")

                        creative_manifest_out2 = build_result.get("creative_manifest", {})
                        if creative_manifest_out2.get("output_format"):
                            output_format = creative_manifest_out2["output_format"]
                            data["output_format"] = output_format

                            # Only use generative URL if user didn't provide one
                            if isinstance(output_format, dict) and output_format.get("url"):
                                if not data.get("url"):
                                    data["url"] = output_format["url"]
                                    logger.info(f"[sync_creatives] Got URL from generative output: {data['url']}")
                                else:
                                    logger.info(
                                        "[sync_creatives] Preserving user-provided URL, "
                                        "not overwriting with generative output"
                                    )

                        # Extract url/width/height from top-level creative_manifest_out2 (unconditional)
                        # These are set by the creative-agent for rendered formats (e.g. text_ad_search → HTML preview)
                        if creative_manifest_out2.get("url") and not data.get("url"):
                            data["url"] = creative_manifest_out2["url"]
                            logger.info(
                                f"[sync_creatives] Got URL from generative output manifest (create): {data['url']}"
                            )
                        if creative_manifest_out2.get("width") and not data.get("width"):
                            data["width"] = creative_manifest_out2["width"]
                        if creative_manifest_out2.get("height") and not data.get("height"):
                            data["height"] = creative_manifest_out2["height"]

                        # Persist brand from the media buy request so adapters can route by domain
                        # (e.g. Affilizz domain guard in SiteplugCreativeManager._sync_text_ad_to_affilizz).
                        # brand is buyer-supplied creative metadata — not a generation decision.
                        if not data.get("brand"):
                            if media_buy_brand:
                                data["brand"] = media_buy_brand
                                changes.append("brand")
                            elif getattr(creative, "brand", None):
                                brand_val = creative.brand
                                data["brand"] = brand_val if isinstance(brand_val, dict) else {"domain": str(brand_val)}
                                changes.append("brand")

                        logger.info(
                            f"[sync_creatives] Generative creative built: "
                            f"status={data.get('generative_status')}, "
                            f"context_id={data.get('generative_context_id')}"
                        )

                    # Skip preview_creative call since we already have the output
                    preview_result = None
                else:
                    # Static creative - use preview_creative
                    # Build AdCP-compliant creative manifest
                    format_id_str = creative_format.id
                    creative_manifest: dict[str, Any] = {
                        "format_id": {
                            "id": creative_format.id,
                            "agent_url": str(format_obj.agent_url),
                        },
                        "assets": _validate_creative_assets(creative.assets) if creative.assets else {},
                    }
                    # Optional extras (non-spec but harmless for agent context)
                    if creative.name:
                        creative_manifest["name"] = creative.name
                    if data.get("url"):
                        creative_manifest["url"] = data.get("url")

                    # Call creative agent's preview_creative for validation + preview
                    logger.info(
                        f"[sync_creatives] Calling preview_creative for validation: {format_id_str} "
                        f"from agent {format_obj.agent_url}, has_assets={bool(creative.assets)}, "
                        f"has_url={bool(data.get('url'))}"
                    )

                    preview_result = run_async_in_sync_context(
                        registry.preview_creative(
                            agent_url=format_obj.agent_url,
                            format_id=format_id_str,
                            creative_manifest=creative_manifest,
                        )
                    )

                # Extract preview data and store in data field
                if preview_result and preview_result.get("previews"):
                    # Store full preview response for UI (per AdCP PR #119)
                    # This preserves all variants and renders for UI display
                    data["preview_response"] = preview_result

                    # Also extract primary preview URL for backward compatibility
                    first_preview = preview_result["previews"][0]
                    renders = first_preview.get("renders", [])
                    if renders:
                        first_render = renders[0]

                        # Only use preview URL if user didn't provide one
                        if first_render.get("preview_url") and not data.get("url"):
                            data["url"] = first_render["preview_url"]
                            logger.info(f"[sync_creatives] Got preview URL from creative agent: {data['url']}")
                        elif data.get("url"):
                            logger.info(
                                "[sync_creatives] Preserving user-provided URL from assets, "
                                "not overwriting with preview URL"
                            )

                        # Only use preview dimensions if user didn't provide them
                        dimensions = first_render.get("dimensions", {})
                        if dimensions.get("width") and not data.get("width"):
                            data["width"] = dimensions["width"]
                        if dimensions.get("height") and not data.get("height"):
                            data["height"] = dimensions["height"]
                        if dimensions.get("duration") and not data.get("duration"):
                            data["duration"] = dimensions["duration"]

                    logger.info(
                        f"[sync_creatives] Preview data populated: "
                        f"url={bool(data.get('url'))}, "
                        f"width={data.get('width')}, "
                        f"height={data.get('height')}, "
                        f"variants={len(preview_result.get('previews', []))}"
                    )
                else:
                    # Preview generation returned no previews
                    # Only acceptable if creative has a media_url (direct URL to creative asset)
                    has_media_url = bool(getattr(creative, "url", None) or data.get("url"))

                    if has_media_url:
                        # Static creatives with media_url don't need previews
                        warning_msg = f"Preview generation returned no previews for {creative_id} (static creative with media_url)"
                        logger.warning(f"[sync_creatives] {warning_msg}")
                        # Continue with creative creation - preview is optional for static creatives
                    else:
                        # Creative agent should have generated previews but didn't
                        error_msg = f"Preview generation failed for {creative_id}: no previews returned and no media_url provided"
                        logger.error(f"[sync_creatives] {error_msg}")
                        return (_failed_sync_result(creative_id, error_msg), False)

        except Exception as validation_error:
            # Creative agent validation failed (network error, agent down, etc.)
            # Do NOT store the creative - it needs validation before acceptance
            error_msg = (
                f"Creative agent unreachable or validation error: {str(validation_error)}. "
                f"Retry recommended - creative agent may be temporarily unavailable."
            )
            logger.error(
                f"[sync_creatives] {error_msg} - rejecting creative {creative_id}",
                exc_info=True,
            )
            return (_failed_sync_result(creative_id, error_msg), False)

    # Determine creative status based on approval mode

    # Create initial creative with pending_review status (will be updated based on approval mode)
    creative_status = CreativeStatusEnum.pending_review.value
    needs_approval = False

    # Extract complete format info including parameters (AdCP 2.5)
    # Use validated format_value (already auto-upgraded from string)
    format_info = _extract_format_info(format_value)

    db_creative = creative_repo.create(
        creative_id=creative.creative_id or None,
        name=creative.name,
        agent_url=format_info["agent_url"],
        format=format_info["format_id"],
        format_parameters=cast(dict | None, format_info["parameters"]),
        principal_id=principal_id,
        status=creative_status,
        data=data,
    )

    # Update creative_id if it was generated (i6k: model attribute assignment)
    if not creative.creative_id:
        creative.creative_id = db_creative.creative_id

    # Now apply approval mode logic
    if approval_mode == "auto-approve":
        db_creative.status = CreativeStatusEnum.approved.value
        needs_approval = False
    elif approval_mode == "ai-powered":
        # Submit to background AI review (async)

        from src.admin.blueprints.creatives import (
            _ai_review_executor,
            _ai_review_lock,
            _ai_review_tasks,
        )

        # Set status to pending_review for AI review
        db_creative.status = CreativeStatusEnum.pending_review.value
        needs_approval = True

        # Submit background task
        task_id = f"ai_review_{db_creative.creative_id}_{uuid.uuid4().hex[:8]}"

        # Import the async function
        from src.admin.blueprints.creatives import _ai_review_creative_async

        future = _ai_review_executor.submit(
            _ai_review_creative_async,
            creative_id=db_creative.creative_id,
            tenant_id=tenant["tenant_id"],
            webhook_url=webhook_url,
            slack_webhook_url=tenant.get("slack_webhook_url"),
            principal_name=principal_id,
        )

        # Track the task
        with _ai_review_lock:
            _ai_review_tasks[task_id] = {
                "future": future,
                "creative_id": db_creative.creative_id,
                "created_at": time.time(),
            }

        logger.info(
            f"[sync_creatives] Submitted AI review for new creative {db_creative.creative_id} (task: {task_id})"
        )
    else:  # require-human
        db_creative.status = CreativeStatusEnum.pending_review.value
        needs_approval = True

    return (
        SyncCreativeResult(
            creative_id=db_creative.creative_id,
            action="created",
            status=db_creative.status,
            platform_id=None,
            review_feedback=None,
            assigned_to=None,
            assignment_errors=None,
        ),
        needs_approval,
    )
