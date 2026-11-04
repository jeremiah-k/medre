# medre-test-tier: compatibility
"""Core-authorized exact-target replay into a pinned LXMF local SDK peer.

The sender process executes ``ReplayEngine`` through ``PipelineRunner`` and
``LxmfAdapter``. The receiver remains a separate raw-SDK Reticulum instance.
This is a local-integration candidate, not hardware or external-network proof.
The test is intentionally gated and is not part of required CI until its full
native dependencies and process ownership are independently qualified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.lxmf_scenario import run_owned_scenario
from tests.integration.test_lxmf_exact_target_propagation import _RECEIVER_SOURCE

try:
    from medre.adapters.lxmf.compat import HAS_LXMF
except ImportError:
    HAS_LXMF = False

pytestmark = [
    pytest.mark.local_integration,
    pytest.mark.lxmf_sdk,
    pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership"),
    pytest.mark.skipif(not HAS_LXMF, reason="requires pinned LXMF/RNS SDKs"),
]

_PREFIX = "MEDRE_LXMF_CORE_REPLAY_RESULT="


async def test_core_replay_binds_one_lxmf_target_to_independent_recipient(
    tmp_path: Path,
) -> None:
    receiver = tmp_path / "receiver.py"
    receiver.write_text(_RECEIVER_SOURCE, encoding="utf-8")
    project_root = Path(__file__).resolve().parents[2]
    pythonpath = os.pathsep.join(
        part for part in (str(project_root), os.environ.get("PYTHONPATH")) if part
    )
    try:
        completed = await run_owned_scenario(
            [
                sys.executable,
                "-m",
                "tests.helpers.lxmf_core_replay_sender",
                str(tmp_path),
                str(receiver),
            ],
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": pythonpath},
            timeout=300.0,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("core-authorized native replay exceeded its process deadline")
    assert completed.returncode == 0, (
        f"core-native replay exited {completed.returncode}\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    matching = [
        line[len(_PREFIX) :]
        for line in completed.stdout.splitlines()
        if line.startswith(_PREFIX)
    ]
    assert len(matching) == 1, "native replay scenario must publish one verdict"
    evidence = json.loads(matching[0])
    assert evidence["scenario"] == "lxmf_core_authorized_exact_target_replay"
    assert evidence["tier"] == "local_integration"
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2] / ".github/adapter-compatibility.json"
        ).read_text(encoding="utf-8")
    )
    pinned = [row for row in manifest if row["transport"] == "lxmf"]
    assert len(pinned) == 1
    assert evidence["provider_repository"] == pinned[0]["repository"]
    assert evidence["compatibility_pin"] == pinned[0]["ref"]
    assert evidence["scope"] == "recipient_observation"
    assert evidence["result"] == "accepted"
    assert evidence["binding"] == "current"
    assert evidence["source"] == "replay"
    assert evidence["attempt_number"] >= 1
    assert evidence["native_id_matches_receiver"] is True
    assert evidence["destination_matches_receiver"] is True
    assert evidence["recipient_callback_observed"] is True
    assert evidence["sibling_deliveries"] == 0
    assert len(evidence["native_evidence_fingerprint"]) == 64
    assert (tmp_path / "b-ready.json").is_file()
    received = json.loads((tmp_path / "b-received.json").read_text(encoding="utf-8"))
    assert received["received"] is True
    assert received["delivery_count"] == 1
    verdict_path = tmp_path / "recipient-verdict.json"
    published = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert published["received"] is True
    assert published["body_matches"] is True
    assert published["native_id_matches"] is True
    assert published["evidence_fingerprint"] == evidence["native_evidence_fingerprint"]
    assert set(published) == {
        "received",
        "delivery_count",
        "body_matches",
        "native_id_matches",
        "destination_matches",
        "evidence_fingerprint",
    }
