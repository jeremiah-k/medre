"""Run native conversation qualification with independently provisioned peers."""

from __future__ import annotations

import time
from pathlib import Path

from ..fidelity import run as campaign
from ..plan import load_plan
from .sources import write_json


async def run(root: Path, *, presentation: bool = False) -> None:
    from scripts.analyze_fidelity_campaign import analyze

    from .resilience import fixture

    name = "presentation" if presentation else "fidelity"
    config = fixture(root, name)
    output = root / "results" / (name + "-" + str(time.time_ns()))
    await campaign(
        load_plan(config, only={"matrix", "xmpp"}), output, presentation=presentation
    )
    write_json(output / "analysis.json", await analyze(output))
