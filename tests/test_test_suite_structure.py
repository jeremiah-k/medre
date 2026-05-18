"""Meta-test: enforces structural boundaries on the test suite.

Runs filesystem-level checks against ``tests/`` to keep the suite
well-organized as files are split and refactored.

All checks are read-only — no files are created or modified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent

# All test files must be <= MAX_LINES. No exemptions.
OVERSIZED_TEST_ALLOWLIST: dict[str, int] = {}

MAX_LINES = 1_500

# Deleted monoliths — guarding against reintroduction of deleted files.
DELETED_MONOLITHS = (
    "test_adapter_callback_bridge",
    "test_longrun_callback_bridge",
    "test_operator_workflows",
    "test_pipeline",
    "test_replay",
    "test_cli",
    "test_alpha_walkthrough_cli",
    "test_docker_bridge_artifacts",
)

# New bridge / operator files — must not contain fixed asyncio.sleep(N) with N>0.
NEW_BRIDGE_OPERATOR_FILES = [
    "test_fake_adapter_ingress_equivalence.py",
    "test_bidirectional_bridge_safety.py",
    "test_fake_runtime_soak.py",
    "test_fake_runtime_startup_snapshot.py",
    "test_fanout_source_exclusion.py",
    "test_matrix_wrapper_ingress.py",
    "test_meshtastic_fake_bridge_errors.py",
    "test_meshtastic_fake_bridge_session.py",
    "test_meshtastic_wrapper_ingress.py",
    "test_meshcore_wrapper_ingress.py",
    "test_longrun_bidirectional_bridge.py",
    "test_self_message_prevention.py",
    "test_wrapper_multi_callback.py",
    "test_loop_prevention_persistence.py",
    "test_cli_config_workflows.py",
    "test_cli_route_workflows.py",
    "test_cli_diagnostics_workflows.py",
    "test_cli_run_workflows.py",
    "test_cli_install_metadata.py",
    "test_cli_smoke_run_session.py",
    "test_cli_scenario_crosscheck.py",
    "test_cli_route_commands.py",
    "test_cli_config_commands.py",
    "test_cli_parser.py",
    "test_cli_smoke_commands.py",
    "test_cli_diagnostics_commands.py",
    "test_cli_run_commands.py",
    "test_cli_inspect_commands.py",
    "test_cli_evidence_commands.py",
    "test_cli_command_help_hints.py",
    "test_cli_replay_surface.py",
    "test_alpha_cli_config_and_smoke.py",
    "test_alpha_cli_inspect_flow.py",
    "test_alpha_cli_replay_flow.py",
    "test_alpha_cli_error_paths.py",
    "test_docker_artifact_core.py",
    "test_docker_artifact_plan.py",
    "test_docker_artifact_metadata.py",
    "test_docker_artifact_honesty.py",
]

# New helper modules — must not contain broad type: ignore / pyright: ignore.
HELPER_FILES = [
    "helpers/alpha_cli.py",
    "helpers/async_utils.py",
    "helpers/assertions.py",
    "helpers/bridge.py",
    "helpers/cli.py",
    "helpers/docker_artifacts.py",
    "helpers/fake_runtime.py",
    "helpers/matrix.py",
    "helpers/matrix_session.py",
    "helpers/meshtastic.py",
    "helpers/meshtastic_bridge.py",
    "helpers/replay.py",
    "helpers/replay_routing.py",
    "helpers/runtime_builder.py",
    "helpers/soak.py",
    "helpers/storage.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_lines(path: Path) -> int:
    """Return the number of lines in *path* (0 if missing)."""
    if not path.exists():
        return 0
    return sum(1 for _ in path.open(encoding="utf-8", errors="replace"))


def _has_fixed_sleep(source: str) -> bool:
    """Return True if *source* contains ``asyncio.sleep(<literal>)`` with a
    positive numeric literal (not zero and not a variable).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback to regex if the file can't be parsed.
        return bool(re.search(r"asyncio\.sleep\(\s*[1-9]\d*(?:\.\d+)?\s*\)", source))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match ``asyncio.sleep(...)``.
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
            and func.attr == "sleep"
        ):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, (ast.Constant,)):
            # ast.Constant in 3.8+ covers int/float literals.
            val = arg.value
            if isinstance(val, (int, float)) and val > 0:
                return True
    return False


# ===================================================================
# Check 1 — line-count boundary
# ===================================================================


def test_no_file_exceeds_1500_lines() -> None:
    """Every test file is ≤ 1 500 lines."""
    failures: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        name = path.name
        lines = _count_lines(path)
        if lines > MAX_LINES:
            failures.append(f"  {name}: {lines} lines (limit {MAX_LINES})")

    assert (
        not failures
    ), "Test files exceed the 1 500-line limit:\n" + "\n".join(failures)


# ===================================================================
# Check 2 — no imports from deleted monoliths
# ===================================================================


