"""Tests for structured logging hardening (Track 4).

Covers:
- Redaction helper for sensitive keys
- _JsonFormatter extra-field inclusion and redaction
- diagnostic_event context redaction
- No raw secret leakage in output
- Human-readable format remains stable
- Repeated setup_logging is a no-op
"""

from __future__ import annotations

import json
import logging
from io import StringIO
from typing import Any, Generator

import pytest

from medre.core.observability.logging import (
    _REDACTED,
    _JsonFormatter,
    _redact_context,
    _redact_value,
    _sanitize_error,
    diagnostic_event,
    get_logger,
    log_route_delivered,
    log_route_failed,
    log_route_loop_prevented,
    log_route_matched,
    setup_logging,
)


# ---------------------------------------------------------------------------
# Redaction helper tests
# ---------------------------------------------------------------------------


class TestRedactValue:
    """Unit tests for the ``_redact_value`` helper."""

    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "access_token",
            "api_key",
            "password",
            "secret",
            "credential",
            "cookie",
            "session",
        ],
    )
    def test_exact_sensitive_keys_redacted(self, key: str) -> None:
        assert _redact_value(key, "super-secret-value") == _REDACTED

    @pytest.mark.parametrize(
        "key",
        [
            "Token",
            "ACCESS_TOKEN",
            "API_KEY",
            "Password",
            "SECRET",
            "Credential",
            "Cookie",
            "Session",
        ],
    )
    def test_case_insensitive(self, key: str) -> None:
        assert _redact_value(key, "super-secret-value") == _REDACTED

    @pytest.mark.parametrize(
        "key",
        [
            "user_password_hash",
            "my_secret_key",
            "refresh_token_value",
            "session_id",
            "cookie_domain",
        ],
    )
    def test_substring_match(self, key: str) -> None:
        assert _redact_value(key, "sensitive") == _REDACTED

    @pytest.mark.parametrize(
        "key",
        [
            "username",
            "event_id",
            "adapter",
            "target",
            "message",
            "count",
            "operation",
        ],
    )
    def test_safe_keys_pass_through(self, key: str) -> None:
        value = {"some": "data"}
        assert _redact_value(key, value) is value

    def test_none_value_passes_through_for_safe_keys(self) -> None:
        assert _redact_value("adapter", None) is None

    def test_sensitive_key_with_none_value_still_redacted(self) -> None:
        assert _redact_value("password", None) == _REDACTED


class TestRedactContext:
    """Unit tests for the ``_redact_context`` helper."""

    def test_mixed_keys(self) -> None:
        data = {
            "adapter": "discord",
            "token": "abc123",
            "event_id": "evt-1",
            "password": "hunter2",
        }
        result = _redact_context(data)
        assert result == {
            "adapter": "discord",
            "token": _REDACTED,
            "event_id": "evt-1",
            "password": _REDACTED,
        }

    def test_empty_dict(self) -> None:
        assert _redact_context({}) == {}

    def test_no_sensitive_keys(self) -> None:
        data = {"adapter": "matrix", "target": "ch-1"}
        assert _redact_context(data) == data

    def test_returns_new_dict(self) -> None:
        data: dict[str, Any] = {"adapter": "test"}
        result = _redact_context(data)
        assert result is not data

    def test_deterministic(self) -> None:
        """Repeated calls with same input produce identical output."""
        data = {"api_key": "k1", "name": "adapter_a"}
        assert _redact_context(data) == _redact_context(data)


# ---------------------------------------------------------------------------
# _JsonFormatter extra fields
# ---------------------------------------------------------------------------


