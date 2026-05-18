"""Shared helpers for fake-runtime smoke / soak / startup / snapshot tests.

Extracted from tests/test_fake_runtime_smoke.py so that the split test files
can reuse the same config builders and lifecycle helpers without cross-test
imports.
"""

from __future__ import annotations

import asyncio
from typing import Any

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths
from medre.core.events.kinds import EventKind
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_until(
    predicate: Any,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Poll *predicate* every *interval* seconds until it returns ``True``.

    Raises ``AssertionError`` if *timeout* expires before the predicate
    is satisfied.  The predicate can be any synchronous callable.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"wait_until timed out after {timeout}s: "
                f"predicate {predicate!r} never satisfied"
            )
        await asyncio.sleep(min(interval, remaining))


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def make_multi_adapter_config() -> RuntimeConfig:
    """Build RuntimeConfig matching examples/configs/fake-multi-adapter.toml.

    All four adapter types enabled with ``adapter_kind="fake"``.
    """
    return RuntimeConfig(
        runtime=RuntimeOptions(name="fake-multi-dev"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "fake_matrix": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "fake_meshtastic": MeshtasticRuntimeConfig(
                    adapter_id="fake_meshtastic",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshcore={
                "fake_meshcore": MeshCoreRuntimeConfig(
                    adapter_id="fake_meshcore",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            lxmf={
                "fake_lxmf": LxmfRuntimeConfig(
                    adapter_id="fake_lxmf",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def make_two_adapter_config_with_route() -> tuple[RuntimeConfig, Route]:
    """Config with two fake Matrix adapters + a route from one to the other."""
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="smoke-routing"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "mx_alpha": MatrixRuntimeConfig(
                    adapter_id="mx_alpha",
                    enabled=True,
                    adapter_kind="fake",
                ),
                "mx_beta": MatrixRuntimeConfig(
                    adapter_id="mx_beta",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    route = Route(
        id="alpha-to-beta",
        source=RouteSource(
            adapter="mx_alpha",
            event_kinds=(EventKind.MESSAGE_TEXT,),
            channel=None,
        ),
        targets=[RouteTarget(adapter="mx_beta")],
    )
    return config, route


def make_cross_transport_config_with_route() -> tuple[RuntimeConfig, Route]:
    """Config with Matrix + Meshtastic adapters and a cross-transport route."""
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="smoke-cross-transport"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "mx_src": MatrixRuntimeConfig(
                    adapter_id="mx_src",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "mesh_dst": MeshtasticRuntimeConfig(
                    adapter_id="mesh_dst",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )
    route = Route(
        id="matrix-to-mesh",
        source=RouteSource(
            adapter="mx_src",
            event_kinds=(EventKind.MESSAGE_TEXT,),
            channel=None,
        ),
        targets=[RouteTarget(adapter="mesh_dst")],
    )
    return config, route


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------


async def build_and_start(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp from config and start it."""
    builder = RuntimeBuilder(config, paths)
    app = builder.build()
    await app.start()
    return app


async def clean_stop(app: MedreApp) -> None:
    """Stop a running MedreApp, asserting it reaches STOPPED."""
    await app.stop()
    assert app.state is RuntimeState.STOPPED
