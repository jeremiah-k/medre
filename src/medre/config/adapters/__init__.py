"""Config-owned adapter configuration models.

Owns adapter configuration dataclasses, config validation errors, and
Matrix credential sidecar helpers.  The config layer does not import
concrete adapter packages.

Canonical imports::

    from medre.config.adapters.matrix import MatrixConfig
    from medre.config.adapters.errors import MatrixConfigError
    from medre.config.adapters.matrix_credentials import load_credentials_json
"""
