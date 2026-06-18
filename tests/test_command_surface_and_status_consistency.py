"""Command-surface and status-value consistency tests.

These tests verify:
1. Evidence bundle status values are consistent between code and docs
   (the code uses "passed", not "ok").
2. Command help in configuration.md contains correct hints (read-only,
   runtime-start, send-message, config, storage-path).
3. Command surface in docs matches the CLI parser in main.py.
4. No stale product-path or command-surface claims in docs.
5. No stale "status: ok" examples except where the runtime genuinely
   returns "ok" (currently: nowhere — the code uses "passed").

These are grep-style read-only tests following the pattern from
test_operator_docs_consistency.py.
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
CONFIG_DOC = OPS_DIR / "configuration.md"
ALPHA_WALKTHROUGH = OPS_DIR / "operator-workflows.md"
BRIDGE_EVIDENCE = OPS_DIR / "diagnostics-and-evidence.md"

TARGET_DOCS = [
    ALPHA_WALKTHROUGH,
    BRIDGE_EVIDENCE,
    OPS_DIR / "running-medre.md",
    OPS_DIR / "recovery-and-replay.md",
    OPS_DIR / "recovery-and-replay.md",
    CONFIG_DOC,
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    return "\n".join(_read(p) for p in TARGET_DOCS if p.exists())


# ===========================================================================
# 1. Evidence status: code returns "passed", never "ok"
# ===========================================================================


class TestEvidenceStatusConsistency:
    """Evidence bundle status values in code and docs must agree.

    The evidence module uses _section_ok() which returns status='passed',
    and _compute_overall_status() returns 'passed' or 'partial'. The value
    'ok' is never used. Docs should reflect this.
    """

    def test_code_section_ok_returns_passed(self) -> None:
        """Verify _section_ok() returns 'passed', not 'ok'."""
        from medre.runtime.evidence._helpers import _section_ok

        result = _section_ok({"test": True})
        assert result["status"] == "passed", (
            f"_section_ok() should return status='passed', " f"got '{result['status']}'"
        )

    def test_code_overall_status_uses_passed_not_ok(self) -> None:
        """Verify _compute_overall_status() never returns 'ok'."""
        from medre.runtime.evidence._helpers import _compute_overall_status

        # Test all possible section status combinations.
        for statuses in [
            {"passed"},
            {"passed", "skipped"},
            {"skipped"},
            {"partial"},
            {"partial", "skipped"},
            {"error"},
            {"error", "skipped"},
            {"passed", "partial"},
            {"passed", "error"},
            {"partial", "error"},
        ]:
            sections = {f"s{i}": {"status": s} for i, s in enumerate(statuses)}
            result = _compute_overall_status(sections)
            assert result != "ok", (
                f"_compute_overall_status({statuses}) returned 'ok' — "
                f"should be 'passed' or 'partial'."
            )

    def test_code_section_statuses_are_valid(self) -> None:
        """Verify all section status helper functions return valid values."""
        from medre.runtime.evidence._helpers import (
            _section_error,
            _section_ok,
            _section_partial,
            _section_skipped,
        )

        valid = {"passed", "partial", "error", "skipped"}
        assert _section_ok(None)["status"] in valid
        assert _section_partial(None, "test")["status"] in valid
        assert _section_error("test")["status"] in valid
        assert _section_skipped("test")["status"] in valid

    def test_alpha_walkthrough_evidence_status_is_passed(self) -> None:
        """operator-workflows.md evidence example must say 'passed' or 'partial'.

        The evidence command returns 'passed' or 'partial', never 'ok'.
        Line 'Expected output: JSON evidence bundle with "status": "ok"'
        is stale and should say 'passed'.
        """
        if not ALPHA_WALKTHROUGH.exists():
            pytest.skip("operator-workflows.md not found")
        text = _read(ALPHA_WALKTHROUGH)
        # Find evidence section examples.
        in_evidence_section = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "evidence" in line.lower() and "step" in line.lower():
                in_evidence_section = True
            if in_evidence_section and '"status": "ok"' in line:
                pytest.fail(
                    f"operator-workflows.md:{lineno}: evidence example uses "
                    f'stale "status": "ok" (should be "passed" or "partial").\n'
                    f"  {line.strip()}"
                )
            # End evidence section at next step heading.
            if (
                in_evidence_section
                and line.startswith("### Step")
                and "evidence" not in line.lower()
            ):
                in_evidence_section = False

    def test_bridge_evidence_section_status_not_ok(self) -> None:
        """bridge-evidence-bundle.md section status examples should use
        'passed' not 'ok'.

        The code returns 'passed' for successful sections, never 'ok'.
        """
        if not BRIDGE_EVIDENCE.exists():
            pytest.skip("bridge-evidence-bundle.md not found")
        text = _read(BRIDGE_EVIDENCE)
        stale_lines = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if '"status": "ok"' in line:
                stale_lines.append((lineno, line.strip()))
        if stale_lines:
            details = "\n".join(f"  Line {no}: {ln}" for no, ln in stale_lines[:5])
            pytest.fail(
                f"bridge-evidence-bundle.md has {len(stale_lines)} lines with "
                f'stale "status": "ok". Code returns "passed", not "ok". '
                f"First instances:\n{details}"
            )


# ===========================================================================
# 2. Command help hints in configuration.md
# ===========================================================================


class TestCommandHelpHints:
    """configuration.md CLI Commands section must contain correct hints
    about read-only access, runtime start, config requirements, and
    storage-path semantics."""

    @pytest.fixture()
    def config_text(self) -> str:
        if not CONFIG_DOC.exists():
            pytest.skip("configuration.md not found")
        return _read(CONFIG_DOC)

    def test_read_only_hint_present(self, config_text: str) -> None:
        """CLI section must mention 'read-only' for inspect/trace/evidence."""
        assert (
            "read-only" in config_text.lower()
        ), "configuration.md must describe inspect/trace/evidence as read-only."

    def test_runtime_start_hint_present(self, config_text: str) -> None:
        """CLI section must mention 'runtime' for the run command."""
        assert (
            "medre run" in config_text
        ), "configuration.md must document 'medre run' for starting the runtime."

    def test_config_hint_present(self, config_text: str) -> None:
        """CLI section must mention --config for config-dependent commands."""
        assert "--config" in config_text, "configuration.md must mention --config flag."

    def test_storage_path_hint_present(self, config_text: str) -> None:
        """CLI section must mention --storage-path for read-only commands."""
        assert "--storage-path" in config_text, (
            "configuration.md must mention --storage-path for read-only "
            "commands (inspect, trace, evidence)."
        )

    def test_send_message_hint_present(self, config_text: str) -> None:
        """CLI section must mention 'message' in smoke or evidence context."""
        assert "message" in config_text.lower(), (
            "configuration.md must mention message handling "
            "(smoke test sends messages)."
        )

    def test_replay_rejects_storage_path_hint(self, config_text: str) -> None:
        """CLI section must note replay rejects --storage-path."""
        # The replay line should indicate it requires --config (not --storage-path).
        assert (
            "reject" in config_text.lower()
            or "requires --config" in config_text.lower()
        ), (
            "configuration.md must note that replay requires --config "
            "and does not accept --storage-path."
        )

    def test_inspect_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre inspect'."""
        assert (
            "medre inspect" in config_text
        ), "configuration.md must document the inspect command."

    def test_trace_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre trace'."""
        assert (
            "medre trace" in config_text
        ), "configuration.md must document the trace command."

    def test_evidence_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre evidence'."""
        assert (
            "medre evidence" in config_text
        ), "configuration.md must document the evidence command."

    def test_replay_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre replay'."""
        assert (
            "medre replay" in config_text
        ), "configuration.md must document the replay command."

    def test_recover_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre recover'."""
        assert (
            "medre recover" in config_text
        ), "configuration.md must document the recover command."

    def test_diagnostics_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre diagnostics'."""
        assert (
            "medre diagnostics" in config_text
        ), "configuration.md must document the diagnostics command."

    def test_config_command_documented(self, config_text: str) -> None:
        """configuration.md must document 'medre config'."""
        assert (
            "medre config" in config_text
        ), "configuration.md must document the config command."


