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

# No oversized-test allowlist. Every test_*.py file must be <= MAX_LINES.

MAX_LINES = 1_500

# Deleted monoliths — guarding against reintroduction of deleted files.
DELETED_MONOLITHS = (
    "test_adapter_callback_bridge",
    "test_longrun_callback_bridge",
    "test_operator_workflows",
    "test_pipeline",
    "test_replay",
    "test_cli",
    "test_docker_bridge_artifacts",
)

# Marker names whose presence on a module, class, or test function exempts
# the file from the project-wide fixed-sleep check. Live/soak tests run
# against real services and need real-time pacing; hardware/docker tests
# gate on availability checks that already use bounded waits.
EXEMPT_MARKERS = frozenset({"live", "soak", "hardware", "docker"})

# Explicit allowlist of fixed-sleep sites that are load-bearing for the
# behaviour under test. Each entry is ``(relative_path, line_number)``.
# Add a new entry here ONLY when the sleep cannot be removed without
# changing the test's intent, and pair it with a justification comment in
# the source so future readers understand why it stays.
ALLOW_FIXED_SLEEP: tuple[tuple[str, int], ...] = (
    # Slow adapter delivery simulation — required so the test pipeline
    # observes a non-instant delivery and exercises the delivery success
    # path through the outbox.
    ("tests/test_pipeline_outbox.py", 673),
    # Slow adapter for ≥1 renewal cycle to fire — required so the renewal
    # loop has time to attempt multiple renewals before the slow delivery
    # completes.
    ("tests/test_pipeline_outbox.py", 1136),
    # SlowStopOnStartFailure.stop() simulates a hung stop that ignores
    # CancelledError, exercising the bounded-poll stop helper.
    ("tests/helpers/startup_cleanup.py", 215),
    # _slow_cancel pause before raising CancelledError so the test has a
    # window to call task.cancel() before the stop returns.
    ("tests/helpers/startup_cleanup.py", 347),
)


# Helper modules — must not contain broad type: ignore / pyright: ignore.
HELPER_FILES_LEGACY = [
    "helpers/walkthrough.py",
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
    with path.open(encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def _expr_references_marker(node: ast.AST, names: set[str]) -> bool:
    """Return True if *node* (any expression) references one of *names*
    via an ``Attribute`` (e.g. ``pytest.mark.live``) or by collecting a
    list/tuple of such markers. Bare ``Name`` nodes do NOT count — alias
    resolution is handled in :func:`_module_has_marker`.
    """
    if isinstance(node, ast.Attribute):
        # Direct match: ``pytest.mark.live`` or chained ``pytest.mark.something.live``.
        if node.attr in names and _is_pytest_mark_chain(node):
            return True
        # Otherwise descend into the chain (e.g. ``pytest.mark.live.something``).
        return _expr_references_marker(node.value, names)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_expr_references_marker(elt, names) for elt in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _expr_references_marker(node.left, names) or _expr_references_marker(
            node.right, names
        )
    if isinstance(node, ast.Call):
        return _expr_references_marker(node.func, names) or any(
            _expr_references_marker(a, names) for a in node.args
        )
    return False


def _is_pytest_mark_chain(node: ast.Attribute) -> bool:
    """Return True if *node* is the terminal of a ``pytest.mark.<name>``
    attribute chain (e.g. ``pytest.mark.live``).
    """
    inner = node.value
    # Walk up: ``pytest.mark.X`` ⇒ inner is ``Attribute(value=Name('pytest'), attr='mark')``
    while isinstance(inner, ast.Attribute):
        if inner.attr == "mark" and isinstance(inner.value, ast.Name):
            return inner.value.id == "pytest"
        inner = inner.value
    return False


def _decorators_refer_to_marker(
    decorators: list[ast.expr], names: set[str]
) -> bool:
    """Return True if any decorator in *decorators* references one of *names*.

    Walks decorator chains so that ``@pytest.mark.skipif(...)`` decorating
    a class also marks the class as live/soak/etc when the skipif reason
    references a live-only environment variable (best-effort heuristic
    based on direct attribute references).
    """
    for dec in decorators:
        if _expr_references_marker(dec, names):
            return True
        # Handle ``name = [pytest.mark.live, ...]`` references: if a
        # decorator is a bare Name that resolves to a module-level list,
        # the caller must look up the alias via the module's local
        # namespace.  We don't resolve aliases here — pytestmark aliases
        # are handled in :func:`_module_has_marker`.
    return False


def _module_has_marker(tree: ast.Module, names: set[str]) -> bool:
    """Return True if *tree* applies any of *names* via module-level
    ``pytestmark = ...`` or top-level function/class decorators.
    """
    # Pre-compute alias values for module-level ``foo = [...]`` markers.
    aliases: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name):
                aliases[tgt.id] = node.value

    def _value_resolves_marker(value: ast.AST) -> bool:
        if _expr_references_marker(value, names):
            return True
        # Resolve a bare alias: ``pytestmark = pytestmark_matrix``.
        if isinstance(value, ast.Name) and value.id in aliases:
            return _value_resolves_marker(aliases[value.id])
        return False

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "pytestmark"
                    and _value_resolves_marker(node.value)
                ):
                    return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _decorators_refer_to_marker(node.decorator_list, names):
                return True
            # Classes may also carry a class-level pytestmark = ...
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "pytestmark"
                        for t in sub.targets
                    ):
                        if _value_resolves_marker(sub.value):
                            return True
    return False


