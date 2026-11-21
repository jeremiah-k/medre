#!/usr/bin/env python3
"""Released-artifact continuity qualification (A3 rows D-0 and D-1).

Prove the gap between the released-DDL fixture (``tests/fixtures/
released-storage-0.1.0.sql``) and a database produced by an actual installed
release, using authentic published wheels from a locally cached artifact
directory.  No native credentials or radio state participate: the producer and
smoke steps run the offline fake-adapter sample route in sanitized sandboxes.

Scenarios
---------
``d0`` — actual release-created stopped database to the current wheel:

1. Install the authentic ``medre`` 0.1.0 wheel (sha256-pinned below) plus the
   released ``medre-adapter-meshtastic`` 0.1.1 wheel (SDK-free) into a
   throwaway venv.
2. Through the released console script only: ``medre config sample`` then
   ``medre smoke --config <sample> --storage-path <db> --json`` — a populated
   stopped database with canonical events, native references, a delivery
   receipt and retained outbox work.
3. Hash the original database (it is never opened writable) and validate the
   read-only catalog status with the released console script
   (``medre storage status``), confirming the bytes are unchanged.
4. On a COPY, perform a writable reopen with a clean candidate wheel built
   from this checkout with the repo's pinned build backend (``python -m
   build --wheel --no-isolation``, provenance recorded). Compare each
   of two startup cycles against the full released catalog and rows plus
   the empty, allowlisted handoff settlement table.
5. Verify the consumer console script classifies the reopened copy as
   ``Catalog: current``.
6. On separate copies, overwrite the schema version stamp (``99`` and
   ``not-an-integer``) and verify the existing refusal behavior of the
   current wheel: startup raises ``StorageInitializationError`` with the
   established operator language and the database bytes are preserved.

``d1`` — published provider floor pair:

1. Read the cached ``medre-adapter-meshtastic`` 0.1.1 wheel METADATA to
   extract its actual declared core requirement (``medre<0.2.0,>=0.1.0``;
   floor 0.1.0) — never assume it.
2. Install the declared core floor wheel plus the adapter wheel with its
   full declared dependency set (including the pinned transport SDK) into a
   separate clean venv and read back the installed versions from
   ``importlib.metadata``.
3. Isolated-import probe: ``medre`` and adapter modules resolve inside the
   venv, the ``meshtastic`` transport registers and contributes an offline
   fake sample, and no checkout path leaks into ``sys.path``.
4. Bridge smoke with the released pair, outside the checkout:
   ``medre config sample`` → ``medre config check`` → ``medre smoke --json``
   with a passed report, a sent receipt and a clean ``stopped`` shutdown.

Artifact cache (exact commands)
-------------------------------
The scenarios consume locally cached PyPI downloads; the cache is retained
between runs and referenced by ``tests/test_released_artifact_continuity.py``.
Default location ``~/.cache/medre-review-venvs/slice3-d/artifacts``
(override with ``--artifact-cache PATH`` or
``MEDRE_RELEASED_ARTIFACT_CACHE``):

.. code-block:: text

    mkdir -p ~/.cache/medre-review-venvs/slice3-d/artifacts
    python -m pip download --no-deps \
        -d ~/.cache/medre-review-venvs/slice3-d/artifacts medre==0.1.0
    python -m pip download --no-deps \
        -d ~/.cache/medre-review-venvs/slice3-d/artifacts \
        medre-adapter-meshtastic==0.1.1

Every cached file is sha256-checked against the pinned digests below before
use; a cache miss reports ``skipped`` (exit 0) and a digest mismatch is a
loud failure — a run is never faked green.  Network access is required for
the throwaway installs (core dependencies and the adapter's declared SDK).

Reuse: sandbox/leakage controls, the wheel build and its package/schema
verification are reused from ``scripts/check_installed_package.py``; the
catalog/row comparison and refusal expectations mirror
``tests/test_released_storage_catalog.py``.

Exit codes: 0 = passed or skipped, 1 = a check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Published PyPI artifacts consumed by this qualification.  The digests were
# recorded from the PyPI JSON API (pypi.org/pypi/<name>/<version>/json) and
# are re-verified against the local cache on every run.  The adapter wheel's
# declared core floor is read from its METADATA at runtime, not hardcoded.
_PINNED_ARTIFACTS = {
    "medre-0.1.0-py3-none-any.whl": (
        "4dc5657eb221eb4e9acd887b16fcb5f8df809b23585afb032e06b672624ff294"
    ),
    "medre_adapter_meshtastic-0.1.1-py3-none-any.whl": (
        "1076010d3e74c6e61beb48683d4994b6170ebc5b6f0c3bf24f2a418414f97e12"
    ),
}
_DEFAULT_CACHE = (
    Path.home() / ".cache" / "medre-review-venvs" / "slice3-d" / "artifacts"
)
_CACHE_ENV_VAR = "MEDRE_RELEASED_ARTIFACT_CACHE"

# Reused installed-wheel proof machinery (same checkout, same conventions).
_SPEC = importlib.util.spec_from_file_location(
    "medre_check_installed_package",
    _REPO_ROOT / "scripts" / "check_installed_package.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_proof = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_proof)

_CHILD_TIMEOUT_S = 180
_VENV_TIMEOUT_S = 300
_INSTALL_TIMEOUT_S = 900

# Established operator-facing refusal language for unsupported catalogs
# (mirrors tests/test_released_storage_catalog.py).
_REFUSAL_LANGUAGE = ("verify a backup", "compatible build", "explicit reset")

_IDENTITY_TABLES = (
    "canonical_events",
    "native_message_refs",
    "delivery_receipts",
    "delivery_outbox",
    "delivery_outbox_snapshots",
    "adapter_checkpoints",
)
_HANDOFF_SETTLEMENTS = "delivery_handoff_settlements"

_OPEN_STORAGE_SNIPPET = """
import asyncio, sys
from medre.core.storage.sqlite.storage import SQLiteStorage

