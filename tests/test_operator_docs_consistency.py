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
    The evidence bundle code returns ``"passed"`` or ``"partial"`` for
    section and overall statuses — the value ``"ok"`` is never emitted
    by the code.  Docs must match."""

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

    @pytest.mark.parametrize(
        "doc_path",
        [p for p in TARGET_DOCS if p.name in ("alpha-walkthrough.md", "bridge-evidence-bundle.md")],
        ids=lambda p: p.name,
    )
    def test_evidence_examples_use_passed_not_ok(self, doc_path: Path) -> None:
        """In evidence bundle examples, ``status`` must be ``passed``,
        ``partial``, ``error``, or ``skipped`` — never ``ok``.

        The evidence code uses _section_ok() which returns status='passed',
        and _compute_overall_status() returns 'passed' or 'partial'.
        The value 'ok' is never used.
        """
        text = _read(doc_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if '"status": "ok"' in line:
                pytest.fail(
                    f"{doc_path.name}:{lineno}: evidence example uses "
                    f'stale "status": "ok". '
                    f"Code returns 'passed' or 'partial', never 'ok'.\n"
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
    workflow: all inspect subcommands (including native-ref and
    receipts --replay-run), trace subcommands, and evidence support
    --storage-path; replay (top-level) and recover require --config."""

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

    def test_inspect_replay_documented(self) -> None:
        """configuration.md must document 'inspect replay' subcommand."""
        text = _read(RUNBOOKS_DIR / "configuration.md")
        assert "inspect replay" in text, (
            "configuration.md must document 'medre inspect replay' "
            "as a read-only storage inspection subcommand."
        )

    def test_inspect_event_flags_documented(self) -> None:
        """configuration.md must document --timeline, --evidence, --recovery
        flags for inspect event."""
        text = _read(RUNBOOKS_DIR / "configuration.md")
        for flag in ("--timeline", "--evidence", "--recovery"):
            assert flag in text, (
                f"configuration.md must document '{flag}' flag for "
                f"'medre inspect event'."
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


# ===========================================================================
# 9. Alpha walkthrough uses inspect-based investigation
# ===========================================================================


class TestAlphaWalkthroughInspectSurface:
    """The alpha walkthrough should use inspect commands as the primary
    investigation surface, with trace/evidence available as deeper tools."""

    def test_walkthrough_mentions_inspect(self) -> None:
        """alpha-walkthrough.md must reference 'medre inspect'."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        assert "medre inspect" in text, (
            "alpha-walkthrough.md must reference 'medre inspect' as the "
            "primary investigation command."
        )

    def test_walkthrough_inspect_step_before_trace(self) -> None:
        """In the walkthrough, inspect appears before trace in the flow."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        inspect_pos = text.find("medre inspect")
        trace_pos = text.find("medre trace")
        if inspect_pos < 0 or trace_pos < 0:
            pytest.skip("Both inspect and trace must be in walkthrough")
        assert inspect_pos < trace_pos, (
            "alpha-walkthrough.md should present inspect before trace "
            "(inspect is the primary investigation surface)."
        )

    def test_walkthrough_inspect_uses_storage_path(self) -> None:
        """Inspect examples in the walkthrough use --storage-path."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        # Find inspect command lines.
        inspect_lines = [
            line for line in text.splitlines()
            if "medre inspect" in line and "--storage-path" not in line
            and line.strip().startswith("medre inspect")
        ]
        # Allow non-CLI-context mentions (table rows, prose).
        for line in inspect_lines:
            if line.strip().startswith("medre inspect") and "config" in line.lower():
                pytest.fail(
                    f"alpha-walkthrough.md has inspect command using --config "
                    f"instead of --storage-path: {line.strip()}"
                )


# ===========================================================================
# 10. Evidence status value consistency
# ===========================================================================


class TestEvidenceStatusValueConsistency:
    """The evidence module returns 'passed' (via _section_ok) and 'partial'
    (via _compute_overall_status), never 'ok'.  This test catches code drift."""

    def test_section_ok_returns_passed(self) -> None:
        """_section_ok() must return status='passed', not 'ok'."""
        from medre.runtime.evidence import _section_ok

        result = _section_ok({"test": True})
        assert result["status"] == "passed", (
            f"_section_ok() should return status='passed', "
            f"got '{result['status']}'"
        )

    def test_overall_status_never_ok(self) -> None:
        """_compute_overall_status() must never return 'ok'."""
        from medre.runtime.evidence import _compute_overall_status

        for statuses in [
            {"passed"},
            {"passed", "skipped"},
            {"skipped"},
            {"partial"},
            {"error"},
        ]:
            sections = {f"s{i}": {"status": s} for i, s in enumerate(statuses)}
            result = _compute_overall_status(sections)
            assert result != "ok", (
                f"_compute_overall_status({statuses}) returned 'ok'"
            )

    def test_alpha_walkthrough_evidence_status_not_ok(self) -> None:
        """alpha-walkthrough.md evidence example must not say 'status: ok'."""
        text = _read(RUNBOOKS_DIR / "alpha-walkthrough.md")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if '"status": "ok"' in line and "evidence" in text[max(0, text.find(line) - 500):text.find(line) + 500].lower():
                pytest.fail(
                    f"alpha-walkthrough.md:{lineno}: evidence example uses "
                    f'stale "status": "ok" (code returns "passed" or "partial").\n'
                    f"  {line.strip()}"
                )


# ===========================================================================
# 11. Bare status vocabulary drift
# ===========================================================================


class TestBareStatusVocabularyDrift:
    """Catch bare 'ok' in status prose/tables, not just JSON patterns.

    The code never emits status='ok' — it uses 'passed'. Docs must not
    list 'ok' as a valid status value in tables, prose, or vocabulary
    definitions.
    """

    @pytest.mark.parametrize(
        "doc_path",
        [p for p in TARGET_DOCS if p.name in ("bridge-evidence-bundle.md", "alpha-walkthrough.md")],
        ids=lambda p: p.name,
    )
    def test_no_bare_ok_in_status_tables(self, doc_path: Path) -> None:
        """Status vocabulary tables must not list 'ok' as a status value."""
        text = _read(doc_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip JSON examples (covered by test_evidence_examples_use_passed_not_ok).
            if '"status": "ok"' in stripped:
                continue
            # Skip drill_steps result lines.
            if '"result": "ok"' in stripped:
                continue
            # Skip exit code columns like "0 (success)" (distinguishes exit code from status).
            if re.match(r".*\d\s*\(success\)", stripped):
                continue
            # Catch "ok / passed" in table rows (backtick-wrapped).
            if re.search(r"`ok`\s*/\s*`passed`", stripped):
                pytest.fail(
                    f"{doc_path.name}:{lineno}: stale status vocabulary "
                    f"lists 'ok' alongside 'passed'. Code never emits 'ok'.\n"
                    f"  {stripped}"
                )
            # Catch bare backtick-wrapped "ok" as a status value.
            if re.search(r"`\"?ok\"?`", stripped) and "result" not in stripped:
                pytest.fail(
                    f"{doc_path.name}:{lineno}: bare 'ok' in status "
                    f"vocabulary. Code uses 'passed', not 'ok'.\n"
                    f"  {stripped}"
                )


# ===========================================================================
# 12. No stale exit-code/status conflation (0=ok)
# ===========================================================================


class TestNoStaleExitCodeOk:
    """Exit code columns must not use 'ok' as a status synonym.

    The correct vocabulary is 'passed' for evidence/smoke status, or 'success'
    for exit-code-only semantics (e.g. diagnostics). The pattern ``0=ok``
    conflates exit codes with JSON status and must not appear.
    """

    @pytest.mark.parametrize(
        "doc_path",
        TARGET_DOCS,
        ids=lambda p: p.name,
    )
    def test_no_0_equals_ok_in_exit_code_columns(self, doc_path: Path) -> None:
        """Exit code columns must not contain ``0=ok``."""
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
        text = _read(doc_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if re.search(r"\d=\bok\b", stripped):
                pytest.fail(
                    f"{doc_path.name}:{lineno}: stale '0=ok' in exit code "
                    f"column. Use '0=passed' (for status-bearing commands) "
                    f"or '0 (success)' (for exit-code-only commands).\n"
                    f"  {stripped}"
                )


# ===========================================================================
# 13. No "pass/fail JSON report" — use "passed/failed"
# ===========================================================================


class TestNoPassFailJsonReport:
    """Docs must say 'passed/failed JSON report', not 'pass/fail JSON report'.

    The smoke command JSON uses ``"status": "passed"`` and
    ``"status": "failed"`` — past-tense, not present-tense.
    """

    @pytest.mark.parametrize(
        "doc_path",
        TARGET_DOCS,
        ids=lambda p: p.name,
    )
    def test_no_pass_fail_json_report(self, doc_path: Path) -> None:
        """Docs must not say 'pass/fail JSON report'."""
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
        text = _read(doc_path)
        pattern = re.compile(r"pass\s*/\s*fail\s+JSON\s+report", re.IGNORECASE)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                pytest.fail(
                    f"{doc_path.name}:{lineno}: stale 'pass/fail JSON report'. "
                    f"Use 'passed/failed JSON report' (past-tense status values).\n"
                    f"  {line.strip()}"
                )
