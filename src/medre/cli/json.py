"""JSON serialisation helpers for CLI output.

The single source of truth for ``--json`` emission in MEDRE CLI commands
is :func:`to_json`.  It accepts a ``dict``, ``dataclass``, ``msgspec.Struct``,
or any value the ``msgspec.json`` encoder can handle (containers,
``datetime``, ``UUID``, …) and returns a deterministic pretty-printed
JSON string (sorted keys, ``indent=2``).

Using one helper everywhere keeps the shape of CLI output consistent
across commands so operators can parse it with the same tooling.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any


def to_json(obj: Any) -> str:
    """Serialise *obj* to canonical JSON text.

    Accepts ``dict``, ``dataclass``, ``msgspec.Struct``, or any msgspec-
    encodable value.  Returns a sorted-keys, indented string suitable
    for direct ``print()``.

    Conversion order:

    1. Plain ``dict`` (and any container msgspec handles) — round-trip
       through ``msgspec.json.encode``/``decode`` so datetimes and
       custom types are normalised, then dump with ``sort_keys`` and
       ``indent=2``.
    2. ``dataclasses.dataclass`` instances — recursively converted via
       :func:`dataclasses.asdict` and then dumped the same way.  This
       matches the historical shape produced by callers that already
       did ``json.dumps(asdict(...))``.
    3. Fallback for unknown types — ``default=str`` so ``datetime`` and
       similar repr-strings still serialise instead of raising
       ``TypeError``.

    All branches produce the same canonical shape (sorted keys,
    indent=2, trailing newline omitted — callers add it if needed).
    """
    raw: Any
    if is_dataclass(obj) and not isinstance(obj, type):
        raw = asdict(obj)
    else:
        raw = obj
    try:
        import msgspec

        encoded = msgspec.json.encode(raw)
        return json.dumps(json.loads(encoded), sort_keys=True, indent=2)
    except ImportError:
        return json.dumps(raw, sort_keys=True, indent=2, default=str)
    except (TypeError, ValueError):
        return json.dumps(raw, sort_keys=True, indent=2, default=str)