async def main():
    storage = SQLiteStorage(sys.argv[1])
    try:
        await storage.initialize()
    finally:
        await storage.close()

asyncio.run(main())
"""


class ScenarioFailure(AssertionError):
    """A real qualification check failed (never silently absorbed)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailure(message)


def default_artifact_cache() -> Path:
    """Resolve the artifact cache directory from env/flag/default."""
    env = os.environ.get(_CACHE_ENV_VAR)
    return Path(env) if env else _DEFAULT_CACHE


def cache_status(cache: Path) -> dict[str, str]:
    """Return pinned artifact name -> path for present files (missing omitted)."""
    found: dict[str, str] = {}
    for name, _digest in _PINNED_ARTIFACTS.items():
        path = cache / name
        if path.is_file():
            found[name] = str(path)
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_cached_artifacts(cache: Path, names: list[str]) -> dict[str, Any]:
    """Sha256-verify the required cached artifacts and read their versions."""
    artifacts: dict[str, Any] = {}
    for name in names:
        expected = _PINNED_ARTIFACTS.get(name)
        path = cache / name
        if expected is None or not path.is_file():
            raise ScenarioFailure(f"required artifact missing from cache: {name}")
        actual = _sha256(path)
        _require(
            actual == expected,
            f"cached artifact digest mismatch for {name}: {actual} != {expected}",
        )
        artifacts[name] = {
            "path": str(path),
            "sha256": actual,
            "version": _wheel_version(path),
        }
    return artifacts


def _wheel_version(path: Path) -> str:
    """Read the distribution version from a wheel's METADATA."""
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
        ]
        _require(len(metadata_names) == 1, f"expected one METADATA in {path.name}")
        text = archive.read(metadata_names[0]).decode("utf-8")
    match = re.search(r"^Version: (.+)$", text, re.MULTILINE)
    _require(match is not None, f"no Version header in {path.name} METADATA")
    return match.group(1).strip()


def _adapter_declared_core_floor(wheel: Path) -> str:
    """Extract the ``>=`` floor of the adapter's declared ``medre`` requirement."""
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
        ]
        _require(len(metadata_names) == 1, f"expected one METADATA in {wheel.name}")
        text = archive.read(metadata_names[0]).decode("utf-8")
    requirement = None
    for line in text.splitlines():
        if line.startswith("Requires-Dist:") and re.match(
            r"Requires-Dist:\s*medre(?:\s*[;<>=!])", line
        ):
            requirement = line.split(":", 1)[1].strip().split(";")[0].strip()
            break
    _require(
        requirement is not None,
        f"{wheel.name} METADATA declares no medre requirement",
    )
    match = re.search(r">=\s*(\d+(?:\.\d+)*)", requirement or "")
    _require(
        match is not None,
        f"no >= floor in declared medre requirement {requirement!r}",
    )
    return match.group(1)


