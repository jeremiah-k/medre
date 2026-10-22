"""Use the lab's independent native accounts for process and network recovery."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml

from ..plan import load_plan
from ..recovery import run as campaign
from .sources import write_json


def fixture(root: Path, campaign_name: str) -> Path:
    """Prepare a private Matrix/XMPP campaign from provisioned identities."""
    fixtures = json.loads((root / "results/provision.json").read_text())
    for name in ("matrix", "xmpp"):
        if fixtures.get(name, {}).get("status") != "ready":
            raise ValueError(
                "Qualify OAuth and provision Matrix/XMPP before native qualification"
            )
    runtime = {"adapters": {}}
    endpoints = {}
    for name in ("matrix", "xmpp"):
        runtime["adapters"][name] = {
            name: {
                "enabled": True,
                "adapter_kind": "real",
                "origin_label": name.capitalize() + " lab",
                **fixtures[name]["runtime"],
            }
        }
        endpoints[name] = {**fixtures[name]["endpoint"]}
    endpoints["xmpp"]["expected_prefixes"] = {"matrix": "[Matrix lab] Peer: "}
    endpoints["matrix"]["expected_prefixes"] = {"xmpp": "[peer/Xmpp lab]: "}
    (root / (campaign_name + "-runtime.yaml")).write_text(
        yaml.safe_dump(runtime, sort_keys=False)
    )
    config = root / (campaign_name + "-campaign.yaml")
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "runtime_config": campaign_name + "-runtime.yaml",
                "timeout_seconds": 90,
                "settle_seconds": 5,
                "endpoints": endpoints,
            },
            sort_keys=False,
        )
    )
    profile = root / "profiles/medre"
    os.environ.update(
        XDG_CONFIG_HOME=str(profile / "config"),
        XDG_STATE_HOME=str(profile / "state"),
        XDG_DATA_HOME=str(profile / "data"),
    )
    return config


async def run(root: Path) -> None:
    """Run and analyze bounded recovery with provisioned Matrix/XMPP peers."""
    from scripts.analyze_recovery_campaign import analyze

    config = fixture(root, "resilience")
    output = root / "results" / ("resilience-" + str(time.time_ns()))
    await campaign(load_plan(config, only={"matrix", "xmpp"}), output)
    write_json(output / "analysis.json", await analyze(output))
