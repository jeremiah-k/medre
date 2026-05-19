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
from medre.core.events import CanonicalEvent, EventRelation
from medre.core.rendering.renderer import RenderingResult
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REACTION_KEY,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
    PORTNUM_TEXT,
)


class MatrixRenderer:
    """Renderer for Matrix presentation targets.

    Produces ``m.room.message`` content dicts with ``m.text`` msgtype,
    a body string, optional relation metadata (replies and reactions),
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
                mx_event_id = self._matrix_target_event_id(rel, target_adapter)
                native_data: dict[str, object] = {}
                if event.metadata and event.metadata.native:
                    native_data = dict(event.metadata.native.data)
                # Extract MMRelay meshtastic_replyId from relation metadata
                rel_meta = getattr(rel, "metadata", {}) or {}
                mmrelay_id = rel_meta.get("meshtastic_reply_id")
                if mmrelay_id in (None, ""):
                    mmrelay_id = native_data.get(KEY_REPLY_ID)
                if mx_event_id:
                    # Matrix-native reply — render m.in_reply_to with Matrix event ID
                    original_text = rel.fallback_text or ""
                    sender = (
                        getattr(rel.target_native_ref, "adapter", "")
                        if rel.target_native_ref
                        else ""
                    )
                    content["body"] = build_reply_body(body, sender, original_text)
                    content["m.relates_to"] = {
                        "m.in_reply_to": {
                            "event_id": mx_event_id,
                        }
                    }
                # Always inject KEY_REPLY_ID when a Matrix-native target or MMRelay metadata is present
                # (used by MMRelay-compatible Matrix consumers)
                mx_reply_id = (
                    mmrelay_id if mmrelay_id not in (None, "") else mx_event_id
                )
                if mx_reply_id not in (None, ""):
                    content[KEY_REPLY_ID] = str(mx_reply_id)

            elif rel.relation_type == "reaction":
                self._render_reaction(rel, body, content, target_adapter, event)

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        # Determine if a reaction relation was rendered (emote fallback sets
        # its own MMRelay metadata, so general injection must be skipped to
        # avoid overwriting KEY_TEXT with the payload body).
        _is_reaction = (
            event.relations and event.relations[0].relation_type == "reaction"
        )

        # Inject mmrelay-compatible metadata when enabled (skip for
        # reactions — _render_reaction already handles all MMRelay keys).
        if self._mmrelay_compat and not _is_reaction:
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
    # Target ID ownership
    # ------------------------------------------------------------------

    @staticmethod
    def _matrix_target_event_id(rel: Any, target_adapter: str) -> str | None:
        """Return a Matrix-native target event ID from a relation, or ``None``.

        A Matrix-native target ID is valid only when the relation's
        ``target_native_ref`` belongs to *target_adapter* and has a
        non-empty ``native_message_id``.

        The canonical ``rel.target_event_id`` is **never** used as a Matrix
        event ID — it is an internal MEDRE canonical ID, not a Matrix
        event ID.
        """
        ref = getattr(rel, "target_native_ref", None)
        if ref is None:
            return None
        adapter = getattr(ref, "adapter", None)
        if adapter != target_adapter:
            return None
        mid = getattr(ref, "native_message_id", None)
        return str(mid) if mid else None

    # ------------------------------------------------------------------
    # Reaction helpers
    # ------------------------------------------------------------------

    _REACTION_SYMBOL_FALLBACK = "\u26a0\ufe0f"  # ⚠️

    @staticmethod
    def _extract_reaction_symbol(rel: EventRelation, event: CanonicalEvent) -> str:
        """Return the reaction emoji/symbol with a fallback chain.

        Preference order: ``rel.key``, ``event.payload['key']``,
        ``event.payload['body']``.  Leading/trailing whitespace is
        stripped.  Falls back to ⚠️ when all sources are blank.
        """
        for source in (
            rel.key,
            event.payload.get("key"),
            event.payload.get("body"),
        ):
            if source is not None:
                stripped = str(source).strip()
                if stripped:
                    return stripped
        return MatrixRenderer._REACTION_SYMBOL_FALLBACK

    @staticmethod
    def _extract_original_text(rel: EventRelation, event: CanonicalEvent) -> str:
        """Return the original message text preview for a reaction.

        Preference order:

        1. ``rel.metadata['meshtastic_text']`` or ``rel.metadata['text']``
        2. ``rel.fallback_text``
        3. Event native metadata ``meshtastic_text`` or ``text``
        4. Empty string
        """
        # 1. Relation metadata (set by pipeline enrichment / codec)
        rel_meta = getattr(rel, "metadata", {}) or {}
        text = rel_meta.get("meshtastic_text") or rel_meta.get("text")
        if text:
            return str(text)

        # 2. Fallback text on the relation
        if rel.fallback_text:
            return str(rel.fallback_text)

        # 3. Event native metadata fields
        if event.metadata and event.metadata.native:
            native_data = event.metadata.native.data
            text = native_data.get("meshtastic_text") or native_data.get("text")
            if text:
                return str(text)

        # 4. Empty string
        return ""

    @staticmethod
    def _abbreviate_text(text: str, max_len: int = 40) -> str:
        """Normalise newlines to spaces and truncate with ``...`` when long."""
        normalized = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        # Collapse consecutive spaces from mixed line-ending replacement
        while "  " in normalized:
            normalized = normalized.replace("  ", " ")
        if len(normalized) > max_len:
            return normalized[:max_len] + "..."
        return normalized

    def _format_reaction_prefix(self, event: CanonicalEvent) -> str:
        """Format the configured relay prefix for a reaction emote body.

        Returns the formatted prefix string (may be empty when no prefix
        template is configured).  Longname / shortname are preserved
        exactly as received — no truncation or case folding.
        """
        if not self._matrix_relay_prefix:
            return ""
        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)
        try:
            return self._matrix_relay_prefix.format(
                longname=native_data.get("longname", ""),
                shortname=native_data.get("shortname", ""),
                meshnet_name=self._meshnet_name,
                from_id=native_data.get("from_id", ""),
            )
        except (KeyError, IndexError, ValueError):
            return self._matrix_relay_prefix

    # ------------------------------------------------------------------
    # Reaction rendering
    # ------------------------------------------------------------------

    def _render_reaction(
        self,
        rel: EventRelation,
        body: str,
        content: dict[str, object],
        target_adapter: str,
        event: CanonicalEvent,
    ) -> None:
        """Render a reaction relation into the Matrix content dict.

        When a Matrix-native target ID (owned by *target_adapter*) is
        available and mmrelay_compat is false, produces a true
        ``m.reaction`` via an internal ``_matrix_event_type`` key
        (consumed by the adapter).

        When mmrelay_compat is true or no Matrix-native target exists,
        falls back to an ``m.emote`` with MMRelay-compatible body and
        full mesh metadata.

        The canonical ``rel.target_event_id`` is **never** used as a
        Matrix event ID — it is an internal MEDRE canonical ID.
        """
        mx_event_id = self._matrix_target_event_id(rel, target_adapter)

        # Extract MMRelay reply ID from relation metadata for fallback
        rel_meta = getattr(rel, "metadata", {}) or {}

        if mx_event_id is not None and not self._mmrelay_compat:
            # True Matrix reaction — adapter will use _matrix_event_type
            # Remove default msgtype/body set at top of render()
            content.pop("msgtype", None)
            content.pop("body", None)
            symbol = self._extract_reaction_symbol(rel, event)
            content["m.relates_to"] = {
                "rel_type": "m.annotation",
                "event_id": mx_event_id,
                "key": symbol,
            }
            # Internal key consumed by adapter; never leaks to homeserver
            content["_matrix_event_type"] = "m.reaction"
        else:
            # mmrelay_compat or missing Matrix-native target → m.emote fallback
            symbol = self._extract_reaction_symbol(rel, event)
            original_text = self._abbreviate_text(
                self._extract_original_text(rel, event)
            )
            prefix = self._format_reaction_prefix(event)

            if not prefix or not prefix.strip():
                emote_body = f'\n reacted {symbol} to "{original_text}"'
            elif prefix[-1].isspace():
                emote_body = f'\n {prefix}reacted {symbol} to "{original_text}"'
            else:
                emote_body = f'\n {prefix} reacted {symbol} to "{original_text}"'

            content["msgtype"] = "m.emote"
            content["body"] = emote_body
            content[KEY_EMOJI] = EMOJI_FLAG_VALUE
            content[KEY_REACTION_KEY] = symbol

            # KEY_TEXT: original text preview (not reaction emoji)
            content[KEY_TEXT] = original_text

            # KEY_REPLY_ID: prefer meshtastic_reply_id from metadata,
            # fall back to the Matrix-native target event ID when available.
            mmrelay_reply_id = rel_meta.get("meshtastic_reply_id")
            if mmrelay_reply_id not in (None, ""):
                content[KEY_REPLY_ID] = str(mmrelay_reply_id)
            elif mx_event_id not in (None, ""):
                content[KEY_REPLY_ID] = str(mx_event_id)

            # Mesh provenance metadata
            native_data: dict[str, object] = {}
            if event.metadata and event.metadata.native:
                native_data = dict(event.metadata.native.data)

            content[KEY_ID] = str(native_data.get("packet_id", ""))
            content[KEY_LONGNAME] = str(native_data.get("longname", ""))
            content[KEY_SHORTNAME] = str(native_data.get("shortname", ""))
            content[KEY_MESHNET] = self._meshnet_name
            content[KEY_PORTNUM] = PORTNUM_TEXT

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