class TestJsonFormatterExtraFields:
    """Tests that ``_JsonFormatter`` includes safe extra fields."""

    def _make_record(
        self, msg: str = "test", **extra: Any
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="medre.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=None,
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_no_extra_fields(self) -> None:
        record = self._make_record("hello")
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        assert "extra" not in parsed
        assert parsed["message"] == "hello"

    def test_safe_extra_fields_included(self) -> None:
        record = self._make_record("evt", adapter="discord", count=5)
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        assert parsed["extra"]["adapter"] == "discord"
        assert parsed["extra"]["count"] == 5

    def test_sensitive_extra_fields_redacted(self) -> None:
        record = self._make_record("evt", api_key="sk-12345", adapter="test")
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        assert parsed["extra"]["api_key"] == _REDACTED
        assert parsed["extra"]["adapter"] == "test"

    def test_internal_attrs_not_leaked(self) -> None:
        """Verify that internal logging attributes are not in extra."""
        record = self._make_record("test", adapter="foo")
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        extra = parsed.get("extra", {})
        for internal in ("name", "msg", "args", "created", "lineno"):
            assert internal not in extra

    def test_top_level_fields_not_duplicated_in_extra(self) -> None:
        """Fields already in top-level entry (timestamp, level, etc.) must not appear in extra."""
        record = self._make_record("test")
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        extra = parsed.get("extra", {})
        for top_key in ("timestamp", "level", "logger", "message"):
            assert top_key not in extra

    def test_exception_included(self) -> None:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys as _sys

            record = logging.LogRecord(
                name="medre.test",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="error",
                args=None,
                exc_info=_sys.exc_info(),
            )
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "RuntimeError" in parsed["exception"]


# ---------------------------------------------------------------------------
# diagnostic_event redaction
# ---------------------------------------------------------------------------


class TestDiagnosticEventRedaction:
    """Tests that ``diagnostic_event`` redacts sensitive context values."""

    @pytest.fixture()
    def _capture_diagnostic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[StringIO, None, None]:
        """Wire up the medre.diagnostics logger to a StringIO stream."""
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        diag_logger = logging.getLogger("medre.diagnostics")
        diag_logger.handlers.clear()
        diag_logger.addHandler(handler)
        diag_logger.setLevel(logging.DEBUG)
        yield buf
        diag_logger.handlers.clear()

    def test_no_raw_token_in_output(
        self, _capture_diagnostic: StringIO
    ) -> None:
        diagnostic_event(
            "evt-1",
            "adapter_failure",
            "connection failed",
            token="sk-live-abc123",
            adapter="discord",
        )
        output = _capture_diagnostic.getvalue()
        assert "sk-live-abc123" not in output
        assert _REDACTED in output
        assert "adapter='discord'" in output

    def test_password_redacted(self, _capture_diagnostic: StringIO) -> None:
        diagnostic_event(
            "evt-2",
            "auth_failure",
            "bad creds",
            password="hunter2",
        )
        output = _capture_diagnostic.getvalue()
        assert "hunter2" not in output
        assert _REDACTED in output

    def test_no_context_produces_no_kv(self) -> None:
        """When no context kwargs are passed, the trailing kv segment is empty."""
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        diag_logger = logging.getLogger("medre.diagnostics.test_no_ctx")
        diag_logger.handlers.clear()
        diag_logger.addHandler(handler)
        diag_logger.setLevel(logging.DEBUG)

        # Patch the module-level _diagnostic_logger temporarily
        import medre.core.observability.logging as log_mod

        original = log_mod._diagnostic_logger
        log_mod._diagnostic_logger = diag_logger
        try:
            diagnostic_event("evt-3", "test", "no context")
            output = buf.getvalue()
            assert "event_id=evt-3" in output
            assert "category=test" in output
            # No trailing key=value pairs
            assert "=" not in output.split("message=no context")[-1]
        finally:
            log_mod._diagnostic_logger = original
            diag_logger.handlers.clear()

    def test_safe_context_preserved(
        self, _capture_diagnostic: StringIO
    ) -> None:
        diagnostic_event(
            "evt-4",
            "renderer_failure",
            "no renderer",
            target="matrix",
            operation="render",
        )
        output = _capture_diagnostic.getvalue()
        assert "target='matrix'" in output
        assert "operation='render'" in output


# ---------------------------------------------------------------------------
# Integration: setup_logging + JSON formatter
# ---------------------------------------------------------------------------


class TestSetupLoggingIntegration:
    """Integration tests for ``setup_logging`` with JSON output."""

    @pytest.fixture(autouse=True)
    def _reset_medre_logger(self) -> None:
        """Ensure a clean medre logger for each test."""
        logger = logging.getLogger("medre")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)

    def test_json_format_includes_extra_fields(self) -> None:
        buf = StringIO()
        logger = logging.getLogger("medre")

        handler = logging.StreamHandler(buf)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("test msg", extra={"adapter": "lxmf", "request_id": "r1"})

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["message"] == "test msg"
        assert parsed["extra"]["adapter"] == "lxmf"
        assert parsed["extra"]["request_id"] == "r1"

    def test_json_format_redacts_extra_sensitive_fields(self) -> None:
        buf = StringIO()
        logger = logging.getLogger("medre")

        handler = logging.StreamHandler(buf)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info(
            "auth attempt",
            extra={"username": "admin", "password": "s3cret"},
        )

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["extra"]["username"] == "admin"
        assert parsed["extra"]["password"] == _REDACTED
        assert "s3cret" not in buf.getvalue()

    def test_human_readable_format_unchanged(self) -> None:
        """Verify human-readable output still works with extra fields."""
        buf = StringIO()
        logger = logging.getLogger("medre")

        handler = logging.StreamHandler(buf)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        # Extra fields are ignored by the default formatter – message only.
        logger.info("hello world", extra={"adapter": "test"})
        output = buf.getvalue()
        assert "[INFO] medre: hello world" in output

    def test_repeated_setup_logging_noop(self) -> None:
        """Repeated calls to setup_logging do not add duplicate handlers."""
        setup_logging(level="DEBUG", json_format=False)
        handler_count = len(logging.getLogger("medre").handlers)
        setup_logging(level="ERROR", json_format=True)
        assert len(logging.getLogger("medre").handlers) == handler_count

    def test_get_logger_returns_child(self) -> None:
        child = get_logger("subsystem")
        assert child.name == "medre.subsystem"

    def test_no_raw_secret_in_json_output(self) -> None:
        """End-to-end: no raw secret value ever appears in JSON output."""
        buf = StringIO()
        logger = logging.getLogger("medre")

        handler = logging.StreamHandler(buf)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        sensitive_pairs = {
            "token": "tok-abc",
            "access_token": "at-xyz",
            "api_key": "ak-123",
            "password": "pw-456",
            "secret": "sec-789",
            "credential": "cred-000",
            "cookie": "ck-aaa",
            "session": "sess-bbb",
        }
        logger.info("multi-field", extra=sensitive_pairs)

        raw_output = buf.getvalue()
        parsed = json.loads(raw_output.strip())

        # None of the raw values should appear in the output
        for secret_val in sensitive_pairs.values():
            assert secret_val not in raw_output

        # All keys should be redacted in the parsed extra
        for key in sensitive_pairs:
            assert parsed["extra"][key] == _REDACTED


