"""Meshtastic renderer for target-specific event rendering.

The :class:`MeshtasticRenderer` converts canonical events into
Meshtastic-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

When initialised with a :class:`~medre.adapters.meshtastic.config.MeshtasticConfig`
that contains a non-empty ``relay_prefix``, the renderer prepends a formatted
prefix to the message text.  The prefix template uses Python ``str.format()``
syntax with the following variables:

* ``{longname}`` — sender long name (from event native metadata, if available).
* ``{shortname}`` — sender short name (from event native metadata, if available).
* ``{meshnet_name}`` — the mesh network name from the adapter config.
* ``{from_id}`` — the sender's numeric node ID.

This renderer is owned by the Meshtastic adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshtastic"``, the renderer
matches on that platform string directly.

**Tranche 1 scope**: text messages only.  Length-limit enforcement is
noted but not applied; full enforcement is deferred to a later tranche.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult

if TYPE_CHECKING:
    from medre.adapters.meshtastic.config import MeshtasticConfig


class MeshtasticRenderer:
    """Renderer for Meshtastic transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    When *config* is provided and ``config.relay_prefix`` is non-empty,
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
        self._relay_prefix = config.relay_prefix if config else ""
        self._meshnet_name = config.meshnet_name if config else ""

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"meshtastic"``.

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
    # Prefix formatting
    # ------------------------------------------------------------------

    def _format_prefix(self, event: CanonicalEvent) -> str:
        """Format ``relay_prefix`` template using event source metadata.

        Available template variables:

        * ``{longname}`` — sender long name from native metadata.
        * ``{shortname}`` — sender short name from native metadata.
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
        if not self._relay_prefix:
            return ""

        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        format_vars = {
            "longname": str(native_data.get("longname", "")),
            "shortname": str(native_data.get("shortname", "")),
            "meshnet_name": self._meshnet_name,
            "from_id": str(native_data.get("from_id", event.source_transport_id or "")),
        }

        try:
            return self._relay_prefix.format(**format_vars)
        except (KeyError, IndexError, ValueError):
            # If the template references unknown variables, return it as-is
            # so the message still goes through with an unformatted prefix.
            return self._relay_prefix

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
          configured ``relay_prefix`` prepended if set.
        * ``channel_index``: parsed from *target_channel* or ``0``.
        * ``meshnet_name``: the configured mesh network name.

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
        text = str(event.payload.get("body", event.payload.get("text", "")))

        # Prepend relay prefix when configured
        prefix = self._format_prefix(event)
        if prefix:
            text = f"{prefix}{text}"

        # Parse channel index from target_channel
        channel_index = 0
        if target_channel is not None:
            try:
                channel_index = int(target_channel)
            except (ValueError, TypeError):
                channel_index = 0

        content: dict[str, object] = {
            "text": text,
            "channel_index": channel_index,
            "meshnet_name": self._meshnet_name,
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
        }
        if prefix:
            metadata["relay_prefix"] = prefix

        # TODO(tranche-N): Meshtastic has a ~228 byte payload limit.
        # Future tranches should enforce truncation here.
        # For now we pass through.
        truncated = False

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            truncated=truncated,
        )
