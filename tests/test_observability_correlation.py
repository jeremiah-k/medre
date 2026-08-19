"""Structured correlation context tests."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from medre.core.observability.correlation import (
    correlation_fields,
    correlation_scope,
    current_correlation,
)
from medre.core.observability.logging import _DEPENDENCY_DEFAULTS, setup_logging


def test_correlation_scope_nests_and_restores() -> None:
    assert correlation_fields() == {}
    with correlation_scope(trace_id="trace-1", event_id="evt-1"):
        assert correlation_fields() == {"trace_id": "trace-1", "event_id": "evt-1"}
        with correlation_scope(route_id="route-a", target_adapter="matrix"):
            assert correlation_fields() == {
                "trace_id": "trace-1",
                "event_id": "evt-1",
                "route_id": "route-a",
                "target_adapter": "matrix",
            }
        assert current_correlation().route_id is None
    assert correlation_fields() == {}


async def test_correlation_context_is_task_local() -> None:
    entered_a = asyncio.Event()
    entered_b = asyncio.Event()
    release = asyncio.Event()

    async def worker(event_id: str, entered: asyncio.Event) -> dict[str, str]:
        with correlation_scope(event_id=event_id):
            entered.set()
            await release.wait()
            return correlation_fields()

    first = asyncio.create_task(worker("evt-a", entered_a))
    second = asyncio.create_task(worker("evt-b", entered_b))
    await entered_a.wait()
    await entered_b.wait()
    release.set()
    a, b = await asyncio.gather(first, second)

    assert a == {"event_id": "evt-a"}
    assert b == {"event_id": "evt-b"}
    assert correlation_fields() == {}


def test_json_logging_inherits_correlation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    medre_logger = logging.getLogger("medre")
    previous_root_handlers = list(root.handlers)
    previous_root_level = root.level
    previous_medre_handlers = list(medre_logger.handlers)
    previous_medre_level = medre_logger.level
    previous_medre_propagate = medre_logger.propagate
    previous_dependency_levels = {
        name: logging.getLogger(name).level for name in _DEPENDENCY_DEFAULTS
    }
    previous_handler_state = [
        (handler, handler.level, handler.formatter, list(handler.filters))
        for handler in {*root.handlers, *medre_logger.handlers}
    ]
    try:
        setup_logging(level="INFO", json_format=True)
        with correlation_scope(trace_id="trace-json", event_id="evt-json"):
            logging.getLogger("medre.core_reliability").info("correlated")

        captured = capsys.readouterr().out.strip().splitlines()
        record = json.loads(captured[-1])
        assert record["message"] == "correlated"
        assert record["extra"]["trace_id"] == "trace-json"
        assert record["extra"]["event_id"] == "evt-json"
    finally:
        root.handlers[:] = previous_root_handlers
        root.setLevel(previous_root_level)
        medre_logger.handlers[:] = previous_medre_handlers
        medre_logger.setLevel(previous_medre_level)
        medre_logger.propagate = previous_medre_propagate
        for handler, level, formatter, filters in previous_handler_state:
            handler.setLevel(level)
            handler.setFormatter(formatter)
            handler.filters[:] = filters
        for name, level in previous_dependency_levels.items():
            logging.getLogger(name).setLevel(level)
