"""Immutable rendering evidence snapshot for replay-readiness.

A :class:`RenderingEvidence` captures the decision inputs and observable
outcomes of a single rendering pass without duplicating rendered payloads.
It is produced once by the pipeline after a renderer returns and attached
to the :class:`~medre.core.rendering.renderer.RenderingResult`.

**Design constraints**

* Immutable (frozen dataclass).
* JSON-safe via :meth:`to_dict` — all values are plain strs, ints, bools,
  or ``None``.
* No payload duplication — only *metrics* (character/byte counts) are
  recorded, never the rendered text itself.
* Individual renderers do not produce evidence; the pipeline builds it.

Public symbols
--------------
* :class:`RelationTargetEvidence` - per-relation native/fallback evidence.
* :class:`RenderingEvidence` - frozen evidence snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Module-level literal type aliases defined before any class in renderer.py
# — safe to import even when renderer.py is mid-load during circular import.
from medre.core.rendering.renderer import (
    CapabilityLevel,
    DeliveryStrategyMethod,
    FallbackApplied,
)

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent
    from medre.core.rendering.renderer import RenderingContext, RenderingResult

#: Evidence schema version.  Bumped on schema-breaking field changes.
EVIDENCE_SCHEMA_VERSION: str = "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_char_byte_metrics(
    payload: dict[str, object],
    metadata: dict[str, object],
) -> tuple[int | None, int | None, int | None, int | None]:
    """Derive normalised text metrics from *payload* and *metadata*.

    Returns ``(rendered_chars, rendered_bytes, original_chars, original_bytes)``.
    Any unavailable metric is ``None``.

    Resolution order for rendered metrics:
      1. Renderer-provided metadata keys ``rendered_text_chars`` and
         ``rendered_text_bytes`` (when present and ``int``).
      2. Fallback to known payload string keys: ``"text"``, ``"body"``,
         ``"content"`` (first string value wins).
    """
    rendered_chars: int | None = None
    rendered_bytes: int | None = None

    # Prefer explicit renderer metadata when available.
    raw_chars = metadata.get("rendered_text_chars")
    raw_bytes = metadata.get("rendered_text_bytes")
    if isinstance(raw_chars, int):
        rendered_chars = raw_chars
    if isinstance(raw_bytes, int):
        rendered_bytes = raw_bytes

    # Fallback: derive from known payload string keys.
    if rendered_chars is None or rendered_bytes is None:
        for key in ("text", "body", "content"):
            val = payload.get(key)
            if isinstance(val, str):
                if rendered_chars is None:
                    rendered_chars = len(val)
                if rendered_bytes is None:
                    rendered_bytes = len(val.encode("utf-8"))
                break

    original_chars: int | None = None
    raw_orig = metadata.get("original_length")
    if isinstance(raw_orig, int):
        original_chars = raw_orig

    original_bytes: int | None = None
    raw_orig_bytes = metadata.get("original_text_bytes")
    if isinstance(raw_orig_bytes, int):
        original_bytes = raw_orig_bytes

    return rendered_chars, rendered_bytes, original_chars, original_bytes


# ---------------------------------------------------------------------------
# RelationTargetEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationTargetEvidence:
    """Per-relation evidence explaining native vs fallback rendering decision.

    Captures enough structured data to answer:

    * Was this relation rendered natively or did it fall back?
    * Which target was selected (event id, native message id)?
    * Was the target resolved / available?
    * What was the fallback text source?

    Attributes
    ----------
    relation_type:
        The kind of relationship (``"reply"``, ``"reaction"``,
        ``"edit"``, ``"delete"``, ``"thread"``).
    render_mode:
        ``"native"`` when the target adapter handles the relation
        natively; ``"fallback"`` when degraded inline text is used
        instead.  Derived from ``delivery_strategy``,
        ``capability_level``, and ``fallback_applied``.
    target_event_id:
        The canonical event ID of the target event, or ``None`` if
        not resolved.
    target_native_message_id:
        The native message ID from ``target_native_ref`` if available,
        or ``None``.
    target_available:
        ``True`` when ``target_event_id`` is present (the relation
        target was resolved to a canonical event).  ``None`` when
        the target was not resolved or resolution is unknown.  This
        reflects data availability, not adapter validation — callers
        should not assume ``True`` means the native message still
        exists on the target platform.
    fallback_text_source:
        The ``fallback_text`` from the relation, if known, or ``None``.
    """

    relation_type: str
    render_mode: str
    target_event_id: str | None
    target_native_message_id: str | None
    target_available: bool | None
    fallback_text_source: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe plain dict representation."""
        return {
            "relation_type": self.relation_type,
            "render_mode": self.render_mode,
            "target_event_id": self.target_event_id,
            "target_native_message_id": self.target_native_message_id,
            "target_available": self.target_available,
            "fallback_text_source": self.fallback_text_source,
        }


# ---------------------------------------------------------------------------
# Helpers — relation evidence derivation
# ---------------------------------------------------------------------------