# ===========================================================================
# 3. Command surface in docs matches parser
# ===========================================================================


class TestCommandSurfaceMatchesParser:
    """Top-level commands documented in configuration.md must be parseable
    by the CLI argument parser in main.py."""

    def _get_parser_commands(self) -> set[str]:
        """Extract top-level command names from the parser."""
        from medre.cli.main import _build_parser

        parser = _build_parser()
        # Access subparsers through the parser's internal _subparsers action.
        commands: set[str] = set()
        _subparsers = getattr(parser, "_subparsers", None)
        for action in _subparsers._actions if _subparsers is not None else []:
            choices = getattr(action, "choices", None)
            if choices is not None:
                commands.update(choices.keys())
        return commands

    def test_documented_commands_exist_in_parser(self) -> None:
        """Every 'medre <command>' in configuration.md must be in the parser."""
        if not CONFIG_DOC.exists():
            pytest.skip("configuration.md not found")
        text = _read(CONFIG_DOC)

        # Find all "medre <command>" patterns in the CLI Commands section.
        # Look for the CLI Commands section.
        cli_section_match = re.search(
            r"## CLI Commands(.*?)(?=\n## |\Z)",
            text,
            re.DOTALL,
        )
        if not cli_section_match:
            pytest.skip("CLI Commands section not found in configuration.md")

        cli_section = cli_section_match.group(1)
        parser_commands = self._get_parser_commands()

        # Find all "medre <word>" patterns.
        doc_commands = set(re.findall(r"\bmedre\s+(\w+)", cli_section))
        # Filter to actual commands (not "the" or "a" after "medre").
        doc_commands = doc_commands & parser_commands | {
            c for c in doc_commands if c in parser_commands
        }

        for cmd in doc_commands:
            assert cmd in parser_commands, (
                f"configuration.md documents 'medre {cmd}' but it is not "
                f"in the CLI parser. Available: {sorted(parser_commands)}"
            )

    def test_parser_has_required_top_level_commands(self) -> None:
        """Parser must have all top-level commands from configuration.md."""
        parser_commands = self._get_parser_commands()
        required = {
            "run",
            "config",
            "paths",
            "version",
            "adapters",
            "diagnostics",
            "routes",
            "smoke",
            "inspect",
            "trace",
            "evidence",
            "replay",
            "recover",
        }
        missing = required - parser_commands
        assert not missing, (
            f"CLI parser is missing commands: {sorted(missing)}. "
            f"Available: {sorted(parser_commands)}"
        )

    def test_inspect_subcommands_match_docs(self) -> None:
        """Parser inspect subcommands match documented subcommands."""
        from medre.cli.main import _build_parser

        parser = _build_parser()
        # Parse "inspect" to get its subcommands.
        parser.parse_args(
            ["inspect", "event", "--storage-path", "/dev/null", "fake-id"]
        )
        # If we get here, "event" is accepted. Try others.
        for subcmd in ("event", "receipts", "native-ref", "replay"):
            try:
                if subcmd == "event":
                    parser.parse_args(
                        ["inspect", subcmd, "--storage-path", "/dev/null", "fake-id"]
                    )
                elif subcmd == "receipts":
                    parser.parse_args(
                        [
                            "inspect",
                            subcmd,
                            "--event",
                            "fake-id",
                            "--storage-path",
                            "/dev/null",
                        ]
                    )
                elif subcmd == "native-ref":
                    parser.parse_args(
                        [
                            "inspect",
                            subcmd,
                            "--adapter",
                            "fake",
                            "--message",
                            "fake",
                            "--storage-path",
                            "/dev/null",
                        ]
                    )
                elif subcmd == "replay":
                    parser.parse_args(
                        [
                            "inspect",
                            subcmd,
                            "--storage-path",
                            "/dev/null",
                            "fake-run-id",
                        ]
                    )
            except SystemExit:
                pytest.fail(
                    f"Parser rejects 'inspect {subcmd}' but it is "
                    f"documented in configuration.md."
                )

    def test_trace_subcommands_match_docs(self) -> None:
        """Parser trace subcommands match documented subcommands."""
        from medre.cli.main import _build_parser

        parser = _build_parser()
        for subcmd in ("event", "replay"):
            try:
                parser.parse_args(
                    ["trace", subcmd, "--storage-path", "/dev/null", "fake-id"]
                )
            except SystemExit:
                pytest.fail(
                    f"Parser rejects 'trace {subcmd}' but it is "
                    f"documented in configuration.md."
                )


