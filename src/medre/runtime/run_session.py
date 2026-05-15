"""Operator-facing run-session: complete operate→send→inspect→stop→diagnose workflow.

Provides :func:`run_bridge_session` — a single async function that
exercises the full MEDRE lifecycle with persistent storage: start runtime,
inject a fake bridge message, poll for delivery, stop gracefully, write a
final snapshot, and produce a cross-linked evidence report.

Unlike :func:`~medre.runtime.smoke.run_fake_bridge_smoke` (which uses
in-memory storage and exits immediately), the run-session path is designed
for operators who need inspectable, persistent evidence artifacts.  The
resulting SQLite database, snapshot file, and JSON report can be queried
after the session completes using ``medre trace``, ``medre inspect``,
``medre evidence``, and ``medre diagnostics`` commands.

Fake injection stays scoped to this module.  No adapter-level publish
callback or public injection API is exposed.

Package boundary decision
-------------------------
Operator tools (smoke, drill, evidence, trace, recover, run_session) live
in ``runtime/`` because they depend on the runtime lifecycle (MedreApp,
RuntimeBuilder).  If a future ``medre.operator`` package is created, it
will import from ``runtime``, never the reverse.  No files should be moved
out of ``runtime/`` without updating this comment and the import graph.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from medre.adapters.base import (
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.config.loader import load_config
from medre.config.env import apply_env_overrides
from medre.core.events.canonical import CanonicalEvent, NativeMessageRef
from medre.core.events.kinds import EventKind
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

__all__ = ["run_bridge_session"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RECEIPT_POLL_TIMEOUT: float = 3.0
"""Seconds to wait for delivery receipts after event injection."""

_RECEIPT_POLL_INTERVAL: float = 0.1
"""Seconds between receipt polling attempts."""

_SUPPORTED_SCENARIOS: tuple[str, ...] = (
    "happy_path",
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "degraded_live_health",
)

_LIMITATIONS: list[str] = [
    "Fake adapters only — no real transport connectivity proven",
    "Persistent storage (SQLite) but no crash-recovery proof",
    "Single-event session — no sustained throughput or load evidence",
    "No reconnection resilience or retry-against-live proof",
    "Fire-and-forget delivery model for radio transports",
    (
        "Native refs are derived from actual stored receipts; adapters "
        "that return native_message_id=None (e.g. local enqueue) produce "
        "no native refs."
    ),
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_smoke_config_path() -> str | None:
    """Return the shipped fake-bridge-smoke.toml path if it exists."""
    this_dir = Path(__file__).resolve().parent
    candidate = (
        this_dir.parent.parent.parent
        / "examples" / "configs" / "fake-bridge-smoke.toml"
    )
    if candidate.is_file():
        return str(candidate)
    return None


def _make_session_event(
    adapter: Any,
    text: str,
) -> CanonicalEvent:
    """Create a canonical event with both 'body' and 'text' payload keys.

    Bridges the gap between FakeMatrixAdapter.make_event (stores under
    ``"body"``) and TextRenderer (reads ``payload["text"]``) so rendered
    output is non-empty and inspectable.
    """
    base = adapter.make_event(text=text, event_kind=EventKind.MESSAGE_TEXT)
    merged = dict(base.payload)
    merged["text"] = text
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


def _pick_source_adapter(app: MedreApp) -> tuple[str, Any]:
    """Select a deterministic source adapter for session injection.

    Prefers a Matrix-platform adapter since fake-bridge-smoke.toml routes
    originate from ``fake_matrix``.
    """
    for aid in sorted(app.adapters.keys()):
        adapter = app.adapters[aid]
        if getattr(adapter, "platform", None) == "matrix":
            return aid, adapter
    aid = sorted(app.adapters.keys())[0]
    return aid, app.adapters[aid]


async def _poll_for_receipts(
    storage: Any,
    event_id: str,
    timeout: float = _RECEIPT_POLL_TIMEOUT,
    interval: float = _RECEIPT_POLL_INTERVAL,
) -> list[Any]:
    """Poll storage for delivery receipts until found or timeout.

    Returns the receipt list (may be empty on timeout).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            receipts = await storage.list_receipts_for_event(event_id)
            if receipts:
                return receipts
        except Exception:
            pass
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            break
        await asyncio.sleep(min(interval, deadline - now))

    # Final attempt.
    try:
        return await storage.list_receipts_for_event(event_id)
    except Exception:
        return []


