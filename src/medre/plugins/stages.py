"""Pipeline extension stages a plugin may subscribe to."""

from __future__ import annotations

from enum import Enum

__all__ = ["PluginStage"]


class PluginStage(str, Enum):
    """Extension stages between ``STORE`` and ``ROUTE`` in the pipeline.

    The stages mirror the reserved extension points of
    ``docs/spec/architecture.md`` §2. Their normative semantics, including
    the typed decisions each stage may return, live in
    ``docs/spec/plugins.md``. Executing subscribed plugins in the pipeline
    arrives with the plugin execution runtime; until then the stages remain
    reserved.
    """

    ENRICH = "enrich"
    TRANSFORM = "transform"
    EVENT_POLICY = "event_policy"
