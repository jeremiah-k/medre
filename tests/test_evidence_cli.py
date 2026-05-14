"""Tests for medre evidence CLI and evidence bundle collection.

Proves that :func:`medre.runtime.evidence.collect_evidence_bundle` produces
a valid evidence report with all expected sections, proper status computation,
secret redaction, and read-only storage behaviour.

Every test:

- Uses **fake adapters** — no live transports or SDKs.
- Uses **in-memory or temp-file storage** — no network.
- Verifies **read-only** storage behaviour (no file creation).
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main
from medre.runtime.evidence import collect_evidence_bundle


# ---------------------------------------------------------------------------
# Sample TOML configs
# ---------------------------------------------------------------------------

CONFIG_FAKE_ADAPTERS = """\
[runtime]
name = "test-evidence"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test_evidence.db"

[adapters.matrix.main]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "syt_super_secret_token_12345"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "fake"
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"

[routes.bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_MEMORY_STORAGE = """\
[runtime]
name = "test-evidence-memory"

[storage]
backend = "memory"

[adapters.matrix.main]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok_secret_abc"
encryption_mode = "plaintext"
"""

CONFIG_ROUTE_ERRORS = """\
[runtime]
name = "test-evidence-route-errors"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
encryption_mode = "plaintext"

[routes.broken]
source_adapters = ["nonexistent"]
dest_adapters = ["also_missing"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_NO_ADAPTERS = """\
[runtime]
name = "test-evidence-no-adapters"

[storage]
backend = "memory"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear config-related env vars for each test."""
    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also clear any MEDRE adapter env vars that might leak.
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_MATRIX") or key.startswith("MEDRE_MESHTASTIC"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def config_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write fake-adapter config to temp file with MEDRE_HOME isolation."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_FAKE_ADAPTERS)
    return p


@pytest.fixture()
def config_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write memory-storage config to temp file."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_MEMORY_STORAGE)
    return p


@pytest.fixture()
def config_route_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write config with route errors to temp file."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_ROUTE_ERRORS)
    return p


@pytest.fixture()
def config_no_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write config with no adapters to temp file."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_NO_ADAPTERS)
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> str:
    """Run CLI with given args, capture stdout, return output."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_json(*args: str) -> dict[str, Any]:
    """Run CLI with --json and return parsed dict."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return json.loads(stdout.getvalue())


async def _make_populated_db(
    db_path: str,
) -> tuple[str, str]:
    """Create and populate a SQLite DB for testing. Returns (event_id, receipt_id)."""
    from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
    from medre.core.events.kinds import EventKind
    from medre.core.events.metadata import EventMetadata
    from medre.core.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(db_path)
    await storage.initialize()

    event = CanonicalEvent(
        event_id="ev-evidence-test-001",
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        source_adapter="main",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "evidence test message"},
        metadata=EventMetadata(),
    )
    await storage.append(event)

    receipt = DeliveryReceipt(
        receipt_id="rcpt-001",
        event_id="ev-evidence-test-001",
        delivery_plan_id="dp-001",
        target_adapter="radio",
        status="sent",
        source="live",
        created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    await storage.append_receipt(receipt)

    await storage.close()
    return event.event_id, receipt.receipt_id


async def _make_populated_db_with_failure(
    db_path: str,
    event_id: str = "ev-evidence-fail-001",
    receipt_status: str = "failed",
    receipt_error: str | None = "TimeoutError: connection timed out",
    receipt_source: str = "live",
) -> str:
    """Create and populate a SQLite DB with a receipt in given status.

    Returns the event_id.
    """
    from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
    from medre.core.events.kinds import EventKind
    from medre.core.events.metadata import EventMetadata
    from medre.core.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(db_path)
    await storage.initialize()

    event = CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        source_adapter="main",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "evidence failure test"},
        metadata=EventMetadata(),
    )
    await storage.append(event)

    receipt = DeliveryReceipt(
        receipt_id="rcpt-fail-001",
        event_id=event_id,
        delivery_plan_id="dp-fail-001",
        target_adapter="radio",
        status=receipt_status,
        source=receipt_source,
        error=receipt_error,
        created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    await storage.append_receipt(receipt)

    await storage.close()
    return event_id


# ---------------------------------------------------------------------------
# Tests: collect_evidence_bundle — core behaviour
# ---------------------------------------------------------------------------


class TestEvidenceBundleCore:
    """Core evidence bundle collection tests."""

    @pytest.mark.asyncio
    async def test_bundle_status_ok(self, config_fake: Path) -> None:
        """Fake adapter config with no storage DB produces partial (missing DB)."""
        report = await collect_evidence_bundle(str(config_fake))
        assert report["schema_version"] == 1
        assert report["status"] in ("ok", "partial")
        assert report["collected_at"] is not None
        assert report["medre_version"] is not None
        assert report["config_source"] is not None

    @pytest.mark.asyncio
    async def test_bundle_has_all_sections(self, config_fake: Path) -> None:
        """All expected sections are present."""
        report = await collect_evidence_bundle(str(config_fake))
        sections = report["sections"]
        assert "config_summary" in sections
        assert "route_validation" in sections
        assert "diagnostics_snapshot" in sections
        assert "live_health" in sections
        assert "storage" in sections

    @pytest.mark.asyncio
    async def test_live_health_skipped_by_default(self, config_fake: Path) -> None:
        """Live health section is skipped without --include-refresh-health."""
        report = await collect_evidence_bundle(str(config_fake))
        assert report["runtime_started"] is False
        assert report["sections"]["live_health"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_live_health_populated_with_flag(self, config_fake: Path) -> None:
        """Live health is populated when include_refresh_health=True."""
        report = await collect_evidence_bundle(
            str(config_fake),
            include_refresh_health=True,
        )
        assert report["runtime_started"] is True
        assert report["sections"]["live_health"]["status"] in ("ok", "partial")
        assert report["sections"]["live_health"]["data"] is not None

    @pytest.mark.asyncio
    async def test_config_error_status(self) -> None:
        """Invalid config path produces error status."""
        report = await collect_evidence_bundle("/nonexistent/config.toml")
        assert report["status"] == "error"
        assert len(report["errors"]) > 0
        assert report["sections"] == {}


# ---------------------------------------------------------------------------
# Tests: config summary redaction
# ---------------------------------------------------------------------------

# Secret patterns to verify are never present in output.
_SECRET_PATTERNS_IN_OUTPUT = (
    "syt_super_secret_token_12345",
    "tok_secret_abc",
    "access_token",
    "password",
)


class TestEvidenceRedaction:
    """Verify secrets are never present in evidence output."""

    @pytest.mark.asyncio
    async def test_config_summary_no_secret_values(self, config_fake: Path) -> None:
        """Config summary never contains adapter config secret values."""
        report = await collect_evidence_bundle(str(config_fake))
        raw = json.dumps(report, sort_keys=True)
        # The secret token value must never appear.
        assert "syt_super_secret_token_12345" not in raw
        assert "tok_secret_abc" not in raw

    @pytest.mark.asyncio
    async def test_config_summary_adapter_metadata_only(self, config_fake: Path) -> None:
        """Config summary adapters have only safe metadata fields."""
        report = await collect_evidence_bundle(str(config_fake))
        adapters = report["sections"]["config_summary"]["data"]["adapters"]
        assert len(adapters) >= 2
        for adapter in adapters:
            # Safe fields only.
            assert "transport" in adapter
            assert "adapter_id" in adapter
            assert "enabled" in adapter
            assert "adapter_kind" in adapter
            # No secret-bearing fields.
            assert "access_token" not in adapter
            assert "password" not in adapter
            assert "config" not in adapter

    @pytest.mark.asyncio
    async def test_no_access_token_key_in_output(self, config_fake: Path) -> None:
        """No 'access_token' key appears in the full JSON output."""
        report = await collect_evidence_bundle(str(config_fake))
        raw = json.dumps(report, sort_keys=True)
        # 'access_token' as a key should not appear (it's not in adapter metadata).
        # We check for "access_token": pattern (as a JSON key).
        assert '"access_token"' not in raw


# ---------------------------------------------------------------------------
# Tests: storage section
# ---------------------------------------------------------------------------


class TestEvidenceStorage:
    """Storage section read-only behaviour."""

    @pytest.mark.asyncio
    async def test_missing_db_partial(self, config_fake: Path) -> None:
        """Missing DB produces partial storage section, no file creation."""
        report = await collect_evidence_bundle(str(config_fake))
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "partial"
        assert storage_section["data"]["db_exists"] is False
        # Verify DB was NOT created.
        assert not Path(storage_section["data"]["db_path"]).exists()

    @pytest.mark.asyncio
    async def test_memory_storage_skipped(self, config_memory: Path) -> None:
        """Memory backend produces skipped storage section."""
        report = await collect_evidence_bundle(str(config_memory))
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_existing_db_ok(self, config_fake: Path) -> None:
        """Existing populated DB produces ok storage section."""
        # With MEDRE_HOME, {state} resolves to config_fake.parent / "state"
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        await _make_populated_db(db_path)

        report = await collect_evidence_bundle(str(config_fake))
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "ok"
        assert storage_section["data"]["db_exists"] is True
        assert storage_section["data"]["event_count"] == 1
        assert storage_section["data"]["receipt_count"] == 1

    @pytest.mark.asyncio
    async def test_event_lookup(self, config_fake: Path) -> None:
        """--event fetches the event from storage."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id, _ = await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "ok"
        assert storage_section["data"]["event"] is not None
        assert storage_section["data"]["event"]["event_id"] == event_id

    @pytest.mark.asyncio
    async def test_event_not_found_partial(self, config_fake: Path) -> None:
        """--event with non-existent ID produces partial."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), event_id="nonexistent-event-id",
        )
        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "partial"
        assert storage_section["data"]["event"] is None

    @pytest.mark.asyncio
    async def test_replay_run_lookup(self, config_fake: Path) -> None:
        """--replay-run fetches receipts (empty list when no match)."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), replay_run_id="nonexistent-run",
        )
        storage_section = report["sections"]["storage"]
        # No receipts match, but the section itself is ok (storage worked).
        assert storage_section["status"] in ("ok", "partial")
        assert storage_section["data"]["replay_run_receipts"] is not None
        assert isinstance(storage_section["data"]["replay_run_receipts"], list)

    @pytest.mark.asyncio
    async def test_db_not_created_during_evidence(self, config_fake: Path) -> None:
        """Evidence collection never creates a missing DB file."""
        report = await collect_evidence_bundle(str(config_fake))
        db_path = report["sections"]["storage"]["data"]["db_path"]
        assert not Path(db_path).exists()


