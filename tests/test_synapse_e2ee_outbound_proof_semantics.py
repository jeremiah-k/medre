"""Isolated verdict and observation contracts for the required Docker proof.

The production-shaped rig still requires the real Docker qualification. These
checks execute the acceptance module's actual helpers without importing nio.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SOURCE = Path(__file__).parent / "integration/test_synapse_e2ee_outbound_acceptance.py"


def _load_helpers() -> dict[str, Any]:
    source = ast.parse(_SOURCE.read_text())
    names = {
        "ObservedEvent",
        "OutboundAcceptanceEvidence",
        "evaluate_outbound_acceptance",
        "_record_peer_sync",
        "_peer_observation_store",
    }
    nodes = [
        node
        for node in source.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in nodes} == names
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    context = {"dataclass": dataclass, "field": field, "Any": Any}
    exec(
        compile(ast.fix_missing_locations(module), str(_SOURCE), "exec"), context
    )  # nosec B102 - executes the acceptance module's own extracted helper definitions
    return context


_HELPERS = _load_helpers()


def _evidence(kind: str = "attempt") -> Any:
    cls = _HELPERS["OutboundAcceptanceEvidence"]
    observed = _HELPERS["ObservedEvent"]
    return cls(
        event_id="e-1",
        target_adapter="matrix",
        target_room_id="!room:test",
        expected_body="verified",
        outbox_rows=({"status": "sent", "outbox_id": "o-1"},),
        receipts=(
            {
                "status": "sent",
                "receipt_kind": kind,
                "event_id": "e-1",
                "outbox_id": "o-1",
                "adapter_message_id": "$ciphertext",
                "confirmation_level": "remote_service",
            },
        ),
        outbound_refs=(
            {
                "adapter": "matrix",
                "direction": "outbound",
                "native_message_id": "$ciphertext",
                "native_channel_id": "!room:test",
            },
        ),
        observations=(observed("$ciphertext", "verified", "!room:test", True),),
    )


def test_positive_verdict_requires_explicit_attempt_kind() -> None:
    evaluate = _HELPERS["evaluate_outbound_acceptance"]
    assert evaluate(_evidence()) == []
    assert "attempt_receipt_missing" in evaluate(_evidence(kind=None))
    assert "attempt_receipt_missing" in evaluate(_evidence(kind="final"))


def _sync(*events: Any) -> Any:
    return SimpleNamespace(
        rooms=SimpleNamespace(
            join={
                "!room:test": SimpleNamespace(timeline=SimpleNamespace(events=events))
            }
        )
    )


def test_initial_ciphertext_is_observed_and_later_key_can_upgrade_it() -> None:
    ciphertext = type("MegolmEvent", (), {})()
    ciphertext.event_id = "$ciphertext"
    ciphertext.source = {"content": {"ciphertext": "encrypted"}}
    decrypted = type("RoomMessageText", (), {})()
    decrypted.event_id = "$ciphertext"
    decrypted.decrypted = True
    decrypted.source = {"content": {"body": "verified"}}

    class Peer:
        key_arrived = False

        def decrypt_event(self, event: Any) -> Any:
            assert event is ciphertext
            if not self.key_arrived:
                raise LookupError("Megolm key unavailable")
            return decrypted

    peer = Peer()
    record = _HELPERS["_record_peer_sync"]
    seen = _HELPERS["_peer_observation_store"]
    initial = record(peer, "!room:test", _sync(ciphertext))
    assert len(initial) == 1 and initial[0].decrypted is False
    assert seen(peer)["$ciphertext"]["kind"] == "MegolmEvent"
    peer.key_arrived = True
    upgraded = record(peer, "!room:test", _sync())
    assert len(upgraded) == 1 and upgraded[0].decrypted is True
    assert upgraded[0].body == "verified"
    assert seen(peer)["$ciphertext"]["kind"] == "RoomMessageText"
    assert not peer._medre_pending_ciphertexts
