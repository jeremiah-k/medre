#!/usr/bin/env python3
"""Verify the built medre wheel as a clean, installed package.

This is the permanent installed-package artifact proof. Running

    python scripts/check_installed_package.py

builds the core wheel with the declared build backend (setuptools via the
``build`` frontend, no isolation, after verifying the declared pinned backend
requirement), creates a
throwaway virtual environment, installs ONLY that wheel with its core
dependencies, and then exercises the installed ``medre`` console script
from an isolated working directory with a sanitized environment:

* ``--help`` / ``version`` / ``paths`` / ``adapters``
* ``config sample`` written to a real file and validated with
  ``config check --config <file>``
* ``smoke --config <file> --json`` — full pipeline PASS with delivery
  evidence, persisted SQLite storage inside the sandbox, and a clean
  ``stopped`` shutdown
* ``recover --help`` / ``replay --help`` — recovery CLI contract surfaces

Leakage controls: ``PYTHON*``/``MEDRE_*``/``PIP_*`` and venv ambient
variables are stripped from every child process (names only are reported;
values are never printed), and HOME plus all four XDG roots point into a
self-created temp sandbox that is removed on exit — including on failure
or timeout. The proof fails loudly if the imported ``medre`` package, its
metadata, or the console script come from anywhere other than the fresh
installation, or if any optional transport SDK distribution is present.

``--wheel PATH`` verifies an already-built wheel (e.g. to avoid a second
build) instead of building one; the wheel name must still match this
checkout's ``pyproject.toml`` version.

The wheel build runs in the source checkout (setuptools writes the
gitignored ``build/`` and ``*.egg-info`` trees there); every proof child
runs outside the checkout. POSIX only: the console script is expected at
``<venv>/bin/medre``.

Exit codes: 0 = all proofs passed, 1 = a proof or prerequisite failed.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from importlib.util import find_spec
from pathlib import Path
from typing import Any, NoReturn

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

_BUILD_TIMEOUT_S = 600
_VENV_TIMEOUT_S = 180
_INSTALL_TIMEOUT_S = 900
_CHILD_TIMEOUT_S = 120
_OUTPUT_LIMIT = 4000

# Ambient variable groups stripped from every proof child. MEDRE_* can
# carry configuration and secrets; PYTHON* (PYTHONPATH/PYTHONHOME/...)
# and PIP_* can redirect imports and installs; VIRTUAL_ENV/TMPDIR leak
# the parent interpreter's environment.
_DROP_PREFIXES = ("MEDRE_", "PYTHON", "PIP_")
_DROP_EXACT = frozenset({"VIRTUAL_ENV", "TMPDIR"})

# Requirement-name prefix of a PEP 508 requirement string.  The artifact proof
# needs distribution names only; version/extras/markers remain authoritative in
# ``pyproject.toml`` and are handled by the installer/build checks.
_REQUIREMENT_NAME_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# Child-side probe: runs in the proof venv with ``python -I`` and prints
# distribution/import facts as JSON.
_PROBE_SRC = """\
import importlib.metadata
import json
import sys

import medre

distributions = sorted(
    dist.metadata["Name"]
    for dist in importlib.metadata.distributions()
    if dist.metadata["Name"]
)
print(
    json.dumps(
        {
            "medre_file": medre.__file__,
            "medre_version": importlib.metadata.version("medre"),
            "executable": sys.executable,
            "prefix": sys.prefix,
            "sys_path": sys.path,
            "distributions": distributions,
        }
    )
)
"""

# Child-side probe: parse the generated sample config with the installed
# YAML stack (pyyaml is a core dependency) and print its top-level keys.
_YAML_KEYS_SRC = """\
import json
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as fh:
    data = yaml.safe_load(fh)
print(json.dumps(sorted(data) if isinstance(data, dict) else []))
"""


def _clip(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    """Return *text* trimmed to its trailing *limit* characters."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return f"...[clipped {len(text) - limit} chars]...{text[-limit:]}"


