"""Reference plugin proving the plugin contract end to end.

``text_stats`` is the canonical first-party example: it subscribes to the
enrichment stage, derives word and character counts for text messages as a
``plugin.custom`` event with a ``text_stats`` sub-kind in the payload, and
requires no privileged capabilities, no configuration, and no external SDKs.
It is the reference implementation and conformance fixture for the contract;
MEDRE registers no providers itself, so it is deliberately not exposed
through an entry point.
"""

from __future__ import annotations

from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.plugins.capabilities import PluginCapability
from medre.plugins.context import PluginContext
from medre.plugins.decisions import Derive, DerivedEvent, EnrichOutcome, Pass
from medre.plugins.spec import PluginSpec
from medre.plugins.stages import PluginStage
from medre.registration import SymbolRef

__all__ = ["TextStatsPlugin", "plugin_spec"]


class TextStatsPlugin:
    """Derive ``plugin.custom`` text statistics for plain text messages."""

    _context: PluginContext | None = None

    async def initialize(self, context: PluginContext) -> None:
        self._context = context

    async def enrich(self, event: CanonicalEvent) -> EnrichOutcome:
        if event.event_kind not in {EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED}:
            return Pass()
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            return Pass()
        return Derive(
            (
                DerivedEvent(
                    kind=EventKind.PLUGIN_CUSTOM,
                    payload={
                        "subtype": "text_stats",
                        "chars": len(text),
                        "words": len(text.split()),
                    },
                ),
            )
        )

    async def shutdown(self) -> None:
        self._context = None


def plugin_spec() -> PluginSpec:
    """Return the declarative registration for the reference plugin."""
    return PluginSpec(
        plugin_id="text_stats",
        version="1.0.0",
        capabilities=frozenset(
            {PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS}
        ),
        stages=frozenset({PluginStage.ENRICH}),
        plugin=SymbolRef(__name__, "TextStatsPlugin"),
    )
