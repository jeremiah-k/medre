"""Configuration error hierarchy for MEDRE runtime configuration.

All configuration-related errors inherit from :class:`ConfigError` so
callers can catch the base class or a specific subclass as needed.
"""


class ConfigError(Exception):
    """Base exception for all configuration errors."""


class ConfigNotFoundError(ConfigError):
    """Raised when the configuration file cannot be found."""


class ConfigValidationError(ConfigError):
    """Raised when configuration validation fails."""


class ConfigFileError(ConfigError):
    """Raised when the configuration file cannot be read or parsed."""
