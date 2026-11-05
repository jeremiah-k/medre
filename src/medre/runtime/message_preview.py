"""Synthetic, SDK-free rendering preview for an already-configured route.

The preview invokes the runtime's registered pure renderers and route-expansion
compiler. It does not enter the delivery pipeline, persist an event, create a
send grant, or attempt provider I/O. Its strategy is a synthetic demonstration,
not a resolved capability or a prediction of native acceptance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from medre.config.model import RuntimeConfig
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import RouteDestination
from medre.runtime.builder import _register_adapter_renderers
from medre.runtime.route_plan import RoutePlanLeg, build_route_plan

PREVIEW_FORMAT = "medre-synthetic-message-preview"
SAMPLE_BODY = "MEDRE synthetic preview — no message was sent."
PREVIEW_LIMITATIONS = (
    "Synthetic source and text; real sender metadata and relations are not available.",
    "Renderer output is not delivery planning, adapter acceptance, or a recipient witness.",
    "No SDK session, outbox, receipt, native-send grant, or network operation occurs.",
)


def _text_projection(result: RenderingResult) -> tuple[str | None, list[str]]:
    """Display only text that a known native renderer produced; never dump payloads."""
    if result.radio_plan is not None:
        parts = [part.wire_text for part in result.radio_plan.parts]
        if parts:
            return "\n".join(parts), parts
    payload = result.payload
    for key in ("body", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value, [value]
    operation = payload.get("_matrix_operation")
    if isinstance(operation, dict) and operation.get("kind") == "send_event":
        content = operation.get("content")
        if isinstance(content, dict):
            body = content.get("body")
            if isinstance(body, str):
                return body, [body]
    return None, []


def _destination(leg: RoutePlanLeg) -> RouteDestination | None:
    if leg.dest_destination_kind is None:
        return None
    return RouteDestination(
        kind=leg.dest_destination_kind,
        destination_hash=leg.dest_destination_hash,
        destination_name=leg.dest_destination_name,
    )


def _synthetic_event(
    leg: RoutePlanLeg, *, repeat: int, reply: bool, event_kind: str
) -> CanonicalEvent:
    relations = (
        (
            EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=None,
                key=None,
                fallback_text="an earlier synthetic message",
            ),
        )
        if reply
        else ()
    )
    return CanonicalEvent(
        event_id="medre-synthetic-preview-event",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_adapter=leg.source_adapter_id,
        source_transport_id="synthetic-preview-source",
        source_channel_id=leg.source_channel,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": SAMPLE_BODY * repeat, "displayname": "Preview Sender"},
        metadata=EventMetadata(),
    )


async def build_message_preview(
    config: RuntimeConfig,
    *,
    route_id: str,
    repeat: int = 1,
    relation: Literal["none", "reply"] = "none",
) -> dict[str, Any]:
    """Render bounded synthetic messages for every enabled leg of one route.

    A route-level conflict, disabled leg, missing renderer, or renderer refusal
    never becomes a green success. No exception text or raw native payload is
    exported; some native renderers intentionally refuse synthetic relations.
    """
    if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 32:
        raise ValueError("repeat must be an integer from 1 to 32")
    if relation not in ("none", "reply"):
        raise ValueError("relation must be none or reply")
    plan = build_route_plan(config)
    if plan.conflicts or any(route.error is not None for route in plan.routes):
        raise ValueError("Route configuration contains expansion errors or conflicts")
    matches = [route for route in plan.routes if route.route_id == route_id]
    if len(matches) != 1 or not matches[0].enabled or not matches[0].legs:
        raise ValueError("Route is absent, disabled, or has no resolved destinations")
    if len(matches[0].legs) > 128:
        raise ValueError("Route preview exceeds the 128-leg display limit")
    route_config = next(rc for rc in config.routes.routes if rc.route_id == route_id)
    allowed_kinds = (
        route_config.policy.allowed_event_types
        if route_config.policy is not None
        else None
    )
    if not allowed_kinds or "message.created" in allowed_kinds:
        event_kind = "message.created"
    elif "message.text" in allowed_kinds:
        event_kind = "message.text"
    else:
        raise ValueError("Route does not permit a synthetic text event")

    pipeline = RenderingPipeline()
    _register_adapter_renderers(pipeline, config)
    pipeline.register(TextRenderer(), priority=100)
    known_targets = {
        adapter_id: rtc for _transport, adapter_id, rtc in config.adapters.all_configs()
    }
    outputs: list[dict[str, Any]] = []
    for leg in matches[0].legs:
        record: dict[str, Any] = {
            "expanded_route_id": leg.expanded_route_id,
            "source_adapter": leg.source_adapter_id,
            "target_adapter": leg.dest_adapter_id,
            "target_transport": leg.dest_transport,
            "strategy": "fallback_text" if relation == "reply" else "direct",
            "status": "refused",
        }
        rtc = known_targets.get(leg.dest_adapter_id)
        if rtc is None or not rtc.enabled:
            record["reason"] = "target_not_enabled"
            outputs.append(record)
            continue
        target_config = getattr(rtc, "config", None)
        char_budget = getattr(target_config, "max_text_chars", None)
        byte_budget = getattr(target_config, "max_text_bytes", None)
        if not isinstance(char_budget, int) or isinstance(char_budget, bool):
            char_budget = None
        if not isinstance(byte_budget, int) or isinstance(byte_budget, bool):
            byte_budget = None
        event = _synthetic_event(
            leg, repeat=repeat, reply=relation == "reply", event_kind=event_kind
        )
        try:
            result = await pipeline.render(
                event,
                target_adapter=leg.dest_adapter_id,
                target_channel=leg.dest_channel,
                target_platform=leg.dest_transport,
                max_text_chars=char_budget,
                max_text_bytes=byte_budget,
                delivery_strategy="fallback_text" if relation == "reply" else "direct",
                capability_level="fallback" if relation == "reply" else "native",
                source_origin_label=leg.source_origin_label,
                target_destination=_destination(leg),
            )
            if (
                result.event_id != event.event_id
                or result.target_adapter != leg.dest_adapter_id
                or result.target_channel != leg.dest_channel
            ):
                raise ValueError("Renderer returned mismatched target identity")
            text, segments = _text_projection(result)
            if text is None:
                record["reason"] = "non_text_payload"
            else:
                record.update(
                    status="rendered",
                    renderer=(
                        result.rendering_evidence.renderer
                        if result.rendering_evidence is not None
                        else "unknown"
                    ),
                    text=text,
                    segments=segments,
                    text_chars=len(text),
                    text_bytes=len(text.encode("utf-8")),
                    truncated=result.truncated,
                    fallback_applied=result.fallback_applied,
                )
        except Exception:
            # Renderer exceptions can quote config values or native identities.
            record["reason"] = "renderer_refusal"
        outputs.append(record)
    return {
        "format": PREVIEW_FORMAT,
        "version": 1,
        "evidence_level": "offline_renderer_only",
        "source": "synthetic",
        "route_id": route_id,
        "relation_probe": relation,
        "sample_event_kind": event_kind,
        "status": (
            "rendered"
            if all(v["status"] == "rendered" for v in outputs)
            else "incomplete"
        ),
        "legs": outputs,
        "limitations": list(PREVIEW_LIMITATIONS),
    }