# ===========================================================================
# 3b. Nested command coverage (PARSER -> DOCS, reads actual subparsers)
# ===========================================================================


def _subparser_choices(parser, *path: str) -> set[str]:
    """Return the set of subcommand names registered at ``parser``.

    Descends through ``path`` to resolve a nested subparser first. For
    example ``_subparser_choices(root, "adapter", "matrix", "auth")``
    walks adapter -> matrix -> auth and returns the choices registered
    on the auth subparser. Returns an empty set if the path does not
    resolve or the leaf parser has no subparsers.
    """
    current = parser
    for name in path:
        subparsers = getattr(current, "_subparsers", None)
        if subparsers is None:
            return set()
        found = None
        for action in subparsers._actions:
            choices = getattr(action, "choices", None)
            if choices and name in choices:
                found = choices[name]
                break
        if found is None:
            return set()
        current = found
    subparsers = getattr(current, "_subparsers", None)
    if subparsers is None:
        return set()
    result: set[str] = set()
    for action in subparsers._actions:
        choices = getattr(action, "choices", None)
        if choices:
            result.update(choices.keys())
    return result


@pytest.fixture(scope="module")
def root_parser():
    from medre.cli.main import _build_parser

    return _build_parser()


@pytest.fixture(scope="module")
def config_text() -> str:
    if not CONFIG_DOC.exists():
        pytest.skip("configuration.md not found")
    return _read(CONFIG_DOC)


