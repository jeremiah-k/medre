"""Explicit local-owner control for the exact unstarted-cancel and
exact-target decision authorities.

The OS file owner and private state directory are the local permission boundary.
Actor labels are audit provenance only, never credentials. A preview has no
execution authority: the write transaction rechecks all identity/version data.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.observability.exact_target import (
    ExactTargetPreview,
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite._operator_decision import (
    ACTIVE_NATIVE_UNDECIDABLE,
    COMPETING_GENERATION,
    CONFIRMATION_PENDING_STATUS,
    EXACT_TARGET_GONE,
    OBSERVATION_STALE,
    RECEIPT_PROVENANCE_CONTRADICTION,
    SETTLED_RECEIPT_CONTRADICTION,
    SETTLED_TARGET_UNDECIDABLE,
    TERMINAL_REOPEN_REFUSED,
    CancelUnstartedDecision,
    ConfirmationPendingResolution,
    ExactTargetDecision,
    OperatorDecisionRefused,
)
from medre.core.storage.sqlite.storage import SQLiteStorage

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_FAILED, EXIT_NOT_FOUND
from .json import to_json
from .storage_helpers import _open_readonly_storage


def _check_private_local_store(path: str) -> str:
    """Reject remote/ambiguous file authority before constructing a writer.

    This supports only a private POSIX local file and its private parent.
    A shared group, symlink, hard link, or other user's file is not a
    sufficient basis for a local operator action.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise PermissionError("Local cancellation requires a POSIX owner boundary")
    if ".." in Path(path).parts:
        raise PermissionError("Refusing parent traversal in database path")
    candidate = Path(os.path.abspath(path))
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise PermissionError("Refusing a symlinked database path")
    owner = os.geteuid()
    for ancestor in candidate.parents:
        info = ancestor.stat()
        if info.st_uid not in {0, owner} or (
            info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
        ):
            raise PermissionError(
                "Database ancestors must be controlled by the owner or root"
            )
    entry = candidate.stat()
    parent = candidate.parent.stat()
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != 1
        or entry.st_uid != os.geteuid()
        or entry.st_mode & 0o077
    ):
        raise PermissionError("Database must be an owner-only regular file")
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
    ):
        raise PermissionError("Database parent must be an owner-only directory")
    return str(candidate)


def _decision_fields(item: Any) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "delivery_plan_id": item.delivery_plan_id,
        "target_adapter": item.target_adapter,
        "target_channel": item.target_channel or None,
        "outbox_id": item.outbox_id,
        "expected_generation": item.attempt_number,
        "expected_status": item.status,
        "expected_updated_at": (
            item.updated_at.isoformat()
            if hasattr(item.updated_at, "isoformat")
            else str(item.updated_at)
        ),
    }


#: Refusal implications rendered for observed categories the decision
#: authority refuses. The strings quote the authority's frozen refusal
#: vocabulary. ``ambiguous`` lists both refusal conditions a contested
#: identity can produce. Categories absent from this mapping
#: (``content_unavailable`` and ``route_or_plan_changed``) render the
#: re-observation sentence below.
_DECISION_CATEGORY_IMPLICATIONS: dict[str, tuple[str, ...]] = {
    "in_progress": (ACTIVE_NATIVE_UNDECIDABLE,),
    "awaiting_feedback": (ACTIVE_NATIVE_UNDECIDABLE,),
    "inspect_native": (ACTIVE_NATIVE_UNDECIDABLE,),
    "preview_replay": (TERMINAL_REOPEN_REFUSED,),
    "needs_operator_action": (TERMINAL_REOPEN_REFUSED,),
    "completed": (SETTLED_TARGET_UNDECIDABLE,),
    "cancelled": (SETTLED_TARGET_UNDECIDABLE,),
    "not_found": (EXACT_TARGET_GONE,),
    "ambiguous": (EXACT_TARGET_GONE, COMPETING_GENERATION),
    "stale": (OBSERVATION_STALE,),
}
_REOBSERVE_REQUIRED = (
    "This observation does not support a decision; re-observe the exact target"
)


def _decision_category_implications(preview: ExactTargetPreview) -> tuple[str, ...]:
    """Name the authority refusals implied by one observed category."""
    if preview.category in _DECISION_CATEGORY_IMPLICATIONS:
        return _DECISION_CATEGORY_IMPLICATIONS[preview.category]
    return (_REOBSERVE_REQUIRED,)


