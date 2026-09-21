"""Matrix-specific runtime configuration preparation.

The runtime builder invokes this module through the built-in adapter registry.
Keeping Matrix route/state preparation here prevents transport-specific policy
from accumulating as ``if transport == ...`` branches in generic assembly.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from medre.config.adapters.matrix import MatrixConfig

__all__ = ["matrix_runtime_directories", "prepare_matrix_runtime_config"]


def prepare_matrix_runtime_config(
    config: MatrixConfig,
    *,
    adapter_id: str,
    paths: Any,
    routes: Iterable[Any],
) -> MatrixConfig:
    """Derive Matrix state paths and route-driven room auto-join settings."""
    prepared = config
    if prepared.store_path is None:
        store_path = paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"
        prepared = replace(prepared, store_path=str(store_path))

    source_rooms: set[str] = set()
    all_rooms: set[str] = set(prepared.auto_join_rooms)
    for route in routes:
        if not getattr(route, "enabled", False):
            continue
        source = getattr(route, "source", None)
        if source is not None and getattr(source, "adapter", None) == adapter_id:
            channel = getattr(source, "channel", None)
            if isinstance(channel, str) and channel.startswith("!"):
                source_rooms.add(channel)
                all_rooms.add(channel)
        for target in getattr(route, "targets", ()):
            if getattr(target, "adapter", None) != adapter_id:
                continue
            channel = getattr(target, "channel", None)
            if isinstance(channel, str) and channel.startswith("!"):
                all_rooms.add(channel)

    if prepared.room_allowlist is not None:
        missing = source_rooms - prepared.room_allowlist
        if missing:
            raise ValueError(
                f"Matrix adapter {adapter_id!r} has room_allowlist that omits "
                f"source rooms from routes: {sorted(missing)}. Either add these "
                f"rooms to room_allowlist or set room_allowlist to None to "
                f"accept all rooms."
            )

    merged = tuple(sorted(all_rooms))
    if merged != prepared.auto_join_rooms:
        prepared = replace(prepared, auto_join_rooms=merged)
    return prepared


def matrix_runtime_directories(
    config: MatrixConfig | None,
    *,
    adapter_id: str,
    paths: Any,
) -> tuple[Path, ...]:
    """Return Matrix-owned state directories required before startup."""
    if config is not None and config.store_path:
        return (Path(config.store_path),)
    return (
        paths.adapter_transport_state_dir(adapter_id, "matrix") / "store",
    )
