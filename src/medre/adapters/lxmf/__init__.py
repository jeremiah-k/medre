"""LXMF adapter package for the MEDRE framework.

This package provides an LXMF transport adapter that connects to
Reticulum/LXMF routers and bridges message payloads into the canonical
event stream.

Public symbols
--------------
* :class:`~medre.adapters.lxmf.adapter.LxmfAdapter` — the adapter
  itself.
* :class:`~medre.adapters.lxmf.config.LxmfConfig` — configuration
  dataclass.
* :class:`~medre.adapters.lxmf.codec.LxmfCodec` — decode helper.
* :class:`~medre.adapters.lxmf.renderer.LxmfRenderer` — platform
  renderer for LXMF content payloads.
* :class:`~medre.adapters.lxmf.packet_classifier.LxmfPacketClassifier` —
  packet classification helper.
* :class:`~medre.adapters.lxmf.fields.LxmfFieldsHelper` —
  MEDRE envelope helper for LXMF fields.
* Exception hierarchy: :class:`~medre.adapters.lxmf.errors.LxmfError`,
  :class:`~medre.adapters.lxmf.errors.LxmfConnectionError`,
  :class:`~medre.adapters.lxmf.errors.LxmfSendError`,
  :class:`~medre.adapters.lxmf.errors.LxmfConfigError`,
  :class:`~medre.adapters.lxmf.errors.LxmfCodecError`,
  :class:`~medre.adapters.lxmf.errors.LxmfPacketError`.
"""

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.errors import (
    LxmfCodecError,
    LxmfConfigError,
    LxmfConnectionError,
    LxmfError,
    LxmfPacketError,
    LxmfSendError,
)
from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.adapters.lxmf.renderer import LxmfRenderer

__all__ = [
    "LxmfAdapter",
    "LxmfCodec",
    "LxmfCodecError",
    "LxmfConfig",
    "LxmfConfigError",
    "LxmfConnectionError",
    "LxmfError",
    "LxmfFieldsHelper",
    "LxmfPacketClassifier",
    "LxmfPacketError",
    "LxmfRenderer",
    "LxmfSendError",
]
