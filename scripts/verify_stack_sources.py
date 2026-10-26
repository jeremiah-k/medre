"""Audit exact provider Git objects against an exported source manifest.

Usage: python scripts/verify_stack_sources.py ROOT [--require-pinned-heads]
ROOT contains manifest.tsv and sibling repository checkouts. Missing pinned
objects fail even if the current checkout looks plausible. A checkout newer
than the configured pin is reported separately and is not silently adopted.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.ci.adapter_matrix import resolve_adapters

_SHA = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class _ExportedSource:
    """Bind a recorded checkout revision to the artifact that must reproduce it."""

    commit: str
    bundle: str


def _git(repo: Path, *args: str, checkout_config: bool = False) -> str:
    """Run a bounded local query, isolating artifact proof from checkout policy.

    Only working-tree status respects the user's normalization configuration.
    Bundle object verification always excludes ambient configuration and objects.
    """
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    if checkout_config:
        for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            if key in os.environ:
                environment[key] = os.environ[key]
    else:
        environment.update(
            {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        )
    result = subprocess.run(
        [
            "git",
            "-c",
            "protocol.allow=never",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=environment,
    )
    return result.stdout.strip()


def _exported_refs(root: Path) -> dict[str, _ExportedSource]:
    """Reject ambiguous TSV rows before any repository paths are constructed."""
    with (root / "manifest.tsv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.reader(source, delimiter="\t"))
    if not rows or rows[0] != ["repo", "bundle", "sha", "branch"]:
        raise ValueError("source manifest has an invalid header")
    refs: dict[str, _ExportedSource] = {}
    for row in rows[1:]:
        if (
            len(row) != 4
            or not _NAME.fullmatch(row[0])
            or not _SHA.fullmatch(row[2])
            or not all(row)
            or row[0] in refs
        ):
            raise ValueError("source manifest contains invalid or duplicate entries")
        bundle = Path(row[1])
        if (
            bundle.is_absolute()
            or "\\" in row[1]
            or ":" in row[1]
            or any(part in {"", ".", ".."} for part in row[1].split("/"))
            or not (root / bundle).resolve().is_relative_to(root)
        ):
            raise ValueError("bundle path must remain within the source export")
        refs[row[0]] = _ExportedSource(row[2], row[1])
    if "medre" not in refs:
        raise ValueError("source manifest must identify the core checkout")
    return refs


def _verify_bundle(
    root: Path, source: _ExportedSource, pin: str
) -> tuple[bool, str | None]:
    """Prove offline object closure in an empty store, never borrowing checkout objects.

    Verification rejects incremental prerequisites, corrupt packs, absent required
    commits and missing reachable trees/blobs. No transport or ambient Git object
    configuration is available to repair an incomplete artifact.
    """
    bundle = root / source.bundle
    if not bundle.is_file():
        return False, "missing_bundle"
    phase = "invalid_or_incremental_bundle"
    try:
        with tempfile.TemporaryDirectory(prefix="medre-bundle-check-") as directory:
            isolated = Path(directory)
            _git(isolated, "init", "--bare", "--template=")
            _git(isolated, "bundle", "verify", str(bundle))
            phase = "invalid_bundle_pack"
            _git(isolated, "bundle", "unbundle", str(bundle))
            phase = "missing_required_commit"
            commits = sorted({source.commit, pin})
            for commit in commits:
                if _git(isolated, "cat-file", "-t", commit) != "commit":
                    return False, phase
            phase = "incomplete_object_graph"
            _git(
                isolated,
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--no-dangling",
                *commits,
            )
    except (OSError, subprocess.SubprocessError):
        return False, phase
    return True, None


def _inspect_repository(
    root: Path, name: str, exported: _ExportedSource, pin: str, transport: str
) -> dict[str, object]:
    """Keep checkout provenance separate from exact pinned-object availability."""
    checkout = root / name
    head = None
    clean = False
    available = False
    try:
        if (
            Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
            != checkout.resolve()
        ):
            raise ValueError("source checkout is not an independent repository")
        head = _git(checkout, "rev-parse", "HEAD")
        clean = not _git(
            checkout,
            "status",
            "--porcelain",
            "--untracked-files=normal",
            checkout_config=True,
        )
        # An annotated tag that peels to a commit is not the pinned commit object.
        available = _git(checkout, "cat-file", "-t", pin) == "commit"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    bundle_verified, bundle_error = _verify_bundle(root, exported, pin)
    return {
        "transport": transport,
        "repository": name,
        "pinned_ref": pin,
        "exported_ref": exported.commit,
        "bundle": exported.bundle,
        "bundle_verified": bundle_verified,
        "bundle_error": bundle_error,
        "checkout_ref": head,
        "pinned_object_present": available,
        "checkout_clean": clean,
        "checkout_at_pinned_ref": head == pin,
        "checkout_at_exported_ref": head == exported.commit,
    }


def inspect_stack(root: Path) -> list[dict[str, object]]:
    """Verify a nonempty core-owned pin set and every recorded source checkout."""
    root = Path(root).resolve()
    exported = _exported_refs(root)
    compatibility = resolve_adapters(
        root / "medre/.github/adapter-compatibility.json", resolve=False
    )
    if any(not _SHA.fullmatch(pin["ref"]) for pin in compatibility):
        raise ValueError("source verification requires exact commit pins")
    selected = {"medre": (exported["medre"].commit, "core")}
    for pin in compatibility:
        name = pin["repository"].rsplit("/", 1)[-1]
        if name in selected or name not in exported:
            raise ValueError("adapter compatibility pin missing or ambiguous in export")
        selected[name] = (pin["ref"], pin["transport"])
    # Verify all recorded repositories, including any additional exported source.
    return [
        _inspect_repository(root, name, ref, *selected.get(name, (ref.commit, name)))
        for name, ref in exported.items()
    ]


def main(argv: list[str] | None = None) -> int:
    """Verify the declared offline artifacts and emit separate checkout diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-pinned-heads", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inspect_stack(args.root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Source export invalid: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return int(
        any(
            not row["bundle_verified"]
            or not row["pinned_object_present"]
            or not row["checkout_at_exported_ref"]
            or not row["checkout_clean"]
            or (args.require_pinned_heads and not row["checkout_at_pinned_ref"])
            for row in result
        )
    )


if __name__ == "__main__":
    sys.exit(main())
