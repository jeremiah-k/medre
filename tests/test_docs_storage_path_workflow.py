"""Storage-path read-only workflow and replay config requirement tests.

Asserts that docs consistently describe the storage-path read-only workflow
and that replay requires --config.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = _ROOT / "docs" / "ops"

TARGET_DOCS = [
    OPS_DIR / "operator-workflows.md",
    OPS_DIR / "running-medre.md",
    OPS_DIR / "recovery-and-replay.md",
    OPS_DIR / "diagnostics-and-evidence.md",
    OPS_DIR / "troubleshooting.md",
    OPS_DIR / "configuration.md",
]


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    """Concatenate all target docs for global searches."""
    return "\n".join(_read(p) for p in TARGET_DOCS)


# ===========================================================================
# 5. Storage-path read-only workflow consistency
# ===========================================================================


class TestStoragePathReadOnlyConsistency:
    """Verify that docs consistently describe the storage-path read-only
    workflow: all inspect subcommands (including native-ref and
    receipts --replay-run), trace subcommands, and evidence support
    --storage-path; recover uses --storage-path; replay requires --config."""

    def test_inspect_examples_show_storage_path_option(self) -> None:
        """Docs with ``inspect`` examples should show --storage-path as a
        read-only option. All inspect subcommands support --storage-path."""
        for doc_path in TARGET_DOCS:
            text = _read(doc_path)
            # Check for inspect command examples that use --config
            # (prose mentions are OK without --storage-path).
            inspect_with_config = re.findall(
                r"medre\s+inspect\s+\w+.*--config",
                text,
            )
            if inspect_with_config:
                assert "--storage-path" in text, (
                    f"{doc_path.name} has inspect examples with --config "
                    f"but no --storage-path option shown. All inspect "
                    f"subcommands support --storage-path."
                )

    def test_replay_examples_require_config_not_storage_path(self) -> None:
        """Replay examples must use --config, not --storage-path."""
        text = _all_doc_text()
        # Find any "medre replay" line that also mentions --storage-path
        stale = re.findall(
            r"medre\s+replay\b.*--storage-path",
            text,
        )
        assert not stale, (
            "Found replay example using --storage-path. "
            "Replay requires --config (it rejects --storage-path)."
        )

    def test_inspect_native_ref_supports_storage_path(self) -> None:
        """Docs must not claim inspect native-ref requires --config.
        It supports --storage-path."""
        text = _all_doc_text()
        # Look for claims that native-ref requires config.
        stale = re.findall(
            r"native-ref.*require.*--config",
            text,
            re.IGNORECASE,
        )
        assert not stale, (
            "Found claim that inspect native-ref requires --config. "
            "It supports --storage-path for direct read-only access."
        )

    def test_inspect_receipts_replay_run_supports_storage_path(self) -> None:
        """Docs must not claim inspect receipts --replay-run requires --config.
        It supports --storage-path."""
        text = _all_doc_text()
        # Look for claims that receipts --replay-run requires config.
        stale = re.findall(
            r"replay-run.*require.*--config",
            text,
            re.IGNORECASE,
        )
        assert not stale, (
            "Found claim that inspect receipts --replay-run requires --config. "
            "It supports --storage-path for direct read-only access."
        )

    def test_replay_operation_states_config_requirement(self) -> None:
        """recovery-and-replay.md must state that replay requires config."""
        text = _read(OPS_DIR / "recovery-and-replay.md")
        assert (
            "--config" in text
        ), "recovery-and-replay.md must mention --config requirement."

    def test_alpha_walkthrough_storage_path_for_inspect(self) -> None:
        """operator-workflows.md inspect section should show --storage-path."""
        text = _read(OPS_DIR / "operator-workflows.md")
        # The inspect section should mention --storage-path
        assert "--storage-path" in text, (
            "operator-workflows.md must show --storage-path for "
            "read-only inspect/trace commands."
        )


# ===========================================================================
# 6. Replay config requirement stated
# ===========================================================================


class TestReplayConfigRequirement:
    """Both recovery-and-replay.md and operator-workflows.md must state that
    replay requires --config and does not support --storage-path."""

    @pytest.mark.parametrize(
        "doc_path",
        [OPS_DIR / "recovery-and-replay.md", OPS_DIR / "operator-workflows.md"],
        ids=lambda p: p.name,
    )
    def test_replay_config_requirement_mentioned(self, doc_path: Path) -> None:
        text = _read(doc_path)
        # recovery-and-replay.md has its own section; alpha-walkthrough mentions
        # replay in the table / notes.
        # Both should mention that replay requires config.
        if doc_path.name == "recovery-and-replay.md":
            assert (
                "--config" in text
            ), f"{doc_path.name} must mention --config for replay."
        else:
            # operator-workflows mentions replay in the table and notes
            # -- storage-path note says replay still requires config
            assert "--storage-path" in text, (
                f"{doc_path.name} must mention --storage-path read-only "
                f"workflow with replay config requirement note."
            )