def _as_text(data: str | bytes | None) -> str | None:
    """Coerce captured subprocess output to text."""
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _fail(
    label: str,
    detail: str,
    *,
    command: list[str] | None = None,
    returncode: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> NoReturn:
    """Print proof diagnostics and exit 1. Never prints environment values."""
    print(f"installed-package proof: FAIL [{label}]", file=sys.stderr)
    print(f"  {detail}", file=sys.stderr)
    if command is not None:
        print(f"  command: {shlex.join(command)}", file=sys.stderr)
    if returncode is not None:
        print(f"  exit code: {returncode}", file=sys.stderr)
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        if text:
            print(f"  {stream}: {_clip(text)}", file=sys.stderr)
    sys.exit(1)


def _expect(
    condition: bool,
    label: str,
    detail: str,
    *,
    command: list[str] | None = None,
    proc: subprocess.CompletedProcess[str] | None = None,
) -> None:
    """Fail with diagnostics when *condition* is false."""
    if not condition:
        if command is None and proc is not None:
            args = proc.args
            command = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        _fail(
            label,
            detail,
            command=command,
            returncode=proc.returncode if proc is not None else None,
            stdout=proc.stdout if proc is not None else None,
            stderr=proc.stderr if proc is not None else None,
        )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: int,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run *command*, capturing output; a timeout is a loud failure."""
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _fail(
            label,
            f"command timed out after {timeout}s",
            command=command,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        )


def _normalize(name: str) -> str:
    """PEP 503-normalize a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_project_metadata() -> tuple[str, str, list[str], dict[str, Any]]:
    """Return project identity, build requirements, and parsed metadata."""
    try:
        with _PYPROJECT_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        _fail("metadata", f"cannot read {_PYPROJECT_PATH}: {exc}")
    project = data.get("project", {})
    name = str(project.get("name", ""))
    version = str(project.get("version", ""))
    if name != "medre" or not re.fullmatch(r"\d+(\.\d+)+", version):
        _fail(
            "metadata",
            f"unexpected project metadata: name={name!r} version={version!r}",
        )
    requires = [str(r) for r in data.get("build-system", {}).get("requires", [])]
    return name, version, requires, data


def _declared_distributions(
    data: dict[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return core-required and transport-optional distribution names.

    ``pyproject.toml`` is the only dependency authority.  The ``dev`` extra is
    intentionally excluded from the core-only proof because it is build/test
    tooling, not a transport SDK surface.
    """

    def _names(requirements: object, *, field: str) -> set[str]:
        if not isinstance(requirements, list):
            _fail("metadata", f"pyproject.toml {field} must be a list")

        names: set[str] = set()
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, str):
                _fail(
                    "metadata",
                    f"pyproject.toml {field}[{index}] must be a requirement string",
                )
            match = _REQUIREMENT_NAME_RE.match(requirement)
            if match is None:
                _fail(
                    "metadata",
                    f"pyproject.toml {field}[{index}] has no distribution name",
                )
            names.add(_normalize(match.group(1)))
        return names

    project = data.get("project", {})
    if not isinstance(project, dict):
        _fail("metadata", "pyproject.toml [project] must be a table")

    core = {_normalize(str(project.get("name", "")))}
    core.update(
        _names(project.get("dependencies", []), field="project.dependencies")
    )

    optional: set[str] = set()
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        _fail(
            "metadata",
            "pyproject.toml [project.optional-dependencies] must be a table",
        )
    for extra, requirements in extras.items():
        names = _names(
            requirements,
            field=f"project.optional-dependencies.{extra}",
        )
        if str(extra) == "dev":
            continue
        optional.update(names)

    return frozenset(core), frozenset(optional - core)


def _verify_build_requirements(requires: list[str]) -> None:
    """Verify exact ``build-system.requires`` pins for ``--no-isolation``.

    The artifact proof intentionally disables build isolation, so the active
    interpreter's build-backend distributions must match the exact versions
    declared by this checkout.  A non-exact declaration is rejected rather
    than silently claiming a pinned-tooling proof.
    """
    for requirement in requires:
        match = re.fullmatch(
            r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^;\s]+)\s*",
            requirement,
        )
        if match is None:
            _fail(
                "prerequisite",
                "build-system requirement must be an exact pin for the "
                f"--no-isolation proof: {requirement!r}",
            )
        name, expected = match.groups()
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            _fail(
                "prerequisite",
                f"build requirement {name!r} is not installed; install this "
                "checkout's build tooling first, e.g. pip install -e '.[dev]'",
            )
        if installed != expected:
            _fail(
                "prerequisite",
                f"build requirement {name!r} is {installed!r}, but pyproject.toml "
                f"declares {expected!r}; --no-isolation would use the wrong "
                "backend tooling",
            )


def _clear_source_build_artifacts() -> None:
    """Remove stale setuptools build outputs before a source wheel build."""
    shutil.rmtree(_REPO_ROOT / "build", ignore_errors=True)
    for egg_info in (_REPO_ROOT / "src").glob("*.egg-info"):
        shutil.rmtree(egg_info, ignore_errors=True)


def _resolve_wheel(
    wheel_arg: str | None,
    out_dir: Path,
    version: str,
    build_requires: list[str],
) -> Path:
    """Build the core wheel, or accept an explicit --wheel path."""
    if wheel_arg is not None:
        wheel = Path(wheel_arg).resolve()
        _expect(
            wheel.is_file(),
            "input",
            f"--wheel path does not exist: {wheel}",
        )
    else:
        missing = [m for m in ("build", "setuptools") if find_spec(m) is None]
        if missing:
            _fail(
                "prerequisite",
                f"module(s) {missing} are required to build the wheel with "
                "the declared backend; install this checkout's build "
                "tooling first, e.g. pip install -e '.[dev]'",
            )
        _verify_build_requirements(build_requires)
        _clear_source_build_artifacts()
        command = [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out_dir),
        ]
        proc = _run(
            command,
            cwd=_REPO_ROOT,
            env=None,
            timeout=_BUILD_TIMEOUT_S,
            label="build",
        )
        _expect(
            proc.returncode == 0,
            "build",
            "wheel build failed",
            command=command,
            proc=proc,
        )
        wheels = sorted(out_dir.glob("medre-*.whl"))
        _expect(
            len(wheels) == 1,
            "build",
            f"expected exactly one medre wheel in {out_dir}, found "
            f"{[w.name for w in wheels]}",
            command=command,
            proc=proc,
        )
        wheel = wheels[0]
    _expect(
        wheel.name.startswith(f"medre-{version}-") and wheel.name.endswith(".whl"),
        "wheel",
        f"wheel name {wheel.name!r} does not match this checkout's version "
        f"{version!r}; refusing a stale or foreign artifact",
    )
    return wheel


def _stripped_names() -> list[str]:
    """Names of ambient variables that will be stripped from children."""
    return sorted(
        name
        for name in os.environ
        if name.startswith(_DROP_PREFIXES) or name in _DROP_EXACT
    )


def _child_env(home: Path, tmp: Path, xdg: dict[str, Path]) -> dict[str, str]:
    """Build the sanitized environment for every proof child process."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_DROP_PREFIXES) and key not in _DROP_EXACT
    }
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            **{key: str(path) for key, path in xdg.items()},
        }
    )
    return env