async def _collect_native_refs(
    app: MedreApp,
    outcomes: list[Any],
    event_id: str,
    errors: list[str],
) -> list[dict[str, str]]:
    """Resolve native refs for each successful delivery outcome.

    Derives evidence from actual stored receipts rather than fabricating
    platform-specific IDs.  Looks up ``NativeMessageRef`` records persisted
    by the pipeline when adapters return an ``AdapterDeliveryResult`` with
    ``native_message_id`` set.
    """
    refs: list[dict[str, str]] = []
    storage = app.storage
    if storage is None:
        return refs

    # Retrieve actual native refs stored by the pipeline for this event.
    native_ref_records: list[NativeMessageRef] = []
    try:
        native_ref_records = await storage.list_native_refs_for_event(event_id)
    except (AttributeError, TypeError):
        # Storage backend may not implement list_native_refs_for_event in
        # all test mocks; fall back gracefully.
        _logger.debug(
            "Storage does not support list_native_refs_for_event; "
            "falling back to per-adapter resolve_native_ref",
        )
    except Exception as exc:
        errors.append(f"Native ref lookup error: {exc}")
        return refs

    for nref in native_ref_records:
        # Only include outbound refs for adapters that have successful outcomes.
        if nref.direction != "outbound":
            continue
        has_success = any(
            o.status == "success" and o.target_adapter == nref.adapter
            for o in outcomes
        )
        if not has_success:
            continue
        # Verify via resolve_native_ref.
        try:
            resolved = await storage.resolve_native_ref(
                nref.adapter,
                nref.native_channel_id,
                nref.native_message_id,
            )
        except Exception as exc:
            errors.append(
                f"resolve_native_ref failed for adapter={nref.adapter}: {exc}"
            )
            continue
        refs.append({
            "adapter": nref.adapter,
            "channel": nref.native_channel_id or "",
            "native_id": nref.native_message_id,
            "resolves_to": resolved or nref.event_id,
        })

    return refs


def _build_cross_linked_commands(
    event_id: str,
    config_path: str | None,
    snapshot_path: str | None,
) -> dict[str, str]:
    """Build cross-linked CLI command strings for the report."""
    cfg_flag = f"--config {config_path}" if config_path else ""
    return {
        "trace": f"medre trace event {event_id} {cfg_flag}".strip(),
        "inspect_receipts": (
            f"medre inspect receipts --event {event_id} {cfg_flag}".strip()
        ),
        "evidence": (
            f"medre evidence --event {event_id} {cfg_flag} --json".strip()
        ),
        "final_snapshot": f"cat {snapshot_path}" if snapshot_path else "(not saved)",
    }


# ---------------------------------------------------------------------------
# Scenario injection helpers
# ---------------------------------------------------------------------------


def _expected_failure_kind(scenario: str) -> str | None:
    """Return the expected failure classification for a scenario."""
    return {
        "renderer_failure": "renderer_failure",
        "adapter_permanent_failure": "adapter_permanent",
        "adapter_transient_failure": "adapter_transient",
        "capacity_rejection": "capacity_rejection",
        "degraded_live_health": "degraded_health",
    }.get(scenario)


