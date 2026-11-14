"""Content-free, revision-bound native qualification records; never send authority."""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import jsonschema  # type: ignore[import-untyped]
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from medre.core.evidence.native_evidence import NativeEvidenceObservation

if __package__:
    from .adapter_matrix import candidate_snapshot, resolve_adapters
else:
    from adapter_matrix import candidate_snapshot, resolve_adapters

_SCHEMA = "docs/schemas/native-qualification.schema.json"
_OBSERVATION_FIELDS = (
    "event_id",
    "delivery_plan_id",
    "target_adapter",
    "target_channel",
    "outbox_id",
    "attempt_number",
    "receipt_id",
    "native_message_id",
    "scope",
    "tier",
    "result",
    "evidence_fingerprint",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or Path(relative).is_absolute():
        raise ValueError("qualification harness path escapes its repository")
    return path


def observation_summary(observation: NativeEvidenceObservation) -> dict[str, object]:
    """Select known identifier/classification fields, never error text or metadata."""
    return {name: getattr(observation, name) for name in _OBSERVATION_FIELDS}


def qualification_rows(
    root: Path, *, candidate_refs: str | None = None
) -> list[dict[str, Any]]:
    """Read committed pins, or explicitly select a complete ephemeral candidate."""
    manifest = root / ".github/adapter-compatibility.json"
    rows = resolve_adapters(manifest, resolve=False)
    return candidate_snapshot(rows, candidate_refs) if candidate_refs else rows


def provider_provenance(
    root: Path, checkout: Path, transport: str, *, candidate_refs: str | None = None
) -> dict[str, Any]:
    """Verify the installed, editable provider and its declared SDK requirements."""
    rows = qualification_rows(root, candidate_refs=candidate_refs)
    matches = [row for row in rows if row["transport"] == transport]
    if len(matches) != 1:
        raise ValueError("qualification requires exactly one manifest provider")
    row = matches[0]
    if _git(checkout, "rev-parse", "HEAD") != row["ref"]:
        raise ValueError("provider checkout differs from the compatibility pin")
    if _git(checkout, "status", "--porcelain"):
        raise ValueError("provider checkout has modified or untracked source")
    project = tomllib.loads((checkout / "pyproject.toml").read_text())["project"]
    requirements = [Requirement(raw) for raw in project["dependencies"]]
    versions = {}
    for requirement in requirements:
        if canonicalize_name(requirement.name) == "medre" or (
            requirement.marker is not None and not requirement.marker.evaluate()
        ):
            continue
        version = importlib.metadata.version(requirement.name)
        if version not in requirement.specifier:
            raise ValueError("installed SDK differs from the provider requirement")
        versions[canonicalize_name(requirement.name)] = version
    if not versions:
        raise ValueError("provider declares no SDK dependencies")
    # SDK/fixture tests must actually use this installed editable provider.
    direct = importlib.metadata.distribution(row["distribution"]).read_text(
        "direct_url.json"
    )
    location = json.loads(direct or "{}")
    url = urlparse(location.get("url", ""))
    source = Path(url2pathname(url.path)).resolve()
    if (
        url.scheme != "file"
        or url.netloc not in ("", "localhost")
        or source != checkout.resolve()
        or not location.get("dir_info", {}).get("editable")
    ):
        raise ValueError("installed provider is not the qualified checkout")
    return {
        "transport": transport,
        "repository": row["repository"],
        "revision": row["ref"],
        "sdk_versions": versions,
    }


def _candidate_digest(root: Path, candidate_refs: str) -> str:
    """Bind all selected transports, independent of JSON key or manifest order."""
    rows = qualification_rows(root, candidate_refs=candidate_refs)
    return sha256(_canonical(sorted(rows, key=lambda row: row["transport"])))


def _record_schema(root: Path, *, candidate: bool) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((root / _SCHEMA).read_text())
    if candidate:
        # Version two is CI-only. Never modify the frozen committed-pin v1
        # schema or allow a candidate statement to validate as v1 evidence.
        schema.pop("$id", None)
        schema["properties"]["schema_version"] = {"const": 2}
        schema["required"].append("candidate_snapshot_sha256")
        schema["properties"]["candidate_snapshot_sha256"] = {"$ref": "#/$defs/digest"}
    return schema


def build_record(
    root: Path,
    checkout: Path,
    *,
    transport: str,
    observation: dict[str, object],
    observer_kind: str,
    witness_hashes: list[str],
    harnesses: list[tuple[str, str]],
    binding: tuple[str, str],
    candidate_refs: str | None = None,
) -> dict[str, Any]:
    """Snapshot the actual pair and harness bytes after successful teardown.

    Hashes establish consistency and reproducibility, not observer authenticity.
    Dirty core runs remain explicitly marked and cannot serve as CI proof.
    """
    provider = provider_provenance(
        root, checkout, transport, candidate_refs=candidate_refs
    )
    sources = []
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    for owner, relative in harnesses:
        repository = root if owner == "core" else checkout
        source = _file(repository, relative)
        tracked = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    relative,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode
            == 0
        )
        if owner == "provider" and not tracked:
            raise ValueError("provider harness is not part of the pinned commit")
        dirty |= owner == "core" and not tracked
        sources.append(
            {
                "repository": owner,
                "path": relative,
                "sha256": sha256(source.read_bytes()),
            }
        )
    record: dict[str, Any] = {
        "schema_version": 1,
        "core": {"revision": _git(root, "rev-parse", "HEAD"), "dirty": dirty},
        "provider": provider,
        "runtime": {"python": platform.python_version(), "platform": platform.system()},
        "ci": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        },
        "harnesses": sources,
        "observation": observation,
        "ledger_binding": {"state": binding[0], "reason": binding[1]},
        "observer": {
            "kind": observer_kind,
            "count": len(witness_hashes),
            "witness_sha256": witness_hashes,
        },
    }
    if candidate_refs:
        record["schema_version"] = 2
        record["candidate_snapshot_sha256"] = _candidate_digest(root, candidate_refs)
    record["record_sha256"] = sha256(_canonical(record))
    problems = check_record(
        root, checkout, record, require_clean=False, candidate_refs=candidate_refs
    )
    if problems:
        raise ValueError("; ".join(problems))
    return record


