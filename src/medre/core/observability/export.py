"""Safe text export helpers for runtime metric snapshots.

The exporter consumes already-sanitized runtime snapshot dictionaries and emits
only numeric/boolean leaves.  String values are deliberately excluded from
samples, and no labels are emitted.  Bounded mapping keys may still become
sanitized metric-name segments.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_METRIC_PART = re.compile(r"[^a-zA-Z0-9_]+")


def _metric_part(value: object) -> str:
    part = _METRIC_PART.sub("_", str(value)).strip("_").lower()
    if not part:
        return "unnamed"
    if part[0].isdigit():
        part = f"n_{part}"
    return part


def snapshot_to_prometheus(
    snapshot: Mapping[str, Any], *, prefix: str = "medre"
) -> str:
    """Render numeric snapshot leaves in Prometheus text exposition format.

    Every exported value is a gauge representing the instantaneous snapshot.
    Mapping keys become metric-name components; strings, ``None``, sequences,
    and non-finite floats are skipped.  No labels are emitted.  Callers should
    keep mapping-key domains bounded because keys may become metric names.
    """
    root = _metric_part(prefix)
    samples: list[tuple[str, tuple[str, ...], int | float]] = []

    def walk(
        value: Any, sanitized_path: tuple[str, ...], original_path: tuple[str, ...]
    ) -> None:
        if isinstance(value, Mapping):
            for key in sorted(value, key=lambda item: str(item)):
                key_text = str(key)
                walk(
                    value[key],
                    (*sanitized_path, _metric_part(key_text)),
                    (*original_path, key_text),
                )
            return
        name = "_".join((root, *sanitized_path))
        if isinstance(value, bool):
            samples.append((name, original_path, 1 if value else 0))
            return
        if isinstance(value, int):
            samples.append((name, original_path, value))
            return
        if isinstance(value, float) and math.isfinite(value):
            samples.append((name, original_path, value))

    walk(snapshot, (), ())
    lines: list[str] = []
    seen: dict[str, tuple[str, ...]] = {}
    for name, original_path, value in sorted(samples):
        previous_path = seen.get(name)
        if previous_path is not None:
            raise ValueError(
                "metric name collision after sanitization: "
                f"{'.'.join(previous_path)!r} and {'.'.join(original_path)!r} "
                f"both map to {name!r}"
            )
        seen[name] = original_path
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + ("\n" if lines else "")