@pytest.mark.parametrize("monolith_stem", DELETED_MONOLITHS)
def test_no_imports_from_deleted_monoliths(monolith_stem: str) -> None:
    """No ``tests/`` .py file imports from a deleted monolith, guarding
    against reintroduction of already-deleted files.
    """
    monolith_file = f"{monolith_stem}.py"
    deleted_monolith_files = {f"{s}.py" for s in DELETED_MONOLITHS}
    # This meta-test references all monolith names as string literals — skip it.
    meta_test_file = Path(__file__).name
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(TESTS_DIR)
        # Skip the monolith itself, other deleted monoliths, __pycache__,
        # and this meta-test (which names all monoliths as string literals).
        if (
            str(rel) == monolith_file
            or str(rel) in deleted_monolith_files
            or path.name == meta_test_file
            or "__pycache__" in str(rel)
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Check for both absolute and relative import forms.
        patterns = [
            rf"\bimport\s+{re.escape(monolith_stem)}\b",
            rf"\bfrom\s+{re.escape(monolith_stem)}\b",
            # Also catch relative imports inside tests/ like:
            #   from .test_adapter_callback_bridge import ...
            rf"\bfrom\s+\.\s*{re.escape(monolith_stem)}\b",
        ]
        for pat in patterns:
            if re.search(pat, source):
                pytest.fail(f"{rel} imports from deleted monolith '{monolith_stem}'")


# ===================================================================
# Check 3 — no fixed sleeps in new files
# ===================================================================


@pytest.mark.parametrize("filename", NEW_BRIDGE_OPERATOR_FILES)
def test_no_fixed_sleeps_in_new_files(filename: str) -> None:
    """New bridge/operator test files must not contain ``asyncio.sleep(N)``
    with a positive numeric literal.
    """
    path = TESTS_DIR / filename
    if not path.exists():
        pytest.skip(f"{filename} does not exist yet")
        return

    source = path.read_text(encoding="utf-8")
    assert not _has_fixed_sleep(source), (
        f"{filename} contains a fixed asyncio.sleep(N) with N > 0. "
        f"Use an event, flag, or short poll loop instead."
    )


# ===================================================================
# Check 4 — no broad type: ignore / pyright: ignore in helpers
# ===================================================================


@pytest.mark.parametrize("rel_path", HELPER_FILES)
def test_no_broad_type_ignores_in_helpers(rel_path: str) -> None:
    """Helper modules must not contain ``# type: ignore`` or
    ``# pyright: ignore`` directives.
    """
    path = TESTS_DIR / rel_path
    if not path.exists():
        pytest.skip(f"{rel_path} does not exist yet")
        return

    source = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if "# type: ignore" in stripped or "# pyright: ignore" in stripped:
            pytest.fail(
                f"{rel_path}:{lineno} contains a broad type/pright ignore:\n"
                f"  {line.strip()}"
            )


# ===================================================================
# Check 5 — Docker tests remain marker-gated
# ===================================================================


def test_docker_marker_registered() -> None:
    """``docker`` marker must be registered in ``pyproject.toml``."""
    pyproject = TESTS_DIR.parent / "pyproject.toml"
    assert pyproject.exists(), "pyproject.toml not found at repo root"
    content = pyproject.read_text(encoding="utf-8")
    assert (
        '"docker:' in content or "docker:" in content
    ), "The 'docker' marker is not registered in pyproject.toml markers config"


def test_integration_conftest_applies_docker_marker() -> None:
    """``tests/integration/conftest.py`` must apply ``pytest.mark.docker`` to
    all tests in the package.
    """
    conftest = TESTS_DIR / "integration" / "conftest.py"
    assert conftest.exists(), "tests/integration/conftest.py is missing"
    source = conftest.read_text(encoding="utf-8")
    assert (
        "pytest.mark.docker" in source
    ), "integration conftest does not apply pytest.mark.docker"


def test_integration_test_files_exist_and_use_docker_gate() -> None:
    """Every file under ``tests/integration/`` must live alongside a
    ``conftest.py`` that gates with ``pytest.mark.docker`` (verified above).
    This test simply confirms integration test files exist.
    """
    integration_dir = TESTS_DIR / "integration"
    assert integration_dir.is_dir(), "tests/integration/ directory is missing"
    test_files = list(integration_dir.glob("test_*.py"))
    assert len(test_files) > 0, "No integration test files found in tests/integration/"


# ===================================================================
# Check 6 — no test module imports another test module
# ===================================================================


def test_no_test_imports_other_test_modules() -> None:
    """No ``tests/`` .py file may import from another ``test_*.py`` module.

    This prevents tight coupling between test modules and keeps the suite
    maintainable. Imports from ``tests.helpers`` are allowed.
    """
    meta_test_file = Path(__file__).name  # skip this file itself
    bad: list[tuple[str, int, str]] = []

    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = str(path.relative_to(TESTS_DIR))
        if path.name == meta_test_file or "__pycache__" in rel:
            continue

        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            # Catch: from tests.test_X import Y
            if s.startswith("from tests.test_"):
                bad.append((rel, i, s))
            # Catch: import tests.test_X
            elif s.startswith("import tests.test_"):
                bad.append((rel, i, s))
            # Catch: from .test_X import Y  (relative import inside tests/)
            elif s.startswith("from .test_"):
                bad.append((rel, i, s))
            # Catch: from tests import test_X  (uncommon but possible)
            elif s.startswith("from tests import test_"):
                bad.append((rel, i, s))
            # Allow from tests.helpers and from tests.conftest
            elif s.startswith("from tests.helpers") or s.startswith("import tests.helpers"):
                continue

    assert not bad, (
        "Test modules must not import from other test modules:\n"
        + "\n".join(f"  {f}:{ln}: {l}" for f, ln, l in bad)
    )
