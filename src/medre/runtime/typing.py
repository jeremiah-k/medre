"""Own optional transient activity routing without durable message authority."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import time

from medre.core.contracts.typing import (
    TypingActivity,
    TypingSignal,
    TypingSnapshot,
    TypingSender,
    TypingTarget,
    TypingUpdate,
)
from medre.core.policies.route_policy import evaluate_route_policy
from medre.core.routing.router import Router
from medre.core.supervision.typing import TypingLeasePool

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PolicySource:
    """Supply the shared allowlist evaluator without fabricating a canonical event."""

    source_adapter: str
    source_transport_id: str
    source_channel_id: str


class TypingRelay:
    """Route source-bound activity to bounded, coalesced native sender ports.

    Configured routes remain authoritative on admission and before every send.
    A single owned worker expires activity; at most eight native calls run at
    once, each with a two-second deadline. Incoming activity never queues a
    message or awaits a transport. Shutdown clears before adapters close.
    """

    def __init__(self, router: Router, ports: Mapping[str, TypingSender]) -> None:
        """Retain the live router and native ports, initially refusing activity."""
        self._router = router
        self._ports = dict(ports)
        self._pool = TypingLeasePool()
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._stopping: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(8)
        self.accepted = 0
        self.refused = 0
        self.send_failures = 0

    def _targets(
        self, adapter: str, channel: str, sender: str
    ) -> tuple[TypingTarget, ...]:
        """Reuse enabled route grants and every static sender/channel allowlist."""
        targets = set()
        source = _PolicySource(adapter, sender, channel)
        for route in self._router.typing_routes(adapter, channel):
            for target in route.targets:
                if (
                    target.adapter not in self._ports
                    or target.channel is None
                    or target.destination is not None
                ):
                    continue
                if (
                    route.policy is not None
                    and not evaluate_route_policy(route.policy, source, target).allowed
                ):
                    continue
                targets.add(TypingTarget(target.adapter, target.channel))
        return tuple(
            sorted(targets, key=lambda target: (target.adapter_id, target.channel_id))
        )

    def publisher(self, adapter: str) -> Callable[[TypingSignal], Awaitable[bool]]:
        """Bind immutable source adapter identity without exposing routing authority."""

        async def publish(activity: TypingSignal) -> bool:
            """Return local admission only; neither persist nor acknowledge native sends."""
            if (
                not self._accepting
                or self._task is None
                or self._task.done()
                or adapter not in self._ports
            ):
                self.refused += 1
                return False
            if isinstance(activity, TypingSnapshot):
                self._pool.remove_absent(
                    adapter, activity.channel_id, frozenset(activity.sender_ids)
                )
                admitted = True
                for sender in activity.sender_ids:
                    targets = self._targets(adapter, activity.channel_id, sender)
                    accepted = self._pool.observe(
                        adapter,
                        TypingActivity(
                            activity.channel_id, sender, True, activity.duration_seconds
                        ),
                        targets,
                    )
                    admitted = admitted and accepted and bool(targets)
                self.accepted += int(admitted)
                self.refused += int(not admitted)
                self._changed.set()
                return admitted
            targets = (
                self._targets(adapter, activity.channel_id, activity.sender_id)
                if activity.typing
                else ()
            )
            changed = self._pool.observe(adapter, activity, targets)
            admitted = changed and (not activity.typing or bool(targets))
            self.accepted += int(admitted)
            self.refused += int(not admitted)
            if changed:
                self._changed.set()
            return admitted

        return publish

    @property
    def adapter_ids(self) -> frozenset[str]:
        """Return the configured native port identities without exposing handles."""
        return frozenset(self._ports)

    def start(self, ready_ids: frozenset[str]) -> None:
        """Enable source-bound publishers after all adapter starts have completed."""
        if self._task is not None or self._stopping is not None:
            raise RuntimeError("Typing relay requires a fresh runtime after start/stop")
        self._ports = {
            name: port for name, port in self._ports.items() if name in ready_ids
        }
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name="medre-typing")
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        """Consume worker failures once and refuse activity without leaking details."""
        if not task.cancelled() and (error := task.exception()) is not None:
            self._accepting = False
            _logger.error("Typing worker stopped error_type=%s", type(error).__name__)

    def _reconcile(self) -> None:
        """Revoke removed/disabled route permissions without renewing any actor lease."""
        grants: dict[tuple[str, str, str], frozenset[TypingTarget]] = {}

        def allowed(
            adapter: str, channel: str, sender: str, target: TypingTarget
        ) -> bool:
            """Resolve each actor's grants once per reconciliation, not per target."""
            key = (adapter, channel, sender)
            if key not in grants:
                grants[key] = frozenset(self._targets(adapter, channel, sender))
            return target in grants[key]

        self._pool.retain(allowed)

    async def _send(self, update: TypingUpdate, *, clearing: bool = False) -> None:
        """Send fresh desired state with bounded concurrency and secret-free failures."""
        async with self._semaphore:
            if not clearing:
                self._reconcile()
                desired = self._pool.desired(update.target)
                if not update.typing and desired.typing:
                    # poll() forgot this clear's emission. A fresh positive must
                    # come from the next poll so shutdown can track and clear it.
                    return
                update = desired
            try:
                async with asyncio.timeout(2):
                    await self._ports[update.target.adapter_id].send_typing(update)
            except Exception as exc:
                self.send_failures += 1
                _logger.debug(
                    "Typing send failed adapter=%s error_type=%s",
                    update.target.adapter_id,
                    type(exc).__name__,
                )

    async def _run(self) -> None:
        """Own one deadline-driven loop; never retain an incoming transport backlog."""
        while True:
            self._changed.clear()
            self._reconcile()
            updates = self._pool.poll()
            if updates:
                await asyncio.gather(*(self._send(update) for update in updates))
                # A publish during a native await must not lose its wakeup.
                if self._changed.is_set():
                    continue
            deadline = self._pool.next_deadline
            # Recheck grants at least once per second while activity exists.
            delay = (
                None
                if deadline is None
                else min(1.0, max(0.0, deadline - time.monotonic()))
            )
            try:
                if delay is None:
                    await self._changed.wait()
                else:
                    await asyncio.wait_for(self._changed.wait(), delay)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        """Await one shielded cleanup; cancellation never abandons owned native calls."""
        if self._stopping is None:
            self._accepting = False
            self._stopping = asyncio.create_task(self._stop(), name="medre-typing-stop")
        await asyncio.shield(self._stopping)

    async def _stop(self) -> None:
        """Refuse publication, reap owned sends, then best-effort clear live ports."""
        self._accepting = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        clears = self._pool.reset()
        if clears:
            await asyncio.gather(
                *(self._send(update, clearing=True) for update in clears)
            )
