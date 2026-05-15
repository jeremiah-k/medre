"""Meta-test: enforces structural boundaries on the test suite.

Runs filesystem-level checks against ``tests/`` to keep the suite
well-organized as files are split and refactored.

All checks are read-only — no files are created or modified.
"""

from __future__ import annotations

import re
import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent

# Legacy files that are allowed to exceed 1 500 lines until they are split.
# Each carries a TODO comment inside.
LEGACY_ALLOWLIST: dict[str, int] = {
    "test_pipeline.py": 2_937,
    "test_matrix_session.py": 2_241,
    "test_cli.py": 2_172,
    "test_replay.py": 1_850,
    "test_storage.py": 1_939,
    "test_canonical_events.py": 1_992,
    "test_meshtastic_fake_bridge.py": 1_540,
    "test_fake_runtime_smoke.py": 1_506,
    # Monoliths slated for deletion (Wave 3) — exempt from line count.
    "test_adapter_callback_bridge.py": 1_772,
    "test_operator_workflows.py": 1_997,
    # Pre-existing files that exceed the limit — allowlisted until split.
    "test_replay_routing.py": 1_584,
}

MAX_LINES = 1_500

# Monolith files slated for deletion (Wave 3 step 2).
DELETED_MONOLITHS = (
    "test_adapter_callback_bridge",
    "test_longrun_callback_bridge",
    "test_operator_workflows",
)

# New bridge / operator files — must not contain fixed asyncio.sleep(N) with N>0.
NEW_BRIDGE_OPERATOR_FILES = [
    "test_fake_adapter_ingress_equivalence.py",
    "test_bidirectional_bridge_safety.py",
    "test_fanout_source_exclusion.py",
    "test_matrix_wrapper_ingress.py",
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
]

# New helper modules — must not contain broad type: ignore / pyright: ignore.
HELPER_FILES = [
    "helpers/bridge.py",
    "helpers/matrix.py",
    "helpers/async_utils.py",
    "helpers/assertions.py",
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
    """Every test file is ≤ 1 500 lines unless allowlisted."""
    failures: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        name = path.name
        lines = _count_lines(path)
        if name in LEGACY_ALLOWLIST:
            # Legacy file — just confirm it's roughly where we expect.
            expected = LEGACY_ALLOWLIST[name]
            assert lines <= expected + 200, (
                f"Legacy file {name} grew beyond its allowlisted budget "
                f"(~{expected} lines, now {lines}). Update the allowlist or split it."
            )
            continue
        if lines > MAX_LINES:
            failures.append(f"  {name}: {lines} lines (limit {MAX_LINES})")

    assert not failures, (
        "Non-allowlisted test files exceed the 1 500-line limit:\n"
        + "\n".join(failures)
    )


# ===================================================================
# Check 2 — no imports from deleted monoliths
# ===================================================================


@pytest.mark.parametrize("monolith_stem", DELETED_MONOLITHS)
def test_no_imports_from_deleted_monoliths(monolith_stem: str) -> None:
    """No ``tests/`` .py file imports from a monolith slated for deletion,
    except the monolith file itself.
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
                pytest.fail(
                    f"{rel} imports from deleted monolith '{monolith_stem}'"
                )


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
    assert '"docker:' in content or "docker:" in content, (
        "The 'docker' marker is not registered in pyproject.toml markers config"
    )


def test_integration_conftest_applies_docker_marker() -> None:
    """``tests/integration/conftest.py`` must apply ``pytest.mark.docker`` to
    all tests in the package.
    """
    conftest = TESTS_DIR / "integration" / "conftest.py"
    assert conftest.exists(), "tests/integration/conftest.py is missing"
    source = conftest.read_text(encoding="utf-8")
    assert "pytest.mark.docker" in source, (
        "integration conftest does not apply pytest.mark.docker"
    )


def test_integration_test_files_exist_and_use_docker_gate() -> None:
    """Every file under ``tests/integration/`` must live alongside a
    ``conftest.py`` that gates with ``pytest.mark.docker`` (verified above).
    This test simply confirms integration test files exist.
    """
    integration_dir = TESTS_DIR / "integration"
    assert integration_dir.is_dir(), "tests/integration/ directory is missing"
    test_files = list(integration_dir.glob("test_*.py"))
    assert len(test_files) > 0, (
        "No integration test files found in tests/integration/"
    )
