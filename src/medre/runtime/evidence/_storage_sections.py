"""Storage evidence sections — shared backend collector, config-backed, and storage-path direct mode."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable

from medre.config.paths import MedrePaths, MedrePathsError

from ._helpers import (
    _LIMITATIONS,
    SCHEMA_VERSION,
    _compute_overall_status,
    _get_version,
    _now_utc,
    _section_error,
    _section_ok,
    _section_partial,
    _section_skipped,
)

# ---------------------------------------------------------------------------
# Shared storage data collector
# ---------------------------------------------------------------------------


async def _collect_storage_data_from_backend(
    storage: Any,
    db_path: str,
    event_id: str | None,
    replay_run_id: str | None,
) -> dict[str, Any]:
    """Collect storage evidence from an already-opened read-only backend.

    Accepts a storage instance opened in read-only mode by the caller.
    Returns a section dict (``status``, ``data``, ``error``) with the same
    shape as the ``storage`` section in the evidence bundle.

    The caller is responsible for opening and closing the storage backend.
    """
    import medre.runtime.timeline as _timeline

    data: dict[str, Any] = {
        "db_exists": True,
        "db_path": db_path,
        "event": None,
        "event_count": None,
        "native_refs_for_event": None,
        "receipt_count": None,
        "replay_run_receipts": None,
        "timeline": None,
        "replay_timeline": None,
    }

    try:
        # Counts.
        data["event_count"] = await storage.count_events()
        data["receipt_count"] = await storage.count_receipts()

        # Optional event lookup.
        if event_id is not None:
            import json as _json

            import msgspec

            tl_result = await _timeline.assemble_event_timeline(
                storage,
                event_id,
            )
            if tl_result is not None:
                event = tl_result["event"]
                receipts = tl_result["receipts"]
                native_refs = tl_result["native_refs"]

                data["event"] = _json.loads(msgspec.json.encode(event))
                data["native_refs_for_event"] = [
                    {
                        **_json.loads(msgspec.json.encode(r)),
                        "resolves_to": r.event_id,
                    }
                    for r in native_refs
                ]

                data["timeline"] = tl_result["timeline_entries"]

                # Compact incident summary using shared classification.
                from medre.core.observability.classification import (
                    failure_category,
                    infer_failure_kind,
                    recommended_commands,
                )
                from medre.runtime.reporting import (
                    delivery_receipt_to_report_dict as _receipt_to_report,
                )

                receipt_dicts = [_json.loads(msgspec.json.encode(r)) for r in receipts]
                # Enriched report dicts with derived fields (retryable,
                # failure_kind_detail, retry policy, etc.).
                enriched_dicts = [_receipt_to_report(r) for r in receipts]

                failed_count = sum(
                    1
                    for r in receipt_dicts
                    if r.get("status") in ("failed", "dead_lettered")
                )
                sent_count = sum(1 for r in receipt_dicts if r.get("status") == "sent")

                first_failure_kind: str | None = None
                worst_category = "success"
                for r in receipt_dicts:
                    if r.get("status") in ("failed", "dead_lettered"):
                        # Use persisted failure_kind first, fall back to inference.
                        fk = r.get("failure_kind") or infer_failure_kind(
                            r.get("error"),
                            r.get("status", ""),
                        )
                        if first_failure_kind is None:
                            first_failure_kind = fk
                        cat = failure_category(fk)
                        if cat != "success":
                            worst_category = cat
                            break

                # If no failed/dead_lettered receipts set the category,
                # check suppressed receipts for classification.
                if worst_category == "success":
                    for r in receipt_dicts:
                        if r.get("status") == "suppressed":
                            fk = r.get("failure_kind") or infer_failure_kind(
                                r.get("error"),
                                r.get("status", ""),
                            )
                            if first_failure_kind is None:
                                first_failure_kind = fk
                            cat = failure_category(fk)
                            if cat != "success":
                                worst_category = cat
                                break

                has_replay = any(r.get("source") == "replay" for r in receipt_dicts)
                has_native_refs = len(native_refs) > 0

                # Determine overall classification for the event.
                if failed_count == 0 and worst_category == "success":
                    classification = "success"
                elif worst_category != "success":
                    classification = worst_category
                else:
                    classification = "unknown"

                cmds = (
                    recommended_commands(classification, event_id)
                    if classification != "success"
                    else [f"medre inspect event {event_id} --timeline"]
                )

                # Specialised lower-level command for evidence bundle.
                evidence_cmd = f"medre evidence --event {event_id} --json"

                # Primary commands are the inspect-first recommendations.
                # Specialised commands are lower-level tools (evidence, trace).
                structured_commands: dict[str, Any] = {
                    "primary": cmds,
                    "specialized": [evidence_cmd],
                }

                # If a replay run is in context, add inspect_replay to primary.
                if replay_run_id is not None:
                    structured_commands["primary"] = list(cmds) + [
                        f"medre inspect event {event_id} --replay-run {replay_run_id}",
                    ]

                # --- Incident summary enrichment (additive) ---

                dead_lettered_count = sum(
                    1 for r in receipt_dicts if r.get("status") == "dead_lettered"
                )
                suppressed_count = sum(
                    1 for r in receipt_dicts if r.get("status") == "suppressed"
                )
                sent_unconfirmed_count = sum(
                    1 for r in receipt_dicts if r.get("status") == "sent"
                )

                # Target-keyed delivery state: group by composite key
                # (target_adapter, target_channel, route_id, delivery_plan_id),
                # keeping the receipt with the highest attempt_number per key,
                # then the latest receipt sequence within that attempt.
                _target_groups: dict[str, list[dict[str, object]]] = {}
                for rd in enriched_dicts:
                    comp = _json.dumps(
                        {
                            "delivery_plan_id": rd.get("delivery_plan_id"),
                            "route_id": rd.get("route_id"),
                            "target_adapter": rd.get("target_adapter"),
                            "target_channel": rd.get("target_channel"),
                        },
                        sort_keys=True,
                    )
                    _target_groups.setdefault(comp, []).append(rd)

                delivery_state_by_target: dict[str, dict[str, object]] = {}
                for target_key, group in _target_groups.items():
                    # Select receipt with the highest attempt_number, then
                    # the latest receipt sequence within that attempt.
                    best_idx = 0
                    best_attempt: int = 0
                    for idx, rd in enumerate(group):
                        attempt = rd.get("attempt_number")
                        attempt_int = attempt if isinstance(attempt, int) else 0
                        if attempt_int > best_attempt or (
                            attempt_int == best_attempt and idx > best_idx
                        ):
                            best_attempt = attempt_int
                            best_idx = idx
                    best = group[best_idx]
                    delivery_state_by_target[target_key] = {
                        "target_adapter": best.get("target_adapter"),
                        "target_channel": best.get("target_channel"),
                        "route_id": best.get("route_id"),
                        "delivery_plan_id": best.get("delivery_plan_id"),
                        "status": best.get("status"),
                        "attempt_number": best.get("attempt_number"),
                        "failure_kind": best.get("failure_kind"),
                        "failure_kind_detail": best.get("failure_kind_detail"),
                        "retryable": best.get("retryable"),
                        "next_retry_at": best.get("next_retry_at"),
                        "native_message_id": best.get("native_message_id"),
                        "adapter_message_id": best.get("adapter_message_id"),
                    }

                data["incident_summary"] = {
                    # Original keys (unchanged).
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
                    "commands": structured_commands,
                    # Additive enrichment keys.
                    "dead_lettered_count": dead_lettered_count,
                    "suppressed_count": suppressed_count,
                    "sent_unconfirmed_count": sent_unconfirmed_count,
                    "delivery_state_by_target": delivery_state_by_target,
                }
            # else: event not found — keep None, not an error for the section.

        # Optional replay-run receipts.
        if replay_run_id is not None:
            import json as _json

            import msgspec

            tl_replay = await _timeline.assemble_replay_timeline(
                storage,
                replay_run_id,
            )
            if tl_replay is not None:
                data["replay_run_receipts"] = [
                    _json.loads(msgspec.json.encode(r)) for r in tl_replay["receipts"]
                ]
                data["replay_timeline"] = tl_replay["timeline_entries"]
            else:
                data["replay_run_receipts"] = []

        # If event was requested but not found, report partial.
        if event_id is not None and data["event"] is None:
            return _section_partial(data, f"Event {event_id!r} not found in storage")

        return _section_ok(data)
    except Exception as exc:
        return _section_partial(data, f"Storage query error: {exc}")


# ---------------------------------------------------------------------------
# Storage section (config-backed)
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
    from medre.core.storage.sqlite import SQLiteStorage

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
    if not db_exists:
        return _section_partial(
            {
                "db_exists": False,
                "db_path": db_path,
                "event": None,
                "event_count": None,
                "native_refs_for_event": None,
                "receipt_count": None,
                "replay_run_receipts": None,
                "timeline": None,
                "replay_timeline": None,
            },
            f"Database file does not exist: {db_path}",
        )

    # Open read-only.
    storage: Any | None = None
    try:
        storage = await SQLiteStorage.open_readonly(db_path)
    except Exception as exc:
        return _section_partial(
            {
                "db_exists": True,
                "db_path": db_path,
                "event": None,
                "event_count": None,
                "native_refs_for_event": None,
                "receipt_count": None,
                "replay_run_receipts": None,
                "timeline": None,
                "replay_timeline": None,
            },
            f"Cannot open database read-only: {exc}",
        )

    try:
        return await _collect_storage_data_from_backend(
            storage,
            db_path,
            event_id,
            replay_run_id,
        )
    finally:
        if storage is not None:
            await storage.close()


# ---------------------------------------------------------------------------
# storage-path direct mode
# ---------------------------------------------------------------------------


def _build_storage_path_bundle(
    sections: dict[str, Any],
    errors: list[str],
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    """Assemble the top-level bundle dict for ``--storage-path`` mode."""
    overall = _compute_overall_status(sections)
    return {
        "collected_at": now_fn().isoformat(),
        "command": "evidence",
        "config_source": "storage_path",
        "errors": errors,
        "generated_at": now_fn().isoformat(),
        "limitations": _LIMITATIONS,
        "medre_version": _get_version(),
        "runtime_started": False,
        "schema_version": SCHEMA_VERSION,
        "sections": sections,
        "status": overall,
    }


async def _collect_storage_path_bundle(
    storage_path: str,
    *,
    event_id: str | None = None,
    replay_run_id: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Collect evidence bundle using a direct storage path (no config file).

    Opens the database read-only and collects only storage/trace evidence.
    Config, route validation, diagnostics, and live health sections are
    skipped with clear notes.

    Storage data collection is delegated to
    :func:`_collect_storage_data_from_backend`, shared with the config-backed
    path.
    """
    from medre.core.storage.sqlite import SQLiteStorage

    _now = now_fn or _now_utc

    sections: dict[str, Any] = {}
    errors: list[str] = []

    sections["config_summary"] = _section_skipped(
        "Not available with --storage-path (no config file loaded)"
    )
    sections["route_validation"] = _section_skipped(
        "Not available with --storage-path (no config file loaded)"
    )
    sections["diagnostics_snapshot"] = _section_skipped(
        "Not available with --storage-path (no runtime built)"
    )
    sections["live_health"] = _section_skipped(
        "Not available with --storage-path (no runtime started)"
    )

    # Storage section: open the DB directly.
    db_exists = os.path.exists(storage_path)
    if not db_exists:
        sections["storage"] = _section_partial(
            {
                "db_exists": False,
                "db_path": storage_path,
                "event": None,
                "event_count": None,
                "native_refs_for_event": None,
                "receipt_count": None,
                "replay_run_receipts": None,
                "timeline": None,
                "replay_timeline": None,
            },
            f"Database file does not exist: {storage_path}",
        )
        errors.append(sections["storage"]["error"])
        return _build_storage_path_bundle(sections, errors, _now)

    storage: Any | None = None
    try:
        storage = await SQLiteStorage.open_readonly(storage_path)
    except Exception as exc:
        sections["storage"] = _section_partial(
            {
                "db_exists": True,
                "db_path": storage_path,
                "event": None,
                "event_count": None,
                "native_refs_for_event": None,
                "receipt_count": None,
                "replay_run_receipts": None,
                "timeline": None,
                "replay_timeline": None,
            },
            f"Cannot open database read-only: {exc}",
        )
        errors.append(sections["storage"]["error"])
        return _build_storage_path_bundle(sections, errors, _now)

    try:
        sections["storage"] = await _collect_storage_data_from_backend(
            storage,
            storage_path,
            event_id,
            replay_run_id,
        )
    finally:
        if storage is not None:
            await storage.close()

    if sections["storage"]["error"]:
        errors.append(sections["storage"]["error"])

    return _build_storage_path_bundle(sections, errors, _now)
