"""Exercise radio-matrix collection gates without opening physical devices."""

from __future__ import annotations

import importlib
import runpy
from pathlib import Path

import pytest

from medre.adapters.lxmf import compat
from tests.helpers import docker_probe

_MATRIX_TEST = Path(__file__).with_name("test_radio_message_matrix_live.py")
_REQUIRED_ENV = (
    "MEDRE_RADIO_MATRIX",
    "MESHTASTIC_MEDRE_SERIAL_PORT",
    "MESHTASTIC_PEER_SERIAL_PORT",
    "MESHCORE_MEDRE_BLE_ADDRESS",
    "MESHCORE_PEER_BLE_ADDRESS",
    "LXMF_MEDRE_RNS_CONFIG",
    "LXMF_MEDRE_IDENTITY",
    "LXMF_PEER_RNS_CONFIG",
    "LXMF_PEER_IDENTITY",
)


@pytest.mark.parametrize("missing", [*_REQUIRED_ENV, None])
def test_lxmf_matrix_requires_opt_in_and_every_endpoint(
    monkeypatch: pytest.MonkeyPatch, missing: str | None,
) -> None:
    """An installed SDK must not bypass any owned-endpoint collection gate."""
    # Peer helpers capture endpoint settings at import. Load them before the
    # synthetic environment so their cached globals retain the real settings.
    importlib.import_module("tests.helpers.meshcore_live_peer")
    importlib.import_module("tests.helpers.meshtastic_live_peer")
    for name in _REQUIRED_ENV:
        monkeypatch.setenv(name, "1" if name == "MEDRE_RADIO_MATRIX" else "test-endpoint")
    if missing is not None:
        monkeypatch.delenv(missing)
    monkeypatch.setenv("LXMF_PEER_DELIVERY_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("MEDRE_MATRIX_TRAFFIC", "24")
    monkeypatch.setattr(compat, "HAS_LXMF", True)
    monkeypatch.setattr(docker_probe, "HAS_DOCKER", False)

    namespace = runpy.run_path(str(_MATRIX_TEST))
    test = namespace["test_lxmf_fourth_transport_relay"]
    skipped = any(
        mark.args[0] for mark in test.pytestmark if mark.name == "skipif"
    )
    assert skipped is (missing is not None), f"collection gate failed for {missing}"