def check_record(
    root: Path,
    checkout: Path,
    record: dict[str, Any],
    *,
    require_clean: bool = True,
    candidate_refs: str | None = None,
) -> list[str]:
    """Fail closed on stale pins, altered harnesses, malformed data, and replayed CI IDs."""
    if not isinstance(record, dict):
        return ["qualification schema: record must be an object"]
    if record.get("schema_version") == 2 and not candidate_refs:
        return ["candidate qualification requires the same explicit snapshot"]
    if record.get("schema_version") == 1 and candidate_refs:
        return ["committed-pin qualification cannot verify a candidate snapshot"]
    schema = _record_schema(root, candidate=bool(candidate_refs))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(record))
    if errors:
        return [
            f"qualification schema: {error.json_path}: {error.message}"
            for error in errors
        ]
    problems = []
    unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
    if sha256(_canonical(unsigned)) != record["record_sha256"]:
        problems.append("qualification record digest differs")
    if record["core"]["revision"] != _git(root, "rev-parse", "HEAD"):
        problems.append("core revision differs")
    if require_clean and (
        record["core"]["dirty"]
        or _git(root, "status", "--porcelain", "--untracked-files=no")
    ):
        problems.append("qualification requires clean core source")
    try:
        if candidate_refs and record["candidate_snapshot_sha256"] != _candidate_digest(
            root, candidate_refs
        ):
            problems.append("candidate snapshot digest differs")
        provider = provider_provenance(
            root,
            checkout,
            record["provider"]["transport"],
            candidate_refs=candidate_refs,
        )
        if provider != record["provider"]:
            problems.append("provider or SDK provenance differs")
        seen = set()
        for source in record["harnesses"]:
            key = (source["repository"], source["path"])
            if key in seen:
                problems.append("duplicate harness")
            seen.add(key)
            repository = root if source["repository"] == "core" else checkout
            if (
                sha256(_file(repository, source["path"]).read_bytes())
                != source["sha256"]
            ):
                problems.append("harness digest differs")
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        problems.append(str(exc))
    if record["observer"]["count"] != len(record["observer"]["witness_sha256"]):
        problems.append("observer count differs from witnesses")
    if {source["repository"] for source in record["harnesses"]} != {"core", "provider"}:
        problems.append("both core and provider harnesses are required")
    scope = (
        "provider_acceptance"
        if record["observer"]["kind"] == "companion_protocol"
        else "recipient_observation"
    )
    if record["observation"]["scope"] != scope:
        problems.append("observer kind differs from claim scope")
    expected_observer = {
        "meshcore": "companion_protocol",
        "lxmf": "recipient_callback",
    }[record["provider"]["transport"]]
    if record["observer"]["kind"] != expected_observer:
        problems.append("provider proof does not support this observer kind")
    for field, variable in (
        ("run_id", "GITHUB_RUN_ID"),
        ("run_attempt", "GITHUB_RUN_ATTEMPT"),
    ):
        if os.environ.get(variable) and record["ci"][field] != os.environ[variable]:
            problems.append("CI execution identity differs")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", nargs="+")
    parser.add_argument("--provider", type=Path, default=Path(".adapter"))
    parser.add_argument(
        "--allow-dirty", action="store_true", help="local experiments only"
    )
    parser.add_argument("--expect-records", type=int)
    parser.add_argument(
        "--candidate-refs", help="Complete candidate snapshot; not committed-pin proof"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    # pytest publishes a "current" symlink alongside each numbered directory.
    # Aliases name one record; distinct files repeating a statement do not.
    paths = sorted(
        {
            str(Path(path).resolve())
            for pattern in args.records
            for path in glob.glob(pattern, recursive=True)
            if Path(path).is_file()
        }
    )
    if not paths or any(
        not glob.glob(pattern, recursive=True) for pattern in args.records
    ):
        raise SystemExit("No qualification records found")
    if args.expect_records is not None and len(paths) != args.expect_records:
        raise SystemExit("Qualification record count differs")
    seen = set()
    for path in paths:
        record = json.loads(Path(path).read_text())
        problems = check_record(
            root,
            args.provider,
            record,
            require_clean=not args.allow_dirty,
            candidate_refs=args.candidate_refs,
        )
        if problems:
            raise SystemExit(f"{path}: " + "; ".join(problems))
        if record["record_sha256"] in seen:
            raise SystemExit("Duplicate qualification statement")
        seen.add(record["record_sha256"])
    print(f"Verified {len(paths)} native qualification record(s)")


if __name__ == "__main__":
    main()
