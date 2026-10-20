"""Ordered, durable canonical plugin execution; discovery belongs to assembly.

Every invocation consumes an immutable event and produces a validated decision.
Only the host commits proposals and state. External plugin effects remain
at-least-once across uncommitted invocations; this is not a process sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.plugins.context import Plugin, PluginContext
from medre.core.contracts.plugins.decisions import Deny, Derive, Pass, Supersede
from medre.core.contracts.plugins.instances import (
    PluginInstanceConfig,
    missing_required_grants,
    unexpected_grants,
)
from medre.core.contracts.plugins.records import (
    MAX_PLUGIN_DEPTH,
    PluginExecutionKey,
    PluginExecutionRecord,
    PluginFailure,
    PluginStateSnapshot,
    canonical_json,
    configuration_digest,
    state_digest,
)
from medre.core.contracts.plugins.spec import PluginSpec, validate_plugin_spec
from medre.core.contracts.plugins.stages import PluginStage
from medre.core.contracts.plugins.validation import (
    PluginCapabilityError,
    validate_plugin_decision,
)
from medre.core.events import CanonicalEvent
from medre.core.storage.backend import (
    PluginExecutionConflictError,
    PluginStateConflictError,
    StorageBackend,
)

PLUGIN_HOST_CONTRACT_VERSION = 1
_LOG = logging.getLogger(__name__)
_METHODS = {
    PluginStage.ENRICH: "enrich",
    PluginStage.TRANSFORM: "transform",
    PluginStage.EVENT_POLICY: "evaluate",
}


class PluginExecutionDeferredError(RuntimeError):
    """A temporary operational pause; retain work without spending its budget."""


class PluginExecutionFailedError(RuntimeError):
    """Plugin processing failed; the ingress poison-work budget must apply."""


@dataclass(frozen=True, slots=True)
class PluginBinding:
    """An assembly-validated instance and its independently owned implementation."""

    spec: PluginSpec
    instance: PluginInstanceConfig
    implementation: Plugin | None
    required: bool = True

    def __post_init__(self) -> None:
        validate_plugin_spec(self.spec)
        if not isinstance(self.required, bool):
            raise TypeError("required must be a boolean")
        if self.spec.plugin_id != self.instance.plugin_id:
            raise ValueError("plugin instance belongs to a different type")
        if not self.required and self.spec.stages != frozenset({PluginStage.ENRICH}):
            raise ValueError("only enrichment-only instances may be optional")
        if self.instance.enabled:
            if missing_required_grants(self.spec, self.instance) or unexpected_grants(
                self.spec, self.instance
            ):
                raise PluginCapabilityError(
                    "plugin instance has missing or unexpected grants"
                )
            for method in (
                "initialize",
                "shutdown",
                *(_METHODS[stage] for stage in self.spec.stages),
            ):
                if not callable(getattr(self.implementation, method, None)):
                    raise TypeError(f"plugin implementation must provide {method}")


@dataclass(frozen=True, slots=True)
class PluginExecutionLimits:
    timeout_seconds: float = 10.0
    max_events: int = 128
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        for name in ("timeout_seconds", "retry_delay_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < 0
                or (name == "timeout_seconds" and value == 0)
            ):
                raise ValueError(
                    f"{name} must be finite and {'positive' if name == 'timeout_seconds' else 'non-negative'}"
                )
            object.__setattr__(self, name, float(value))
        for name in ("max_events", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass
class _InvocationState:
    snapshot: PluginStateSnapshot
    key: PluginExecutionKey
    values: Mapping[str, Any] | None = None
    active: bool = True


class _StateViolation(ValueError):
    pass


class _StateAccess:
    def __init__(self) -> None:
        self.slot: ContextVar[_InvocationState | None] = ContextVar(
            "plugin_state", default=None
        )

    def _active(self) -> _InvocationState:
        state = self.slot.get()
        if state is None or not state.active:
            raise PluginCapabilityError(
                "plugin state is only available during an active invocation"
            )
        return state

    def execution_key(self) -> PluginExecutionKey:
        return self._active().key

    def read(self) -> Mapping[str, Any]:
        state = self._active()
        return state.snapshot.values if state.values is None else state.values

    def write(self, values: Mapping[str, Any]) -> None:
        state = self._active()
        try:
            state.values = PluginStateSnapshot(state.snapshot.revision, values).values
        except (TypeError, ValueError) as exc:
            raise _StateViolation(str(exc)) from exc


@dataclass
class _Instance:
    binding: PluginBinding
    context: PluginContext
    state: _StateAccess
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    quarantined: bool = False
    pending: asyncio.Task | None = None


class PluginExecutor:
    """Execute a frozen ordered plan and return only policy-qualified events.

    Assembly owns lifecycle, as it does for adapters. Start before admitting
    work; stop after ingress drains. No factory, registry, or SDK is imported
    here. An empty executor still binds its recovery plan before routing.
    """

    def __init__(
        self,
        storage: StorageBackend,
        bindings: tuple[PluginBinding, ...],
        *,
        limits: PluginExecutionLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._limits = limits or PluginExecutionLimits()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._instances: list[_Instance] = []
        self._started: list[_Instance] = []
        self._running = False
        identities, implementations = set(), set()
        manifest = []
        for binding in bindings:
            identity = (binding.spec.plugin_id, binding.instance.instance_id)
            if identity in identities:
                raise ValueError("duplicate plugin instance identity")
            identities.add(identity)
            if binding.instance.enabled:
                if id(binding.implementation) in implementations:
                    raise ValueError(
                        "plugin instances must own separate implementations"
                    )
                implementations.add(id(binding.implementation))
            state = _StateAccess()
            context = PluginContext(
                *identity,
                logging.getLogger("medre.plugins." + ".".join(identity)),
                self._clock,
                asyncio.Event(),
                config=binding.instance.config,
                state=state,
                _execution=state.execution_key,
            )
            self._instances.append(_Instance(binding, context, state))
            manifest.append(
                {
                    "plugin_id": identity[0],
                    "instance_id": identity[1],
                    "version": binding.spec.version,
                    "config": configuration_digest(binding.spec, binding.instance),
                    "implementation": [
                        binding.spec.plugin.module,
                        binding.spec.plugin.name,
                    ],
                    "required": binding.required,
                }
            )
        self._plan_digest = hashlib.sha256(
            canonical_json(
                {
                    "host_contract": PLUGIN_HOST_CONTRACT_VERSION,
                    "instances": manifest,
                    "limits": asdict(self.limits),
                }
            ).encode()
        ).hexdigest()
        self._positions = tuple(
            (stage, instance)
            for stage in PluginStage
            for instance in self._instances
            if instance.binding.instance.enabled
            and stage in instance.binding.spec.stages
        )

    @property
    def limits(self) -> PluginExecutionLimits:
        return self._limits

    @property
    def plan_digest(self) -> str:
        return self._plan_digest

    async def _bounded(self, instance: _Instance, coroutine) -> Any:
        if instance.pending is not None and not instance.pending.done():
            coroutine.close()
            raise PluginExecutionDeferredError(
                "plugin invocation has not acknowledged cancellation"
            )
        task = asyncio.create_task(coroutine)
        instance.pending = task

        # Consume late outcomes, including tasks that ignore cancellation. No
        # late return value can be admitted after its invocation is invalidated.
        def consume(done: asyncio.Task) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.limits.timeout_seconds)
            if not done:
                task.cancel()
                raise TimeoutError("plugin invocation exceeded its deadline")
            if task.cancelled():
                raise RuntimeError("plugin invocation cancelled itself")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def start(self) -> None:
        if self._running:
            return
        if self._started:
            raise RuntimeError("plugin cleanup is incomplete")
        try:
            for instance in self._instances:
                if not instance.binding.instance.enabled:
                    continue
                instance.context.shutdown_event.clear()

                async def initialize(item=instance):
                    await item.binding.implementation.initialize(item.context)

                # Include partially initialized implementations in cleanup.
                self._started.append(instance)
                await self._bounded(instance, initialize())
            self._running = True
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> bool:
        self._running = False
        for instance in reversed(tuple(self._started)):
            instance.context.shutdown_event.set()
            try:

                async def shutdown(item=instance):
                    await item.binding.implementation.shutdown()

                await self._bounded(instance, shutdown())
            except Exception:
                continue
            self._started.remove(instance)
        return not self._started

    async def _execute(
        self,
        instance: _Instance,
        stage: PluginStage,
        event: CanonicalEvent,
        scope: str,
        available: int,
    ) -> PluginExecutionRecord:
        binding = instance.binding
        key = PluginExecutionKey(
            event.event_id,
            binding.spec.plugin_id,
            binding.instance.instance_id,
            stage,
            scope,
        )
        async with instance.lock:
            if not self._running:
                raise PluginExecutionDeferredError("plugin executor is stopping")
            previous = await self._storage.get_plugin_execution(key)
            config_digest = configuration_digest(binding.spec, binding.instance)
            if previous is not None:
                if (
                    previous.plan_digest,
                    previous.plugin_version,
                    previous.config_digest,
                ) != (self.plan_digest, binding.spec.version, config_digest):
                    raise PluginExecutionFailedError(
                        "plugin recovery plan or implementation changed"
                    )
                if previous.settled:
                    if previous.quarantines_instance:
                        instance.quarantined = True
                    return previous
                if previous.attempt >= self.limits.max_attempts:
                    raise PluginExecutionFailedError("plugin retry attempts exhausted")
                if (
                    self._clock() - previous.recorded_at
                ).total_seconds() < self.limits.retry_delay_seconds:
                    raise PluginExecutionDeferredError(
                        "plugin retry delay has not elapsed"
                    )
            if instance.pending is not None and not instance.pending.done():
                raise PluginExecutionDeferredError("plugin cancellation is pending")
            snapshot = await self._storage.get_plugin_state(
                key.plugin_id, key.instance_id, scope=scope
            )
            if instance.quarantined:
                return await self._storage.commit_plugin_execution(
                    PluginExecutionRecord(
                        key=key,
                        attempt=previous.attempt + 1 if previous else 1,
                        plugin_version=binding.spec.version,
                        config_digest=config_digest,
                        plan_digest=self.plan_digest,
                        recorded_at=self._clock(),
                        state_revision=snapshot.revision,
                        failure=PluginFailure.QUARANTINED,
                        failure_reason="quarantined_instance: implementation was not invoked",
                        decision=(
                            Pass()
                            if stage is PluginStage.ENRICH and not binding.required
                            else None
                        ),
                    )
                )
            state = _InvocationState(snapshot, key)
            token = instance.state.slot.set(state)
            failure, reason, decision = None, "", None
            try:

                async def invoke():
                    return await getattr(binding.implementation, _METHODS[stage])(event)

                try:
                    decision = await self._bounded(instance, invoke())
                except _StateViolation as exc:
                    failure, reason = PluginFailure.CONTRACT, str(exc)
                except PluginCapabilityError as exc:
                    failure, reason = PluginFailure.CAPABILITY, str(exc)
                except TimeoutError as exc:
                    failure, reason = PluginFailure.TIMEOUT, str(exc)
                except Exception as exc:
                    failure, reason = (
                        PluginFailure.TRANSIENT,
                        f"{type(exc).__name__}: {exc}",
                    )
                state.active = False
                if failure is None:
                    try:
                        validate_plugin_decision(
                            binding.spec,
                            binding.instance,
                            stage,
                            event.event_kind,
                            decision,
                        )
                        count = (
                            len(decision.events)
                            if isinstance(decision, Derive)
                            else int(isinstance(decision, Supersede))
                        )
                        if (
                            count
                            and max(event.depth, len(event.lineage)) >= MAX_PLUGIN_DEPTH
                        ):
                            raise ValueError(
                                "plugin derivation exceeds maximum lineage depth"
                            )
                        if count > available:
                            raise ValueError("source-event expansion budget exceeded")
                        record = PluginExecutionRecord(
                            key=key,
                            attempt=previous.attempt + 1 if previous else 1,
                            plugin_version=binding.spec.version,
                            config_digest=config_digest,
                            plan_digest=self.plan_digest,
                            recorded_at=self._clock(),
                            state_revision=snapshot.revision,
                            state_digest=(
                                state_digest(state.values)
                                if state.values is not None
                                else None
                            ),
                            decision=decision,
                        )
                    except PluginCapabilityError as exc:
                        failure, reason = PluginFailure.CAPABILITY, str(exc)
                    except (TypeError, ValueError) as exc:
                        failure, reason = PluginFailure.CONTRACT, str(exc)
                if failure is not None:
                    record = PluginExecutionRecord(
                        key=key,
                        attempt=previous.attempt + 1 if previous else 1,
                        plugin_version=binding.spec.version,
                        config_digest=config_digest,
                        plan_digest=self.plan_digest,
                        recorded_at=self._clock(),
                        state_revision=snapshot.revision,
                        failure=failure,
                        failure_reason=reason or failure.value,
                        decision=(
                            Pass()
                            if stage is PluginStage.ENRICH and not binding.required
                            else None
                        ),
                    )
                result = await self._storage.commit_plugin_execution(
                    record, state=state.values if failure is None else None
                )
                if result.quarantines_instance:
                    instance.quarantined = True
                return result
            finally:
                state.active = False
                instance.state.slot.reset(token)

    async def process(
        self,
        event_id: str,
        *,
        scope: str = "live",
        on_stage: Callable[[PluginStage], None] | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        if not self._running:
            raise PluginExecutionDeferredError("plugin executor is not running")
        try:
            await self._storage.bind_plugin_plan(event_id, scope, self.plan_digest)
            pending = [(event_id, 0)]
            qualified: list[CanonicalEvent] = []
            admitted = 1
            while pending:
                if not self._running:
                    raise PluginExecutionDeferredError("plugin executor is stopping")
                current_id, cursor = pending.pop()
                event = await self._storage.get(current_id)
                if event is None:
                    raise PluginExecutionFailedError(
                        "plugin continuation event is missing"
                    )
                await self._storage.bind_plugin_plan(
                    current_id, scope, self.plan_digest
                )
                while cursor < len(self._positions):
                    stage, instance = self._positions[cursor]
                    cursor += 1
                    if (
                        instance.binding.spec.input_kinds
                        and event.event_kind not in instance.binding.spec.input_kinds
                    ):
                        continue
                    if on_stage is not None:
                        on_stage(stage)
                    record = await self._execute(
                        instance, stage, event, scope, self.limits.max_events - admitted
                    )
                    if record.decision is None:
                        if record.settled or record.attempt >= self.limits.max_attempts:
                            raise PluginExecutionFailedError(
                                "plugin execution has no successful disposition: "
                                + (
                                    "settled required failure"
                                    if record.settled
                                    else "retry attempts exhausted"
                                )
                            )
                        raise PluginExecutionDeferredError(
                            "plugin execution has no successful disposition"
                        )
                    children = record.produced_event_ids
                    admitted += len(children)
                    if admitted > self.limits.max_events:
                        raise PluginExecutionFailedError(
                            "stored plugin expansion exceeds plan limits"
                        )
                    pending.extend((child, cursor) for child in reversed(children))
                    if isinstance(record.decision, (Deny, Supersede)):
                        if isinstance(record.decision, Deny):
                            _LOG.info(
                                "Plugin policy suppressed event_id=%s instance=%s.%s",
                                event.event_id,
                                record.key.plugin_id,
                                record.key.instance_id,
                            )
                        break
                else:
                    qualified.append(event)
            if not self._running:
                raise PluginExecutionDeferredError("plugin executor is stopping")
            return tuple(qualified)
        except PluginStateConflictError as exc:
            raise PluginExecutionDeferredError(
                "plugin state changed before commit; re-evaluate source work"
            ) from exc
        except PluginExecutionConflictError as exc:
            raise PluginExecutionFailedError(
                "plugin recovery plan or execution evidence conflicts"
            ) from exc
