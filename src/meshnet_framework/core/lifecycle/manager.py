"""Lifecycle manager for coordinating adapter startup, shutdown, and health.

The :class:`LifecycleManager` owns a registry of adapter instances and
tracks their :class:`~meshnet_framework.core.lifecycle.states.AdapterState`
through the state machine defined in
:mod:`~meshnet_framework.core.lifecycle.states`.
"""

from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Protocol

from meshnet_framework.core.lifecycle.states import (
    AdapterState,
    InvalidStateTransition,
    require_valid_transition,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class ManagedAdapter(Protocol):
    """Minimal interface that an adapter must implement for lifecycle management.

    The lifecycle manager calls these methods during startup, shutdown,
    and health checks.
    """

    async def start(self) -> None:
        """Start the adapter and prepare it for event processing."""
        ...

    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        ...

    async def health_check(self) -> AdapterState:
        """Return the adapter's self-reported state.

        Returns
        -------
        AdapterState
            The current state as reported by the adapter.
        """
        ...


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class LifecycleManager:
    """Coordinate lifecycle of all registered adapters.

    The manager maintains a state registry for every adapter and enforces
    legal transitions via the state machine defined in
    :mod:`~meshnet_framework.core.lifecycle.states`.

    Typical usage::

        manager = LifecycleManager()
        await manager.register_adapter("matrix-1", matrix_adapter)
        await manager.start_all()
        # ... run ...
        await manager.stop_all()
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ManagedAdapter] = {}
        self._states: dict[str, AdapterState] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_adapter(self, adapter_id: str, adapter: Any) -> None:
        """Register an adapter instance with the lifecycle manager.

        The adapter is placed in the :attr:`~AdapterState.INITIALIZING` state
        immediately.  Use :meth:`start_all` to transition it to
        :attr:`~AdapterState.READY`.

        Parameters
        ----------
        adapter_id:
            Unique identifier for this adapter instance.
        adapter:
            The adapter object.  Must implement :class:`ManagedAdapter`
            (i.e. expose ``start()``, ``stop()``, and ``health_check()``
            async methods).

        Raises
        ------
        ValueError
            If *adapter_id* is already registered.
        """
        if adapter_id in self._adapters:
            raise ValueError(
                f"Adapter {adapter_id!r} is already registered"
            )
        self._adapters[adapter_id] = adapter  # type: ignore[assignment]
        self._states[adapter_id] = AdapterState.INITIALIZING
        logger.info("Registered adapter %s (state=INITIALIZING)", adapter_id)

    # ------------------------------------------------------------------
    # Lifecycle operations
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Start all adapters in the INITIALIZING state.

        Each adapter's ``start()`` method is awaited in order of
        registration.  On success the adapter transitions to
        :attr:`~AdapterState.READY`; on exception it transitions to
        :attr:`~AdapterState.FAILED`.
        """
        for adapter_id, adapter in self._adapters.items():
            if self._states[adapter_id] != AdapterState.INITIALIZING:
                continue
            try:
                await adapter.start()
                self._states[adapter_id] = AdapterState.READY
                logger.info("Adapter %s started (state=READY)", adapter_id)
            except Exception:
                self._states[adapter_id] = AdapterState.FAILED
                logger.exception(
                    "Adapter %s failed during start (state=FAILED)",
                    adapter_id,
                )

    async def stop_all(self, timeout: float = 30.0) -> None:
        """Gracefully stop all running adapters.

        Each adapter's ``stop()`` method is called with an overall
        *timeout* (in seconds).  Adapters that do not stop within the
        timeout are left in their current state and a warning is logged.

        Parameters
        ----------
        timeout:
            Maximum total seconds to wait for all adapters to stop.
        """
        stoppable_ids = [
            aid
            for aid, state in self._states.items()
            if state
            not in (AdapterState.STOPPING, AdapterState.FAILED)
        ]

        if not stoppable_ids:
            return

        async def _stop_one(adapter_id: str) -> None:
            adapter = self._adapters[adapter_id]
            try:
                self._states[adapter_id] = AdapterState.STOPPING
                await adapter.stop()
                self._states[adapter_id] = AdapterState.FAILED  # terminal
                logger.info("Adapter %s stopped", adapter_id)
            except Exception:
                self._states[adapter_id] = AdapterState.FAILED
                logger.exception(
                    "Adapter %s failed during stop (state=FAILED)",
                    adapter_id,
                )

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(
                    *(_stop_one(aid) for aid in stoppable_ids)
                )
        except TimeoutError:
            logger.warning(
                "stop_all timed out after %.1fs – some adapters may not "
                "have shut down cleanly",
                timeout,
            )

    async def health_check_all(self) -> dict[str, AdapterState]:
        """Query each adapter's health and update the state registry.

        Returns
        -------
        dict[str, AdapterState]
            Mapping of adapter ID to its current (possibly updated) state.
        """
        results: dict[str, AdapterState] = {}
        for adapter_id, adapter in self._adapters.items():
            try:
                reported = await adapter.health_check()
                current = self._states[adapter_id]
                if reported != current:
                    try:
                        require_valid_transition(current, reported)
                        self._states[adapter_id] = reported
                    except InvalidStateTransition:
                        logger.warning(
                            "Adapter %s reported illegal transition "
                            "%s -> %s; forcing to reported state",
                            adapter_id,
                            current.value,
                            reported.value,
                        )
                        self._states[adapter_id] = reported
                results[adapter_id] = reported
            except Exception:
                self._states[adapter_id] = AdapterState.FAILED
                results[adapter_id] = AdapterState.FAILED
                logger.exception(
                    "Adapter %s health check failed (state=FAILED)",
                    adapter_id,
                )
        return results

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_state(self, adapter_id: str) -> AdapterState:
        """Return the current state of the named adapter.

        Parameters
        ----------
        adapter_id:
            The registered adapter identifier.

        Returns
        -------
        AdapterState

        Raises
        ------
        KeyError
            If *adapter_id* is not registered.
        """
        try:
            return self._states[adapter_id]
        except KeyError:
            raise KeyError(
                f"No adapter registered with id={adapter_id!r}"
            ) from None

    async def transition_to(
        self, adapter_id: str, state: AdapterState
    ) -> None:
        """Explicitly transition an adapter to a new state.

        The transition must be valid according to the state machine; see
        :func:`~meshnet_framework.core.lifecycle.states.require_valid_transition`.

        Parameters
        ----------
        adapter_id:
            The registered adapter identifier.
        state:
            The target state.

        Raises
        ------
        KeyError
            If *adapter_id* is not registered.
        InvalidStateTransition
            If the transition is illegal.
        """
        current = self.get_state(adapter_id)
        require_valid_transition(current, state)
        self._states[adapter_id] = state
        logger.info(
            "Adapter %s transitioned %s -> %s",
            adapter_id,
            current.value,
            state.value,
        )