def _candidate_source_identity() -> dict[str, Any]:
    """Identify the source tree, including whether the worktree is modified."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    return {
        "revision": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def _build_candidate_wheel(sandbox: Path) -> dict[str, Any]:
    """Build the current wheel with the checkout's pinned build backend."""
    source = _candidate_source_identity()
    _name, version, build_requires, _data = _proof._load_project_metadata()
    _proof._verify_build_requirements(build_requires)
    wheel_dir = sandbox / "wheel"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel = _proof._resolve_wheel(None, wheel_dir, version, build_requires)
    _require(
        _candidate_source_identity() == source,
        "source identity changed while building wheel",
    )
    command = [sys.executable, "-m", "build", "--wheel", "--no-isolation"]
    return {
        "path": str(wheel),
        "sha256": _sha256(wheel),
        "version": version,
        "build_command": command,
        "build_interpreter": sys.executable,
        "build_requires": build_requires,
        "source": source,
    }


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScenarioFailure(
            f"command timed out after {timeout}s: {_proof.shlex.join(command)}"
        ) from exc
    return result


def _require_ok(proc: subprocess.CompletedProcess[str], label: str) -> None:
    _require(
        proc.returncode == 0,
        f"{label} failed (exit {proc.returncode}): {_proof._clip(proc.stderr or proc.stdout)}",
    )


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_console(venv: Path) -> Path:
    return venv / ("Scripts/medre.exe" if os.name == "nt" else "bin/medre")


def _make_venv(sandbox: Path, name: str) -> Path:
    venv = sandbox / name
    proc = _run(
        [sys.executable, "-m", "venv", str(venv)],
        cwd=sandbox,
        env=dict(os.environ),
        timeout=_VENV_TIMEOUT_S,
    )
    _require_ok(proc, f"venv creation ({name})")
    return venv


def _pip_install(venv: Path, work: Path, env: dict[str, str], *args: str) -> None:
    proc = _run(
        [str(_venv_python(venv)), "-m", "pip", "install", "--no-cache-dir", *args],
        cwd=work,
        env=env,
        timeout=_INSTALL_TIMEOUT_S,
    )
    _require_ok(proc, "pip install")


def _metadata_readback(venv: Path, work: Path, env: dict[str, str]) -> dict[str, str]:
    probe = (
        "import importlib.metadata as im, json\n"
        "names = ['medre', 'medre-adapter-meshtastic', 'mtjk', 'PyPubSub']\n"
        "out = {}\n"
        "for n in names:\n"
        "    try:\n"
        "        out[n] = im.version(n)\n"
        "    except im.PackageNotFoundError:\n"
        "        out[n] = None\n"
        "print(json.dumps(out))\n"
    )
    proc = _run(
        [str(_venv_python(venv)), "-I", "-c", probe],
        cwd=work,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )
    _require_ok(proc, "metadata readback probe")
    return json.loads(proc.stdout)