def _collect_fixed_sleep_calls(tree: ast.Module) -> list[tuple[int, str]]:
    """Return a list of ``(line_number, qualified_name)`` for every fixed
    sleep call in *tree* — i.e. ``asyncio.sleep(<positive literal>)`` or
    ``time.sleep(<positive literal>)``.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.attr == "sleep"
            and func.value.id in ("asyncio", "time")
        ):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Constant):
            continue
        val = arg.value
        if not isinstance(val, (int, float)) or val <= 0:
            continue
        found.append((node.lineno, f"{func.value.id}.sleep({val})"))
    return found


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

    assert not failures, "Test files exceed the 1 500-line limit:\n" + "\n".join(
        failures
    )


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
# Check 3 — no fixed sleeps project-wide
# ===================================================================


def test_no_fixed_sleeps_project_wide() -> None:
    """Project-wide check: no ``tests/`` file (other than the integration
    suite, files marked live/soak/hardware/docker, or the explicit
    allowlist) may contain ``asyncio.sleep(<positive literal>)`` or
    ``time.sleep(<positive literal>)`` calls.

    Use an ``Event``/``wait_until``/``wait_for(timeout=...)`` instead so
    the suite stays deterministic and runs as fast as the production
    code permits.
    """
    allow = {(p, ln) for p, ln in ALLOW_FIXED_SLEEP}
    failures: list[str] = []

    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(TESTS_DIR)
        rel_str = str(rel)
        # Skip pycache and this meta-test (which references the allowlist).
        if "__pycache__" in rel_str or path.name == Path(__file__).name:
            continue
        # Integration suite is gated by docker + live hardware; skip.
        if rel_str.startswith("integration/"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        # Skip files that declare an exempt marker (live/soak/hardware/docker)
        # at module, class, or top-level function level.
        if _module_has_marker(tree, set(EXEMPT_MARKERS)):
            continue

        # Also skip files where every fixed sleep lives in the allowlist.
        offenders = [
            (ln, name)
            for ln, name in _collect_fixed_sleep_calls(tree)
            if (f"tests/{rel_str}", ln) not in allow
        ]
        if offenders:
            for ln, name in offenders:
                failures.append(f"  {rel_str}:{ln}  {name}")

    assert not failures, (
        "Fixed sleeps detected outside the allowlist. "
        "Use tests/helpers/async_utils.py::wait_until, an asyncio.Event, "
        "or asyncio.wait_for(..., timeout=...) instead:\n" + "\n".join(failures)
    )


# ===================================================================
# Check 4 — no broad type: ignore / pyright: ignore in helpers
# ===================================================================


@pytest.mark.parametrize("rel_path", HELPER_FILES_LEGACY)
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


# Helper-files allowlist is declared above the helper-function section so
# the parametrize decorator can resolve the name at collection time.


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
            elif s.startswith("from tests.helpers") or s.startswith(
                "import tests.helpers"
            ):
                continue

    assert (
        not bad
    ), "Test modules must not import from other test modules:\n" + "\n".join(
        f"  {f}:{ln}: {line}" for f, ln, line in bad
    )
