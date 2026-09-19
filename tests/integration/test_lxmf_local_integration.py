"""Process-isolated local integration tests for real LXMF/RNS lifecycle.

Evidence scope: these tests exercise the pinned LXMF/RNS SDKs on this
machine over a loopback-only Reticulum UDPInterface pair.  They are
local-SDK evidence — not simulated unit evidence, and not external/
live-network or hardware evidence.

The SDK prerequisite is the pinned optional extra ``medre[lxmf]``.
`pyproject.toml` owns the declared versions and `uv.lock` records the resolved
artifacts; without the extra the module is skipped rather than failing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from medre.adapters.lxmf.compat import HAS_LXMF
from tests.helpers.sdk_contract import declared_extra_pins

pytestmark = [
    pytest.mark.local_integration,
    pytest.mark.lxmf_sdk,
    pytest.mark.skipif(
        not HAS_LXMF,
        reason="requires the pinned lxmf/rns SDKs (pip install 'medre[lxmf]')",
    ),
]


def _run_probe(tmp_path: Path, scenario: str, timeout: float = 45) -> dict[str, object]:
    workdir = tmp_path / scenario
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.helpers.lxmf_local_probe",
            scenario,
            str(workdir),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        pytest.fail(
            "LXMF local integration probe failed "
            f"with exit code {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )

    prefix = "MEDRE_LOCAL_INTEGRATION_RESULT="
    result_line = next(
        (
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(prefix)
        ),
        None,
    )
    if result_line is None:
        pytest.fail(
            "LXMF local integration probe did not emit a result line\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )
    return json.loads(result_line[len(prefix) :])


def test_real_router_repeated_lifecycle_persistence_and_failure_cleanup(
    tmp_path: Path,
) -> None:
    result = _run_probe(tmp_path, "suite")
    assert result == {
        "callback_count": 0,
        "cycles": 3,
        "stable_destination": True,
        "startup_failure": "LxmfConnectionError",
    }


@pytest.mark.soak
def test_real_router_local_soak_preserves_identity_across_restarts(
    tmp_path: Path,
) -> None:
    result = _run_probe(tmp_path, "soak")
    assert result == {"cycles": 10, "stable_destination": True}


def test_relation_preserved_across_two_local_instances(tmp_path: Path) -> None:
    """A MEDRE relation survives a real two-process LXMF roundtrip.

    Instance A (sender process) renders a relation-bearing canonical
    event via ``LxmfRenderer`` — MEDRE envelope under LXMF
    ``FIELD_CUSTOM_META`` (``0xFD``) — and delivers it through its real
    ``LxmfAdapter``/``LXMRouter`` over a loopback Reticulum
    ``UDPInterface`` pair.  Distinct process B receives through the real
    inbound adapter/codec path.  Asserted separately from receipt:

    - decoded semantic equality: body, title, relation kind, referenced
      canonical event id, and referenced native identity;
    - cross-instance identity: B's decoded ``source_transport_id`` is
      A's registered delivery destination hash, and the LXMF message
      hash A received from ``deliver()`` equals the message id B
      decoded (content-addressed identity preserved end to end);
    - runtime SDK versions match the pins declared in ``pyproject.toml``.
    """
    result = _run_probe(tmp_path, "relation", timeout=150)
    pins = declared_extra_pins("lxmf")
    assert result == {
        "b_received": True,
        "b_content_match": True,
        "b_title_match": True,
        "b_envelope_event_id_match": True,
        "b_relations_match": True,
        "b_source_is_a": True,
        "a_native_id_matches_b_message_id": True,
        "lxmf_version": pins["lxmf"].version,
        "rns_version": pins["rns"].version,
    }
