"""Matrix adapter package for the MEDRE framework.

This package provides a full-featured Matrix presentation adapter
that connects to a homeserver via ``mindroom-nio`` and bridges
messages into the canonical event stream.

Public symbols
--------------
* :class:`~medre.adapters.matrix.adapter.MatrixAdapter` — the adapter
  itself.
* :class:`~medre.adapters.matrix.config.MatrixConfig` — configuration
  dataclass.
* :class:`~medre.adapters.matrix.session.MatrixSession` — session
  lifecycle boundary owning the nio client.
* :class:`~medre.adapters.matrix.codec.MatrixCodec` — decode
  helper (native → canonical).
* :class:`~medre.adapters.matrix.renderer.MatrixRenderer` — platform
  renderer for Matrix content payloads.
* :class:`~medre.adapters.matrix.metadata.MatrixMetadataEnvelope` —
  provenance envelope embedded in message content.
* :class:`~medre.adapters.matrix.relations.MatrixRelationHandler` —
  relation extraction helper.
* Exception hierarchy: :class:`~medre.adapters.matrix.errors.MatrixError`,
  :class:`~medre.adapters.matrix.errors.MatrixConnectionError`,
  :class:`~medre.adapters.matrix.errors.MatrixSendError`,
  :class:`~medre.adapters.matrix.errors.MatrixConfigError`,
  :class:`~medre.adapters.matrix.errors.MatrixCodecError`.
"""

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import (
    MatrixCodecError,
    MatrixConfigError,
    MatrixConnectionError,
    MatrixError,
    MatrixSendError,
)
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import MatrixRelationHandler
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.matrix.session import MatrixSession

__all__ = [
    "MatrixAdapter",
    "MatrixCodec",
    "MatrixCodecError",
    "MatrixConfig",
    "MatrixConfigError",
    "MatrixConnectionError",
    "MatrixError",
    "MatrixMetadataEnvelope",
    "MatrixRelationHandler",
    "MatrixRenderer",
    "MatrixSendError",
    "MatrixSession",
]
