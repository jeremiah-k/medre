"""Inspect consolidation semantic equivalence tests.

These tests protect the product-shape direction where ``inspect`` subcommands
consolidate the same data returned by ``trace``, ``evidence``, and ``recover``.
They do NOT implement the inspect consolidation — they verify:

1. **Baseline correctness**: existing commands (``trace event``, ``evidence
   --event``, ``recover --event``, ``trace replay``) return semantically
   correct output that any future inspect consolidation must match.
2. **Equivalence contracts**: when ``inspect event --timeline``,
   ``inspect event --evidence``, ``inspect event --recovery``, and
   ``inspect replay <run_id>`` become available in the CLI parser, they
   must produce semantically equivalent output to their counterparts.

The equivalence tests are skip-gated: they only run when the inspect
consolidation flags are available in the CLI parser.  Until then, the
baseline tests prove the reference outputs are correct.

Semantic comparison uses key fields (entry_types, receipt counts,
event_id presence, classification categories) rather than exact string
equality.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SMOKELIKE_YAML = """\
runtime:
  name: inspect-consolidation
  shutdown_timeout_seconds: 10

logging:
  level: WARNING
  format: text

storage:
  backend: sqlite
  path: "{storage_path}"

adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: fake
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: inspect-consolidation

routes:
  mx_to_mesh:
    source_adapters:
      - fake_matrix
    dest_adapters:
      - fake_meshtastic
    directionality: source_to_dest
    enabled: true
"""


def _write_config(tmp_path: Path, db_path: Path) -> str:
    cfg = tmp_path / "inspect_consolidation.yaml"
    cfg.write_text(_SMOKELIKE_YAML.format(storage_path=str(db_path)))
    return str(cfg)


def _seed_db(tmp_path: Path) -> tuple[str, Path]:
    """Seed a DB via smoke and return (event_id, db_path)."""
    db_path = tmp_path / "inspect_consolidation.db"
    config_path = _write_config(tmp_path, db_path)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "smoke",
                    "--config",
                    config_path,
                    "--json",
                ]
            )
    assert exc_info.value.code == 0, f"Smoke seed failed: {stderr_buf.getvalue()}"
    report = json.loads(stdout_buf.getvalue())
    assert report["status"] == "passed"
    return report["event_id"], db_path


def _run_cli_json(*args: str) -> Any:
    """Run CLI with args, capture JSON stdout, and return parsed output.

    Return type is Any because json.loads produces untyped dicts/lists.
    Callers narrow by checking isinstance or accessing known keys.
    """
    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
        main(list(args))
    return json.loads(stdout_buf.getvalue())


def _has_inspect_flag(flag: str) -> bool:
    """Check if inspect event supports a given flag (e.g. --timeline)."""
    from medre.cli.main import _build_parser

    parser = _build_parser()
    # Try parsing with the flag to see if it's accepted.
    try:
        parser.parse_args(
            ["inspect", "event", "--storage-path", "/dev/null", flag, "fake-id"]
        )
    except SystemExit:
        return False
    return True


def _has_inspect_subcommand(subcommand: str) -> bool:
    """Check if inspect has a given subcommand (e.g. replay)."""
    from medre.cli.main import _build_parser

    parser = _build_parser()
    try:
        parser.parse_args(
            ["inspect", subcommand, "--storage-path", "/dev/null", "fake-id"]
        )
    except SystemExit:
        return False
    return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


# ===================================================================
# Baseline: trace event produces valid timeline
# ===================================================================


class TestTraceEventBaseline:
    """Verify ``trace event --json`` output structure and semantics.

    These establish the reference output that ``inspect event --timeline``
    must match once the consolidation lands.
    """

    def test_trace_event_timeline_is_list(self, tmp_path: Path) -> None:
        """trace event --json returns a JSON list."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert isinstance(result, list)

    def test_trace_event_has_required_entry_types(self, tmp_path: Path) -> None:
        """Timeline contains event and receipt entry types."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        entry_types = {e["entry_type"] for e in result}
        assert "event" in entry_types, f"Missing 'event' entry type. Got: {entry_types}"
        assert (
            "receipt" in entry_types
        ), f"Missing 'receipt' entry type. Got: {entry_types}"

    def test_trace_event_entries_have_required_keys(self, tmp_path: Path) -> None:
        """Each timeline entry has timestamp, ordinal, entry_type, data."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        for entry in result:
            assert "timestamp" in entry, f"Missing timestamp in: {entry}"
            assert "ordinal" in entry, f"Missing ordinal in: {entry}"
            assert "entry_type" in entry, f"Missing entry_type in: {entry}"
            assert "data" in entry, f"Missing data in: {entry}"

    def test_trace_event_references_correct_event_id(self, tmp_path: Path) -> None:
        """Event entry references the correct event_id."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        event_entries = [e for e in result if e["entry_type"] == "event"]
        assert len(event_entries) >= 1
        assert event_entries[0]["data"]["event_id"] == event_id

    def test_trace_event_receipt_entries_have_status(self, tmp_path: Path) -> None:
        """Receipt entries include a status field."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        receipt_entries = [e for e in result if e["entry_type"] == "receipt"]
        assert len(receipt_entries) >= 1
        for re in receipt_entries:
            assert (
                "status" in re["data"]
            ), f"Receipt entry missing 'status' field: {re['data']}"
            assert re["data"]["status"] in (
                "sent",
                "failed",
                "dead_lettered",
                "transient_failure",
                "skipped",
            ), f"Unexpected receipt status: {re['data']['status']}"


