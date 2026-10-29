"""Failure-kind classification shared between recover and evidence.

The persisted ``failure_kind`` on a receipt is the machine authority; this
module maps it to recovery categories and recommended operator commands.
Nothing reconstructs a classification from error wording.

Public symbols
--------------
* :data:`RETRYABLE_KINDS` — frozenset of retryable failure-kind strings.
* :data:`PERMANENT_KINDS` — frozenset of permanent failure-kind strings.
* :data:`OPERATIONAL_KINDS` — frozenset of operational failure-kind strings.
* :func:`failure_category` — map a failure-kind to a recovery category.
* :func:`recommended_commands` — suggested next commands for a category.
"""

from __future__ import annotations

import shlex

__all__ = [
    "RETRYABLE_KINDS",
    "PERMANENT_KINDS",
    "OPERATIONAL_KINDS",
    "failure_category",
    "recommended_commands",
]

# ---------------------------------------------------------------------------
# Failure-kind categories for recovery classification.
# ---------------------------------------------------------------------------

RETRYABLE_KINDS: frozenset[str] = frozenset({"adapter_transient", "retry_exhausted"})
"""Failure kinds whose recovery remedy is another attempt.

``adapter_transient`` may still succeed on automatic retry;
``retry_exhausted`` means automatic retries are spent and manual replay
is the remaining remedy — the retryable command set recommends exactly
that. A dead-lettered receipt without a persisted kind stays
``unknown``: ``dead_lettered`` is lifecycle authority, not a
failure-kind signal."""

PERMANENT_KINDS: frozenset[str] = frozenset(
    {
        "adapter_permanent",
        "adapter_missing",
        "renderer_failure",
        "planner_failure",
        "loop_suppressed",
        "policy_suppressed",
        "outbox_not_owned",
    }
)
"""Failure kinds that are permanent and unlikely to succeed on retry."""

OPERATIONAL_KINDS: frozenset[str] = frozenset(
    {
        "capacity_rejection",
        "shutdown_rejection",
        "deadline_exceeded",
    }
)
"""Failure kinds caused by operational conditions (capacity, shutdown, deadline)."""


def failure_category(failure_kind: str | None) -> str:
    """Map a persisted failure-kind string to a recovery category.

    ``None`` maps to ``"unknown"``: receipts without a failure kind carry
    no machine-readable classification and nothing is reconstructed from
    error wording.

    Returns one of: ``"retryable"``, ``"permanent"``, ``"operational"``,
    ``"unknown"``.
    """
    if failure_kind in RETRYABLE_KINDS:
        return "retryable"
    if failure_kind in PERMANENT_KINDS:
        return "permanent"
    if failure_kind in OPERATIONAL_KINDS:
        return "operational"
    return "unknown"


def recommended_commands(
    category: str,
    event_id: str,
    *,
    storage_path: str | None = None,
    target_adapter: str | None = None,
    route_id: str | None = None,
    include_send: bool = True,
) -> list[str]:
    """Return recommended next commands for a failure category.

    Generated recommendations prefer ``medre inspect`` commands as the
    primary operator interface.  The ``medre trace event`` command remains
    available as a specialised / lower-level tool but is not the default
    recommendation.

    A nonempty *storage_path* adds ``--storage-path`` to every generated
    ``inspect`` command. When omitted or empty, operators must supply the path
    required by those commands. Unrecognized categories return only an event
    timeline inspection command.

    **Replay commands** (``medre replay``) do not include ``--config``
    in the generated string.  Operators may need to append
    ``--config PATH`` depending on deployment layout — the CLI
    auto-discovers the config file via XDG defaults, but explicit
    paths are required when the config lives outside the standard
    search locations.

    Nonempty destination and route filters narrow all generated replay commands
    to the adapter/route; they cannot isolate individual channels. For retryable
    failures, ``include_send=True`` adds a ``best_effort`` command after the
    ``dry_run`` preview. Set it false to return only inspection and preview
    commands. This function generates strings without executing any commands.
    Opaque IDs and paths are shell-quoted without changing their argument values.
    """

    def option(name: str, value: str) -> list[str]:
        """Return option arguments that preserve values beginning with a dash."""
        return [f"{name}={value}"] if value.startswith("-") else [name, value]

    sp = option("--storage-path", storage_path) if storage_path else []
    scope: list[str] = []
    if target_adapter:
        scope += option("--target-adapters", target_adapter)
    if route_id:
        scope += option("--route-ids", route_id)

    def inspect(option: str) -> str:
        """Return an event inspection command for the requested view flag."""
        # Opaque positional IDs beginning with '-' need argparse's sentinel,
        # in addition to shell quoting. Put options before that sentinel.
        args = ["medre", "inspect", "event"]
        if event_id.startswith("-"):
            args += [option, *sp, "--", event_id]
        else:
            args += [event_id, option, *sp]
        return shlex.join(args)

    def replay(mode: str) -> str:
        """Return an event replay command with any adapter and route filters."""
        event = option("--event", event_id)
        return shlex.join(["medre", "replay", "--mode", mode, *event, *scope])

    if category == "retryable":
        commands = [inspect("--recovery"), replay("dry_run")]
        if include_send:
            commands.append(replay("best_effort"))
        return commands
    if category == "permanent":
        event = option("--event", event_id)
        return [
            inspect("--evidence"),
            shlex.join(["medre", "inspect", "receipts", *event, *sp]),
        ]
    if category == "operational":
        return ["medre diagnostics", "medre config check", inspect("--timeline")]
    return [inspect("--timeline")]
