"""Import-time blocking I/O checks.

Scans all src/medre/**/*.py for obvious blocking calls at module level.
Uses alias resolution (extract_aliases) to catch aliased calls like
sp.run, t.sleep, etc.

NOTE: This is best-effort static analysis, not type inference.
We match call names against known blocking patterns without attempting
to resolve the actual type of the receiver object.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

from medre.runtime.architecture_ast import resolve_call_name
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
# _BLOCKING_BARE — bare function/method names like "open" or "urlopen".
#   Matched via **exact equality**.  Only real bare builtins/helpers
#   belong here.  Dotted forms (e.g. pathlib.Path.open) must go in
#   _BLOCKING_DOTTED for precise matching.
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
    # pathlib — explicit dotted forms (exact match after resolve_call_name)
    "pathlib.Path.read_text",
    "pathlib.Path.write_text",
    "pathlib.Path.read_bytes",
    "pathlib.Path.write_bytes",
    "pathlib.Path.open",
    "urllib.request.urlopen",
    # builtins.open / io.open aliases
    "builtins.open",
    "io.open",
    # aiohttp
    "aiohttp.ClientSession",
)

_BLOCKING_BARE: tuple[str, ...] = (
    # builtins / ubiquitous
    "open",
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
    except SyntaxError as exc:
        rel = str(py_file.relative_to(_REPO))
        return [
            f"{rel}:{getattr(exc, 'lineno', '?')}: SyntaxError while scanning "
            f"import-time I/O: {exc}"
        ]

    # Extract aliases from the file
    aliases = extract_aliases(tree)

    calls = top_level_calls(tree)
    rel = str(py_file.relative_to(_REPO))
    violations: list[str] = []

    for call in calls:
        # Resolve alias
        resolved = resolve_call_name(call.func, aliases)

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
            # 2. Exact match against bare blocking names
            for bare in _BLOCKING_BARE:
                if resolved == bare:
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
            called_names = {resolve_call_name(c.func, aliases) for c in calls}
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

    def test_syntax_error_produces_violation(self) -> None:
        """A .py file with invalid syntax produces a violation mentioning SyntaxError."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py",
            delete=False,
            prefix="test_io_syntax_err_",
            dir=_SRC,
        )
        try:
            tmp.write(b"def f(\n")  # invalid syntax
            tmp.flush()
            tmp.close()

            violations = _scan_file(Path(tmp.name))
            assert violations, "Scanner should report SyntaxError as violation"
            assert (
                "SyntaxError" in violations[0]
            ), f"Expected SyntaxError in violation, got: {violations[0]}"
        finally:
            Path(tmp.name).unlink(missing_ok=True)

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


# ---------------------------------------------------------------------------
# resolve_call_name — recursive alias resolution tests
# ---------------------------------------------------------------------------


class TestResolveCallName:
    """Tests for recursive alias resolution in resolve_call_name()."""

    def test_chained_alias_dotted(self) -> None:
        """P.read_text with {"P": "Path", "Path": "pathlib.Path"} -> pathlib.Path.read_text."""
        result = resolve_call_name("P.read_text", {"P": "Path", "Path": "pathlib.Path"})
        assert result == "pathlib.Path.read_text"

    def test_chained_alias_bare(self) -> None:
        """runner with {"runner": "run", "run": "subprocess.run"} -> subprocess.run."""
        result = resolve_call_name("runner", {"runner": "run", "run": "subprocess.run"})
        assert result == "subprocess.run"

    def test_deep_chain(self) -> None:
        """a.x with {"a": "b", "b": "pkg.mod"} -> pkg.mod.x."""
        result = resolve_call_name("a.x", {"a": "b", "b": "pkg.mod"})
        assert result == "pkg.mod.x"

    def test_cycle_detected(self) -> None:
        """a.x with {"a": "b", "b": "a"} -> terminates without infinite loop."""
        result = resolve_call_name("a.x", {"a": "b", "b": "a"})
        # Must terminate; exact value is less important than no hang
        assert result in ("a.x", "b.x")

    def test_self_cycle(self) -> None:
        """a.x with {"a": "a"} -> a.x (self-cycle detected, stops)."""
        result = resolve_call_name("a.x", {"a": "a"})
        assert result == "a.x"

    def test_no_alias(self) -> None:
        """Unaliased name passes through unchanged."""
        assert resolve_call_name("foo.bar", {}) == "foo.bar"
        assert resolve_call_name("foo", {}) == "foo"

    def test_single_alias(self) -> None:
        """Single-level alias still works."""
        assert resolve_call_name("sp.run", {"sp": "subprocess"}) == "subprocess.run"


# ---------------------------------------------------------------------------
# Bare matching precision — no false positives from suffix matching
# ---------------------------------------------------------------------------


class TestBareMatchingPrecision:
    """Bare matching must not suffix-match arbitrary obj.method() calls."""

    def test_bare_open_is_flagged(self) -> None:
        """Builtin open() at module level is flagged."""
        source = 'x = open("f")\n'
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        calls = top_level_calls(tree)
        resolved = [resolve_call_name(c.func, aliases) for c in calls]
        assert "open" in resolved

    def test_obj_open_not_flagged(self) -> None:
        """obj.open() should NOT be flagged by bare matching."""
        source = 'x = obj.open("f")\n'
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        calls = top_level_calls(tree)
        resolved = [resolve_call_name(c.func, aliases) for c in calls]
        # obj.open resolves to "obj.open" — must NOT match bare "open"
        assert resolved == ["obj.open"]
        for bare in _BLOCKING_BARE:
            assert resolved[0] != bare, f"obj.open should not match bare '{bare}'"

    def test_obj_read_text_not_flagged(self) -> None:
        """obj.read_text() should NOT be flagged."""
        source = "x = obj.read_text()\n"
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        calls = top_level_calls(tree)
        resolved_calls = [resolve_call_name(c.func, aliases) for c in calls]
        assert "obj.read_text" in resolved_calls
        assert "pathlib.Path.read_text" not in resolved_calls

    def test_from_import_urlopen_flagged_via_dotted(self) -> None:
        """from urllib.request import urlopen; urlopen() caught via dotted alias."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py",
            delete=False,
            prefix="test_urlopen_from_",
            dir=_SRC,
        )
        try:
            tmp.write(
                b"from urllib.request import urlopen\nurlopen('https://example.com')\n"
            )
            tmp.flush()
            tmp.close()
            violations = _scan_file(Path(tmp.name))
            assert any("urllib.request.urlopen" in v for v in violations)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_bare_urlopen_not_in_blocking_bare(self) -> None:
        """urlopen removed from _BLOCKING_BARE to prevent false positives."""
        assert "urlopen" not in _BLOCKING_BARE

    def test_dotted_urllib_request_urlopen_flagged(self) -> None:
        """urllib.request.urlopen() caught by _BLOCKING_DOTTED directly."""
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py",
            delete=False,
            prefix="test_urlopen_dotted_",
            dir=_SRC,
        )
        try:
            tmp.write(
                b"import urllib.request\nurllib.request.urlopen('https://example.com')\n"
            )
            tmp.flush()
            tmp.close()
            violations = _scan_file(Path(tmp.name))
            assert any("urllib.request.urlopen" in v for v in violations)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
