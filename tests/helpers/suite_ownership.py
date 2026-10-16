"""Static test ownership, before pytest imports adapter-dependent modules.

Compatibility modules declare ``# medre-test-tier: compatibility``. All other
modules belong to the adapter-free suite. An import dependency walk prevents
new direct or helper-mediated adapter imports from quietly leaving that suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPATIBILITY = "# medre-test-tier: compatibility"


def is_compatibility(path: Path, root: Path) -> bool:
    """Read module or containing-suite ownership without importing it."""
    files = [path] if path.is_file() else [path / "conftest.py"]
    for directory in [path.parent, *path.parents]:
        if directory == root.parent:
            break
        files.append(directory / "conftest.py")
    return any(
        COMPATIBILITY in file.read_text(encoding="utf-8").splitlines()[:10]
        for file in files
        if file.is_file()
    )


def external_imports(path: Path, root: Path, seen: set[Path] | None = None) -> set[str]:
    """Follow local test imports and identify medre modules absent from core."""
    seen = set() if seen is None else seen
    if path in seen or not path.is_file():
        return set()
    seen.add(path)
    text = path.read_text(encoding="utf-8")
    if COMPATIBILITY in text.splitlines()[:10]:
        return {f"compatibility helper: {path.relative_to(root)}"}
    tree = ast.parse(text)
    imports = set()
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            package = list(path.relative_to(root).parts[:-1])
            prefix = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
            if not module:
                continue
            imports.add(module)
            for alias in node.names:
                bindings[alias.asname or alias.name] = module + "." + alias.name
            if module in {
                "medre.adapters",
                "medre.adapters.fakes",
                "medre.config.adapters",
            } or module.startswith("tests"):
                imports.update(
                    module + "." + alias.name
                    for alias in node.names
                    if alias.name != "*"
                )

    def qualified_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = qualified_name(node.value)
            return parent + "." + node.attr if parent else ""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            parent = qualified_name(node.args[0])
            return parent + "." + node.args[1].value if parent else ""
        return ""

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            function = qualified_name(node.func).rsplit(".", 1)[-1]
            if function in {"import_module", "__import__", "importorskip"}:
                imports.add(node.args[0].value)
        # Namespace attributes can couple a test without an import statement.
        # Stop at real source modules: their class/function attributes are not
        # additional module imports.
        qualified = qualified_name(node)
        for namespace in ("medre.adapters", "medre.config.adapters"):
            if not qualified.startswith(namespace + "."):
                continue
            parts = qualified.split(".")
            for length in range(len(namespace.split(".")) + 1, len(parts) + 1):
                if parts[length - 1].startswith("__"):
                    break
                module = ".".join(parts[:length])
                source = root / "src" / Path(*parts[:length])
                if source.with_suffix(".py").is_file():
                    break
                if not (source / "__init__.py").is_file():
                    imports.add(module)
                    break
    external = set()
    for module in imports:
        if module.startswith("medre."):
            source = root / "src" / Path(*module.split("."))
            if (
                not source.with_suffix(".py").is_file()
                and not (source / "__init__.py").is_file()
            ):
                external.add(module)
        elif module.startswith("tests."):
            source = root / Path(*module.split("."))
            dependency = source.with_suffix(".py")
            if not dependency.is_file():
                dependency = source / "__init__.py"
            external.update(external_imports(dependency, root, seen))
    return external


def undeclared_dependencies(root: Path) -> dict[str, list[str]]:
    """Return accidental coupling that requires an explicit ownership decision."""
    return {
        path.relative_to(root).as_posix(): sorted(dependencies)
        for path in sorted(
            set((root / "tests").rglob("test_*.py"))
            | set((root / "tests").rglob("conftest.py"))
        )
        if not is_compatibility(path, root)
        and (dependencies := external_imports(path, root))
    }
