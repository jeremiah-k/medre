"""Config-owned adapter configuration models.

Owns adapter configuration dataclasses, config validation errors, and
Matrix credential sidecar helpers.  The config layer does not import
concrete adapter packages.

Canonical imports::

    from medre.config.adapters import MatrixConfig
    from medre.config.adapters.errors import MatrixConfigError
    from medre.config.adapters.matrix_credentials import load_credentials_json
"""

from medre.config.adapters.errors import (
    AdapterConfigError,
    LxmfConfigError,
    MatrixConfigError,
    MeshCoreConfigError,
    MeshtasticConfigError,
)
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig

__all__ = [
    "AdapterConfigError",
    "LxmfConfig",
    "LxmfConfigError",
    "MatrixConfig",
    "MatrixConfigError",
    "MeshCoreConfig",
    "MeshCoreConfigError",
    "MeshtasticConfig",
    "MeshtasticConfigError",
]
