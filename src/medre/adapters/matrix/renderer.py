"""Matrix renderer for target-specific event rendering.

The :class:`MatrixRenderer` converts canonical events into Matrix-ready
content payloads (``m.room.message`` dicts with ``msgtype``, ``body``,
optional ``m.relates_to``, and a MEDRE metadata envelope).

This renderer is owned by the Matrix adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"matrix"``, the renderer
matches on that platform string directly.

**Tranche 1 scope**: text messages and native replies are supported.
Reactions are deferred to a later tranche.
"""
from __future__ import annotations

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import build_reply_body
from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult
from medre.interop.mmrelay import KEY_ID, KEY_LONGNAME, KEY_SHORTNAME, KEY_MESHNET, KEY_PORTNUM, KEY_TEXT, PORTNUM_TEXT

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

        # Apply relay prefix for mesh→Matrix direction
        body = self._apply_matrix_relay_prefix(event, body)

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