# ---------------------------------------------------------------------------
# Tests: route validation
# ---------------------------------------------------------------------------


class TestEvidenceRouteValidation:
    """Route validation section."""

    @pytest.mark.asyncio
    async def test_valid_routes(self, config_fake: Path) -> None:
        """Valid route config produces ok route_validation."""
        report = await collect_evidence_bundle(str(config_fake))
        rv = report["sections"]["route_validation"]
        assert rv["status"] == "ok"
        assert rv["data"]["valid"] is True
        assert rv["data"]["route_count"] == 1
        assert rv["data"]["route_enabled"] == 1

    @pytest.mark.asyncio
    async def test_route_errors(self, config_route_errors: Path) -> None:
        """Invalid route config produces partial route_validation."""
        report = await collect_evidence_bundle(str(config_route_errors))
        rv = report["sections"]["route_validation"]
        assert rv["status"] == "partial"
        assert rv["data"]["valid"] is False
        assert len(rv["data"]["route_errors"]) > 0


# ---------------------------------------------------------------------------
# Tests: diagnostics snapshot
# ---------------------------------------------------------------------------


class TestEvidenceDiagnosticsSnapshot:
    """Diagnostics snapshot section."""

    @pytest.mark.asyncio
    async def test_snapshot_present(self, config_fake: Path) -> None:
        """Snapshot section has data with adapters."""
        report = await collect_evidence_bundle(str(config_fake))
        ds = report["sections"]["diagnostics_snapshot"]
        assert ds["status"] == "ok"
        assert ds["data"] is not None
        assert "schema_version" in ds["data"]

    @pytest.mark.asyncio
    async def test_no_adapters_snapshot_error(self, config_no_adapters: Path) -> None:
        """No adapters produces error diagnostics_snapshot."""
        report = await collect_evidence_bundle(str(config_no_adapters))
        ds = report["sections"]["diagnostics_snapshot"]
        assert ds["status"] == "error"


