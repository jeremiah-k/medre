"""Shared soak test helpers: runtime harness, diagnostics, and env validation.

Imported by soak-related test files.  Contains no pytest fixtures — those
live in each test file so pytest can discover them.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Iteration configuration
# ---------------------------------------------------------------------------

_DEFAULT_ITERATIONS: int = 50
_MAX_ITERATIONS: int = 200


def _get_iterations() -> int:
    """Read iteration count from env, clamped to bounds."""
    raw = os.environ.get("SOAK_HARNESS_ITERATIONS", "")
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return _DEFAULT_ITERATIONS
    return max(1, min(val, _MAX_ITERATIONS))


# ---------------------------------------------------------------------------
# DiagnosticsSnapshot
# ---------------------------------------------------------------------------


@dataclass
class DiagnosticsSnapshot:
    """Point-in-time diagnostics captured during a soak run."""

    iteration: int
    runtime_state: str
    adapter_count: int
    event_metrics: dict[str, Any] | None
    capacity: dict[str, Any] | None


# ---------------------------------------------------------------------------
# SoakRuntime — test helper
# ---------------------------------------------------------------------------


class SoakRuntime:
    """Test harness that builds and drives a MEDRE runtime with fake adapters.

    Builds a runtime with one fake adapter per transport (Matrix, Meshtastic,
    MeshCore, LXMF) using ``adapter_kind="fake"`` and in-memory storage.
    Provides lifecycle management and diagnostic collection for soak tests.

    This is **not** a production runtime — it is a test-only harness for
    verifying stability patterns without live transports.

    Parameters
    ----------
    iterations:
        Number of event-delivery iterations per run (default from env).
    """

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        iterations: int | None = None,
    ) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self.iterations = iterations or _get_iterations()
        self.app: MedreApp | None = None
        self._snapshots: list[DiagnosticsSnapshot] = []

    # -- Config construction -------------------------------------------------

    @staticmethod
    def make_config() -> RuntimeConfig:
        """Build a RuntimeConfig with all four fake adapters enabled."""
        return RuntimeConfig(
            runtime=RuntimeOptions(name="soak-harness"),
            logging=LoggingConfig(level="WARNING"),
            storage=StorageConfig(backend="memory"),
            limits=RuntimeLimits(
                max_inflight_deliveries=50,
                max_inflight_replay_events=50,
            ),
            adapters=AdapterConfigSet(
                matrix={
                    "soak_matrix": MatrixRuntimeConfig(
                        adapter_id="soak_matrix",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                meshtastic={
                    "soak_mesh": MeshtasticRuntimeConfig(
                        adapter_id="soak_mesh",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                meshcore={
                    "soak_meshcore": MeshCoreRuntimeConfig(
                        adapter_id="soak_meshcore",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                lxmf={
                    "soak_lxmf": LxmfRuntimeConfig(
                        adapter_id="soak_lxmf",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )

    def make_paths(self) -> MedrePaths:
        """Resolve MedrePaths to a temp directory."""
        self._monkeypatch.setenv("MEDRE_HOME", str(self._tmp_path))
        return resolve()

    # -- Lifecycle -----------------------------------------------------------

    def build(self) -> MedreApp:
        """Build (but do not start) the runtime."""
        config = self.make_config()
        paths = self.make_paths()
        builder = RuntimeBuilder(config, paths)
        self.app = builder.build()
        return self.app

    async def start(self) -> None:
        """Build and start the runtime."""
        if self.app is None:
            self.build()
        assert self.app is not None
        await self.app.start()

    async def stop(self) -> None:
        """Stop the runtime if running."""
        if self.app is not None:
            await self.app.stop()

    async def restart(self) -> None:
        """Stop (if needed), rebuild, and start a fresh runtime."""
        await self.stop()
        self.app = None
        self._snapshots.clear()
        await self.start()

    async def start_fresh(self) -> None:
        """Build a new runtime from scratch and start it."""
        self.app = None
        self._snapshots.clear()
        await self.start()

    # -- Event delivery ------------------------------------------------------

    async def deliver_events(self, count: int = 1) -> list[dict[str, Any]]:
        """Deliver *count* fake inbound events through all adapters.

        Uses each fake adapter's ``simulate_inbound`` method to publish
        events through the pipeline.  Returns a list of per-iteration
        summary dicts.

        This exercises the full pipeline path: adapter -> event bus ->
        pipeline runner -> rendering -> outbound adapter delivery.
        """
        assert self.app is not None
        results: list[dict[str, Any]] = []

        for i in range(count):
            delivered = 0
            for _adapter_id, adapter in self.app.adapters.items():
                ctx = getattr(adapter, "ctx", None)
                if ctx is None:
                    continue
                # Use publish_inbound directly -- fake adapters may not
                # all support simulate_inbound uniformly.  Instead, call
                # the context's publish_inbound which the pipeline
                # subscribes to.
                if hasattr(adapter, "simulate_inbound"):
                    try:
                        if hasattr(adapter, "make_text_event"):
                            event = adapter.make_text_event(
                                f"soak-msg-{i}",
                                channel="soak-channel",
                            )
                        elif hasattr(adapter, "make_event"):
                            event = adapter.make_event(
                                f"soak-msg-{i}",
                                channel="soak-channel",
                            )
                        else:
                            continue
                        await adapter.simulate_inbound(event)
                        delivered += 1
                    except Exception:
                        # Soak harness should not crash on individual
                        # delivery failures, but we should log them.
                        ctx.logger.exception("Soak delivery iteration failed")
            results.append({"iteration": i, "delivered": delivered})

        return results

    # -- Diagnostics ---------------------------------------------------------

    def capture_diagnostics(self, iteration: int = 0) -> DiagnosticsSnapshot:
        """Capture a diagnostics snapshot at the given iteration."""
        assert self.app is not None
        snap = self.app.diagnostic_snapshot()
        snapshot = DiagnosticsSnapshot(
            iteration=iteration,
            runtime_state=snap.get("runtime_state", "unknown"),
            adapter_count=len(self.app.adapters),
            event_metrics=None,
            capacity=snap.get("capacity"),
        )
        self._snapshots.append(snapshot)
        return snapshot

    @property
    def snapshots(self) -> list[DiagnosticsSnapshot]:
        """All captured diagnostics snapshots."""
        return list(self._snapshots)

    def is_clean_state(self) -> bool:
        """Check if the runtime is in a clean (stopped or initialized) state."""
        if self.app is None:
            return True
        return self.app.state in (
            RuntimeState.INITIALIZED,
            RuntimeState.STOPPED,
        )


# ---------------------------------------------------------------------------
# Asyncio task counting
# ---------------------------------------------------------------------------


def _count_asyncio_tasks() -> int:
    """Return the number of currently alive asyncio tasks."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return len([t for t in asyncio.all_tasks(loop) if not t.done()])


