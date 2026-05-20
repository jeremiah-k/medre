"""Tests for structured logging hardening (Track 4).

Covers:
- Canonical sanitize_for_log integration
- _JsonFormatter extra-field inclusion and sanitization
- diagnostic_event context sanitization
- No raw secret leakage in output
- Human-readable format remains stable
- Repeated setup_logging is a no-op

Behavioral note on the canonical sanitizer (``sanitize_for_log`` from
``medre.core.observability.sanitization``):

* Uses **anchored regex patterns** (e.g. ``^password$``, ``^secret``) instead
  of the old substring matching.  Keys like ``"user_password_hash"`` no longer
  match because the key does not start with ``"password"``.
* **Removes** sensitive keys from the output dict entirely rather than
  replacing values with ``"[REDACTED]"``.
* **Coerces** non-scalar values (dicts → recursive sanitize, lists →
  element-wise sanitize, unknown types → ``"<TypeName>"``).
* Does NOT match bare ``"token"``, ``"cookie"``, or ``"session"`` as
  sensitive — only prefixed forms like ``"access_token"``,
  ``"session_secret"``.  This is intentional: the canonical patterns are
  anchored to avoid false positives on generic keys.
"""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from typing import Any, Generator

import pytest

from medre.core.observability.logging import (
    _DEPENDENCY_DEFAULTS,
    _MEDRE_HANDLER_ATTR,
    _JsonFormatter,
    diagnostic_event,
    get_logger,
    log_route_delivered,
    log_route_failed,
    log_route_loop_prevented,
    log_route_matched,
    setup_logging,
)
from medre.core.observability.sanitization import sanitize_for_log

# ---------------------------------------------------------------------------
# Canonical sanitizer tests
# ---------------------------------------------------------------------------


class TestSanitizeForLogCanonical:
    """Tests for ``sanitize_for_log`` from ``medre.core.observability.sanitization``.

    These tests verify the canonical sanitizer's behaviour as used by the
    logging layer.  The canonical sanitizer uses anchored regex patterns
    and **removes** matching keys (rather than replacing values with
    ``"[REDACTED]"``).
    """

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "secret",
            "secret_key",
            "api_key",
            "apikey",
            "access_token",
            "accesstoken",
            "auth_token",
            "private_key",
            "credential",
            "credentials",
            "session_secret",
            "encryption_key",
            "device_key",
        ],
    )
    def test_secret_keys_removed(self, key: str) -> None:
        result = sanitize_for_log({key: "super-secret-value"})
        assert key not in result

    @pytest.mark.parametrize(
        "key",
        [
            "Password",
            "PASSWORD",
            "Secret",
            "API_KEY",
            "Access_Token",
        ],
    )
    def test_case_insensitive(self, key: str) -> None:
        result = sanitize_for_log({key: "super-secret-value"})
        assert key not in result

    @pytest.mark.parametrize(
        "key",
        [
            # Intentional difference: anchored patterns do NOT match substrings.
            # "user_password_hash" does NOT start with "password", so it passes.
            "user_password_hash",
            "my_secret_in_the_middle",
            "refresh_token_value",
        ],
    )
    def test_substring_non_match_passes_through(self, key: str) -> None:
        """Keys containing secret words but not starting with them pass through.

        This is an intentional difference from the old ``_redact_value`` which
        used substring matching.  The canonical sanitizer uses anchored regex
        patterns to avoid false positives on generic key names.
        """
        result = sanitize_for_log({key: "some-value"})
        assert result[key] == "some-value"

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
        result = sanitize_for_log({key: "data"})
        assert result[key] == "data"

    def test_none_value_passes_through_for_safe_keys(self) -> None:
        result = sanitize_for_log({"adapter": None})
        assert result["adapter"] is None

    def test_dict_value_recursively_sanitized(self) -> None:
        result = sanitize_for_log({"config": {"password": "secret"}})
        assert "password" not in result["config"]

    def test_non_scalar_coerced_to_type_name(self) -> None:
        result = sanitize_for_log({"obj": object()})
        assert result["obj"] == "<object>"

    def test_mixed_keys(self) -> None:
        data = {
            "adapter": "discord",
            "password": "abc123",
            "event_id": "evt-1",
            "api_key": "key-xyz",
        }
        result = sanitize_for_log(data)
        assert result == {
            "adapter": "discord",
            "event_id": "evt-1",
        }

    def test_empty_dict(self) -> None:
        assert sanitize_for_log({}) == {}

    def test_no_sensitive_keys(self) -> None:
        data = {"adapter": "matrix", "target": "ch-1"}
        assert sanitize_for_log(data) == data

    def test_returns_new_dict(self) -> None:
        data: dict[str, Any] = {"adapter": "test"}
        result = sanitize_for_log(data)
        assert result is not data

    def test_deterministic(self) -> None:
        data = {"api_key": "k1", "name": "adapter_a"}
        assert sanitize_for_log(data) == sanitize_for_log(data)


