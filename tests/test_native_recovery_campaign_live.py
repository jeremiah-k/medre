# medre-test-tier: compatibility
"""Opt-in process death and transport outage qualification with native peers."""

import os
from pathlib import Path

import pytest

from scripts.adapter_campaign.plan import load_plan
from scripts.adapter_campaign.recovery import run
from scripts.analyze_recovery_campaign import analyze

pytestmark = [pytest.mark.live, pytest.mark.timeout(1000)]


async def test_native_process_and_transport_recovery(tmp_path):
    config = os.environ.get("MEDRE_RECOVERY_CAMPAIGN")
    if not config:
        pytest.skip(
            "Set MEDRE_RECOVERY_CAMPAIGN to a private Matrix/XMPP campaign YAML"
        )
    output = Path(
        os.environ.get("MEDRE_RECOVERY_OUTPUT", str(tmp_path / "recovery-campaign"))
    )
    report = await run(load_plan(Path(config), only={"matrix", "xmpp"}), output)
    assert report["status"] == "passed"
    assert (await analyze(output))["status"] == "passed"