# ---------------------------------------------------------------------------
# Tests: overall status computation
# ---------------------------------------------------------------------------


class TestEvidenceOverallStatus:
    """Overall status is correctly computed from sections."""

    @pytest.mark.asyncio
    async def test_all_ok_when_db_exists(self, config_fake: Path) -> None:
        """All sections ok when DB exists and config is valid."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        await _make_populated_db(db_path)

        report = await collect_evidence_bundle(str(config_fake))
        assert report["status"] == "ok"

    @pytest.mark.asyncio
    async def test_partial_when_storage_missing(self, config_fake: Path) -> None:
        """Partial when storage DB is missing."""
        report = await collect_evidence_bundle(str(config_fake))
        assert report["status"] == "partial"

    @pytest.mark.asyncio
    async def test_error_when_config_missing(self) -> None:
        """Error when config file does not exist."""
        report = await collect_evidence_bundle("/nonexistent/path.toml")
        assert report["status"] == "error"


# ---------------------------------------------------------------------------
# Tests: JSON output validity
# ---------------------------------------------------------------------------


class TestEvidenceJsonOutput:
    """JSON output is valid and complete."""

    @pytest.mark.asyncio
    async def test_json_dumps_succeeds(self, config_fake: Path) -> None:
        """Full report is valid JSON."""
        report = await collect_evidence_bundle(str(config_fake))
        raw = json.dumps(report, sort_keys=True, indent=2)
        parsed = json.loads(raw)
        assert parsed["schema_version"] == 1

    @pytest.mark.asyncio
    async def test_json_has_limitations(self, config_fake: Path) -> None:
        """Report includes limitations."""
        report = await collect_evidence_bundle(str(config_fake))
        assert isinstance(report["limitations"], list)
        assert len(report["limitations"]) > 0

    @pytest.mark.asyncio
    async def test_json_has_errors_list(self, config_fake: Path) -> None:
        """Report includes errors list (may be empty)."""
        report = await collect_evidence_bundle(str(config_fake))
        assert isinstance(report["errors"], list)


# ---------------------------------------------------------------------------
# Tests: CLI dispatch
# ---------------------------------------------------------------------------


class TestEvidenceCli:
    """CLI dispatch for evidence command."""

    def test_evidence_cli_json(self, config_fake: Path) -> None:
        """CLI evidence --json produces valid JSON output."""
        result = _run_cli_json("evidence", "--config", str(config_fake), "--json")
        assert result["schema_version"] == 1
        assert "sections" in result

    def test_evidence_cli_human_readable(self, config_fake: Path) -> None:
        """CLI evidence without --json produces human-readable output."""
        output = _run_cli("evidence", "--config", str(config_fake))
        assert "Evidence:" in output

    def test_evidence_cli_config_error(self) -> None:
        """CLI evidence with bad config path exits with error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["evidence", "--config", "/nonexistent.toml", "--json"])
        assert exc_info.value.code == 2  # EXIT_CONFIG

    def test_evidence_cli_event_arg(self, config_fake: Path) -> None:
        """CLI evidence --event passes event_id to bundle."""
        result = _run_cli_json(
            "evidence", "--config", str(config_fake), "--json",
            "--event", "ev-123",
        )
        storage = result["sections"]["storage"]
        # DB doesn't exist, so it'll be partial, but event_id was passed.
        assert storage["status"] == "partial"

    def test_evidence_cli_replay_run_arg(self, config_fake: Path) -> None:
        """CLI evidence --replay-run passes replay_run_id to bundle."""
        result = _run_cli_json(
            "evidence", "--config", str(config_fake), "--json",
            "--replay-run", "run-456",
        )
        storage = result["sections"]["storage"]
        # Missing DB so partial, but replay_run_id was passed.
        assert storage["status"] in ("partial", "ok")


