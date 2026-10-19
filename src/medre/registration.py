"""Shared registration primitives for adapter and plugin declarations.

Adapter types and plugin types are declarative value types distributed
independently of the runtime. Both describe implementations with lazy
``SymbolRef`` strings so configuration parsing and CLI discovery never import
optional SDK-backed provider modules merely to learn what is installed.

The registration token grammar is shared: transports, destination kinds, and
plugin identifiers all follow the same platform-neutral pattern.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "REGISTRATION_NAME_RE",
    "SymbolRef",
    "valid_registration_name",
]


REGISTRATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def valid_registration_name(value: object) -> bool:
    """Whether a value follows the platform-neutral registration token grammar."""
    return isinstance(value, str) and REGISTRATION_NAME_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """Lazy reference to a Python symbol.

    Keeping references lazy is important: config parsing and CLI discovery
    must not import optional SDK-backed provider modules merely to learn which
    transports MEDRE supports.
    """

    module: str
    name: str

    def load(self) -> Any:
        """Import and return the referenced symbol."""
        module = importlib.import_module(self.module)
        return getattr(module, self.name)
