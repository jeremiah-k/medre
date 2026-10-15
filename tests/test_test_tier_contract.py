"""Adapter-free ownership is exhaustive and new import coupling cannot drift."""

from pathlib import Path

import pytest

from tests.helpers.suite_ownership import (
    COMPATIBILITY,
    external_imports,
    is_compatibility,
    undeclared_dependencies,
)


def test_core_test_modules_do_not_import_external_adapters():
    assert undeclared_dependencies(Path(__file__).resolve().parents[1]) == {}


def test_helper_dependency_is_checked_before_collection(tmp_path):
    root = tmp_path
    helpers = root / "tests/helpers"
    helpers.mkdir(parents=True)
    (helpers / "native.py").write_text(
        "from medre.adapters.private_chat import Client\n"
    )
    test = root / "tests/test_example.py"
    test.write_text("from tests.helpers.native import make_client\n")
    assert external_imports(test, root) == {"medre.adapters.private_chat"}
    assert "tests/test_example.py" in undeclared_dependencies(root)
    test.write_text(COMPATIBILITY + "\n" + test.read_text())
    assert undeclared_dependencies(root) == {}


def test_suite_declaration_applies_before_loading_its_conftest(tmp_path):
    suite = tmp_path / "tests/compatibility"
    suite.mkdir(parents=True)
    (suite / "conftest.py").write_text(
        COMPATIBILITY + "\nraise RuntimeError('must not import')\n"
    )
    assert is_compatibility(suite, tmp_path)
    assert is_compatibility(suite / "test_example.py", tmp_path)


def test_relative_and_literal_dynamic_helper_imports_are_guarded(tmp_path):
    helpers = tmp_path / "tests/helpers"
    helpers.mkdir(parents=True)
    (helpers / "native.py").write_text(
        "from medre.adapters.private_chat import Client\n"
    )
    path = tmp_path / "tests/test_example.py"
    path.write_text("from .helpers.native import make_client\n")
    assert external_imports(path, tmp_path) == {"medre.adapters.private_chat"}
    path.write_text(
        'import importlib\nimportlib.import_module("medre.adapters.private_chat")\n'
    )
    assert external_imports(path, tmp_path) == {"medre.adapters.private_chat"}


@pytest.mark.parametrize(
    "code",
    [
        'import pytest\npytest.importorskip("medre.adapters.private_chat")',
        'from pytest import importorskip as optional\noptional("medre.adapters.private_chat")',
        "import medre.adapters\nmedre.adapters.private_chat.Client()",
        'import medre.adapters as adapters\ngetattr(adapters, "private_chat")',
    ],
)
def test_optional_and_namespace_dependencies_are_guarded(tmp_path, code):
    package = tmp_path / "src/medre/adapters"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    test = tmp_path / "tests/test_example.py"
    test.parent.mkdir()
    test.write_text(code)
    assert external_imports(test, tmp_path) == {"medre.adapters.private_chat"}


def test_conftest_dependencies_require_ownership(tmp_path):
    suite = tmp_path / "tests/operational"
    suite.mkdir(parents=True)
    (suite / "conftest.py").write_text(
        "from medre.adapters.private_chat import Client\n"
    )
    assert undeclared_dependencies(tmp_path) == {
        "tests/operational/conftest.py": ["medre.adapters.private_chat"]
    }
