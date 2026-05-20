"""Import-time blocking I/O checks.

Scans all src/medre/**/*.py for obvious blocking calls at module level.
Uses alias resolution (extract_aliases) to catch aliased calls like
sp.run, t.sleep, etc.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers.ast_imports import (
    extract_aliases,
    parse_python,
    top_level_calls,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"

# Function names/patterns that indicate blocking I/O at import time.
# Module-level calls to these are forbidden in src/medre/**/*.py.
_BLOCKING_FUNCS: tuple[str, ...] = (
    "open",
    "read_text",
    "write_text",
    "sqlite3.connect",
    "requests.get",
    "requests.post",
    "requests.request",
    "urlopen",
    "create_connection",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_call",
    "subprocess.check_output",
    "asyncio.run",
    "time.sleep",
)

# Explit allowlist for known intentional module-level calls.
# Format: (file_path_rel, func_name, reason)
_ALLOWLIST: list[tuple[str, str, str]] = [
    # Add entries here if needed, e.g.:
    # ("medre/config/loader.py", "open", "Config file loading during init"),
]


def _resolve_via_aliases(
    func_name: str,
    aliases: dict[str, str],
) -> str:
    """Resolve an aliased call like 'sp.run' to 'subprocess.run'.

    Checks if the call's root object maps to an alias:
      'sp.run' -> aliases.get('sp', 'sp') + '.run'
    If the resolved name is 'subprocess.run', returns that.
    Otherwise returns the original func_name.
    """
    if "." not in func_name:
        # Direct call like 'sleep(1)' — check if 'sleep' is an alias
        base = aliases.get(func_name)
        if base:
            return base
        return func_name

    parts = func_name.split(".")
    root = parts[0]
    if root in aliases:
        resolved = aliases[root] + "." + ".".join(parts[1:])
        return resolved
    return func_name


def _is_allowed(file_rel: str, func_name: str) -> bool:
    """Check if a blocking call is in the allowlist."""
    for aw_rel, aw_func, _reason in _ALLOWLIST:
        if file_rel == aw_rel and func_name == aw_func:
            return True
    return False


def _scan_file(py_file: Path) -> list[str]:
    """Scan a single Python file for blocking I/O calls at module level.

    Returns violation descriptions.
    """
    try:
        tree = parse_python(py_file)
    except SyntaxError:
        return []  # Skip files with syntax errors

    # Extract aliases from the file
    aliases = extract_aliases(tree)

    calls = top_level_calls(tree)
    rel = str(py_file.relative_to(_REPO))
    violations: list[str] = []

    for call in calls:
        # Resolve alias
        resolved = _resolve_via_aliases(call.func, aliases)

        # Check against blocking funcs
        for blocking_func in _BLOCKING_FUNCS:
            if resolved == blocking_func or resolved.endswith(f".{blocking_func}"):
                if _is_allowed(rel, resolved):
                    continue
                violations.append(
                    f"{rel}:{call.lineno}: blocking call {resolved}() "
                    f"(from {call.func}) at module level"
                )
                break

    return violations


class TestNoBlockingIOAtImport:
    """No module under src/medre/ may perform blocking I/O at import time."""

    def test_all_modules_no_blocking_io(self) -> None:
        all_violations: list[str] = []
        for py_file in sorted(_SRC.rglob("*.py")):
            violations = _scan_file(py_file)
            all_violations.extend(violations)

        assert (
            not all_violations
        ), "Blocking I/O calls found at module level:\n" + "\n".join(all_violations)

    def test_allowlist_entries_are_documented(self) -> None:
        """If the allowlist has entries, list them so they stay visible."""
        if _ALLOWLIST:
            entries = "\n".join(
                f"  {rel}: {func} — {reason}" for rel, func, reason in _ALLOWLIST
            )
            print(f"Allowlisted blocking I/O:\n{entries}")
