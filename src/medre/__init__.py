"""MEDRE shared core; extend path for independently installed adapters."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