def _decision_decidable(preview: ExactTargetPreview) -> bool:
    """Mirror the decision authority's unstarted-scheduled eligibility.

    The row is ``pending``/``retry_wait`` with no reservation and no claim
    ownership — the same predicate ``cancel_eligible`` mirrors. Reservations
    promote the category to ``in_progress`` before this check, so the
    category gate excludes them; the claim-ownership check matches the
    eligibility mirror. Competing generations and staleness remain
    transaction-verified and never make a preview decidable here.
    """
    return preview.cancel_eligible and preview.category in {
        "pending",
        "retry_scheduled",
    }


def _decision_observed_fields(preview: ExactTargetPreview) -> dict[str, Any]:
    """The observed state an apply must copy exactly, as cancellation renders it."""
    return {
        "event_id": preview.event_id,
        "delivery_plan_id": preview.delivery_plan_id,
        "target_adapter": preview.target_adapter,
        "target_channel": preview.target_channel,
        "outbox_id": preview.outbox_id,
        "expected_generation": preview.attempt_number,
        "expected_status": preview.status,
        "expected_updated_at": preview.updated_at,
    }


def _decision_preview_payload(preview: ExactTargetPreview) -> dict[str, Any]:
    """Metadata-only JSON for one exact-target decision observation."""
    decidable = _decision_decidable(preview)
    payload: dict[str, Any] = {
        "apply_authorized": False,
        "decision_capable": decidable,
        "preview": preview.to_dict(),
    }
    if decidable:
        payload["observed"] = _decision_observed_fields(preview)
    else:
        payload["refusal_implications"] = list(_decision_category_implications(preview))
    return payload