# ---------------------------------------------------------------------------
# Meshtastic soak env validation
# ---------------------------------------------------------------------------

_MESHTASTIC_CONNECTION_TYPE = os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower()


def _validate_meshtastic_soak_env() -> tuple[bool, str]:
    """Check Meshtastic soak env vars, mirroring live smoke gating.

    Returns (ok, reason).  ``ok`` is True when the required vars for the
    selected connection type are present.
    """
    ct = _MESHTASTIC_CONNECTION_TYPE
    if not ct:
        return (
            False,
            "Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) for Meshtastic soak",
        )
    if ct == "tcp":
        if not os.environ.get("MESHTASTIC_HOST"):
            return False, "MESHTASTIC_HOST required for TCP soak"
    elif ct == "serial":
        if not os.environ.get("MESHTASTIC_SERIAL_PORT"):
            return False, "MESHTASTIC_SERIAL_PORT required for serial soak"
    elif ct == "ble":
        if not os.environ.get("MESHTASTIC_BLE_ADDRESS"):
            return False, "MESHTASTIC_BLE_ADDRESS required for BLE soak"
    else:
        return False, f"Unknown MESHTASTIC_CONNECTION_TYPE {ct!r}; use tcp/serial/ble"
    return True, ""


def _make_meshtastic_config() -> Any:
    """Build a MeshtasticConfig from environment variables.

    Supports tcp, serial, and ble connection types with the same env var
    convention as the live smoke tests (``test_meshtastic_live.py``).
    """
    from medre.config.adapters.meshtastic import MeshtasticConfig

    ct = _MESHTASTIC_CONNECTION_TYPE
    if ct == "serial":
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="serial",
            serial_port=os.environ["MESHTASTIC_SERIAL_PORT"],
            default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
        )
    elif ct == "ble":
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="ble",
            ble_address=os.environ["MESHTASTIC_BLE_ADDRESS"],
            default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
        )
    else:  # tcp (default)
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="tcp",
            host=os.environ.get("MESHTASTIC_HOST"),
            port=int(os.environ.get("MESHTASTIC_PORT", "4403")),
            default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
        )
