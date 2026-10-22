"""SDK-free oracles for encrypted messages, retained media, and restart evidence."""

from __future__ import annotations

import hashlib
import re

from .evidence import verify

CASES = (
    ("forward_text", "matrix", False, 0),
    ("reverse_text", "matrix_target", False, 0),
    ("forward_file", "matrix", True, 0),
    ("reverse_file", "matrix_target", True, 0),
    ("restart_text", "matrix", False, 1),
    ("restart_file", "matrix_target", True, 1),
)
PREFIX = "[encrypted relay] "


def example(prefix: str, name: str) -> dict:
    """Derive immutable input expectations from a validated run nonce and case."""
    if not re.fullmatch(r"ME-[0-9a-f]{12}-", prefix):
        raise ValueError("Invalid encrypted campaign nonce")
    if name not in {case[0] for case in CASES}:
        raise ValueError("Unknown encrypted scenario")
    text = prefix + name + " café 🦉"
    data = (text + "\n").encode() + bytes(range(256)) * 8
    return dict(
        text=text,
        filename=name + ".bin",
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def verify_case(
    case: dict,
    item: dict,
    records: list[dict],
    topology: dict,
    prefix: str,
    ready: list[float],
) -> None:
    """Check evidence against configured identities and independent input bytes.

    Both native events must decrypt, belong to the configured rooms/devices,
    and correlate with ingress and terminal durable delivery. Raise on missing,
    duplicate, contradictory, or cross-generation evidence.
    """
    name, source, media, generation = next(
        row for row in CASES if row[0] == case["name"]
    )
    target = "matrix_target" if source == "matrix" else "matrix"
    expected = example(prefix, name)
    assert case["source"] == source and case["target"] == target
    assert case["core"] == item, "Durable evidence changed after capture"
    verify(
        item,
        source=source,
        channel=topology[source]["room"],
        targets={target: topology[target]["room"]},
    )
    event = item["event"]
    assert event["event_kind"] == ("message.file" if media else "message.created")
    assert event["payload"]["body"] == expected["text"]
    ack = [
        r
        for r in records
        if r["kind"] == "sent" and r.get("native_id") == case["source_ack"]
    ]
    assert len(ack) == 1 and case["source_ack"], "Missing native source acknowledgement"
    assert ack[0]["at"] > ready[generation]
    if generation == 0 and len(ready) == 2:
        assert ack[0]["at"] < ready[1], "Initial case escaped its runtime generation"
    inbound = [r for r in item["refs"] if r["direction"] == "inbound"]
    assert any(
        r["adapter"] == source and r["native_message_id"] == case["source_ack"]
        for r in inbound
    )
    sent = next(r for r in item["receipts"] if r["status"] == "sent")
    assert sent["adapter_message_id"], "Missing native destination acknowledgement"
    hits = [
        r for r in records if r["kind"] == "received" and expected["text"] in r["text"]
    ]
    assert len(hits) == 2, "Expected exactly one decrypted source and destination"
    for endpoint, sender, device, native_id, text in (
        (
            source,
            topology["peer"]["user_id"],
            topology["peer"]["device_id"],
            case["source_ack"],
            expected["text"],
        ),
        (
            target,
            topology[target]["user_id"],
            topology[target]["device_id"],
            sent["adapter_message_id"],
            PREFIX + expected["text"],
        ),
    ):
        matching = [r for r in hits if r["room"] == topology[endpoint]["room"]]
        assert len(matching) == 1, "Native event reached the wrong room"
        record = matching[0]
        assert record["sender"] == sender and record["device_id"] == device
        assert record["native_id"] == native_id and record["text"] == text
        assert record["decrypted"] is True and record["wire_type"] == "m.room.encrypted"
        assert record["algorithm"] == "m.megolm.v1.aes-sha2"
        assert re.fullmatch("[0-9a-f]{64}", record["wire_sha256"])
        if media:
            attachment = record["attachment"]
            for key in ("filename", "size_bytes", "sha256"):
                assert (
                    attachment[key] == expected[key]
                ), "Native media differs from input bytes"
            assert attachment["mime_type"] == "application/octet-stream"
            assert re.fullmatch("[0-9a-f]{64}", attachment["ciphertext_sha256"])
            assert attachment["ciphertext_sha256"] != expected["sha256"]
        else:
            assert "attachment" not in record
    if media:
        descriptor = event["payload"]["attachment"]
        assert descriptor["content_ref"] == "sha256:" + expected["sha256"]
        assert descriptor["filename"] == expected["filename"]
        assert descriptor["size_bytes"] == expected["size_bytes"]
        assert descriptor["mime_type"] == "application/octet-stream"
        assert not descriptor.get("unavailable_reason")
    assert_no_crypto(event["payload"])


def assert_no_crypto(value) -> None:
    """Reject transient Matrix encryption material in canonical payloads."""
    if isinstance(value, dict):
        assert (
            not {"key", "iv", "ciphertext", "file", "url"} & value.keys()
        ), "Transient crypto material was persisted"
        for child in value.values():
            assert_no_crypto(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_crypto(child)
