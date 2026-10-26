"""Operator-declared provider state for a selected stopped MEDRE snapshot.

No filesystem autodiscovery is permitted. Operators select a common state root
containing each provider's declared files, including any XDG profile/cache files
that are required to preserve identity. The file list is an operational input,
not proof that native processes have exited.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from medre.maintenance.stopped_snapshot import SnapshotEntry, _open_regular

_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_INVENTORY_BYTES = 1024 * 1024


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate inventory keys before their values become authoritative."""
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate inventory key")
        data[key] = value
    return data


@dataclass(frozen=True, slots=True)
class StoppedInventory:
    """Exact file selection and declared provider coverage; not SDK completeness."""

    entries: tuple[SnapshotEntry, ...]
    providers: tuple[str, ...]

    @classmethod
    def load(cls, filename: Path) -> StoppedInventory:
        """Load a bounded, closed JSON inventory without following an input symlink."""
        path = Path(filename)
        if path.is_symlink():
            raise ValueError("inventory must be a bounded regular file")
        # Share capture's descriptor-based regular-file check. Path metadata
        # followed by a separate open can race with replacement by a symlink
        # or a blocking device. Bound both the descriptor size and bytes read.
        with _open_regular(path) as source:
            if os.fstat(source.fileno()).st_size > _MAX_INVENTORY_BYTES:
                raise ValueError("inventory exceeds maximum size")
            raw = source.read(_MAX_INVENTORY_BYTES + 1)
        if len(raw) > _MAX_INVENTORY_BYTES:
            raise ValueError("inventory exceeds maximum size")
        data = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (
            not isinstance(data, dict)
            or set(data) != {"format", "version", "providers", "files"}
            or data["format"] != "medre-stopped-inventory"
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["providers"], list)
            or not isinstance(data["files"], list)
        ):
            raise ValueError("unsupported stopped inventory")
        providers = data["providers"]
        if (
            not providers
            or any(
                not isinstance(v, str) or _PROVIDER.fullmatch(v) is None
                for v in providers
            )
            or len(providers) != len(set(providers))
            or "core" not in providers
        ):
            raise ValueError("inventory requires distinct providers including core")
        owners: set[str] = set()
        paths: set[str] = set()
        entries: list[SnapshotEntry] = []
        for row in data["files"]:
            if not isinstance(row, dict) or set(row) != {
                "provider",
                "path",
                "role",
                "format",
            }:
                raise ValueError("invalid inventory file declaration")
            owner = row["provider"]
            if not isinstance(owner, str) or owner not in providers:
                raise ValueError("inventory file names an undeclared provider")
            entry = SnapshotEntry(row["path"], row["role"], row["format"])
            if entry.path in paths:
                raise ValueError("inventory file is declared more than once")
            if entry.role == "core_sqlite" and owner != "core":
                raise ValueError("only core may own the core SQLite file")
            owners.add(owner)
            paths.add(entry.path)
            entries.append(entry)
        if (
            owners != set(providers)
            or sum(entry.role == "core_sqlite" for entry in entries) != 1
        ):
            raise ValueError("each declared provider must own selected state")
        return cls(tuple(entries), tuple(providers))