# ===================================================================
# Baseline: evidence --event produces valid bundle
# ===================================================================


class TestEvidenceEventBaseline:
    """Verify ``evidence --event --json`` output structure and semantics.

    These establish the reference output that ``inspect event --evidence``
    must match once the consolidation lands.
    """

    def test_evidence_bundle_has_status(self, tmp_path: Path) -> None:
        """Evidence bundle has a valid status field."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert result["status"] in (
            "passed",
            "partial",
        ), f"Expected 'passed' or 'partial', got '{result['status']}'"

    def test_evidence_bundle_has_sections(self, tmp_path: Path) -> None:
        """Evidence bundle has a sections dict."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert "sections" in result
        assert isinstance(result["sections"], dict)

    def test_evidence_storage_section_has_event(self, tmp_path: Path) -> None:
        """Storage section contains the correct event."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        storage = result["sections"]["storage"]
        assert storage["data"]["event"] is not None
        assert storage["data"]["event"]["event_id"] == event_id

    def test_evidence_section_status_never_ok(self, tmp_path: Path) -> None:
        """Section status values are 'passed', 'partial', 'error', or 'skipped'.

        The code uses _section_ok() which returns status='passed', never 'ok'.
        This test catches any drift.
        """
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        for name, section in result["sections"].items():
            assert section["status"] != "ok", (
                f"Section '{name}' has stale status='ok'. "
                f"Code returns 'passed', not 'ok'."
            )


# ===================================================================
# Baseline: recover --event produces valid runbook
# ===================================================================


class TestRecoverEventBaseline:
    """Verify ``recover --event --json`` output structure and semantics.

    These establish the reference output that ``inspect event --recovery``
    must match once the consolidation lands.
    """

    def test_recover_runbook_has_scope(self, tmp_path: Path) -> None:
        """Recovery runbook has scope='event'."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert result["scope"] == "event"

    def test_recover_runbook_has_event_id(self, tmp_path: Path) -> None:
        """Recovery runbook references the correct event_id."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert result["event_id"] == event_id

    def test_recover_runbook_has_failure_classification(self, tmp_path: Path) -> None:
        """Recovery runbook has failure_classification dict."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert "failure_classification" in result

    def test_recover_classification_categories_are_valid(
        self,
        tmp_path: Path,
    ) -> None:
        """Classification categories are from the known set."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        valid_categories = {"retryable", "permanent", "operational", "unknown"}
        for cat in result.get("failure_classification", {}):
            assert cat in valid_categories, f"Unexpected classification category: {cat}"

    def test_recover_runbook_has_timeline(self, tmp_path: Path) -> None:
        """Recovery runbook includes a timeline."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert "timeline" in result
        assert isinstance(result["timeline"], list)

    def test_recover_has_recommended_commands(self, tmp_path: Path) -> None:
        """Recovery runbook includes recommended_commands list."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert "recommended_commands" in result
        assert isinstance(result["recommended_commands"], list)


# ===================================================================
# Baseline: trace replay produces valid replay timeline
# ===================================================================


class TestTraceReplayBaseline:
    """Verify ``trace replay --json`` output structure and semantics.

    These establish the reference output that ``inspect replay <run_id>``
    must match once the consolidation lands.
    """

    def _seed_with_replay(self, tmp_path: Path) -> tuple[str, Path, str]:
        """Seed DB with replay receipts and return (event_id, db_path, run_id).

        Uses direct storage seeding to ensure a known replay_run_id.
        """
        import asyncio
        from datetime import datetime, timezone

        from medre.core.events.canonical import (
            CanonicalEvent,
            DeliveryReceipt,
            EventMetadata,
        )
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = tmp_path / "inspect_replay_baseline.db"
        event_id = "evt-replay-baseline"
        run_id = "run-baseline-001"

        async def _seed() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            await storage.initialize()

            ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=ts,
                source_adapter="fake_matrix",
                source_transport_id="fake-transport",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "replay baseline test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            receipt = DeliveryReceipt(
                receipt_id="rcpt-replay-baseline-1",
                event_id=event_id,
                delivery_plan_id="plan-baseline",
                target_adapter="fake_meshtastic",
                status="sent",
                source="replay",
                replay_run_id=run_id,
                created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
            )
            await storage.append_receipt(receipt)
            await storage.close()

        asyncio.run(_seed())
        return event_id, db_path, run_id

    def test_trace_replay_returns_dict(self, tmp_path: Path) -> None:
        """trace replay --json returns a JSON dict."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)
        result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert isinstance(result, dict)

    def test_trace_replay_has_run_id(self, tmp_path: Path) -> None:
        """Replay timeline has the correct run_id."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)
        result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert result["run_id"] == run_id

    def test_trace_replay_has_receipt_count(self, tmp_path: Path) -> None:
        """Replay timeline has receipt_count > 0."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)
        result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert result["receipt_count"] >= 1

    def test_trace_replay_timeline_entries(self, tmp_path: Path) -> None:
        """Replay timeline includes receipt entries."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)
        result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        types = {e["entry_type"] for e in result["timeline"]}
        assert (
            "receipt" in types
        ), f"Expected 'receipt' in timeline entry types, got: {types}"


# ===================================================================
# Equivalence: inspect event --timeline ≡ trace event
# ===================================================================


class TestInspectEventTimelineEquivalence:
    """When ``inspect event --timeline`` is used, its timeline entries must
    be semantically equivalent to ``trace event`` output.

    ``inspect event --timeline`` wraps output in a compound object:
    ``{"event": {...}, "timeline": [...]}``. The ``timeline`` field
    must match ``trace event`` output in entry types and content.
    """

    def test_timeline_entry_types_match(self, tmp_path: Path) -> None:
        """inspect event --timeline and trace event produce the same entry types."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--timeline",
        )
        trace_result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        # inspect wraps in compound object, trace returns flat list.
        inspect_timeline = inspect_result["timeline"]
        inspect_types = {e["entry_type"] for e in inspect_timeline}
        trace_types = {e["entry_type"] for e in trace_result}
        assert (
            inspect_types == trace_types
        ), f"Entry type mismatch: inspect={inspect_types}, trace={trace_types}"

    def test_timeline_receipt_count_matches(self, tmp_path: Path) -> None:
        """Both produce the same number of receipt entries."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--timeline",
        )
        trace_result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        inspect_receipts = [
            e for e in inspect_result["timeline"] if e["entry_type"] == "receipt"
        ]
        trace_receipts = [e for e in trace_result if e["entry_type"] == "receipt"]
        assert len(inspect_receipts) == len(trace_receipts), (
            f"Receipt count mismatch: inspect={len(inspect_receipts)}, "
            f"trace={len(trace_receipts)}"
        )

    def test_timeline_event_id_matches(self, tmp_path: Path) -> None:
        """Both reference the same event_id in their event entries."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--timeline",
        )
        trace_result = _run_cli_json(
            "trace",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        # inspect: event_id in top-level "event" dict AND in timeline entries.
        assert inspect_result["event"]["event_id"] == event_id
        # trace: event entry references the same event_id.
        trace_evt = next(e for e in trace_result if e["entry_type"] == "event")
        assert trace_evt["data"]["event_id"] == event_id


# ===================================================================
# Equivalence: inspect event --evidence ≡ evidence --event
# ===================================================================


class TestInspectEventEvidenceEquivalence:
    """When ``inspect event --evidence`` lands, its output must be
    semantically equivalent to ``evidence --event``.

    Tests are skip-gated until the flag is available.
    """

    @pytest.fixture(autouse=True)
    def _require_evidence_flag(self) -> None:
        if not _has_inspect_flag("--evidence"):
            pytest.skip("inspect event --evidence not yet available")

    def test_evidence_status_matches(self, tmp_path: Path) -> None:
        """Both return the same overall status."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--evidence",
        )
        evidence_result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert inspect_result["evidence"]["status"] == evidence_result["status"], (
            f"Status mismatch: inspect={inspect_result['evidence']['status']}, "
            f"evidence={evidence_result['status']}"
        )

    def test_evidence_event_data_matches(self, tmp_path: Path) -> None:
        """Both contain the same event data in the storage section."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--evidence",
        )
        evidence_result = _run_cli_json(
            "evidence",
            "--event",
            event_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        # inspect: event_id in top-level "event" dict.
        inspect_evt_id = inspect_result["event"]["event_id"]
        evidence_evt_id = (
            evidence_result.get("sections", {})
            .get("storage", {})
            .get("data", {})
            .get("event", {})
            .get("event_id")
        )
        assert inspect_evt_id == evidence_evt_id == event_id

    def test_evidence_section_status_never_ok(self, tmp_path: Path) -> None:
        """Section statuses are never 'ok' — must be 'passed', 'partial', etc."""
        event_id, db_path = _seed_db(tmp_path)
        result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--evidence",
        )
        for name, section in result["evidence"].get("sections", {}).items():
            assert section["status"] != "ok", (
                f"Section '{name}' has stale status='ok'. "
                f"Code returns 'passed', not 'ok'."
            )


