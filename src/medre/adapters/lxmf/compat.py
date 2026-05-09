"""Optional LXMF/Reticulum dependency guard.

When the ``lxmf`` and ``RNS`` packages are available,
:data:`HAS_LXMF` is ``True`` and the adapter can create real
LXMRouter instances.  When absent, the adapter raises
:class:`~medre.adapters.lxmf.errors.LxmfConnectionError`
on :meth:`start` for non-fake connection types.

This module is the **sole** import site for ``lxmf`` and ``RNS``.
All other LXMF adapter modules must not import ``lxmf`` or ``RNS``
directly.
"""
from __future__ import annotations

HAS_LXMF: bool

try:
    import lxmf  # noqa: F401

    HAS_LXMF = True
except ImportError:
    HAS_LXMF = False
