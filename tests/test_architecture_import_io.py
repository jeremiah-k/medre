"""Import-time blocking I/O checks.

Scans all src/medre/**/*.py for obvious blocking calls at module level.
Uses alias resolution (extract_aliases) to catch aliased calls like
sp.run, t.sleep, etc.

NOTE: This is best-effort static analysis, not type inference.
We match call names against known blocking patterns without attempting
to resolve the actual type of the receiver object.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.helpers.ast_imports import (
    extract_aliases,
    parse_python,
    top_level_calls,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"

# ---------------------------------------------------------------------------
# Blocking-function patterns, split into two categories:
#
# _BLOCKING_DOTTED — fully-qualified dotted names like "subprocess.run".
#   Matched via **exact equality** after alias resolution.
#   (e.g. sp.run -> subprocess.run matches; foo.subprocess.run does not)
#
# _BLOCKING_BARE — bare function/method names like "open" or "read_text".
#   Matched via **suffix match** so that both `open()` and
#   `pathlib.Path.open()` are caught.  This is intentionally broad to
#   avoid missing real violations; false positives are managed through
#   the allowlist.
# ---------------------------------------------------------------------------

_BLOCKING_DOTTED: tuple[str, ...] = (
    # subprocess
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_call",
    "subprocess.check_output",
    # os — process spawning
    "os.system",
    "os.popen",
    # sqlite
    "sqlite3.connect",
    # requests / http
    "requests.get",
    "requests.post",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "httpx.request",
    # asyncio
    "asyncio.run",
    # time
    "time.sleep",
    # socket / ssl
    "socket.socket",
    "socket.create_connection",
    "ssl.wrap_socket",
    # shutil
    "shutil.copy",
    "shutil.move",
    "shutil.rmtree",
    # pathlib — explicit dotted forms (caught via suffix on bare names too)
    "pathlib.Path.read_text",
    "pathlib.Path.write_text",
    "pathlib.Path.read_bytes",
    "pathlib.Path.write_bytes",
    # aiohttp
    "aiohttp.ClientSession",
)

_BLOCKING_BARE: tuple[str, ...] = (
    # builtins / ubiquitous
    "open",
    "urlopen",
    # pathlib method names (commonly called on Path objects)
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    # socket helpers
    "create_connection",
)

# Explicit allowlist for known intentional module-level calls.
# Format: (file_path_rel: str, func_name: str, reason: str)
#   file_path_rel — path relative to repo root under src/ (e.g. "medre/config.py")
#   func_name     — resolved blocking function name (e.g. "open")
#   reason        — justification, must be >= 10 characters
# Entries MUST be sorted by (file_path_rel, func_name).
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

        # 1. Exact match against dotted blocking patterns
        for dotted in _BLOCKING_DOTTED:
            if resolved == dotted:
                if _is_allowed(rel, resolved):
                    continue
                violations.append(
                    f"{rel}:{call.lineno}: blocking call {resolved}() "
                    f"(from {call.func}) at module level"
                )
                break
        else:
            # 2. Suffix match against bare blocking names
            for bare in _BLOCKING_BARE:
                if resolved == bare or resolved.endswith(f".{bare}"):
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
        """Every allowlist entry must be well-formed, point to a real file,
        and reference a function that actually exists at module level."""
        # --- structural checks that guard even when _ALLOWLIST is empty ---
        assert isinstance(_ALLOWLIST, list), "_ALLOWLIST must be a list"

        for idx, entry in enumerate(_ALLOWLIST):
            assert isinstance(
                entry, tuple
            ), f"Entry {idx} must be a tuple, got {type(entry).__name__}"
            assert len(entry) == 3, (
                f"Entry {idx} must be (file_path_rel, func_name, reason), "
                f"got {len(entry)} elements"
            )
            file_path_rel, func_name, reason = entry

            # file_path_rel — non-empty, must exist under src/
            assert (
                isinstance(file_path_rel, str) and file_path_rel
            ), f"Entry {idx}: file_path_rel must be a non-empty string"
            full_path = _REPO / "src" / file_path_rel
            assert full_path.is_file(), f"Entry {idx}: {full_path} does not exist"

            # func_name — non-empty
            assert (
                isinstance(func_name, str) and func_name
            ), f"Entry {idx}: func_name must be a non-empty string"

            # reason — non-empty, >= 10 chars
            assert isinstance(reason, str) and len(reason) >= 10, (
                f"Entry {idx}: reason must be >= 10 characters, "
                f"got {len(reason)!r}: {reason!r}"
            )

            # Verify the function IS actually called at module level
            tree = parse_python(full_path)
            calls = top_level_calls(tree)
            aliases = extract_aliases(tree)
            called_names = {_resolve_via_aliases(c.func, aliases) for c in calls}
            assert func_name in called_names, (
                f"Entry {idx}: {func_name!r} is not called at module level "
                f"in {file_path_rel}. Stale allowlist entry?"
            )

        # Sorted by (file_path_rel, func_name)
        if len(_ALLOWLIST) > 1:
            sorted_keys = sorted((rel, func) for rel, func, _ in _ALLOWLIST)
            actual_keys = [(rel, func) for rel, func, _ in _ALLOWLIST]
            assert (
                actual_keys == sorted_keys
            ), "_ALLOWLIST must be sorted by (file_path_rel, func_name)"

    def test_scanner_catches_known_violation(self) -> None:
        """Verify the scanner flags a file with `open()` at module level."""
        # Place temp file inside repo so relative_to(_REPO) works
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py",
            delete=False,
            prefix="test_io_violation_",
            dir=_SRC,
        )
        try:
            tmp.write(b'x = open("test.txt")\n')
            tmp.flush()
            tmp.close()

            violations = _scan_file(Path(tmp.name))
            assert (
                violations
            ), "Scanner should flag open() at module level but found nothing"
            assert (
                "open" in violations[0]
            ), f"Expected 'open' in violation, got: {violations[0]}"
        finally:
            Path(tmp.name).unlink(missing_ok=True)
