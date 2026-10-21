"""Host-built plugin invocation fixtures independent of installed providers."""

from datetime import datetime, timezone

from medre.plugins.decisions import Pass
from medre.plugins.records import PluginExecutionKey, PluginExecutionRecord
from medre.plugins.stages import PluginStage


def make_record(**overrides) -> PluginExecutionRecord:
    values = dict(
        key=PluginExecutionKey("evt-1", "text_stats", "one", PluginStage.ENRICH),
        attempt=1,
        plugin_version="1.0.0",
        config_digest="a" * 64,
        plan_digest="b" * 64,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decision=Pass(),
    )
    values.update(overrides)
    return PluginExecutionRecord(**values)
