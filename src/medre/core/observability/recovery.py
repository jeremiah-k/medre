"""Read-only recovery guidance from the shared delivery authority snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    ResolvedDeliverySnapshot,
    effective_generation,
)
from medre.core.observability.classification import (
    failure_category,
    recommended_commands,
)

RecoveryDisposition = Literal[
    "pending",
    "in_progress",
    "awaiting_feedback",
    "retry_scheduled",
    "completed",
    "cancelled",
    "inspect_native",
    "preview_replay",
    "needs_operator_action",
]


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """A description of one current delivery, never permission to resend it."""

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    route_id: str | None
    outbox_id: str | None
    attempt_number: int | None
    outbox_status: str | None
    receipt_id: str | None
    receipt_status: str | None
    disposition: RecoveryDisposition
    reason: str
    commands: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the schema-facing action with JSON arrays, not tuples."""
        result = asdict(self)
        result["commands"] = list(self.commands)
        return result


def delivery_guidance(
    authority: DeliveryAuthorityResolver[Any], event_id: str, *, storage_path: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Share current-action commands between event runbooks and evidence.

    Return action dictionaries in the resolver's order and a command list
    deduplicated in first-seen order, excluding completed and cancelled actions
    from command aggregation. If no commands remain, return a read-only event
    inspection command.

    The caller supplies authority scoped to the event; ``event_id`` is used only
    for the fallback command. ``storage_path`` is embedded in inspection commands.
    """
    actions = [
        recovery_action(snapshot, storage_path=storage_path).to_dict()
        for snapshot in authority.ordered_snapshots()
    ]
    commands = list(
        dict.fromkeys(
            command
            for action in actions
            if action["disposition"] not in {"completed", "cancelled"}
            for command in action["commands"]
        )
    )
    if not commands:
        commands = recommended_commands("unknown", event_id, storage_path=storage_path)
    return actions, commands


def recovery_action(
    snapshot: ResolvedDeliverySnapshot[Any], *, storage_path: str
) -> RecoveryAction:
    """Explain mutable ownership before considering a historical failure remedy.

    A current outbox generation without a receipt is represented explicitly.
    Stale receipts cannot authorize replay or turn uncertain work into failure.
    Without an outbox, the authoritative receipt supplies the delivery state.
    Return an action containing that state, its disposition and reason, and
    inspection commands or an adapter/route-scoped dry-run preview. Replay
    filters cannot isolate individual channels. Retry scheduling reflects stored
    deadline presence, without checking whether the deadline has passed.

    ``storage_path`` is embedded in inspection commands, not opened here.
    Commands remain suggestions; this function performs no native or storage I/O.
    """
    identity = snapshot.identity
    outbox = snapshot.current_outbox
    receipt = (
        snapshot.current_receipt
        if outbox is not None
        else snapshot.authoritative_receipt
    )
    status = outbox.status if outbox is not None else None
    receipt_status = receipt.status if receipt is not None else None
    route_id = (
        getattr(outbox, "route_id", None) or getattr(receipt, "route_id", None) or None
    )
    category = failure_category(getattr(receipt, "failure_kind", None))
    disposition: RecoveryDisposition = "needs_operator_action"
    reason = "No settled failure remedy is established by the current evidence."
    if status == "pending":
        disposition, reason = (
            "pending",
            "Durable work awaits dispatch; do not create another send.",
        )
    elif status == "in_progress":
        disposition, reason = (
            "in_progress",
            "An attempt owns dispatch; inspect its lease before intervention.",
        )
    elif status == "queued" or (status is None and receipt_status == "queued"):
        disposition, reason = (
            "awaiting_feedback",
            "Native queue admission awaits correlated feedback; acceptance is unresolved.",
        )
    elif status == "retry_wait":
        if getattr(outbox, "next_attempt_at", None):
            disposition, reason = (
                "retry_scheduled",
                "The retry worker owns the scheduled attempt; avoid a competing replay.",
            )
        else:
            reason = (
                "Retry wait has no scheduling deadline; inspect lifecycle consistency."
            )
    elif status == "sent" or (status is None and receipt_status == "sent"):
        disposition, reason = (
            "completed",
            "Local completion is recorded; this does not establish recipient delivery.",
        )
    elif status == "cancelled" or (status is None and receipt_status == "cancelled"):
        disposition, reason = (
            "cancelled",
            "Cancellation is terminal; a fresh send requires a deliberate operator decision.",
        )
    elif status == "abandoned" or (status is None and receipt_status == "abandoned"):
        disposition, reason = (
            "inspect_native",
            "Native acceptance may be uncertain; inspect the destination before any resend.",
        )
    elif status is None and getattr(receipt, "next_retry_at", None) is not None:
        disposition, reason = (
            "retry_scheduled",
            "A retained receipt schedules retry; inspect that owner before replay.",
        )
    elif receipt_status in {"failed", "dead_lettered"}:
        if status not in {None, "dead_lettered"}:
            reason = "Unrecognized current outbox state; historical failure is not replay authority."
        elif category == "retryable":
            disposition, reason = (
                "preview_replay",
                "Preview the failed destination with current configuration before any send.",
            )
        else:
            reason = "Inspect the persisted failure and correct its cause before considering replay."

    command_category = "unknown"
    if disposition == "preview_replay":
        command_category = "retryable"
    elif (
        disposition == "needs_operator_action"
        and status in {None, "dead_lettered"}
        and receipt_status in {"failed", "dead_lettered"}
    ):
        command_category = category

    commands = recommended_commands(
        command_category,
        identity.event_id,
        storage_path=storage_path,
        target_adapter=identity.target_adapter,
        route_id=route_id,
        include_send=False,
    )
    return RecoveryAction(
        event_id=identity.event_id,
        delivery_plan_id=identity.delivery_plan_id,
        target_adapter=identity.target_adapter,
        target_channel=identity.target_channel,
        route_id=route_id,
        outbox_id=getattr(outbox, "outbox_id", None),
        attempt_number=(
            effective_generation(outbox)
            if outbox is not None
            else getattr(receipt, "attempt_number", None)
        ),
        outbox_status=status,
        receipt_id=getattr(receipt, "receipt_id", None),
        receipt_status=receipt_status,
        disposition=disposition,
        reason=reason,
        commands=tuple(commands),
    )
