"""Core run-session orchestration.

Provides :func:`run_bridge_session` — a single async function that
exercises the full MEDRE lifecycle with persistent storage: start runtime,
inject bridge message(s), poll for delivery, stop gracefully, write a
final snapshot, and produce a cross-linked evidence report.

Unlike :func:`~medre.runtime.smoke.run_fake_bridge_smoke` (which uses
in-memory storage by default), the run-session path uses SQLite storage
(temporary or user-provided) for all sessions.  This ensures every session
produces inspectable, persistent evidence artifacts.  The resulting SQLite
database, snapshot file, and JSON report can be queried after the session
completes using ``medre trace``, ``medre inspect``, ``medre evidence``,
and ``medre diagnostics`` commands.

Package boundary decision
-------------------------
Operator tools (smoke, drill, evidence, trace, recover, run_session) live
in ``runtime/`` because they depend on the runtime lifecycle (MedreApp,
RuntimeBuilder).  If a future ``medre.operator`` package is created, it
will import from ``runtime``, never the reverse.  No files should be moved
out of ``runtime/`` without updating this comment and the import graph.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent
from medre.core.observability.sanitization import sanitize_error
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

from .evidence import (
    _collect_native_refs,
    _make_session_event,
    _pick_source_adapter,
    _poll_for_receipts,
)
from .report import _LIMITATIONS, _build_cross_linked_commands
from .scenario import (
    _expected_failure_kind,
    _inject_scenario,
    _observed_failure_kind,
    _operator_interpretation,
    _simulation_method,
    scenario_category,
)

__all__ = ["run_bridge_session", "DEFAULT_INGRESS_MODE"]

_logger = logging.getLogger(__name__)

# Supported ingress modes for run-session injection.
_SUPPORTED_INGRESS_MODES: frozenset[str] = frozenset(
    {
        "direct_pipeline",
        "adapter_callback",
    }
)

#: Default ingress mode: inject events directly into the pipeline via
#: :meth:`~medre.core.engine.pipeline.PipelineRunner.handle_ingress`.
DEFAULT_INGRESS_MODE: str = "direct_pipeline"


# ---------------------------------------------------------------------------
# Config path resolution
# ---------------------------------------------------------------------------


def _default_smoke_config_path() -> str | None:
    """Return the shipped fake-bridge-smoke.toml path if it exists."""
    this_dir = Path(__file__).resolve().parent
    candidate = (
        this_dir.parent.parent.parent.parent
        / "examples"
        / "configs"
        / "fake-bridge-smoke.toml"
    )
    if candidate.is_file():
        return str(candidate)
    return None


@dataclasses.dataclass(frozen=True)
class _ResolvedPaths:
    """Resolved config and storage paths for a run-session."""

    config_path: str | None
    config_source: str
    storage_path: str
    storage_ephemeral: bool


def _resolve_paths(
    config_path: str | None,
    storage_path: str | None,
) -> _ResolvedPaths:
    """Resolve config and storage paths for run-session.

    Falls back to the shipped ``fake-bridge-smoke.toml`` when
    *config_path* is ``None``.  Creates a temporary SQLite file when
    *storage_path* is ``None``.
    """
    resolved_config_path = config_path
    config_source_value = "explicit"
    if resolved_config_path is None:
        default = _default_smoke_config_path()
        if default is not None:
            resolved_config_path = default
            config_source_value = "default"

    storage_provided = storage_path is not None
    if not storage_provided:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db",
            prefix="medre-session-",
            delete=False,
        )
        storage_path = tmp.name
        tmp.close()

    return _ResolvedPaths(
        config_path=resolved_config_path,
        config_source=config_source_value,
        storage_path=storage_path,  # type: ignore[arg-type]
        storage_ephemeral=not storage_provided,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_bridge_session(
    config_path: str | None = None,
    storage_path: str | None = None,
    snapshot_dir: str | None = None,
    *,
    message_text: str = "medre run-session test",
    message_count: int = 1,
    scenario: str = "happy_path",
    ingress_mode: str = DEFAULT_INGRESS_MODE,
    now_fn: Callable[[], datetime] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run a complete operate→send→inspect→stop→diagnose session.

    Steps:

    1. Load config, override storage to SQLite at *storage_path*.
    2. Build runtime via :class:`RuntimeBuilder`.
    3. Start runtime via :meth:`MedreApp.start`.
    4. Inject ``message_count`` ``message.text`` events through the pipeline.
    5. Poll for delivery receipts (timeout 3 seconds per event).
    6. Trigger graceful shutdown via :meth:`MedreApp.stop`.
    7. Build and save final runtime snapshot.
    8. Inspect storage: retrieve events, receipts, native refs.
    9. Return compact operator report with cross-linked commands.

    Parameters
    ----------
    config_path:
        Path to TOML config file.  Defaults to
        ``examples/configs/fake-bridge-smoke.toml`` when available.
    storage_path:
        Path for the SQLite database.  Required for persistent evidence.
        When ``None``, a temporary file is created and its path is
        included in the report.
    snapshot_dir:
        Directory to write the final snapshot JSON.  Defaults to the
        parent directory of *storage_path*.
    message_text:
        Body text for the injected event(s).
    message_count:
        Number of events to inject.  When > 1, multiple events are
        injected sequentially and evidence is aggregated.  Report
        includes ``message_count`` and ``event_ids`` fields.
    scenario:
        Failure scenario to inject.  One of: ``"happy_path"``,
        ``"renderer_failure"``, ``"adapter_permanent_failure"``,
        ``"adapter_transient_failure"``, ``"capacity_rejection"``,
        ``"degraded_live_health"``.
    ingress_mode:
        How events enter the pipeline.  ``"direct_pipeline"`` (default)
        injects via ``handle_ingress()`` and collects
        ``DeliveryOutcome`` objects directly.  ``"adapter_callback"``
        injects via ``adapter.simulate_inbound()`` which goes through
        the adapter's ``publish_inbound`` callback — closer to the
        real adapter ingress path but does not return outcomes
        (evidence is collected from storage polling instead).
    now_fn:
        Injectable clock for deterministic testing.
    monotonic_fn:
        Injectable monotonic clock for deterministic testing.

    Returns
    -------
    dict[str, Any]
        Compact operator report.  JSON-safe.
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))
    is_failure_scenario = scenario != "happy_path"

    # -- Resolve config and storage paths ------------------------------------
    paths_resolved = _resolve_paths(config_path, storage_path)
    resolved_config_path = paths_resolved.config_path
    config_source_value = paths_resolved.config_source
    storage_path = paths_resolved.storage_path
    storage_provided = not paths_resolved.storage_ephemeral

    # -- Step 1: Load config ------------------------------------------------
    try:
        config, source, paths = load_config(resolved_config_path)
    except Exception as exc:
        return {
            "status": "failed",
            "command": "run_session",
            "fail_reason": sanitize_error(f"Config load error: {exc}"),
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "storage_path": storage_path,
            "limitations": _LIMITATIONS,
            "sanitized": True,
        }

    config_source_value = source.value
    config = apply_env_overrides(config, paths)

    # Override storage to SQLite.
    config = dataclasses.replace(
        config,
        storage=dataclasses.replace(
            config.storage,
            backend="sqlite",
            path=storage_path,
        ),
    )

    # -- Step 2: Build runtime ----------------------------------------------
    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return {
            "status": "failed",
            "command": "run_session",
            "fail_reason": sanitize_error(f"Runtime build error: {exc}"),
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "storage_path": storage_path,
            "limitations": _LIMITATIONS,
            "sanitized": True,
        }

    # -- Step 3: Start runtime ----------------------------------------------
    try:
        await app.start()
    except Exception as exc:
        return {
            "status": "failed",
            "command": "run_session",
            "fail_reason": sanitize_error(f"Runtime start error: {exc}"),
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "storage_path": storage_path,
            "started_adapters": list(app.started_adapter_ids),
            "limitations": _LIMITATIONS,
            "sanitized": True,
        }

    # -- Step 4: Inject event(s) --------------------------------------------
    source_aid, source_adapter = _pick_source_adapter(app)

    if ingress_mode not in _SUPPORTED_INGRESS_MODES:
        return {
            "status": "failed",
            "command": "run_session",
            "fail_reason": f"Invalid ingress_mode: {ingress_mode!r}",
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "storage_path": storage_path,
            "limitations": _LIMITATIONS,
        }

    # Inject scenario-specific failure before event injection.
    scenario_error: str | None = None
    if is_failure_scenario:
        scenario_error = await _inject_scenario(
            app,
            scenario,
            source_aid,
            ingress_mode=ingress_mode,
        )

    # Inject multiple events if message_count > 1.
    all_outcomes: list[Any] = []
    event_ids: list[str] = []
    injection_error: str | None = None

    for _i in range(message_count):
        event = _make_session_event(source_adapter, message_text)
        event_ids.append(event.event_id)

        try:
            if ingress_mode == "adapter_callback":
                # Adapter-callback path: inject through the adapter's
                # simulate_inbound, which calls ctx.publish_inbound.
                # This does not return DeliveryOutcomes — evidence is
                # collected from storage polling below.
                await source_adapter.simulate_inbound(event)
            else:
                # Direct-pipeline path: inject through handle_ingress,
                # which returns DeliveryOutcomes for immediate evidence.
                outcomes = await app.pipeline_runner.handle_ingress(event)
                all_outcomes.extend(outcomes)
        except Exception as exc:
            if injection_error is None:
                injection_error = f"{type(exc).__name__}: {exc}"
            # Continue injecting remaining events even if one fails.

    # Use first event_id for primary report fields; all IDs in event_ids list.
    primary_event_id = event_ids[0]

    # -- Step 5: Poll for delivery receipts ---------------------------------
    all_receipts: list[Any] = []
    storage = app.storage
    collection_errors: list[str] = []
    if storage is not None and injection_error is None:
        for eid in event_ids:
            try:
                receipts = await _poll_for_receipts(storage, eid)
                all_receipts.extend(receipts)
            except Exception as exc:
                collection_errors.append(f"Receipt polling error: {exc}")

    # Collect evidence while runtime is still running (storage is open).
    # Stored event (primary)
    stored_event: CanonicalEvent | None = None
    if storage is not None:
        try:
            stored_event = await storage.get(primary_event_id)
        except Exception as exc:
            collection_errors.append(f"Stored event lookup error: {exc}")

    # Native refs (must be collected before stop closes storage).
    all_native_refs: list[dict[str, str]] = []
    for eid in event_ids:
        event_receipts = [r for r in all_receipts if r.event_id == eid]
        refs = await _collect_native_refs(
            app,
            event_receipts,
            eid,
            collection_errors,
        )
        all_native_refs.extend(refs)

    # -- Step 6: Graceful shutdown ------------------------------------------
    try:
        await app.stop()
    except Exception as exc:
        _logger.warning("Session stop error (non-fatal): %s", exc)

    # -- Step 7: Build and save final snapshot ------------------------------
    snapshot_path: str | None = None
    snap: dict[str, Any] = {}
    try:
        snap = build_runtime_snapshot(
            app,
            now_fn=now_fn,
            monotonic_fn=monotonic_fn,
        )
    except Exception as exc:
        collection_errors.append(f"Snapshot build error: {exc}")

    if snapshot_dir is None:
        snapshot_dir = str(Path(storage_path).parent)
    try:
        snap_file = Path(snapshot_dir) / f"snapshot-{primary_event_id[:8]}.json"
        snap_file.parent.mkdir(parents=True, exist_ok=True)
        snap_file.write_text(
            json.dumps(snap, indent=2, sort_keys=True, default=str) + "\n",
        )
        snapshot_path = str(snap_file)
    except Exception as exc:
        _logger.warning("Snapshot write error: %s", exc)

    # -- Step 8: Inspect (already collected before stop) --------------------
    # Accounting
    accounting: dict[str, int] | None = None
    accounting_obj = getattr(app, "_runtime_accounting", None)
    if accounting_obj is not None and hasattr(accounting_obj, "snapshot"):
        try:
            accounting = accounting_obj.snapshot()
        except Exception as exc:
            collection_errors.append(f"Accounting snapshot error: {exc}")

    # Target adapters and route IDs from outcomes, falling back to receipts.
    if all_outcomes:
        target_adapters = sorted(
            {o.target_adapter for o in all_outcomes if o.status == "success"}
        )
        route_ids = sorted({o.route_id for o in all_outcomes if o.route_id})
    else:
        target_adapters = sorted(
            {r.target_adapter for r in all_receipts if r.status == "sent"}
        )
        route_ids = sorted({r.route_id for r in all_receipts if r.route_id})

    # Receipt summaries
    receipt_summaries = [
        {
            "receipt_id": r.receipt_id,
            "target_adapter": r.target_adapter,
            "status": r.status,
            "source": r.source,
            "route_id": r.route_id,
            "event_id": r.event_id,
            "delivery_plan_id": r.delivery_plan_id,
            "error": r.error,
            "attempt_number": r.attempt_number,
            "native_message_id": r.adapter_message_id,
        }
        for r in all_receipts
    ]

    # Final snapshot checks
    lifecycle = snap.get("lifecycle", {})
    runtime_state = lifecycle.get("runtime_state", "unknown")
    final_snapshot_checks = {
        "schema_version": snap.get("schema_version", SCHEMA_VERSION),
        "runtime_state": runtime_state,
    }

    # -- Step 9: Build report -----------------------------------------------
    event_stored = stored_event is not None
    has_sent_receipt = any(r.status == "sent" for r in all_receipts)
    has_success = (
        any(o.status == "success" for o in all_outcomes)
        if all_outcomes
        else has_sent_receipt
    )
    delivered_count = accounting.get("outbound_delivered", 0) if accounting else 0

    if is_failure_scenario:
        # Failure scenarios: passed means the expected failure was observed.
        scenario_category(scenario)
        _simulation_method(scenario)

        if scenario == "degraded_live_health":
            # Health scenario — not a failure_kind scenario.
            # Check if runtime is still running and health is degraded.
            adapter_health_entries = (
                snap.get("health", {})
                .get(
                    "live_health",
                    {},
                )
                .get("adapters", {})
            )
            observed_health = "degraded"
            for _aid, entry in adapter_health_entries.items():
                if entry.get("health") == "degraded":
                    observed_health = "degraded"
                    break
            else:
                # No degraded entry found — may still be OK if we patched it.
                observed_health = "unknown"
            passed = scenario_error is None and observed_health == "degraded"
            fail_reasons: list[str] = []
            if scenario_error is not None:
                fail_reasons.append(f"Scenario setup failed: {scenario_error}")
            if observed_health != "degraded":
                fail_reasons.append(
                    f"Expected health=degraded, observed={observed_health}"
                )
        else:
            # Failure-kind scenarios (renderer, adapter permanent/transient,
            # capacity).
            if ingress_mode == "adapter_callback" and not all_outcomes:
                observed = _observed_failure_kind(all_receipts, use_receipts=True)
            else:
                observed = _observed_failure_kind(all_outcomes)
            expected = _expected_failure_kind(scenario)
            passed = observed == expected and scenario_error is None
            fail_reasons = []
            if scenario_error is not None:
                fail_reasons.append(f"Scenario setup failed: {scenario_error}")
            if observed != expected:
                fail_reasons.append(
                    f"Expected failure_kind={expected}, observed={observed}"
                )
    else:
        # Happy path: all steps must succeed.
        passed = (
            event_stored
            and has_success
            and has_sent_receipt
            and delivered_count >= message_count
            and injection_error is None
            and runtime_state == "stopped"
        )
        fail_reasons = []
        if injection_error is not None:
            fail_reasons.append(f"Event injection failed: {injection_error}")
        if not event_stored:
            fail_reasons.append("Event not found in storage")
        if not has_success:
            fail_reasons.append("No successful delivery outcome")
        if not has_sent_receipt:
            fail_reasons.append("No receipt with status 'sent'")
        if delivered_count < message_count:
            fail_reasons.append(
                f"Accounting outbound_delivered {delivered_count} < {message_count}"
            )
        if runtime_state != "stopped":
            fail_reasons.append(
                f"Runtime state is '{runtime_state}', expected 'stopped'"
            )

    # Accounting printer uses the same 5 field names as run_commands.py.
    accounting_display: dict[str, int] | None = None
    if accounting is not None:
        accounting_display = {
            "inbound": accounting.get("inbound_accepted", 0),
            "outbound_delivered": accounting.get("outbound_delivered", 0),
            "outbound_failed": accounting.get("outbound_failed", 0),
            "loop_prevented": accounting.get("loop_prevented", 0),
            "capacity_rejections": accounting.get("capacity_rejections", 0),
        }

    commands = _build_cross_linked_commands(
        primary_event_id,
        resolved_config_path,
        snapshot_path,
        storage_path=storage_path,
    )

    # -- Sanitize error fields -----------------------------------------------
    sanitized = False

    def _sanitize(text: str) -> str:
        nonlocal sanitized
        result = sanitize_error(text)
        if result != text:
            sanitized = True
        return result

    # Sanitize fail reasons.
    fail_reasons = [_sanitize(r) for r in fail_reasons]

    # Sanitize collection errors.
    if collection_errors:
        collection_errors = [_sanitize(e) for e in collection_errors]

    # Sanitize injection error if present.
    if injection_error is not None:
        injection_error = _sanitize(injection_error)

    report: dict[str, Any] = {
        "status": "passed" if passed else "failed",
        "command": "run_session",
        "evidence_level": "fake_run_session",
        "ingress_mode": ingress_mode,
        "timestamp": _now().isoformat(),
        "generated_at": _now().isoformat(),
        "config_source": config_source_value,
        "storage_path": storage_path,
        "storage_ephemeral": not storage_provided,
        "final_snapshot_path": snapshot_path,
        "event_id": primary_event_id,
        "event_ids": event_ids,
        "message_count": message_count,
        "route_id": route_ids[0] if route_ids else None,
        "source_adapter": source_aid,
        "target_adapters": target_adapters,
        "delivery_receipts": receipt_summaries,
        "native_refs": all_native_refs,
        "accounting": accounting_display,
        "final_snapshot_checks": final_snapshot_checks,
        "commands": commands,
        "errors": list(collection_errors) if collection_errors else [],
        "limitations": _LIMITATIONS,
    }

    if sanitized:
        report["sanitized"] = True

    if not passed:
        report["fail_reasons"] = fail_reasons

    if collection_errors:
        report["collection_errors"] = collection_errors

    # Scenario-specific report fields.
    report["operator_interpretation"] = _operator_interpretation(scenario)
    if is_failure_scenario:
        report["scenario"] = scenario
        report["scenario_category"] = scenario_category(scenario)
        report["simulated"] = True
        report["simulation_method"] = _simulation_method(scenario)

        if scenario == "degraded_live_health":
            # Health scenario — use health-specific fields instead of
            # failure_kind fields.
            adapter_health_entries = (
                snap.get("health", {})
                .get(
                    "live_health",
                    {},
                )
                .get("adapters", {})
            )
            observed_health = "unknown"
            for _aid, entry in adapter_health_entries.items():
                if entry.get("health") == "degraded":
                    observed_health = "degraded"
                    break
            report["expected_health"] = "degraded"
            report["observed_health"] = observed_health
        else:
            # Failure-kind scenarios.
            report["expected_failure_kind"] = _expected_failure_kind(scenario)
            if ingress_mode == "adapter_callback" and not all_outcomes:
                report["observed_failure_kind"] = _observed_failure_kind(
                    all_receipts, use_receipts=True
                )
            else:
                report["observed_failure_kind"] = _observed_failure_kind(all_outcomes)
    else:
        report["scenario_category"] = "happy_path"
        report["simulated"] = False

    return report
