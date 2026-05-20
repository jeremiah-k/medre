"""Tests for tests/helpers/ast_imports.py"""

from __future__ import annotations

import ast

from tests.helpers.ast_imports import (
    all_imports,
    extract_aliases,
    find_relative_imports,
    import_matches,
    runtime_scope_imports,
    top_level_calls,
)


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


class TestRuntimeScopeImports:
    """Tests for runtime_scope_imports() which excludes function bodies and TYPE_CHECKING."""

    def test_module_level_import_detected(self) -> None:
        source = "import os\nimport sys\n"
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "os" in modules
        assert "sys" in modules

    def test_module_level_try_import_detected(self) -> None:
        source = """
try:
    import nio
except ImportError:
    pass
"""
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "nio" in modules

    def test_class_body_import_detected(self) -> None:
        source = """
class MyClass:
    import os
    def method(self): pass
"""
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "os" in modules

    def test_type_checking_import_ignored(self) -> None:
        source = """
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import os
"""
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "os" not in modules

    def test_function_local_import_ignored(self) -> None:
        source = """
def my_func():
    import os
"""
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "os" not in modules

    def test_line_numbers_populated(self) -> None:
        source = "import os\nimport sys\n"
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        assert all(r.lineno > 0 for r in records)

    def test_with_block_import_detected(self) -> None:
        source = """
import contextlib
with contextlib.suppress(Exception):
    import nio
"""
        tree = _parse(source)
        records = runtime_scope_imports(tree)
        modules = {r.module for r in records}
        assert "nio" in modules


class TestAllImports:
    """Tests for all_imports() which includes everything."""

    def test_includes_function_local_import(self) -> None:
        source = """
def my_func():
    import os
"""
        tree = _parse(source)
        records = all_imports(tree)
        modules = {r.module for r in records}
        assert "os" in modules

    def test_line_numbers_populated(self) -> None:
        source = "import os\n"
        tree = _parse(source)
        records = all_imports(tree)
        assert all(r.lineno > 0 for r in records)


class TestImportMatches:
    """Tests for import_matches()."""

    def test_exact_match(self) -> None:
        assert import_matches("medre.runtime", ("medre.runtime",))

    def test_submodule_match(self) -> None:
        assert import_matches("medre.runtime.builder", ("medre.runtime",))

    def test_no_match(self) -> None:
        assert not import_matches("medre.core", ("medre.runtime",))

    def test_multiple_prefixes(self) -> None:
        assert import_matches("nio", ("medre.runtime", "nio", "meshtastic"))


class TestTopLevelCalls:
    """Tests for top_level_calls()."""

    def test_module_level_call_detected(self) -> None:
        source = "print('hello')\n"
        tree = _parse(source)
        calls = top_level_calls(tree)
        assert len(calls) == 1
        assert calls[0].func == "print"

    def test_call_in_function_ignored(self) -> None:
        source = """
def my_func():
    print('hello')
"""
        tree = _parse(source)
        calls = top_level_calls(tree)
        assert len(calls) == 0

    def test_call_in_type_checking_ignored(self) -> None:
        source = """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    print('hello')
"""
        tree = _parse(source)
        calls = top_level_calls(tree)
        assert len(calls) == 0

    def test_line_numbers_populated(self) -> None:
        source = "import os\nprint('hello')\nx = 1\n"
        tree = _parse(source)
        calls = top_level_calls(tree)
        assert all(c.lineno > 0 for c in calls)


