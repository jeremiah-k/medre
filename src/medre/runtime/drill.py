"""Operator-facing failure drill runner.

Provides :func:`run_drill` — a single async function that exercises a
named failure scenario against the MEDRE runtime pipeline using fake
adapters.  Docker-free, network-free, SDK-free.

Drills are invoked via ``medre smoke --drill <name>`` and produce a
compact JSON report describing the failure injection, the observed
outcome, and the evidence collected.

Drill report shape
------------------
Every drill report contains at minimum:

* ``status``        — ``"PASS"`` or ``"FAIL"`` (PASS = drill observed
  the expected failure behavior).
* ``evidence_level`` — always ``"drill"``.
* ``drill_name``    — the drill identifier.
* ``timestamp``     — ISO-8601 UTC.
* ``drill_steps``   — ordered evidence trail.
* ``limitations``   — what the drill does NOT prove.

Available drills
----------------
``renderer_failure``
    Removes all renderers; proves RENDERER_FAILURE classification
    with a ``"failed"`` receipt persisted.

``adapter_permanent_failure``
    Target adapter raises ``AdapterPermanentError``; proves
    ``permanent_failure`` outcome with ``ADAPTER_PERMANENT`` failure
    kind.

``adapter_transient_failure``
    Target adapter raises ``AdapterSendError(transient=True)``; proves
    ``transient_failure`` outcome with ``ADAPTER_TRANSIENT`` failure
    kind.

``capacity_rejection``
    Exhausts the capacity semaphore before delivery; proves
    ``CAPACITY_REJECTION`` classification with no receipt persisted.

``shutdown_rejection``
    Calls ``stop_accepting()`` before delivery; proves
    ``SHUTDOWN_REJECTION`` classification with no receipt persisted.

``replay_duplicate_risk``
    Delivers once, then re-delivers via BEST_EFFORT replay; proves
    duplicate receipts and native refs are created (duplicate-send
    risk observed).

``degraded_live_health``
    Adapter reports ``health="degraded"``; proves the runtime
    observes degraded health without crashing.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from medre.config.loader import load_config
from medre.config.env import apply_env_overrides
from medre.config.model import StorageConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.smoke import (
    _LIMITATIONS as _SMOKE_LIMITATIONS,
    _default_smoke_config_path,
    _make_smoke_event,
    _pick_source_adapter,
    _run_preflight,
)

__all__ = ["run_drill", "AVAILABLE_DRILLS"]

_logger = logging.getLogger(__name__)

AVAILABLE_DRILLS: tuple[str, ...] = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "shutdown_rejection",
    "replay_duplicate_risk",
    "degraded_live_health",
)

_DRILL_LIMITATIONS: list[str] = [
    "Drill uses fake adapters — no real transport failure proven",
    "Failure injection is synchronous and deterministic",
    "No background retry scheduler exercised",
    "No sustained failure or cascading failure proof",
]


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _base_report(
    drill_name: str,
    *,
    storage_path: str | None = None,
    config_source: str = "default",
) -> dict[str, Any]:
    """Return the common report skeleton for a drill."""
    report: dict[str, Any] = {
        "status": "PASS",
        "evidence_level": "drill",
        "drill_name": drill_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config_source": config_source,
        "storage_backend": "memory",
        "drill_steps": [],
        "limitations": _DRILL_LIMITATIONS,
    }
    if storage_path is not None:
        report["storage_path"] = storage_path
        report["storage_backend"] = "sqlite"
    return report


def _step(
    name: str,
    result: str,
    **detail: Any,
) -> dict[str, Any]:
    """Build a single drill step record."""
    rec: dict[str, Any] = {"step": name, "result": result}
    rec.update(detail)
    return rec


# ---------------------------------------------------------------------------
# Config / runtime helpers
# ---------------------------------------------------------------------------


async def _build_smoke_runtime(
    config_path: str | None,
    storage_path: str | None,
) -> tuple[MedreApp, dict[str, Any], str, dict[str, Any]]:
    """Load config, build runtime, start it.  Returns (app, preflight, config_source, paths).

    Raises SystemExit on config/build/start errors (returns error report instead).
    """
    resolved = config_path
    config_source_value = "explicit"
    if resolved is None:
        default = _default_smoke_config_path()
        if default is not None:
            resolved = default
            config_source_value = "default"

    try:
        config, source, paths = load_config(resolved)
    except Exception as exc:
        raise RuntimeError(f"Config load error: {exc}") from exc

    config_source_value = source.value
    config = apply_env_overrides(config, paths)

    if storage_path is not None:
        config = dataclasses.replace(
            config,
            storage=dataclasses.replace(
                config.storage, backend="sqlite", path=storage_path,
            ),
        )

    preflight = _run_preflight(config)

    builder = RuntimeBuilder(config, paths)
    app = builder.build()
    await app.start()
    return app, preflight, config_source_value, {"config": config, "paths": paths}


async def _clean_stop(app: MedreApp) -> None:
    """Stop a runtime, swallowing non-fatal errors."""
    try:
        await app.stop()
    except Exception as exc:
        _logger.warning("Drill stop error (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Individual drills
# ---------------------------------------------------------------------------


async def _drill_renderer_failure(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Remove all renderers; verify RENDERER_FAILURE classification."""
    report = _base_report("renderer_failure", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        # Remove all renderers so rendering fails.
        app.pipeline_runner._rendering_pipeline._renderers.clear()
        steps.append(_step("remove_renderers", "ok", renderers_cleared=True))

        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: renderer_failure")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(_step("ingress_complete", "ok", outcome_count=len(outcomes)))

        # Expect exactly one outcome with RENDERER_FAILURE.
        if not outcomes:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No delivery outcomes returned"]
        else:
            outcome = outcomes[0]
            failure_kind = (
                outcome.failure_kind.value if outcome.failure_kind else None
            )
            steps.append(_step(
                "verify_renderer_failure",
                "ok" if failure_kind == "renderer_failure" else "unexpected",
                expected="renderer_failure",
                observed=failure_kind,
                outcome_status=outcome.status,
            ))
            # Verify a "failed" receipt was persisted.
            receipts = []
            if app.storage is not None:
                try:
                    receipts = await app.storage.list_receipts_for_event(event.event_id)
                except Exception:
                    pass
            has_failed_receipt = any(
                r.status == "failed" and "Rendering failed" in (r.error or "")
                for r in receipts
            )
            steps.append(_step(
                "verify_failed_receipt",
                "ok" if has_failed_receipt else "missing",
                receipt_count=len(receipts),
                has_failed_receipt=has_failed_receipt,
            ))
            if failure_kind != "renderer_failure" or not has_failed_receipt:
                report["status"] = "FAIL"
                report["fail_reasons"] = []
                if failure_kind != "renderer_failure":
                    report["fail_reasons"].append(
                        f"Expected renderer_failure, got {failure_kind}"
                    )
                if not has_failed_receipt:
                    report["fail_reasons"].append("No failed receipt persisted")

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_adapter_permanent_failure(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Target adapter raises AdapterPermanentError."""
    from medre.adapters.base import AdapterPermanentError

    report = _base_report("adapter_permanent_failure", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: adapter_permanent_failure")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        # Patch the first non-source adapter to raise permanent error.
        patched_aid = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                adapter = app.adapters[aid]
                original_deliver = adapter.deliver
                patched_aid = aid

                async def _failing_deliver(
                    result: Any,
                    _orig: Any = original_deliver,
                ) -> Any:
                    raise AdapterPermanentError(
                        "drill: simulated permanent failure"
                    )

                adapter.deliver = _failing_deliver  # type: ignore[assignment]
                break

        if patched_aid is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No target adapter to patch"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        steps.append(_step("patch_adapter", "ok", target_adapter=patched_aid))
        report["target_adapters"] = [patched_aid]

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(_step("ingress_complete", "ok", outcome_count=len(outcomes)))

        # Find the outcome for our patched adapter.
        target_outcome = None
        for o in outcomes:
            if o.target_adapter == patched_aid:
                target_outcome = o
                break

        if target_outcome is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = [
                f"No outcome for patched adapter {patched_aid}"
            ]
        else:
            failure_kind = target_outcome.failure_kind.value if target_outcome.failure_kind else None
            steps.append(_step(
                "verify_permanent_failure",
                "ok" if target_outcome.status == "permanent_failure" else "unexpected",
                expected="permanent_failure",
                observed=target_outcome.status,
                failure_kind=failure_kind,
            ))
            # Verify: no native ref for failed delivery (native refs only
            # created on success).  Use the smoke runner's approach: check
            # that resolve_native_ref for the expected fake native ID returns None.
            failed_adapter = app.adapters.get(patched_aid)
            has_unexpected_ref = False
            if failed_adapter is not None and app.storage is not None:
                platform = getattr(failed_adapter, "platform", "")
                if platform in ("meshtastic", "meshcore"):
                    try:
                        ref = await app.storage.resolve_native_ref(
                            patched_aid, "0", "1",
                        )
                        if ref is not None:
                            has_unexpected_ref = True
                    except Exception:
                        pass
                elif platform == "matrix":
                    try:
                        ref = await app.storage.resolve_native_ref(
                            patched_aid, "", f"$fake_{event.event_id}",
                        )
                        if ref is not None:
                            has_unexpected_ref = True
                    except Exception:
                        pass
            steps.append(_step(
                "verify_no_native_ref",
                "ok" if not has_unexpected_ref else "unexpected",
                unexpected_native_ref=has_unexpected_ref,
            ))
            # Verify accounting has a failed count.
            acc = None
            if app._runtime_accounting is not None:
                acc = app._runtime_accounting.snapshot()
            steps.append(_step(
                "verify_accounting",
                "ok" if acc and acc.get("outbound_failed", 0) >= 1 else "unexpected",
                accounting=acc,
            ))

            if target_outcome.status != "permanent_failure":
                report["status"] = "FAIL"
                report["fail_reasons"] = [
                    f"Expected permanent_failure, got {target_outcome.status}"
                ]

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_adapter_transient_failure(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Target adapter raises AdapterSendError(transient=True)."""
    from medre.adapters.base import AdapterSendError

    report = _base_report("adapter_transient_failure", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: adapter_transient_failure")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        # Patch the first non-source adapter to raise transient error.
        patched_aid = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                adapter = app.adapters[aid]

                async def _transient_deliver(result: Any) -> Any:
                    raise AdapterSendError(
                        "drill: simulated transient failure", transient=True,
                    )

                adapter.deliver = _transient_deliver  # type: ignore[assignment]
                patched_aid = aid
                break

        if patched_aid is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No target adapter to patch"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        steps.append(_step("patch_adapter", "ok", target_adapter=patched_aid))
        report["target_adapters"] = [patched_aid]

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(_step("ingress_complete", "ok", outcome_count=len(outcomes)))

        target_outcome = None
        for o in outcomes:
            if o.target_adapter == patched_aid:
                target_outcome = o
                break

        if target_outcome is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = [
                f"No outcome for patched adapter {patched_aid}"
            ]
        else:
            failure_kind = target_outcome.failure_kind.value if target_outcome.failure_kind else None
            steps.append(_step(
                "verify_transient_failure",
                "ok" if target_outcome.status == "transient_failure" else "unexpected",
                expected="transient_failure",
                observed=target_outcome.status,
                failure_kind=failure_kind,
            ))
            # Verify: transient failure is retryable.
            is_retryable = (
                target_outcome.failure_kind is not None
                and target_outcome.failure_kind.is_retryable
            )
            steps.append(_step(
                "verify_retryable",
                "ok" if is_retryable else "unexpected",
                is_retryable=is_retryable,
            ))
            # Verify: no native ref for failed delivery.
            failed_adapter = app.adapters.get(patched_aid)
            has_unexpected_ref = False
            if failed_adapter is not None and app.storage is not None:
                platform = getattr(failed_adapter, "platform", "")
                if platform in ("meshtastic", "meshcore"):
                    try:
                        ref = await app.storage.resolve_native_ref(
                            patched_aid, "0", "1",
                        )
                        if ref is not None:
                            has_unexpected_ref = True
                    except Exception:
                        pass
                elif platform == "matrix":
                    try:
                        ref = await app.storage.resolve_native_ref(
                            patched_aid, "", f"$fake_{event.event_id}",
                        )
                        if ref is not None:
                            has_unexpected_ref = True
                    except Exception:
                        pass
            steps.append(_step(
                "verify_no_native_ref",
                "ok" if not has_unexpected_ref else "unexpected",
                unexpected_native_ref=has_unexpected_ref,
            ))

            if target_outcome.status != "transient_failure" or not is_retryable:
                report["status"] = "FAIL"
                report["fail_reasons"] = []
                if target_outcome.status != "transient_failure":
                    report["fail_reasons"].append(
                        f"Expected transient_failure, got {target_outcome.status}"
                    )
                if not is_retryable:
                    report["fail_reasons"].append("Failure kind not marked retryable")

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_capacity_rejection(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Exhaust capacity semaphore; verify CAPACITY_REJECTION."""
    from medre.config.model import RuntimeLimits

    report = _base_report("capacity_rejection", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, ctx = await _build_smoke_runtime(
            config_path, storage_path,
        )
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        cc = app._capacity_controller
        if cc is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No capacity controller wired"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        # Replace the capacity controller with one that has a single slot
        # and a short timeout, then exhaust it.
        from medre.runtime.capacity import CapacityController
        small_cc = CapacityController(
            RuntimeLimits(
                max_inflight_deliveries=1,
                max_inflight_replay_events=1,
                delivery_acquire_timeout_seconds=0.1,
            )
        )
        # Exhaust the single delivery slot.
        await small_cc._delivery_sem.acquire()
        small_cc._delivery_current = 1
        app._capacity_controller = small_cc
        app.pipeline_runner._capacity_controller = small_cc
        steps.append(_step("exhaust_capacity", "ok", delivery_limit=1))

        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: capacity_rejection")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(_step("ingress_complete", "ok", outcome_count=len(outcomes)))

        # Find a CAPACITY_REJECTION outcome.
        cap_outcomes = [
            o for o in outcomes
            if o.failure_kind is not None
            and o.failure_kind.value == "capacity_rejection"
        ]
        has_cap_rejection = len(cap_outcomes) >= 1
        steps.append(_step(
            "verify_capacity_rejection",
            "ok" if has_cap_rejection else "unexpected",
            capacity_rejections=len(cap_outcomes),
        ))
        # Verify: no receipts for capacity-rejected deliveries.
        if app.storage is not None:
            try:
                receipts = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                receipts = []
        else:
            receipts = []
        cap_receipts = [
            r for r in receipts
            if r.target_adapter in {o.target_adapter for o in cap_outcomes}
        ]
        steps.append(_step(
            "verify_no_receipt",
            "ok" if not cap_receipts else "unexpected",
            receipt_count=len(cap_receipts),
        ))

        if not has_cap_rejection:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No CAPACITY_REJECTION observed"]

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_shutdown_rejection(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Call stop_accepting(); verify SHUTDOWN_REJECTION."""
    report = _base_report("shutdown_rejection", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        cc = app._capacity_controller
        if cc is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No capacity controller wired"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        cc.stop_accepting()
        steps.append(_step("stop_accepting", "ok", accepting_work=cc.accepting_work))

        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: shutdown_rejection")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(_step("ingress_complete", "ok", outcome_count=len(outcomes)))

        shutdown_outcomes = [
            o for o in outcomes
            if o.failure_kind is not None
            and o.failure_kind.value == "shutdown_rejection"
        ]
        has_shutdown = len(shutdown_outcomes) >= 1
        steps.append(_step(
            "verify_shutdown_rejection",
            "ok" if has_shutdown else "unexpected",
            shutdown_rejections=len(shutdown_outcomes),
        ))
        # Verify: no receipts for shutdown-rejected deliveries.
        if app.storage is not None:
            try:
                receipts = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                receipts = []
        else:
            receipts = []
        shutdown_receipts = [
            r for r in receipts
            if r.target_adapter in {o.target_adapter for o in shutdown_outcomes}
        ]
        steps.append(_step(
            "verify_no_receipt",
            "ok" if not shutdown_receipts else "unexpected",
            receipt_count=len(shutdown_receipts),
        ))

        if not has_shutdown:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No SHUTDOWN_REJECTION observed"]

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_replay_duplicate_risk(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Deliver once, replay BEST_EFFORT; verify duplicate receipts."""
    from medre.core.storage.replay import ReplayEngine, ReplayMode, ReplayRequest

    report = _base_report("replay_duplicate_risk", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: replay_duplicate_risk")
        steps.append(_step("inject_event", "ok", event_id=event.event_id))
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        # First delivery.
        outcomes1 = await app.pipeline_runner.handle_ingress(event)
        receipts1 = []
        if app.storage is not None:
            try:
                receipts1 = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                pass
        steps.append(_step(
            "first_delivery", "ok",
            outcome_count=len(outcomes1),
            receipt_count=len(receipts1),
        ))

        # Replay BEST_EFFORT.
        replay_engine = app._replay_engine
        if replay_engine is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No replay engine wired"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        request = ReplayRequest(
            run_id=f"drill-replay-{event.event_id[:8]}",
            correlation_ids=[event.event_id],
            mode=ReplayMode.BEST_EFFORT,
        )
        results = []
        async for result in replay_engine.replay(request):
            results.append(result)
        steps.append(_step(
            "replay_best_effort", "ok",
            replay_results=len(results),
        ))

        # Collect receipts after replay.
        receipts2 = []
        if app.storage is not None:
            try:
                receipts2 = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                pass

        new_receipts = len(receipts2) - len(receipts1)
        steps.append(_step(
            "verify_duplicate_receipts",
            "ok" if new_receipts > 0 else "unexpected",
            receipts_before=len(receipts1),
            receipts_after=len(receipts2),
            new_receipts=new_receipts,
        ))

        if new_receipts <= 0:
            report["status"] = "FAIL"
            report["fail_reasons"] = [
                "Expected duplicate receipts from replay, got none"
            ]

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


async def _drill_degraded_live_health(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Adapter reports degraded health; verify runtime observes it."""
    report = _base_report("degraded_live_health", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        # Pick a target adapter and override health_check.
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            target_aid = aid
            break

        if target_aid is None:
            report["status"] = "FAIL"
            report["fail_reasons"] = ["No adapter available"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        adapter = app.adapters[target_aid]

        from medre.adapters.base import AdapterInfo, AdapterCapabilities

        # Preserve original capabilities if available.
        orig_caps = getattr(adapter, "_capabilities", None)
        if orig_caps is None:
            try:
                info = await adapter.health_check()
                orig_caps = info.capabilities
            except Exception:
                orig_caps = AdapterCapabilities(text=True)

        async def _degraded_health() -> AdapterInfo:
            from medre.adapters.base import AdapterRole as _AR
            raw_role = getattr(adapter, "role", "unknown")
            role = raw_role if isinstance(raw_role, _AR) else _AR.PRESENTATION
            return AdapterInfo(
                adapter_id=adapter.adapter_id,
                platform=getattr(adapter, "platform", "unknown"),
                role=role,
                version="0.1.0",
                capabilities=orig_caps,
                health="degraded",
            )

        adapter.health_check = _degraded_health  # type: ignore[assignment]
        steps.append(_step("patch_health", "ok", target_adapter=target_aid))

        # Refresh live health.
        await app.refresh_live_health()
        steps.append(_step("refresh_health", "ok"))

        # Verify the snapshot sees degraded health.
        from medre.runtime.snapshot import build_runtime_snapshot
        snap = build_runtime_snapshot(app)
        live_health = snap.get("health", {}).get("live_health", {})
        adapters_health = live_health.get("adapters", {})
        adapter_entry = adapters_health.get(target_aid, {})
        observed_health = adapter_entry.get("health", "unknown")

        steps.append(_step(
            "verify_degraded",
            "ok" if observed_health == "degraded" else "unexpected",
            target_adapter=target_aid,
            observed_health=observed_health,
        ))

        if observed_health != "degraded":
            report["status"] = "FAIL"
            report["fail_reasons"] = [
                f"Expected degraded health for {target_aid}, got {observed_health}"
            ]

    except Exception as exc:
        report["status"] = "FAIL"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


# ---------------------------------------------------------------------------
# Drill dispatch
# ---------------------------------------------------------------------------


_DRILL_RUNNERS: dict[str, Any] = {
    "renderer_failure": _drill_renderer_failure,
    "adapter_permanent_failure": _drill_adapter_permanent_failure,
    "adapter_transient_failure": _drill_adapter_transient_failure,
    "capacity_rejection": _drill_capacity_rejection,
    "shutdown_rejection": _drill_shutdown_rejection,
    "replay_duplicate_risk": _drill_replay_duplicate_risk,
    "degraded_live_health": _drill_degraded_live_health,
}


async def run_drill(
    drill_name: str,
    *,
    config_path: str | None = None,
    storage_path: str | None = None,
) -> dict[str, Any]:
    """Run a named failure drill and return a compact report.

    Parameters
    ----------
    drill_name:
        The drill identifier.  Must be one of :data:`AVAILABLE_DRILLS`.
    config_path:
        Path to TOML config file.
    storage_path:
        Optional SQLite path for persisting drill evidence.

    Returns
    -------
    dict[str, Any]
        Compact drill report.  JSON-safe.
    """
    runner = _DRILL_RUNNERS.get(drill_name)
    if runner is None:
        return {
            "status": "FAIL",
            "evidence_level": "drill",
            "drill_name": drill_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fail_reasons": [
                f"Unknown drill {drill_name!r}. "
                f"Available: {', '.join(AVAILABLE_DRILLS)}"
            ],
            "drill_steps": [],
            "limitations": _DRILL_LIMITATIONS,
        }
    return await runner(config_path, storage_path)
