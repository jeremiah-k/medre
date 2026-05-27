"""Status vocabulary consistency tests.

Asserts that operator-facing docs use the correct status values
("passed"/"failed", never "ok") and exit-code vocabulary.
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
    OPS_DIR / "recovery-and-replay.md",
    OPS_DIR / "diagnostics-and-evidence.md",
    OPS_DIR / "operator-workflows.md",
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
# 1. No stale "status: ok" in smoke examples
# ===========================================================================


class TestNoStaleSmokeStatusOk:
    """Smoke command reports ``"passed"`` / ``"failed"``, not ``"ok"``.
    The evidence bundle code returns ``"passed"`` or ``"partial"`` for
    section and overall statuses — the value ``"ok"`` is never emitted
    by the code.  Docs must match."""

    @pytest.mark.parametrize(
        "doc_path",
        [
            p
            for p in TARGET_DOCS
            if p.name in ("operator-workflows.md", "diagnostics-and-evidence.md")
        ],
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
        [
            p
            for p in TARGET_DOCS
            if p.name in ("operator-workflows.md", "diagnostics-and-evidence.md")
        ],
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
# 10. Evidence status value consistency
# ===========================================================================


class TestEvidenceStatusValueConsistency:
    """The evidence module returns 'passed' (via _section_ok) and 'partial'
    (via _compute_overall_status), never 'ok'.  This test catches code drift."""

    def test_section_ok_returns_passed(self) -> None:
        """_section_ok() must return status='passed', not 'ok'."""
        from medre.runtime.evidence._helpers import _section_ok

        result = _section_ok({"test": True})
        assert result["status"] == "passed", (
            f"_section_ok() should return status='passed', " f"got '{result['status']}'"
        )

    def test_overall_status_never_ok(self) -> None:
        """_compute_overall_status() must never return 'ok'."""
        from medre.runtime.evidence._helpers import _compute_overall_status

        for statuses in [
            {"passed"},
            {"passed", "skipped"},
            {"skipped"},
            {"partial"},
            {"error"},
        ]:
            sections = {f"s{i}": {"status": s} for i, s in enumerate(statuses)}
            result = _compute_overall_status(sections)
            assert result != "ok", f"_compute_overall_status({statuses}) returned 'ok'"

    def test_alpha_walkthrough_evidence_status_not_ok(self) -> None:
        """alpha-walkthrough.md evidence example must not say 'status: ok'."""
        text = _read(OPS_DIR / "operator-workflows.md")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if (
                '"status": "ok"' in line
                and "evidence"
                in text[max(0, text.find(line) - 500) : text.find(line) + 500].lower()
            ):
                pytest.fail(
                    f"operator-workflows.md:{lineno}: evidence example uses "
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
        [
            p
            for p in TARGET_DOCS
            if p.name in ("diagnostics-and-evidence.md", "operator-workflows.md")
        ],
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
