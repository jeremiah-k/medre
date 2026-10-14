"""Transport independence when no adapter entry points are installed."""

import os
import subprocess
import sys
from pathlib import Path


def test_config_and_runtime_import_without_adapter_distributions() -> None:
    """Core imports and configuration remain meaningful without providers."""
    program = """
import importlib.metadata
from unittest.mock import patch
with patch.object(importlib.metadata, 'entry_points', return_value=()):
    from medre.adapter_registry import registered_transports
    from medre.config.model import RuntimeConfig, GenericAdapterRuntimeConfig
    from medre.config.loader import load_config
    from medre.runtime.builder import RuntimeBuilder
    from medre.cli import main
    assert registered_transports() == ()
    assert RuntimeConfig and GenericAdapterRuntimeConfig and load_config and RuntimeBuilder and main
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_core_no_longer_embeds_transport_config_and_fakes() -> None:
    src = Path(__file__).resolve().parents[1] / "src/medre"
    for name in ("matrix", "meshtastic", "meshcore", "lxmf"):
        assert not (src / "config/adapters" / f"{name}.py").exists()
        assert not (src / "adapters/fakes" / f"{name}.py").exists()
