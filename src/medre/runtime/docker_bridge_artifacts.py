"""Opt-in Docker bridge artifact collection for Matrix <-> Meshtastic validation.

Provides :func:`collect_docker_bridge_artifacts` — a function that creates a
timestamped run directory, invokes Docker integration tests for a given
scenario, and writes a ``summary.json`` with full evidence even on failure.

This module is **opt-in** — it is not called from any default CI path.  Invoke
it via ``scripts/ci/run-docker-bridge-artifacts.sh`` or import and call
directly from Python.

No Docker required for unit testing: the artifact collection, redaction, and
summary generation logic is fully testable with mocked Docker results.

Artifact directory convention
-----------------------------
Artifacts are written to::

    .ci-artifacts/docker-bridge-runs/<ISO-timestamp>/

Reuses the existing ``MEDRE_CI_ARTIFACT_DIR`` env var and
``.ci-artifacts/docker-integration`` convention from
``tests/integration/conftest.py``.

Supported scenarios
-------------------
- ``matrix_to_meshtastic`` — Matrix inbound, Meshtastic outbound.
- ``meshtastic_to_matrix`` — Meshtastic inbound, Matrix outbound.
- ``bidirectional`` — Both directions exercised.

summary.json shape
------------------
::

    {
        "status": "passed" | "failed" | "partial",
        "scenario": "matrix_to_meshtastic" | ...,
        "timestamp": "<ISO-8601 UTC>",
        "run_directory": "<absolute path>",
        "matrix": {
            "container": "..." | null,
            "room": "..." | null,
            "event_id": "..." | null,
            "ingress_path": "sync_loop" | "direct_on_room_message_fallback" | null,
        },
        "meshtastic": {
            "daemon": "..." | null,
            "inbound": { ... } | null,
            "outbound": { ... } | null,
        },
        "medre": {
            "event_id": "..." | null,
            "receipt": { ... } | null,
            "native_refs": [],
            "runtime": { ... },
            "limitations": [ ... ],
        },
        "logs": { "pytest_stdout": "..." | null, "pytest_stderr": "..." | null },
        "config_snapshot": { ... } | null,
        "inspect_artifacts": [ ... ],
        "errors": [ ... ],
        "artifact_plan": { "required": [...], "best_effort": [...] },
        "artifact_paths": { "<filename>": "<absolute path>", ... },
        "missing_artifacts": { "required": [...], "best_effort": [...] },
    }

Structured metadata
-------------------
When the integration test fixture writes ``run-metadata.json`` to the run
directory, the collector reads it and uses its fields (``storage_path``,
``event_id``, ``matrix``, ``meshtastic``, ``medre``, ``config_data``,
``log_paths``) as primary evidence — pytest stdout/stderr remain logs
only.  When ``run-metadata.json`` is absent, evidence falls back to
deprecated regex parsing with an explicit limitation.

Honesty requirements
--------------------
- Tokens/passwords are redacted via :func:`~medre.core.observability.sanitization.sanitize_for_log`.
- Docs must state: no real external Matrix account or real radio is proven.
- On failure, ``summary.json`` is still written with ``status: "failed"`` or
  ``"partial"`` and populated ``limitations``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from medre.core.observability.sanitization import sanitize_error, sanitize_for_log

__all__ = [
    "ARTIFACT_PLAN",
    "get_artifact_plan",
    "SUPPORTED_SCENARIOS",
    "collect_docker_bridge_artifacts",
    "build_summary",
    "redact_config_snapshot",
    "create_run_directory",
    "write_summary",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_SCENARIOS: tuple[str, ...] = (
    "matrix_to_meshtastic",
    "meshtastic_to_matrix",
    "bidirectional",
)

_DEFAULT_ARTIFACT_BASE = Path(
    os.environ.get(
        "MEDRE_CI_ARTIFACT_DIR",
        str(
            Path(__file__).resolve().parent.parent.parent.parent.parent
            / ".ci-artifacts"
            / "docker-bridge-runs"
        ),
    )
)

_LIMITATIONS: list[str] = [
    "Docker containers run on localhost — not a real network environment",
    "No real external Matrix account proven (container-local Synapse only)",
    "No real radio hardware proven (container-local meshtasticd simulation only)",
    "Single-direction or limited bidirectional smoke — not sustained throughput",
    "Meshtastic inbound through real pubsub callback is unconfirmed",
    "Cross-adapter bridge proven in container smoke test only — not sustained or multi-hop",
    "No reconnect resilience, retry-against-live, or load evidence",
    "Fire-and-forget delivery model for radio transports",
]

_MAX_LOG_SIZE: int = 256 * 1024  # 256 KiB per log capture

# ---------------------------------------------------------------------------
# Artifact plan — required and best-effort filenames
# ---------------------------------------------------------------------------

ARTIFACT_PLAN: dict[str, list[str]] = {
    "required": [
        "summary.json",
        "run-metadata.json",
        "config.yaml",
        "synapse.log",
        "meshtasticd.log",
    ],
    "best_effort": [
        "medre.log",
        "receipts.json",
        "native-refs.json",
        "inspect-timeline.json",
        "evidence.json",
        "final-snapshot.json",
    ],
}
"""Structured artifact plan for operator-readable output.

