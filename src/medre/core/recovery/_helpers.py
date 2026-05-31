"""Shared duck-typed helpers for recovery classification and building."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

__all__ = ["_get", "_to_str", "_parse_as_utc"]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Duck-typed field access — ``dict.get`` or ``getattr``."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_str(val: Any) -> str:
    """Coerce to string safely."""
    if val is None:
        return ""
    return str(val)


def _parse_as_utc(ts: str) -> datetime:
    """Parse an ISO-8601 string and normalise to UTC.

    Naive timestamps (no ``tzinfo``) are treated as UTC.
    """
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