# ===================================================================
# Equivalence: inspect event --recovery ≡ recover --event
# ===================================================================


class TestInspectEventRecoveryEquivalence:
    """When ``inspect event --recovery`` lands, its output must be
    semantically equivalent to ``recover --event`` classification.

    Tests are skip-gated until the flag is available.
    """

    @pytest.fixture(autouse=True)
    def _require_recovery_flag(self) -> None:
        if not _has_inspect_flag("--recovery"):
            pytest.skip("inspect event --recovery not yet available")

    def test_recovery_classification_matches(self, tmp_path: Path) -> None:
        """Both produce the same failure classification categories."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--recovery",
        )
        recover_result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        inspect_cats = set(inspect_result.get("failure_classification", {}).keys())
        recover_cats = set(recover_result.get("failure_classification", {}).keys())
        assert inspect_cats == recover_cats, (
            f"Classification mismatch: inspect={inspect_cats}, "
            f"recover={recover_cats}"
        )

    def test_recovery_event_id_matches(self, tmp_path: Path) -> None:
        """Both reference the same event_id."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--recovery",
        )
        recover_result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert (
            inspect_result["recovery"]["event_id"]
            == recover_result["event_id"]
            == event_id
        )

    def test_recovery_timeline_length_matches(self, tmp_path: Path) -> None:
        """Both produce timelines of the same length."""
        event_id, db_path = _seed_db(tmp_path)
        inspect_result = _run_cli_json(
            "inspect",
            "event",
            event_id,
            "--storage-path",
            str(db_path),
            "--recovery",
        )
        recover_result = _run_cli_json(
            "recover",
            "--storage-path",
            str(db_path),
            "--event",
            event_id,
            "--json",
        )
        assert len(inspect_result["recovery"]["timeline"]) == len(
            recover_result["timeline"]
        )


