"""Health-aware delivery admission policy.

The runtime owns health observations (:class:`~medre.runtime.app.MedreApp`
retains per-adapter native health reports); the core pipeline owns the
delivery policy over them. To keep layering intact the runtime injects a
read-only lookup (:data:`AdapterHealthFn`) into the pipeline and the retry
worker; core never imports the runtime.

Policy
------
A target adapter whose current health is not ``healthy`` MUST NOT be handed
new native work: an adapter known degraded (for example a BLE link in
reconnect backoff) would turn every dispatch into an expected transient
failure. Instead the obligation is deferred — ``deliver()`` is not invoked,
one INFO line is logged, a ``delivery_deferred`` diagnostic is recorded, and
the durable outbox row's ``next_attempt_at`` is pushed out by a bounded
horizon without consuming an attempt. When the adapter's health returns to
``healthy``, the runtime notifies the retry worker, which redispatches the
held obligations.

A missing observation (``None``) does not block delivery: adapters that
never report health keep today's behavior, and adapters that never completed
startup are already gated by the retry worker's availability allow-list.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from time import monotonic
from typing import TypeAlias

#: Reads the runtime's current normalized health string for one adapter.
#:
#: Returns one of the :data:`~medre.core.supervision.health.VALID_HEALTH_STRINGS`
#: values (``"healthy"``, ``"degraded"``, ``"failed"``, ``"unknown"``,
#: ``"starting"``, ``"stopping"``) or ``None`` when the runtime holds no
#: observation for the adapter.
AdapterHealthFn: TypeAlias = Callable[[str], str | None]

#: Health string of a fully operational adapter.
HEALTHY: str = "healthy"

#: How far a deferral pushes an obligation's ``next_attempt_at`` forward.
#:
#: The push is deliberately short: the obligation stays durable and the
#: retry worker skips it while the adapter remains unhealthy, so this value
#: only bounds how long a recovery can go unnoticed once the runtime's
#: health-change notification has fired.
DEFERRAL_NEXT_ATTEMPT_PUSH_SECONDS: float = 30.0

#: Safety net: maximum wall-clock time one adapter may hold obligations in
#: health deferral before normal delivery/retry rules resume.
#:
#: An adapter that never recovers MUST NOT strand obligations silently;
#: after this horizon the admission gate stops blocking and the worker stops
#: skipping the adapter, deliveries are attempted again, and normal
#: retry/retention rules (attempt burn, backoff, dead-letter) resume.
MAX_DEFERRAL_HORIZON_SECONDS: float = 3600.0


def health_allows_delivery(health: str | None) -> bool:
    """Return whether *health* permits a new native delivery attempt.

    ``None`` (no observation) allows delivery; only ``"healthy"`` among
    observed states allows delivery. Degraded, failed, transitional
    (``starting`` / ``stopping``), and unknown-adapter-state observations
    all block.
    """
    return health is None or health == HEALTHY


class AdapterHealthGate:
    """One process-wide health admission policy shared by pipeline and worker.

    The runtime constructs the gate around its current-health lookup and
    injects the same instance into the delivery pipeline (admission) and the
    retry worker (claim exclusion and recovery notification), so both see
    one consistent deferral-horizon state.

    Within the maximum deferral horizon, an adapter observed as not healthy
    blocks new deliveries and is excluded from claims. After the horizon
    expires for an adapter, the gate stops blocking it — normal
    claim/retry/retention rules resume — until a ``healthy`` observation
    clears the expiry marker or a backlog observation shows that its lane drained.
    """

    def __init__(
        self,
        health_fn: AdapterHealthFn,
        *,
        max_horizon_seconds: float = MAX_DEFERRAL_HORIZON_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._health_fn = health_fn
        self._max_horizon_seconds = max_horizon_seconds
        self._log = logger or logging.getLogger(__name__)
        self._deferral_started: dict[str, float] = {}
        self._horizon_expired: set[str] = set()

    def blocking_health(self, adapter_id: str) -> str | None:
        """Return the blocking health string for *adapter_id*, else ``None``.

        ``None`` means delivery may proceed: the adapter is healthy, has no
        observation, or has exhausted the maximum deferral horizon (safety
        net — normal retry/retention rules resume).
        """
        health = self._health_fn(adapter_id)
        if health_allows_delivery(health):
            self.observe(adapter_id, health)
            return None
        if adapter_id in self._horizon_expired:
            return None
        now = monotonic()
        started = self._deferral_started.setdefault(adapter_id, now)
        if (now - started) >= self._max_horizon_seconds:
            self._mark_horizon_expired(adapter_id)
            self._deferral_started.pop(adapter_id, None)
            return None
        return health

    def unhealthy_adapters(self, universe: Iterable[str]) -> tuple[str, ...]:
        """Observe current health without starting clocks on idle adapters."""
        return tuple(
            sorted(
                {
                    aid
                    for aid in universe
                    if not health_allows_delivery(self._health_fn(aid))
                }
            )
        )

    def excluded_adapters(
        self,
        universe: Iterable[str],
        *,
        held: Iterable[str],
    ) -> tuple[str, ...]:
        """Exclude unhealthy lanes; age only a positively observed backlog.

        An observed empty lane ends the prior deferral episode even during
        the same outage. A continuously nonempty lane keeps its expired
        horizon so normal retry/retention rules can drain it.
        """
        held_set = set(held)
        excluded: list[str] = []
        for adapter_id in sorted(set(universe)):
            if adapter_id in held_set:
                if self.blocking_health(adapter_id) is not None:
                    excluded.append(adapter_id)
            else:
                self._deferral_started.pop(adapter_id, None)
                self._horizon_expired.discard(adapter_id)
                # Still exclude an idle unhealthy adapter so a row inserted
                # after the backlog read cannot slip through the claim query.
                if not health_allows_delivery(self._health_fn(adapter_id)):
                    excluded.append(adapter_id)
        return tuple(excluded)

    def observe(self, adapter_id: str, health: str | None) -> None:
        """Apply one runtime health transition for *adapter_id*.

        A ``healthy`` observation clears both the deferral clock and the
        horizon-expiry marker so the adapter returns to full gating on any
        later degradation.
        """
        if health_allows_delivery(health):
            self._deferral_started.pop(adapter_id, None)
            self._horizon_expired.discard(adapter_id)

    def _mark_horizon_expired(self, adapter_id: str) -> None:
        if adapter_id not in self._horizon_expired:
            self._horizon_expired.add(adapter_id)
            self._log.info(
                "Deferral horizon expired for adapter=%s; resuming normal "
                "delivery and retry rules for its obligations",
                adapter_id,
            )
