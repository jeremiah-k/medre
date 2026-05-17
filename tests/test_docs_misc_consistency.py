"""Miscellaneous docs consistency tests.

Classes 3,4,8,16,18,19,20,29,30: private CLI imports, replay
distinguishability, retry semantics, stale trace event config,
config check exit code, docker compose filenames, source-tree examples
wording, no tcp_port in examples, live config helper uses port.
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
    RUNBOOKS_DIR / "bridge-recovery.md",
    RUNBOOKS_DIR / "replay-operation.md",
    RUNBOOKS_DIR / "bridge-evidence-bundle.md",
    RUNBOOKS_DIR / "event-tracing.md",
    RUNBOOKS_DIR / "bridge-failure-drills.md",
    RUNBOOKS_DIR / "configuration.md",
]

_OPERATOR_COMMAND_SURFACE = _ROOT / "docs" / "architecture" / "operator-command-surface.md"
_EXAMPLES_CONFIGS_DIR = _ROOT / "examples" / "configs"
_LIVE_CONFIG_HELPER = _ROOT / "tests" / "helpers" / "live_config.py"


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _all_doc_text() -> str:
    """Concatenate all target docs for global searches."""
    return "\n".join(_read(p) for p in TARGET_DOCS)


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
# 16. No stale "medre trace event ... --config" in operator docs
# ===========================================================================


class TestNoStaleTraceEventConfigInOperatorDocs:
    """Read-only trace/inspect commands in operator docs should prefer
    ``--storage-path`` over ``--config``.  The pattern ``medre trace event
    ... --config`` is stale in operator-facing runbooks.

    Specialized reference docs (event-tracing.md command reference sections
    1.1 and 1.3) may still show ``--config`` since the trace command supports
    both.  But operator workflow sections, investigation examples, and
    quick-reference tables should use ``--storage-path`` for read-only DB
    access.
    """

    # Docs where operator workflow examples appear.
    _OPERATOR_WORKFLOW_DOCS = [
        RUNBOOKS_DIR / "bridge-recovery.md",
        RUNBOOKS_DIR / "replay-operation.md",
        RUNBOOKS_DIR / "bridge-operation.md",
    ]

    @pytest.mark.parametrize(
        "doc_path",
        [p for p in _OPERATOR_WORKFLOW_DOCS if p.exists()],
        ids=lambda p: p.name,
    )
    def test_no_trace_event_config_in_operator_docs(self, doc_path: Path) -> None:
        """Operator workflow docs must not show ``medre trace event ... --config``.
        Use ``--storage-path`` for read-only DB access instead."""
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

    @pytest.mark.parametrize(
        "doc_path",
        [p for p in _OPERATOR_WORKFLOW_DOCS if p.exists()],
        ids=lambda p: p.name,
    )
    def test_no_inspect_config_in_workflow_examples(self, doc_path: Path) -> None:
        """Inspect examples in operator workflow docs should use --storage-path
        for read-only access, not --config.

        This catches patterns like ``medre inspect receipts ... --config``
        that should be ``--storage-path`` for read-only investigation.
        """
        text = _read(doc_path)
        # Find inspect command lines that use --config (inside code blocks)
        stale = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if re.match(r"^medre\s+inspect\s+\w+.*--config", stripped):
                stale.append((lineno, stripped))
        assert not stale, (
            f"{doc_path.name} has inspect command examples using --config "
            f"instead of --storage-path. Inspect supports --storage-path for "
            f"direct read-only DB access. Found at lines: "
            f"{[s[0] for s in stale[:5]]}"
        )


# ===========================================================================
# 18. Config check exit code is 2, not 1
# ===========================================================================


class TestConfigCheckExitCode:
    """configuration.md must document the correct exit code for ``medre
    config check`` on config errors.  The codebase uses ``EXIT_CONFIG = 2``,
    so docs must say exit code 2, not 1."""

    def test_config_check_exit_code_is_2(self) -> None:
        """configuration.md must say ``Exits with code 2`` for config check
        errors, not code 1."""
        text = _read(RUNBOOKS_DIR / "configuration.md")
        # Find the config check description area
        assert "code 2" in text, (
            "configuration.md must document exit code 2 for config check "
            "errors (EXIT_CONFIG = 2 in exit_codes.py)."
        )
        # Ensure we don't have the old incorrect value in that context
        stale = re.findall(
            r"config check.*exit.*code\s+1",
            text,
            re.IGNORECASE,
        )
        assert not stale, (
            "configuration.md has stale 'exit code 1' for config check. "
            "The correct value is 2 (EXIT_CONFIG = 2)."
        )


# ===========================================================================
# 19. Docker compose filename references actual file
# ===========================================================================


class TestDockerComposeFilenameAccuracy:
    """Docs that reference docker-compose files must reference
    ``docker-compose.integration.yaml`` (which exists) rather than
    non-existent filenames like ``docker-compose.synapse.yml`` or
    ``docker-compose.meshtasticd.yml``."""

    _STALE_NAMES = [
        "docker-compose.synapse.yml",
        "docker-compose.meshtasticd.yml",
    ]

    @pytest.mark.parametrize(
        "doc_path",
        TARGET_DOCS,
        ids=lambda p: p.name,
    )
    def test_no_stale_docker_compose_filenames(self, doc_path: Path) -> None:
        """Docs must not reference non-existent docker-compose files."""
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
        text = _read(doc_path)
        for stale in self._STALE_NAMES:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if stale in line:
                    pytest.fail(
                        f"{doc_path.name}:{lineno}: references non-existent "
                        f"'{stale}'. The actual file is "
                        f"'docker-compose.integration.yaml'.\n"
                        f"  {line.strip()}"
                    )


# ===========================================================================
# 20. Source-tree examples wording consistency
# ===========================================================================


class TestSourceTreeExamplesWording:
    """Docs that reference ``examples/configs/`` must note that these are
    source-repo files, not installed package data.  Installed-package users
    should use ``medre config sample``."""

    @pytest.mark.parametrize(
        "doc_path",
        [RUNBOOKS_DIR / "alpha-walkthrough.md", RUNBOOKS_DIR / "alpha-installation.md"],
        ids=lambda p: p.name,
    )
    def test_examples_path_mentioned_with_source_tree_note(
        self, doc_path: Path
    ) -> None:
        """Docs referencing examples/configs must note source-tree vs
        installed-package distinction."""
        if not doc_path.exists():
            pytest.skip(f"{doc_path.name} not found")
        text = _read(doc_path)
        if "examples/configs/" not in text:
            pytest.skip(f"{doc_path.name} does not reference examples/configs/")
        # Must have some mention of source-tree/installed distinction
        has_source_note = (
            "source" in text.lower()
            and ("checkout" in text.lower() or "tree" in text.lower() or "clone" in text.lower())
        )
        assert has_source_note, (
            f"{doc_path.name} references examples/configs/ but does not "
            f"note that these are source-tree files, not installed package "
            f"data. Add a source-tree note and mention 'medre config sample' "
            f"as the installed-package alternative."
        )


# ===========================================================================
# 29. No tcp_port in example configs
# ===========================================================================


class TestNoTcpPortInExamples:
    """Example TOML configs must use ``port`` (not ``tcp_port``) to match
    the current config schema and live-config helper."""

    def test_no_example_uses_tcp_port(self) -> None:
        """No .toml file under examples/configs/ may contain ``tcp_port``."""
        if not _EXAMPLES_CONFIGS_DIR.is_dir():
            pytest.skip("examples/configs/ directory not found")
        toml_files = sorted(_EXAMPLES_CONFIGS_DIR.glob("*.toml"))
        if not toml_files:
            pytest.skip("No .toml files found in examples/configs/")
        violations: list[str] = []
        for toml_path in toml_files:
            text = _read(toml_path)
            if "tcp_port" in text.lower():
                violations.append(toml_path.name)
        assert not violations, (
            "The following example configs contain 'tcp_port' (should be "
            "'port'): " + ", ".join(violations)
        )


# ===========================================================================
# 30. Live config helper uses port, not tcp_port
# ===========================================================================


class TestLiveConfigHelperUsesPort:
    """tests/helpers/live_config.py must write ``port = `` (not
    ``tcp_port``) in the TOML it generates, keeping the helper consistent
    with the config schema and example configs."""

    def test_live_config_uses_port_not_tcp_port(self) -> None:
        """write_live_bridge_toml must emit ``port = `` and never
        ``tcp_port``."""
        if not _LIVE_CONFIG_HELPER.exists():
            pytest.skip("tests/helpers/live_config.py not found")
        text = _read(_LIVE_CONFIG_HELPER)
        assert "port = " in text, (
            "tests/helpers/live_config.py must contain 'port = ' in the "
            "write_live_bridge_toml function area."
        )
        assert "tcp_port" not in text, (
            "tests/helpers/live_config.py must not contain 'tcp_port'. "
            "The config schema uses 'port', not 'tcp_port'."
        )
