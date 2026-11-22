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

Horizons are per obligation
---------------------------
Each deferred obligation carries its own deferral horizon, keyed by the
durable outbox identity, measured on a process-local monotonic clock from
that obligation's first blocked admission. Later-arriving work in a
continuously unhealthy lane starts a fresh horizon of its own instead of
inheriting an adapter-wide episode, and one obligation's horizon expiring
re-attempts that obligation without ending the deferral protection of
siblings whose horizons are still running: their claims are re-armed by
admission (silently, without new deferral records) until their own horizons
fire. A healthy observation or an observed drained lane clears every
horizon recorded for that adapter. Deferral horizons are scheduling state
only: they never alter storage-authoritative attempt numbering, and losing
them to a restart is safe because the next blocked admission starts a fresh
episode with health re-observed.

Once any horizon on a lane has expired, the lane leaves claim exclusion for
the rest of the episode (until health or a drained-lane observation clears
it): normal claim rules resume, and the admission gate keeps obligations
inside their own horizons deferred. Held work that was never admitted by
this process (rows deferred before a restart) anchors a lane-level fallback
horizon so its lane still reopens for reconciliation. An adapter that never
recovers therefore cannot strand obligations silently.
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

#: Safety net: maximum wall-clock time one obligation may stay held in
#: health deferral before normal delivery/retry rules resume for it.
#:
#: An obligation whose adapter never recovers MUST NOT be stranded silently;
#: after this horizon the admission gate stops blocking that obligation and
#: the worker stops skipping its lane, deliveries are attempted again, and
#: normal retry/retention rules (attempt burn, backoff, dead-letter) resume.
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

    Horizon state is per obligation. Admission for an obligation whose own
    horizon is unexpired blocks; an obligation whose horizon expired is
    allowed through (its normal retry/retention rules resume). Claim
    exclusion keeps a lane closed while every known horizon on it is
    unexpired and none has fired yet; the first fired horizon opens the lane
    for the rest of the episode.
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
        # adapter -> obligation (outbox id) -> monotonic start of that
        # obligation's deferral episode. Bounded by the durable outbox
        # working set: entries are consumed on horizon expiry and cleared
        # with the adapter on healthy or drained-lane observations.
        self._obligation_started: dict[str, dict[str, float]] = {}
        # adapter -> obligations whose horizon has fired. They stay admitted
        # through the gate (normal rules) until the adapter state is
        # cleared; the set holds the lane open (claim exclusion off) for the
        # rest of the episode and makes the expiry line log exactly once.
        self._obligation_expired: dict[str, set[str]] = {}
        # Fallback episode anchor for held work this process never admitted
        # (rows deferred before a restart). Preserves the held-work episode
        # semantics for lanes whose obligations carry no known horizon.
        self._lane_started: dict[str, float] = {}
        self._lane_expired: set[str] = set()

    def blocking_health(
        self, adapter_id: str, *, obligation_id: str | None = None
    ) -> str | None:
        """Return the blocking health string for *adapter_id*, else ``None``.

        ``None`` means delivery may proceed: the adapter is healthy, has no
        observation, its obligation horizon expired (per-obligation safety
        net — normal retry/retention rules resume for that obligation), or
        the lane fallback horizon expired (lane safety net).

        *obligation_id* is the durable outbox identity of the work under
        admission. A fresh obligation observed while its lane is blocked
        starts its own horizon; obligations without an identity (explicit
        replays refused before allocation, dispatches with no durable row)
        are gated at lane level only.
        """
        health = self._health_fn(adapter_id)
        # Inline the delivery-allowing test so the not-healthy string is
        # narrowed for the rest of the admission decision; the shared
        # predicate stays in :func:`health_allows_delivery` for callers that
        # only need the boolean.
        if health is None or health == HEALTHY:
            self.observe(adapter_id, health)
            return None
        now = monotonic()
        self._expire_due(adapter_id, now)
        if obligation_id is None:
            return self._blocking_health_lane(adapter_id, now, health)
        clocks = self._obligation_started.get(adapter_id, {})
        if obligation_id in self._obligation_expired.get(adapter_id, ()):
            return None
        started = clocks.get(obligation_id)
        if started is not None:
            if (now - started) < self._max_horizon_seconds:
                return health
            return None
        # Fresh obligation. Inside the terminal lane safety net no new
        # horizon is anchored: normal rules already govern the lane.
        if adapter_id in self._lane_expired:
            return None
        self._obligation_started.setdefault(adapter_id, {})[obligation_id] = now
        return health

    def _blocking_health_lane(
        self, adapter_id: str, now: float, health: str
    ) -> str | None:
        """Gate work with no durable obligation identity at lane level.

        Blocked while any lane horizon is unexpired. Once a lane horizon —
        the fallback, or any consumed obligation horizon — has fired, the
        lane is in safety-net mode and identity-less work follows normal
        rules. A fresh lane anchors the fallback on first block.
        """
        if self._lane_alive(adapter_id, now):
            return health
        if adapter_id not in self._lane_expired and not self._obligation_expired.get(
            adapter_id
        ):
            self._anchor_lane_if_unknown(adapter_id, now)
            return health
        return None

    def holds_obligation(self, adapter_id: str, obligation_id: str) -> bool:
        """Return whether *obligation_id* is currently inside a deferral episode.

        Read-only helper so the admission path can distinguish an
        obligation's first deferral (one INFO line, one ``delivery_deferred``
        diagnostic) from a re-arm of the same episode (silent rescheduling).
        """
        return obligation_id in self._obligation_started.get(adapter_id, {})

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
        """Exclude unhealthy lanes whose deferral horizons have not fired.

        An observed empty lane ends every recorded horizon for that adapter
        even during the same outage. A lane with only unexpired horizons
        stays excluded so held obligations are not claim-churned; the first
        horizon to expire opens the lane for the rest of the episode so
        normal retry/retention rules can act on it, while admission keeps
        the still-held siblings deferred.
        """
        held_set = set(held)
        excluded: list[str] = []
        for adapter_id in sorted(set(universe)):
            if adapter_id in held_set:
                current = self._health_fn(adapter_id)
                if health_allows_delivery(current):
                    self.observe(adapter_id, current)
                    continue
                now = monotonic()
                self._expire_due(adapter_id, now)
                self._anchor_lane_if_unknown(adapter_id, now)
                if self._lane_alive(adapter_id, now) and not self._lane_fired(
                    adapter_id
                ):
                    excluded.append(adapter_id)
            else:
                self._clear_adapter(adapter_id)
                # Still exclude an idle unhealthy adapter so a row inserted
                # after the backlog read cannot slip through the claim query.
                if not health_allows_delivery(self._health_fn(adapter_id)):
                    excluded.append(adapter_id)
        return tuple(excluded)

    def observe(self, adapter_id: str, health: str | None) -> None:
        """Apply one runtime health transition for *adapter_id*.

        A ``healthy`` observation clears every horizon recorded for the
        adapter — per-obligation clocks, fired horizons, and the lane
        fallback — so later degradation starts fresh episodes.
        """
        if health_allows_delivery(health):
            self._clear_adapter(adapter_id)

    # -- Horizon bookkeeping ------------------------------------------------

    def _expire_due(self, adapter_id: str, now: float) -> None:
        """Consume horizons that reached *now*, logging each expiry once."""
        clocks = self._obligation_started.get(adapter_id)
        if clocks:
            fired = self._obligation_expired.setdefault(adapter_id, set())
            for obligation_id in sorted(clocks):
                started = clocks[obligation_id]
                if (now - started) < self._max_horizon_seconds:
                    continue
                del clocks[obligation_id]
                if obligation_id not in fired:
                    fired.add(obligation_id)
                    self._log.info(
                        "Deferral horizon expired for adapter=%s obligation=%s; "
                        "resuming normal delivery and retry rules for this "
                        "obligation",
                        adapter_id,
                        obligation_id,
                    )
        lane_started = self._lane_started.get(adapter_id)
        if lane_started is not None:
            if (now - lane_started) < self._max_horizon_seconds:
                return
            del self._lane_started[adapter_id]
            if adapter_id not in self._lane_expired:
                self._lane_expired.add(adapter_id)
                self._log.info(
                    "Deferral horizon expired for adapter=%s; resuming normal "
                    "delivery and retry rules for its obligations",
                    adapter_id,
                )

    def _anchor_lane_if_unknown(self, adapter_id: str, now: float) -> None:
        """Anchor the lane fallback when held work carries no known horizon.

        Lanes whose obligations were admitted by this process carry horizon
        state of their own (running or already fired) and never fall back
        to a lane anchor: their expiries must open the lane instead of
        re-arming a fresh episode behind the obligations' back.
        """
        if adapter_id in self._lane_expired:
            return
        if adapter_id in self._lane_started:
            return
        if self._obligation_started.get(adapter_id):
            return
        if self._obligation_expired.get(adapter_id):
            return
        self._lane_started[adapter_id] = now

    def _lane_alive(self, adapter_id: str, now: float) -> bool:
        """Return whether any horizon anchor for *adapter_id* is unexpired."""
        clocks = self._obligation_started.get(adapter_id)
        if clocks:
            horizon = self._max_horizon_seconds
            if any((now - started) < horizon for started in clocks.values()):
                return True
        lane_started = self._lane_started.get(adapter_id)
        return lane_started is not None and (
            (now - lane_started) < self._max_horizon_seconds
        )

    def _lane_fired(self, adapter_id: str) -> bool:
        """Return whether any horizon on this lane has already expired."""
        return adapter_id in self._lane_expired or bool(
            self._obligation_expired.get(adapter_id)
        )

    def _clear_adapter(self, adapter_id: str) -> None:
        """Drop every horizon recorded for *adapter_id*."""
        self._obligation_started.pop(adapter_id, None)
        self._obligation_expired.pop(adapter_id, None)
        self._lane_started.pop(adapter_id, None)
        self._lane_expired.discard(adapter_id)
