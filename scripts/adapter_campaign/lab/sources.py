"""Explicit checkout selection and auditable, credential-free build snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from medre.dev_environment import DevEnvironment

from ..plan import TRANSPORTS
from . import LabError


def selected_sources(core: Path, profile: Path, mmrelay: Path) -> dict[str, Path]:
    env = DevEnvironment.inspect(
        argparse.Namespace(
            project=str(core),
            profile=str(profile),
            sources=None,
            sdk=True,
            editable=[],
            adapters=None,
        )
    )
    aliases = [selection.split("[", 1)[0] for selection in env.adapters]
    if set(aliases) != set(env.editable):
        raise LabError(
            "The lab requires explicit editable paths for every selected adapter"
        )
    if set(aliases) != TRANSPORTS:
        raise LabError(
            "Select all seven campaign adapters with explicit editable paths"
        )
    return {"medre": env.root, "mmrelay": mmrelay.resolve(), **env.editable}


def snapshot(path: Path, destination: Path, *, allow_dirty: bool = False) -> dict:
    """Copy tracked worktree bytes, never a checkout's untracked state or venv."""

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(path), *args], timeout=30)

    if Path(git("rev-parse", "--show-toplevel").decode().strip()) != path.resolve():
        raise ValueError("Each source must name its checkout root")
    changes = git("status", "--porcelain", "--untracked-files=no").decode()
    revision = git("rev-parse", "HEAD").decode().strip()
    if changes and not allow_dirty:
        raise LabError("Commit source changes or explicitly use --allow-dirty")
    destination.mkdir(parents=True)
    digest = hashlib.sha256()
    count = 0
    for encoded in git("ls-files", "-z").split(b"\0"):
        if not encoded:
            continue
        relative = Path(encoded.decode())
        source = path / relative
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid tracked source path")
        if not source.resolve().is_relative_to(path.resolve()) or source.is_symlink():
            raise ValueError("Build snapshots do not follow source symlinks")
        if not source.is_file():
            raise ValueError("Build snapshots require complete tracked files")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        target.write_bytes(content)
        shutil.copymode(source, target)
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(content).to_bytes(8, "big") + content)
        digest.update((source.stat().st_mode & 0o111).to_bytes(2, "big"))
        count += 1
    if (
        git("rev-parse", "HEAD").decode().strip() != revision
        or git("status", "--porcelain", "--untracked-files=no").decode() != changes
    ):
        raise LabError(
            "Source checkout changed during snapshot; retry in a fresh directory"
        )
    return {
        "checkout": str(path),
        "commit": revision,
        "dirty": bool(changes),
        "snapshot_sha256": digest.hexdigest(),
        "files": count,
    }


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