def _derive_relation_render_mode(
    relation_type: str,
    delivery_strategy: DeliveryStrategyMethod,
    capability_level: CapabilityLevel,
    fallback_applied: FallbackApplied | None,
    *,
    target_event_id: str | None = None,
    target_native_message_id: str | None = None,
) -> str:
    """Derive the render mode for a single relation.

    Returns ``"native"`` or ``"fallback"`` based on the overall rendering
    context and target availability.  The logic is:

    1. ``delivery_strategy == "fallback_text"`` → all relations fallback.
    2. ``capability_level`` in ``("fallback", "unsupported")`` → all
       relations fallback.
    3. ``fallback_applied`` starts with ``"relation_"`` and its suffix
       matches the relation type → this specific relation fallback.
    4. ``target_event_id`` is ``None`` → no resolved target → fallback.
    5. ``target_native_message_id`` is ``None`` or empty → no usable
       native ref → fallback.
    6. Otherwise → native.
    """
    if delivery_strategy == "fallback_text":
        return "fallback"
    if capability_level in ("fallback", "unsupported"):
        return "fallback"
    if fallback_applied is not None and fallback_applied.startswith("relation_"):
        suffix = fallback_applied[len("relation_") :]
        if suffix == relation_type:
            return "fallback"
    if target_event_id is None:
        return "fallback"
    if not target_native_message_id:
        return "fallback"
    return "native"


