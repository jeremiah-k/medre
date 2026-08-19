"""Task-local structured correlation context for pipeline observability."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class CorrelationContext:
    """Identifiers that join one ingress event to delivery evidence."""

    trace_id: str | None = None
    event_id: str | None = None
    conversation_id: str | None = None
    route_id: str | None = None
    delivery_plan_id: str | None = None
    target_adapter: str | None = None
    outbox_id: str | None = None
    receipt_id: str | None = None
    source: str | None = None
    replay_run_id: str | None = None


_CONTEXT: ContextVar[CorrelationContext | None] = ContextVar(
    "medre_correlation_context", default=None
)


def current_correlation() -> CorrelationContext:
    """Return the immutable correlation context for the current task."""
    ctx = _CONTEXT.get()
    return ctx if ctx is not None else CorrelationContext()


def correlation_fields() -> dict[str, str]:
    """Return non-empty correlation fields suitable for structured logs."""
    return {
        key: value
        for key, value in asdict(current_correlation()).items()
        if isinstance(value, str) and value
    }


@contextmanager
def correlation_scope(**fields: str | None) -> Iterator[CorrelationContext]:
    """Temporarily merge correlation identifiers into the current task."""
    unknown = set(fields) - set(CorrelationContext.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown correlation fields: {sorted(unknown)}")
    token = _CONTEXT.set(replace(current_correlation(), **fields))
    try:
        yield _CONTEXT.get()
    finally:
        _CONTEXT.reset(token)
