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
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from medre.config.loader import load_config, ConfigSource
from medre.config.paths import MedrePaths
from medre.config.env import apply_env_overrides
from medre.core.events.canonical import CanonicalEvent
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

_LIMITATIONS: list[str] = [
    "Fake adapters only — no real transport connectivity proven",
    "Persistent storage (SQLite) but no crash-recovery proof",
    "Single-event session — no sustained throughput or load evidence",
    "No reconnection resilience or retry-against-live proof",
    "Fire-and-forget delivery model for radio transports",
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
) -> list[dict[str, str]]:
    """Resolve native refs for each successful delivery outcome."""
    refs: list[dict[str, str]] = []
    for outcome in outcomes:
        if outcome.status != "success":
            continue
        target = outcome.target_adapter
        adapter = app.adapters.get(target)
        if adapter is None:
            continue
        platform = getattr(adapter, "platform", "")
        if platform == "matrix":
            native_id = f"$fake_{outcome.event_id}"
            channel_id = ""
        elif platform in ("meshtastic", "meshcore"):
            native_id = "1"
            channel_id = "0"
        else:
            continue
        if app.storage is None:
            return refs
        resolved = await app.storage.resolve_native_ref(
            target, channel_id, native_id,
        )
        if resolved is not None:
            refs.append({
                "adapter": target,
                "channel": channel_id,
                "native_id": native_id,
                "resolves_to": resolved,
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
# Public API
# ---------------------------------------------------------------------------


async def run_bridge_session(
    config_path: str | None = None,
    storage_path: str | None = None,
    snapshot_dir: str | None = None,
    *,
    message_text: str = "medre run-session test",
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

    # -- Resolve config path ------------------------------------------------
    resolved_config_path = config_path
    config_source_value = "explicit"
    if resolved_config_path is None:
        default = _default_smoke_config_path()
        if default is not None:
            resolved_config_path = default
            config_source_value = "default"

    # -- Resolve storage path -----------------------------------------------
    auto_storage = False
    if storage_path is None:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", prefix="medre-session-", delete=False,
        )
        storage_path = tmp.name
        tmp.close()
        auto_storage = True

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
    import dataclasses as _dc
    from medre.config.model import StorageConfig as _SC
    config = _dc.replace(
        config,
        storage=_dc.replace(config.storage, backend="sqlite", path=storage_path),
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

    outcomes: list[Any] = []
    injection_error: str | None = None
    try:
        outcomes = await app.pipeline_runner.handle_ingress(event)
    except Exception as exc:
        injection_error = f"{type(exc).__name__}: {exc}"

    # -- Step 5: Poll for delivery receipts ---------------------------------
    receipts: list[Any] = []
    storage = app.storage
    if storage is not None and injection_error is None:
        try:
            receipts = await _poll_for_receipts(storage, event.event_id)
        except Exception as exc:
            _logger.warning("Receipt polling error: %s", exc)

    # Collect evidence while runtime is still running (storage is open).
    # Stored event
    stored_event: CanonicalEvent | None = None
    if storage is not None:
        try:
            stored_event = await storage.get(event.event_id)
        except Exception:
            pass

    # Native refs (must be collected before stop closes storage).
    native_refs = await _collect_native_refs(app, outcomes)

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
    except Exception:
        pass

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

    # -- Step 7: Inspect (already collected before stop) --------------------
    # Accounting
    accounting: dict[str, int] | None = None
    accounting_obj = getattr(app, "_runtime_accounting", None)
    if accounting_obj is not None and hasattr(accounting_obj, "snapshot"):
        try:
            accounting = accounting_obj.snapshot()
        except Exception:
            pass

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

    passed = (
        event_stored
        and has_success
        and has_sent_receipt
        and delivered_count >= 1
        and injection_error is None
        and runtime_state == "stopped"
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
    if runtime_state != "stopped":
        fail_reasons.append(f"Runtime state is '{runtime_state}', expected 'stopped'")

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

    return report
