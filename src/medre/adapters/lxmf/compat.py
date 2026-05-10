"""Optional LXMF/Reticulum dependency guard.

When the ``lxmf`` and ``RNS`` packages are available,
:data:`HAS_LXMF` is ``True`` and the adapter can create real
LXMRouter instances.  When absent, the adapter raises
:class:`~medre.adapters.lxmf.errors.LxmfConnectionError`
on :meth:`start` for non-fake connection types.

This module is the **sole** import site for ``lxmf`` and ``RNS``.
All other LXMF adapter modules must not import ``lxmf`` or ``RNS``
directly — they access the modules through :data:`rns_module` and
:data:`lxmf_module` here.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

HAS_LXMF: bool

rns_module: ModuleType | None
"""The ``RNS`` module, or ``None`` when not installed."""

lxmf_module: ModuleType | None
"""The ``lxmf`` module, or ``None`` when not installed."""

try:
    import RNS  # noqa: F401

    rns_module = RNS
except ImportError:
    rns_module = None

try:
    import LXMF  # noqa: F401

    lxmf_module = LXMF
except ImportError:
    lxmf_module = None

HAS_LXMF = (rns_module is not None) and (lxmf_module is not None)


def _require_lxmf() -> tuple[Any, Any]:
    """Return ``(RNS, lxmf)`` modules or raise :class:`ImportError`.

    Convenience for session internals that need both modules.
    """
    if not HAS_LXMF or rns_module is None or lxmf_module is None:
        raise ImportError(
            "lxmf and/or RNS packages are not installed; "
            "pip install lxmf or use connection_type='fake'"
        )
    return rns_module, lxmf_module