Required artifacts are expected after a successful Docker bridge run.
Best-effort artifacts may be absent; missing ones are reported as
limitations.  ``final-snapshot.json`` is always best-effort because
direct PipelineRunner tests cannot produce it without a full MedreApp.
"""


# ---------------------------------------------------------------------------
# Scenario-aware artifact plan
# ---------------------------------------------------------------------------

_BASE_REQUIRED = [
    "summary.json",
    "run-metadata.json",
    "config.yaml",
]

_BEST_EFFORT: list[str] = [
    "medre.log",
    "receipts.json",
    "native-refs.json",
    "inspect-timeline.json",
    "evidence.json",
    "final-snapshot.json",
]

_SCENARIO_REQUIRED_LOGS: dict[str, tuple[str, ...]] = {
    "matrix_to_meshtastic": ("synapse.log", "meshtasticd.log"),
    "meshtastic_to_matrix": ("meshtasticd.log",),
    "bidirectional": ("synapse.log", "meshtasticd.log"),
}


def get_artifact_plan(scenario: str) -> dict[str, list[str]]:
    """Return the artifact plan for *scenario*.

    Parameters
    ----------
    scenario:
        One of :data:`SUPPORTED_SCENARIOS`, or any string.  Unknown
        scenarios fall back to requiring **both** ``synapse.log`` and
        ``meshtasticd.log`` (equivalent to the historical
        :data:`ARTIFACT_PLAN`).

    Returns
    -------
    dict[str, list[str]]
        ``{"required": [...], "best_effort": [...]}``
    """
    logs = _SCENARIO_REQUIRED_LOGS.get(
        scenario,
        ("synapse.log", "meshtasticd.log"),
    )
    return {
        "required": _BASE_REQUIRED + list(logs),
        "best_effort": list(_BEST_EFFORT),
    }


# ---------------------------------------------------------------------------
# Run directory creation
# ---------------------------------------------------------------------------


def create_run_directory(
    base_dir: str | Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> Path:
    """Create a timestamped artifact run directory.

    Parameters
    ----------
    base_dir:
        Parent directory for runs.  Defaults to
        ``.ci-artifacts/docker-bridge-runs/``.
    now_fn:
        Injectable clock for deterministic testing.

    Returns
    -------
    Path
        The created run directory (e.g.
        ``.ci-artifacts/docker-bridge-runs/2026-05-16T12-34-56Z/``).
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))
    ts = _now().strftime("%Y-%m-%dT%H-%M-%SZ")
    base = Path(base_dir) if base_dir is not None else _DEFAULT_ARTIFACT_BASE
    run_dir = base / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


# ---------------------------------------------------------------------------
# Config snapshot redaction
# ---------------------------------------------------------------------------


def redact_config_snapshot(config_data: dict[str, Any]) -> dict[str, Any]:
    """Redact tokens, passwords, and secrets from a config snapshot.

    Uses :func:`~medre.core.observability.sanitization.sanitize_for_log` for
    consistent redaction across the project.
    """
    return sanitize_for_log(config_data)


# ---------------------------------------------------------------------------
# Summary building
# ---------------------------------------------------------------------------