# ===================================================================
# Equivalence: inspect replay <run_id> ≡ trace replay
# ===================================================================


class TestInspectReplayEquivalence:
    """When ``inspect replay <run_id>`` lands, its output must be
    semantically equivalent to ``trace replay``.

    Tests are skip-gated until the subcommand is available.
    """

    @pytest.fixture(autouse=True)
    def _require_replay_subcommand(self) -> None:
        if not _has_inspect_subcommand("replay"):
            pytest.skip("inspect replay subcommand not yet available")

    def _seed_with_replay(self, tmp_path: Path) -> tuple[str, Path, str]:
        """Seed DB with replay receipts and return (event_id, db_path, run_id).

        Uses direct storage seeding to ensure a known replay_run_id.
        """
        import asyncio
        from datetime import datetime, timezone

        from medre.core.events.canonical import (
            CanonicalEvent,
            DeliveryReceipt,
            EventMetadata,
        )
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = tmp_path / "inspect_replay_equiv.db"
        event_id = "evt-replay-equiv"
        run_id = "run-equiv-001"

        async def _seed() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            await storage.initialize()

            ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=ts,
                source_adapter="fake_matrix",
                source_transport_id="fake-transport",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "replay equiv test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            receipt = DeliveryReceipt(
                receipt_id="rcpt-replay-equiv-1",
                event_id=event_id,
                delivery_plan_id="plan-equiv",
                target_adapter="fake_meshtastic",
                status="sent",
                source="replay",
                replay_run_id=run_id,
                created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
            )
            await storage.append_receipt(receipt)
            await storage.close()

        asyncio.run(_seed())
        return event_id, db_path, run_id

    def test_replay_run_id_matches(self, tmp_path: Path) -> None:
        """Both return the same run_id."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)

        inspect_result = _run_cli_json(
            "inspect",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
        )
        trace_result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert inspect_result["run_id"] == trace_result["run_id"] == run_id

    def test_replay_receipt_count_matches(self, tmp_path: Path) -> None:
        """Both report the same receipt_count."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)

        inspect_result = _run_cli_json(
            "inspect",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
        )
        trace_result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert inspect_result["receipt_count"] == trace_result["receipt_count"]

    def test_replay_event_ids_match(self, tmp_path: Path) -> None:
        """Both list the same event_ids."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)

        inspect_result = _run_cli_json(
            "inspect",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
        )
        trace_result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert inspect_result["event_ids"] == trace_result["event_ids"]

    def test_replay_status_matches(self, tmp_path: Path) -> None:
        """Both report the same status."""
        event_id, db_path, run_id = self._seed_with_replay(tmp_path)

        inspect_result = _run_cli_json(
            "inspect",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
        )
        trace_result = _run_cli_json(
            "trace",
            "replay",
            run_id,
            "--storage-path",
            str(db_path),
            "--json",
        )
        assert inspect_result["status"] == trace_result["status"]


# ===================================================================
# Alpha walkthrough uses inspect-based investigation
# ===================================================================


class TestWalkthroughUsesInspect:
    """The alpha walkthrough runbook should use inspect-based investigation
    commands (inspect receipts, inspect event) as the primary investigation
    surface, with trace/evidence/recover available as deeper commands.

    This test verifies the alpha-walkthrough.md references the inspect
    command surface.
    """

    def test_alpha_walkthrough_mentions_inspect(self) -> None:
        """alpha-walkthrough.md must mention 'medre inspect'."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        walkthrough = root / "docs" / "ops" / "operator-workflows.md"
        if not walkthrough.exists():
            pytest.skip("operator-workflows.md not found")
        text = walkthrough.read_text()
        assert "medre inspect" in text, (
            "operator-workflows.md must reference 'medre inspect' as "
            "the primary investigation command."
        )

    def test_alpha_walkthrough_inspect_before_trace(self) -> None:
        """inspect step appears before trace step in walkthrough order."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        walkthrough = root / "docs" / "ops" / "operator-workflows.md"
        if not walkthrough.exists():
            pytest.skip("operator-workflows.md not found")
        text = walkthrough.read_text()
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        if inspect_pos < 0 or trace_pos < 0:
            pytest.skip("Both inspect and trace must be mentioned")
        assert inspect_pos < trace_pos, (
            "operator-workflows.md should present inspect before trace "
            "(inspect is the primary investigation surface)."
        )