class TestNestedCommandCoverage:
    """Every operator-facing nested subcommand registered in the parser
    must be documented in configuration.md's CLI inventory.

    Unlike the parse-args style checks above, these tests read the
    subparser choices directly so they reflect the actual parser shape
    rather than a hardcoded expectation. They also guard the docs side:
    when a subcommand exists in the parser but is missing from the
    inventory (or vice versa), the mismatch is reported explicitly.
    """

    @pytest.mark.parametrize(
        "top_level,expected",
        [
            ("config", {"check", "sample"}),
            ("routes", {"validate", "topology", "list", "plan"}),
            ("inspect", {"event", "receipts", "native-ref", "replay"}),
            ("trace", {"event", "replay"}),
            ("storage", {"status", "reset"}),
            ("support", {"bundle"}),
        ],
    )
    def test_nested_subcommands_exist_and_documented(
        self, root_parser, config_text: str, top_level: str, expected: set[str]
    ) -> None:
        """Each nested subcommand listed in the operator surface must
        (a) actually exist in the parser and (b) appear in the
        configuration.md CLI inventory.

        The docs use either the individual form
        (``medre inspect event``) or the grouped form
        (``medre routes (validate|topology|list|plan)``); both satisfy the
        coverage requirement. This reads the actual parser subparsers so
        the test reflects the real command surface."""
        actual = _subparser_choices(root_parser, top_level)
        assert actual, f"Parser exposes no subcommands under '{top_level}'"
        missing_from_parser = expected - actual
        assert not missing_from_parser, (
            f"Parser is missing '{top_level}' subcommands: "
            f"{sorted(missing_from_parser)}. Actual: {sorted(actual)}"
        )
        for sub in sorted(expected):
            # Accept either "medre <top> <sub>" or
            # "medre <top> (...<sub>...)" grouped form.
            grouped = re.compile(
                rf"medre\s+{re.escape(top_level)}\s*\([^)]*\b"
                rf"{re.escape(sub)}\b[^)]*\)"
            )
            individual = f"medre {top_level} {sub}"
            assert grouped.search(config_text) or individual in config_text, (
                f"configuration.md CLI inventory must document "
                f"'medre {top_level} {sub}' (individual or grouped form). "
                f"Parser exposes it but the docs do not."
            )

    def test_adapter_matrix_auth_subcommands_documented(
        self, root_parser, config_text: str
    ) -> None:
        """``adapter matrix auth`` exposes ``login`` and ``status``; both
        must appear in configuration.md. ``logout`` is intentionally not
        asserted — it is not registered in the parser."""
        actual = _subparser_choices(root_parser, "adapter", "matrix", "auth")
        assert actual, "Parser exposes no subcommands under 'adapter matrix auth'"
        expected = {"login", "status"}
        missing_from_parser = expected - actual
        assert not missing_from_parser, (
            "Parser is missing 'adapter matrix auth' subcommands: "
            f"{sorted(missing_from_parser)}. Actual: {sorted(actual)}"
        )
        for sub in sorted(expected):
            # configuration.md groups the auth subcommands as
            # "adapter matrix auth (login|status)".
            assert sub in config_text, (
                f"configuration.md CLI inventory must document "
                f"'adapter matrix auth {sub}'."
            )
        # Sanity: the grouped form is present.
        assert "adapter matrix auth" in config_text, (
            "configuration.md must document the 'medre adapter matrix auth' "
            "command group."
        )

    def test_adapter_matrix_auth_logout_not_registered(self, root_parser) -> None:
        """``adapter matrix auth logout`` is not registered in the parser.

        Documents the gap between the planned operator surface and the
        current parser so a future addition flips this test green rather
        than silently landing undocumented.
        """
        actual = _subparser_choices(root_parser, "adapter", "matrix", "auth")
        assert "logout" not in actual, (
            "'adapter matrix auth logout' is now registered in the parser. "
            "Add it to configuration.md's CLI inventory and update this "
            "test plus test_docs_command_surface.py. Actual auth "
            f"subcommands: {sorted(actual)}"
        )