async def _preview_exact_target_decision(
    storage_path: str, identity: DeliveryIdentity
) -> None:
    """Observe one exact target for decision-making; never authorize a write.

    The typed preview is opened on the same private owner-only file the
    apply path validates: its content-authority read may durably record an
    expired mandatory dependency, which a read-only connection cannot
    record. The rendered JSON stays metadata-only either way.
    """
    try:
        selection = ExactTargetSelection(
            event_id=identity.event_id,
            delivery_plan_id=identity.delivery_plan_id,
            target_adapter=identity.target_adapter,
            target_channel=identity.target_channel,
        )
    except ValueError as exc:
        print(f"Invalid exact-target selection: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc
    store = SQLiteStorage(storage_path)
    try:
        await store.initialize()
        preview = await preview_exact_target(store, selection)
    except ValueError as exc:
        print(f"Invalid exact-target selection: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc
    finally:
        await store.close()
    print(to_json(_decision_preview_payload(preview)))


async def _apply_exact_target_decision(
    args: Any, storage_path: str, identity: DeliveryIdentity
) -> None:
    """Record one audited exact-target decision under the private-local boundary."""
    if not args.yes or not args.actor_label or not args.decision_id:
        print(
            "Decision apply requires --yes, --actor-label and --decision-id",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    observed = {
        "--outbox-id": args.outbox_id,
        "--expected-generation": args.expected_generation,
        "--expected-status": args.expected_status,
        "--expected-updated-at": args.expected_updated_at,
    }
    missing = sorted(name for name, value in observed.items() if value is None)
    if missing:
        print(
            "Decision apply requires the observed state: " + ", ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    try:
        decision = ExactTargetDecision(
            decision_id=args.decision_id,
            actor_id=args.actor_label,
            action=args.action,
            identity=identity,
            outbox_id=args.outbox_id,
            expected_generation=args.expected_generation,
            expected_status=args.expected_status,
            expected_updated_at=args.expected_updated_at,
        )
    except ValueError as exc:
        print(f"Invalid decision request: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc
    store = SQLiteStorage(storage_path)
    try:
        await store.initialize()
        committed = await store.record_exact_target_decision(decision)
    except OperatorDecisionRefused as exc:
        print(f"Decision refused: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from exc
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc
    finally:
        await store.close()
    print(to_json({"decision_id": decision.decision_id, "committed": committed}))


async def _resolve_exact_target_decision(
    args: Any, storage_path: str, identity: DeliveryIdentity
) -> None:
    """Dispatch the exact-target decision preview or apply for one identity."""
    if not args.apply:
        await _preview_exact_target_decision(storage_path, identity)
        return
    await _apply_exact_target_decision(args, storage_path, identity)


def _confirmation_pending_observed_fields(item: DeliveryOutboxItem) -> dict[str, Any]:
    """The observed confirmation-pending state an apply must copy exactly."""
    return _decision_fields(item)


def _receipt_provenance_refusal(
    item: DeliveryOutboxItem, receipts: list[Any]
) -> str | None:
    """Name the receipt-evidence refusal for one confirmation-pending row.

    Reuses the lifecycle ``_receipt_provenance_conflicts`` discipline: every
    receipt bound to the effective attempt must carry the claimed delivery
    identity, origin, and replay-run provenance. A committed ``sent``
    receipt settles the delivery already and outranks any ruling, and a
    terminal receipt at the effective attempt leaves the delivery settled
    by evidence, so the resolution authority refuses it. This mirrors the
    write-transaction refusals exactly so a capable preview never hands the
    operator an observation the apply would refuse.
    """
    attempt_receipts = [
        receipt
        for receipt in receipts
        if receipt.attempt_number == item.attempt_number
        and receipt.outbox_id == item.outbox_id
    ]
    if any(receipt.status == "sent" for receipt in attempt_receipts):
        return SETTLED_RECEIPT_CONTRADICTION
    if DeliveryLifecycleService._receipt_provenance_conflicts(
        item, item.attempt_number, attempt_receipts
    ):
        return RECEIPT_PROVENANCE_CONTRADICTION
    if any(
        receipt.status in {"cancelled", "abandoned", "dead_lettered", "suppressed"}
        for receipt in attempt_receipts
    ):
        return SETTLED_TARGET_UNDECIDABLE
    return None


async def _preview_confirmation_pending(
    storage_path: str, identity: DeliveryIdentity
) -> None:
    """Observe one confirmation-pending row; never authorize a write."""
    store = await _open_readonly_storage(storage_path)
    try:
        items = await store.list_outbox_items_for_event(identity.event_id)
        candidates = [
            item
            for item in items
            if DeliveryIdentity(
                item.event_id,
                item.delivery_plan_id,
                item.target_adapter,
                item.target_channel,
            )
            == identity
            and item.status == CONFIRMATION_PENDING_STATUS
            and item.active_attempt is None
            and all(
                getattr(item, key) is None
                for key in ("worker_id", "locked_at", "lease_until")
            )
        ]
        if len(candidates) == 1 and any(
            other.outbox_id != candidates[0].outbox_id
            and DeliveryIdentity(
                other.event_id,
                other.delivery_plan_id,
                other.target_adapter,
                other.target_channel,
            )
            == identity
            and max(other.attempt_number, other.active_attempt or 0)
            >= candidates[0].attempt_number
            for other in items
        ):
            candidates = []
        if len(candidates) != 1:
            print(
                "Exact confirmation-pending obligation is absent or ambiguous",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_NOT_FOUND)
        item = candidates[0]
        receipts = await store.list_receipts_for_outbox(item.outbox_id)
        refusal = _receipt_provenance_refusal(item, receipts)
        payload: dict[str, Any] = {"apply_authorized": False}
        if refusal is not None:
            payload["decision_capable"] = False
            payload["refusal_implications"] = [refusal]
        else:
            payload["decision_capable"] = True
            payload["observed"] = _confirmation_pending_observed_fields(item)
        print(to_json(payload))
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc
    finally:
        await store.close()


async def _apply_confirmation_pending(
    args: Any, storage_path: str, identity: DeliveryIdentity
) -> None:
    """Resolve one confirmation-pending row under the private-local boundary."""
    if not args.yes or not args.actor_label or not args.decision_id:
        print(
            "Resolution apply requires --yes, --actor-label and --decision-id",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    observed = {
        "--outbox-id": args.outbox_id,
        "--expected-generation": args.expected_generation,
        "--expected-status": args.expected_status,
        "--expected-updated-at": args.expected_updated_at,
    }
    missing = sorted(name for name, value in observed.items() if value is None)
    if missing:
        print(
            "Resolution apply requires the observed state: " + ", ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    try:
        decision = ConfirmationPendingResolution(
            decision_id=args.decision_id,
            actor_id=args.actor_label,
            identity=identity,
            outbox_id=args.outbox_id,
            expected_generation=args.expected_generation,
            expected_status=args.expected_status,
            expected_updated_at=args.expected_updated_at,
            resolution=args.resolution,
        )
    except ValueError as exc:
        print(f"Invalid resolution request: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc
    store = SQLiteStorage(storage_path)
    try:
        await store.initialize()
        item = await store.get_outbox_item(decision.outbox_id)
        if item is not None and item.status == CONFIRMATION_PENDING_STATUS:
            receipts = await store.list_receipts_for_outbox(item.outbox_id)
            refusal = _receipt_provenance_refusal(item, receipts)
            if refusal is not None:
                print(f"Resolution refused: {refusal}", file=sys.stderr)
                raise SystemExit(EXIT_FAILED)
        committed = await store.resolve_confirmation_pending(decision)
    except OperatorDecisionRefused as exc:
        print(f"Resolution refused: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from exc
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc
    finally:
        await store.close()
    print(to_json({"decision_id": decision.decision_id, "committed": committed}))


async def _resolve_confirmation_pending(args: Any, storage_path: str) -> None:
    """Dispatch the confirmation-pending preview or apply for one identity."""
    channel = None if args.no_channel else args.target_channel
    identity = DeliveryIdentity(args.event, args.plan_id, args.target_adapter, channel)
    if not identity.complete:
        print("A complete delivery identity is required", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if not args.apply:
        await _preview_confirmation_pending(storage_path, identity)
        return
    await _apply_confirmation_pending(args, storage_path, identity)


async def _resolve_unstarted(args: Any) -> None:
    """Preview one target or transactionally apply the observed exact request."""
    try:
        storage_path = _check_private_local_store(args.storage_path)
    except (PermissionError, OSError) as exc:
        print(f"Local permission refused: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc

    if not args.no_channel and not args.target_channel:
        print("An explicit nonempty channel is required", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if args.resolution is not None:
        if args.action is not None:
            print(
                "--resolution and --action are mutually exclusive",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_CONFIG)
        await _resolve_confirmation_pending(args, storage_path)
        return
    channel = args.target_channel if not args.no_channel else None
    identity = DeliveryIdentity(args.event, args.plan_id, args.target_adapter, channel)
    if not identity.complete:
        print("A complete delivery identity is required", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    if args.action is not None:
        await _resolve_exact_target_decision(args, storage_path, identity)
        return
    if args.expected_status == CONFIRMATION_PENDING_STATUS:
        print(
            "Queued confirmation-pending work requires --resolution",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    if args.apply:
        if not args.yes or not args.actor_label or not args.decision_id:
            print(
                "Apply requires --yes, --actor-label and --decision-id",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_CONFIG)
        try:
            request = CancelUnstartedDecision(
                decision_id=args.decision_id,
                actor_id=args.actor_label,
                identity=identity,
                outbox_id=args.outbox_id,
                expected_generation=args.expected_generation,
                expected_status=args.expected_status,
                expected_updated_at=args.expected_updated_at,
            )
        except ValueError as exc:
            print(f"Invalid cancellation request: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_CONFIG) from exc
        store = SQLiteStorage(storage_path)
        try:
            await store.initialize()
            changed = await store.cancel_unstarted_delivery(request)
        except OperatorDecisionRefused as exc:
            print(f"Cancellation refused: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_FAILED) from exc
        except StorageError as exc:
            print(f"Storage error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_BUILD) from exc
        finally:
            await store.close()
        print(to_json({"decision_id": request.decision_id, "committed": changed}))
        return

    # The first step is read-only and contains no execution token.
    store = await _open_readonly_storage(storage_path)
    try:
        items = await store.list_outbox_items_for_event(args.event)
        candidates = [
            item
            for item in items
            if DeliveryIdentity(
                item.event_id,
                item.delivery_plan_id,
                item.target_adapter,
                item.target_channel,
            )
            == identity
            and item.status in {"pending", "retry_wait"}
            and item.active_attempt is None
            and all(
                getattr(item, key) is None
                for key in ("worker_id", "locked_at", "lease_until")
            )
        ]
        if len(candidates) == 1 and any(
            other.outbox_id != candidates[0].outbox_id
            and DeliveryIdentity(
                other.event_id,
                other.delivery_plan_id,
                other.target_adapter,
                other.target_channel,
            )
            == identity
            and max(other.attempt_number, other.active_attempt or 0)
            >= candidates[0].attempt_number
            for other in items
        ):
            candidates = []
        if len(candidates) != 1:
            print(
                "Exact unstarted obligation is absent or ambiguous",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_NOT_FOUND)
        print(
            to_json(
                {"apply_authorized": False, "observed": _decision_fields(candidates[0])}
            )
        )
    except StorageError as exc:
        print(f"Storage error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BUILD) from exc
    finally:
        await store.close()
