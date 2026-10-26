"""Project native health reports without polling or granting delivery authority."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.adapter import AdapterInfo
from medre.core.lifecycle.states import AdapterState
from medre.core.supervision.health import (
    AdapterLiveHealth,
    LiveHealthSnapshot,
    normalize_adapter_health,
)
from medre.core.supervision.supervision import (
    classify_runtime_health,
    count_adapter_state_categories,
)


def observed_health(
    info: AdapterInfo,
    state: AdapterState,
    adapter: Any,
    *,
    adapter_id: str,
    observed_mono: float | None = None,
    observed_wall: str | None = None,
) -> AdapterLiveHealth:
    """Capture a completed observation for the runtime's bound adapter identity.

    A provider returning another adapter's identity is invalid evidence;
    reject it rather than borrowing that provider's readiness.
    """
    if info.adapter_id != adapter_id:
        raise ValueError("health sample must match its bound adapter")
    normalized = normalize_adapter_health(info, lifecycle_state=state, adapter=adapter)
    return AdapterLiveHealth(
        adapter_id=adapter_id,
        health=normalized["health"],
        adapter_state=state,
        fake_or_live=normalized["fake_or_live"],
        poll_timestamp_monotonic=(
            time.monotonic() if observed_mono is None else observed_mono
        ),
        poll_timestamp_wall=observed_wall or datetime.now(timezone.utc).isoformat(),
    )


def reported_health_snapshot(
    entries: Mapping[str, AdapterLiveHealth], poll_count: int
) -> LiveHealthSnapshot:
    """Aggregate retained observations; preserve each adapter's own observation time.

    Native reports do not count as manual polling cycles. They refresh only
    their own entry; other providers retain the timestamps of their observations.
    """
    states = [entry.adapter_state for entry in entries.values()]
    healthy, degraded, failed, transitional = count_adapter_state_categories(states)
    return LiveHealthSnapshot(
        runtime_health=classify_runtime_health(states).value,
        adapter_summary={
            "healthy": healthy,
            "degraded": degraded,
            "failed": failed,
            "transitional": transitional,
            "total": len(states),
        },
        adapters=dict(sorted(entries.items())),
        poll_timestamp_monotonic=time.monotonic(),
        poll_timestamp_wall=datetime.now(timezone.utc).isoformat(),
        poll_count=poll_count,
        source="adapter_notification",
    )
