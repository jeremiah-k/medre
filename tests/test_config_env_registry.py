"""Registry-driven environment configuration contract tests."""

from __future__ import annotations

import pytest

from medre.adapter_registry import registered_transports
from medre.config.env import _REJECTED_TRANSPORT_PREFIXES, MedreEnvConfig
from medre.config.errors import ConfigValidationError


def test_rejected_legacy_transport_prefixes_follow_registry() -> None:
    """Legacy transport env rejection derives from registered transports."""
    expected = tuple(
        f"MEDRE_{transport.upper().replace('-', '_')}_"
        for transport in registered_transports()
    )
    assert _REJECTED_TRANSPORT_PREFIXES == expected


@pytest.mark.parametrize("transport", registered_transports())
def test_registered_transport_legacy_env_prefix_is_rejected(transport: str) -> None:
    """Every registered transport legacy prefix is rejected by the parser."""
    prefix = f"MEDRE_{transport.upper().replace('-', '_')}_"
    with pytest.raises(ConfigValidationError, match="Unsupported transport env"):
        MedreEnvConfig.from_environ({f"{prefix}ENABLED": "1"})
