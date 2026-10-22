# medre-test-tier: compatibility
"""Explicit native reply and restart qualification with dedicated accounts."""

import os
from pathlib import Path

import pytest

from scripts.adapter_campaign.fidelity import run
from scripts.adapter_campaign.plan import load_plan
from scripts.analyze_fidelity_campaign import analyze

# Ten 90-second cases plus separately bounded peer starts, restart, and cleanup.
pytestmark = [pytest.mark.live, pytest.mark.timeout(1800)]


async def test_native_matrix_xmpp_conversation_fidelity(tmp_path):
    config = os.environ.get("MEDRE_FIDELITY_CAMPAIGN")
    if not config:
        pytest.skip("Set MEDRE_FIDELITY_CAMPAIGN to a dedicated Matrix/XMPP fixture")
    output = Path(os.environ.get("MEDRE_FIDELITY_OUTPUT", str(tmp_path / "fidelity")))
    assert (await run(load_plan(Path(config), only={"matrix", "xmpp"}), output))[
        "status"
    ] == "passed"
    assert (await analyze(output))["status"] == "passed"
