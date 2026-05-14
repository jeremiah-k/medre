"""Evidence bundle collector for operator support diagnostics.

Provides :func:`collect_evidence_bundle` — a single async function that
collects a comprehensive, read-only diagnostic bundle from a MEDRE
configuration.  The bundle is a JSON-safe ``dict`` suitable for attachment
to bug reports, support tickets, or operational dashboards.

Read-only by default
--------------------
The evidence command does **not** start the runtime or mutate storage
unless ``include_refresh_health=True`` is passed.  When live health is
requested, the runtime is started once, health is refreshed, and the
runtime is stopped cleanly.  The report unambiguously flags this via
``runtime_started: true``.

Report shape
------------
The top-level dict contains:

* ``schema_version`` — ``1`` (frozen during pre-release).
* ``status`` — ``"ok"`` | ``"partial"`` | ``"error"``.
* ``collected_at`` — ISO-8601 UTC timestamp.
* ``medre_version`` — installed package version.
* ``config_source`` — how the config file was found.
* ``runtime_started`` — ``true`` only when ``--include-refresh-health``
  was used and the runtime actually started.
* ``sections`` — grouped evidence (each with its own ``status``).
* ``errors`` — flat list of bounded error strings across all sections.
* ``limitations`` — honest list of what the evidence does **not** prove.

Section status values: ``"ok"``, ``"partial"``, ``"error"``, ``"skipped"``.

Public symbols
--------------
* :func:`collect_evidence_bundle` — main entry point.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from medre.config.loader import load_config, ConfigSource
from medre.config.paths import MedrePaths, MedrePathsError
from medre.config.env import apply_env_overrides, MedreEnvConfig, _SECRET_ENV_NAMES
from medre.observability.sanitization import sanitize_error

__all__ = ["collect_evidence_bundle"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
"""Evidence bundle schema version.  Frozen at 1 during pre-release."""

_MAX_ERROR_LEN: int = 512
"""Truncation limit for error strings in the report."""

_LIMITATIONS: list[str] = [
    "Evidence is a point-in-time snapshot, not continuous monitoring",
    "Diagnostics snapshot reflects build-time state unless --include-refresh-health is used",
    "Storage section requires an existing initialised database",
    "Fake adapters report synthetic health, not real transport connectivity",
    "No sustained throughput, reconnection resilience, or load evidence",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_version() -> str:
    """Return the MEDRE version string."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fixed_now() -> datetime:
    """Deterministic timestamp for non-live sections."""
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _fixed_mono() -> float:
    return 0.0


def _section_ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "error": None, "data": data}


def _section_partial(data: Any, error: str) -> dict[str, Any]:
    return {"status": "partial", "error": sanitize_error(error), "data": data}


def _section_error(error: str) -> dict[str, Any]:
    return {"status": "error", "error": sanitize_error(error), "data": None}


def _section_skipped(note: str) -> dict[str, Any]:
    return {"status": "skipped", "error": None, "data": None, "note": note}


def _compute_overall_status(sections: dict[str, dict[str, Any]]) -> str:
    """Compute overall status from per-section statuses."""
    statuses = {s.get("status") for s in sections.values()}
    if not statuses or statuses == {"skipped"}:
        return "ok"
    if statuses <= {"ok", "skipped"}:
        return "ok"
    if "error" in statuses and all(s in ("error", "skipped") for s in statuses):
        # All attempted sections errored.
        return "partial"
    return "partial"


# ---------------------------------------------------------------------------
# Config summary
# ---------------------------------------------------------------------------


