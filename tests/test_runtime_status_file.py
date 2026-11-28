"""Tests for the optional, content-free runtime status file."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from medre.runtime.status_file import SCHEMA, RuntimeStatusFile


async def test_lifecycle_and_atomic_private_output(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    status = RuntimeStatusFile(path)
    await status.initialize()
    first = json.loads(path.read_text())
    assert first["schema"] == SCHEMA
    assert first["phase"] == "starting"
    assert first["process_id"] == os.getpid()
    assert first["runtime_health"] is None
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    await status.publish("stopping")
    second = json.loads(path.read_text())
    assert second["run_id"] == first["run_id"]
    assert second["phase"] == "stopping"
    await status.publish("stopped")
    assert json.loads(path.read_text())["phase"] == "stopped"
    assert list(tmp_path.iterdir()) == [path]
    replacement = RuntimeStatusFile(path)
    await replacement.initialize()
    assert json.loads(path.read_text())["run_id"] != first["run_id"]


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX replaces a destination an open reader holds",
)
async def test_open_reader_reads_the_previous_document(tmp_path: Path) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    first = json.loads(status.path.read_text())
    handle = status.path.open("rb")
    try:
        await status.publish("stopping")
        assert json.load(handle) == first
    finally:
        handle.close()
    assert json.loads(status.path.read_text())["phase"] == "stopping"


async def test_refused_replacement_keeps_last_good_file_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    first = status.path.read_bytes()
    calls: list[Any] = []

    def refuse(*args: Any) -> None:
        calls.append(args)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("medre.runtime.status_file.os.replace", refuse)
    with caplog.at_level(logging.WARNING, logger="medre.runtime.status_file"):
        await status.publish("running")
    assert len(calls) == 1
    assert status.path.read_bytes() == first
    assert status._write_failed is True
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    monkeypatch.undo()
    await status.publish("stopped")
    assert status._write_failed is False
    assert json.loads(status.path.read_text())["phase"] == "stopped"
    assert list(tmp_path.iterdir()) == [status.path]


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows refuses replacing a destination an open reader holds",
)
async def test_open_reader_blocks_replacement_until_closed(tmp_path: Path) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    first = status.path.read_bytes()
    handle = status.path.open("rb")
    try:
        await status.publish("stopping")
        assert status.path.read_bytes() == first
        assert json.loads(status.path.read_text())["phase"] == "starting"
        assert status._write_failed is True
    finally:
        handle.close()
    await status.publish("stopped")
    assert json.loads(status.path.read_text())["phase"] == "stopped"
    assert status._write_failed is False
    assert list(tmp_path.iterdir()) == [status.path]


@pytest.mark.parametrize("content", ["not JSON", '{"private_key":"SECRET"}', "[]"])
async def test_never_replace_unrelated_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ValueError):
        await RuntimeStatusFile(path).initialize()
    assert path.read_text() == content


async def test_projection_uses_retained_observation_not_private_data(
    tmp_path: Path,
) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    status.app = cast(
        Any,
        SimpleNamespace(
            started_adapter_ids=["private-adapter"],
            live_health=SimpleNamespace(
                runtime_health="degraded",
                poll_timestamp_wall="2020-01-01T00:00:00+00:00",
                error="SECRET",
            ),
            boot_summary=SimpleNamespace(runtime_health="healthy"),
            route_eligibility=SimpleNamespace(registered=("private-route",)),
            startup_readiness=SimpleNamespace(
                skipped=(SimpleNamespace(route_id="private-route"),)
            ),
        ),
    )
    status.phase = "running"
    result = status.snapshot()
    assert result["health_scope"] == "retained"
    assert result["runtime_health"] == "degraded"
    assert result["health_observed_at"] == 1577836800.0
    assert result["adapters_started"] == 1
    assert result["startup_routes_registered"] == 1
    assert result["startup_routes_blocked"] == 1
    assert "private" not in json.dumps(result)
    assert "SECRET" not in json.dumps(result)
    await status.publish("stopping")
    assert status.snapshot()["runtime_health"] is None
    assert status.snapshot()["startup_routes_registered"] == 1
    assert status.snapshot()["startup_routes_blocked"] == 1


async def test_worker_start_failure_allows_next_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()

    def refuse(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", refuse)
        await status.publish("running")
    assert json.loads(status.path.read_text())["phase"] == "starting"

    await status.publish("stopped")
    assert json.loads(status.path.read_text())["phase"] == "stopped"


async def test_failed_start_preserves_available_route_count(tmp_path: Path) -> None:
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    status.app = cast(
        Any,
        SimpleNamespace(
            route_eligibility=SimpleNamespace(registered=("private-route",)),
            startup_readiness=None,
        ),
    )
    await status.initialize()
    await status.publish("failed")
    result = json.loads(status.path.read_text())
    assert result["startup_routes_registered"] == 1
    assert result["startup_routes_blocked"] is None


async def test_unexpected_projection_failure_is_best_effort(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    status = RuntimeStatusFile(path)
    await status.initialize()
    old = path.read_bytes()

    def fail():
        raise RuntimeError("private error")

    monkeypatch.setattr(status, "snapshot", fail)
    await status.publish("running")
    assert path.read_bytes() == old


async def test_uninitialized_status_never_writes_terminal_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.json"
    status = RuntimeStatusFile(path)
    await status.publish("failed")
    assert not path.exists()


async def test_periodic_publication_recovers_without_changing_run_id(
    tmp_path, monkeypatch
):
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    first = json.loads(status.path.read_text())
    ticks = asyncio.Queue()
    ready = asyncio.Queue()

    async def controlled_sleep(_seconds):
        ready.put_nowait(None)
        await ticks.get()

    monkeypatch.setattr("medre.runtime.status_file.asyncio.sleep", controlled_sleep)
    original = status._write
    attempts = []

    def write(observation):
        attempts.append(observation)
        if len(attempts) == 2:
            raise OSError("injected")
        original(observation)

    monkeypatch.setattr(status, "_write", write)
    task = asyncio.create_task(status.heartbeat())
    try:
        await asyncio.wait_for(ready.get(), 2)
        for expected in (1, 2, 3):
            ticks.put_nowait(None)
            await asyncio.wait_for(ready.get(), 2)
            actual = json.loads(status.path.read_text())
            assert actual["run_id"] == first["run_id"]
            assert actual == attempts[0 if expected == 2 else expected - 1]
        assert len(attempts) == 3
        assert not status._write_failed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_startup_health_remains_passive_and_schema_valid(tmp_path):
    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).parents[1] / "docs/schemas/runtime-status.schema.json"
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    validator.validate(json.loads(status.path.read_text()))
    status.app = cast(
        Any,
        SimpleNamespace(
            started_adapter_ids=["private"],
            live_health=None,
            boot_summary=SimpleNamespace(runtime_health="healthy"),
            route_eligibility=None,
            startup_readiness=None,
        ),
    )
    await status.publish("running")
    result = json.loads(status.path.read_text())
    assert result["health_scope"] == "startup"
    assert result["health_observed_at"] is None
    validator.validate(result)
    await status.publish("stopped")
    validator.validate(json.loads(status.path.read_text()))


async def test_stalled_writer_is_single_flight_and_cannot_hold_shutdown(
    tmp_path, monkeypatch
):
    status = RuntimeStatusFile(tmp_path / "runtime.json")
    await status.initialize()
    first = status.path.read_bytes()
    entered = threading.Event()
    release = threading.Event()
    original = status._write
    attempts = []

    def stall(observation):
        attempts.append(observation)
        entered.set()
        assert release.wait(5)
        original(observation)

    monkeypatch.setattr(status, "_write", stall)
    monkeypatch.setattr("medre.runtime.status_file.WRITE_TIMEOUT_SECONDS", 0.05)
    try:
        await asyncio.wait_for(status.publish("running"), 1)
        assert entered.is_set()
        await asyncio.wait_for(status.publish("stopped"), 1)
        assert len(attempts) == 1
        status.close()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.shield(status._pending_write), 2)
    assert status.path.read_bytes() == first