def _operator_interpretation(scenario: str) -> str:
    """Return operator-facing guidance for interpreting a scenario result."""
    return {
        "happy_path": (
            "All steps should succeed. If any step fails, investigate "
            "the specific failing component."
        ),
        "renderer_failure": (
            "Event injection should produce a 'failed' receipt with "
            "failure_kind=renderer_failure. The event is stored but "
            "never delivered. No native refs should be created."
        ),
        "adapter_permanent_failure": (
            "Delivery should fail with permanent_failure classification. "
            "A 'failed' receipt is persisted. No native ref for the "
            "failed adapter. Outbound_failed accounting should increment."
        ),
        "adapter_transient_failure": (
            "Delivery should fail with transient_failure classification "
            "and is_retryable=True. A 'failed' receipt is persisted. "
            "No native ref for the failed adapter."
        ),
        "capacity_rejection": (
            "Delivery should be rejected with capacity_rejection "
            "classification. No receipt should be persisted for the "
            "rejected delivery. Capacity_rejections accounting should "
            "increment."
        ),
        "degraded_live_health": (
            "Runtime should observe degraded adapter health without "
            "crashing. Event delivery may still succeed. The snapshot "
            "should reflect degraded health for the target adapter."
        ),
    }.get(scenario, "Unknown scenario.")


async def _inject_scenario(
    app: MedreApp,
    scenario: str,
    source_aid: str,
) -> str | None:
    """Inject failure conditions for the given scenario.

    Modifies runtime state in-place before event injection.  Returns
    ``None`` on success, or an error description if scenario setup failed.
    """
    if scenario == "renderer_failure":
        pipeline = app.pipeline_runner
        if pipeline is None or not hasattr(pipeline, "_rendering_pipeline"):
            return "No rendering pipeline to clear"
        pipeline._rendering_pipeline._renderers.clear()

    elif scenario == "adapter_permanent_failure":
        target_aid: str | None = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                target_aid = aid
                break
        if target_aid is None:
            return "No target adapter to patch for permanent failure"
        adapter = app.adapters[target_aid]
        original_deliver = adapter.deliver

        async def _failing_deliver(
            result: Any,
            _orig: Any = original_deliver,
        ) -> Any:
            raise AdapterPermanentError(
                "run-session: simulated permanent failure"
            )

        adapter.deliver = _failing_deliver  # type: ignore[assignment]

    elif scenario == "adapter_transient_failure":
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                target_aid = aid
                break
        if target_aid is None:
            return "No target adapter to patch for transient failure"
        adapter = app.adapters[target_aid]
        original_deliver = adapter.deliver

        async def _transient_deliver(
            result: Any,
            _orig: Any = original_deliver,
        ) -> Any:
            raise AdapterSendError(
                "run-session: simulated transient failure",
                transient=True,
            )

        adapter.deliver = _transient_deliver  # type: ignore[assignment]

    elif scenario == "capacity_rejection":
        cc = app._capacity_controller
        if cc is None:
            return "No capacity controller wired"
        # Exhaust the delivery semaphore.
        from medre.runtime.capacity import CapacityController
        from medre.config.model import RuntimeLimits
        small_cc = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=1,
                max_inflight_replay_events=1,
                delivery_acquire_timeout_seconds=0.1,
            ),
        )
        await small_cc._delivery_sem.acquire()
        small_cc._delivery_current = 1
        app._capacity_controller = small_cc
        app.pipeline_runner._capacity_controller = small_cc

    elif scenario == "degraded_live_health":
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            target_aid = aid
            break
        if target_aid is None:
            return "No adapter available for health patch"
        from medre.adapters.base import AdapterInfo, AdapterCapabilities, AdapterRole
        adapter = app.adapters[target_aid]

        # Preserve original capabilities.
        orig_caps: AdapterCapabilities | None = None
        try:
            info = await adapter.health_check()
            orig_caps = info.capabilities
        except Exception:
            orig_caps = AdapterCapabilities(text=True)

        async def _degraded_health() -> AdapterInfo:
            raw_role = getattr(adapter, "role", "unknown")
            role = (
                raw_role
                if isinstance(raw_role, AdapterRole)
                else AdapterRole.PRESENTATION
            )
            return AdapterInfo(
                adapter_id=adapter.adapter_id,
                platform=getattr(adapter, "platform", "unknown"),
                role=role,
                version="0.1.0",
                capabilities=orig_caps,
                health="degraded",
            )

        adapter.health_check = _degraded_health  # type: ignore[assignment]
        await app.refresh_live_health()

    return None


