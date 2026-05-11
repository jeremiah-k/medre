"""Configuration error hierarchy for MEDRE runtime configuration.

All configuration-related errors inherit from :class:`ConfigError` so
callers can catch the base class or a specific subclass as needed.
"""


class ConfigError(Exception):
    """Base exception for all configuration errors."""


class ConfigNotFoundError(ConfigError):
    """Raised when the configuration file cannot be found."""


class ConfigValidationError(ConfigError):
    """Raised when configuration validation fails.

    Parameters
    ----------
    message:
        Human-readable description of the problem.
    transport:
        Transport type involved (e.g. ``"matrix"``), if applicable.
    adapter_id:
        Adapter identifier involved, if applicable.
    section_path:
        Dot-separated config path like ``"adapters.matrix.main"``, if applicable.
    """

    def __init__(
        self,
        message: str = "",
        *,
        transport: str | None = None,
        adapter_id: str | None = None,
        section_path: str | None = None,
    ) -> None:
        self.transport = transport
        self.adapter_id = adapter_id
        self.section_path = section_path
        super().__init__(message)


class ConfigFileError(ConfigError):
    """Raised when the configuration file cannot be read or parsed."""
