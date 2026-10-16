"""Shared adapter configuration validation base class."""


class AdapterConfigError(ValueError):
    """An installed adapter rejected its configuration."""
