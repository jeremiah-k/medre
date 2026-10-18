# medre-test-tier: compatibility
"""Explicitly gated native peers → MEDRE → native peers qualification."""

import os
from pathlib import Path

import pytest

from scripts.adapter_campaign.plan import load_plan
from scripts.adapter_campaign.run import run
from tests.helpers.live_harness import PINNED_SDK_UNRAISABLE_FILTERS

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.timeout(900),
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
    pytest.mark.filterwarnings(
        r"ignore:setDaemon\(\) is deprecated:DeprecationWarning"
    ),
    *(pytest.mark.filterwarnings(spec) for spec in PINNED_SDK_UNRAISABLE_FILTERS),
]


async def test_native_messages_across_selected_adapters(tmp_path):
    fixture = os.environ.get("MEDRE_ADAPTER_CAMPAIGN")
    if not fixture:
        pytest.skip("Set MEDRE_ADAPTER_CAMPAIGN to a private campaign YAML")
    selected = os.environ.get("MEDRE_ADAPTER_CAMPAIGN_ONLY")
    plan = load_plan(Path(fixture), set(selected.split(",")) if selected else None)
    output = Path(
        os.environ.get(
            "MEDRE_ADAPTER_CAMPAIGN_OUTPUT", str(tmp_path / "native-campaign")
        )
    )
    report = await run(plan, output.resolve())
    assert report["status"] == "passed"
    assert len(report["completed_pairs"]) == len(plan.pairs)
