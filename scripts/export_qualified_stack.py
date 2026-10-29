#!/usr/bin/env python3
"""Offline, fail-closed export of core and exact immutable provider pin objects.

Unlike source-checkout exports, this uses core's compatibility manifest as the
sole provider authority. The output includes Git history, not private runtime
state. Git history may itself contain secrets; protect the artifact accordingly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ci.adapter_matrix import resolve_adapters


def _git(directory: Path, *args: str, timeout: float = 90) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    process = subprocess.run(
        [
            "git",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-C",
            str(directory),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return process.stdout.strip()


def _require_checkout(path: Path, pin: str) -> tuple[str, str]:
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"Repository directory missing or unsafe: {path.name}")
    if Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
        raise ValueError(f"Not an independent checkout: {path.name}")
    if _git(path, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError(f"Checkout is dirty: {path.name}")
    try:
        object_type = _git(path, "cat-file", "-t", pin)
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"Required pinned commit missing or unreadable: {path.name} {pin}: "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    if object_type != "commit":
        raise ValueError(f"Required pinned object is not a commit: {path.name} {pin}")
    return _git(path, "rev-parse", "HEAD"), _git(path, "rev-parse", f"{pin}^{{tree}}")


def _sha256_file(path: Path) -> str:
    """Hash reachable history with bounded memory regardless of bundle size."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def export(
    source: Path, output: Path, *, git_timeout: float = 600
) -> dict[str, object]:
    """Publish only after all pinned objects and independent bundles verify."""
    if not math.isfinite(git_timeout) or git_timeout <= 0:
        raise ValueError("Git operation timeout must be finite and positive")
    source, output = source.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output directory already exists; refusing overwrite")
    core = source / "medre"
    core_head = _git(core, "rev-parse", "HEAD")
    # Checkout status can hide edits (for example assume-unchanged). Select
    # providers from the exact core commit that the archive will reproduce.
    with tempfile.TemporaryDirectory(prefix="medre-pin-manifest-") as scratch:
        manifest = Path(scratch) / "manifest.json"
        manifest.write_text(
            _git(core, "show", f"{core_head}:.github/adapter-compatibility.json"),
            encoding="utf-8",
        )
        pins = resolve_adapters(manifest, resolve=False)
    if any(re.fullmatch(r"[0-9a-f]{40}", item["ref"]) is None for item in pins):
        raise ValueError(
            "Qualified exports require full immutable provider commit SHAs"
        )
    entries = [("medre", "core", core_head)] + [
        (item["repository"].split("/")[-1], item["transport"], item["ref"])
        for item in pins
    ]
    if len({name for name, _, _ in entries}) != len(entries):
        raise ValueError("Duplicate repository in core compatibility manifest")
    observations = [
        (name, transport, pin, *_require_checkout(source / name, pin))
        for name, transport, pin in entries
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="medre-pins-", dir=output.parent) as tmp:
        stage = Path(tmp) / "release"
        stage.mkdir(mode=0o700)
        bundles = stage / "bundles"
        bundles.mkdir(mode=0o700)
        records = []
        for name, transport, pin, checkout, tree in observations:
            with tempfile.TemporaryDirectory(prefix="medre-pin-git-") as scratch:
                bare = Path(scratch) / "bare.git"
                bare.mkdir()
                _git(bare, "init", "--bare", "--template=")
                # Only fetch the exact pinned commit into an isolated Git store.
                _git(
                    bare,
                    "fetch",
                    "--no-tags",
                    str(source / name),
                    pin,
                    timeout=git_timeout,
                )
                _git(bare, "update-ref", "refs/heads/qualified", pin)
                archive = bundles / f"{name}.bundle"
                _git(
                    bare,
                    "bundle",
                    "create",
                    str(archive),
                    "refs/heads/qualified",
                    timeout=git_timeout,
                )
                with tempfile.TemporaryDirectory(prefix="medre-pin-check-") as check:
                    isolated = Path(check) / "verify.git"
                    isolated.mkdir()
                    _git(isolated, "init", "--bare", "--template=")
                    _git(
                        isolated, "bundle", "verify", str(archive), timeout=git_timeout
                    )
                    _git(
                        isolated,
                        "fetch",
                        str(archive),
                        "refs/heads/qualified",
                        timeout=git_timeout,
                    )
                    if _git(isolated, "rev-parse", "FETCH_HEAD") != pin:
                        raise ValueError(f"Bundle does not reproduce exact pin: {name}")
                    if _git(isolated, "rev-parse", "FETCH_HEAD^{tree}") != tree:
                        raise ValueError(f"Bundle tree differs: {name}")
            records.append(
                {
                    "repository": name,
                    "transport": transport,
                    "pinned_commit": pin,
                    "pinned_tree": tree,
                    "checkout_head": checkout,
                    "checkout_matches_pin": checkout == pin,
                    "bundle": archive.relative_to(stage).as_posix(),
                    "sha256": _sha256_file(archive),
                }
            )
        result: dict[str, object] = {
            "format": "medre-qualified-source-export",
            "version": 1,
            "core_commit": core_head,
            "repositories": records,
            "provider_sdk_wheels_included": False,
            "runtime_profiles_included": False,
            "native_or_hardware_qualified": False,
        }
        (stage / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
        # rename refuses a competing nonempty destination instead of nesting
        # the release inside it, as shutil.move would do.
        if output.exists() or output.is_symlink():
            raise FileExistsError("Output directory appeared during export")
        stage.rename(output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", type=Path, help="Directory containing medre and adapter checkouts"
    )
    parser.add_argument(
        "output", type=Path, help="New private directory; must not exist"
    )
    parser.add_argument(
        "--git-timeout",
        type=float,
        default=600,
        help="Timeout in seconds for each Git fetch/bundle operation (default: 600)",
    )
    args = parser.parse_args()
    try:
        manifest = export(args.source, args.output, git_timeout=args.git_timeout)
    except subprocess.CalledProcessError as exc:
        parser.exit(
            2, f"Qualified export refused: {exc}\n{(exc.stderr or '').strip()}\n"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Qualified export refused: {type(exc).__name__}: {exc}\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