# ---------------------------------------------------------------------------
# Tests: incident_summary in storage section
# ---------------------------------------------------------------------------


class TestIncidentSummary:
    """Compact incident summary in evidence bundles when --event is used."""

    @pytest.mark.asyncio
    async def test_incident_summary_success(self, config_fake: Path) -> None:
        """All-sent receipts produce classification='success'."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id, _ = await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["event_id"] == event_id
        assert summary["event_kind"] == "message.text"
        assert summary["source_adapter"] == "main"
        assert summary["classification"] == "success"
        assert summary["first_failure_kind"] is None
        assert summary["receipt_count"] == 1
        assert summary["failed_count"] == 0
        assert summary["sent_count"] == 1
        assert summary["replay_receipts_present"] is False
        assert summary["native_refs_present"] is False
        assert isinstance(summary["recommended_commands"], list)

    @pytest.mark.asyncio
    async def test_incident_summary_retryable(self, config_fake: Path) -> None:
        """Failed receipt with timeout error produces retryable classification."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_failure(
            db_path,
            event_id="ev-evidence-retry-001",
            receipt_status="failed",
            receipt_error="TimeoutError: connection timed out",
        )

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "retryable"
        assert summary["first_failure_kind"] == "adapter_transient"
        assert summary["failed_count"] == 1
        assert summary["sent_count"] == 0

    @pytest.mark.asyncio
    async def test_incident_summary_permanent(self, config_fake: Path) -> None:
        """Failed receipt with permission error produces permanent classification."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_failure(
            db_path,
            event_id="ev-evidence-perm-001",
            receipt_status="failed",
            receipt_error="permission denied",
        )

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "permanent"
        assert summary["first_failure_kind"] == "adapter_permanent"

    @pytest.mark.asyncio
    async def test_incident_summary_operational(self, config_fake: Path) -> None:
        """Failed receipt with capacity error produces operational classification."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_failure(
            db_path,
            event_id="ev-evidence-op-001",
            receipt_status="failed",
            receipt_error="delivery_capacity_exceeded",
        )

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["classification"] == "operational"
        assert summary["first_failure_kind"] == "capacity_rejection"

    @pytest.mark.asyncio
    async def test_incident_summary_replay_receipts(self, config_fake: Path) -> None:
        """Replay receipts are detected in incident summary."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_failure(
            db_path,
            event_id="ev-evidence-replay-001",
            receipt_status="failed",
            receipt_error="TimeoutError: connection timed out",
            receipt_source="replay",
        )

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        assert summary["replay_receipts_present"] is True

    @pytest.mark.asyncio
    async def test_incident_summary_recommended_commands(self, config_fake: Path) -> None:
        """Recommended commands are populated based on classification."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id = await _make_populated_db_with_failure(
            db_path,
            event_id="ev-evidence-cmds-001",
            receipt_status="failed",
            receipt_error="TimeoutError: connection timed out",
        )

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        cmds = summary["recommended_commands"]
        assert len(cmds) > 0
        cmd_text = " ".join(cmds)
        assert "trace event" in cmd_text

    @pytest.mark.asyncio
    async def test_incident_summary_absent_without_event(self, config_fake: Path) -> None:
        """incident_summary is not present when --event is not provided."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        await _make_populated_db(db_path)

        report = await collect_evidence_bundle(str(config_fake))
        data = report["sections"]["storage"]["data"]
        assert "incident_summary" not in data or data.get("incident_summary") is None

    @pytest.mark.asyncio
    async def test_incident_summary_fields_complete(self, config_fake: Path) -> None:
        """All required incident_summary fields are present."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id, _ = await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        required_fields = [
            "event_id", "event_kind", "source_adapter",
            "first_failure_kind", "classification",
            "replay_receipts_present", "native_refs_present",
            "receipt_count", "failed_count", "sent_count",
            "recommended_commands",
        ]
        for field in required_fields:
            assert field in summary, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_incident_summary_json_safe(self, config_fake: Path) -> None:
        """incident_summary is fully JSON-serialisable."""
        db_path = str(config_fake.parent / "state" / "test_evidence.db")
        event_id, _ = await _make_populated_db(db_path)

        report = await collect_evidence_bundle(
            str(config_fake), event_id=event_id,
        )
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        raw = json.dumps(summary, sort_keys=True)
        assert isinstance(raw, str)
