"""Shared CLI test helpers: config constants and runner utilities.

Imported by focused test_cli_*.py files split from the original test_cli.py
monolith.  Contains no pytest fixtures — those live in each test file so
pytest can discover them.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from medre.cli import main

# ---------------------------------------------------------------------------
# Sample YAML configs used across route / config / diagnostics / run tests
# ---------------------------------------------------------------------------


CONFIG_MINIMAL = """\
runtime: {}
"""


CONFIG_NO_ADAPTERS = """\
runtime:
  name: test-no-adapters
storage:
  backend: sqlite
  path: '{state}/test.db'
"""


# ---------------------------------------------------------------------------
# Inspect-related config constants
# ---------------------------------------------------------------------------

CONFIG_INSPECT_SQLITE = """\
runtime:
  name: test-inspect
storage:
  backend: sqlite
  path: '{state}/inspect.db'
"""

CONFIG_INSPECT_MEMORY = """\
runtime:
  name: test-inspect-memory
storage:
  backend: memory
"""


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str, tmp_path: Path | None = None) -> str:
    """Run CLI with given args, capture stdout, and return output."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        # SystemExit(0) is fine (e.g. --help); non-zero is an error
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return stdout.getvalue(), stderr.getvalue(), code