def main(argv: list[str] | None = None) -> int:
    """Run the installed-package proof; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="check_installed_package",
        description=(
            "Build the core wheel, install it into an isolated venv, and "
            "prove the installed console script's public behavior."
        ),
    )
    parser.add_argument(
        "--wheel",
        metavar="PATH",
        default=None,
        help="verify this already-built wheel instead of building one",
    )
    args = parser.parse_args(argv)

    _name, version, build_requires, project_data = _load_project_metadata()
    required_distributions, forbidden_distributions = _declared_distributions(
        project_data
    )

    sandbox = Path(tempfile.mkdtemp(prefix="medre-installed-proof-"))
    try:
        home = sandbox / "home"
        work = sandbox / "work"
        tmp = sandbox / "tmp"
        wheel_dir = sandbox / "wheel"
        venv_dir = sandbox / "venv"
        xdg = {
            "XDG_CONFIG_HOME": sandbox / "xdg" / "config",
            "XDG_STATE_HOME": sandbox / "xdg" / "state",
            "XDG_DATA_HOME": sandbox / "xdg" / "data",
            "XDG_CACHE_HOME": sandbox / "xdg" / "cache",
        }
        for directory in (home, work, tmp, wheel_dir, *xdg.values()):
            directory.mkdir(parents=True)

        wheel = _resolve_wheel(args.wheel, wheel_dir, version, build_requires)

        venv_python = venv_dir / "bin" / "python"
        console = venv_dir / "bin" / "medre"
        venv_command = [sys.executable, "-m", "venv", str(venv_dir)]
        proc = _run(
            venv_command,
            cwd=_REPO_ROOT,
            env=None,
            timeout=_VENV_TIMEOUT_S,
            label="venv",
        )
        _expect(
            proc.returncode == 0 and venv_python.is_file(),
            "venv",
            "temporary venv creation failed",
            command=venv_command,
            proc=proc,
        )

        env = _child_env(home, tmp, xdg)
        stripped = _stripped_names()
        print(f"proof sandbox: {sandbox}")
        print(f"wheel:         {wheel.name}")
        if stripped:
            print(f"stripped ambient env vars (names only): {', '.join(stripped)}")

        install_command = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            # --isolated ignores pip env vars and pip.conf files on top of
            # the stripped/sandboxed environment, so an inherited
            # PIP_TARGET/PIP_PREFIX/PIP_USER or config file cannot redirect
            # the install out of the proof venv.
            "--isolated",
            "--no-cache-dir",
            str(wheel),
        ]
        proc = _run(
            install_command,
            cwd=work,
            env=env,
            timeout=_INSTALL_TIMEOUT_S,
            label="install",
        )
        _expect(
            proc.returncode == 0,
            "install",
            "core wheel install failed — no fallback to an editable or "
            "source install is permitted",
            command=install_command,
            proc=proc,
        )
        _expect(
            console.is_file(),
            "install",
            "console script 'medre' missing from the venv bin directory",
            command=install_command,
        )
        probe_command = [str(venv_python), "-I", "-c", _PROBE_SRC]
        proc = _run(
            probe_command,
            cwd=work,
            env=env,
            timeout=_CHILD_TIMEOUT_S,
            label="provenance",
        )
        _expect(
            proc.returncode == 0,
            "provenance",
            "medre import probe failed in the proof venv",
            command=probe_command,
            proc=proc,
        )
        try:
            probe = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _fail(
                "provenance",
                "probe did not emit parsable JSON",
                command=probe_command,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )

        medre_file = Path(str(probe["medre_file"])).resolve()
        venv_resolved = venv_dir.resolve()
        _expect(
            venv_resolved in medre_file.parents and "site-packages" in medre_file.parts,
            "provenance",
            f"imported medre package is not the fresh installation: " f"{medre_file}",
        )
        _expect(
            str(probe["medre_version"]) == version,
            "provenance",
            f"installed metadata version {probe['medre_version']!r} does "
            f"not match pyproject version {version!r}",
        )

        repo_resolved = _REPO_ROOT.resolve()
        leaked_paths = []
        for entry in probe["sys_path"]:
            if not entry:
                continue
            candidate = Path(entry)
            candidate = candidate.resolve() if candidate.exists() else candidate
            if candidate == repo_resolved or repo_resolved in candidate.parents:
                leaked_paths.append(entry)
        _expect(
            not leaked_paths,
            "provenance",
            f"source checkout leaked into the child sys.path: {leaked_paths}",
        )

        installed = {_normalize(name) for name in probe["distributions"]}
        missing = sorted(required_distributions - installed)
        _expect(
            not missing,
            "install",
            f"core distributions missing from the proof venv: {missing}",
        )
        sdk_present = sorted(forbidden_distributions & installed)
        _expect(
            not sdk_present,
            "install",
            "optional transport SDK distributions present in the core-only "
            f"proof venv: {sdk_present}",
        )

        def run_cli(*arguments: str, label: str) -> subprocess.CompletedProcess[str]:
            return _run(
                [str(console), *arguments],
                cwd=work,
                env=env,
                timeout=_CHILD_TIMEOUT_S,
                label=label,
            )

        # -- help / version ----------------------------------------------
        proc = run_cli("--help", label="help")
        _expect(
            proc.returncode == 0 and "usage:" in proc.stdout.lower(),
            "help",
            "medre --help did not exit 0 with usage text",
            proc=proc,
        )

        proc = run_cli("version", label="version")
        first_line = (proc.stdout.splitlines() or [""])[0].strip()
        _expect(
            proc.returncode == 0 and first_line == f"medre {version}",
            "version",
            f"'medre version' first line was {first_line!r}, expected "
            f"'medre {version}'",
            proc=proc,
        )

        # -- paths ---------------------------------------------------------
        proc = run_cli("paths", label="paths")
        _expect(
            proc.returncode == 0
            and "XDG" in proc.stdout
            and str(xdg["XDG_STATE_HOME"]) in proc.stdout,
            "paths",
            "resolved paths do not reflect the isolated XDG_STATE_HOME",
            proc=proc,
        )

        # -- adapters -------------------------------------------------------
        # SDK absence itself is proven by the distribution-facts probe above;
        # here the command just has to run and enumerate every transport.
        proc = run_cli("adapters", label="adapters")
        _expect(
            proc.returncode == 0
            and all(
                t in proc.stdout for t in ("matrix", "meshtastic", "meshcore", "lxmf")
            ),
            "adapters",
            "'medre adapters' did not enumerate all four transports",
            proc=proc,
        )

        # -- config sample → real file → config check ----------------------
        proc = run_cli("config", "sample", label="config sample")
        _expect(
            proc.returncode == 0 and "adapters:" in proc.stdout,
            "config sample",
            "'medre config sample' produced no adapter configuration",
            proc=proc,
        )
        sample_path = work / "sample.yaml"
        sample_path.write_text(proc.stdout, encoding="utf-8")

        yaml_command = [
            str(venv_python),
            "-I",
            "-c",
            _YAML_KEYS_SRC,
            str(sample_path),
        ]
        proc = _run(
            yaml_command,
            cwd=work,
            env=env,
            timeout=_CHILD_TIMEOUT_S,
            label="config sample",
        )
        _expect(
            proc.returncode == 0,
            "config sample",
            "sample config did not parse as YAML in the installed " "environment",
            command=yaml_command,
            proc=proc,
        )
        keys = set(json.loads(proc.stdout))
        _expect(
            {"runtime", "adapters", "routes", "storage", "logging"} <= keys,
            "config sample",
            f"sample config top-level keys missing required sections: "
            f"{sorted(keys)}",
        )

        proc = run_cli(
            "config",
            "check",
            "--config",
            str(sample_path),
            label="config check",
        )
        _expect(
            proc.returncode == 0,
            "config check",
            "'medre config check' did not validate the generated sample",
            proc=proc,
        )
        _expect(
            "matrix_radio_bridge" in proc.stdout,
            "config check",
            "active sample route missing from the config check inventory",
            proc=proc,
        )

        # -- smoke ----------------------------------------------------------
        proc = run_cli(
            "smoke",
            "--config",
            str(sample_path),
            "--json",
            label="smoke",
        )
        _expect(
            proc.returncode == 0,
            "smoke",
            "'medre smoke' exited nonzero against the sample config",
            proc=proc,
        )
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _fail(
                "smoke",
                "smoke output was not parsable JSON",
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        _expect(
            report.get("status") == "passed",
            "smoke",
            f"smoke status={report.get('status')!r} "
            f"fail_reasons={report.get('fail_reasons')}",
            proc=proc,
        )
        _expect(bool(report.get("event_id")), "smoke", "no event_id in report")
        receipts = report.get("delivery_receipts") or []
        _expect(
            any(r.get("status") == "sent" for r in receipts),
            "smoke",
            "no delivery receipt with status 'sent'",
        )
        accounting = report.get("accounting") or {}
        _expect(
            accounting.get("outbound_delivered", 0) >= 1,
            "smoke",
            f"accounting outbound_delivered < 1: {accounting}",
        )
        _expect(
            report.get("shutdown_status") == "stopped",
            "smoke",
            f"runtime did not reach 'stopped': {report.get('shutdown_status')!r}",
        )
        _expect(
            "radio" in (report.get("target_adapters") or []),
            "smoke",
            f"expected delivery to adapter 'radio': {report.get('target_adapters')}",
        )
        _expect(
            report.get("storage_backend") == "sqlite",
            "smoke",
            f"expected sqlite storage backend: {report.get('storage_backend')!r}",
        )
        storage_path = report.get("storage_path")
        sandbox_resolved = sandbox.resolve()
        _expect(
            isinstance(storage_path, str)
            and sandbox_resolved in Path(storage_path).resolve().parents,
            "smoke",
            f"smoke storage path outside the proof sandbox: {storage_path!r}",
        )

        # -- default-discovery smoke -----------------------------------------
        # Place the generated sample at the discovered default config
        # location inside the sandbox, then run bare ``medre smoke --json``:
        # the default Docker-free smoke for wheel users (no example configs
        # exist outside the checkout), with the same delivery-evidence
        # contract as the explicit-config run.
        discovered_dir = xdg["XDG_CONFIG_HOME"] / "medre"
        discovered_dir.mkdir(parents=True, exist_ok=True)
        (discovered_dir / "config.yaml").write_text(
            sample_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        proc = run_cli("smoke", "--json", label="default smoke")
        _expect(
            proc.returncode == 0,
            "default smoke",
            "'medre smoke --json' with the discovered default config " "exited nonzero",
            proc=proc,
        )
        try:
            default_report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            _fail(
                "default smoke",
                "default smoke output was not parsable JSON",
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        _expect(
            default_report.get("status") == "passed"
            and default_report.get("shutdown_status") == "stopped"
            and any(
                r.get("status") == "sent"
                for r in default_report.get("delivery_receipts") or []
            ),
            "default smoke",
            "default smoke lacked delivery/stopped evidence: status="
            f"{default_report.get('status')!r} shutdown="
            f"{default_report.get('shutdown_status')!r}",
            proc=proc,
        )

        # -- recovery/replay CLI contract boundaries -----------------------
        proc = run_cli("recover", "--help", label="recover help")
        _expect(
            proc.returncode == 0
            and "--storage-path" in proc.stdout
            and "--failed-only" not in proc.stdout
            and "--dry-run" not in proc.stdout,
            "recover help",
            "recover help must require --storage-path and must not offer "
            "--failed-only or --dry-run",
            proc=proc,
        )
        proc = run_cli("replay", "--help", label="replay help")
        _expect(
            proc.returncode == 0
            and "--mode" in proc.stdout
            and "dry_run" in proc.stdout
            and "--storage-path" not in proc.stdout,
            "replay help",
            "replay help must offer --mode with dry_run and must not take "
            "--storage-path",
            proc=proc,
        )

        print("installed-package proof: PASS")
        print(f"  medre package:   {medre_file}")
        print(f"  version:         {probe['medre_version']}")
        print("  distributions:   " + ", ".join(sorted(installed)))
        print(
            "  proofs:          help, version, paths, adapters, config "
            "sample, config check, smoke (explicit config), smoke "
            "(discovered default), recover --help, replay --help"
        )
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
