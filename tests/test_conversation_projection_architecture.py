"""Architecture guards for conversation projection authority boundaries."""

from __future__ import annotations

import ast
import re
from collections import Counter
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


def test_runtime_marks_projection_clean_after_pipeline_stop_before_storage_close() -> None:
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    stop = next(
        member
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for member in cls.body
        if isinstance(member, ast.AsyncFunctionDef) and member.name == "stop"
    )

    def _call_line(owner: str, method: str) -> int:
        return next(
            node.lineno
            for node in ast.walk(stop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == owner
        )

    pipeline_stop = _call_line("pipeline_runner", "stop")
    projection_clean = _call_line(
        "pipeline_runner", "mark_conversation_projection_clean"
    )
    storage_close = _call_line("storage", "close")

    assert pipeline_stop < projection_clean < storage_close


def test_projection_code_does_not_update_canonical_event_evidence() -> None:
    """Derived repair may mutate its table, never canonical_events/event_relations."""
    conversation_source = (_STORAGE / "_conversation.py").read_text(encoding="utf-8")
    forbidden = re.compile(
        r"\b(update|delete\s+from|insert\s+into)\s+"
        r"(canonical_events|event_relations)\b",
        re.IGNORECASE,
    )
    assert forbidden.search(conversation_source) is None


def test_pipeline_repairs_projection_after_event_and_native_identity_arrival() -> None:
    runner = (_ROOT / "src/medre/core/engine/pipeline/runner.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(runner)
    calls: Counter[tuple[str, str]] = Counter()
    tracked = {
        "_repair_conversation_after_event_available",
        "repair_after_event_available",
        "repair_after_native_ref_available",
    }

    class _RepairCallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_name: str | None = None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            previous = self.function_name
            self.function_name = node.name
            self.generic_visit(node)
            self.function_name = previous

        def visit_Call(self, node: ast.Call) -> None:
            if (
                self.function_name is not None
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in tracked
            ):
                calls[(self.function_name, node.func.attr)] += 1
            self.generic_visit(node)

    _RepairCallVisitor().visit(tree)

    assert calls == Counter(
        {
            (
                "_repair_conversation_after_event_available",
                "repair_after_event_available",
            ): 1,
            ("handle_ingress", "_repair_conversation_after_event_available"): 2,
            ("admit_ingress", "_repair_conversation_after_event_available"): 1,
            (
                "process_admitted_event",
                "_repair_conversation_after_event_available",
            ): 1,
            (
                "_repair_conversation_after_native_ref",
                "repair_after_native_ref_available",
            ): 1,
        }
    )
