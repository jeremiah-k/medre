"""Explicit local-owner control for the existing exact unstarted-cancel authority.

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
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite._operator_decision import (
    CancelUnstartedDecision,
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
    channel = args.target_channel if not args.no_channel else None
    identity = DeliveryIdentity(args.event, args.plan_id, args.target_adapter, channel)
    if not identity.complete:
        print("A complete delivery identity is required", file=sys.stderr)
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
