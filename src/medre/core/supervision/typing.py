"""Bound transient typing leases and coalesce actors into relay indicators.

The pool performs no I/O, starts no tasks, and stores no durable evidence.
Call it serially with a monotonic clock; transport sends consume poll() results
as best-effort desired state, independently of message delivery authority.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import time

from medre.core.contracts.typing import (
    MAX_TYPING_SECONDS,
    TypingActivity,
    TypingTarget,
    TypingUpdate,
    _identifier,
)

MIN_REFRESH_SECONDS = 0.5

_Source = tuple[str, str, str]


def _target_key(target: TypingTarget) -> tuple[str, str]:
    """Order destinations independently of source or configuration iteration."""
    return target.adapter_id, target.channel_id


@dataclass(frozen=True, slots=True)
class _Lease:
    """Keep one source's deadline and its immutable authorized destinations."""

    expires_at: float
    targets: frozenset[TypingTarget]


@dataclass(frozen=True, slots=True)
class _Emission:
    """Remember a generated refresh deadline without claiming native delivery."""

    expires_at: float
    refresh_at: float


class TypingLeasePool:
    """Aggregate finite actor leases within independent source/destination bounds.

    observe() accepts only pre-authorized concrete destinations. poll() returns
    coalesced state transitions and periodic refreshes; it never sends, retries,
    persists, or treats a generated update as an acknowledged delivery. reset()
    forgets every source and returns best-effort clears for emitted indicators.

    Keep at most max_sources actor leases and max_targets active destinations.
    At most max_targets emitted destinations can await a clear, independently
    of active targets. No background task, timer per actor, history, or backlog
    is allocated. The caller polls by next_deadline and on accepted changes.
    """

    def __init__(
        self,
        *,
        max_sources: int = 1024,
        max_targets: int = 64,
        refresh_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Set finite bounds and an injectable, nondecreasing monotonic clock."""
        if any(
            type(value) is not int or value < 1 for value in (max_sources, max_targets)
        ):
            raise ValueError("Typing capacity limits must be positive integers")
        if (
            isinstance(refresh_seconds, bool)
            or not isinstance(refresh_seconds, (int, float))
            or not math.isfinite(refresh_seconds)
            or refresh_seconds < MIN_REFRESH_SECONDS
        ):
            raise ValueError("refresh_seconds must be finite and at least 0.5")
        self._max_sources = max_sources
        self._max_targets = max_targets
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._last_time: float | None = None
        self._leases: dict[_Source, _Lease] = {}
        self._targets: dict[TypingTarget, set[_Source]] = {}
        self._emitted: dict[TypingTarget, _Emission] = {}

    def _now(self) -> float:
        """Reject invalid or reversed clocks before mutating any lease state."""
        now = self._clock()
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or (self._last_time is not None and now < self._last_time)
        ):
            raise ValueError("Typing clock must be finite and nondecreasing")
        self._last_time = now
        return now

    def _remove(self, key: _Source) -> None:
        """Release one actor without clearing another actor's destinations."""
        lease = self._leases.pop(key)
        for target in lease.targets:
            sources = self._targets[target]
            sources.remove(key)
            if not sources:
                del self._targets[target]

    def _expire(self, now: float) -> None:
        """Remove expired leases; emitted destinations await poll() cleanup."""
        for key, lease in tuple(self._leases.items()):
            if lease.expires_at <= now:
                self._remove(key)

    def observe(
        self,
        source_adapter: str,
        activity: TypingActivity,
        targets: tuple[TypingTarget, ...],
    ) -> bool:
        """Admit or renew an actor lease, returning False on capacity refusal.

        The caller binds source identity and supplies authorized destinations;
        no route discovery, wildcard expansion, policy bypass, or sending occurs.
        A sender stop always releases its existing lease, even at capacity.
        Self-echo destinations are excluded. Target changes replace this actor's
        grants atomically; refusal retains its prior unexpired lease.
        """
        _identifier(source_adapter, "source_adapter")
        if not isinstance(activity, TypingActivity):
            raise ValueError("activity must be a TypingActivity")
        if not isinstance(targets, tuple):
            raise ValueError("targets must be a tuple of TypingTarget values")
        selected_targets: set[TypingTarget] = set()
        if activity.typing:
            for target in targets:
                if not isinstance(target, TypingTarget):
                    raise ValueError("targets must be a tuple of TypingTarget values")
                if (target.adapter_id, target.channel_id) == (
                    source_adapter,
                    activity.channel_id,
                ):
                    continue
                selected_targets.add(target)
                if len(selected_targets) > self._max_targets:
                    return False
        now = self._now()
        self._expire(now)
        key = (source_adapter, activity.channel_id, activity.sender_id)
        if not activity.typing:
            if key in self._leases:
                self._remove(key)
            return True
        selected = frozenset(selected_targets)
        existing = self._leases.get(key)
        if not selected:
            if existing is not None:
                self._remove(key)
            return True
        if existing is None and len(self._leases) >= self._max_sources:
            return False
        retained = set(self._targets)
        if existing is not None:
            retained.difference_update(
                t for t in existing.targets if self._targets[t] == {key}
            )
        if len(retained | selected) > self._max_targets:
            return False
        expires = now + activity.duration_seconds
        if math.isfinite(expires) and expires - now > activity.duration_seconds:
            expires = math.nextafter(expires, now)
        if not math.isfinite(expires) or expires <= now:
            raise ValueError("Typing clock cannot represent a finite lease deadline")
        if existing is not None:
            self._remove(key)
        self._leases[key] = _Lease(expires, selected)
        for target in selected:
            self._targets.setdefault(target, set()).add(key)
        return True

    def poll(self) -> tuple[TypingUpdate, ...]:
        """Expire leases and return bounded clears, changes, and due refreshes.

        Refresh at half the emitted lifetime or refresh_seconds, whichever is
        sooner, with a half-second floor and expiry as the final deadline.
        Shorter aggregate lifetimes refresh immediately. Order targets
        deterministically; clear absent destinations before emitting positives.
        """
        now = self._now()
        self._expire(now)
        updates = []
        for target in sorted(
            self._emitted.keys() - self._targets.keys(), key=_target_key
        ):
            updates.append(TypingUpdate(target, 0.0))
            del self._emitted[target]
        for target in sorted(self._targets, key=_target_key):
            expires = max(self._leases[key].expires_at for key in self._targets[target])
            prior = self._emitted.get(target)
            if prior is None or now >= prior.refresh_at or expires < prior.expires_at:
                duration = min(MAX_TYPING_SECONDS, expires - now)
                updates.append(TypingUpdate(target, duration))
                refresh = min(
                    expires,
                    now
                    + max(
                        MIN_REFRESH_SECONDS, min(self._refresh_seconds, duration / 2)
                    ),
                )
                if refresh <= now:
                    refresh = expires
                self._emitted[target] = _Emission(expires, refresh)
        return tuple(updates)

    @property
    def next_deadline(self) -> float | None:
        """Return the next absolute lease/refresh deadline, or None when idle.

        Accepted updates require an immediate poll independent of this deadline.
        This property reads existing state without consulting or advancing time.
        """
        times = [lease.expires_at for lease in self._leases.values()]
        times.extend(emission.refresh_at for emission in self._emitted.values())
        return min(times) if times else None

    def remove_absent(
        self, source_adapter: str, channel: str, senders: frozenset[str]
    ) -> None:
        """Release omitted actors from one source-scoped native replacement list."""
        self._expire(self._now())
        for key in tuple(self._leases):
            if key[0] == source_adapter and key[1] == channel and key[2] not in senders:
                self._remove(key)

    def retain(self, allowed: Callable[[str, str, str, TypingTarget], bool]) -> None:
        """Withdraw grants without extending deadlines or inventing fresh activity."""
        self._expire(self._now())
        for key, lease in tuple(self._leases.items()):
            targets = frozenset(
                target for target in lease.targets if allowed(*key, target)
            )
            if targets != lease.targets:
                self._remove(key)
                if targets:
                    self._leases[key] = _Lease(lease.expires_at, targets)
                    for target in targets:
                        self._targets.setdefault(target, set()).add(key)

    def desired(self, target: TypingTarget) -> TypingUpdate:
        """Read fresh aggregate state immediately before a native send, without renewal."""
        now = self._now()
        self._expire(now)
        sources = self._targets.get(target, set())
        seconds = max(
            (self._leases[key].expires_at - now for key in sources), default=0.0
        )
        return TypingUpdate(target, min(MAX_TYPING_SECONDS, seconds))

    def reset(self) -> tuple[TypingUpdate, ...]:
        """Forget all actors and return one clear per previously emitted target.

        Use on shutdown or permission withdrawal. Failure to send a clear is
        not retried here; adapters must document native expiration limits.
        """
        updates = tuple(
            TypingUpdate(target, 0.0)
            for target in sorted(self._emitted, key=_target_key)
        )
        self._leases.clear()
        self._targets.clear()
        self._emitted.clear()
        return updates