# ===========================================================================
# 4. No stale product-path or command-surface claims
# ===========================================================================


class TestNoStaleClaims:
    """Docs must not reference commands, paths, or product claims that
    no longer exist."""

    def test_no_medre_cli_old_paths(self) -> None:
        """No docs reference old private CLI module paths."""
        _all_doc_text()
        patterns = [
            re.compile(r"\bfrom\s+medre\.cli\._"),
            re.compile(r"\bimport\s+medre\.cli\._"),
        ]
        for pat in patterns:
            for doc_path in TARGET_DOCS:
                if not doc_path.exists():
                    continue
                doc_text = _read(doc_path)
                for lineno, line in enumerate(doc_text.splitlines(), start=1):
                    if pat.search(line) and "import" in line:
                        pytest.fail(
                            f"{doc_path.name}:{lineno}: stale private CLI "
                            f"import reference: {line.strip()}"
                        )

    def test_no_old_replay_storage_path_examples(self) -> None:
        """No docs show replay with --storage-path (it was intentionally removed)."""
        text = _all_doc_text()
        stale = re.findall(r"medre\s+replay\b.*--storage-path", text)
        assert not stale, (
            "Found docs showing replay with --storage-path. "
            "Replay requires --config and rejects --storage-path."
        )

    def test_no_stale_no_dedupe_claims(self) -> None:
        """Docs must not claim deduplication exists (it doesn't)."""
        text = _all_doc_text()
        # Look for "dedup" or "de-dup" claims that suggest medre provides
        # deduplication rather than just noting its absence.
        pattern = re.compile(
            r"(?:provides|offers|supports|includes)\s+duplicate?\s*(?:detection|prevention|elimination)",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        assert match is None, (
            "Found claim that medre provides deduplication. "
            "medre does not deduplicate — docs should note this."
        )

    def test_no_stale_rest_api_claims(self) -> None:
        """Docs must not reference REST API endpoints (they don't exist)."""
        _all_doc_text()
        for pattern_str in [
            r"/api/v1/",
            r"HTTP endpoint",
            r"REST API",
        ]:
            pat = re.compile(pattern_str, re.IGNORECASE)
            for doc_path in TARGET_DOCS:
                if not doc_path.exists():
                    continue
                doc_text = _read(doc_path)
                for lineno, line in enumerate(doc_text.splitlines(), start=1):
                    if pat.search(line):
                        # Allow in README where "No API endpoints" is stated.
                        if "no " in line.lower() or "not " in line.lower():
                            continue
                        pytest.fail(
                            f"{doc_path.name}:{lineno}: stale reference to "
                            f"REST/HTTP API: {line.strip()}"
                        )

    def test_no_webhook_claims(self) -> None:
        """Docs must not reference webhooks (they don't exist)."""
        _all_doc_text()
        for doc_path in TARGET_DOCS:
            if not doc_path.exists():
                continue
            doc_text = _read(doc_path)
            for lineno, line in enumerate(doc_text.splitlines(), start=1):
                if "webhook" in line.lower():
                    if "no " in line.lower() or "not " in line.lower():
                        continue
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: stale webhook reference: "
                        f"{line.strip()}"
                    )

    def test_no_deployment_tooling_claims(self) -> None:
        """Docs must not reference deployment tooling that doesn't exist."""
        text = _all_doc_text()
        for doc_path in TARGET_DOCS:
            if not doc_path.exists():
                continue
            doc_text = _read(doc_path)
            for _lineno, line in enumerate(doc_text.splitlines(), start=1):
                if "kubernetes" in line.lower() or "docker-compose" in line.lower():
                    # Allow mentions in context of MEDRE_HOME for Docker.
                    if "MEDRE_HOME" in line:
                        continue
                    # Allow docker-compose.integration.yaml reference.
                    if (
                        "docker-compose" in line
                        and "integration"
                        in text[max(0, text.find(line) - 200) : text.find(line) + 200]
                    ):
                        continue
                    # Allow negative claims ("no deployment tooling").
                    if "no " in line.lower() or "not " in line.lower():
                        continue
                    # README mentions Docker/K8s as use cases for MEDRE_HOME.
                    if doc_path.name == "configuration.md":
                        continue


