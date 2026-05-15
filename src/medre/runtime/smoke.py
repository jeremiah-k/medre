"""Operator-facing fake bridge smoke runner.

Provides :func:`run_fake_bridge_smoke` — a single async function that
proves the MEDRE runtime pipeline works end-to-end with fake adapters.
Default smoke uses in-memory storage unless ``--storage-path`` is provided;
run-session uses SQLite persistent/temp storage.  Docker-free, network-free,
SDK-free.

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
  ``adapter.simulate_inbound(event)``, which goes through the
  adapter's ``ctx.publish_inbound`` callback before reaching
  ``handle_ingress`` internally.  This exercises the same callback
  path that real adapter inbound messages use, but does not return
  ``DeliveryOutcome`` objects — evidence is collected from storage
  polling instead.

Design note: ``handle_ingress`` is used instead of
:meth:`~medre.adapters.fake_matrix.FakeMatrixAdapter.simulate_inbound`
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

from medre.config.loader import load_config, ConfigSource
from medre.config.paths import MedrePaths, resolve as resolve_paths
from medre.config.env import apply_env_overrides
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.rendering.renderer import RenderingResult
from medre.observability.sanitization import sanitize_error
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

__all__ = ["run_fake_bridge_smoke"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limitations text
# ---------------------------------------------------------------------------

_LIMITATIONS: list[str] = [
    "Fake adapters only — no real transport connectivity proven",
    "In-memory storage — no persistence or crash-recovery proof",
    "No live codec verification for real packet formats",
    "No reconnection resilience or retry-against-live proof",
    "Fire-and-forget delivery model for radio transports",
]

# ---------------------------------------------------------------------------
# Default config resolution
# ---------------------------------------------------------------------------


def _default_smoke_config_path() -> str | None:
    """Return the shipped fake-bridge-smoke.toml path if it exists."""
    # Walk up from this file to find the repo root (src/medre/runtime/smoke.py)
    this_dir = Path(__file__).resolve().parent
    candidate = this_dir.parent.parent.parent / "examples" / "configs" / "fake-bridge-smoke.toml"
    if candidate.is_file():
        return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Event creation helper
# ---------------------------------------------------------------------------


def _make_smoke_event(
    adapter: Any,
    text: str,
) -> CanonicalEvent:
    """Create a canonical event with both 'body' and 'text' payload keys.

    FakeMatrixAdapter.make_event stores text under ``"body"`` but
    TextRenderer reads ``payload["text"]``.  This helper bridges the gap
    so the rendered output is non-empty and inspectable.
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


# ---------------------------------------------------------------------------
# Source adapter selection
# ---------------------------------------------------------------------------


def _pick_source_adapter(app: MedreApp) -> tuple[str, Any]:
    """Select a deterministic source adapter for smoke injection.

    Prefers a Matrix-platform adapter (presentation layer) since
    ``fake-bridge-smoke.toml`` routes originate from ``fake_matrix``.
    Falls back to the first adapter sorted alphabetically.
    """
    for aid in sorted(app.adapters.keys()):
        adapter = app.adapters[aid]
        if getattr(adapter, "platform", None) == "matrix":
            return aid, adapter
    # Fallback: first sorted adapter
    aid = sorted(app.adapters.keys())[0]
    return aid, app.adapters[aid]


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


def _run_preflight(config: Any) -> dict[str, Any]:
    """Run config and route validation without starting the runtime.

    Returns a dict summarising what was validated.
    """
    from medre.runtime.route_engine import (
        build_runtime_routes,
        RouteValidationError,
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
        except RouteValidationError as exc:
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
) -> list[dict[str, str]]:
    """Resolve native refs for each successful delivery outcome."""
    refs: list[dict[str, str]] = []
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
                    target, native_channel_id, native_message_id,
                )
            except Exception:
                continue
            refs.append({
                "adapter": target,
                "channel": native_channel_id,
                "native_id": native_message_id,
                "resolves_to": resolved or getattr(nref, "event_id", ""),
            })
    return refs


# ---------------------------------------------------------------------------
# Main smoke runner
# ---------------------------------------------------------------------------