def _collect_config_summary(
    config: Any,
    source: ConfigSource,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Build a redacted config summary section.

    Exposes only adapter wrapper metadata (transport, adapter_id, enabled,
    adapter_kind) — never adapter config internals (access_token, etc.).
    """
    adapters_summary: list[dict[str, Any]] = []
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapters_summary.append({
            "adapter_id": adapter_id,
            "adapter_kind": getattr(rtc, "adapter_kind", "real"),
            "enabled": rtc.enabled,
            "transport": transport,
        })

    routes_summary: list[dict[str, Any]] = []
    routes = config.routes
    if routes is not None:
        for route in routes.routes:
            routes_summary.append({
                "dest_adapters": sorted(route.dest_adapters),
                "directionality": route.directionality.value,
                "enabled": route.enabled,
                "route_id": route.route_id,
                "source_adapters": sorted(route.source_adapters),
            })

    # Limits — numeric, no secrets.
    limits = config.limits
    limits_summary: dict[str, Any] = {}
    if hasattr(limits, "max_inflight_deliveries"):
        limits_summary["delivery_acquire_timeout_seconds"] = (
            limits.delivery_acquire_timeout_seconds
        )
        limits_summary["max_inflight_deliveries"] = limits.max_inflight_deliveries
        limits_summary["max_inflight_replay_events"] = limits.max_inflight_replay_events
        limits_summary["shutdown_drain_timeout_seconds"] = (
            limits.shutdown_drain_timeout_seconds
        )

    # Env overrides — list names, redact secret values.
    env = MedreEnvConfig.from_environ()
    env_overrides_applied: list[str] = sorted(
        name for name, _value in env.provenance.redacted_items()
    )

    # Paths — already safe (no secrets).
    paths_summary = paths.to_diagnostics()

    # Storage.
    storage_backend = config.storage.backend if config.storage else "none"
    storage_path: str | None = None
    if config.storage and config.storage.path:
        storage_path = config.storage.path
    elif config.storage:
        storage_path = str(paths.database_path)

    data: dict[str, Any] = {
        "adapters": adapters_summary,
        "env_overrides_applied": env_overrides_applied,
        "limits": limits_summary,
        "logging_level": config.logging.level if config.logging else "INFO",
        "paths": paths_summary,
        "routes": routes_summary,
        "runtime_name": config.runtime.name if config.runtime else "medre",
        "storage_backend": storage_backend,
        "storage_path": storage_path,
    }

    return _section_ok(data)


# ---------------------------------------------------------------------------
# Route validation
# ---------------------------------------------------------------------------


def _collect_route_validation(config: Any) -> dict[str, Any]:
    """Validate route configuration and return results."""
    from medre.runtime.route_engine import (
        build_runtime_routes,
        RouteValidationError,
    )

    routes = config.routes
    route_list = routes.routes if routes is not None else []
    route_count = len(route_list)
    route_enabled = sum(1 for r in route_list if r.enabled)

    errors: list[str] = []
    warnings: list[str] = []

    if routes is not None:
        try:
            build_runtime_routes(routes)
        except RouteValidationError as exc:
            errors.append(str(exc))

    # Check adapter references.
    known_adapter_ids: set[str] = set()
    for _transport, adapter_id, rtc in config.adapters.all_configs():
        known_adapter_ids.add(adapter_id)

    for route in route_list:
        if not route.enabled:
            continue
        for sa in route.source_adapters:
            if sa not in known_adapter_ids:
                errors.append(
                    f"route {route.route_id!r}: source adapter {sa!r} not defined"
                )
        for da in route.dest_adapters:
            if da not in known_adapter_ids:
                errors.append(
                    f"route {route.route_id!r}: dest adapter {da!r} not defined"
                )

    data: dict[str, Any] = {
        "route_count": route_count,
        "route_enabled": route_enabled,
        "route_errors": [sanitize_error(e) for e in errors],
        "route_warnings": [sanitize_error(w) for w in warnings],
        "valid": len(errors) == 0,
    }

    if errors:
        return _section_partial(data, f"{len(errors)} route validation error(s)")
    return _section_ok(data)


# ---------------------------------------------------------------------------
# Diagnostics snapshot
# ---------------------------------------------------------------------------


async def _collect_diagnostics_snapshot(
    config: Any,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Build diagnostics snapshot section (no runtime start, no I/O)."""
    from medre.runtime.snapshot import build_runtime_snapshot

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_error("No adapters enabled in configuration")

    from medre.runtime.builder import RuntimeBuilder

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return _section_error(f"Runtime build error: {exc}")

    if not app.adapters:
        return _section_error(
            f"All {len(app.build_failures)} enabled adapter(s) failed to construct"
        )

    snapshot = build_runtime_snapshot(
        app,
        now_fn=_fixed_now,
        monotonic_fn=_fixed_mono,
    )
    return _section_ok(snapshot)


# ---------------------------------------------------------------------------
# Live health
# ---------------------------------------------------------------------------


async def _collect_live_health(
    config: Any,
    paths: MedrePaths,
) -> dict[str, Any]:
    """Start runtime, refresh health once, capture snapshot, stop cleanly.

    The caller is responsible for setting ``runtime_started`` in the
    top-level report based on whether this section succeeds.
    """
    from medre.runtime.snapshot import build_runtime_snapshot

    enabled_adapters = config.adapters.all_enabled()
    if not enabled_adapters:
        return _section_error("No adapters enabled — cannot start for health check")

    from medre.runtime.builder import RuntimeBuilder

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return _section_error(f"Runtime build error: {exc}")

    if not app.adapters:
        return _section_error(
            f"All {len(app.build_failures)} enabled adapter(s) failed to construct"
        )

    try:
        await app.start()
    except Exception as exc:
        return _section_error(f"Runtime startup failed: {exc}")

    try:
        await app.refresh_live_health()
        snapshot = build_runtime_snapshot(app)
        return _section_ok(snapshot)
    except Exception as exc:
        return _section_partial(None, f"Health refresh error: {exc}")
    finally:
        try:
            await app.stop()
        except Exception as exc:
            _logger.warning("Error during evidence live-health shutdown: %s", exc)


# ---------------------------------------------------------------------------
# Storage section
# ---------------------------------------------------------------------------


async def _collect_storage_section(
    config: Any,
    paths: MedrePaths,
    event_id: str | None,
    replay_run_id: str | None,
) -> dict[str, Any]:
    """Build storage evidence section using read-only access.

    Never creates or mutates the database file.  Missing/invalid storage
    produces a partial or skipped section.
    """
    from medre.config.paths import MedrePathsError as _MPE
    from medre.core.storage.sqlite import SQLiteStorage
    from medre.runtime.trace import (
        assemble_event_timeline,
        assemble_replay_timeline,
    )

    storage_config = config.storage

    # Memory backend — nothing persistent to inspect.
    if storage_config.backend == "memory":
        return _section_skipped(
            "Storage backend is 'memory' — no persistent data to inspect"
        )

    # Resolve DB path.
    if storage_config.path:
        try:
            db_path = str(paths.expand_placeholder(storage_config.path))
        except MedrePathsError as exc:
            return _section_error(f"Invalid storage path: {exc}")
    else:
        db_path = str(paths.database_path)

    db_exists = os.path.exists(db_path)
    data: dict[str, Any] = {
        "db_exists": db_exists,
        "db_path": db_path,
        "event": None,
        "event_count": None,
        "native_refs_for_event": None,
        "receipt_count": None,
        "replay_run_receipts": None,
        "timeline": None,
        "replay_timeline": None,
    }

    if not db_exists:
        return _section_partial(data, f"Database file does not exist: {db_path}")

    # Open read-only.
    storage: SQLiteStorage | None = None
    try:
        storage = await SQLiteStorage.open_readonly(db_path)
    except Exception as exc:
        return _section_partial(data, f"Cannot open database read-only: {exc}")

    try:
        # Counts.
        data["event_count"] = await storage.count_events()
        data["receipt_count"] = await storage.count_receipts()

        # Optional event lookup.
        if event_id is not None:
            import msgspec
            import json as _json

            event = await storage.get(event_id)
            if event is not None:
                data["event"] = _json.loads(msgspec.json.encode(event))

                # Fetch native refs and relations for timeline assembly.
                native_refs = await storage.list_native_refs_for_event(event_id)
                relations = await storage.list_relations(event_id)
                receipts = await storage.list_receipts_for_event(event_id)
                data["native_refs_for_event"] = [
                    _json.loads(msgspec.json.encode(r)) for r in native_refs
                ]

                data["timeline"] = assemble_event_timeline(
                    event, receipts, native_refs, relations,
                )

                # Compact incident summary using shared classification.
                from medre.observability.classification import (
                    infer_failure_kind,
                    failure_category,
                    recommended_commands,
                )

                receipt_dicts = [
                    _json.loads(msgspec.json.encode(r)) for r in receipts
                ]
                failed_count = sum(
                    1 for r in receipt_dicts
                    if r.get("status") in ("failed", "dead_lettered")
                )
                sent_count = sum(
                    1 for r in receipt_dicts
                    if r.get("status") == "sent"
                )

                first_failure_kind: str | None = None
                worst_category = "success"
                for r in receipt_dicts:
                    if r.get("status") in ("failed", "dead_lettered"):
                        fk = infer_failure_kind(
                            r.get("error"), r.get("status", ""),
                        )
                        if first_failure_kind is None:
                            first_failure_kind = fk
                        cat = failure_category(fk)
                        if cat != "success":
                            worst_category = cat
                            break

                has_replay = any(
                    r.get("source") == "replay" for r in receipt_dicts
                )
                has_native_refs = len(native_refs) > 0

                # Determine overall classification for the event.
                if failed_count == 0:
                    classification = "success"
                elif worst_category != "success":
                    classification = worst_category
                else:
                    classification = "unknown"

                cmds = recommended_commands(classification, event_id) \
                    if classification != "success" \
                    else [f"medre trace event {event_id}"]

                data["incident_summary"] = {
                    "event_id": event_id,
                    "event_kind": event.event_kind,
                    "source_adapter": event.source_adapter,
                    "first_failure_kind": first_failure_kind,
                    "classification": classification,
                    "replay_receipts_present": has_replay,
                    "native_refs_present": has_native_refs,
                    "receipt_count": len(receipt_dicts),
                    "failed_count": failed_count,
                    "sent_count": sent_count,
                    "recommended_commands": cmds,
                }
            # else: event not found — keep None, not an error for the section.

        # Optional replay-run receipts.
        if replay_run_id is not None:
            import msgspec
            import json as _json

            receipts = await storage.list_receipts_by_replay_run(replay_run_id)
            data["replay_run_receipts"] = [
                _json.loads(msgspec.json.encode(r)) for r in receipts
            ]

            if receipts:
                data["replay_timeline"] = assemble_replay_timeline(
                    replay_run_id, receipts, {},
                )

        # If event was requested but not found, report partial.
        if event_id is not None and data["event"] is None:
            return _section_partial(
                data, f"Event {event_id!r} not found in storage"
            )

        return _section_ok(data)
    except Exception as exc:
        return _section_partial(data, f"Storage query error: {exc}")
    finally:
        if storage is not None:
            await storage.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect_evidence_bundle(
    config_path: str | None = None,
    *,
    event_id: str | None = None,
    replay_run_id: str | None = None,
    include_refresh_health: bool = False,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Collect a comprehensive evidence bundle.

    Parameters
    ----------
    config_path:
        Path to TOML config file (``None`` uses standard discovery).
    event_id:
        When provided, include the event and its delivery receipts from
        storage (read-only).
    replay_run_id:
        When provided, include delivery receipts for this replay run from
        storage (read-only).
    include_refresh_health:
        When ``True``, start the runtime once, refresh adapter health,
        capture a live snapshot, and stop the runtime cleanly.  The report
        will have ``runtime_started: true``.
    now_fn:
        Injectable clock for deterministic testing.

    Returns
    -------
    dict[str, Any]
        JSON-safe evidence bundle with ``schema_version``, ``status``,
        ``sections``, ``errors``, and ``limitations``.
    """
    _now = now_fn or _now_utc

    # -- Step 1: Load config ------------------------------------------------
    try:
        config, source, paths = load_config(config_path)
    except Exception as exc:
        return {
            "collected_at": _now().isoformat(),
            "config_source": None,
            "errors": [sanitize_error(str(exc))],
            "limitations": _LIMITATIONS,
            "medre_version": _get_version(),
            "runtime_started": False,
            "schema_version": SCHEMA_VERSION,
            "sections": {},
            "status": "error",
        }

    config = apply_env_overrides(config, paths)

    sections: dict[str, Any] = {}
    errors: list[str] = []
    runtime_started = False

    # -- Config summary (always ok if we got here) --------------------------
    sections["config_summary"] = _collect_config_summary(config, source, paths)

    # -- Route validation ---------------------------------------------------
    sections["route_validation"] = _collect_route_validation(config)
    if sections["route_validation"]["error"]:
        errors.append(sections["route_validation"]["error"])

    # -- Diagnostics snapshot (no start) ------------------------------------
    sections["diagnostics_snapshot"] = await _collect_diagnostics_snapshot(
        config, paths,
    )
    if sections["diagnostics_snapshot"]["error"]:
        errors.append(sections["diagnostics_snapshot"]["error"])

    # -- Live health (only if requested) ------------------------------------
    if include_refresh_health:
        sections["live_health"] = await _collect_live_health(config, paths)
        # Mark runtime as started if the section isn't an outright error
        # from the build phase (build errors mean it never started).
        lh_status = sections["live_health"].get("status")
        if lh_status in ("ok", "partial"):
            runtime_started = True
        if sections["live_health"]["error"]:
            errors.append(sections["live_health"]["error"])
    else:
        sections["live_health"] = _section_skipped(
            "Use --include-refresh-health to populate this section"
        )

    # -- Storage section ----------------------------------------------------
    sections["storage"] = await _collect_storage_section(
        config, paths, event_id, replay_run_id,
    )
    if sections["storage"]["error"]:
        errors.append(sections["storage"]["error"])

    # -- Compute overall status ---------------------------------------------
    overall = _compute_overall_status(sections)

    return {
        "collected_at": _now().isoformat(),
        "config_source": source.value,
        "errors": errors,
        "limitations": _LIMITATIONS,
        "medre_version": _get_version(),
        "runtime_started": runtime_started,
        "schema_version": SCHEMA_VERSION,
        "sections": sections,
        "status": overall,
    }