def build_summary(
    *,
    status: str,
    scenario: str,
    run_directory: str | Path,
    matrix: dict[str, Any] | None = None,
    meshtastic: dict[str, Any] | None = None,
    medre: dict[str, Any] | None = None,
    logs: dict[str, str | None] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    inspect_artifacts: list[str] | None = None,
    errors: list[str] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    artifact_plan: dict[str, list[str]] | None = None,
    artifact_paths: dict[str, str] | None = None,
    missing_artifacts: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build a summary.json-compliant dict.

    All string fields are sanitized for tokens/passwords.  The summary is
    always valid JSON even on failure — ``status`` may be ``"failed"`` or
    ``"partial"``.

    Parameters
    ----------
    status:
        One of ``"passed"``, ``"failed"``, ``"partial"``.
    scenario:
        One of :data:`SUPPORTED_SCENARIOS`.
    run_directory:
        Absolute path to the artifact run directory.
    matrix:
        Matrix evidence fields (container, room, event_id, ingress_path).
    meshtastic:
        Meshtastic evidence fields (daemon, inbound, outbound).
    medre:
        MEDRE evidence fields (event_id, receipt, native_refs, runtime,
        limitations).
    logs:
        Captured log output (pytest_stdout, pytest_stderr).  Truncated to
        :data:`_MAX_LOG_SIZE`.
    config_snapshot:
        Redacted config snapshot (already passed through
        :func:`redact_config_snapshot`).
    inspect_artifacts:
        List of inspect-related artifact file paths written.
    errors:
        Accumulated error strings (already sanitized).
    now_fn:
        Injectable clock for deterministic testing.
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))

    matrix_data = matrix or {}
    meshtastic_data = meshtastic or {}
    medre_data = medre or {}

    # Default limitations from medre section, falling back to module-level.
    limitations = medre_data.get("limitations", _LIMITATIONS)

    # Truncate logs to prevent unbounded summary size.
    safe_logs: dict[str, str | None] = {}
    if logs:
        for key, value in logs.items():
            if value is not None and len(value) > _MAX_LOG_SIZE:
                safe_logs[key] = value[:_MAX_LOG_SIZE] + "\n...[truncated]"
            else:
                safe_logs[key] = value
    else:
        safe_logs = {"pytest_stdout": None, "pytest_stderr": None}

    # Sanitize error strings.
    safe_errors: list[str] = []
    if errors:
        for err in errors:
            safe_errors.append(sanitize_error(err))

    # Sanitize string values in matrix/meshtastic/medre sections that might
    # contain tokens or passwords.
    def _redact_strings(d: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                out[k] = sanitize_error(v)
            elif isinstance(v, dict):
                out[k] = _redact_strings(v)
            elif isinstance(v, list):
                out[k] = [
                    sanitize_error(item) if isinstance(item, str) else item
                    for item in v
                ]
            else:
                out[k] = v
        return out

    return {
        "status": status,
        "scenario": scenario,
        "timestamp": _now().isoformat(),
        "run_directory": str(run_directory),
        "matrix": _redact_strings(matrix_data),
        "meshtastic": _redact_strings(meshtastic_data),
        "medre": {
            **_redact_strings(
                {k: v for k, v in medre_data.items() if k != "limitations"}
            ),
            "limitations": limitations,
        },
        "logs": safe_logs,
        "config_snapshot": config_snapshot,
        "inspect_artifacts": inspect_artifacts or [],
        "errors": safe_errors,
        "artifact_plan": artifact_plan if artifact_plan is not None else ARTIFACT_PLAN,
        "artifact_paths": artifact_paths or {},
        "missing_artifacts": missing_artifacts or {},
    }


# ---------------------------------------------------------------------------
# Summary writing
# ---------------------------------------------------------------------------


def write_summary(summary: dict[str, Any], run_directory: str | Path) -> Path:
    """Write ``summary.json`` to the run directory.

    Always writes, even if the summary contains errors or a failed status.
    Returns the path to the written file.
    """
    run_dir = Path(run_directory)
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
    )
    return summary_path


# ---------------------------------------------------------------------------
# Docker test result parsing
# ---------------------------------------------------------------------------


