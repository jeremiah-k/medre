"""Matrix renderer for target-specific event rendering.

The :class:`MatrixRenderer` converts canonical events into Matrix-ready
content payloads (``m.room.message`` dicts with ``msgtype``, ``body``,
optional ``m.relates_to``, and a MEDRE metadata envelope).

This renderer is owned by the Matrix adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"matrix"``, the renderer
matches on that platform string directly.

**Supported relation types**: text messages, native replies, and
reactions (true ``m.reaction`` or MMRelay emote fallback).
"""

from __future__ import annotations

from typing import Any

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import build_reply_body
from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult
from medre.interop.mmrelay import (
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
    EMOJI_FLAG_VALUE,
    PORTNUM_TEXT,
)


class MatrixRenderer:
    """Renderer for Matrix presentation targets.

    Produces ``m.room.message`` content dicts with ``m.text`` msgtype,
    a body string, optional relation metadata (replies only in tranche 1),
    and a MEDRE provenance envelope.

    Selection is via the pipeline's platform registry.
    """

    name: str = "matrix"

    _PLATFORM: str = "matrix"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        *,
        mmrelay_compat: bool = False,
        meshnet_name: str = "",
        matrix_relay_prefix: str = "",
    ) -> None:
        self._mmrelay_compat = mmrelay_compat
        self._meshnet_name = meshnet_name
        self._matrix_relay_prefix = matrix_relay_prefix or ""

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"matrix"``.

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.
        target_platform:
            Platform name of the target adapter, supplied by the
            rendering pipeline's platform registry.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        return target_platform == self._PLATFORM

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

        * ``msgtype``: ``"m.text"`` (or ``"m.emote"`` for reaction fallback)
        * ``body``: extracted text from the event payload
        * ``medre.envelope``: provenance metadata
        * ``m.relates_to``: added for replies and reactions

        **Replies** preserve ``m.in_reply_to`` and inject ``KEY_REPLY_ID``
        from native/relation metadata when available.

        **Reactions** render as true ``m.reaction`` (with internal
        ``_matrix_event_type='m.reaction'``) when a target event/native
        Matrix id is available and mmrelay_compat is false.  When
        mmrelay_compat is true or the target is missing, an ``m.emote``
        fallback is rendered with ``KEY_REPLY_ID``, ``KEY_TEXT``,
        ``KEY_EMOJI=1`` and existing fields.

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

        # Apply relay prefix for mesh→Matrix direction
        body = self._apply_matrix_relay_prefix(event, body)

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
        }

        # Handle relations — reply and reaction
        if event.relations:
            rel = event.relations[0]

            if rel.relation_type == "reply":
                native_ref = rel.target_native_ref
                target_event_id = (
                    native_ref.native_message_id
                    if native_ref
                    else (rel.target_event_id or "")
                )
                # Inject KEY_REPLY_ID from native metadata when available
                reply_id = target_event_id
                native_data: dict[str, object] = {}
                if event.metadata and event.metadata.native:
                    native_data = dict(event.metadata.native.data)
                # Prefer relation metadata meshtastic_reply_id or native ref
                mx_reply_id = (
                    native_data.get(KEY_REPLY_ID) or target_event_id
                )
                # Build reply body with fallback quote
                original_text = rel.fallback_text or ""
                sender = native_ref.adapter if native_ref else ""
                content["body"] = build_reply_body(body, sender, original_text)
                content["m.relates_to"] = {
                    "m.in_reply_to": {
                        "event_id": target_event_id,
                    }
                }
                if mx_reply_id:
                    content[KEY_REPLY_ID] = str(mx_reply_id)

            elif rel.relation_type == "reaction":
                self._render_reaction(event, rel, body, content)

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        # Inject mmrelay-compatible metadata when enabled
        if self._mmrelay_compat:
            self._inject_mmrelay_metadata(event, content)

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

    # ------------------------------------------------------------------
    # Reaction rendering
    # ------------------------------------------------------------------

    def _render_reaction(
        self,
        event: CanonicalEvent,
        rel: Any,
        body: str,
        content: dict[str, object],
    ) -> None:
        """Render a reaction relation into the Matrix content dict.

        When a target event/native Matrix ID is available and mmrelay_compat
        is false, produces a true ``m.reaction`` via an internal
        ``_matrix_event_type`` key (consumed by the adapter).

        When mmrelay_compat is true or the target is missing, falls back to
        an ``m.emote`` with MMRelay fields.
        """
        native_ref = rel.target_native_ref
        target_event_id = (
            native_ref.native_message_id
            if native_ref
            else (rel.target_event_id or "")
        )
        emoji = rel.key or body

        # Determine if we can emit a true m.reaction
        has_target = bool(target_event_id)

        if has_target and not self._mmrelay_compat:
            # True Matrix reaction — adapter will use _matrix_event_type
            content["msgtype"] = "m.text"
            content["body"] = emoji
            content["m.relates_to"] = {
                "rel_type": "m.annotation",
                "event_id": target_event_id,
                "key": emoji,
            }
            # Internal key consumed by adapter; never leaks to homeserver
            content["_matrix_event_type"] = "m.reaction"
        else:
            # mmrelay_compat or missing target → m.emote fallback
            content["msgtype"] = "m.emote"
            content["body"] = body
            content[KEY_REPLY_ID] = target_event_id
            content[KEY_TEXT] = body
            content[KEY_EMOJI] = EMOJI_FLAG_VALUE

    # ------------------------------------------------------------------
    # Relay prefix
    # ------------------------------------------------------------------

    def _apply_matrix_relay_prefix(self, event: CanonicalEvent, body: str) -> str:
        """Prepend the configured relay prefix template to *body*.

        When :attr:`_matrix_relay_prefix` is non-empty, the template is formatted
        using variables extracted from the event's native metadata:

        * ``{longname}`` — sender long name.
        * ``{shortname}`` — sender short name.
        * ``{meshnet_name}`` — mesh network name from config.
        * ``{from_id}`` — sender node ID.

        If the prefix is empty, *body* is returned unchanged.
        """
        if not self._matrix_relay_prefix:
            return body

        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        formatted_prefix = self._matrix_relay_prefix.format(
            longname=native_data.get("longname", ""),
            shortname=native_data.get("shortname", ""),
            meshnet_name=self._meshnet_name,
            from_id=native_data.get("from_id", ""),
        )
        return f"{formatted_prefix}{body}"

    # ------------------------------------------------------------------
    # mmrelay compatibility
    # ------------------------------------------------------------------

    def _inject_mmrelay_metadata(
        self,
        event: CanonicalEvent,
        content: dict[str, object],
    ) -> None:
        """Embed mmrelay-compatible mesh metadata into *content*.

        When mmrelay compatibility is enabled, the Matrix content payload
        is augmented with wire-format keys that mirror the fields mmrelay
        consumers expect.  The key names come from
        :mod:`medre.interop.mmrelay` so that the wire contract lives
        outside any single adapter.

        Injected keys (see :mod:`medre.interop.mmrelay` for names):

        * packet ID from native metadata.
        * sender long name from native metadata.
        * sender short name from native metadata.
        * mesh network name from config.
        * hardcoded ``"TEXT_MESSAGE_APP"`` port number.
        * message body/text from the event payload.
        """
        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        text = str(event.payload.get("body", event.payload.get("text", "")))

        content[KEY_ID] = str(native_data.get("packet_id", ""))
        content[KEY_LONGNAME] = str(native_data.get("longname", ""))
        content[KEY_SHORTNAME] = str(native_data.get("shortname", ""))
        content[KEY_MESHNET] = self._meshnet_name
        content[KEY_PORTNUM] = PORTNUM_TEXT
        content[KEY_TEXT] = text