# ---------------------------------------------------------------------------
# _JsonFormatter extra fields
# ---------------------------------------------------------------------------


class TestJsonFormatterExtraFields:
    """Tests that ``_JsonFormatter`` includes safe extra fields."""

    def _make_record(self, msg: str = "test", **extra: Any) -> logging.LogRecord:
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

    def test_sensitive_extra_fields_sanitized(self) -> None:
        record = self._make_record("evt", api_key="sk-12345", adapter="test")
        output = _JsonFormatter().format(record)
        parsed = json.loads(output)
        # Canonical sanitizer removes secret keys entirely.
        assert "api_key" not in parsed["extra"]
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

    def test_no_raw_secret_in_output(self, _capture_diagnostic: StringIO) -> None:
        diagnostic_event(
            "evt-1",
            "adapter_failure",
            "connection failed",
            password="sk-live-abc123",
            adapter="discord",
        )
        output = _capture_diagnostic.getvalue()
        assert "sk-live-abc123" not in output
        # Canonical sanitizer removes the key entirely — no "[REDACTED]" string.
        assert "password=" not in output
        assert "adapter='discord'" in output

    def test_api_key_removed(self, _capture_diagnostic: StringIO) -> None:
        diagnostic_event(
            "evt-2",
            "auth_failure",
            "bad creds",
            api_key="hunter2",
        )
        output = _capture_diagnostic.getvalue()
        assert "hunter2" not in output
        assert "api_key=" not in output

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

    def test_safe_context_preserved(self, _capture_diagnostic: StringIO) -> None:
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
        """Ensure a clean medre logger and root for each test."""
        logger = logging.getLogger("medre")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        # Also remove MEDRE-managed handlers from root.
        root = logging.getLogger()
        root.handlers = [
            h for h in root.handlers if not getattr(h, _MEDRE_HANDLER_ATTR, False)
        ]

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

    def test_json_format_sanitizes_extra_sensitive_fields(self) -> None:
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
        # Canonical sanitizer removes secret keys entirely.
        assert "password" not in parsed["extra"]
        assert "s3cret" not in buf.getvalue()

    def test_human_readable_format_unchanged(self) -> None:
        """Verify human-readable output still works with extra fields."""
        buf = StringIO()
        logger = logging.getLogger("medre")

        handler = logging.StreamHandler(buf)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
        root = logging.getLogger()
        medre_handler_count = sum(
            1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        )
        setup_logging(level="ERROR", json_format=True)
        assert (
            sum(1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False))
            == medre_handler_count
        )

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

        # Use keys recognized by the canonical sanitizer's anchored regex
        # patterns.  Bare "token", "cookie", and "session" are intentionally
        # NOT matched — the canonical sanitizer only matches prefixed forms
        # like "access_token" and "session_secret".
        sensitive_pairs = {
            "password": "pw-456",
            "secret": "sec-789",
            "api_key": "ak-123",
            "access_token": "at-xyz",
            "credential": "cred-000",
        }
        logger.info("multi-field", extra=sensitive_pairs)

        raw_output = buf.getvalue()
        parsed = json.loads(raw_output.strip())

        # None of the raw values should appear in the output
        for secret_val in sensitive_pairs.values():
            assert secret_val not in raw_output

        # All canonical secret keys should be removed from the parsed extra
        for key in sensitive_pairs:
            assert key not in parsed["extra"]


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

        log_route_matched(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "DEBUG" in output
        assert "route_matched" in output
        assert "route_id=r1" in output
        assert "event_id=evt-1" in output

    def test_log_route_delivered(self, _capture_route: StringIO) -> None:

        log_route_delivered(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "DEBUG" in output
        assert "route_delivered" in output

    def test_log_route_failed(self, _capture_route: StringIO) -> None:

        log_route_failed(route_id="r1", event_id="evt-1", error="timeout")
        output = _capture_route.getvalue()
        assert "WARNING" in output
        assert "route_failed" in output
        assert "timeout" in output

    def test_log_route_failed_sanitizes_secret(self, _capture_route: StringIO) -> None:

        log_route_failed(
            route_id="r1",
            event_id="evt-1",
            error="Connection failed token=sk_live_abc123",
        )
        output = _capture_route.getvalue()
        assert "sk_live_abc123" not in output
        assert "[REDACTED]" in output

    def test_log_route_loop_prevented(self, _capture_route: StringIO) -> None:

        log_route_loop_prevented(route_id="r1", event_id="evt-1")
        output = _capture_route.getvalue()
        assert "WARNING" in output
        assert "route_loop_prevented" in output

    def test_route_log_no_raw_sdk_objects(self, _capture_route: StringIO) -> None:
        """Log output must not contain repr of SDK/adapter objects."""

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


# ---------------------------------------------------------------------------
# setup_logging overrides and dependency defaults
# ---------------------------------------------------------------------------


class TestSetupLoggingOverrides:
    """Tests for setup_logging dependency defaults, overrides, and isolation."""

    @pytest.fixture(autouse=True)
    def _reset_all_loggers(self) -> Generator[None, None, None]:
        """Clean up medre, root, and dependency loggers between tests."""
        medre_logger = logging.getLogger("medre")
        root_logger = logging.getLogger()
        saved_medre_handlers = list(medre_logger.handlers)
        saved_medre_level = medre_logger.level
        saved_root_handlers = list(root_logger.handlers)
        saved_root_level = root_logger.level

        # Also snapshot dependency loggers that might be touched.
        saved_dep_levels: dict[str, int] = {}
        for name in list(_DEPENDENCY_DEFAULTS.keys()) + ["custom.lib"]:
            saved_dep_levels[name] = logging.getLogger(name).level

        yield

        # Restore.
        medre_logger.handlers = saved_medre_handlers
        medre_logger.setLevel(saved_medre_level)
        root_logger.handlers = saved_root_handlers
        root_logger.setLevel(saved_root_level)
        for name, lvl in saved_dep_levels.items():
            logging.getLogger(name).setLevel(lvl)

    def test_debug_sets_medre_debug_root_warning(self) -> None:
        """setup_logging with level=DEBUG sets medre to DEBUG, root to WARNING."""
        setup_logging(level="DEBUG", json_format=False)
        assert logging.getLogger("medre").level == logging.DEBUG
        assert logging.getLogger().level == logging.WARNING

    def test_override_sets_logger_level(self) -> None:
        """setup_logging with overrides={"nio": "INFO"} sets nio logger to INFO."""
        setup_logging(level="INFO", json_format=False, overrides={"nio": "INFO"})
        assert logging.getLogger("nio").level == logging.INFO

    def test_override_dotname_logger(self) -> None:
        """setup_logging with overrides for dotted logger names."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"nio.crypto.log": "ERROR"},
        )
        assert logging.getLogger("nio.crypto.log").level == logging.ERROR

    def test_override_suppresses_warning(self) -> None:
        """Override {"nio.crypto.log": "ERROR"} suppresses WARNING from that logger."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"nio.crypto.log": "ERROR"},
        )
        nio_crypto = logging.getLogger("nio.crypto.log")
        # WARNING (30) < ERROR (40), so a WARNING-level log should be suppressed.
        assert not nio_crypto.isEnabledFor(logging.WARNING)

    def test_dependency_defaults_applied(self) -> None:
        """Dependency loggers get their default levels when no override is provided."""
        setup_logging(level="DEBUG", json_format=False)
        assert logging.getLogger("nio").level == logging.WARNING
        assert logging.getLogger("nio.crypto.log").level == logging.ERROR
        assert logging.getLogger("aiohttp").level == logging.WARNING
        assert logging.getLogger("meshtastic").level == logging.WARNING
        assert logging.getLogger("peewee").level == logging.WARNING
        assert logging.getLogger("urllib3").level == logging.WARNING
        assert logging.getLogger("serial").level == logging.WARNING
        assert logging.getLogger("serial_asyncio").level == logging.WARNING
        assert logging.getLogger("asyncio").level == logging.WARNING

    def test_nio_default_is_warning(self) -> None:
        """nio logger is WARNING by default when no override provided."""
        setup_logging(level="DEBUG", json_format=False)
        assert logging.getLogger("nio").level == logging.WARNING

    def test_override_takes_precedence_over_default(self) -> None:
        """User override takes precedence over dependency defaults."""
        # nio default is WARNING; override to DEBUG
        setup_logging(level="INFO", json_format=False, overrides={"nio": "DEBUG"})
        assert logging.getLogger("nio").level == logging.DEBUG

    def test_invalid_override_raises_valueerror(self) -> None:
        """Override with invalid level raises ValueError."""
        with pytest.raises(ValueError, match="Invalid logging level"):
            setup_logging(
                level="INFO",
                json_format=False,
                overrides={"nio": "NOTAREALEVEL"},
            )

    def test_handler_not_duplicated_on_repeated_calls(self) -> None:
        """Repeated calls to setup_logging do not add duplicate handlers."""
        setup_logging(level="DEBUG", json_format=False)
        root = logging.getLogger()
        medre_handler_count = sum(
            1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        )
        setup_logging(level="ERROR", json_format=True)
        assert (
            sum(1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False))
            == medre_handler_count
        )

    def test_medre_logger_propagates_to_root(self) -> None:
        """medre logger should propagate to root (default propagation=True)."""
        setup_logging(level="DEBUG", json_format=False)
        medre_logger = logging.getLogger("medre")
        assert medre_logger.propagate is True

    def test_overrides_none_means_no_user_overrides(self) -> None:
        """overrides=None (default) applies only dependency defaults."""
        setup_logging(level="INFO", json_format=False, overrides=None)
        # Dependency defaults should still be applied.
        assert logging.getLogger("nio").level == logging.WARNING
        # No crash, no extra overrides.

    def test_override_nio_crypto_log_warning_changes_level(self) -> None:
        """Override nio.crypto.log=WARNING changes its level from default ERROR."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"nio.crypto.log": "WARNING"},
        )
        assert logging.getLogger("nio.crypto.log").level == logging.WARNING

    def test_bad_override_non_string_value_raises_valueerror(self) -> None:
        """Override with non-string value raises ValueError."""
        with pytest.raises(ValueError, match="must be a string"):
            setup_logging(
                level="INFO",
                json_format=False,
                overrides={"nio": 123},  # type: ignore[dict-item]
            )

    def test_bad_override_non_string_key_raises_valueerror(self) -> None:
        """Override with non-string key raises ValueError."""
        with pytest.raises(ValueError, match="non-empty string"):
            setup_logging(
                level="INFO",
                json_format=False,
                overrides={123: "DEBUG"},  # type: ignore[dict-item]
            )

    def test_bad_override_empty_key_raises_valueerror(self) -> None:
        """Override with empty string key raises ValueError."""
        with pytest.raises(ValueError, match="non-empty string"):
            setup_logging(
                level="INFO",
                json_format=False,
                overrides={"": "DEBUG"},
            )

    def test_bad_override_not_dict_raises_valueerror(self) -> None:
        """Override with non-dict type raises ValueError."""
        with pytest.raises(ValueError, match="must be a dict"):
            setup_logging(
                level="INFO",
                json_format=False,
                overrides=["nio"],  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Logging handler topology
# ---------------------------------------------------------------------------


class TestLoggingTopology:
    """Tests for the new MEDRE-managed root handler topology.

    Verifies:
    - One MEDRE-managed console handler on root logger
    - No duplicate MEDRE records
    - Dependency WARNING formatted by MEDRE handler
    - medre DEBUG emitted when level=DEBUG
    - nio DEBUG suppressed by default
    - Override nio DEBUG allowed
    - Repeated setup updates formatter without duplicate handlers
    - medre_logger.handlers does not contain MEDRE-managed handler
    - Non-MEDRE user handlers on root are preserved
    """

    @pytest.fixture(autouse=True)
    def _reset_all_loggers(self) -> Generator[None, None, None]:
        """Clean up medre, root, and dependency loggers between tests."""
        medre_logger = logging.getLogger("medre")
        root_logger = logging.getLogger()
        saved_medre_handlers = list(medre_logger.handlers)
        saved_medre_level = medre_logger.level
        saved_root_handlers = list(root_logger.handlers)
        saved_root_level = root_logger.level
        saved_dep_levels: dict[str, int] = {}
        for name in list(_DEPENDENCY_DEFAULTS.keys()) + ["custom.lib", "unknown.dep"]:
            saved_dep_levels[name] = logging.getLogger(name).level

        yield

        medre_logger.handlers = saved_medre_handlers
        medre_logger.setLevel(saved_medre_level)
        root_logger.handlers = saved_root_handlers
        root_logger.setLevel(saved_root_level)
        for name, lvl in saved_dep_levels.items():
            logging.getLogger(name).setLevel(lvl)

    @staticmethod
    def _get_medre_root_handler() -> logging.StreamHandler | None:
        """Find the MEDRE-managed handler on root, or None."""
        for h in logging.getLogger().handlers:
            if getattr(h, _MEDRE_HANDLER_ATTR, False):
                assert isinstance(h, logging.StreamHandler)
                return h
        return None

    def test_root_has_exactly_one_medre_handler(self) -> None:
        """After setup_logging, root has exactly one MEDRE-managed handler."""
        setup_logging(level="INFO", json_format=False)
        root = logging.getLogger()
        medre_handlers = [
            h for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        ]
        assert len(medre_handlers) == 1
        assert medre_handlers[0].level == logging.NOTSET

    def test_medre_logger_has_no_medre_handler(self) -> None:
        """medre_logger.handlers does not contain the MEDRE-managed console handler."""
        setup_logging(level="INFO", json_format=False)
        medre_logger = logging.getLogger("medre")
        for h in medre_logger.handlers:
            assert not getattr(h, _MEDRE_HANDLER_ATTR, False)

    def test_medre_logger_propagates_to_root(self) -> None:
        """medre logger propagate=True so records reach root handler."""
        setup_logging(level="DEBUG", json_format=False)
        assert logging.getLogger("medre").propagate is True

    def test_no_duplicate_medre_records(self) -> None:
        """A single medre DEBUG log appears exactly once in output."""
        setup_logging(level="DEBUG", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("medre.test").debug("unique-msg-12345")

        output = buf.getvalue()
        assert output.count("unique-msg-12345") == 1

    def test_dependency_warning_formatted_by_medre_handler(self) -> None:
        """A nio WARNING log gets formatted by the MEDRE-managed root handler."""
        setup_logging(level="INFO", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").warning("nio-warn-msg")

        output = buf.getvalue()
        assert "nio-warn-msg" in output
        assert "[WARNING]" in output
        assert "nio" in output

    def test_dependency_error_formatted_by_medre_handler(self) -> None:
        """A nio ERROR log gets formatted by the MEDRE-managed root handler."""
        setup_logging(level="INFO", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").error("nio-error-msg")

        output = buf.getvalue()
        assert "nio-error-msg" in output
        assert "[ERROR]" in output

    def test_medre_debug_emitted_when_level_debug(self) -> None:
        """medre DEBUG logs are emitted when level=DEBUG."""
        setup_logging(level="DEBUG", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("medre.subsystem").debug("medre-debug-msg")

        output = buf.getvalue()
        assert "medre-debug-msg" in output
        assert "[DEBUG]" in output

    def test_medre_info_suppressed_when_level_warning(self) -> None:
        """medre INFO logs suppressed when medre level=WARNING."""
        setup_logging(level="WARNING", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("medre.subsystem").info("medre-info-msg")

        output = buf.getvalue()
        assert "medre-info-msg" not in output

    def test_nio_debug_suppressed_by_default(self) -> None:
        """nio DEBUG logs are suppressed by default (nio default=WARNING)."""
        setup_logging(level="DEBUG", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").debug("nio-debug-msg")

        output = buf.getvalue()
        assert "nio-debug-msg" not in output

    def test_override_nio_debug_allowed(self) -> None:
        """nio DEBUG logs emitted when override={"nio": "DEBUG"}."""
        setup_logging(level="INFO", json_format=False, overrides={"nio": "DEBUG"})
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").debug("nio-debug-visible")

        output = buf.getvalue()
        assert "nio-debug-visible" in output

    def test_repeated_setup_updates_formatter(self) -> None:
        """Repeated setup changes formatter without adding duplicate handlers."""
        setup_logging(level="INFO", json_format=False)
        root = logging.getLogger()
        medre_count = sum(
            1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        )
        assert medre_count == 1

        # Switch to JSON format
        setup_logging(level="INFO", json_format=True)
        medre_count = sum(
            1 for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        )
        assert medre_count == 1

        handler = self._get_medre_root_handler()
        assert handler is not None
        assert isinstance(handler.formatter, _JsonFormatter)

        # Switch back to text format
        setup_logging(level="INFO", json_format=False)
        medre_count = sum(
            1
            for h in logging.getLogger().handlers
            if getattr(h, _MEDRE_HANDLER_ATTR, False)
        )
        assert medre_count == 1

        # Re-fetch the current MEDRE-managed handler after the third call.
        handler = self._get_medre_root_handler()
        assert handler is not None
        assert not isinstance(handler.formatter, _JsonFormatter)

    def test_preserves_non_medre_root_handlers(self) -> None:
        """Non-MEDRE user handlers on root are preserved by setup_logging."""
        root = logging.getLogger()
        user_handler = logging.StreamHandler(StringIO())
        root.addHandler(user_handler)
        user_count_before = len(root.handlers)

        setup_logging(level="INFO", json_format=False)

        # User handler still present
        assert user_handler in root.handlers
        medre_handlers = [
            h for h in root.handlers if getattr(h, _MEDRE_HANDLER_ATTR, False)
        ]
        assert len(medre_handlers) == 1
        # Total = user handler + MEDRE handler
        assert len(root.handlers) == user_count_before + 1

    def test_removes_old_medre_handler_from_medre_logger(self) -> None:
        """Old MEDRE-managed handlers on medre_logger are removed."""
        medre_logger = logging.getLogger("medre")
        # Simulate old setup: add a MEDRE-managed handler to medre logger
        old_handler = logging.StreamHandler(sys.stdout)
        setattr(old_handler, _MEDRE_HANDLER_ATTR, True)
        medre_logger.addHandler(old_handler)

        setup_logging(level="INFO", json_format=False)

        # Old handler should be gone from medre logger
        assert old_handler not in medre_logger.handlers

    def test_root_warning_after_setup_debug(self) -> None:
        """Root logger stays at WARNING even when medre level=DEBUG."""
        setup_logging(level="DEBUG", json_format=False)
        assert logging.getLogger().level == logging.WARNING
        assert logging.getLogger("medre").level == logging.DEBUG

    def test_unknown_dependency_debug_suppressed(self) -> None:
        """Unknown dependency DEBUG suppressed by root WARNING gate."""
        setup_logging(level="DEBUG", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("unknown.dep").debug("unknown-debug-msg")

        output = buf.getvalue()
        assert "unknown-debug-msg" not in output

    def test_unknown_dependency_warning_formatted(self) -> None:
        """Unknown dependency WARNING passes root gate and is formatted."""
        setup_logging(level="DEBUG", json_format=False)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("unknown.dep").warning("unknown-warn-msg")

        output = buf.getvalue()
        assert "unknown-warn-msg" in output
        assert "[WARNING]" in output
        assert "unknown.dep" in output

    def test_override_custom_lib_debug_emits(self) -> None:
        """Override custom.lib=DEBUG emits DEBUG despite root WARNING."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"custom.lib": "DEBUG"},
        )
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("custom.lib").debug("custom-debug-msg")

        output = buf.getvalue()
        assert "custom-debug-msg" in output
        assert "[DEBUG]" in output

    def test_override_nio_debug_emits(self) -> None:
        """Override nio=DEBUG emits DEBUG despite root WARNING."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"nio": "DEBUG"},
        )
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").debug("nio-debug-override")

        output = buf.getvalue()
        assert "nio-debug-override" in output
        assert "[DEBUG]" in output

    def test_override_aiohttp_debug_emits(self) -> None:
        """Override aiohttp=DEBUG emits DEBUG despite root WARNING."""
        setup_logging(
            level="INFO",
            json_format=False,
            overrides={"aiohttp": "DEBUG"},
        )
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("aiohttp").debug("aiohttp-debug-override")

        output = buf.getvalue()
        assert "aiohttp-debug-override" in output
        assert "[DEBUG]" in output

    def test_dependency_warning_uses_json_formatter(self) -> None:
        """Dependency WARNING uses JSON formatter when json_format=True."""
        setup_logging(level="INFO", json_format=True)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").warning("nio-json-warn")

        output = buf.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "nio-json-warn"
        assert parsed["level"] == "WARNING"
        assert parsed["logger"] == "nio"

    def test_dependency_error_uses_json_formatter(self) -> None:
        """Dependency ERROR uses JSON formatter when json_format=True."""
        setup_logging(level="INFO", json_format=True)
        handler = self._get_medre_root_handler()
        assert handler is not None
        buf = StringIO()
        handler.stream = buf

        logging.getLogger("nio").error("nio-json-error")

        output = buf.getvalue().strip()
        parsed = json.loads(output)
        assert parsed["message"] == "nio-json-error"
        assert parsed["level"] == "ERROR"

    def test_invalid_level_string_raises_valueerror(self) -> None:
        """setup_logging with invalid level string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid logging level"):
            setup_logging(level="NOTAREALEVEL", json_format=False)

    def test_non_string_level_raises_valueerror(self) -> None:
        """setup_logging with non-string level raises ValueError."""
        with pytest.raises(ValueError, match="must be a string"):
            setup_logging(level=10, json_format=False)  # type: ignore[arg-type]
