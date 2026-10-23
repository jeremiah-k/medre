"""The lab probes and provisions the same independent native connection roles."""

import sys
from itertools import product
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from scripts.adapter_campaign.lab import hardware, provision, control
from scripts.adapter_campaign.lab.sources import write_json
from scripts.adapter_campaign.meshtastic_connection import (
    connection_config,
    role_connection,
)


@pytest.mark.parametrize(
    "gateway,peer", tuple(product(("serial_port", "ble_address", "host"), repeat=2))
)
async def test_lab_probes_and_provisions_independent_transports(
    tmp_path, monkeypatch, gateway, peer
):
    """Serial/BLE/TCP choices survive inspection and generated runtime/peer config."""
    for name in ("profiles", "results", "logs"):
        (tmp_path / name).mkdir()
    selectors = {
        "serial_port": "/dev/radio",
        "ble_address": "AA:BB:CC:DD:EE:FF",
        "host": "radio.test",
    }
    settings = {
        f"gateway_{gateway}": selectors[gateway],
        f"peer_{peer}": selectors[peer],
        "channel": 0,
    }
    write_json(tmp_path / "hardware.json", {"meshtastic": settings})
    opened = []

    def open_interface(connection, *, timeout):
        opened.append(connection)
        number = len(opened)
        return SimpleNamespace(
            myInfo=SimpleNamespace(my_node_num=number),
            localNode=SimpleNamespace(channels=[], localConfig=object()),
            getMyNodeInfo=lambda: {"user": {"longName": f"node-{number}"}},
            close=Mock(),
        )

    monkeypatch.setattr(hardware, "open_interface", open_interface)
    monkeypatch.setitem(
        sys.modules,
        "google.protobuf.json_format",
        SimpleNamespace(MessageToDict=lambda _: {}),
    )
    await hardware.inspect_hardware(tmp_path)
    assert opened == [role_connection(settings, role) for role in ("gateway", "peer")]
    fixtures = {
        name: {
            "status": "ready",
            "runtime": {},
            "endpoint": {
                "transport": name,
                "channel": "fixture",
                "expected_sender": "fixture",
                "peer": {},
            },
        }
        for name in ("matrix", "xmpp", "chatmail", "simplexchat", "lxmf")
    }
    write_json(tmp_path / "results/provision.json", fixtures)
    monkeypatch.setattr(control, "command", Mock())
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, "fixture")
    await provision.campaign(tmp_path)
    runtime = yaml.safe_load((tmp_path / "runtime.yaml").read_text())
    campaign = yaml.safe_load((tmp_path / "campaign.yaml").read_text())
    config = runtime["adapters"]["meshtastic"]["meshtastic"]
    expected = connection_config(role_connection(settings, "gateway"))
    assert {key: config[key] for key in expected} == expected
    assert campaign["endpoints"]["meshtastic"]["peer"] == role_connection(
        settings, "peer"
    )


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"host": "radio", "serial_port": "/dev/radio"},
        {"ble_address": ""},
        {"host": "radio", "port": True},
    ],
)
def test_invalid_connection_is_rejected_before_native_construction(settings):
    """Malformed or ambiguous selectors cannot quietly choose a different radio."""
    with pytest.raises(ValueError):
        connection_config(settings)