class TestFindRelativeImports:
    """Tests for find_relative_imports()."""

    def test_single_dot_import_resolved(self) -> None:
        source = "from .sibling import X\n"
        tree = _parse(source)
        # file_path in medre/core/observability/ → resolves to medre.core.observability.sibling
        records = find_relative_imports(
            tree,
            file_path="/home/user/src/medre/core/observability/module.py",
        )
        assert len(records) >= 1
        modules = {r.module for r in records}
        assert (
            "medre.core.observability.sibling" in modules
            or "medre.core.observability.sibling.X" in modules
        )

    def test_double_dot_import_resolved(self) -> None:
        source = "from ..routing import Route\n"
        tree = _parse(source)
        # file_path in medre/core/observability/ → up 2 levels → medre.core.routing
        records = find_relative_imports(
            tree,
            file_path="/home/user/src/medre/core/observability/module.py",
        )
        assert len(records) >= 1
        modules = {r.module for r in records}
        assert "medre.core.routing" in modules or "medre.core.routing.Route" in modules

    def test_over_traversal_returns_empty(self) -> None:
        """Going beyond package root should not crash and returns module or empty."""
        source = "from ....root import X\n"
        tree = _parse(source)
        records = find_relative_imports(
            tree,
            file_path="/home/user/src/medre/core/observability/module.py",
        )
        # Should not crash; may return empty or a truncated result
        assert records is not None

    def test_no_relative_imports_returns_empty(self) -> None:
        source = "import os\nimport sys\n"
        tree = _parse(source)
        records = find_relative_imports(tree)
        assert len(records) == 0

    def test_mixed_absolute_and_relative(self) -> None:
        source = """
import os
from . import sibling
from ..parent import ParentClass
"""
        tree = _parse(source)
        records = find_relative_imports(
            tree,
            file_path="/home/user/src/medre/core/observability/module.py",
        )
        relative_modules = {r.module for r in records}
        # Both relatives should appear
        assert any("sibling" in m for m in relative_modules)
        assert any("parent" in m or "ParentClass" in m for m in relative_modules)


class TestTopLevelCallsNested:
    """Tests that top_level_calls captures nested calls."""

    def test_json_load_open_detected(self) -> None:
        source = 'json.load(open("config.json"))\n'
        tree = ast.parse(source)
        calls = top_level_calls(tree)
        funcs = {c.func for c in calls}
        assert "json.load" in funcs
        assert "open" in funcs

    def test_wrapper_subprocess_run_detected(self) -> None:
        source = 'wrapper(subprocess.run(["git", "status"]))\n'
        tree = ast.parse(source)
        calls = top_level_calls(tree)
        funcs = {c.func for c in calls}
        assert "wrapper" in funcs
        assert "subprocess.run" in funcs

    def test_path_read_text_detected(self) -> None:
        source = 'Path("file").read_text()\n'
        tree = ast.parse(source)
        calls = top_level_calls(tree)
        funcs = {c.func for c in calls}
        assert "read_text" in funcs or "Path.read_text" in funcs

    def test_call_in_function_ignored(self) -> None:
        source = """
def my_func():
    json.load(open("config.json"))
"""
        tree = ast.parse(source)
        calls = top_level_calls(tree)
        assert len(calls) == 0

    def test_call_in_type_checking_ignored(self) -> None:
        source = """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    print('hello')
"""
        tree = ast.parse(source)
        calls = top_level_calls(tree)
        assert len(calls) == 0


class TestExtractAliases:
    """Tests for extract_aliases()."""

    def test_import_as_alias(self) -> None:
        source = "import subprocess as sp\n"
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        assert aliases.get("sp") == "subprocess"

    def test_from_import_alias(self) -> None:
        source = "from subprocess import run\n"
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        assert aliases.get("run") == "subprocess.run"

    def test_import_no_alias(self) -> None:
        source = "import os\n"
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        assert aliases.get("os") == "os"

    def test_multiple_aliases(self) -> None:
        source = """
import asyncio as aio
from time import sleep
import subprocess as sp
from pathlib import Path
"""
        tree = ast.parse(source)
        aliases = extract_aliases(tree)
        assert aliases.get("aio") == "asyncio"
        assert aliases.get("sleep") == "time.sleep"
        assert aliases.get("sp") == "subprocess"
        assert aliases.get("Path") == "pathlib.Path"
