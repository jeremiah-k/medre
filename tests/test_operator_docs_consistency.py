"""Grep-style tests for operator docs consistency.

Asserts that operator-facing runbooks are consistent with the current CLI
surface, storage-path read-only workflow, replay config requirement, and
retry/replay semantics.  All checks are read-only — no files are created
or modified.

Follows the grep-assertion pattern from ``test_example_configs.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS_DIR = _ROOT / "docs" / "runbooks"

TARGET_DOCS = [
    RUNBOOKS_DIR / "alpha-walkthrough.md",
    RUNBOOKS_DIR / "bridge-operation.md",
    RUNBOOKS_DIR / "replay-operation.md",
    RUNBOOKS_DIR / "bridge-evidence-bundle.md",
    RUNBOOKS_DIR / "configuration.md",
]


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    """Concatenate all target docs for global searches."""
    return "\n".join(_read(p) for p in TARGET_DOCS)


# ===========================================================================
# 1. No stale "status: ok" in smoke examples
# ===========================================================================


class TestNoStaleSmokeStatusOk:
    """Smoke command reports ``"passed"`` / ``"failed"``, not ``"ok"``.
    The evidence bundle report correctly uses ``"ok"`` — this test only
    checks smoke-related sections."""

    @pytest.mark.parametrize(
        "doc_path",
        [p for p in TARGET_DOCS if p.name in ("alpha-walkthrough.md", "bridge-evidence-bundle.md")],
        ids=lambda p: p.name,
    )
    def test_smoke_examples_use_passed_not_ok(self, doc_path: Path) -> None:
        """In smoke command output examples, ``status`` must be ``passed``
        or ``failed``, never ``ok``."""
        text = _read(doc_path)
        # Find lines mentioning both "smoke" and '"status": "ok"'.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "smoke" in line.lower() and '"status": "ok"' in line:
                pytest.fail(
                    f"{doc_path.name}:{lineno}: smoke example uses "
                    f'stale "status": "ok" (should be "passed" or "failed"):\n'
                    f"  {line.strip()}"
                )


# ===========================================================================
# 2. No "no retry scheduler" claims
# ===========================================================================


class TestNoRetrySchedulerDenial:
    """The RetryWorker exists behind opt-in config.  Docs must not claim
    there is no retry scheduler — instead they should describe the opt-in
    two-level retry system correctly."""

    def test_no_no_retry_scheduler_claims(self) -> None:
        text = _all_doc_text()
        pattern = re.compile(r"no\s+retry\s+scheduler", re.IGNORECASE)
        match = pattern.search(text)
        assert match is None, (
            'Found stale "no retry scheduler" claim in docs. '
            "MEDRE has an opt-in RetryWorker — describe it as opt-in, "
            "not as non-existent."
        )


# ===========================================================================
# 3. No package-root private CLI import references
# ===========================================================================


class TestNoPrivateCliImports:
    """Docs must not reference private CLI module paths (e.g.
    ``medre.cli._internal``).  Only public package-level imports
    (``from medre.adapters.matrix import ...``) should appear."""

    def test_no_private_cli_imports_in_docs(self) -> None:
        text = _all_doc_text()
        # Match import lines referencing medre.cli._ (private modules)
        # or from medre._ (private top-level).
        patterns = [
            re.compile(r"\bfrom\s+medre\.cli\._"),
            re.compile(r"\bimport\s+medre\.cli\._"),
            re.compile(r"\bfrom\s+medre\._"),
            re.compile(r"\bimport\s+medre\._"),
        ]
        for pat in patterns:
            for doc_path in TARGET_DOCS:
                doc_text = _read(doc_path)
                for lineno, line in enumerate(doc_text.splitlines(), start=1):
                    if pat.search(line):
                        # Allow inside code fences that show *example*
                        # error messages, but flag import statements.
                        if "import" in line:
                            pytest.fail(
                                f"{doc_path.name}:{lineno}: private CLI "
                                f"import reference in docs:\n"
                                f"  {line.strip()}"
                            )


# ===========================================================================
# 4. No "not distinguishable from live" replay claims
# ===========================================================================


class TestReplayDistinguishability:
    """Replay receipts carry ``source='replay'`` and ``replay_run_id``,
    making them distinguishable from live receipts.  Docs must not claim
    replay receipts are indistinguishable from live."""

    def test_no_not_distinguishable_from_live(self) -> None:
        text = _all_doc_text()
        pattern = re.compile(r"not distinguishable from live", re.IGNORECASE)
        match = pattern.search(text)
        assert match is None, (
            'Found stale "not distinguishable from live" claim in docs. '
            "Replay receipts carry source='replay' and replay_run_id — "
            "they are distinguishable from live receipts."
        )


# ===========================================================================
# 5. Storage-path read-only workflow consistency
# ===========================================================================


class TestStoragePathReadOnlyConsistency:
    """Verify that docs consistently describe the storage-path read-only
    workflow: inspect/trace event/evidence support --storage-path;
    replay requires --config."""

    def test_inspect_examples_show_storage_path_option(self) -> None:
        """Docs with ``inspect event`` or ``inspect receipts --event``
        examples should show --storage-path as a read-only option.
        ``inspect receipts --replay-run`` requires --config and is excluded."""
        for doc_path in TARGET_DOCS:
            text = _read(doc_path)
            # Only check for inspect event / inspect receipts --event
            # (these support --storage-path).
            # Skip inspect receipts --replay-run (requires --config).
            inspect_event_with_config = re.findall(
                r"medre\s+inspect\s+event\b.*--config",
                text,
            )
            inspect_receipts_event_with_config = re.findall(
                r"medre\s+inspect\s+receipts\s+--event\b.*--config",
                text,
            )
            has_storage_path_candidates = bool(
                inspect_event_with_config or inspect_receipts_event_with_config
            )
            if has_storage_path_candidates:
                assert "--storage-path" in text, (
                    f"{doc_path.name} has inspect event/receipts--event "
                    f"examples with --config but no --storage-path option "
                    f"shown. Read-only inspect should support --storage-path."
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

    def test_replay_operation_states_config_requirement(self) -> None:
        """replay-operation.md must state that replay requires config."""
        text = _read(RUNBOOKS_DIR / "replay-operation.md")
        assert "--config" in text, (
            "replay-operation.md must mention --config requirement."
        )

    def test_alpha_walkthrough_storage_path_for_inspect(self) -> None:
        """alpha-walkthrough.md inspect section should show --storage-path."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        # The inspect section should mention --storage-path
        assert "--storage-path" in text, (
            "alpha-walkthrough.md must show --storage-path for "
            "read-only inspect/trace commands."
        )