def _build_relation_evidence(
    event: CanonicalEvent,
    ctx: RenderingContext,
    fallback_applied: FallbackApplied | None,
) -> tuple[RelationTargetEvidence, ...]:
    """Build per-relation evidence entries from event relations and context."""
    if not event.relations:
        return ()

    entries: list[RelationTargetEvidence] = []
    for rel in event.relations:
        target_native_msg_id: str | None = None
        if rel.target_native_ref is not None:
            target_native_msg_id = rel.target_native_ref.native_message_id

        render_mode = _derive_relation_render_mode(
            relation_type=rel.relation_type,
            delivery_strategy=ctx.delivery_strategy,
            capability_level=ctx.capability_level,
            fallback_applied=fallback_applied,
            target_event_id=rel.target_event_id,
            target_native_message_id=target_native_msg_id,
        )

        target_available: bool | None = None
        if rel.target_event_id is not None:
            target_available = True

        entries.append(
            RelationTargetEvidence(
                relation_type=rel.relation_type,
                render_mode=render_mode,
                target_event_id=rel.target_event_id,
                target_native_message_id=target_native_msg_id,
                target_available=target_available,
                fallback_text_source=rel.fallback_text,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# RenderingEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderingEvidence:
    """Immutable snapshot of rendering decision inputs and outcomes.

    Built by
    :meth:`RenderingPipeline.render()
    <medre.core.rendering.renderer.RenderingPipeline.render>`
    after a renderer returns and attached to the resulting
    :class:`~medre.core.rendering.renderer.RenderingResult`
    via :func:`dataclasses.replace`.

    **Fields classification**

    *Decision inputs* (from :class:`RenderingContext`):
        ``renderer``, ``delivery_strategy``, ``target_adapter``,
        ``target_platform``, ``target_channel``, ``max_text_chars``,
        ``max_text_bytes``, ``capability_level``, ``capability_policy``.

    *Outputs / diagnostics* (from :class:`RenderingResult`):
        ``fallback_applied``, ``truncated``.

    *Derived metrics*:
        ``rendered_text_chars``, ``rendered_text_bytes``,
        ``original_text_chars``, ``original_text_bytes``.

    Attributes
    ----------
    schema_version:
        Evidence schema version string (currently ``"1"``).
    renderer:
        Name of the renderer that produced the result.
    delivery_strategy:
        The resolved delivery strategy method.
    target_adapter:
        Target adapter routing identifier.
    target_platform:
        Platform name, or ``None`` if unknown.
    target_channel:
        Target channel / conversation, or ``None``.
    max_text_chars:
        Character budget from adapter capabilities, or ``None``.
    max_text_bytes:
        UTF-8 byte budget from adapter capabilities, or ``None``.
    capability_level:
        Capability level from the rendering context.  Reflects the
        ``capability_level`` field of
        :class:`~medre.core.rendering.renderer.RenderingContext`,
        which the normal pipeline populates from the capability decision
        (via :class:`CapabilityDecisionResolver`) when available.  Defaults
        to ``"native"`` when no capability decision is supplied.
    capability_policy:
        Optional policy hint from the context, or ``None``.  Reserved
        for forward compatibility; the default pipeline does not set it.
    fallback_applied:
        Fallback reason tag, or ``None`` when no fallback was applied.
    truncated:
        Whether the rendered content was truncated.
    rendered_text_chars:
        Character length of the rendered text payload, or ``None`` if
        the payload does not contain a ``"text"`` field.
    rendered_text_bytes:
        UTF-8 byte length of the rendered text payload, or ``None``.
    original_text_chars:
        Character length of the original (pre-truncation) text as
        reported in result metadata, or ``None`` when unavailable.
    original_text_bytes:
        UTF-8 byte length of the original text as reported in result
        metadata, or ``None`` when unavailable.
    conversation_id:
        Canonical conversation identifier from the event, or ``None``
        when not yet computed.  Populated when the event is available
        at evidence construction time.
    root_event_id:
        Canonical event ID of the root event in a relation chain, or
        ``None`` when not yet computed.  Populated when the event is
        available at evidence construction time.
    relation_evidence:
        Per-relation structured evidence explaining native vs fallback
        rendering decisions for each relation on the event.  Empty
        tuple when the event has no relations or when event data is
        unavailable at evidence construction time.
    """

    # --- Schema ---
    schema_version: str

    # --- Decision inputs (RenderingContext) ---
    renderer: str
    delivery_strategy: DeliveryStrategyMethod
    target_adapter: str
    target_platform: str | None
    target_channel: str | None
    max_text_chars: int | None
    max_text_bytes: int | None
    capability_level: CapabilityLevel
    capability_policy: str | None

    # --- Outputs / diagnostics (RenderingResult) ---
    fallback_applied: FallbackApplied | None
    truncated: bool

    # --- Derived text metrics ---
    rendered_text_chars: int | None
    rendered_text_bytes: int | None
    original_text_chars: int | None
    original_text_bytes: int | None

    # --- Relation / conversation evidence ---
    conversation_id: str | None = None
    root_event_id: str | None = None
    relation_evidence: tuple[RelationTargetEvidence, ...] = ()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_context_and_result(
        cls,
        renderer_name: str,
        ctx: RenderingContext,
        result: RenderingResult,
        *,
        event: CanonicalEvent | None = None,
    ) -> RenderingEvidence:
        """Build evidence from a rendering context and its result.

        Parameters
        ----------
        renderer_name:
            Name of the renderer that produced *result* (from
            ``renderer.name`` in the pipeline loop).
        ctx:
            The frozen rendering context used for this render call.
        result:
            The rendering result returned by the renderer.
        event:
            The canonical event being rendered, or ``None`` when
            event data is unavailable (e.g. manual construction).
            When provided, ``conversation_id``, ``root_event_id``,
            and per-relation evidence are populated from the event.
        """
        rendered_chars, rendered_bytes, original_chars, original_bytes = (
            _text_char_byte_metrics(result.payload, result.metadata)
        )

        conversation_id: str | None = None
        root_event_id: str | None = None
        relation_evidence: tuple[RelationTargetEvidence, ...] = ()

        if event is not None:
            conversation_id = getattr(event, "conversation_id", None)
            root_event_id = getattr(event, "root_event_id", None)
            relation_evidence = _build_relation_evidence(
                event=event,
                ctx=ctx,
                fallback_applied=result.fallback_applied,
            )

        return cls(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            renderer=renderer_name,
            delivery_strategy=ctx.delivery_strategy,
            target_adapter=ctx.target_adapter,
            target_platform=ctx.target_platform,
            target_channel=ctx.target_channel,
            max_text_chars=ctx.max_text_chars,
            max_text_bytes=ctx.max_text_bytes,
            capability_level=ctx.capability_level,
            capability_policy=ctx.capability_policy,
            fallback_applied=result.fallback_applied,
            truncated=result.truncated,
            rendered_text_chars=rendered_chars,
            rendered_text_bytes=rendered_bytes,
            original_text_chars=original_chars,
            original_text_bytes=original_bytes,
            conversation_id=conversation_id,
            root_event_id=root_event_id,
            relation_evidence=relation_evidence,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe plain dict representation.

        All fields are always included (even when ``None``) so that the
        serialised shape is stable and deterministic regardless of whether
        ``json.dumps`` or ``msgspec.json.encode`` is used as the encoder.
        This eliminates shape drift between the ``to_dict`` path and
        ``msgspec`` which includes ``null`` for unset fields.
        """
        return {
            "schema_version": self.schema_version,
            "renderer": self.renderer,
            "delivery_strategy": self.delivery_strategy,
            "target_adapter": self.target_adapter,
            "target_platform": self.target_platform,
            "target_channel": self.target_channel,
            "max_text_chars": self.max_text_chars,
            "max_text_bytes": self.max_text_bytes,
            "capability_level": self.capability_level,
            "capability_policy": self.capability_policy,
            "fallback_applied": self.fallback_applied,
            "truncated": self.truncated,
            "rendered_text_chars": self.rendered_text_chars,
            "rendered_text_bytes": self.rendered_text_bytes,
            "original_text_chars": self.original_text_chars,
            "original_text_bytes": self.original_text_bytes,
            "conversation_id": self.conversation_id,
            "root_event_id": self.root_event_id,
            "relation_evidence": [re.to_dict() for re in self.relation_evidence],
        }