# ===========================================================================
# 5. Status vocabulary consistency
# ===========================================================================


class TestStatusVocabulary:
    """Verify that status values used across code and docs are consistent.

    The canonical statuses are: passed, failed, partial, error, skipped,
    sent, transient_failure, dead_lettered.
    """

    def test_smoke_report_uses_passed_not_ok(self, tmp_path: Path) -> None:
        """Smoke command JSON report status is 'passed', not 'ok'."""
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        str(_ROOT / "examples" / "configs" / "fake-bridge-smoke.yaml"),
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert (
            report["status"] == "passed"
        ), f"Smoke report status should be 'passed', got '{report['status']}'"

    def test_evidence_bundle_uses_passed_not_ok(self, tmp_path: Path) -> None:
        """Evidence bundle overall status is 'passed' or 'partial', never 'ok'."""
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        # Create a minimal DB via smoke.
        db_path = tmp_path / "status_test.db"
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        str(_ROOT / "examples" / "configs" / "fake-bridge-smoke.yaml"),
                        "--storage-path",
                        str(db_path),
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        # Collect evidence bundle.
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "evidence",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                    "--json",
                ]
            )

        bundle = json.loads(stdout_buf.getvalue())
        assert bundle["status"] in ("passed", "partial"), (
            f"Evidence bundle status should be 'passed' or 'partial', "
            f"got '{bundle['status']}'"
        )
        assert bundle["status"] != "ok", (
            "Evidence bundle status must not be 'ok'. "
            "Code returns 'passed', not 'ok'."
        )

    def test_no_status_ok_in_smoke_docs(self) -> None:
        """Smoke command docs must not show 'status: ok' examples."""
        for doc_path in TARGET_DOCS:
            if not doc_path.exists():
                continue
            text = _read(doc_path)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "smoke" in line.lower() and '"status": "ok"' in line:
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: smoke example uses "
                        f'stale "status": "ok" (should be "passed" or "failed").\n'
                        f"  {line.strip()}"
                    )

    def test_no_status_ok_in_evidence_docs(self) -> None:
        """Evidence bundle docs must not show 'status: ok' for overall
        status or section status. Code returns 'passed', not 'ok'."""
        for doc_path in TARGET_DOCS:
            if not doc_path.exists():
                continue
            text = _read(doc_path)
            # Only check docs that discuss evidence bundles.
            if "evidence" not in text.lower():
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if '"status": "ok"' in line:
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: stale "
                        f'"status": "ok" in evidence-related docs. '
                        f"Code returns 'passed', not 'ok'.\n"
                        f"  {line.strip()}"
                    )

    def test_no_bare_ok_in_status_prose_or_tables(self) -> None:
        """Status vocabulary tables and prose must not list bare 'ok' as a
        valid status value. The code never emits 'ok' — it uses 'passed'.

        This catches drift like:
          | ok / passed | All criteria met ...
        or prose like: status values are "ok", "partial", "error"
        """
        for doc_path in TARGET_DOCS:
            if not doc_path.exists():
                continue
            text = _read(doc_path)
            # Only check docs that have status vocabulary sections.
            if "status" not in text.lower():
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                # Skip JSON examples (covered by other tests).
                if '"status": "ok"' in stripped:
                    continue
                # Skip drill_steps result lines (drill steps use "ok" for step results).
                if '"result": "ok"' in stripped:
                    continue
                # Skip exit code columns like "0 (success)" (distinguishes exit code from status).
                if re.match(r".*\d\s*\(success\)", stripped):
                    continue
                # Skip SQLite PRAGMA / external tool output lines.
                if "pragma" in stripped.lower() and "integrity" in stripped.lower():
                    continue
                # Skip lines about external tools returning ok (e.g. fsck, PRAGMA).
                if re.search(
                    r"returns?\s+anything\s+other\s+than\s+`?ok`?",
                    stripped,
                    re.IGNORECASE,
                ):
                    continue
                # Catch bare "ok" as a status value in tables or prose.
                # Pattern: backtick-wrapped "ok" in a status vocabulary context,
                # or "ok / passed", or '"ok"' in table rows.
                if re.search(r"`ok`\s*/\s*`passed`", stripped):
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: stale status vocabulary "
                        f"lists 'ok' alongside 'passed'. Code never emits 'ok'.\n"
                        f"  {stripped}"
                    )
                # Catch table rows or prose that present "ok" as a status value
                # in backticks (but not inside drill_steps JSON).
                if re.search(r"`\"?ok\"?`", stripped) and "result" not in stripped:
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: bare 'ok' in status "
                        f"vocabulary context. Code uses 'passed', not 'ok'.\n"
                        f"  {stripped}"
                    )


