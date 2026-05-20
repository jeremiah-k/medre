"""Tests for tests/helpers/ast_imports.py"""
from __future__ import annotations

import ast

from tests.helpers.ast_imports import (
    all_imports,
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