async def run_fake_bridge_smoke(
    config_path: str | None = None,
    *,
    message_text: str = "medre smoke test",
    storage_path: str | None = None,
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
        Path to TOML config file.  Defaults to
        ``examples/configs/fake-bridge-smoke.toml`` when available.
    message_text:
        Body text for the injected event.
    storage_path:
        When provided, override storage to SQLite at this path instead of
        the default in-memory backend.  Allows operators to persist smoke
        evidence for post-run inspection.  ``None`` (the default) keeps
        the config's original ``storage.backend`` (typically ``"memory"``).
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
    except Exception as exc:
        return {
            "status": "failed",
            "command": "smoke",
            "fail_reason": sanitize_error(f"Config load error: {exc}"),
            "evidence_level": "fake_bridge",
            "timestamp": _now().isoformat(),
            "limitations": _LIMITATIONS,
            "sanitized": True,
        }

    config_source_value = source.value
    config = apply_env_overrides(config, paths)

    # -- Override storage if --storage-path provided -------------------------
    if storage_path is not None:
        import dataclasses as _dc
        from medre.config.model import StorageConfig as _SC
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
        }

    # -- Step 5: Inject event -----------------------------------------------
    source_aid, source_adapter = _pick_source_adapter(app)
    event = _make_smoke_event(source_adapter, message_text)

    outcomes: list[Any] = []
    injection_error: str | None = None
    try:
        outcomes = await app.pipeline_runner.handle_ingress(event)
    except Exception as exc:
        injection_error = f"{type(exc).__name__}: {exc}"

    # -- Step 6: Collect evidence -------------------------------------------
    storage = app.storage

    # Stored event
    stored_event: CanonicalEvent | None = None
    if storage is not None:
        try:
            stored_event = await storage.get(event.event_id)
        except Exception:
            pass

    # Delivery receipts
    receipts: list[Any] = []
    if storage is not None:
        try:
            receipts = await storage.list_receipts_for_event(event.event_id)
        except Exception:
            pass

    # Native refs
    native_refs = await _collect_native_refs(app, outcomes)

    # Accounting
    accounting: dict[str, int] | None = None
    if app._runtime_accounting is not None:
        try:
            accounting = app._runtime_accounting.snapshot()
        except Exception:
            pass

    # Route stats
    route_stats: dict[str, Any] | None = None
    if app.route_stats is not None:
        try:
            route_stats = app.route_stats.snapshot()
        except Exception:
            pass

    # Full snapshot
    snap: dict[str, Any] = {}
    try:
        snap = build_runtime_snapshot(
            app, now_fn=now_fn, monotonic_fn=monotonic_fn,
        )
    except Exception:
        pass

    # Target adapters from outcomes
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

    # -- Step 7: Stop cleanly -----------------------------------------------
    try:
        await app.stop()
    except Exception as exc:
        _logger.warning("Smoke stop error (non-fatal): %s", exc)

    # -- Step 8: Build report -----------------------------------------------
    # passed criteria
    event_stored = stored_event is not None
    has_success = any(o.status == "success" for o in outcomes)
    has_sent_receipt = any(r.status == "sent" for r in receipts)
    delivered_count = accounting.get("outbound_delivered", 0) if accounting else 0

    passed = (
        event_stored
        and has_success
        and has_sent_receipt
        and delivered_count >= 1
        and injection_error is None
    )

    fail_reasons: list[str] = []
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

    # Sanitize error fields.
    sanitized = False
    _sanitized_reasons: list[str] = []
    for r in fail_reasons:
        s = sanitize_error(r)
        if s != r:
            sanitized = True
        _sanitized_reasons.append(s)
    fail_reasons = _sanitized_reasons

    report: dict[str, Any] = {
        "status": "passed" if passed else "failed",
        "command": "smoke",
        "evidence_level": "fake_bridge",
        "timestamp": _now().isoformat(),
        "generated_at": _now().isoformat(),
        "config_source": config_source_value,
        "storage_backend": config.storage.backend,
        **({"storage_path": storage_path} if storage_path is not None else {}),
        "preflight": preflight,
        "source_adapter": source_aid,
        "target_adapters": target_adapters,
        "event_id": event.event_id,
        "route_ids": route_ids,
        "delivery_receipts": receipt_summaries,
        "native_refs": native_refs,
        "accounting": accounting,
        "route_stats": route_stats,
        "snapshot": {
            "schema_version": snap.get("schema_version", SCHEMA_VERSION),
            "lifecycle": snap.get("lifecycle", {}),
            "routes": {
                "stats": snap.get("routes", {}).get("stats", {}),
            },
            "accounting": snap.get("accounting", {}),
        },
        "limitations": _LIMITATIONS,
    }

    if sanitized:
        report["sanitized"] = True

    if not passed:
        report["fail_reasons"] = fail_reasons

    return report
