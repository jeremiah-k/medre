"""Compatibility guard for optional meshtastic dependency.

``mtjk`` is installed as ``pip install mtjk`` and imported as ``meshtastic``.

.. todo::
    Before implementing real TCP/serial/BLE connection support, verify
    that the ``mtjk`` package (dist name ``mtjk``, import name
    ``meshtastic``) is the intended dependency and that its callback
    packet shapes match the fixtures in this test suite.  If the package
    diverges from expectations, update the fixture shapes and codec
    accordingly.
"""
from __future__ import annotations

HAS_MESHTASTIC: bool
try:
    import meshtastic  # noqa: F401

    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False
