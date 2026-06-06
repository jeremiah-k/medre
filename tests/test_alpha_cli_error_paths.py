"""Alpha CLI tests: error paths and no-traceback assertions.

Split from the original test_alpha_walkthrough_cli.py monolith.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.alpha_cli import (
    smoke_config_path,
)

# ---------------------------------------------------------------------------
# Test: no tracebacks on invalid inputs
# ---------------------------------------------------------------------------


class TestAlphaNoTracebacks:
    """Verify commands produce clean errors, not tracebacks."""

    def test_inspect_receipts_missing_storage_path(self) -> None:
        """inspect receipts without --storage-path exits cleanly."""
        with pytest.raises(SystemExit):
            main(["inspect", "receipts", "--event", "nonexistent"])

    def test_inspect_event_missing_storage_path(self) -> None:
        """inspect event without --storage-path exits cleanly."""
        with pytest.raises(SystemExit):
            main(["inspect", "event", "nonexistent", "--timeline"])

    def test_replay_rejects_storage_path(self, tmp_path: Path) -> None:
        """replay does not accept --storage-path; argparse rejects it."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit):
                main(
                    [
                        "replay",
                        "--config",
                        smoke_config_path(),
                        "--mode",
                        "dry_run",
                        "--event",
                        "evt-1",
                        "--storage-path",
                        str(tmp_path / "test.db"),
                    ]
                )
        assert "unrecognized arguments" in stderr_buf.getvalue()
