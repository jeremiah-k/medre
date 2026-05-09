"""MeshCore adapter package for the MEDRE framework.

This package provides a MeshCore transport adapter that connects to
radio nodes and bridges event payloads into the canonical event stream.

Public symbols
--------------
* :class:`~medre.adapters.meshcore.adapter.MeshCoreAdapter` — the adapter
  itself.
* :class:`~medre.adapters.meshcore.config.MeshCoreConfig` — configuration
  dataclass.
* :class:`~medre.adapters.meshcore.codec.MeshCoreCodec` — decode helper.
* :class:`~medre.adapters.meshcore.renderer.MeshCoreRenderer` — platform
  renderer for MeshCore content payloads.
* :class:`~medre.adapters.meshcore.packet_classifier.MeshCorePacketClassifier` —
  packet classification helper.
* :class:`~medre.adapters.fake_meshcore.FakeMeshCoreAdapter` —
  fake adapter for testing.
* Exception hierarchy: :class:`~medre.adapters.meshcore.errors.MeshCoreError`,
  :class:`~medre.adapters.meshcore.errors.MeshCoreConnectionError`,
  :class:`~medre.adapters.meshcore.errors.MeshCoreSendError`,
  :class:`~medre.adapters.meshcore.errors.MeshCoreConfigError`,
  :class:`~medre.adapters.meshcore.errors.MeshCoreCodecError`,
  :class:`~medre.adapters.meshcore.errors.MeshCorePacketError`.
"""

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.compat import HAS_MESHCORE
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.meshcore.errors import (
    MeshCoreCodecError,
    MeshCoreConfigError,
    MeshCoreConnectionError,
    MeshCoreError,
    MeshCorePacketError,
    MeshCoreSendError,
)
from medre.adapters.meshcore.packet_classifier import (
    MeshCorePacketClassifier,
)
from medre.adapters.meshcore.renderer import MeshCoreRenderer

__all__ = [
    "HAS_MESHCORE",
    "MeshCoreAdapter",
    "MeshCoreCodec",
    "MeshCoreCodecError",
    "MeshCoreConfig",
    "MeshCoreConfigError",
    "MeshCoreConnectionError",
    "MeshCoreError",
    "MeshCorePacketClassifier",
    "MeshCorePacketError",
    "MeshCoreRenderer",
    "MeshCoreSendError",
]