def _dump_state(path: Path) -> dict[str, Any]:
    """Full catalog + row dump, mirroring tests/test_released_storage_catalog.py."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        catalog = [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL ORDER BY type,name"
            )
        ]
        rows = {
            name: [
                tuple(row)
                for row in db.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'  # nosec B608 # The identifier comes from the persisted catalog enumeration, never external input.
                )
            ]
            for kind, name, _ in catalog
            if kind == "table"
        }
        fk = [tuple(row) for row in db.execute("PRAGMA foreign_key_check")]
        stamp_row = db.execute(
            "SELECT value FROM _medre_schema_meta WHERE key='schema_version'"
        ).fetchone()
    stamp = stamp_row[0] if stamp_row is not None else None
    identities = {name: rows.get(name, []) for name in _IDENTITY_TABLES}
    return {
        "catalog": catalog,
        "rows": rows,
        "foreign_key_violations": fk,
        "schema_version_stamp": stamp,
        "identities": identities,
    }


def _expected_d0_reopen_state(released: dict[str, Any]) -> dict[str, Any]:
    """The exact 0.1.0 state after the approved table additions."""
    _require(
        _HANDOFF_SETTLEMENTS not in released["rows"],
        "the released database already contains the handoff settlement table",
    )
    _CEILING_LOG = "event_content_ceiling_log"
    _HANDOFF_FACTS = "docs/schemas/current/content-free-handoff-facts.schema.sql"
    added = (
        (
            _HANDOFF_SETTLEMENTS,
            "docs/schemas/handoff-settlements.schema.sql",
        ),
        (
            _CEILING_LOG,
            "docs/schemas/event-content-ceiling-log.schema.sql",
        ),
        ("delivery_handoff_uncertainty", _HANDOFF_FACTS),
        ("radio_accepted_part_facts", _HANDOFF_FACTS),
        ("radio_attempt_evidence_summaries", _HANDOFF_FACTS),
        ("delivery_observation_scopes", _HANDOFF_FACTS),
    )
    entries = []
    for table, export in added:
        _require(
            table not in released["rows"],
            f"the released database already contains the {table} table",
        )
        ddl = (_REPO_ROOT / export).read_text(encoding="utf-8")
        with closing(sqlite3.connect(":memory:")) as reference:
            reference.executescript(ddl)
            entry = reference.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        _require(entry is not None, f"the {table} table definition is missing")
        entries.append(entry)
    # The additive ceiling-ordering index completes the approved additions;
    # a single-line DDL keeps its stored text reproducible here.
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.execute(
            "CREATE TABLE event_content_access_policies (hard_ceiling TEXT, event_id TEXT)"
        )
        reference.execute(
            "CREATE INDEX IF NOT EXISTS idx_ceiling_enforcement_order "
            "ON event_content_access_policies(hard_ceiling, event_id)"
        )
        index_entry = reference.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_ceiling_enforcement_order'"
        ).fetchone()
    _require(
        index_entry is not None,
        "the ceiling enforcement index definition is missing",
    )
    return {
        **released,
        "catalog": sorted([*released["catalog"], *entries, index_entry]),
        "rows": {
            **released["rows"],
            **{table: [] for table, _export in added},
        },
    }


def _storage_open_writable(
    venv: Path, work: Path, env: dict[str, str], path: Path
) -> None:
    proc = _run(
        [str(_venv_python(venv)), "-I", "-c", _OPEN_STORAGE_SNIPPET, str(path)],
        cwd=work,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )
    _require_ok(proc, "writable storage open")


def _console(
    venv: Path, work: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return _run(
        [str(_venv_console(venv)), *args],
        cwd=work,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )


def _parse_smoke_report(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    _require_ok(proc, "smoke")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ScenarioFailure("smoke output was not parsable JSON") from exc
    _require(report.get("status") == "passed", f"smoke status {report.get('status')!r}")
    _require(
        report.get("shutdown_status") == "stopped",
        f"smoke shutdown {report.get('shutdown_status')!r}",
    )
    _require(bool(report.get("event_id")), "smoke report has no event_id")
    receipts = report.get("delivery_receipts") or []
    _require(
        any(r.get("status") == "sent" for r in receipts),
        "smoke report has no sent delivery receipt",
    )
    _require(
        report.get("storage_backend") == "sqlite",
        f"smoke storage backend {report.get('storage_backend')!r}",
    )
    return report


def _scenario_d0(sandbox: Path, cache: Path) -> dict[str, Any]:
    """Actual 0.1.0 release-created stopped database to the current wheel."""
    artifacts = _verify_cached_artifacts(
        cache,
        [
            "medre-0.1.0-py3-none-any.whl",
            "medre_adapter_meshtastic-0.1.1-py3-none-any.whl",
        ],
    )
    candidate = _build_candidate_wheel(sandbox)

    work = sandbox / "work"
    work.mkdir(parents=True)
    home = sandbox / "home"
    tmp = sandbox / "tmp"
    xdg = {
        "XDG_CONFIG_HOME": sandbox / "xdg" / "config",
        "XDG_STATE_HOME": sandbox / "xdg" / "state",
        "XDG_DATA_HOME": sandbox / "xdg" / "data",
        "XDG_CACHE_HOME": sandbox / "xdg" / "cache",
    }
    for directory in (home, tmp, *xdg.values()):
        directory.mkdir(parents=True)
    env = _proof._child_env(home, tmp, xdg)

    # -- producer: authentic released artifacts through console scripts ------
    producer = _make_venv(sandbox, "producer-venv")
    _pip_install(
        producer,
        work,
        env,
        artifacts["medre-0.1.0-py3-none-any.whl"]["path"],
    )
    _pip_install(
        producer,
        work,
        env,
        "--no-deps",
        artifacts["medre_adapter_meshtastic-0.1.1-py3-none-any.whl"]["path"],
    )
    producer_versions = _metadata_readback(producer, work, env)
    _require(
        producer_versions["medre"] == "0.1.0",
        f"producer medre version {producer_versions['medre']!r} != 0.1.0",
    )

    proc = _console(producer, work, env, "config", "sample")
    _require_ok(proc, "released config sample")
    sample_path = work / "sample.yaml"
    sample_path.write_text(proc.stdout, encoding="utf-8")

    db_path = work / "released-010.sqlite"
    proc = _console(
        producer,
        work,
        env,
        "smoke",
        "--config",
        str(sample_path),
        "--storage-path",
        str(db_path),
        "--json",
    )
    smoke_report = _parse_smoke_report(proc)
    _require(
        db_path.is_file(),
        "smoke did not persist the SQLite database at --storage-path",
    )

    original_sha = _sha256(db_path)
    wal_path = Path(str(db_path) + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else None
    _require(
        wal_bytes in (None, 0),
        "smoke left an uncheckpointed WAL; a main-file copy would be incomplete",
    )

    # -- read-only catalog status from the released install ------------------
    proc = _console(
        producer, work, env, "storage", "status", "--storage-path", str(db_path)
    )
    _require_ok(proc, "released storage status")
    released_status = proc.stdout
    _require("Status: HEALTHY" in released_status, "released status not HEALTHY")
    _require(
        _sha256(db_path) == original_sha,
        "released read-only status modified the database bytes",
    )

    # -- writable reopen on a copy with the current wheel --------------------
    copy_path = sandbox / "reopen-copy.sqlite"
    copy_path.write_bytes(db_path.read_bytes())
    before = _dump_state(copy_path)
    expected = _expected_d0_reopen_state(before)
    before_sha = _sha256(copy_path)

    consumer = _make_venv(sandbox, "consumer-venv")
    _pip_install(consumer, work, env, candidate["path"])
    consumer_versions = _metadata_readback(consumer, work, env)
    _require(
        consumer_versions["medre"] == candidate["version"],
        "consumer medre metadata does not match the candidate wheel version",
    )
    import_probe = _consumer_import_probe(consumer, work, env)
    _require(
        import_probe["transports"] == [],
        "current core wheel unexpectedly registered a transport",
    )

    opens: list[dict[str, Any]] = []
    for index in (1, 2):
        _storage_open_writable(consumer, work, env, copy_path)
        after = _dump_state(copy_path)
        opens.append(
            {
                "open": index,
                "state_equal": after == before,
                "expected_state_equal": after == expected,
                "identities_equal": after["identities"] == before["identities"],
                "foreign_keys_clean": after["foreign_key_violations"] == [],
                "schema_version_stamp": after["schema_version_stamp"],
                "sha256_after": _sha256(copy_path),
            }
        )
    _require(
        all(
            o["expected_state_equal"]
            and o["identities_equal"]
            and o["foreign_keys_clean"]
            for o in opens
        ),
        "writable reopen differs from the released state plus the empty approved additive tables",
    )
    stamp_changed = any(
        o["schema_version_stamp"] != before["schema_version_stamp"] for o in opens
    )
    _require(not stamp_changed, "schema version stamp changed during reopen")
    persisted_changes_detected = stamp_changed or any(
        not o["state_equal"] for o in opens
    )

    proc = _console(
        consumer, work, env, "storage", "status", "--storage-path", str(copy_path)
    )
    _require_ok(proc, "consumer storage status")
    consumer_status = proc.stdout
    _require("Catalog: current" in consumer_status, "consumer catalog not current")
    _require("Status: HEALTHY" in consumer_status, "consumer status not HEALTHY")

    # -- refusal behavior on separate unsupported fixtures -------------------
    refusal: list[dict[str, Any]] = []
    for stamp in ("99", "not-an-integer"):
        refusal_path = sandbox / f"refusal-{stamp}.sqlite"
        refusal_path.write_bytes(db_path.read_bytes())
        with closing(sqlite3.connect(refusal_path)) as db, db:
            db.execute("UPDATE _medre_schema_meta SET value=?", (stamp,))
        before_bytes = refusal_path.read_bytes()
        proc = _run(
            [
                str(_venv_python(consumer)),
                "-I",
                "-c",
                _OPEN_STORAGE_SNIPPET,
                str(refusal_path),
            ],
            cwd=work,
            env=env,
            timeout=_CHILD_TIMEOUT_S,
        )
        after_bytes = refusal_path.read_bytes()
        refusal.append(
            {
                "stamp": stamp,
                "startup_failed": proc.returncode != 0,
                "storage_initialization_error": "StorageInitializationError"
                in proc.stderr,
                "refusal_language_present": [
                    s for s in _REFUSAL_LANGUAGE if s in proc.stderr
                ],
                "bytes_preserved": before_bytes == after_bytes,
            }
        )
    _require(
        all(
            r["startup_failed"]
            and r["storage_initialization_error"]
            and len(r["refusal_language_present"]) == len(_REFUSAL_LANGUAGE)
            and r["bytes_preserved"]
            for r in refusal
        ),
        "unsupported-catalog refusal behavior regressed",
    )

    _require(
        _sha256(db_path) == original_sha,
        "the original released database was modified",
    )

    return {
        "artifacts": {**artifacts, "candidate_wheel": candidate},
        "producer": {
            "versions": producer_versions,
            "smoke": {
                "status": smoke_report.get("status"),
                "shutdown_status": smoke_report.get("shutdown_status"),
                "event_id": smoke_report.get("event_id"),
                "storage_backend": smoke_report.get("storage_backend"),
                "receipt_statuses": sorted(
                    {
                        r.get("status")
                        for r in smoke_report.get("delivery_receipts", []) or []
                    }
                ),
                "target_adapters": smoke_report.get("target_adapters"),
            },
            "released_status_healthy": "Status: HEALTHY" in released_status,
        },
        "original_database": {
            "sha256": original_sha,
            "wal_bytes": wal_bytes,
            "unchanged_after_all_steps": _sha256(db_path) == original_sha,
        },
        "reopen": {
            "before_sha256": before_sha,
            "identity_counts": {
                name: len(rows) for name, rows in before["identities"].items()
            },
            "table_count": len(before["rows"]),
            "schema_version_stamp": before["schema_version_stamp"],
            "opens": opens,
            "expected_added_tables": [
                "event_content_ceiling_log",
                _HANDOFF_SETTLEMENTS,
            ],
            "persisted_changes_detected": persisted_changes_detected,
            "migration_evidence_scope": "exact persisted state comparison against the approved additive layout",
            "consumer_cli_catalog_current": "Catalog: current" in consumer_status,
            "consumer_cli_healthy": "Status: HEALTHY" in consumer_status,
        },
        "refusal": refusal,
        "candidate_import_probe": import_probe,
    }


def _consumer_import_probe(
    venv: Path, work: Path, env: dict[str, str]
) -> dict[str, Any]:
    probe = (
        "import importlib.metadata as im, json, sys\n"
        "import medre\n"
        "from medre.adapter_registry import registered_transports\n"
        "print(json.dumps({\n"
        "    'medre_file': medre.__file__,\n"
        "    'medre_version': im.version('medre'),\n"
        "    'transports': registered_transports(),\n"
        "    'sys_path': sys.path,\n"
        "}))\n"
    )
    proc = _run(
        [str(_venv_python(venv)), "-I", "-c", probe],
        cwd=work,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )
    _require_ok(proc, "consumer import probe")
    data = json.loads(proc.stdout)
    medre_file = Path(data["medre_file"]).resolve()
    _require(
        venv.resolve() in medre_file.parents and "site-packages" in medre_file.parts,
        "consumer medre import did not come from the throwaway venv",
    )
    repo = _REPO_ROOT.resolve()
    leaked = [
        entry
        for entry in data["sys_path"]
        if entry
        and (Path(entry).resolve() == repo or repo in Path(entry).resolve().parents)
    ]
    _require(not leaked, f"checkout leaked into consumer sys.path: {leaked}")
    return data


def _scenario_d1(sandbox: Path, cache: Path) -> dict[str, Any]:
    """Published provider wheel at its declared core floor."""
    artifacts = _verify_cached_artifacts(
        cache,
        [
            "medre-0.1.0-py3-none-any.whl",
            "medre_adapter_meshtastic-0.1.1-py3-none-any.whl",
        ],
    )
    adapter_wheel = artifacts["medre_adapter_meshtastic-0.1.1-py3-none-any.whl"]
    floor = _adapter_declared_core_floor(Path(adapter_wheel["path"]))
    core_wheel = artifacts["medre-0.1.0-py3-none-any.whl"]
    _require(
        core_wheel["version"] == floor,
        f"cached core wheel {core_wheel['version']} is not the adapter's "
        f"declared floor {floor}",
    )

    work = sandbox / "work"
    work.mkdir(parents=True)
    home = sandbox / "home"
    tmp = sandbox / "tmp"
    xdg = {
        "XDG_CONFIG_HOME": sandbox / "xdg" / "config",
        "XDG_STATE_HOME": sandbox / "xdg" / "state",
        "XDG_DATA_HOME": sandbox / "xdg" / "data",
        "XDG_CACHE_HOME": sandbox / "xdg" / "cache",
    }
    for directory in (home, tmp, *xdg.values()):
        directory.mkdir(parents=True)
    env = _proof._child_env(home, tmp, xdg)

    venv = _make_venv(sandbox, "floor-pair-venv")
    _pip_install(venv, work, env, core_wheel["path"], adapter_wheel["path"])
    _require_ok(
        _run(
            [str(_venv_python(venv)), "-m", "pip", "check"],
            cwd=work,
            env=env,
            timeout=_INSTALL_TIMEOUT_S,
        ),
        "floor-pair dependency consistency",
    )

    versions = _metadata_readback(venv, work, env)
    _require(
        versions["medre"] == floor,
        f"installed core {versions['medre']!r} != floor {floor!r}",
    )
    _require(
        versions["medre-adapter-meshtastic"] == adapter_wheel["version"],
        "installed adapter version does not match the wheel metadata",
    )

    probe = _floor_pair_probe(venv, work, env, versions)

    proc = _console(venv, work, env, "config", "sample")
    _require_ok(proc, "config sample")
    sample_path = work / "sample.yaml"
    sample_path.write_text(proc.stdout, encoding="utf-8")
    proc = _console(venv, work, env, "config", "check", "--config", str(sample_path))
    _require_ok(proc, "config check")

    proc = _console(venv, work, env, "smoke", "--config", str(sample_path), "--json")
    smoke_report = _parse_smoke_report(proc)

    return {
        "artifacts": artifacts,
        "declared_core_floor": floor,
        "installed_versions": versions,
        "import_probe": {
            "medre_file": probe["medre_file"],
            "transports": probe["transports"],
            "sample_transports": probe["sample_transports"],
            "adapter_modules_from_venv": probe["adapter_modules_from_venv"],
        },
        "smoke": {
            "status": smoke_report.get("status"),
            "shutdown_status": smoke_report.get("shutdown_status"),
            "event_id": smoke_report.get("event_id"),
            "storage_backend": smoke_report.get("storage_backend"),
            "receipt_statuses": sorted(
                {
                    r.get("status")
                    for r in smoke_report.get("delivery_receipts", []) or []
                }
            ),
            "target_adapters": smoke_report.get("target_adapters"),
        },
    }


def _floor_pair_probe(
    venv: Path, work: Path, env: dict[str, str], versions: dict[str, str]
) -> dict[str, Any]:
    probe = (
        "import importlib, importlib.metadata as im, json, sys\n"
        "import medre\n"
        "from medre.adapter_registry import registered_transports, iter_adapter_specs\n"
        "modules = {}\n"
        "for spec in iter_adapter_specs():\n"
        "    for ref in (spec.config, spec.runtime_config, spec.fake_adapter):\n"
        "        if ref is not None:\n"
        "            modules[ref.module] = importlib.import_module(ref.module).__file__\n"
        "print(json.dumps({\n"
        "    'medre_file': medre.__file__,\n"
        "    'medre_version': im.version('medre'),\n"
        "    'adapter_version': im.version('medre-adapter-meshtastic'),\n"
        "    'transports': registered_transports(),\n"
        "    'sample_transports': [s.transport for s in iter_adapter_specs()\n"
        "                          if s.fake_adapter is not None and s.sample_config is not None],\n"
        "    'adapter_modules': modules,\n"
        "    'sys_path': sys.path,\n"
        "}))\n"
    )
    proc = _run(
        [str(_venv_python(venv)), "-I", "-c", probe],
        cwd=work,
        env=env,
        timeout=_CHILD_TIMEOUT_S,
    )
    _require_ok(proc, "floor pair import probe")
    data = json.loads(proc.stdout)
    venv_resolved = venv.resolve()
    medre_file = Path(data["medre_file"]).resolve()
    _require(
        venv_resolved in medre_file.parents and "site-packages" in medre_file.parts,
        "medre import did not come from the throwaway venv",
    )
    bad = [
        module
        for module, filename in data["adapter_modules"].items()
        if not (
            filename
            and venv_resolved in Path(filename).resolve().parents
            and "site-packages" in Path(filename).parts
        )
    ]
    _require(not bad, f"adapter modules did not come from the installed wheel: {bad}")
    repo = _REPO_ROOT.resolve()
    leaked = [
        entry
        for entry in data["sys_path"]
        if entry
        and (Path(entry).resolve() == repo or repo in Path(entry).resolve().parents)
    ]
    _require(not leaked, f"checkout leaked into floor-pair sys.path: {leaked}")
    _require(
        data["medre_version"] == versions["medre"]
        and data["adapter_version"] == versions["medre-adapter-meshtastic"],
        "imported metadata versions disagree with the distribution readback",
    )
    data["adapter_modules_from_venv"] = not bad
    return data


_SCENARIOS = {"d0": _scenario_d0, "d1": _scenario_d1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_released_artifact_continuity",
        description=(
            "Qualify released-artifact continuity: a real 0.1.0-created "
            "database reopened by a clean current wheel (D-0) and the "
            "published Meshtastic provider wheel at its declared core floor "
            "(D-1)."
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=["d0", "d1", "all"],
        default="all",
        help="qualification scenario to run (default: all)",
    )
    parser.add_argument(
        "--artifact-cache",
        type=Path,
        default=None,
        help=(
            "directory holding the sha256-pinned published wheels "
            f"(default: ${_CACHE_ENV_VAR} or {_DEFAULT_CACHE})"
        ),
    )
    parser.add_argument(
        "--require-artifacts",
        action="store_true",
        help="Fail if any required published wheel is absent (qualification mode)",
    )
    parser.add_argument(
        "--sandbox",
        type=Path,
        default=None,
        help="working directory for throwaway venvs and state "
        "(default: a fresh temp directory, removed on exit)",
    )
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="also write the JSON summary to this path",
    )
    args = parser.parse_args(argv)

    cache = args.artifact_cache or default_artifact_cache()
    selected = ["d0", "d1"] if args.scenario == "all" else [args.scenario]

    owned_sandbox = args.sandbox is None
    sandbox = (
        Path(tempfile.mkdtemp(prefix="medre-released-artifact-"))
        if owned_sandbox
        else args.sandbox.resolve()
    )
    sandbox.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, Any]] = {}
    exit_code = 0
    try:
        for scenario in selected:
            missing = [
                name for name in _PINNED_ARTIFACTS if not (cache / name).is_file()
            ]
            if missing:
                summaries[scenario] = {
                    "scenario": scenario,
                    "status": "skipped",
                    "reason": (
                        "artifact cache missing pinned published wheels: "
                        + ", ".join(missing)
                        + f"; populate with pip download per the module docstring (cache: {cache})"
                    ),
                }
                if args.require_artifacts:
                    exit_code = 1
                continue
            try:
                scenario_dir = sandbox / scenario
                _require(
                    not scenario_dir.exists(),
                    f"scenario sandbox already exists: {scenario_dir}",
                )
                scenario_dir.mkdir()
                result = _SCENARIOS[scenario](scenario_dir, cache)
                summaries[scenario] = {
                    "scenario": scenario,
                    "status": "passed",
                    **result,
                }
            except (
                ScenarioFailure,
                OSError,
                subprocess.SubprocessError,
                ValueError,
            ) as exc:
                summaries[scenario] = {
                    "scenario": scenario,
                    "status": "failed",
                    "reason": str(exc),
                }
                exit_code = 1
    finally:
        try:
            if args.summary_json is not None:
                args.summary_json.parent.mkdir(parents=True, exist_ok=True)
                args.summary_json.write_text(
                    json.dumps(summaries, indent=2, sort_keys=True), encoding="utf-8"
                )
        finally:
            if owned_sandbox:
                shutil.rmtree(sandbox)

    print(json.dumps(summaries, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