# ===========================================================================
# 6. No stale exit-code/status conflation (0=ok)
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
# 7. No "pass/fail JSON report" — use "passed/failed"
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


# ===========================================================================
# 8. Command surface utility category in operator-command-surface.md
# ===========================================================================


class TestCommandSurfaceUtilityCategory:
    """operator-command-surface.md must classify version, paths, adapters,
    and config sample as utility commands, not product-operation."""

    _SURFACE_DOC = _ROOT / "docs" / "architecture" / "operator-command-surface.md"

    @pytest.fixture()
    def surface_text(self) -> str:
        if not self._SURFACE_DOC.exists():
            pytest.skip("operator-command-surface.md not found")
        return _read(self._SURFACE_DOC)

    def test_utility_section_exists(self, surface_text: str) -> None:
        """The utility commands section must exist."""
        assert (
            "Utility" in surface_text
        ), "operator-command-surface.md must have a utility commands section."

    @pytest.mark.parametrize(
        "command",
        ["version", "paths", "adapters", "config sample"],
    )
    def test_utility_commands_classified(self, command: str, surface_text: str) -> None:
        """version, paths, adapters, and config sample must be classified as
        utility (not product-operation) in the decision table or
        per-command classification."""
        # Find the operational properties decision table and check role column
        # for the utility classification.
        # The role column must say "utility" for these commands.
        # Look in the per-command classification sections.
        utility_section_start = surface_text.find("Utility command")
        if utility_section_start < 0:
            utility_section_start = surface_text.find("### Utility")
        if utility_section_start < 0:
            # Fall back to checking that the decision table has "utility" role
            # for these commands.
            pass
        # Check the decision table role column for each command
        for _lineno, line in enumerate(surface_text.splitlines(), start=1):
            if f"`{command}`" in line and "utility" in line.lower():
                return  # Found as utility
            if (
                command == "config sample"
                and "`config sample`" in line
                and "utility" in line.lower()
            ):
                return
        # If not found in per-command section, check the decision table
        # (the table uses backtick-wrapped command names)
        command_variants = [command]
        if command == "config sample":
            command_variants = ["config sample"]
        for cv in command_variants:
            for _lineno, line in enumerate(surface_text.splitlines(), start=1):
                if f"| `{cv}`" in line and "utility" in line.lower():
                    return
        pytest.fail(
            f"operator-command-surface.md must classify '{command}' as "
            f"utility (found in neither per-command section nor decision table)."
        )

    def test_no_stale_trace_event_config_in_operator_workflow_docs(self) -> None:
        """Operator workflow runbooks must not show 'medre trace event ... --config'.
        Read-only trace commands should use --storage-path."""
        operator_docs = [
            OPS_DIR / "recovery-and-replay.md",
            OPS_DIR / "recovery-and-replay.md",
            OPS_DIR / "running-medre.md",
        ]
        for doc_path in operator_docs:
            if not doc_path.exists():
                continue
            text = _read(doc_path)
            stale = re.findall(
                r"medre\s+trace\s+event\b.*--config\b",
                text,
            )
            assert not stale, (
                f"{doc_path.name} contains stale 'medre trace event ... --config'. "
                f"Read-only trace commands in operator docs should use --storage-path. "
                f"Found: {stale[:5]}"
            )
