"""LXMF renderer for target-specific event rendering.

The :class:`LxmfRenderer` converts canonical events into
LXMF-ready content payloads (dicts with ``content``, ``title``,
``fields``, and ``destination_hash``).

This renderer is owned by the LXMF adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"lxmf"``, the renderer
matches on that platform string directly.

**Tranche 1 scope**: text messages with optional title and fields
envelope.  Length-limit enforcement is noted but not applied.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult


class LxmfRenderer:
    """Renderer for LXMF transport targets.

    Produces content dicts with ``content``, ``title``, ``fields``, and
    ``destination_hash``.

    When ``metadata_embedding`` is enabled (default), the renderer
    embeds a MEDRE envelope in the ``fields`` dict containing the
    event ID, relations, and metadata keys.

    Selection is via the pipeline's platform registry.

    Parameters
    ----------
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
    """

    name: str = "lxmf"
    """Platform name this renderer handles."""

    _PLATFORM: str = "lxmf"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        metadata_embedding: bool = True,
    ) -> None:
        self._metadata_embedding = metadata_embedding

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"lxmf"``.

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
        *,
        max_text_chars: int | None = None,
        delivery_strategy: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into an LXMF content payload.

        The rendered payload includes:

        * ``content``: extracted text from the event payload.
        * ``title``: title from the event payload, or empty string.
        * ``fields``: dict with optional MEDRE envelope.
        * ``destination_hash``: empty string placeholder.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the payload is intended for.
        target_channel:
            Target channel identifier (unused for LXMF, carried through).

        Returns
        -------
        RenderingResult
            The rendered LXMF content dict wrapped in a result.
        """
        text = str(event.payload.get("body", event.payload.get("text", "")))
        title = str(event.payload.get("title", ""))

        # Build fields dict with optional MEDRE envelope
        fields: dict[int, Any] = {}

        if self._metadata_embedding:
            meta_keys: dict[str, Any] = {}
            if event.metadata.native is not None:
                meta_keys = dict(event.metadata.native.data)
            fields = LxmfFieldsHelper.embed_envelope(
                fields=fields,
                event_id=event.event_id,
                relations=event.relations,
                metadata=meta_keys,
                source_adapter=event.source_adapter,
                source_transport_id=event.source_transport_id,
                source_channel_id=event.source_channel_id,
                lineage=event.lineage,
            )

        content: dict[str, object] = {
            "content": text,
            "title": title,
            "fields": fields,
            "destination_hash": "",
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
        }

        truncated = False

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            truncated=truncated,
        )
