"""Unit tests for tests/helpers/live_harness.py.

Covers redaction, environment gating, secret-leak detection,
bounded async execution, smoke-test result serialisation,
NOT EXECUTED result factory, and live artifact directory convention.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.helpers.live_harness import (
    LiveRequirement,
    LiveSmokeResult,
    assert_no_secret_leak,
    bounded,
    get_live_artifact_dir,
    live_env_status,
    live_result_to_json,
    not_executed_result,
    redact_env_value,
)

from tests.helpers.live_config import (
    matrix_second_user_env_set,
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

    def test_missing_var_not_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            assert_no_secret_leak({"token": "syt_secret123"}, ["syt_secret123"])

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


# ===================================================================
# (f) not_executed_result
# ===================================================================


class TestNotExecutedResult:
    """Tests for the NOT EXECUTED result factory."""

    def test_status_is_not_executed(self) -> None:
        """Result has status='not_executed'."""
        result = not_executed_result(
            transport="meshtastic", adapter_id="radio-serial"
        )
        assert result.status == "not_executed"

    def test_transport_and_adapter_id_preserved(self) -> None:
        """Transport and adapter_id are passed through."""
        result = not_executed_result(
            transport="meshcore", adapter_id="ble-radio"
        )
        assert result.transport == "meshcore"
        assert result.adapter_id == "ble-radio"

    def test_reason_appears_in_notes(self) -> None:
        """Provided reason is placed in the notes tuple."""
        result = not_executed_result(
            transport="meshtastic",
            adapter_id="radio-serial",
            reason="serial radio not connected",
        )
        assert result.notes == ("serial radio not connected",)

    def test_no_reason_yields_empty_notes(self) -> None:
        """When no reason given, notes is empty tuple."""
        result = not_executed_result(
            transport="meshtastic", adapter_id="radio"
        )
        assert result.notes == ()

    def test_optional_fields_are_none(self) -> None:
        """No native IDs, storage, or evidence paths."""
        result = not_executed_result(
            transport="meshtastic", adapter_id="radio"
        )
        assert result.native_message_id is None
        assert result.native_channel_id is None
        assert result.storage_path is None
        assert result.evidence_path is None

    def test_serializable_to_json(self) -> None:
        """Result serialises cleanly via live_result_to_json."""
        result = not_executed_result(
            transport="meshtastic",
            adapter_id="radio",
            reason="hardware absent",
        )
        parsed = json.loads(live_result_to_json(result))
        assert parsed["status"] == "not_executed"
        assert parsed["notes"] == ["hardware absent"]

    def test_no_secret_leak_in_not_executed_result(self) -> None:
        """not_executed result with no secrets passes leak check."""
        result = not_executed_result(
            transport="meshtastic",
            adapter_id="radio",
            reason="device not found",
        )
        assert_no_secret_leak(result, ["syt_some_secret_token"])


# ===================================================================
# (g) get_live_artifact_dir
# ===================================================================


class TestGetLiveArtifactDir:
    """Tests for the live artifact directory convention helper."""

    def test_default_path_under_ci_artifacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When MEDRE_LIVE_ARTIFACT_DIR is unset, returns .ci-artifacts/live-evidence/<ts>."""
        monkeypatch.delenv("MEDRE_LIVE_ARTIFACT_DIR", raising=False)
        # The function walks up from live_harness.py to find pyproject.toml.
        # We just verify it returns a Path and the directory exists.
        artifact_dir = get_live_artifact_dir()
        assert isinstance(artifact_dir, Path)
        assert artifact_dir.exists()
        assert "live-evidence" in str(artifact_dir)

    def test_env_override_respected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When MEDRE_LIVE_ARTIFACT_DIR is set, uses that path."""
        custom_dir = tmp_path / "custom-artifacts"
        monkeypatch.setenv("MEDRE_LIVE_ARTIFACT_DIR", str(custom_dir))
        artifact_dir = get_live_artifact_dir()
        assert artifact_dir == custom_dir
        assert artifact_dir.exists()

    def test_directory_created_if_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Creates the directory (and parents) if they don't exist."""
        deep_dir = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("MEDRE_LIVE_ARTIFACT_DIR", str(deep_dir))
        assert not deep_dir.exists()
        artifact_dir = get_live_artifact_dir()
        assert artifact_dir.exists()
        assert artifact_dir == deep_dir

    def test_existing_directory_no_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No error when directory already exists."""
        existing = tmp_path / "already-here"
        existing.mkdir()
        monkeypatch.setenv("MEDRE_LIVE_ARTIFACT_DIR", str(existing))
        artifact_dir = get_live_artifact_dir()
        assert artifact_dir == existing


# ===================================================================
# (h) matrix_second_user_env_set
# ===================================================================


class TestMatrixSecondUserEnvSet:
    """Tests for the second Matrix user environment detection helper."""

    def test_returns_false_when_both_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when neither var is set."""
        monkeypatch.delenv("MATRIX_SECOND_USER_ID", raising=False)
        monkeypatch.delenv("MATRIX_SECOND_ACCESS_TOKEN", raising=False)
        assert matrix_second_user_env_set() is False

    def test_returns_false_when_only_user_id_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when only user ID is set."""
        monkeypatch.setenv("MATRIX_SECOND_USER_ID", "@test:localhost")
        monkeypatch.delenv("MATRIX_SECOND_ACCESS_TOKEN", raising=False)
        assert matrix_second_user_env_set() is False

    def test_returns_false_when_only_token_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when only access token is set."""
        monkeypatch.delenv("MATRIX_SECOND_USER_ID", raising=False)
        monkeypatch.setenv("MATRIX_SECOND_ACCESS_TOKEN", "syt_some_token")
        assert matrix_second_user_env_set() is False

    def test_returns_false_when_empty_strings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when both set to empty strings."""
        monkeypatch.setenv("MATRIX_SECOND_USER_ID", "")
        monkeypatch.setenv("MATRIX_SECOND_ACCESS_TOKEN", "")
        assert matrix_second_user_env_set() is False

    def test_returns_true_when_both_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns True when both vars are set and non-empty."""
        monkeypatch.setenv("MATRIX_SECOND_USER_ID", "@second:localhost")
        monkeypatch.setenv("MATRIX_SECOND_ACCESS_TOKEN", "syt_token_value")
        assert matrix_second_user_env_set() is True

    def test_never_reads_or_prints_token_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Helper returns bool without exposing secret values."""
        monkeypatch.setenv("MATRIX_SECOND_USER_ID", "@second:localhost")
        monkeypatch.setenv("MATRIX_SECOND_ACCESS_TOKEN", "syt_secret_value")
        result = matrix_second_user_env_set()
        assert result is True
        # The function returns bool only — no string value to inspect.
