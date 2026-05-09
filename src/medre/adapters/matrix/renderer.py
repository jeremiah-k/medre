"""Matrix renderer for target-specific event rendering.

The :class:`MatrixRenderer` converts canonical events into Matrix-ready
content payloads (``m.room.message`` dicts with ``msgtype``, ``body``,
optional ``m.relates_to``, and a MEDRE metadata envelope).

This renderer is owned by the Matrix adapter package and is registered
with the rendering pipeline.  It dispatches events whose
``target_adapter`` starts with ``"matrix"``.

**Tranche 1 scope**: text messages and native replies are supported.
Reactions are deferred to a later tranche.
"""
from __future__ import annotations

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import build_reply_body
from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult


class MatrixRenderer:
    """Renderer for Matrix presentation targets.

    Produces ``m.room.message`` content dicts with ``m.text`` msgtype,
    a body string, optional relation metadata (replies only in tranche 1),
    and a MEDRE provenance envelope.

    Matches any ``target_adapter`` that starts with ``"matrix"``.
    """

    name: str = "matrix"

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_adapter* is a Matrix target.

        Selection order (first match wins):

        1. **Platform match** — ``target_platform == "matrix"``.
        2. **Adapter-name prefix** — ``target_adapter`` starts with
           ``"matrix"`` (backward compatibility).

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.
        target_platform:
            Platform name of the target adapter.  ``None`` when the
            pipeline registry is not populated.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        if target_platform == "matrix":
            return True
        return target_adapter.startswith("matrix")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into a Matrix content payload.

        The rendered payload includes:

        * ``msgtype``: ``"m.text"``
        * ``body``: extracted text from the event payload
        * ``medre.envelope``: provenance metadata
        * ``m.relates_to``: added when the event carries a reply relation.
          Reaction relations are deferred to a later tranche.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the payload is intended for.
        target_channel:
            Target room ID, if known.

        Returns
        -------
        RenderingResult
            The rendered Matrix content dict wrapped in a result.
        """
        body = str(event.payload.get("body", event.payload.get("text", "")))

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
        }

        # Handle relations — reply only for tranche 1
        if event.relations:
            rel = event.relations[0]

            if rel.relation_type == "reply":
                native_ref = rel.target_native_ref
                target_event_id = (
                    native_ref.native_message_id
                    if native_ref
                    else (rel.target_event_id or "")
                )
                # Build reply body with fallback quote
                original_text = rel.fallback_text or ""
                sender = (
                    native_ref.adapter if native_ref else ""
                )
                content["body"] = build_reply_body(body, sender, original_text)
                content["m.relates_to"] = {
                    "m.in_reply_to": {
                        "event_id": target_event_id,
                    }
                }

            elif rel.relation_type == "reaction":
                # Reaction rendering is deferred to a later tranche.
                # The event body text is still rendered as m.text.
                pass

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        metadata: dict[str, object] = {
            "renderer": self.name,
        }

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
        )
