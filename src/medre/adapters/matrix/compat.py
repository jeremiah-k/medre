"""Compatibility guards for optional mindroom-nio dependency.

Two module-level flags:

* ``HAS_NIO`` — ``True`` when the ``nio`` package can be imported.
* ``HAS_E2EE`` — ``True`` when the ``nio.crypto`` sub-package is
  available **and** ``ENCRYPTION_ENABLED`` is ``True``.

Both flags are safe to monkeypatch in tests::

    import medre.adapters.matrix.compat as compat
    compat.HAS_E2EE = True   # pretend e2ee is available
"""

from __future__ import annotations

HAS_NIO: bool
HAS_E2EE: bool

try:
    import nio  # noqa: F401

    HAS_NIO = True
except ImportError:
    HAS_NIO = False


def _check_e2ee() -> bool:
    """Detect whether the nio crypto subsystem is available.

    Safely inspects ``nio.crypto.ENCRYPTION_ENABLED`` without importing
    crypto in the core adapter path.  Returns ``False`` if nio is missing
    or if the crypto module / vodozemac dependency is unavailable.
    """
    if not HAS_NIO:
        return False
    try:
        from nio.crypto import ENCRYPTION_ENABLED  # type: ignore[import-untyped]

        return bool(ENCRYPTION_ENABLED)
    except Exception:
        return False


HAS_E2EE = _check_e2ee()
