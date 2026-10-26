"""Historical evidence claims remain scoped and source pins stay authoritative."""

from __future__ import annotations

import json
from pathlib import Path


def test_native_evidence_index_names_provider_sources_without_copying_pins():
    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "docs/dev/native-evidence-index.json").read_text())
    manifest = json.loads((root / ".github/adapter-compatibility.json").read_text())
    assert index["source_authority"] == ".github/adapter-compatibility.json"
    assert index["frozen_baseline"]["directed_pairs"] == 42
    assert index["frozen_baseline"]["source_fixtures"] == 28
    assert index["frozen_baseline"]["native_receipts_reported"] == 168
    assert {item["transport"] for item in index["providers"]} == {
        item["transport"] for item in manifest
    }
    assert len(index["providers"]) == len(
        {item["transport"] for item in index["providers"]}
    )
    assert all(
        item["compatibility_ref_source"] == index["source_authority"]
        for item in index["providers"]
    )
    assert all("ref" not in item and "sha" not in item for item in index["providers"])
    assert all(
        (root / path).is_file() for path in index["frozen_baseline"]["source_documents"]
    )


def test_simplex_and_lxmf_claims_do_not_expand_beyond_historical_evidence():
    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "docs/dev/native-evidence-index.json").read_text())
    providers = {item["transport"]: item for item in index["providers"]}
    assert "direct-contact" in providers["simplexchat"]["outside_reported_scope"]
    assert "independent" in providers["simplexchat"]["reported_native_evidence"]
    assert "Docker TCP" in providers["lxmf"]["historical_substrate"]
    assert "physical RF" in providers["lxmf"]["outside_reported_scope"]
    assert all("outside_reported_scope" in provider for provider in providers.values())
    assert "private" in index["frozen_baseline"]["report_status"]


def test_native_recovery_observations_retain_their_confirmation_limits():
    """Physical witnesses and restored native state do not invent recipient ACKs."""
    root = Path(__file__).resolve().parents[1]
    index = json.loads((root / "docs/dev/native-evidence-index.json").read_text())
    run = next(
        item
        for item in index["qualification_runs"]
        if item["id"] == "native-recovery-2026-10-26"
    )
    for transport in ("meshtastic", "meshcore"):
        observed = run["observations"][transport]
        assert observed["safe_continuation"]["accepted_before_crash"] == 1
        assert observed["safe_continuation"]["native_calls_after_restart"] == 2
        assert observed["uncertain_handoff"]["native_calls_after_restart"] == 0
        assert observed["uncertain_handoff"]["parent_status"] == "dead_lettered"
    assert run["observations"]["simplexchat"]["recipient_evidence"] is False
    assert run["observations"]["stopped_restore"]["providers"] == [
        "core",
        "simplexchat",
    ]
    assert (
        run["observations"]["stopped_restore"]["confirmation_ceiling"] == "local_queue"
    )
    assert len(run["artifact"]["sha256"]) == 64
    assert "not persisted recipient ACKs" in run["interpretation"]