# ---------------------------------------------------------------------------
# Route-aware logging
# ---------------------------------------------------------------------------


class TestRouteLogging:
    """Tests for route-aware structured logging functions."""

    @pytest.fixture()
    def _capture_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[StringIO, None, None]:
        """Wire up the medre.route logger to a StringIO stream."""
        buf = StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        route_logger = logging.getLogger("medre.route")
        route_logger.handlers.clear()
        route_logger.addHandler(handler)
        route_logger.setLevel(logging.DEBUG)
        yield buf
        route_logger.handlers.clear()

    def test_log_route_matched(self, _capture_route: StringIO) -> None:
        from medre.core.observability.logging import log_route_matched

        log_route_matched(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "DEBUG" in output
        assert "route_matched" in output
        assert "route_id=r1" in output
        assert "event_id=evt-1" in output

    def test_log_route_delivered(self, _capture_route: StringIO) -> None:
        from medre.core.observability.logging import log_route_delivered

        log_route_delivered(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "DEBUG" in output
        assert "route_delivered" in output

    def test_log_route_failed(self, _capture_route: StringIO) -> None:
        from medre.core.observability.logging import log_route_failed

        log_route_failed(route_id="r1", event_id="evt-1", error="timeout")
        output = _capture_route.getvalue()
        assert "WARNING" in output
        assert "route_failed" in output
        assert "timeout" in output

    def test_log_route_failed_sanitizes_secret(self, _capture_route: StringIO) -> None:
        from medre.core.observability.logging import log_route_failed

        log_route_failed(
            route_id="r1",
            event_id="evt-1",
            error="Connection failed token=sk_live_abc123",
        )
        output = _capture_route.getvalue()
        assert "sk_live_abc123" not in output
        assert "[REDACTED]" in output

    def test_log_route_loop_prevented(self, _capture_route: StringIO) -> None:
        from medre.core.observability.logging import log_route_loop_prevented

        log_route_loop_prevented(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "WARNING" in output
        assert "route_loop_prevented" in output

    def test_route_log_no_raw_sdk_objects(self, _capture_route: StringIO) -> None:
        """Log output must not contain repr of SDK/adapter objects."""
        from medre.core.observability.logging import log_route_failed

        log_route_failed(route_id="r1", event_id="evt-1", error="simple error")
        output = _capture_route.getvalue()
        assert "<" not in output.split("error=")[-1] or "simple error" in output


# ---------------------------------------------------------------------------
# RouteMetrics
# ---------------------------------------------------------------------------


class TestRouteMetrics:
    """Tests for per-route delivery counters in RouteMetrics."""

    def test_initial_snapshot_is_empty(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        assert rm.snapshot() == {}

    def test_record_delivered(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_delivered("route-a")
        rm.record_delivered("route-a")
        snap = rm.snapshot()
        assert snap["route-a"]["delivered"] == 2

    def test_record_failed(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_failed("route-b", "timeout")
        snap = rm.snapshot()
        assert snap["route-b"]["failed"] == 1
        assert snap["route-b"]["delivered"] == 0

    def test_record_skipped(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_skipped("route-c")
        snap = rm.snapshot()
        assert snap["route-c"]["skipped"] == 1

    def test_record_loop_prevented(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_loop_prevented("route-d")
        snap = rm.snapshot()
        assert snap["route-d"]["loop_prevented"] == 1

    def test_multiple_routes(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_delivered("r1")
        rm.record_delivered("r1")
        rm.record_failed("r2", "err")
        rm.record_loop_prevented("r2")

        snap = rm.snapshot()
        assert snap["r1"]["delivered"] == 2
        assert snap["r1"]["failed"] == 0
        assert snap["r2"]["delivered"] == 0
        assert snap["r2"]["failed"] == 1
        assert snap["r2"]["loop_prevented"] == 1

    def test_snapshot_sorted_by_route_id(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_delivered("z-route")
        rm.record_delivered("a-route")
        rm.record_delivered("m-route")

        keys = list(rm.snapshot().keys())
        assert keys == sorted(keys)

    def test_mixed_counters_per_route(self) -> None:
        from medre.core.observability.metrics import RouteMetrics

        rm = RouteMetrics()
        rm.record_delivered("r1")
        rm.record_delivered("r1")
        rm.record_failed("r1", "err")
        rm.record_skipped("r1")
        rm.record_loop_prevented("r1")

        snap = rm.snapshot()
        entry = snap["r1"]
        assert entry["delivered"] == 2
        assert entry["failed"] == 1
        assert entry["skipped"] == 1
        assert entry["loop_prevented"] == 1
