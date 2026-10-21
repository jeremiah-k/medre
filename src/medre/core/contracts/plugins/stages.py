"""Pipeline extension stages a plugin may subscribe to."""

from __future__ import annotations

from enum import Enum

__all__ = ["PluginStage"]


class PluginStage(str, Enum):
    """Extension stages between ``STORE`` and ``ROUTE`` in the pipeline.

    Explicitly configured execution runs these stages in declaration order.
    Their typed decisions and forward-only continuation semantics live in
    ``docs/spec/plugins.md`` and ``docs/spec/architecture.md`` §2.
    """

    ENRICH = "enrich"
    TRANSFORM = "transform"
    EVENT_POLICY = "event_policy"
