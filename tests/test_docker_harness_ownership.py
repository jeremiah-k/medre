"""Docker harness ownership: test containers never create root-owned host paths.

A container the harness starts either runs as the invoking user or is an
explicitly listed exception whose only writable storage is Docker-managed.
Root-mode containers are never used, the meshtasticd node covers the volume
its image declares, and every container removal also removes anonymous
volumes.  These are source properties of the harness, so they hold in every
lane, including the lanes that never start Docker.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

#: User specifications that would run a container as root.
ROOT_USERS = frozenset({"root", "0", "0:0"})

#: Image constants allowed to start without a host-user pin, each with the
#: reason.  Every such run must still pass the writable-storage rule below.
UNPINNED_IMAGES = {
    "_PROSODY_IMAGE": (
        "the image entrypoint starts as root and demotes the daemon itself; "
        "its state lives in a Docker-managed named volume"
    ),
}

#: Volume path the pinned meshtasticd image declares.  A tmpfs over it stops
#: Docker from creating an anonymous, root-owned volume for every session.
MESHTASTICD_DECLARED_VOLUME = "/var/lib/meshtasticd"

_BIND_FLAGS = frozenset({"-v", "--volume", "--mount"})


def _string_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` assignments of one harness file."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def _literal(node: ast.expr, constants: dict[str, str]) -> str:
    """Concatenate the literal fragments of one argv element.

    A plain module-level string constant is read as its value, so a path
    referenced by name is still visible to the rules.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id, "")
    if isinstance(node, ast.FormattedValue):
        return _literal(node.value, constants)
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal(part, constants) for part in node.values)
    return ""


class _Argv:
    """One ``docker`` argv built as a list or tuple literal in the harness."""

    def __init__(
        self,
        path: Path,
        node: ast.List | ast.Tuple,
        constants: dict[str, str],
    ) -> None:
        self.path = path
        self.line = node.lineno
        self.constants = constants
        self.elements: list[ast.expr] = list(node.elts)
        self.literals: list[str] = [
            _literal(element, constants) for element in self.elements
        ]

    @property
    def id(self) -> str:
        return f"{self.path.name}:{self.line}"

    def values_after(self, flag: str) -> list[str]:
        return [
            self.literals[index + 1]
            for index, value in enumerate(self.literals[:-1])
            if value == flag
        ]

    def image_constants(self) -> set[str]:
        return {
            element.id
            for element in self.elements
            if isinstance(element, ast.Name) and element.id.endswith("_IMAGE")
        }

    def bind_specs(self) -> list[str]:
        return [
            self.literals[index + 1]
            for index, value in enumerate(self.literals[:-1])
            if value in _BIND_FLAGS
        ]


def _is_run_argv(literals: list[str], node: ast.List | ast.Tuple) -> bool:
    return "run" in literals and any(
        isinstance(element, ast.Name) and element.id.endswith("_IMAGE")
        for element in node.elts
    )


def _is_removal_argv(literals: list[str]) -> bool:
    return bool(literals) and literals[0] == "rm"


def _argvs() -> list[_Argv]:
    """Collect every docker run and container-removal argv under ``tests/``."""
    found: list[_Argv] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if '"run"' not in text and '"rm"' not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        constants = _string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            argv = _Argv(path, node, constants)
            if _is_run_argv(argv.literals, node) or _is_removal_argv(argv.literals):
                found.append(argv)
    return found


_ARGVS = _argvs()
_RUN_ARGVS = [argv for argv in _ARGVS if "run" in argv.literals]
_REMOVAL_ARGVS = [argv for argv in _ARGVS if argv not in _RUN_ARGVS]
_UNPINNED_RUN_ARGVS = [argv for argv in _RUN_ARGVS if "--user" not in argv.literals]
_MESHTASTICD_RUN_ARGVS = [
    argv for argv in _RUN_ARGVS if "_MESHTASTICD_IMAGE" in argv.image_constants()
]


def _ids(argvs: list[_Argv]) -> list[str]:
    return [argv.id for argv in argvs]


def _read_only_or_docker_managed(spec: str) -> bool:
    """Whether a bind leaves no writable path on the host.

    A read-only bind never writes.  Otherwise the source is the part before
    the first ``:``; a source with no literal fragment is a bare variable
    read as a Docker-managed volume name, while a host path always carries a
    literal ``/``.
    """
    if spec.endswith(":ro") or "readonly" in spec:
        return True
    return spec.startswith(":")


def test_harness_docker_runs_are_discovered() -> None:
    """The scanner sees every container the harness starts."""
    images = {image for argv in _RUN_ARGVS for image in argv.image_constants()}
    assert {"_SYNAPSE_IMAGE", "_MESHTASTICD_IMAGE", "_PROSODY_IMAGE"} <= images
    assert _REMOVAL_ARGVS, "the harness removes its containers"


@pytest.mark.parametrize("argv", _RUN_ARGVS, ids=_ids(_RUN_ARGVS))
def test_no_root_mode_container(argv: _Argv) -> None:
    """No harness container runs as root or with elevated privileges."""
    assert "--privileged" not in argv.literals
    for user in argv.values_after("--user"):
        assert user not in ROOT_USERS


@pytest.mark.parametrize("argv", _RUN_ARGVS, ids=_ids(_RUN_ARGVS))
def test_run_is_pinned_to_the_host_user(argv: _Argv) -> None:
    """Every run carries ``--user`` unless its image is a listed exception."""
    if "--user" in argv.literals:
        assert argv.values_after("--user") != []
        return
    unpinned = argv.image_constants() & UNPINNED_IMAGES.keys()
    assert unpinned, (
        f"{argv.id} starts {sorted(argv.image_constants())} without --user; "
        "pin the invoking user or list the image in UNPINNED_IMAGES"
    )


@pytest.mark.parametrize("argv", _UNPINNED_RUN_ARGVS, ids=_ids(_UNPINNED_RUN_ARGVS))
def test_unpinned_run_receives_no_writable_host_path(argv: _Argv) -> None:
    """A listed exception only receives read-only or Docker-managed storage."""
    writable = [
        spec for spec in argv.bind_specs() if not _read_only_or_docker_managed(spec)
    ]
    assert (
        not writable
    ), f"{argv.id} runs without --user but bind-mounts a writable host path"


@pytest.mark.parametrize(
    "argv", _MESHTASTICD_RUN_ARGVS, ids=_ids(_MESHTASTICD_RUN_ARGVS)
)
def test_meshtasticd_run_covers_the_declared_volume(argv: _Argv) -> None:
    """A tmpfs over the image's declared volume creates no anonymous volume."""
    assert any(
        spec.split(":", 1)[0] == MESHTASTICD_DECLARED_VOLUME
        for spec in argv.values_after("--tmpfs")
    ), f"{argv.id} leaves {MESHTASTICD_DECLARED_VOLUME} to Docker's anonymous volume"


@pytest.mark.parametrize("argv", _REMOVAL_ARGVS, ids=_ids(_REMOVAL_ARGVS))
def test_container_removal_removes_anonymous_volumes(argv: _Argv) -> None:
    """Removing a container never strands an anonymous volume behind it."""
    assert "-v" in argv.literals, f"{argv.id} removes a container without -v"