def _observed_failure_kind(outcomes: list[Any]) -> str | None:
    """Derive the observed failure classification from delivery outcomes."""
    for o in outcomes:
        if o.failure_kind is not None:
            return o.failure_kind.value
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_bridge_session(
    config_path: str | None = None,
    storage_path: str | None = None,
    snapshot_dir: str | None = None,
    *,
    message_text: str = "medre run-session test",
    scenario: str = "happy_path",
    now_fn: Callable[[], datetime] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run a complete operate→send→inspect→stop→diagnose session.

    Steps:

    1. Load config, override storage to SQLite at *storage_path*.
    2. Build runtime via :class:`RuntimeBuilder`.
    3. Start runtime via :meth:`MedreApp.start`.
    4. Inject one ``message.text`` event through the pipeline.
    5. Poll for delivery receipts (timeout 3 seconds).
    6. Trigger graceful shutdown via :meth:`MedreApp.stop`.
    7. Build and save final runtime snapshot.
    8. Inspect storage: retrieve event, receipts, native refs.
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
        Body text for the injected event.
    scenario:
        Failure scenario to inject.  One of: ``"happy_path"``,
        ``"renderer_failure"``, ``"adapter_permanent_failure"``,
        ``"adapter_transient_failure"``, ``"capacity_rejection"``,
        ``"degraded_live_health"``.
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

    # -- Resolve config path ------------------------------------------------
    resolved_config_path = config_path
    config_source_value = "explicit"
    if resolved_config_path is None:
        default = _default_smoke_config_path()
        if default is not None:
            resolved_config_path = default
            config_source_value = "default"

    # -- Resolve storage path -----------------------------------------------
    storage_provided = storage_path is not None
    if not storage_provided:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", prefix="medre-session-", delete=False,
        )
        storage_path = tmp.name
        tmp.close()

    # -- Step 1: Load config ------------------------------------------------
    try:
        config, source, paths = load_config(resolved_config_path)
    except Exception as exc:
        return {
            "status": "FAIL",
            "fail_reason": f"Config load error: {exc}",
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "storage_path": storage_path,
            "limitations": _LIMITATIONS,
        }

    config_source_value = source.value
    config = apply_env_overrides(config, paths)

    # Override storage to SQLite.
    config = dataclasses.replace(
        config,
        storage=dataclasses.replace(
            config.storage, backend="sqlite", path=storage_path,
        ),
    )

    # -- Step 2: Build runtime ----------------------------------------------
    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return {
            "status": "FAIL",
            "fail_reason": f"Runtime build error: {exc}",
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "storage_path": storage_path,
            "limitations": _LIMITATIONS,
        }

    # -- Step 3: Start runtime ----------------------------------------------
    try:
        await app.start()
    except Exception as exc:
        return {
            "status": "FAIL",
            "fail_reason": f"Runtime start error: {exc}",
            "evidence_level": "fake_run_session",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "storage_path": storage_path,
            "started_adapters": list(app.started_adapter_ids),
            "limitations": _LIMITATIONS,
        }

    # -- Step 4: Inject event -----------------------------------------------
    source_aid, source_adapter = _pick_source_adapter(app)
    event = _make_session_event(source_adapter, message_text)

    # Inject scenario-specific failure before event injection.
    scenario_error: str | None = None
    if is_failure_scenario:
        scenario_error = await _inject_scenario(app, scenario, source_aid)

    outcomes: list[Any] = []
    injection_error: str | None = None
    try:
        outcomes = await app.pipeline_runner.handle_ingress(event)
    except Exception as exc:
        injection_error = f"{type(exc).__name__}: {exc}"

    # -- Step 5: Poll for delivery receipts ---------------------------------
    receipts: list[Any] = []
    storage = app.storage
    collection_errors: list[str] = []
    if storage is not None and injection_error is None:
        try:
            receipts = await _poll_for_receipts(storage, event.event_id)
        except Exception as exc:
            collection_errors.append(f"Receipt polling error: {exc}")

    # Collect evidence while runtime is still running (storage is open).
    # Stored event
    stored_event: CanonicalEvent | None = None
    if storage is not None:
        try:
            stored_event = await storage.get(event.event_id)
        except Exception as exc:
            collection_errors.append(
                f"Stored event lookup error: {exc}"
            )

    # Native refs (must be collected before stop closes storage).
    native_refs = await _collect_native_refs(
        app, outcomes, event.event_id, collection_errors,
    )

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
            app, now_fn=now_fn, monotonic_fn=monotonic_fn,
        )
    except Exception as exc:
        collection_errors.append(f"Snapshot build error: {exc}")

    if snapshot_dir is None:
        snapshot_dir = str(Path(storage_path).parent)
    try:
        snap_file = Path(snapshot_dir) / f"snapshot-{event.event_id[:8]}.json"
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

    # Target adapters and route IDs from outcomes
    target_adapters = sorted({
        o.target_adapter
        for o in outcomes
        if o.status == "success"
    })
    route_ids = sorted({
        o.route_id
        for o in outcomes
        if o.route_id
    })

    # Receipt summaries
    receipt_summaries = [
        {
            "receipt_id": r.receipt_id,
            "target_adapter": r.target_adapter,
            "status": r.status,
            "source": r.source,
            "route_id": r.route_id,
        }
        for r in receipts
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
    has_success = any(o.status == "success" for o in outcomes)
    has_sent_receipt = any(r.status == "sent" for r in receipts)
    delivered_count = (
        accounting.get("outbound_delivered", 0) if accounting else 0
    )

    if is_failure_scenario:
        # Failure scenarios: PASS means the expected failure was observed.
        observed = _observed_failure_kind(outcomes)
        expected = _expected_failure_kind(scenario)
        passed = (
            observed == expected
            and scenario_error is None
        )
        fail_reasons: list[str] = []
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
            and delivered_count >= 1
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
        if delivered_count < 1:
            fail_reasons.append("Accounting outbound_delivered < 1")
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
        event.event_id, resolved_config_path, snapshot_path,
    )

    report: dict[str, Any] = {
        "status": "PASS" if passed else "FAIL",
        "evidence_level": "fake_run_session",
        "timestamp": _now().isoformat(),
        "config_source": config_source_value,
        "storage_path": storage_path,
        "storage_ephemeral": not storage_provided,
        "final_snapshot_path": snapshot_path,
        "event_id": event.event_id,
        "route_id": route_ids[0] if route_ids else None,
        "source_adapter": source_aid,
        "target_adapters": target_adapters,
        "delivery_receipts": receipt_summaries,
        "native_refs": native_refs,
        "accounting": accounting_display,
        "final_snapshot_checks": final_snapshot_checks,
        "commands": commands,
        "limitations": _LIMITATIONS,
    }

    if not passed:
        report["fail_reasons"] = fail_reasons

    if collection_errors:
        report["collection_errors"] = collection_errors

    # Scenario-specific report fields.
    if is_failure_scenario:
        report["scenario"] = scenario
        report["expected_failure_kind"] = _expected_failure_kind(scenario)
        report["observed_failure_kind"] = _observed_failure_kind(outcomes)
        report["operator_interpretation"] = _operator_interpretation(scenario)

    return report
