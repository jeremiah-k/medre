"""Architecture guards for conversation projection authority boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "src/medre/runtime/app.py"
_STORAGE = _ROOT / "src/medre/core/storage/sqlite"


def test_runtime_rebuilds_projection_before_pipeline_and_adapters_start() -> None:
    source = _APP.read_text(encoding="utf-8")
    initialize = source.index("await self.storage.initialize()")
    rebuild = source.index("await self.pipeline_runner.rebuild_conversation_projection()")
    pipeline_start = source.index("await self.pipeline_runner.start()")
    adapter_start = source.index("await adapter.start(ctx)")

    assert initialize < rebuild < pipeline_start < adapter_start


def test_projection_code_does_not_update_canonical_event_evidence() -> None:
    """Derived repair may mutate its table, never canonical_events/event_relations."""
    conversation_source = (_STORAGE / "_conversation.py").read_text(encoding="utf-8")
    lowered = conversation_source.lower()
    assert "update canonical_events" not in lowered
    assert "update event_relations" not in lowered
    assert "delete from canonical_events" not in lowered
    assert "delete from event_relations" not in lowered


def test_pipeline_repairs_projection_after_event_and_native_identity_arrival() -> None:
    runner = (_ROOT / "src/medre/core/engine/pipeline/runner.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(runner)
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {
            "repair_after_event_available",
            "repair_after_native_ref_available",
        }:
            calls.append(node.func.attr)

    assert calls.count("repair_after_event_available") == 4
    assert calls.count("repair_after_native_ref_available") == 1
