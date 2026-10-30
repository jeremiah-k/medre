"""Operator-facing fake bridge smoke runner.

Provides :func:`run_fake_bridge_smoke` — a single async function that
proves the MEDRE runtime pipeline works end-to-end with fake adapters.
Storage backend is determined by the config file (``storage.backend``):
``"memory"`` for ephemeral, ``"sqlite"`` with ``storage.path`` for
persistent evidence.  Docker-free, network-free, SDK-free.

Produces a compact evidence report (plain dict) that can be serialised
as JSON.  The CLI surface ``medre smoke`` calls this function and prints
the report.

Pipeline exercise
-----------------
The smoke injects one ``message.text`` event via
:meth:`~medre.core.engine.pipeline.PipelineRunner.handle_ingress` (not
through the adapter publish callback) and then inspects every evidence
surface: storage, delivery receipts, native refs, accounting counters,
route stats, and the full runtime snapshot.

This is the **direct pipeline** ingress path.  It differs from the
**adapter callback** ingress path exercised by
:func:`~medre.runtime.run_session.orchestration.run_bridge_session`
with ``ingress_mode="adapter_callback"``:

- **Direct pipeline** (smoke, run-session default): calls
  ``handle_ingress(event)`` directly, which returns
  ``list[DeliveryOutcome]`` for immediate evidence collection.
  The adapter-level ``publish_inbound`` callback is not invoked.

- **Adapter callback** (run-session optional): calls
  ``adapter.publish_inbound(event)``, which goes through the
  adapter's ``ctx.publish_inbound`` callback before reaching
  ``handle_ingress`` internally.  This exercises the same callback
  path that real adapter inbound messages use, but does not return
  ``DeliveryOutcome`` objects — evidence is collected from storage
  polling instead.

Design note: ``handle_ingress`` is used instead of
:meth:`~medre.core.contracts.adapter.AdapterContract.publish_inbound`
because the smoke needs the ``list[DeliveryOutcome]`` return value for
evidence collection.  This still exercises the full pipeline
(validate → resolve_relations → store → route → plan → render →
deliver → receipt) — the only path skipped is the adapter-level
publish_inbound callback, which is a thin wrapper around the same
``handle_ingress`` call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent
from medre.core.observability.sanitization import sanitize_error
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.reporting import native_ref_to_report_dict
from medre.runtime.smoke_ingress import (
    eligible_smoke_sources,
    expected_smoke_deliveries,
    make_smoke_event,
    pick_source_adapter,
    validate_fake_smoke,
)
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot


def _build_limitations(storage_backend: str) -> list[str]:
    """Build the limitations list based on the actual storage backend."""
    storage_note = (
        _LIMITATIONS_SQLITE if storage_backend == "sqlite" else _LIMITATIONS_MEMORY
    )
    return _LIMITATIONS + [storage_note]


_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limitations text
# ---------------------------------------------------------------------------

_LIMITATIONS: list[str] = [
    "Fake adapters only — no real transport connectivity proven",
    "No live codec verification for real packet formats",
    "No reconnection resilience or retry-against-live proof",
    "No live delivery acknowledgement or recipient receipt proven",
    "Single source injection — unselected route sources not exercised",
]

_LIMITATIONS_MEMORY: str = "In-memory storage — no persistence or crash-recovery proof"

_LIMITATIONS_SQLITE: str = "Persistent storage (SQLite) but no crash-recovery proof"


def _empty_runtime_evidence() -> dict[str, Any]:
    """Return a fresh runtime evidence dict to avoid shared mutable state."""
    return {
        "adapter_lifecycle": {},
        "shutdown_status": None,
        "runtime_events_count": 0,
    }


# ---------------------------------------------------------------------------
# Default config resolution
# ---------------------------------------------------------------------------


def _default_smoke_config_path() -> str | None:
    """Return the shipped fake-bridge-smoke.yaml path if it exists."""
    # Walk up from this file to find the repo root (src/medre/runtime/smoke.py)
    this_dir = Path(__file__).resolve().parent
    candidate = (
        this_dir.parent.parent.parent
        / "examples"
        / "configs"
        / "fake-bridge-smoke.yaml"
    )
    if candidate.is_file():
        return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


def _run_preflight(config: Any) -> dict[str, Any]:
    """Run config and route validation without starting the runtime.

    Returns a dict summarising what was validated.
    """
    from medre.config.errors import ConfigValidationError
    from medre.runtime.route_engine import (
        RouteValidationError,
        build_runtime_routes,
    )

    route_errors: list[str] = []
    route_count = 0
    route_enabled = 0
    routes = config.routes
    if routes is not None:
        route_list = routes.routes
        route_count = len(route_list)
        route_enabled = sum(1 for r in route_list if r.enabled)
        try:
            build_runtime_routes(routes)
        except (RouteValidationError, ConfigValidationError) as exc:
            route_errors.append(str(exc))

    adapter_count = 0
    adapter_enabled = 0
    for _transport, _aid, rtc in config.adapters.all_configs():
        adapter_count += 1
        if rtc.enabled:
            adapter_enabled += 1

    return {
        "config_valid": True,  # load_config succeeded
        "adapter_count": adapter_count,
        "adapter_enabled": adapter_enabled,
        "route_count": route_count,
        "route_enabled": route_enabled,
        "route_errors": route_errors,
    }


# ---------------------------------------------------------------------------
# Native ref resolution
# ---------------------------------------------------------------------------


async def _collect_native_refs(
    app: MedreApp,
    outcomes: list[Any],
) -> list[dict[str, object]]:
    """Resolve native refs for each successful delivery outcome."""
    refs: list[dict[str, object]] = []
    if app.storage is None:
        return refs

    for outcome in outcomes:
        if outcome.status != "success":
            continue
        target = outcome.target_adapter
        # Query stored native refs for this event from storage.
        try:
            native_ref_records = await app.storage.list_native_refs_for_event(
                outcome.event_id,
            )
        except (AttributeError, TypeError):
            # Storage backend may not implement list_native_refs_for_event.
            continue
        except Exception:
            # cleanup-silent: best-effort native-ref evidence; remaining
            # outcomes still inspected.
            continue

        for nref in native_ref_records:
            if getattr(nref, "direction", None) != "outbound":
                continue
            if getattr(nref, "adapter", None) != target:
                continue
            native_channel_id = getattr(nref, "native_channel_id", "") or ""
            native_message_id = getattr(nref, "native_message_id", "")
            try:
                resolved = await app.storage.resolve_native_ref(
                    target,
                    native_channel_id,
                    native_message_id,
                )
            except Exception:
                # cleanup-silent: skip this ref; remaining refs still
                # inspected.
                continue
            refs.append(
                native_ref_to_report_dict(
                    nref,
                    resolved_to_event_id=resolved or nref.event_id,
                )
            )
    return refs


# ---------------------------------------------------------------------------
# Main smoke runner
# ---------------------------------------------------------------------------


async def run_fake_bridge_smoke(
    config_path: str | None = None,
    *,
    message_text: str = "medre smoke test",
    storage_path: str | None = None,
    source_adapter: str | None = None,
    source_context: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run one fake bridge smoke cycle and return a compact evidence report.

    Steps:

    1. Load + validate config.
    2. Run preflight checks (adapter count, route validation).
    3. Build runtime via :class:`RuntimeBuilder`.
    4. Start runtime via :meth:`MedreApp.start`.
    5. Inject one ``message.text`` event via
       :meth:`~medre.core.engine.pipeline.PipelineRunner.handle_ingress`.
    6. Collect evidence from storage, accounting, route stats, snapshot.
    7. Stop runtime cleanly via :meth:`MedreApp.stop`.
    8. Return compact dict report with PASS/FAIL status.

    Parameters
    ----------
    config_path:
        Path to YAML config file.  Defaults to
        ``examples/configs/fake-bridge-smoke.yaml`` when available.
    message_text:
        Body text for the injected event.
    storage_path:
        When provided, override storage to SQLite at this path instead of
        the default in-memory backend.  Allows operators to persist smoke
        evidence for post-run inspection.  ``None`` (the default) keeps
        the config's original ``storage.backend`` (typically ``"memory"``).
    source_adapter:
        Optional explicit source identifier for route coverage; without it,
        the first eligible source is used. One smoke cycle tests one source.
    source_context:
        Optional conversation address passed to the fake event factory's
        ``channel`` argument. The factory must preserve it exactly.
    now_fn:
        Callable returning UTC datetime (inject for deterministic tests).
    monotonic_fn:
        Callable returning monotonic float seconds (inject for deterministic tests).

    Returns
    -------
    dict[str, Any]
        Compact evidence report.  JSON-safe.  See module docstring for shape.
    """
    _now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- Resolve config path ------------------------------------------------
    resolved_config_path = config_path
    config_source_value = "explicit"
    if resolved_config_path is None:
        default = _default_smoke_config_path()
        if default is not None:
            resolved_config_path = default
            config_source_value = "default"
        # else: fall through to load_config's normal discovery

    # -- Step 1: Load config ------------------------------------------------
    try:
        config, source, paths = load_config(resolved_config_path)
        # Env overrides share the config-error boundary: a rejected
        # identifier must become a sanitized failed report, not a crash.
        config = apply_env_overrides(config, paths)
        validate_fake_smoke(config)
    except Exception as exc:
        return {
            "status": "failed",
            "command": "smoke",
            "fail_reason": sanitize_error(f"Config load error: {exc}"),
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "limitations": _LIMITATIONS,
            "sanitized": True,
            **_empty_runtime_evidence(),
        }
    config_source_value = source.value

    # -- Override storage if --storage-path provided -------------------------
    if storage_path is not None:
        import dataclasses as _dc

        config = _dc.replace(
            config,
            storage=_dc.replace(config.storage, backend="sqlite", path=storage_path),
        )

    # -- Step 2: Preflight --------------------------------------------------
    preflight = _run_preflight(config)

    # -- Step 3: Build runtime ----------------------------------------------
    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
    except Exception as exc:
        return {
            "status": "failed",
            "command": "smoke",
            "fail_reason": sanitize_error(f"Runtime build error: {exc}"),
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "preflight": preflight,
            "limitations": _LIMITATIONS,
            "sanitized": True,
            **_empty_runtime_evidence(),
        }

    # -- Step 4: Start runtime ----------------------------------------------
    try:
        await app.start()
    except Exception as exc:
        return {
            "status": "failed",
            "command": "smoke",
            "fail_reason": sanitize_error(f"Runtime start error: {exc}"),
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "config_source": config_source_value,
            "preflight": preflight,
            "started_adapters": list(app.started_adapter_ids),
            "limitations": _LIMITATIONS,
            "sanitized": True,
            **_empty_runtime_evidence(),
        }

    try:
        # -- Step 5: Inject event -----------------------------------------------
        candidates = eligible_smoke_sources(app)
        try:
            source_aid, source_instance = pick_source_adapter(app, source_adapter)
            event = make_smoke_event(
                source_instance, message_text, source_context=source_context
            )
        except Exception as exc:
            try:
                await app.stop()
            except Exception:
                _logger.exception("Smoke shutdown failed after source rejection")
            return {
                "status": "failed",
                "command": "smoke",
                "fail_reasons": [sanitize_error(f"Smoke source error: {exc}")],
                "source_candidates": candidates,
                "coverage_scope": "single_source",
                "evidence_level": "fake_bridge",
                "timestamp": _now().isoformat(),
                "config_source": config_source_value,
                "preflight": preflight,
                "limitations": _build_limitations(config.storage.backend),
                **_empty_runtime_evidence(),
            }

        # Compare against configured routes: startup may prune unavailable targets
        # from the live router, so outcomes alone cannot prove the complete fanout.
        expected_deliveries = expected_smoke_deliveries(config, event)
        unavailable_deliveries = {
            pair
            for pair in expected_deliveries
            if pair[1] not in app.started_adapter_ids
        }
        outcomes: list[Any] = []
        injection_error: str | None = None
        try:
            outcomes = await app.pipeline_runner.handle_ingress(event)
        except Exception as exc:
            injection_error = f"{type(exc).__name__}: {exc}"

        # -- Step 6: Collect evidence -------------------------------------------
        # Best-effort evidence gathering: every swallow in this section drops
        # the affected field to its None/empty default. The smoke report's
        # PASS/FAIL criteria check what was actually populated, so a failed
        # lookup surfaces as a missing field rather than a swallowed error.
        storage = app.storage

        # Stored event
        stored_event: CanonicalEvent | None = None
        if storage is not None:
            try:
                stored_event = await storage.get_metadata(event.event_id)
            except Exception:
                # cleanup-silent: stored_event stays None; reported as
                # event_stored=False below.
                pass

        # Delivery receipts
        receipts: list[Any] = []
        if storage is not None:
            try:
                receipts = await storage.list_receipts_for_event(event.event_id)
            except Exception:
                # cleanup-silent: receipts stays []; reported as empty below.
                pass

        # Native refs
        native_refs = await _collect_native_refs(app, outcomes)

        # Accounting
        accounting: dict[str, int] | None = None
        if app._runtime_accounting is not None:
            try:
                accounting = app._runtime_accounting.snapshot()
            except Exception:
                # cleanup-silent: accounting stays None.
                pass

        # Route stats
        route_stats: dict[str, Any] | None = None
        if app.route_stats is not None:
            try:
                route_stats = app.route_stats.snapshot()
            except Exception:
                # cleanup-silent: route_stats stays None.
                pass

        # Target adapters from outcomes
        target_adapters = sorted(
            {o.target_adapter for o in outcomes if o.status == "success"}
        )
        route_ids = sorted({o.route_id for o in outcomes if o.route_id})

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

        # -- Step 7: Stop cleanly -----------------------------------------------
        shutdown_error: str | None = None
        try:
            await app.stop()
        except Exception as exc:
            shutdown_error = f"{type(exc).__name__}: {exc}"
            _logger.warning("Smoke shutdown failed: %s", sanitize_error(shutdown_error))

        # -- Full snapshot (after stop so lifecycle.runtime_state reflects "stopped")
        snap: dict[str, Any] = {}
        snapshot_error: str | None = None
        try:
            snap = build_runtime_snapshot(
                app,
                now_fn=now_fn,
                monotonic_fn=monotonic_fn,
            )
        except Exception as exc:
            snapshot_error = f"{type(exc).__name__}: {exc}"

        # -- Step 8: Build report -----------------------------------------------
        # passed criteria
        event_stored = stored_event is not None
        has_success = any(o.status == "success" for o in outcomes)
        has_failure = any(o.status != "success" for o in outcomes)
        missing_deliveries = unavailable_deliveries | (
            expected_deliveries
            - {
                (o.route_id, o.target_adapter)
                for o in outcomes
                if o.status == "success"
            }
        )
        sent_deliveries = {
            (r.route_id, r.target_adapter) for r in receipts if r.status == "sent"
        }
        missing_receipts = expected_deliveries - sent_deliveries
        has_sent_receipt = bool(sent_deliveries)
        delivered_count = accounting.get("outbound_delivered", 0) if accounting else 0
        required_deliveries = len(expected_deliveries)
        runtime_state = (snap.get("lifecycle") or {}).get("runtime_state", "unknown")

        passed = (
            event_stored
            and has_success
            and not has_failure
            and not missing_deliveries
            and not missing_receipts
            and has_sent_receipt
            and delivered_count >= required_deliveries
            and injection_error is None
            and shutdown_error is None
            and snapshot_error is None
            and runtime_state == "stopped"
        )

        fail_reasons: list[str] = []
        if injection_error is not None:
            fail_reasons.append(f"Event injection failed: {injection_error}")
        if not event_stored:
            fail_reasons.append("Event not found in storage")
        if not has_success:
            fail_reasons.append("No successful delivery outcome")
        if has_failure:
            fail_reasons.append(
                "One or more route delivery outcomes were not successful"
            )
        if missing_deliveries:
            fail_reasons.append(
                "Configured route target(s) unavailable or missing successful delivery: "
                + ", ".join(
                    f"{route} -> {target}"
                    for route, target in sorted(missing_deliveries)
                )
            )
        if missing_receipts:
            fail_reasons.append(
                "Configured route target(s) missing durable sent receipt: "
                + ", ".join(
                    f"{route} -> {target}" for route, target in sorted(missing_receipts)
                )
            )
        if not has_sent_receipt:
            fail_reasons.append("No receipt with status 'sent'")
        if delivered_count < required_deliveries:
            fail_reasons.append(
                f"Accounting outbound_delivered {delivered_count} < "
                f"configured delivery count {required_deliveries}"
            )
        if shutdown_error is not None:
            fail_reasons.append(f"Runtime shutdown failed: {shutdown_error}")
        if snapshot_error is not None:
            fail_reasons.append(f"Final runtime snapshot failed: {snapshot_error}")
        if runtime_state != "stopped":
            fail_reasons.append(
                f"Runtime state is {runtime_state!r}, expected 'stopped'"
            )

        # Sanitize error fields.
        sanitized = False
        _sanitized_reasons: list[str] = []
        for r in fail_reasons:
            s = sanitize_error(r)
            if s != r:
                sanitized = True
            _sanitized_reasons.append(s)
        fail_reasons = _sanitized_reasons

        resolved_storage_path = (
            storage_path
            if storage_path is not None
            else (config.storage.path if config.storage.path else None)
        )

        # Derive shutdown_status from lifecycle.runtime_state (consistent
        # with orchestration.py).  The snapshot no longer carries a dedicated
        # shutdown_evidence section.
        _lifecycle = snap.get("lifecycle") or {}
        _runtime_state = _lifecycle.get("runtime_state", "unknown")

        report: dict[str, Any] = {
            "status": "passed" if passed else "failed",
            "command": "smoke",
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "generated_at": _now().isoformat(),
            "config_source": config_source_value,
            "storage_backend": config.storage.backend,
            **(
                {"storage_path": resolved_storage_path}
                if resolved_storage_path is not None
                else {}
            ),
            "preflight": preflight,
            "source_adapter": source_aid,
            "source_context": event.source_channel_id,
            "source_candidates": candidates,
            "coverage_scope": "single_source",
            "target_adapters": target_adapters,
            "event_id": event.event_id,
            "route_ids": route_ids,
            "delivery_receipts": receipt_summaries,
            "native_refs": native_refs,
            "accounting": accounting,
            "route_stats": route_stats,
            "snapshot": {
                "schema_version": snap.get("schema_version", SCHEMA_VERSION),
                "lifecycle": _lifecycle,
                "routes": {
                    "stats": (snap.get("routes") or {}).get("stats", {}),
                },
                "accounting": snap.get("accounting", {}),
            },
            "adapter_lifecycle": _lifecycle.get("adapters", {}),
            "shutdown_status": _runtime_state if _runtime_state == "stopped" else None,
            "runtime_events_count": (
                ((snap.get("diagnostics") or {}).get("runtime_events") or {}).get(
                    "count", 0
                )
            ),
            "limitations": _build_limitations(config.storage.backend),
        }

        if sanitized:
            report["sanitized"] = True

        if not passed:
            report["fail_reasons"] = fail_reasons

        return report
    except Exception as exc:
        return {
            "status": "failed",
            "command": "smoke",
            "fail_reasons": [sanitize_error(f"Smoke execution error: {exc}")],
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "limitations": _build_limitations(config.storage.backend),
            **_empty_runtime_evidence(),
        }
    finally:
        if app.state != RuntimeState.STOPPED:
            try:
                await app.stop()
            except Exception as exc:
                _logger.error(
                    "Final smoke shutdown failed: %s", sanitize_error(str(exc))
                )
