"""Meshtastic adapter package for the MEDRE framework.

This package provides a Meshtastic transport adapter that connects to
radio nodes via the ``mtjk`` library and bridges packets into the
canonical event stream.

Public symbols
--------------
* :class:`~medre.adapters.meshtastic.adapter.MeshtasticAdapter` — the adapter
  itself.
* :class:`~medre.adapters.meshtastic.session.MeshtasticSession` — session
  lifecycle boundary owning raw transport.
* :class:`~medre.adapters.meshtastic.codec.MeshtasticCodec` — decode helper.
* :class:`~medre.adapters.meshtastic.renderer.MeshtasticRenderer` — platform
  renderer for Meshtastic content payloads.
* :class:`~medre.adapters.meshtastic.packet_classifier.MeshtasticPacketClassifier` —
  packet classification helper.
* :class:`~medre.adapters.meshtastic.queue.MeshtasticOutboundQueue` —
  outbound queue with pacing scaffolding.
* :class:`~medre.adapters.fake_meshtastic.FakeMeshtasticAdapter` —
  fake adapter for testing.
* Exception hierarchy: :class:`~medre.adapters.meshtastic.errors.MeshtasticError`,
  :class:`~medre.adapters.meshtastic.errors.MeshtasticConnectionError`,
  :class:`~medre.adapters.meshtastic.errors.MeshtasticSendError`,
  :class:`~medre.adapters.meshtastic.errors.MeshtasticConfigError`,
  :class:`~medre.adapters.meshtastic.errors.MeshtasticCodecError`,
  :class:`~medre.adapters.meshtastic.errors.MeshtasticPacketError`.
"""

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.errors import (
    MeshtasticCodecError,
    MeshtasticConnectionError,
    MeshtasticError,
    MeshtasticPacketError,
    MeshtasticSendError,
)
from medre.adapters.meshtastic.packet_classifier import (
    MeshtasticPacketClassifier,
)
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.adapters.meshtastic.session import (
    MeshtasticSession,
    MeshtasticSessionDiagnostics,
)

__all__ = [
    "MeshtasticAdapter",
    "MeshtasticCodec",
    "MeshtasticCodecError",
    "MeshtasticConnectionError",
    "MeshtasticError",
    "MeshtasticPacketClassifier",
    "MeshtasticPacketError",
    "MeshtasticOutboundQueue",
    "MeshtasticRenderer",
    "MeshtasticSendError",
    "MeshtasticSession",
    "MeshtasticSessionDiagnostics",
]
