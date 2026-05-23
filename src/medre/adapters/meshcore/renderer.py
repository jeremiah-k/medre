"""MeshCore renderer for target-specific event rendering.

The :class:`MeshCoreRenderer` converts canonical events into
MeshCore-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

The renderer is initialised with a **required** mapping of adapter IDs to
:class:`~medre.config.adapters.meshcore.MeshCoreConfig` instances.
At render time the config for *target_adapter* is resolved from this
mapping — there is no fallback or default.  An empty mapping raises
:class:`ValueError` at construction; an unknown *target_adapter* raises
:class:`KeyError` at render time.

This renderer is owned by the MeshCore adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshcore"``, the renderer
matches on that platform string directly.  This decouples renderer
selection from adapter naming conventions.

Text messages are supported.  UTF-8 byte-budget truncation is applied
after final text rendering using :attr:`MeshCoreConfig.max_text_bytes`.
Multi-byte UTF-8 codepoints are never split.  Truncation metadata and
the ``RenderingResult.truncated`` flag report whether the text was
trimmed.

**Important**: local send acceptance is not delivery confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult

if TYPE_CHECKING:
    from medre.config.adapters.meshcore import MeshCoreConfig


class MeshCoreRenderer:
    """Renderer for MeshCore transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    **Target-aware rendering.** The renderer is initialised with a
    mapping of adapter IDs to :class:`~medre.config.adapters.meshcore.MeshCoreConfig`
    instances.  At render time the config for *target_adapter* is resolved
    from this mapping.  This allows multi-node setups where each adapter
    has different ``max_text_bytes`` and ``meshnet_name`` values.

    An empty *configs* mapping raises :class:`ValueError`.  An unknown
    *target_adapter* at render time raises :class:`KeyError`.

    Selection is via the pipeline's platform registry.
    """

    name: str = "meshcore"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshcore"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        *,
        configs: Mapping[str, MeshCoreConfig],
    ) -> None:
        if not configs:
            raise ValueError(
                "MeshCoreRenderer requires at least one adapter config. "
                "Pass a non-empty configs mapping."
            )
        self._configs: dict[str, MeshCoreConfig] = dict(configs)

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"meshcore"``
        and *target_adapter* has a registered config.

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
        return target_platform == self._PLATFORM and target_adapter in self._configs

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into a MeshCore content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload, truncated
          to the configured ``max_text_bytes`` UTF-8 byte budget.
        * ``channel_index``: parsed from *target_channel* or ``0``.
        * ``meshnet_name``: the configured mesh network name.

        **Target-aware config resolution.** The renderer resolves the
        config for *target_adapter* from the ``configs`` mapping supplied
        at construction.  If *target_adapter* is not found, a
        :class:`KeyError` is raised — there is no fallback.

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
            The rendered MeshCore content dict wrapped in a result.
        """
        # Resolve target-adapter-specific config.
        try:
            adapter_config = self._configs[target_adapter]
        except KeyError:
            raise KeyError(
                f"No MeshCoreConfig registered for target_adapter "
                f"{target_adapter!r}. Known adapters: "
                f"{sorted(self._configs.keys())}"
            ) from None

        meshnet_name = adapter_config.meshnet_name
        max_text_bytes = adapter_config.max_text_bytes

        text = str(event.payload.get("body", event.payload.get("text", "")))

        # Parse channel index from target_channel
        channel_index = 0
        if target_channel is not None:
            try:
                channel_index = int(target_channel)
            except (ValueError, TypeError):
                channel_index = 0

        # -- UTF-8 byte-budget truncation after final rendering ------
        truncated_text, was_truncated, original_bytes, rendered_bytes = (
            self._truncate_utf8_bytes(text, max_text_bytes)
        )

        content: dict[str, object] = {
            "text": truncated_text,
            "channel_index": channel_index,
            "meshnet_name": meshnet_name,
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(text),
            "rendered_length": len(truncated_text),
            "original_text_bytes": original_bytes,
            "rendered_text_bytes": rendered_bytes,
            "max_text_bytes": max_text_bytes,
            "truncated": was_truncated,
        }

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            truncated=was_truncated,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool, int, int]:
        """Truncate *text* to at most *max_bytes* UTF-8 bytes.

        Parameters
        ----------
        text:
            The text to potentially truncate.
        max_bytes:
            Maximum number of UTF-8 bytes allowed.  Must be >= 0.

        Returns
        -------
        tuple[str, bool, int, int]
            ``(truncated_text, was_truncated, original_byte_count,
            rendered_byte_count)``.
        """
        encoded = text.encode("utf-8")
        original_bytes = len(encoded)

        if max_bytes == 0:
            return ("", original_bytes > 0, original_bytes, 0)

        if original_bytes <= max_bytes:
            return (text, False, original_bytes, original_bytes)

        # Slice to byte budget and decode with errors="ignore" to
        # avoid splitting multi-byte UTF-8 codepoints.
        truncated_bytes = encoded[:max_bytes]
        truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
        rendered_bytes = len(truncated_text.encode("utf-8"))

        return (truncated_text, True, original_bytes, rendered_bytes)