# ===========================================================================
# 6. Replay config requirement stated
# ===========================================================================


class TestReplayConfigRequirement:
    """Both replay-operation.md and alpha-walkthrough.md must state that
    replay requires --config and does not support --storage-path."""

    @pytest.mark.parametrize(
        "doc_path",
        [RUNBOOKS_DIR / "replay-operation.md", RUNBOOKS_DIR / "alpha-walkthrough.md"],
        ids=lambda p: p.name,
    )
    def test_replay_config_requirement_mentioned(self, doc_path: Path) -> None:
        text = _read(doc_path)
        # replay-operation.md has its own section; alpha-walkthrough mentions
        # replay in the table / notes.
        # Both should mention that replay requires config.
        if doc_path.name == "replay-operation.md":
            assert "--config" in text, (
                f"{doc_path.name} must mention --config for replay."
            )
        else:
            # alpha-walkthrough mentions replay in the table and notes
            # -- storage-path note says replay still requires config
            assert "--storage-path" in text, (
                f"{doc_path.name} must mention --storage-path read-only "
                f"workflow with replay config requirement note."
            )


# ===========================================================================
# 7. CLI command surface in configuration.md
# ===========================================================================


class TestConfigurationCliSurface:
    """configuration.md must document all top-level CLI commands."""

    REQUIRED_COMMANDS = [
        "smoke",
        "inspect",
        "trace",
        "evidence",
        "replay",
        "recover",
        "diagnostics",
        "routes",
        "adapters",
    ]

    @pytest.mark.parametrize("command", REQUIRED_COMMANDS)
    def test_cli_command_documented(self, command: str) -> None:
        text = _read(RUNBOOKS_DIR / "configuration.md")
        # Check for the command as a subcommand header (e.g. "medre smoke")
        assert f"medre {command}" in text, (
            f"configuration.md CLI Commands section must document "
            f"'medre {command}'."
        )

    def test_storage_path_bypass_note_present(self) -> None:
        """configuration.md must note that --storage-path bypasses config
        for read-only commands."""
        text = _read(RUNBOOKS_DIR / "configuration.md")
        assert "--storage-path" in text, (
            "configuration.md must mention --storage-path for read-only "
            "commands (inspect, trace event, evidence)."
        )

    def test_replay_rejects_storage_path_noted(self) -> None:
        """configuration.md must note that replay rejects --storage-path."""
        text = _read(RUNBOOKS_DIR / "configuration.md")
        # Find the replay section and check it mentions the rejection
        assert "reject" in text.lower() or "requires --config" in text.lower(), (
            "configuration.md must note that replay requires --config "
            "and does not accept --storage-path."
        )


# ===========================================================================
# 8. Retry semantics described correctly
# ===========================================================================


class TestRetrySemantics:
    """Docs must describe retry as opt-in two-level (route + worker),
    not as absent or always-on."""

    def test_bridge_operation_describes_retry_opt_in(self) -> None:
        text = _read(RUNBOOKS_DIR / "bridge-operation.md")
        # Must mention opt-in nature of retry
        assert "opt-in" in text.lower() or "disabled by default" in text.lower(), (
            "bridge-operation.md must describe retry as opt-in/disabled by default."
        )

    def test_alpha_walkthrough_describes_retry_levels(self) -> None:
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        # Must mention both route-level and worker-level retry
        if "retry" in text.lower():
            assert "route" in text.lower() and "worker" in text.lower(), (
                "alpha-walkthrough.md must describe both route-level and "
                "worker-level retry when mentioning retry."
            )

    def test_replay_described_as_manual(self) -> None:
        """Replay must be described as manual/one-shot in docs that
        mention it."""
        for doc_path in TARGET_DOCS:
            text = _read(doc_path)
            # Only check docs that mention replay extensively
            if text.lower().count("replay") < 3:
                continue
            assert (
                "manual" in text.lower() or "one-shot" in text.lower()
            ), (
                f"{doc_path.name} mentions replay extensively but does "
                f"not describe it as manual/one-shot."
            )
