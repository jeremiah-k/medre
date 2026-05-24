"""Unit tests for tests/helpers/live_harness.py.

Covers redaction, environment gating, secret-leak detection,
bounded async execution, and smoke-test result serialisation.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.helpers.live_harness import (
    LiveEnvStatus,
    LiveRequirement,
    LiveSmokeResult,
    assert_no_secret_leak,
    bounded,
    live_env_status,
    live_result_to_json,
    redact_env_value,
)

# ===================================================================
# (a) redact_env_value
# ===================================================================


class TestRedactEnvValue:
    """Parametrised tests for secret-name heuristic redaction."""

    @pytest.mark.parametrize(
        ("name", "value", "expected"),
        [
            # None value always redacted regardless of name
            ("ANY_VAR", None, "<redacted>"),
            ("token", None, "<redacted>"),
            # Names containing secret heuristics (case-insensitive)
            ("MY_TOKEN", "abc123", "<redacted>"),
            ("my_token", "abc123", "<redacted>"),
            ("API_SECRET", "s", "<redacted>"),
            ("DB_PASSWORD", "hunter2", "<redacted>"),
            ("PRIVATE_KEY", "k3y", "<redacted>"),
            ("X_AUTH_HEADER", "bearer xyz", "<redacted>"),
            ("AWS_CREDENTIAL_FILE", "/tmp/creds", "<redacted>"),
            # Innocuous names pass through
            ("HOME", "/home/user", "/home/user"),
            ("PATH", "/usr/bin", "/usr/bin"),
            ("MATRIX_HOMESERVER", "https://matrix.org", "https://matrix.org"),
            ("DATABASE_URL", "postgres://localhost", "postgres://localhost"),
            # Empty string passes through for innocuous names
            ("SOME_VAR", "", ""),
        ],
    )
    def test_redact(self, name: str, value: str | None, expected: str) -> None:
        assert redact_env_value(name, value) == expected


# ===================================================================
# (b) live_env_status
# ===================================================================


class TestLiveEnvStatus:
    """Tests for environment gating via live_env_status()."""

    def test_all_present_including_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All vars present; secret flag causes explicit '<redacted>'."""
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://synapse.local")
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "syt_secret123")

        reqs = [
            LiveRequirement("MATRIX_HOMESERVER", secret=False),
            LiveRequirement("MATRIX_ACCESS_TOKEN", secret=True),
        ]
        status = live_env_status(reqs)

        assert status.enabled is True
        assert status.missing == ()
        # secret=True always gets "<redacted>"
        assert status.redacted_values["MATRIX_ACCESS_TOKEN"] == "<redacted>"
        # non-secret, innocuous name → literal value
        assert status.redacted_values["MATRIX_HOMESERVER"] == "https://synapse.local"

    def test_missing_var_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing env var → not enabled, name appears in missing tuple."""
        monkeypatch.delenv("DOES_NOT_EXIST_XYZ", raising=False)

        reqs = [LiveRequirement("DOES_NOT_EXIST_XYZ")]
        status = live_env_status(reqs)

        assert status.enabled is False
        assert "DOES_NOT_EXIST_XYZ" in status.missing

    def test_empty_string_counts_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty-string env var is treated as missing."""
        monkeypatch.setenv("EMPTY_VAR", "")

        reqs = [LiveRequirement("EMPTY_VAR")]
        status = live_env_status(reqs)

        assert status.enabled is False
        assert "EMPTY_VAR" in status.missing

    def test_non_secret_heuristic_redaction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-secret flagged var still gets heuristic redaction for sensitive names."""
        monkeypatch.setenv("API_TOKEN", "tok_abc")
        monkeypatch.setenv("DB_SECRET", "s3cret")

        reqs = [
            LiveRequirement("API_TOKEN", secret=False),
            LiveRequirement("DB_SECRET", secret=False),
        ]
        status = live_env_status(reqs)

        assert status.enabled is True
        # Even though secret=False, redact_env_value heuristically redacts
        # names containing TOKEN / SECRET
        assert status.redacted_values["API_TOKEN"] == "<redacted>"
        assert status.redacted_values["DB_SECRET"] == "<redacted>"

    def test_no_requirements(self) -> None:
        """Empty requirements list → enabled with nothing missing."""
        status = live_env_status([])
        assert status.enabled is True
        assert status.missing == ()
        assert status.redacted_values == {}


# ===================================================================
# (c) assert_no_secret_leak
# ===================================================================


class TestAssertNoSecretLeak:
    """Tests for secret-value leak detection in serialised objects."""

    def test_dict_with_secret_raises(self) -> None:
        """Dict containing a secret value triggers AssertionError."""
        with pytest.raises(AssertionError, match="Secret value leaked"):
            assert_no_secret_leak(
                {"token": "syt_secret123"}, ["syt_secret123"]
            )

    def test_empty_secret_passes(self) -> None:
        """Empty secret strings are skipped and never cause failure."""
        assert_no_secret_leak({"data": "anything"}, [""])

    def test_nested_structure_no_secrets(self) -> None:
        """Dict/list nesting with no secrets passes cleanly."""
        obj = {"items": [1, 2, {"inner": "value"}], "meta": None}
        assert_no_secret_leak(obj, ["not_present"])

    def test_live_smoke_result_with_plaintext_in_notes(self) -> None:
        """LiveSmokeResult with a plaintext token in notes raises."""
        result = LiveSmokeResult(
            transport="matrix",
            adapter_id="test-adapter",
            status="pass",
            notes=("token=syt_secret123",),
        )
        with pytest.raises(AssertionError, match="Secret value leaked"):
            assert_no_secret_leak(result, ["syt_secret123"])

    def test_live_smoke_result_with_redacted_passes(self) -> None:
        """LiveSmokeResult containing only '<redacted>' placeholders passes."""
        result = LiveSmokeResult(
            transport="matrix",
            adapter_id="test-adapter",
            status="pass",
            notes=("token=<redacted>",),
        )
        assert_no_secret_leak(result, ["syt_secret123"])


# ===================================================================
# (d) bounded
# ===================================================================


class TestBounded:
    """Async tests for bounded coroutine execution."""

    async def test_fast_coroutine_returns_value(self) -> None:
        """A coroutine that completes within timeout returns its value."""

        async def quick() -> str:
            return "done"

        result = await bounded(quick(), timeout=5.0, label="quick-test")
        assert result == "done"

    async def test_slow_coroutine_raises_runtime_error(self) -> None:
        """A coroutine that exceeds timeout raises RuntimeError with context."""

        async def slow() -> str:
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(RuntimeError) as exc_info:
            await bounded(slow(), timeout=0.05, label="slow-op")

        msg = str(exc_info.value)
        assert "slow-op" in msg
        assert "0.05" in msg


# ===================================================================
# (e) live_result_to_json
# ===================================================================


class TestLiveResultToJson:
    """Tests for LiveSmokeResult JSON serialisation."""

    def test_produces_valid_json(self) -> None:
        """Output is parseable JSON."""
        result = LiveSmokeResult(
            transport="matrix",
            adapter_id="adapter-1",
            status="pass",
        )
        parsed = json.loads(live_result_to_json(result))
        assert isinstance(parsed, dict)

    def test_tuple_fields_serialized_as_lists(self) -> None:
        """Tuple fields appear as JSON arrays."""
        result = LiveSmokeResult(
            transport="meshtastic",
            adapter_id="adapter-2",
            status="fail",
            notes=("note-a", "note-b"),
        )
        parsed = json.loads(live_result_to_json(result))
        assert parsed["notes"] == ["note-a", "note-b"]

    def test_all_fields_present_with_correct_values(self) -> None:
        """Every field appears in the JSON output with the expected value."""
        result = LiveSmokeResult(
            transport="meshcore",
            adapter_id="adapter-3",
            status="skip",
            native_message_id="msg-99",
            native_channel_id="ch-42",
            storage_path="/tmp/store",
            evidence_path="/tmp/evidence",
            notes=("checked",),
        )
        parsed = json.loads(live_result_to_json(result))

        assert parsed["transport"] == "meshcore"
        assert parsed["adapter_id"] == "adapter-3"
        assert parsed["status"] == "skip"
        assert parsed["native_message_id"] == "msg-99"
        assert parsed["native_channel_id"] == "ch-42"
        assert parsed["storage_path"] == "/tmp/store"
        assert parsed["evidence_path"] == "/tmp/evidence"
        assert parsed["notes"] == ["checked"]

    def test_optional_fields_default_to_none(self) -> None:
        """Optional fields default to null in JSON when not provided."""
        result = LiveSmokeResult(
            transport="lxmf",
            adapter_id="adapter-4",
            status="pass",
        )
        parsed = json.loads(live_result_to_json(result))

        assert parsed["native_message_id"] is None
        assert parsed["native_channel_id"] is None
        assert parsed["storage_path"] is None
        assert parsed["evidence_path"] is None
        assert parsed["notes"] == []
