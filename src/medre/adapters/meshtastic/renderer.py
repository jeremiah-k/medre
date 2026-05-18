"""Meshtastic renderer for target-specific event rendering.

The :class:`MeshtasticRenderer` converts canonical events into
Meshtastic-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

When initialised with a :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`
that contains a non-empty ``radio_relay_prefix``, the renderer prepends a formatted
prefix to the message text.  The prefix template uses Python ``str.format()``
syntax with the following variables:

* ``{longname}`` — sender long name (from event native metadata, if available).
* ``{shortname}`` — sender short name (from event native metadata, if available).
* ``{shortname5}`` — first 5 characters of ``{shortname}`` (or ``{from_id}``
  if shortname is empty).
* ``{meshnet_name}`` — the mesh network name from the adapter config.
* ``{from_id}`` — the sender's numeric node ID.

This renderer is owned by the Meshtastic adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshtastic"``, the renderer
matches on that platform string directly.

**Tranche 1 scope**: text messages only.  No truncation is applied — the
full message text is passed through unchanged, matching mmrelay behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from medre.core.events import CanonicalEvent, EventKind
from medre.core.rendering.renderer import RenderingResult

if TYPE_CHECKING:
    from medre.config.adapters.meshtastic import MeshtasticConfig


class MeshtasticRenderer:
    """Renderer for Meshtastic transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    When *config* is provided and ``config.radio_relay_prefix`` is non-empty,
    the renderer prepends the formatted prefix to the message text using
    event source metadata.

    Selection is via the pipeline's platform registry.
    """

    name: str = "meshtastic"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshtastic"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(self, config: MeshtasticConfig | None = None) -> None:
        self._radio_relay_prefix = config.radio_relay_prefix if config else ""
        self._meshnet_name = config.meshnet_name if config else ""

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    # Event kinds that have a natural plain-text representation for
    # Meshtastic radio transports.
    _SUPPORTED_KINDS: frozenset[str] = frozenset(
        {
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.MESSAGE_EDITED,
            EventKind.MESSAGE_DELETED,
            EventKind.MESSAGE_REACTED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        }
    )

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"meshtastic"``
        and the event kind is supported.

        Parameters
        ----------
        event:
            The canonical event to check.
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
        return (
            target_platform == self._PLATFORM
            and event.event_kind in self._SUPPORTED_KINDS
        )

    # ------------------------------------------------------------------
    # Prefix formatting
    # ------------------------------------------------------------------

    def _format_prefix(self, event: CanonicalEvent) -> str:
        """Format ``radio_relay_prefix`` template using event source metadata.

        Available template variables:

        * ``{longname}`` — sender long name from native metadata.
        * ``{shortname}`` — sender short name from native metadata.
        * ``{shortname5}`` — first 5 chars of shortname (or from_id if empty).
        * ``{meshnet_name}`` — mesh network name from adapter config.
        * ``{from_id}`` — sender node ID from native metadata.

        Falls back to empty strings for any unavailable variables.
        If the template is invalid, returns the raw template unchanged.

        Parameters
        ----------
        event:
            The canonical event whose source metadata is used for formatting.

        Returns
        -------
        str
            The formatted prefix string.
        """
        if not self._radio_relay_prefix:
            return ""

        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        shortname = str(native_data.get("shortname", ""))
        from_id = str(native_data.get("from_id", event.source_transport_id or ""))
        # shortname5: first 5 chars of shortname, or first 5 chars of from_id
        shortname5 = (shortname or from_id)[:5]

        format_vars = {
            "longname": str(native_data.get("longname", "")),
            "shortname": shortname,
            "shortname5": shortname5,
            "meshnet_name": self._meshnet_name,
            "from_id": from_id,
        }

        try:
            return self._radio_relay_prefix.format(**format_vars)
        except (KeyError, IndexError, ValueError):
            # If the template references unknown variables, return it as-is
            # so the message still goes through with an unformatted prefix.
            return self._radio_relay_prefix

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into a Meshtastic content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload, with the
          configured ``radio_relay_prefix`` prepended if set.
        * ``channel_index``: parsed from *target_channel* or ``0``.
        * ``meshnet_name``: the configured mesh network name.

        **Relation rendering** — when the event carries relations:

        * *reply* with a numeric ``target_native_ref.native_message_id`` —
          sets ``reply_id`` (int) and emits plain text without fallback
          prefix.
        * *reply* without numeric native ref — falls back to
          ``"[replying to: {fallback_text}] "`` prefix.
        * *reaction* with a numeric ``target_native_ref.native_message_id`` —
          sets ``reply_id`` (int) and ``emoji`` (1); text is the emoji
          string from ``relation.key`` or payload ``key``/``body``.
        * *reaction* without numeric native ref — emits readable fallback
          text ``"[reacted: {emoji}]"`` with no ``emoji`` field.

        No truncation is applied — the full message text is passed
        through unchanged.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the payload is intended for.
        target_channel:
            Target channel identifier; parsed as an integer channel index.

        Returns
        -------
        RenderingResult
            The rendered Meshtastic content dict wrapped in a result.
        """
        # Parse channel index from target_channel
        channel_index = 0
        if target_channel is not None:
            try:
                channel_index = int(target_channel)
            except (ValueError, TypeError):
                channel_index = 0

        content: dict[str, object] = {
            "channel_index": channel_index,
            "meshnet_name": self._meshnet_name,
        }

        # -- Structured reply / reaction rendering ----------------------------
        is_structured_reaction = False
        if event.relations:
            rel = event.relations[0]
            reply_id = self._native_reply_id_from_relation(rel)

            if rel.relation_type == "reply" and reply_id is not None:
                # Native structured reply — plain text, numeric reply_id
                content["text"] = self._plain_text(event)
                content["reply_id"] = reply_id
            elif rel.relation_type == "reaction":
                emoji_text = rel.key or str(
                    event.payload.get("key", event.payload.get("body", ""))
                )
                if reply_id is not None:
                    # Native structured reaction — emoji text, reply_id, emoji=1
                    content["text"] = emoji_text
                    content["reply_id"] = reply_id
                    content["emoji"] = 1
                    is_structured_reaction = True
                else:
                    # Fallback readable reaction text, no emoji field
                    content["text"] = f"[reacted: {emoji_text}]"
            else:
                # Other relation types or reply without native ref — fallback
                content["text"] = self._extract_text(event)
        else:
            content["text"] = self._extract_text(event)

        # Prepend relay prefix when configured (skip for emoji-only reactions)
        prefix = ""
        if not is_structured_reaction:
            prefix = self._format_prefix(event)
            if prefix:
                content["text"] = f"{prefix}{content['text']}"

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(str(content.get("text", ""))),
        }
        if prefix:
            metadata["radio_relay_prefix"] = prefix

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(event: CanonicalEvent) -> str:
        """Extract the full text from *event* without truncation.

        When the event carries a reply relation with ``fallback_text``,
        the text is augmented with a ``[replying to: ...]`` prefix
        before further processing.
        """
        # -- Relation fallback rendering ------------------------------------
        if event.relations:
            rel = event.relations[0]
            if rel.relation_type == "reply" and rel.fallback_text:
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                return f"[replying to: {rel.fallback_text}] {payload_text}"

        return str(event.payload.get("body", event.payload.get("text", "")))

    @staticmethod
    def _native_reply_id_from_relation(relation: object) -> int | None:
        """Extract a numeric reply ID from a relation's target native ref.

        Returns the ``native_message_id`` parsed as ``int`` when the
        relation carries a :class:`~medre.core.events.canonical.NativeRef`
        whose ``native_message_id`` is a numeric string, otherwise
        ``None``.
        """
        ref = getattr(relation, "target_native_ref", None)
        if ref is None:
            return None
        mid = getattr(ref, "native_message_id", None)
        if mid is None:
            return None
        try:
            return int(mid)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _plain_text(event: CanonicalEvent) -> str:
        """Extract plain text without relation fallback formatting."""
        return str(event.payload.get("body", event.payload.get("text", "")))
