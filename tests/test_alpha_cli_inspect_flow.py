"""Alpha CLI tests: inspect flow (receipts, timeline, evidence, recovery).

Split from the original test_alpha_walkthrough_cli.py monolith.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from medre.cli import main
from tests.helpers.alpha_cli import (
    seed_via_smoke_cli,
)

# ---------------------------------------------------------------------------
# Tests: inspect receipts with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaInspectReceiptsCLI:
    """``medre inspect receipts --event <id> --storage-path <db>`` via main()."""

    def test_inspect_receipts_lists_receipts(self, tmp_path: Path) -> None:
        """inspect receipts --storage-path prints delivery receipts."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )

        output = stdout_buf.getvalue()
        assert "sent" in output

    def test_inspect_receipts_exits_cleanly(self, tmp_path: Path) -> None:
        """inspect receipts does not call sys.exit on success."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        # Should NOT raise SystemExit.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )


# ---------------------------------------------------------------------------
# Tests: inspect event --timeline with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaInspectEventTimelineCLI:
    """``medre inspect event <id> --timeline --storage-path <db>`` via main()."""

    def test_inspect_event_timeline_json(self, tmp_path: Path) -> None:
        """inspect event --timeline --storage-path returns JSON with timeline."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        assert isinstance(result, dict)
        assert "event" in result
        assert "timeline" in result
        assert isinstance(result["timeline"], list)
        assert len(result["timeline"]) >= 1

    def test_inspect_event_timeline_has_receipt_entries(self, tmp_path: Path) -> None:
        """Timeline includes at least one receipt entry."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        entry_types = [e.get("entry_type") for e in result["timeline"]]
        assert (
            "receipt" in entry_types
        ), f"Expected 'receipt' in timeline entry types, got: {entry_types}"


# ---------------------------------------------------------------------------
# Tests: inspect event --evidence with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaInspectEventEvidenceCLI:
    """``medre inspect event <id> --evidence --storage-path <db>`` via main()."""

    def test_inspect_event_evidence_json_bundle(self, tmp_path: Path) -> None:
        """inspect event --evidence --storage-path returns JSON with evidence."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        assert isinstance(result, dict)
        assert "event" in result
        assert "evidence" in result
        assert result["evidence"]["status"] in ("partial", "passed")

    def test_inspect_event_evidence_has_event(self, tmp_path: Path) -> None:
        """Evidence section contains the requested event."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        evidence = result["evidence"]
        assert evidence["sections"]["storage"]["data"]["event"] is not None
        assert evidence["sections"]["storage"]["data"]["event"]["event_id"] == event_id


# ---------------------------------------------------------------------------
# Tests: inspect event --recovery with --storage-path
# ---------------------------------------------------------------------------


class TestAlphaInspectEventRecoveryCLI:
    """``medre inspect event <id> --recovery --storage-path <db>`` via main()."""

    def test_inspect_event_recovery_json(self, tmp_path: Path) -> None:
        """inspect event --recovery --storage-path returns JSON with recovery."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--recovery",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        assert isinstance(result, dict)
        assert "event" in result
        assert "recovery" in result

    def test_inspect_event_combined_flags(self, tmp_path: Path) -> None:
        """inspect event --timeline --evidence --recovery returns all sections."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--evidence",
                    "--recovery",
                    "--storage-path",
                    str(db_path),
                ]
            )

        result = json.loads(stdout_buf.getvalue())
        assert "event" in result
        assert "timeline" in result
        assert "evidence" in result
        assert "recovery" in result
