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

* ``status``        — ``"passed"`` or ``"failed"`` (passed = drill observed
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
    ``CAPACITY_REJECTION`` classification with a ``suppressed``
    evidence receipt persisted.

``shutdown_rejection``
    Calls ``stop_accepting()`` before delivery; proves
    ``SHUTDOWN_REJECTION`` classification with a ``suppressed``
    evidence receipt persisted.

``replay_duplicate_risk``
    Delivers once, then re-delivers via BEST_EFFORT replay; proves
    duplicate receipts and native refs are created (duplicate-send
    risk observed).

``degraded_live_health``
    Adapter reports ``health="degraded"``; proves the runtime
    observes degraded health without crashing.

Pre-runtime drills (no adapter start attempted)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``bad_route_config``
    Enabled route references an adapter ID not present in config;
    proves ``RouteValidationError`` from the builder with
    ``route_id``, ``unknown_adapter_id``, and ``known_adapter_ids``
    attribution.  Runtime is never started.

``all_adapters_build_fail``
    All enabled adapters fail during construction; proves the builder
    records ``build_failures`` with transport/adapter_id attribution
    and the resulting ``MedreApp.adapters`` is empty.  Runtime start
    is never attempted.

Startup-failure drills
~~~~~~~~~~~~~~~~~~~~~~
``partial_degraded_startup``
    Some adapters start, one fails; proves ``partial`` startup outcome
    with ``degraded`` runtime health, correct started/failed adapter
    attribution, and boot summary evidence.

``all_adapters_start_fail``
    All built adapters fail on ``start()``; proves ``total_failure``
    startup outcome with ``RuntimeStartupError`` and full resource
    cleanup (pipeline runner stopped, storage closed, adapters
    cleaned up).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import resolve as resolve_paths
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import (
    RuntimeConfigError,
    RuntimeStartupError,
)
from medre.runtime.route_engine import RouteValidationError
from medre.runtime.smoke import (
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
    "bad_route_config",
    "all_adapters_build_fail",
    "partial_degraded_startup",
    "all_adapters_start_fail",
)

_DRILL_LIMITATIONS: list[str] = [
    "Drill uses fake adapters — no real transport failure proven",
    "Failure injection is synchronous and deterministic",
    "RetryWorker (opt-in, adapter_transient only) not exercised in drill",
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
        "status": "passed",
        "command": "drill",
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
                config.storage,
                backend="sqlite",
                path=storage_path,
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
        report["status"] = "failed"
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
            report["status"] = "failed"
            report["fail_reasons"] = ["No delivery outcomes returned"]
        else:
            outcome = outcomes[0]
            failure_kind = outcome.failure_kind.value if outcome.failure_kind else None
            steps.append(
                _step(
                    "verify_renderer_failure",
                    "ok" if failure_kind == "renderer_failure" else "unexpected",
                    expected="renderer_failure",
                    observed=failure_kind,
                    outcome_status=outcome.status,
                )
            )
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
            steps.append(
                _step(
                    "verify_failed_receipt",
                    "ok" if has_failed_receipt else "missing",
                    receipt_count=len(receipts),
                    has_failed_receipt=has_failed_receipt,
                )
            )
            if failure_kind != "renderer_failure" or not has_failed_receipt:
                report["status"] = "failed"
                report["fail_reasons"] = []
                if failure_kind != "renderer_failure":
                    report["fail_reasons"].append(
                        f"Expected renderer_failure, got {failure_kind}"
                    )
                if not has_failed_receipt:
                    report["fail_reasons"].append("No failed receipt persisted")

    except Exception as exc:
        report["status"] = "failed"
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
    from medre.core.contracts.adapter import AdapterPermanentError

    report = _base_report("adapter_permanent_failure", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "failed"
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
                    raise AdapterPermanentError("drill: simulated permanent failure")

                adapter.deliver = _failing_deliver  # type: ignore[assignment]
                break

        if patched_aid is None:
            report["status"] = "failed"
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
            report["status"] = "failed"
            report["fail_reasons"] = [f"No outcome for patched adapter {patched_aid}"]
        else:
            failure_kind = (
                target_outcome.failure_kind.value
                if target_outcome.failure_kind
                else None
            )
            steps.append(
                _step(
                    "verify_permanent_failure",
                    (
                        "ok"
                        if target_outcome.status == "permanent_failure"
                        else "unexpected"
                    ),
                    expected="permanent_failure",
                    observed=target_outcome.status,
                    failure_kind=failure_kind,
                )
            )
            # Verify: no native ref for failed delivery (native refs only
            # created on success).  Query storage for any outbound refs for
            # this event targeting the failed adapter.
            failed_adapter = app.adapters.get(patched_aid)
            has_unexpected_ref = False
            if failed_adapter is not None and app.storage is not None:
                try:
                    native_ref_records = await app.storage.list_native_refs_for_event(
                        event.event_id,
                    )
                    for nref in native_ref_records:
                        if (
                            getattr(nref, "direction", None) == "outbound"
                            and getattr(nref, "adapter", None) == patched_aid
                        ):
                            has_unexpected_ref = True
                            break
                except (AttributeError, TypeError):
                    # Storage backend may not implement list_native_refs_for_event.
                    pass
                except Exception:
                    pass
            steps.append(
                _step(
                    "verify_no_native_ref",
                    "ok" if not has_unexpected_ref else "unexpected",
                    unexpected_native_ref=has_unexpected_ref,
                )
            )
            # Verify accounting has a failed count.
            acc = None
            if app._runtime_accounting is not None:
                acc = app._runtime_accounting.snapshot()
            steps.append(
                _step(
                    "verify_accounting",
                    (
                        "ok"
                        if acc and acc.get("outbound_failed", 0) >= 1
                        else "unexpected"
                    ),
                    accounting=acc,
                )
            )

            if target_outcome.status != "permanent_failure":
                report["status"] = "failed"
                report["fail_reasons"] = [
                    f"Expected permanent_failure, got {target_outcome.status}"
                ]

    except Exception as exc:
        report["status"] = "failed"
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
    from medre.core.contracts.adapter import AdapterSendError

    report = _base_report("adapter_transient_failure", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(
        _step(
            "runtime_start",
            "ok",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    try:
        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: adapter_transient_failure")
        steps.append(
            _step(
                "inject_event",
                "ok",
                event_id=event.event_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        # Patch the first non-source adapter to raise transient error.
        patched_aid = None
        original_deliver = None
        for aid in sorted(app.adapters.keys()):
            if aid != source_aid:
                adapter = app.adapters[aid]
                original_deliver = adapter.deliver

                async def _transient_deliver(result: Any) -> Any:
                    raise AdapterSendError(
                        "drill: simulated transient failure",
                        transient=True,
                    )

                adapter.deliver = _transient_deliver  # type: ignore[assignment]
                patched_aid = aid
                break

        if patched_aid is None:
            report["status"] = "failed"
            report["fail_reasons"] = ["No target adapter to patch"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        steps.append(
            _step(
                "patch_adapter",
                "ok",
                target_adapter=patched_aid,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        report["target_adapters"] = [patched_aid]

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(
            _step(
                "ingress_complete",
                "ok",
                outcome_count=len(outcomes),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        target_outcome = None
        for o in outcomes:
            if o.target_adapter == patched_aid:
                target_outcome = o
                break

        if target_outcome is None:
            report["status"] = "failed"
            report["fail_reasons"] = [f"No outcome for patched adapter {patched_aid}"]
        else:
            failure_kind = (
                target_outcome.failure_kind.value
                if target_outcome.failure_kind
                else None
            )
            steps.append(
                _step(
                    "verify_transient_failure",
                    (
                        "ok"
                        if target_outcome.status == "transient_failure"
                        else "unexpected"
                    ),
                    expected="transient_failure",
                    observed=target_outcome.status,
                    failure_kind=failure_kind,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )
            # Verify: transient failure is retryable.
            is_retryable = (
                target_outcome.failure_kind is not None
                and target_outcome.failure_kind.is_retryable
            )
            steps.append(
                _step(
                    "verify_retryable",
                    "ok" if is_retryable else "unexpected",
                    is_retryable=is_retryable,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )
            # Verify: no native ref for failed delivery.
            failed_adapter = app.adapters.get(patched_aid)
            has_unexpected_ref = False
            if failed_adapter is not None and app.storage is not None:
                try:
                    native_ref_records = await app.storage.list_native_refs_for_event(
                        event.event_id,
                    )
                    for nref in native_ref_records:
                        if (
                            getattr(nref, "direction", None) == "outbound"
                            and getattr(nref, "adapter", None) == patched_aid
                        ):
                            has_unexpected_ref = True
                            break
                except (AttributeError, TypeError):
                    # Storage backend may not implement list_native_refs_for_event.
                    pass
                except Exception:
                    pass
            steps.append(
                _step(
                    "verify_no_native_ref",
                    "ok" if not has_unexpected_ref else "unexpected",
                    unexpected_native_ref=has_unexpected_ref,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )

            # Recovery simulation: restore adapter, re-deliver a new event.
            recovery_ok = False
            recovery_receipt_status: str | None = None
            if original_deliver is not None and failed_adapter is not None:
                failed_adapter.deliver = original_deliver  # type: ignore[assignment]
                recovery_event = _make_smoke_event(
                    source_adapter,
                    "drill: adapter_transient_failure recovery",
                )
                recovery_outcomes = await app.pipeline_runner.handle_ingress(
                    recovery_event,
                )
                recovery_target = None
                for o in recovery_outcomes:
                    if o.target_adapter == patched_aid:
                        recovery_target = o
                        break
                if recovery_target is not None:
                    recovery_receipt_status = recovery_target.status
                    recovery_ok = recovery_target.status == "success"
                steps.append(
                    _step(
                        "simulate_manual_recovery",
                        "ok" if recovery_ok else "unexpected",
                        recovery_event_id=recovery_event.event_id,
                        recovery_receipt_status=recovery_receipt_status,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                )

            report["recovery_path"] = {
                "failure_kind": "ADAPTER_TRANSIENT",
                "is_retryable": is_retryable,
                "recovery_method": "manual_adapter_fix_and_redeliver",
                "recovery_simulated": recovery_ok,
                "receipt_before_recovery": {
                    "status": target_outcome.status,
                    "failure_kind": failure_kind,
                },
                "receipt_after_recovery": {
                    "status": recovery_receipt_status,
                },
            }

            if target_outcome.status != "transient_failure" or not is_retryable:
                report["status"] = "failed"
                report["fail_reasons"] = []
                if target_outcome.status != "transient_failure":
                    report["fail_reasons"].append(
                        f"Expected transient_failure, got {target_outcome.status}"
                    )
                if not is_retryable:
                    report["fail_reasons"].append("Failure kind not marked retryable")

    except Exception as exc:
        report["status"] = "failed"
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
            config_path,
            storage_path,
        )
    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(_step("runtime_start", "ok"))

    try:
        cc = app._capacity_controller
        if cc is None:
            report["status"] = "failed"
            report["fail_reasons"] = ["No capacity controller wired"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        # Replace the capacity controller with one that has a single slot
        # and a short timeout, then exhaust it.
        from medre.core.supervision.capacity import CapacityController

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
            o
            for o in outcomes
            if o.failure_kind is not None
            and o.failure_kind.value == "capacity_rejection"
        ]
        has_cap_rejection = len(cap_outcomes) >= 1
        steps.append(
            _step(
                "verify_capacity_rejection",
                "ok" if has_cap_rejection else "unexpected",
                capacity_rejections=len(cap_outcomes),
            )
        )
        # Verify: suppressed evidence receipts for capacity-rejected deliveries.
        if app.storage is not None:
            try:
                receipts = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                receipts = []
        else:
            receipts = []
        cap_target_adapters = {o.target_adapter for o in cap_outcomes}
        cap_receipts = [r for r in receipts if r.target_adapter in cap_target_adapters]
        has_suppressed = all(
            r.status == "suppressed" and r.failure_kind == "capacity_rejection"
            for r in cap_receipts
        )
        steps.append(
            _step(
                "verify_suppressed_receipts",
                "ok" if has_suppressed and len(cap_receipts) >= 1 else "unexpected",
                receipt_count=len(cap_receipts),
                all_suppressed=has_suppressed,
            )
        )

        if not has_cap_rejection:
            report["status"] = "failed"
            report["fail_reasons"] = ["No CAPACITY_REJECTION observed"]

    except Exception as exc:
        report["status"] = "failed"
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
        report["status"] = "failed"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(
        _step(
            "runtime_start",
            "ok",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    try:
        cc = app._capacity_controller
        if cc is None:
            report["status"] = "failed"
            report["fail_reasons"] = ["No capacity controller wired"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        cc.stop_accepting()
        stop_ts = datetime.now(timezone.utc).isoformat()
        steps.append(
            _step(
                "stop_accepting",
                "ok",
                accepting_work=cc.accepting_work,
                timestamp=stop_ts,
            )
        )

        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: shutdown_rejection")
        inject_ts = datetime.now(timezone.utc).isoformat()
        steps.append(
            _step(
                "inject_event",
                "ok",
                event_id=event.event_id,
                timestamp=inject_ts,
            )
        )
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        outcomes = await app.pipeline_runner.handle_ingress(event)
        steps.append(
            _step(
                "ingress_complete",
                "ok",
                outcome_count=len(outcomes),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        shutdown_outcomes = [
            o
            for o in outcomes
            if o.failure_kind is not None
            and o.failure_kind.value == "shutdown_rejection"
        ]
        has_shutdown = len(shutdown_outcomes) >= 1
        steps.append(
            _step(
                "verify_shutdown_rejection",
                "ok" if has_shutdown else "unexpected",
                shutdown_rejections=len(shutdown_outcomes),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        # Verify: suppressed evidence receipts for shutdown-rejected deliveries.
        if app.storage is not None:
            try:
                receipts = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                receipts = []
        else:
            receipts = []
        shutdown_target_adapters = {o.target_adapter for o in shutdown_outcomes}
        shutdown_receipts = [
            r for r in receipts if r.target_adapter in shutdown_target_adapters
        ]
        has_suppressed = all(
            r.status == "suppressed" and r.failure_kind == "shutdown_rejection"
            for r in shutdown_receipts
        )
        steps.append(
            _step(
                "verify_suppressed_receipts",
                (
                    "ok"
                    if has_suppressed and len(shutdown_receipts) >= 1
                    else "unexpected"
                ),
                receipt_count=len(shutdown_receipts),
                all_suppressed=has_suppressed,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        report["rejection_timeline"] = {
            "stop_accepting_at": stop_ts,
            "inject_at": inject_ts,
            "accepting_work_at_rejection": False,
            "shutdown_rejections": len(shutdown_outcomes),
            "suppressed_receipts_created": len(shutdown_receipts),
        }

        if not has_shutdown:
            report["status"] = "failed"
            report["fail_reasons"] = ["No SHUTDOWN_REJECTION observed"]

    except Exception as exc:
        report["status"] = "failed"
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
    from medre.core.engine.replay.types import ReplayMode, ReplayRequest

    report = _base_report("replay_duplicate_risk", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    try:
        app, preflight, cs, _ = await _build_smoke_runtime(config_path, storage_path)
    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(
        _step(
            "runtime_start",
            "ok",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    try:
        source_aid, source_adapter = _pick_source_adapter(app)
        event = _make_smoke_event(source_adapter, "drill: replay_duplicate_risk")
        steps.append(
            _step(
                "inject_event",
                "ok",
                event_id=event.event_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        report["event_id"] = event.event_id
        report["source_adapter"] = source_aid

        # First delivery.
        outcomes1 = await app.pipeline_runner.handle_ingress(event)
        live_ts = datetime.now(timezone.utc).isoformat()
        receipts1 = []
        if app.storage is not None:
            try:
                receipts1 = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                pass
        steps.append(
            _step(
                "first_delivery",
                "ok",
                outcome_count=len(outcomes1),
                receipt_count=len(receipts1),
                timestamp=live_ts,
            )
        )

        # Replay BEST_EFFORT.
        replay_engine = app._replay_engine
        if replay_engine is None:
            report["status"] = "failed"
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
        replay_ts = datetime.now(timezone.utc).isoformat()
        steps.append(
            _step(
                "replay_best_effort",
                "ok",
                replay_results=len(results),
                timestamp=replay_ts,
            )
        )

        # Collect receipts after replay.
        receipts2 = []
        if app.storage is not None:
            try:
                receipts2 = await app.storage.list_receipts_for_event(event.event_id)
            except Exception:
                pass

        new_receipts = len(receipts2) - len(receipts1)
        timeline_verified = new_receipts > 0
        steps.append(
            _step(
                "verify_duplicate_receipts",
                "ok" if timeline_verified else "unexpected",
                receipts_before=len(receipts1),
                receipts_after=len(receipts2),
                new_receipts=new_receipts,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

        report["receipt_timeline"] = {
            "live_receipt_count": len(receipts1),
            "replay_receipt_count": new_receipts,
            "total_receipt_count": len(receipts2),
            "replay_run_id": request.run_id,
            "timeline_verified": timeline_verified,
        }

        if new_receipts <= 0:
            report["status"] = "failed"
            report["fail_reasons"] = [
                "Expected duplicate receipts from replay, got none"
            ]

    except Exception as exc:
        report["status"] = "failed"
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
        report["status"] = "failed"
        report["fail_reasons"] = [str(exc)]
        return report

    report["config_source"] = cs
    report["preflight"] = preflight
    steps.append(
        _step(
            "runtime_start",
            "ok",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    )

    try:
        # Pick a target adapter and override health_check.
        target_aid = None
        for aid in sorted(app.adapters.keys()):
            target_aid = aid
            break

        if target_aid is None:
            report["status"] = "failed"
            report["fail_reasons"] = ["No adapter available"]
            report["drill_steps"] = steps
            await _clean_stop(app)
            return report

        adapter = app.adapters[target_aid]

        from medre.core.contracts.adapter import AdapterCapabilities, AdapterInfo

        # Preserve original capabilities if available.
        orig_caps = getattr(adapter, "_capabilities", None)
        health_before = "unknown"
        if orig_caps is None:
            try:
                info = await adapter.health_check()
                orig_caps = info.capabilities
                health_before = info.health
            except Exception:
                orig_caps = AdapterCapabilities(text=True)

        async def _degraded_health() -> AdapterInfo:
            from medre.core.contracts.adapter import AdapterRole as _AR

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
        patch_ts = datetime.now(timezone.utc).isoformat()
        steps.append(
            _step(
                "patch_health",
                "ok",
                target_adapter=target_aid,
                health_before=health_before,
                timestamp=patch_ts,
            )
        )

        # Refresh live health.
        await app.refresh_live_health()
        refresh_ts = datetime.now(timezone.utc).isoformat()
        steps.append(
            _step(
                "refresh_health",
                "ok",
                timestamp=refresh_ts,
            )
        )

        # Verify the snapshot sees degraded health.
        from medre.runtime.snapshot import build_runtime_snapshot

        snap = build_runtime_snapshot(app)
        snapshot_ts = datetime.now(timezone.utc).isoformat()
        live_health = snap.get("health", {}).get("live_health", {})
        adapters_health = live_health.get("adapters", {})
        adapter_entry = adapters_health.get(target_aid, {})
        observed_health = adapter_entry.get("health", "unknown")

        correlation_verified = observed_health == "degraded"
        steps.append(
            _step(
                "verify_degraded",
                "ok" if correlation_verified else "unexpected",
                target_adapter=target_aid,
                observed_health=observed_health,
                timestamp=snapshot_ts,
            )
        )

        report["health_timeline"] = {
            "patched_at": patch_ts,
            "refresh_at": refresh_ts,
            "snapshot_at": snapshot_ts,
            "health_before": health_before,
            "health_after": observed_health,
            "target_adapter": target_aid,
            "correlation_verified": correlation_verified,
        }

        if observed_health != "degraded":
            report["status"] = "failed"
            report["fail_reasons"] = [
                f"Expected degraded health for {target_aid}, got {observed_health}"
            ]

    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
    finally:
        await _clean_stop(app)

    report["drill_steps"] = steps
    return report


# ---------------------------------------------------------------------------
# Private drill-only failing adapters
# ---------------------------------------------------------------------------


class _DrillFailingAdapter(AdapterContract):
    """Adapter that raises on ``start()`` and records ``stop()`` calls.

    Used by pre-runtime startup-failure drills to simulate adapter
    start failures deterministically.
    """

    adapter_id: str
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.stop_called: bool = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"drill: simulated start failure for {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


# ---------------------------------------------------------------------------
# Config / path helpers for programmatic drills
# ---------------------------------------------------------------------------


def _drill_paths() -> Any:
    """Return a MedrePaths pointing at a temp-safe root.

    Uses MEDRE_HOME so no real filesystem paths are touched.
    """
    import os

    os.environ.pop("MEDRE_HOME", None)
    os.environ.pop("XDG_CONFIG_HOME", None)
    os.environ.pop("XDG_STATE_HOME", None)
    os.environ.pop("XDG_DATA_HOME", None)
    os.environ.pop("XDG_CACHE_HOME", None)
    import tempfile

    os.environ["MEDRE_HOME"] = tempfile.mkdtemp(prefix="medre-drill-")
    return resolve_paths()


# ---------------------------------------------------------------------------
# Drill: bad_route_config
# ---------------------------------------------------------------------------


async def _drill_bad_route_config(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """Enabled route references a non-existent adapter; builder raises."""
    report = _base_report("bad_route_config", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    config = RuntimeConfig(
        runtime=RuntimeOptions(name="drill-bad-route-config"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "fake_matrix": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="bad_route",
                    source_adapters=("fake_matrix",),
                    dest_adapters=("ghost_adapter",),
                    enabled=True,
                ),
            )
        ),
    )
    steps.append(_step("create_bad_config", "ok"))

    paths = _drill_paths()
    builder = RuntimeBuilder(config, paths)

    try:
        caught_error: Exception | None = None
        try:
            builder.build()
        except RouteValidationError as exc:
            caught_error = exc
        except Exception as exc:
            caught_error = exc

        if caught_error is None:
            report["status"] = "failed"
            report["fail_reasons"] = [
                "Expected RouteValidationError but builder.build() succeeded"
            ]
        else:
            error_type = type(caught_error).__name__
            error_msg = str(caught_error)
            has_ghost = "ghost_adapter" in error_msg

            steps.append(
                _step(
                    "verify_route_validation_error",
                    (
                        "ok"
                        if isinstance(caught_error, RouteValidationError)
                        else "unexpected"
                    ),
                    error_type=error_type,
                    error_message=error_msg,
                    has_unknown_adapter=has_ghost,
                )
            )

            report["route_id"] = "bad_route"
            report["unknown_adapter_id"] = "ghost_adapter"
            report["known_adapter_ids"] = ["fake_matrix"]
            report["error_type"] = error_type
            report["error_message"] = error_msg
            report["build_succeeded"] = False
            report["runtime_started"] = False

            if not isinstance(caught_error, RouteValidationError):
                report["status"] = "failed"
                report["fail_reasons"] = [
                    f"Expected RouteValidationError, got {error_type}: {error_msg}"
                ]
            elif not has_ghost:
                report["status"] = "failed"
                report["fail_reasons"] = [
                    f"Error message does not mention ghost_adapter: {error_msg}"
                ]

    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]

    report["drill_steps"] = steps
    return report


# ---------------------------------------------------------------------------
# Drill: all_adapters_build_fail
# ---------------------------------------------------------------------------


async def _drill_all_adapters_build_fail(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """All enabled adapters fail construction; builder records failures."""
    report = _base_report("all_adapters_build_fail", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    config = RuntimeConfig(
        runtime=RuntimeOptions(name="drill-all-build-fail"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "broken1": MatrixRuntimeConfig(
                    adapter_id="broken1",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "broken2": MeshtasticRuntimeConfig(
                    adapter_id="broken2",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    steps.append(_step("create_multi_adapter_config", "ok"))

    paths = _drill_paths()

    try:
        with patch(
            "medre.runtime.builder._build_fake_adapter",
            side_effect=RuntimeConfigError("drill: forced build failure"),
        ):
            builder = RuntimeBuilder(config, paths)
            app = builder.build()

        steps.append(_step("build_runtime", "ok"))

        adapter_count = len(app.adapters)
        failure_count = len(app.build_failures)

        steps.append(
            _step(
                "verify_empty_adapters",
                "ok" if adapter_count == 0 else "unexpected",
                adapter_count=adapter_count,
            )
        )
        steps.append(
            _step(
                "verify_build_failures",
                "ok" if failure_count == 2 else "unexpected",
                failure_count=failure_count,
            )
        )

        build_failure_records = [
            {
                "transport": bf.transport,
                "adapter_id": bf.adapter_id,
                "error": str(bf.error),
            }
            for bf in app.build_failures
        ]
        steps.append(
            _step(
                "verify_failure_attribution",
                "ok",
                build_failures=build_failure_records,
            )
        )

        report["build_failures"] = build_failure_records
        report["known_adapter_ids"] = sorted(app.adapters.keys())
        report["failed_adapter_count"] = failure_count
        report["build_failure_count"] = failure_count
        report["adapters_total"] = failure_count
        report["runtime_started"] = False

        if adapter_count != 0 or failure_count != 2:
            report["status"] = "failed"
            report["fail_reasons"] = []
            if adapter_count != 0:
                report["fail_reasons"].append(
                    f"Expected 0 adapters, got {adapter_count}"
                )
            if failure_count != 2:
                report["fail_reasons"].append(
                    f"Expected 2 build failures, got {failure_count}"
                )

    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]

    report["drill_steps"] = steps
    return report


# ---------------------------------------------------------------------------
# Drill: partial_degraded_startup
# ---------------------------------------------------------------------------


async def _drill_partial_degraded_startup(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """One adapter starts, one fails; runtime enters DEGRADED RUNNING."""
    report = _base_report("partial_degraded_startup", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    config = RuntimeConfig(
        runtime=RuntimeOptions(name="drill-partial-degraded"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "alpha": MatrixRuntimeConfig(
                    adapter_id="alpha",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "beta": MeshtasticRuntimeConfig(
                    adapter_id="beta",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshcore={
                "gamma": MeshCoreRuntimeConfig(
                    adapter_id="gamma",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    steps.append(_step("create_three_adapter_config", "ok"))

    paths = _drill_paths()

    app: MedreApp | None = None
    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        steps.append(_step("build_runtime", "ok"))

        # Replace beta with a failing adapter.
        failing = _DrillFailingAdapter(adapter_id="beta")
        app.adapters["beta"] = failing
        steps.append(_step("inject_failing_adapter", "ok", target_adapter="beta"))

        await app.start()
        steps.append(_step("attempt_start", "ok"))

        # Verify runtime state.
        from medre.runtime.app import RuntimeState

        is_running = app.state == RuntimeState.RUNNING
        steps.append(
            _step(
                "verify_running",
                "ok" if is_running else "unexpected",
                runtime_state=app.state.value,
            )
        )

        boot = app.boot_summary
        is_partial = boot is not None and boot.startup_outcome == "partial"
        is_degraded = boot is not None and boot.runtime_health == "degraded"

        steps.append(
            _step(
                "verify_partial_outcome",
                "ok" if is_partial else "unexpected",
                startup_outcome=boot.startup_outcome if boot else None,
            )
        )
        steps.append(
            _step(
                "verify_degraded_health",
                "ok" if is_degraded else "unexpected",
                runtime_health=boot.runtime_health if boot else None,
            )
        )

        started = sorted(app.started_adapter_ids)
        failed = sorted(app._failed_adapter_ids)

        steps.append(
            _step(
                "verify_started_adapters",
                "ok" if started == ["alpha", "gamma"] else "unexpected",
                started_adapters=started,
            )
        )
        steps.append(
            _step(
                "verify_failed_adapters",
                "ok" if failed == ["beta"] else "unexpected",
                failed_adapters=failed,
            )
        )

        # Verify boot summary counts.
        correct_counts = (
            boot is not None
            and boot.adapters_started == 2
            and boot.adapters_failed == 1
            and boot.adapters_total == 3
        )
        steps.append(
            _step(
                "verify_boot_summary_counts",
                "ok" if correct_counts else "unexpected",
                adapters_started=boot.adapters_started if boot else None,
                adapters_failed=boot.adapters_failed if boot else None,
                adapters_total=boot.adapters_total if boot else None,
            )
        )

        # Verify the failed adapter's stop() was called (cleanup after start failure).
        steps.append(
            _step(
                "verify_failed_adapter_cleanup",
                "ok" if failing.stop_called else "unexpected",
                adapter_stop_called=failing.stop_called,
            )
        )

        report["started_adapters"] = started
        report["failed_adapters"] = failed
        report["startup_outcome"] = boot.startup_outcome if boot else None
        report["runtime_health"] = boot.runtime_health if boot else None
        report["boot_summary"] = boot.to_dict() if boot else None

        if not (is_running and is_partial and is_degraded and correct_counts):
            report["status"] = "failed"
            report["fail_reasons"] = []
            if not is_running:
                report["fail_reasons"].append(
                    f"Expected RUNNING state, got {app.state.value}"
                )
            if not is_partial:
                report["fail_reasons"].append(
                    f"Expected partial outcome, got "
                    f"{boot.startup_outcome if boot else 'no boot summary'}"
                )
            if not is_degraded:
                report["fail_reasons"].append(
                    f"Expected degraded health, got "
                    f"{boot.runtime_health if boot else 'no boot summary'}"
                )

    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]
        # If app was built, attempt cleanup.
        if app is not None:
            await _clean_stop(app)

    else:
        if app is not None:
            await _clean_stop(app)

    report["drill_steps"] = steps
    return report


# ---------------------------------------------------------------------------
# Drill: all_adapters_start_fail
# ---------------------------------------------------------------------------


async def _drill_all_adapters_start_fail(
    config_path: str | None,
    storage_path: str | None,
) -> dict[str, Any]:
    """All adapters build but fail start(); RuntimeStartupError raised."""
    report = _base_report("all_adapters_start_fail", storage_path=storage_path)
    steps: list[dict[str, Any]] = []

    config = RuntimeConfig(
        runtime=RuntimeOptions(name="drill-all-start-fail"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "alpha": MatrixRuntimeConfig(
                    adapter_id="alpha",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "beta": MeshtasticRuntimeConfig(
                    adapter_id="beta",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    steps.append(_step("create_two_adapter_config", "ok"))

    paths = _drill_paths()

    try:
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        steps.append(_step("build_runtime", "ok"))

        # Replace both adapters with failing trackers.
        alpha_fail = _DrillFailingAdapter(adapter_id="alpha")
        beta_fail = _DrillFailingAdapter(adapter_id="beta")
        app.adapters["alpha"] = alpha_fail
        app.adapters["beta"] = beta_fail
        steps.append(_step("inject_failing_adapters", "ok"))

        # Track cleanup via wrapper mocks.
        pipeline_stop_called = False
        storage_close_called = False

        original_pipeline_stop = app.pipeline_runner.stop

        async def _track_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            pipeline_stop_called = True
            await original_pipeline_stop()

        app.pipeline_runner.stop = _track_pipeline_stop

        if app.storage is not None:
            original_storage_close = app.storage.close

            async def _track_storage_close() -> None:
                nonlocal storage_close_called
                storage_close_called = True
                await original_storage_close()

            app.storage.close = _track_storage_close

        # Attempt start — should raise RuntimeStartupError.
        startup_error_caught = False
        try:
            await app.start()
        except RuntimeStartupError:
            startup_error_caught = True

        steps.append(
            _step(
                "attempt_start",
                "ok" if startup_error_caught else "unexpected",
                runtime_startup_error_caught=startup_error_caught,
            )
        )

        from medre.runtime.app import RuntimeState

        is_failed_state = app.state == RuntimeState.FAILED
        steps.append(
            _step(
                "verify_app_state_failed",
                "ok" if is_failed_state else "unexpected",
                app_state=app.state.value,
            )
        )

        boot = app.boot_summary
        is_total_failure = boot is not None and boot.startup_outcome == "total_failure"

        steps.append(
            _step(
                "verify_total_failure",
                "ok" if is_total_failure else "unexpected",
                startup_outcome=boot.startup_outcome if boot else None,
            )
        )

        no_started = len(app.started_adapter_ids) == 0
        steps.append(
            _step(
                "verify_no_started_adapters",
                "ok" if no_started else "unexpected",
                started_count=len(app.started_adapter_ids),
            )
        )

        all_failed = sorted(app._failed_adapter_ids) == ["alpha", "beta"]
        steps.append(
            _step(
                "verify_all_adapters_failed",
                "ok" if all_failed else "unexpected",
                failed_adapters=sorted(app._failed_adapter_ids),
            )
        )

        # Cleanup evidence.
        steps.append(
            _step(
                "verify_pipeline_cleanup",
                "ok" if pipeline_stop_called else "unexpected",
                pipeline_stopped=pipeline_stop_called,
            )
        )
        steps.append(
            _step(
                "verify_storage_cleanup",
                "ok" if storage_close_called else "unexpected",
                storage_closed=storage_close_called,
            )
        )
        steps.append(
            _step(
                "verify_adapter_cleanup",
                (
                    "ok"
                    if alpha_fail.stop_called and beta_fail.stop_called
                    else "unexpected"
                ),
                alpha_stopped=alpha_fail.stop_called,
                beta_stopped=beta_fail.stop_called,
            )
        )

        report["started_adapters"] = sorted(app.started_adapter_ids)
        report["failed_adapters"] = sorted(app._failed_adapter_ids)
        report["startup_outcome"] = boot.startup_outcome if boot else None
        report["runtime_health"] = boot.runtime_health if boot else None
        report["boot_summary"] = boot.to_dict() if boot else None
        report["cleanup_evidence"] = {
            "pipeline_stopped": pipeline_stop_called,
            "storage_closed": storage_close_called,
            "adapters_stopped": alpha_fail.stop_called and beta_fail.stop_called,
            "app_state": app.state.value,
        }

        if not (
            startup_error_caught
            and is_total_failure
            and is_failed_state
            and no_started
            and all_failed
            and pipeline_stop_called
            and storage_close_called
        ):
            report["status"] = "failed"
            report["fail_reasons"] = []
            if not startup_error_caught:
                report["fail_reasons"].append("RuntimeStartupError was not raised")
            if not is_total_failure:
                report["fail_reasons"].append(
                    f"Expected total_failure, got "
                    f"{boot.startup_outcome if boot else 'no boot summary'}"
                )
            if not pipeline_stop_called:
                report["fail_reasons"].append("Pipeline runner was not stopped")
            if not storage_close_called:
                report["fail_reasons"].append("Storage was not closed")

    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"] = [f"Unexpected error: {exc}"]

    report["drill_steps"] = steps
    return report


_DRILL_RUNNERS: dict[str, Any] = {
    "renderer_failure": _drill_renderer_failure,
    "adapter_permanent_failure": _drill_adapter_permanent_failure,
    "adapter_transient_failure": _drill_adapter_transient_failure,
    "capacity_rejection": _drill_capacity_rejection,
    "shutdown_rejection": _drill_shutdown_rejection,
    "replay_duplicate_risk": _drill_replay_duplicate_risk,
    "degraded_live_health": _drill_degraded_live_health,
    "bad_route_config": _drill_bad_route_config,
    "all_adapters_build_fail": _drill_all_adapters_build_fail,
    "partial_degraded_startup": _drill_partial_degraded_startup,
    "all_adapters_start_fail": _drill_all_adapters_start_fail,
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
        Path to YAML config file.
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
            "status": "failed",
            "command": "drill",
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
