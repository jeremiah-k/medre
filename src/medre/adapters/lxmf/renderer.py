"""LXMF renderer for target-specific event rendering.

The :class:`LxmfRenderer` converts canonical events into
LXMF-ready content payloads (dicts with ``text``, ``title``,
``fields``, and ``destination_hash``).

This renderer is owned by the LXMF adapter package and is registered
with the rendering pipeline.

Three selection strategies are available (checked in order, first match
wins):

**Platform match**

When the rendering pipeline's platform registry is populated, the pipeline
passes the target adapter's platform (``"lxmf"``) to ``can_render``.
The renderer matches on this platform string directly.

**Adapter-name prefix**

When ``target_platform`` is ``None``, the renderer checks whether
``target_adapter.startswith("lxmf")``.

**Explicit adapter IDs (``known_adapters``)**

The ``known_adapters`` constructor accepts a set of adapter IDs that this
renderer should handle regardless of naming convention.

**Tranche 1 scope**: text messages with optional title and fields
envelope.  Length-limit enforcement is noted but not applied.
"""
from __future__ import annotations

from typing import Any

from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult

from medre.adapters.lxmf.fields import LxmfFieldsHelper


class LxmfRenderer:
    """Renderer for LXMF transport targets.

    Produces content dicts with ``text``, ``title``, ``fields``, and
    ``destination_hash``.

    When ``metadata_embedding`` is enabled (default), the renderer
    embeds a MEDRE envelope in the ``fields`` dict containing the
    event ID, relations, and metadata keys.

    Three selection strategies are supported: platform match,
    adapter-name prefix, and explicit adapter IDs.

    Parameters
    ----------
    known_adapters:
        Optional set of adapter IDs that this renderer should handle.
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
    """

    name: str = "lxmf"
    """Platform name this renderer handles."""

    _PLATFORM: str = "lxmf"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        known_adapters: set[str] | None = None,
        metadata_embedding: bool = True,
    ) -> None:
        self._known_adapters: set[str] = known_adapters or set()
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
        """Return ``True`` when *target_adapter* is an LXMF target.

        Three selection strategies are checked in order (first match wins):

        1. **Platform match** — ``target_platform == "lxmf"``.
        2. **Adapter-name prefix** — ``target_adapter`` starts with
           ``"lxmf"``.
        3. **Known adapters** — ``target_adapter`` is in the explicit set.

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.
        target_platform:
            Platform name of the target adapter.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        if target_platform == self._PLATFORM:
            return True
        if target_adapter.startswith("lxmf"):
            return True
        return target_adapter in self._known_adapters

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into an LXMF content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload.
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
            )

        content: dict[str, object] = {
            "text": text,
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