def _parse_pytest_output(
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Parse pytest output to extract test results for evidence.

    Returns a dict with parsed counts and any event_ids found in output.
    Best-effort parsing — failures return partial data.
    """
    result: dict[str, Any] = {
        "passed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "event_ids": [],
    }

    # Extract summary line: "X passed, Y failed, Z skipped"
    summary_match = re.search(
        r"(\d+) passed(?:,\s*(\d+) failed)?(?:,\s*(\d+) skipped)?(?:,\s*(\d+) error)?",
        stdout,
    )
    if summary_match:
        result["passed_count"] = int(summary_match.group(1) or 0)
        result["failed_count"] = int(summary_match.group(2) or 0)
        result["skipped_count"] = int(summary_match.group(3) or 0)
        result["error_count"] = int(summary_match.group(4) or 0)

    # Extract Matrix event IDs ($...) from output.
    event_ids = re.findall(r"\$[A-Za-z0-9_-]+", stdout)
    result["event_ids"] = list(dict.fromkeys(event_ids))[:20]  # dedupe, cap

    return result


# ---------------------------------------------------------------------------
# Structured metadata reading
# ---------------------------------------------------------------------------


def _read_run_metadata(run_dir: Path) -> dict[str, Any] | None:
    """Read ``run-metadata.json`` from the run directory.

    Returns ``None`` when the file is missing or cannot be parsed.
    Integration test fixtures write this file; the collector reads it.
    """
    metadata_path = run_dir / "run-metadata.json"
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Redacted config.yaml writing
# ---------------------------------------------------------------------------


def _yaml_escape_string(value: str) -> str:
    """Escape a string for a double-quoted YAML scalar.

    Doubles backslashes and double quotes so the value survives a YAML
    double-quoted scalar parse round-trip.  Other control characters are
    left to the consumer; the redacted snapshot only contains operator-safe
    strings at this point.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_yaml_value(value: Any, indent: int = 0) -> str:
    """Format a single scalar value as a YAML inline representation.

    Used for non-collection leaf values.  Returns the text placed after
    the ``key: `` separator.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{_yaml_escape_string(value)}"'
    # Lists and complex objects fall back to a JSON inline scalar so the
    # file remains valid YAML (YAML is a JSON superset).
    return json.dumps(value)


def _write_yaml_lines(
    data: dict[str, Any],
    lines: list[str],
    indent: int = 0,
) -> None:
    """Append YAML mapping lines for *data* into *lines*.

    Nested mappings recurse with increased indentation.  Non-mapping
    values are rendered as inline scalars.
    """
    pad = "  " * indent
    for key, value in sorted(data.items()):
        safe_key = str(key)
        if isinstance(value, dict):
            lines.append(f"{pad}{safe_key}:")
            _write_yaml_lines(value, lines, indent + 1)
        else:
            lines.append(f"{pad}{safe_key}: {_format_yaml_value(value)}")


def _write_redacted_config(run_dir: Path, config_data: dict[str, Any]) -> Path | None:
    """Write a redacted ``config.yaml`` to the run directory.

    Uses :func:`redact_config_snapshot` to strip secrets before writing.
    The output is a hand-rolled YAML mapping (no third-party YAML
    dependency).  Returns the path on success, ``None`` on failure.
    """
    redacted = redact_config_snapshot(config_data)
    config_path = run_dir / "config.yaml"
    try:
        lines: list[str] = []
        _write_yaml_lines(redacted, lines, 0)
        config_path.write_text("\n".join(lines) + "\n")
        return config_path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Log artifact collection from metadata
# ---------------------------------------------------------------------------


def _collect_log_artifacts(
    run_dir: Path,
    metadata: dict[str, Any] | None,
) -> dict[str, Path]:
    """Copy log files referenced in metadata to the run directory.

    Looks for ``log_paths`` in metadata mapping log names to source paths.
    """
    artifacts: dict[str, Path] = {}
    if metadata is None:
        return artifacts

    log_paths = metadata.get("log_paths", {})
    for name, source_path_str in log_paths.items():
        dest_name = f"{name}.log"
        dest_path = run_dir / dest_name
        try:
            source = Path(source_path_str)
            if source.exists():
                shutil.copy2(source, dest_path)
                artifacts[dest_name] = dest_path
        except Exception:
            pass  # best-effort

    return artifacts


# ---------------------------------------------------------------------------
# Storage-derived artifact export
# ---------------------------------------------------------------------------


async def _export_storage_artifacts_async(
    run_dir: Path,
    storage_path: str,
    event_id: str,
) -> dict[str, Path]:
    """Export storage-derived artifacts asynchronously.

    Opens the SQLite database read-only via
    :meth:`SQLiteStorage.open_readonly` and writes:
    - ``receipts.json`` from :meth:`list_receipts_for_event`
    - ``native-refs.json`` from :meth:`list_native_refs_for_event`
    - ``inspect-timeline.json`` from :func:`assemble_event_timeline`
    - ``evidence.json`` from :func:`collect_evidence_bundle`
    """
    from medre.core.storage.sqlite.storage import SQLiteStorage
    from medre.runtime.timeline import assemble_event_timeline
    from medre.runtime.trace import timeline_to_json

    artifacts: dict[str, Path] = {}

    storage = await SQLiteStorage.open_readonly(storage_path)
    try:
        # -- receipts.json --
        receipts = await storage.list_receipts_for_event(event_id)
        if receipts:
            import msgspec

            receipts_data = [json.loads(msgspec.json.encode(r)) for r in receipts]
            receipts_path = run_dir / "receipts.json"
            receipts_path.write_text(
                json.dumps(receipts_data, indent=2, default=str) + "\n",
            )
            artifacts["receipts.json"] = receipts_path

        # -- native-refs.json --
        native_refs = await storage.list_native_refs_for_event(event_id)
        if native_refs:
            import msgspec

            nrefs_data = [json.loads(msgspec.json.encode(r)) for r in native_refs]
            nrefs_path = run_dir / "native-refs.json"
            nrefs_path.write_text(
                json.dumps(nrefs_data, indent=2, default=str) + "\n",
            )
            artifacts["native-refs.json"] = nrefs_path

        # -- inspect-timeline.json --
        tl_result = await assemble_event_timeline(storage, event_id)
        if tl_result is not None and tl_result.get("timeline_entries"):
            tl_path = run_dir / "inspect-timeline.json"
            tl_path.write_text(
                timeline_to_json(tl_result["timeline_entries"]) + "\n",
            )
            artifacts["inspect-timeline.json"] = tl_path
    finally:
        await storage.close()

    # -- evidence.json (separate — manages its own DB connection) --
    from medre.runtime.evidence._bundle import collect_evidence_bundle

    evidence = await collect_evidence_bundle(
        storage_path=storage_path,
        event_id=event_id,
    )
    evidence_path = run_dir / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, default=str) + "\n",
    )
    artifacts["evidence.json"] = evidence_path

    return artifacts


def _export_storage_artifacts_sync(
    run_dir: Path,
    storage_path: str,
    event_id: str,
) -> dict[str, Path]:
    """Synchronous wrapper for :func:`_export_storage_artifacts_async`.

    Uses :func:`asyncio.run` to execute the async export.  Suitable for
    the artifact collector which runs in a subprocess context.
    """
    return asyncio.run(
        _export_storage_artifacts_async(run_dir, storage_path, event_id),
    )


# ---------------------------------------------------------------------------
# Artifact manifest collection
# ---------------------------------------------------------------------------


def _collect_artifact_manifest(
    run_dir: Path,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Collect artifact paths and identify missing required/best-effort files.

    Parameters
    ----------
    run_dir:
        The artifact run directory to scan.
    scenario:
        Optional scenario name.  When provided the required list is
        looked up via :func:`get_artifact_plan`.  When *None* the
        historical :data:`ARTIFACT_PLAN` (both logs) is used as the
        default plan.

    Returns a dict with:
    - ``artifact_paths``: mapping filename → absolute path for existing artifacts
    - ``missing``: mapping with ``required`` and ``best_effort`` lists of missing names
    """
    plan = get_artifact_plan(scenario) if scenario else ARTIFACT_PLAN
    artifact_paths: dict[str, str] = {}
    missing_required: list[str] = []
    missing_best_effort: list[str] = []

    for name in plan["required"]:
        path = run_dir / name
        # summary.json is written by the collector AFTER manifest
        # collection, so always treat it as present.
        if name == "summary.json" or path.exists():
            artifact_paths[name] = str(path)
        else:
            missing_required.append(name)

    for name in plan["best_effort"]:
        path = run_dir / name
        if path.exists():
            artifact_paths[name] = str(path)
        else:
            missing_best_effort.append(name)

    # pytest-stdout.log and pytest-stderr.log are always written by the
    # collector, but are not in the formal artifact plan (they are logs).
    for log_name in ("pytest-stdout.log", "pytest-stderr.log"):
        log_path = run_dir / log_name
        if log_path.exists():
            artifact_paths[log_name] = str(log_path)

    return {
        "artifact_paths": artifact_paths,
        "missing": {
            "required": missing_required,
            "best_effort": missing_best_effort,
        },
    }


def collect_docker_bridge_artifacts(
    scenario: str = "matrix_to_meshtastic",
    *,
    base_dir: str | Path | None = None,
    pytest_args: Sequence[str] = (),
    extra_env: dict[str, str] | None = None,
    timeout_minutes: int = 15,
    now_fn: Callable[[], datetime] | None = None,
    _run_pytest: Callable[..., tuple[int, str, str]] | None = None,
    _storage_export_fn: Callable[[Path, str, str], dict[str, Path]] | None = None,
) -> dict[str, Any]:
    """Collect Docker bridge artifacts for a given scenario.

    This is the main entry point.  It:

    1. Creates a timestamped run directory.
    2. Invokes Docker integration tests via pytest.
    3. Captures stdout/stderr, config snapshots, and inspect artifacts.
    4. Builds and writes ``summary.json``.
    5. Returns the summary dict.

    Parameters
    ----------
    scenario:
        One of :data:`SUPPORTED_SCENARIOS`.
    base_dir:
        Base directory for run directories.
    pytest_args:
        Additional pytest arguments.
    extra_env:
        Extra environment variables for the subprocess.
    timeout_minutes:
        Timeout for the pytest subprocess.
    now_fn:
        Injectable clock for deterministic testing.
    _run_pytest:
        Injectable pytest runner for testing.  Signature:
        ``(cmd, env, timeout, cwd) -> (returncode, stdout, stderr)``.
        When ``None``, runs via :func:`subprocess.run`.
    _storage_export_fn:
        Injectable storage artifact exporter for testing.  Signature:
        ``(run_dir, storage_path, event_id) -> dict[str, Path]``.
        When ``None``, uses :func:`_export_storage_artifacts_sync`.

    Returns
    -------
    dict[str, Any]
        The ``summary.json`` dict.
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))

    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError(
            f"Unsupported scenario {scenario!r}. "
            f"Choose from: {', '.join(SUPPORTED_SCENARIOS)}"
        )

    # -- Step 1: Create run directory ----------------------------------------
    errors: list[str] = []
    try:
        run_dir = create_run_directory(base_dir=base_dir, now_fn=_now)
    except Exception as exc:
        errors.append(f"Failed to create run directory: {exc}")
        run_dir = Path(base_dir or _DEFAULT_ARTIFACT_BASE) / "failed-run"

    # -- Step 2: Build pytest command ----------------------------------------
    test_selectors = _scenario_test_selectors(scenario)
    if not test_selectors:
        raise ValueError(
            f"No test selectors configured for scenario {scenario!r}. "
            f"Add selectors to _scenario_test_selectors() or choose from: "
            f"{', '.join(SUPPORTED_SCENARIOS)}"
        )
    default_args = [
        "-m",
        "docker",
        "-v",
        "--tb=short",
        "--timeout=300",
    ]
    all_args = default_args + list(pytest_args) + test_selectors

    cmd = ["python", "-m", "pytest"] + all_args

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)

    # -- Step 3: Run pytest --------------------------------------------------
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1

    try:
        if _run_pytest is not None:
            returncode, stdout, stderr = _run_pytest(
                cmd,
                env,
                timeout_minutes * 60,
                os.getcwd(),
            )
        else:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_minutes * 60,
                env=env,
            )
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = -1
        raw_stdout = exc.stdout or b""
        raw_stderr = exc.stderr or b""
        stdout = (
            raw_stdout.decode("utf-8", errors="replace")
            if isinstance(raw_stdout, bytes)
            else raw_stdout
        )
        stderr = (
            raw_stderr.decode("utf-8", errors="replace")
            if isinstance(raw_stderr, bytes)
            else raw_stderr
        ) + f"\nTimeout after {timeout_minutes} minutes"
        errors.append(f"Pytest timed out after {timeout_minutes} minutes")
    except FileNotFoundError as exc:
        returncode = -1
        stderr = str(exc)
        errors.append(f"Failed to run pytest: {exc}")
    except Exception as exc:
        returncode = -1
        stderr = str(exc)
        errors.append(f"Unexpected error running pytest: {exc}")

    # -- Step 4: Capture logs to run directory --------------------------------
    log_artifacts: list[str] = []
    try:
        run_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = run_dir / "pytest-stdout.log"
        stdout_path.write_text(stdout)
        log_artifacts.append(str(stdout_path))

        stderr_path = run_dir / "pytest-stderr.log"
        stderr_path.write_text(stderr)
        log_artifacts.append(str(stderr_path))
    except Exception as exc:
        errors.append(f"Failed to write log artifacts: {exc}")

    # -- Step 5: Read structured metadata (if available) ---------------------
    metadata: dict[str, Any] | None = None
    metadata = _read_run_metadata(run_dir)
    metadata_available = metadata is not None

    # -- Step 6: Export storage-derived artifacts (best-effort) ---------------
    storage_artifacts: dict[str, Path] = {}
    storage_path_from_meta: str | None = (
        metadata.get("storage_path") if metadata else None
    )
    event_id_from_meta: str | None = metadata.get("event_id") if metadata else None
    if storage_path_from_meta and event_id_from_meta:
        _export_fn = _storage_export_fn or _export_storage_artifacts_sync
        try:
            storage_artifacts = _export_fn(
                run_dir,
                storage_path_from_meta,
                event_id_from_meta,
            )
        except Exception as exc:
            errors.append(f"Storage artifact export failed: {exc}")

    # -- Step 7: Write redacted config.yaml -----------------------------------
    config_yaml_path: Path | None = None
    config_data_for_yaml: dict[str, Any] | None = None
    if metadata and metadata.get("config_data"):
        config_data_for_yaml = metadata["config_data"]
    # Fall back to existing config snapshot env data (collected later).

    # -- Step 8: Collect log artifacts from metadata --------------------------
    log_artifacts_from_meta = _collect_log_artifacts(run_dir, metadata)

    # -- Step 9: Parse results (deprecated regex fallback) --------------------
    parsed = _parse_pytest_output(stdout, stderr)

    medre_evidence_limitations_note: str | None = None
    if not metadata_available:
        medre_evidence_limitations_note = (
            "run-metadata.json not found: evidence derived from pytest "
            "stdout regex (deprecated fallback, less reliable)"
        )

    # -- Step 10: Collect config snapshot (best-effort) -----------------------
    config_snapshot: dict[str, Any] | None = None
    try:
        config_snapshot = _collect_config_snapshot(scenario, env)
    except Exception as exc:
        errors.append(f"Config snapshot collection failed: {exc}")

    # Write redacted config.yaml from metadata config_data, falling back to
    # the env-based config snapshot collected above.
    if config_data_for_yaml is not None:
        config_yaml_path = _write_redacted_config(run_dir, config_data_for_yaml)
    elif config_snapshot is not None:
        config_yaml_path = _write_redacted_config(run_dir, config_snapshot)

    # -- Step 11: Collect inspect artifacts (best-effort) --------------------
    inspect_artifacts: list[str] = []
    try:
        inspect_artifacts = _collect_inspect_artifacts(run_dir)
    except Exception as exc:
        errors.append(f"Inspect artifact collection failed: {exc}")

    # -- Step 12: Determine status --------------------------------------------
    if returncode == 0 and parsed["failed_count"] == 0 and parsed["error_count"] == 0:
        status = "passed"
    elif returncode == 0:
        status = "partial"
    elif parsed["passed_count"] > 0:
        status = "partial"
    else:
        status = "failed"

    if errors and status == "passed":
        status = "partial"

    # -- Step 13: Build scenario-specific evidence ----------------------------
    matrix_evidence = _build_matrix_evidence(parsed, stdout, scenario, env)
    meshtastic_evidence = _build_meshtastic_evidence(parsed, stdout, scenario, env)
    medre_evidence = _build_medre_evidence(parsed, stdout, scenario, env)

    # -- Step 14: Override with structured metadata (precedence) --------------
    if metadata:
        meta_matrix = metadata.get("matrix", {})
        for key in ("room", "event_id", "ingress_path", "container"):
            if key in meta_matrix and meta_matrix[key] is not None:
                matrix_evidence[key] = meta_matrix[key]

        meta_medre = metadata.get("medre", {})
        for key in ("event_id", "receipt", "native_refs"):
            if key in meta_medre and meta_medre[key] is not None:
                medre_evidence[key] = meta_medre[key]

        meta_meshtastic = metadata.get("meshtastic", {})
        if "packet_ids" in meta_meshtastic and meta_meshtastic["packet_ids"]:
            meshtastic_evidence.setdefault("outbound", {})["packet_ids"] = (
                meta_meshtastic["packet_ids"]
            )
        if "pubsub_proven" in meta_meshtastic:
            meshtastic_evidence.setdefault("inbound", {})["pubsub_proven"] = (
                meta_meshtastic["pubsub_proven"]
            )

    # -- Step 15: Collect artifact manifest -----------------------------------
    plan = get_artifact_plan(scenario)
    artifact_manifest = _collect_artifact_manifest(run_dir, scenario)

    # Report missing required artifacts as honest status notes (not errors
    # that would change the overall status — they are environmental limits).
    for missing in artifact_manifest["missing"]["required"]:
        _logger.warning("Missing required artifact: %s", missing)

    # Report missing best-effort artifacts as limitations.
    missing_best = artifact_manifest["missing"]["best_effort"]
    if "final-snapshot.json" in missing_best:
        medre_evidence.setdefault("limitations", list(_LIMITATIONS)).append(
            "final-snapshot.json not produced: direct PipelineRunner tests "
            "cannot generate it without a full MedreApp"
        )
    if not metadata_available and medre_evidence_limitations_note is not None:
        medre_evidence.setdefault("limitations", list(_LIMITATIONS)).append(
            medre_evidence_limitations_note
        )
    for missing in missing_best:
        if missing != "final-snapshot.json":
            _logger.info("Best-effort artifact missing: %s", missing)

    # -- Step 16: Build and write summary ------------------------------------
    # Aggregate all artifact paths into a single mapping.
    all_artifact_paths: dict[str, str] = dict(artifact_manifest["artifact_paths"])
    for name, path in storage_artifacts.items():
        all_artifact_paths[name] = str(path)
    for name, path in log_artifacts_from_meta.items():
        all_artifact_paths[name] = str(path)
    if config_yaml_path is not None:
        all_artifact_paths["config.yaml"] = str(config_yaml_path)

    summary = build_summary(
        status=status,
        scenario=scenario,
        run_directory=run_dir,
        matrix=matrix_evidence,
        meshtastic=meshtastic_evidence,
        medre=medre_evidence,
        logs={
            "pytest_stdout": stdout,
            "pytest_stderr": stderr,
        },
        config_snapshot=config_snapshot,
        inspect_artifacts=log_artifacts + inspect_artifacts,
        errors=errors,
        now_fn=_now,
        artifact_plan=plan,
        artifact_paths=all_artifact_paths,
        missing_artifacts=artifact_manifest["missing"],
    )

    try:
        summary_path = write_summary(summary, run_dir)
        _logger.info("Summary written to %s", summary_path)
    except Exception as exc:
        _logger.error("Failed to write summary: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# Scenario test selectors
# ---------------------------------------------------------------------------


def _scenario_test_selectors(scenario: str) -> list[str]:
    """Return pytest path/kw selectors for a scenario.

    ``matrix_to_meshtastic`` and ``bidirectional`` both include Matrix bridge
    tests.  ``meshtastic_to_matrix`` includes Meshtastic SDK bridge tests.
    ``bidirectional`` includes both.
    """
    if scenario == "matrix_to_meshtastic":
        return [
            "tests/integration/test_synapse_connectivity.py",
            "tests/integration/test_synapse_bridge_smoke.py",
            "tests/integration/test_synapse_run_session.py",
            "tests/integration/test_cross_adapter_artifact_run.py",
        ]
    elif scenario == "meshtastic_to_matrix":
        return [
            "tests/integration/test_meshtasticd_connectivity.py",
            "tests/integration/test_meshtasticd_sdk_bridge.py",
        ]
    elif scenario == "bidirectional":
        return [
            "tests/integration/test_synapse_connectivity.py",
            "tests/integration/test_synapse_bridge_smoke.py",
            "tests/integration/test_synapse_run_session.py",
            "tests/integration/test_meshtasticd_connectivity.py",
            "tests/integration/test_meshtasticd_sdk_bridge.py",
        ]
    return []


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------


def _build_matrix_evidence(
    parsed: dict[str, Any],
    stdout: str,
    scenario: str,
    env: dict[str, str],
) -> dict[str, Any]:
    """Build Matrix evidence from pytest output and environment."""
    event_ids = parsed.get("event_ids", [])

    # Detect ingress path from output.
    ingress_path: str | None = None
    if "ingress_path=sync_loop" in stdout:
        ingress_path = "sync_loop"
    elif "ingress_path=direct_on_room_message_fallback" in stdout:
        ingress_path = "direct_on_room_message_fallback"
    elif "sync_loop delivered" in stdout:
        ingress_path = "sync_loop"
    elif "direct _on_room_message fallback" in stdout:
        ingress_path = "direct_on_room_message_fallback"

    # Extract room ID from output or env.
    room: str | None = None
    room_match = re.search(r"(![A-Za-z0-9_-]+:[A-Za-z0-9.-]+)", stdout)
    if room_match:
        room = room_match.group(1)

    return {
        "container": env.get("MEDRE_SYNAPSE_IMAGE", "matrixdotorg/synapse:v1.153.0"),
        "room": room,
        "event_id": event_ids[0] if event_ids else None,
        "ingress_path": ingress_path,
    }


def _build_meshtastic_evidence(
    parsed: dict[str, Any],
    stdout: str,
    scenario: str,
    env: dict[str, str],
) -> dict[str, Any]:
    """Build Meshtastic evidence from pytest output and environment."""
    # Best-effort extraction of packet IDs from output.
    outbound_packet_ids: list[str] = []
    packet_matches = re.findall(r"packet[_ ]?id[=:]\s*(\d+)", stdout, re.IGNORECASE)
    outbound_packet_ids = list(dict.fromkeys(packet_matches))[:10]

    # Honest pubsub detection: only actual pubsub callback evidence proves
    # pubsub.  simulate_inbound explicitly means real pubsub is NOT proven.
    _has_pubsub_signal = "pubsub" in stdout.lower()
    _has_simulate_inbound = "simulate_inbound" in stdout

    inbound: dict[str, Any] = {
        "pubsub_proven": _has_pubsub_signal,
    }
    if _has_simulate_inbound and not _has_pubsub_signal:
        inbound["simulated_inbound"] = True

    return {
        "daemon": env.get("MEDRE_MESHTASTICD_IMAGE", "meshtastic/meshtasticd:2.7.15"),
        "inbound": inbound,
        "outbound": (
            {
                "packet_ids": outbound_packet_ids,
            }
            if outbound_packet_ids
            else None
        ),
    }


def _build_medre_evidence(
    parsed: dict[str, Any],
    stdout: str,
    scenario: str,
    env: dict[str, str],
) -> dict[str, Any]:
    """Build MEDRE runtime evidence from pytest output."""
    # Extract receipt status from output.
    receipt_status: str | None = None
    if "receipt_status='sent'" in stdout or 'receipt_status": "sent"' in stdout:
        receipt_status = "sent"
    elif "receipt_status='failed'" in stdout:
        receipt_status = "failed"

    # Extract native refs.
    native_refs: list[dict[str, str]] = []
    ref_matches = re.findall(
        r"native_ref:\s*\{([^}]+)\}",
        stdout,
    )
    for match in ref_matches:
        native_refs.append({"raw": match.strip()})

    return {
        "event_id": (parsed.get("event_ids") or [None])[0],
        "receipt": {"status": receipt_status} if receipt_status else None,
        "native_refs": native_refs,
        "runtime": {
            "passed": parsed.get("passed_count", 0),
            "failed": parsed.get("failed_count", 0),
            "skipped": parsed.get("skipped_count", 0),
            "errors": parsed.get("error_count", 0),
        },
        "limitations": _LIMITATIONS,
    }


# ---------------------------------------------------------------------------
# Config snapshot collection
# ---------------------------------------------------------------------------


def _collect_config_snapshot(
    scenario: str,
    env: dict[str, str],
) -> dict[str, Any] | None:
    """Collect a redacted config snapshot from environment variables.

    Reads the Docker-related environment variables and builds a safe snapshot
    that can be included in the summary.
    """
    raw: dict[str, Any] = {
        "synapse_image": env.get(
            "MEDRE_SYNAPSE_IMAGE", "matrixdotorg/synapse:v1.153.0"
        ),
        "synapse_port": env.get("MEDRE_SYNAPSE_PORT", "8008"),
        "meshtasticd_image": env.get(
            "MEDRE_MESHTASTICD_IMAGE", "meshtastic/meshtasticd:2.7.15"
        ),
        "meshtasticd_port": env.get("MEDRE_MESHTASTICD_PORT", "4403"),
        "meshtasticd_hwid": env.get("MEDRE_MESHTASTICD_HWID", "11"),
        "ready_timeout": env.get("MEDRE_DOCKER_READY_TIMEOUT", "120"),
    }
    return redact_config_snapshot(raw)


def _collect_inspect_artifacts(run_dir: Path) -> list[str]:
    """Collect paths to any inspect-related artifacts in the run directory.

    Looks for files like docker-compose configs, container inspect output,
    etc. that may have been placed by the test suite.
    """
    artifacts: list[str] = []
    for pattern in ("*.json", "*.log", "*.yaml", "*.yml", "*.toml"):
        for path in run_dir.glob(pattern):
            if path.name != "summary.json":
                artifacts.append(str(path))
    return artifacts
