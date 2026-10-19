"""Console status and diagnostic records share the configured log format."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from io import StringIO

import pytest

from medre.cli.run_commands import _print
from medre.core.observability.logging import _JsonFormatter, setup_logging

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} \[")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Restore logger ownership and levels so formatter tests do not leak policy."""
    root = logging.getLogger()
    handlers, root_level = root.handlers[:], root.level
    states = {
        name: (logger.level, logger.propagate, logger.handlers[:])
        for name, logger in root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root.handlers = []
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(root_level)
        for name, logger in root.manager.loggerDict.copy().items():
            if isinstance(logger, logging.Logger):
                level, propagate, saved_handlers = states.get(
                    name, (logging.NOTSET, True, [])
                )
                logger.setLevel(level)
                logger.propagate = propagate
                logger.handlers = saved_handlers


def test_text_status_and_multiline_dependency_message_are_timestamped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Attribute status and dependency continuation lines to their originating record."""
    setup_logging()
    _print("Runtime ready\n  Adapter running")
    logging.getLogger("dependency.example").warning("First line\r\nSecond line")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert all(STAMP.match(line) for line in lines)
    assert "Runtime ready" in lines[0]
    assert "Adapter running" in lines[1]
    assert "[WARNING] dependency.example: Second line" in lines[-1]


def test_traceback_continuations_have_the_record_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep traceback continuations attached to the error event and its timestamp."""
    setup_logging()
    try:
        raise ValueError("broken\nsecond error line")
    except ValueError:
        logging.getLogger("medre.test").exception("Read failed")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 3
    assert all(STAMP.match(line) for line in lines)
    prefixes = [line.split("medre.test: ", 1)[0] for line in lines]
    assert len(set(prefixes)) == 1
    assert "ValueError: broken" in "\n".join(lines)
    assert lines[-1].endswith("second error line")


def test_json_status_is_one_record_with_escaped_newline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep multiline status text inside a single parseable JSON record."""
    setup_logging(json_format=True)
    _print("Runtime ready\n  Adapter running")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "Runtime ready\n  Adapter running"
    assert record["level"] == "INFO"
    assert record["timestamp"]


def test_delayed_json_record_uses_creation_time() -> None:
    """Preserve event time when formatting occurs after record creation."""
    record = logging.LogRecord("medre.test", logging.INFO, "", 0, "event", (), None)
    record.created = 946684800.0
    assert (
        json.loads(_JsonFormatter().format(record))["timestamp"]
        == "2000-01-01T00:00:00+00:00"
    )


@pytest.mark.parametrize("close_first", [False, True])
def test_repeated_setup_follows_redirected_stdout(
    monkeypatch: pytest.MonkeyPatch,
    close_first: bool,
) -> None:
    """Send each invocation to its live stream, including after a closed capture."""
    first, second = StringIO(), StringIO()
    monkeypatch.setattr(sys, "stdout", first)
    setup_logging()
    _print("first invocation")
    assert "first invocation" in first.getvalue()
    if close_first:
        first.close()
    monkeypatch.setattr(sys, "stdout", second)
    setup_logging(json_format=True)
    _print("second invocation")
    records = [json.loads(line) for line in second.getvalue().splitlines()]
    assert [record["message"] for record in records] == ["second invocation"]


def test_status_respects_operator_log_level(capsys: pytest.CaptureFixture[str]) -> None:
    """Suppress informational status without hiding warning and error outcomes."""
    setup_logging(level="WARNING")
    _print("Runtime ready")
    _print("Startup failed", level=logging.ERROR)
    logging.getLogger("medre").warning("Adapter degraded")
    output = capsys.readouterr().out
    assert "Runtime ready" not in output
    assert "Adapter degraded" in output
    assert "[ERROR] medre: Startup failed" in output


def test_shutdown_signal_during_console_flush_defers_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A signal may interrupt stdout flush without recursively logging to it."""
    import signal

    from medre.cli import run_commands

    class InterruptedStream(StringIO):
        """Deliver one shutdown signal while a buffered flush is active."""

        triggered = False
        flushing = False

        def flush(self) -> None:
            """Reject recursive output while simulating an interrupted flush."""
            if self.flushing:
                raise RuntimeError("reentrant console flush")
            self.flushing = True
            try:
                if not self.triggered:
                    self.triggered = True
                    run_commands._request_shutdown(signal.SIGTERM, None)
                super().flush()
            finally:
                self.flushing = False

    stream = InterruptedStream()
    monkeypatch.setattr(run_commands, "shutdown_requested", False)
    monkeypatch.setattr(sys, "stdout", stream)
    setup_logging()
    _print("Runtime ready")
    assert run_commands.shutdown_requested
    assert "Logging error" not in capsys.readouterr().err
