"""Native fixture and campaign orchestration inside the disposable runner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .control import command
from .budgets import NESTED_STAGE_SECONDS, stage_timeout
from .hardware import inspect_hardware
from .sources import write_json

ROOT = Path("/lab")


async def run(stage: str) -> None:
    if stage in {"all", "native"}:
        failures = []
        stages = (["contracts"] if stage == "all" else []) + [
            "oauth",
            "provision",
            "campaign",
            "mmrelay",
            "recovery",
            "logs",
        ]
        outcomes = {}
        for child in stages:
            if child == "mmrelay" and "meshtastic" not in json.loads(
                (ROOT / "hardware.json").read_text()
            ):
                outcomes[child] = "not_configured"
                continue
            try:
                command(
                    ROOT,
                    child,
                    [
                        sys.executable,
                        "-m",
                        "scripts.adapter_campaign.lab.container",
                        child,
                    ],
                    timeout=(
                        stage_timeout(
                            "contracts",
                            len(json.loads((ROOT / "sources.json").read_text())),
                        )
                        if child == "contracts"
                        else NESTED_STAGE_SECONDS
                    ),
                )
                outcomes[child] = "passed"
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append(child)
                outcomes[child] = "failed"
        coverage_path = ROOT / "results/coverage.json"
        coverage = (
            json.loads(coverage_path.read_text())["coverage"]
            if coverage_path.exists()
            else "unqualified"
        )
        write_json(
            ROOT / "results/qualification.json",
            {
                "status": (
                    "failed"
                    if failures
                    else ("passed" if coverage == "full" else "partial")
                ),
                "coverage": coverage,
                "stages": outcomes,
            },
        )
        if failures:
            raise RuntimeError("Lab stages failed: " + ", ".join(failures))
    elif stage == "oauth":
        failures = []
        for owner in ("medre", "mmrelay"):
            python = "/opt/mmrelay/bin/python" if owner == "mmrelay" else sys.executable
            try:
                command(
                    ROOT,
                    f"oauth-{owner}",
                    [
                        python,
                        "/sources/medre/scripts/adapter_campaign/lab/oauth_probe.py",
                        owner,
                    ],
                    timeout=900,
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append(owner)
        if failures:
            raise RuntimeError("Native OAuth failed: " + ", ".join(failures))
    elif stage == "provision":
        await inspect_hardware(ROOT)
        from .provision import provision

        await provision(ROOT)
    elif stage == "campaign":
        from .provision import campaign

        await campaign(ROOT)
    elif stage == "mmrelay":
        from .mmrelay_probe import run

        await run(ROOT)
    elif stage == "recovery":
        failures = []
        for owner in ("medre", "mmrelay"):
            python = "/opt/mmrelay/bin/python" if owner == "mmrelay" else sys.executable
            try:
                command(
                    ROOT,
                    "recovery-" + owner,
                    [
                        python,
                        "/sources/medre/scripts/adapter_campaign/lab/recovery_probe.py",
                        owner,
                    ],
                    timeout=600,
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append(owner)
        if failures:
            raise RuntimeError("Native recovery failed: " + ", ".join(failures))
    elif stage == "logs":
        from .log_review import review

        review(ROOT)
    else:
        # An unimplemented qualification can never become a green placeholder.
        raise NotImplementedError(f"Native {stage} qualification is not implemented")
