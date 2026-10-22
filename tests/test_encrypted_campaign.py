"""Reject native crypto false positives using expectations independent of delivery."""

from copy import deepcopy

import pytest

from scripts.adapter_campaign.encrypted_evidence import (
    CASES,
    PREFIX,
    example,
    verify_case,
    assert_no_crypto,
)

NONCE = "ME-0123456789ab-"


def evidence(name="forward_file"):
    """Construct one complete witness; callers mutate separate native/core facts."""
    _, source, media, generation = next(row for row in CASES if row[0] == name)
    target = "matrix_target" if source == "matrix" else "matrix"
    topology = {
        "matrix": dict(room="!source:lab", user_id="@a:lab", device_id="A"),
        "matrix_target": dict(room="!target:lab", user_id="@b:lab", device_id="B"),
        "peer": dict(user_id="@peer:lab", device_id="P"),
    }
    value = example(NONCE, name)
    shared = dict(
        event_id="canonical",
        delivery_plan_id="plan",
        target_adapter=target,
        target_channel=topology[target]["room"],
        route_id=f"{source}_to_{target}",
        status="sent",
        attempt_number=1,
        outbox_id="outbox",
        replay_run_id=None,
    )
    payload = {"body": value["text"]}
    if media:
        payload["attachment"] = {key: value[key] for key in ("filename", "size_bytes")}
        payload["attachment"].update(
            content_ref="sha256:" + value["sha256"],
            mime_type="application/octet-stream",
        )
    item = dict(
        event=dict(
            event_id="canonical",
            source_adapter=source,
            source_channel_id=topology[source]["room"],
            event_kind="message.file" if media else "message.created",
            payload=payload,
        ),
        receipts=[{**shared, "source": "live", "adapter_message_id": "$dest"}],
        outbox=[{**shared, "dispatch_source": "live"}],
        refs=[
            dict(direction="inbound", adapter=source, native_message_id="$source"),
            dict(direction="outbound", adapter=target, native_message_id="$dest"),
        ],
    )
    rows = [dict(kind="sent", native_id="$source", at=25 if generation else 15)]
    for endpoint, sender, device, native, text in (
        (source, "@peer:lab", "P", "$source", value["text"]),
        (
            target,
            topology[target]["user_id"],
            topology[target]["device_id"],
            "$dest",
            PREFIX + value["text"],
        ),
    ):
        record = dict(
            kind="received",
            room=topology[endpoint]["room"],
            sender=sender,
            device_id=device,
            native_id=native,
            text=text,
            decrypted=True,
            wire_type="m.room.encrypted",
            algorithm="m.megolm.v1.aes-sha2",
            wire_sha256="a" * 64,
        )
        if media:
            record["attachment"] = {
                key: value[key] for key in ("filename", "size_bytes", "sha256")
            }
            record["attachment"].update(
                mime_type="application/octet-stream", ciphertext_sha256="b" * 64
            )
        rows.append(record)
    case = dict(
        name=name,
        source=source,
        target=target,
        source_ack="$source",
        core=deepcopy(item),
    )
    return case, item, rows, topology


@pytest.mark.parametrize("name", [row[0] for row in CASES])
def test_native_witnesses_correlate_with_durable_routing(name):
    """Both directions, media and restart cases satisfy the same closed oracle."""
    case, item, rows, topology = evidence(name)
    verify_case(case, item, rows, topology, NONCE, [10, 20])


@pytest.mark.parametrize(
    "field,value",
    [
        ("room", "!wrong:lab"),
        ("sender", "@wrong:lab"),
        ("device_id", "WRONG"),
        ("decrypted", False),
        ("wire_type", "m.room.message"),
        ("algorithm", "unknown"),
        ("wire_sha256", ""),
        ("native_id", "$wrong"),
        ("text", "wrong caption"),
    ],
)
def test_encrypted_destination_cannot_pass_with_wrong_native_evidence(field, value):
    """A terminal outbox cannot establish native decryption or destination identity."""
    case, item, rows, topology = evidence()
    rows[-1][field] = value
    with pytest.raises(AssertionError):
        verify_case(case, item, rows, topology, NONCE, [10, 20])


@pytest.mark.parametrize(
    "key,value",
    [
        ("filename", "changed.bin"),
        ("sha256", "c" * 64),
        ("size_bytes", 1),
        ("mime_type", "text/plain"),
        ("ciphertext_sha256", ""),
    ],
)
def test_media_metadata_and_plaintext_hash_are_independent_of_core(key, value):
    """File success requires an independently decrypted, unchanged native attachment."""
    case, item, rows, topology = evidence()
    rows[-1]["attachment"][key] = value
    with pytest.raises(AssertionError):
        verify_case(case, item, rows, topology, NONCE, [10, 20])


def test_consistently_wrong_durable_channel_does_not_validate_itself():
    """Outbox and receipt agreement cannot replace configured channel authority."""
    case, item, rows, topology = evidence()
    for row in (*item["receipts"], *item["outbox"]):
        row["target_channel"] = "!wrong:lab"
    case["core"] = deepcopy(item)
    with pytest.raises(AssertionError, match="destination conversation"):
        verify_case(case, item, rows, topology, NONCE, [10, 20])


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "missing", "plaintext_upload", "wrong_generation", "missing_ack"],
)
def test_incomplete_or_ambiguous_native_evidence_is_not_success(mutation):
    """Drops, duplicates, clear media and misplaced acknowledgements fail closed."""
    case, item, rows, topology = evidence()
    if mutation == "duplicate":
        rows.append(deepcopy(rows[-1]))
    elif mutation == "missing":
        rows.pop()
    elif mutation == "plaintext_upload":
        rows[-1]["attachment"]["ciphertext_sha256"] = example(NONCE, case["name"])[
            "sha256"
        ]
    elif mutation == "wrong_generation":
        rows[0]["at"] = 21
    else:
        rows.pop(0)
    with pytest.raises(AssertionError):
        verify_case(case, item, rows, topology, NONCE, [10, 20])


@pytest.mark.parametrize(
    "nonce", ["", "ME-", "ME-000000000000-evil", "ME-00000000000g-"]
)
def test_input_oracle_rejects_weak_nonce(nonce):
    """Never permit empty or ambiguous native message correlation."""
    with pytest.raises(ValueError, match="nonce"):
        example(nonce, "forward_text")


@pytest.mark.parametrize("key", ["key", "iv", "ciphertext", "file", "url"])
def test_crypto_material_cannot_hide_in_nested_payload(key):
    """Canonical payloads retain opaque content references, never media secrets."""
    with pytest.raises(AssertionError, match="crypto material"):
        assert_no_crypto({"attachment": [{key: "secret"}]})


def retained_campaign(directory):
    """Build independent native streams and durable snapshots for analyzer attacks."""
    import json

    report = dict(
        version=1, status="passed", run_prefix=NONCE, cleanup_errors=[], cases=[]
    )
    peer = []
    snapshots = {}
    for name, _, _, _epoch in CASES:
        case, item, rows, topology = evidence(name)

        # Every scenario has independent native and canonical identifiers.
        def identify(value, name=name):
            """Give every scenario distinct correlation identities."""
            if isinstance(value, str):
                return {
                    "$source": "$source-" + name,
                    "$dest": "$dest-" + name,
                    "canonical": "canonical-" + name,
                }.get(value, value)
            if isinstance(value, dict):
                return {key: identify(child) for key, child in value.items()}
            if isinstance(value, list):
                return [identify(child) for child in value]
            return value

        case, item, rows = identify(case), identify(item), identify(rows)
        snapshots[example(NONCE, name)["text"]] = item
        report["cases"].append(case)
        peer.extend(rows)
    for name, entry in topology.items():
        entry["keys"] = dict(curve25519="curve-" + name, ed25519="ed-" + name)
    report["expectations"] = topology
    peer.insert(
        0,
        dict(
            kind="ready",
            at=5,
            identity=topology["peer"]["user_id"],
            device_id="P",
            keys=topology["peer"]["keys"],
        ),
    )
    peer.extend(
        dict(
            kind="sent",
            at=at,
            identities={name: row["keys"] for name, row in topology.items()},
        )
        for at in (11, 21, 30)
    )
    (directory / "results.json").write_text(json.dumps(report))
    (directory / "peer.jsonl").write_text("\n".join(json.dumps(row) for row in peer))
    for epoch in (0, 1):
        (directory / f"runtime-{epoch}.jsonl").write_text(
            json.dumps(
                dict(kind="ready", identity="encrypted-runtime", at=10 + epoch * 10)
            )
        )
    for name in ("runtime-0", "runtime-1", "peer"):
        for suffix in ("stdout", "stderr"):
            (directory / (name + "." + suffix)).write_text(
                "2026-10-22T12:00:00+0000 [INFO] runtime: ready\n"
                if name != "peer" and suffix == "stdout"
                else ""
            )
    return snapshots


@pytest.mark.parametrize(
    "attack",
    [
        None,
        "missing_capture",
        "resource_leak",
        "partial_cases",
        "device_changed",
        "wrong_bytes",
        "missing_runtime_ready",
        "extra_native_delivery",
        "nested_tuple_evidence",
    ],
)
async def test_offline_analysis_rejects_incomplete_or_contradictory_evidence(
    tmp_path, monkeypatch, attack
):
    """Reopen durable and native evidence; no saved passing verdict is sufficient."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from scripts import analyze_encrypted_campaign as module
    from scripts.adapter_campaign import recovery

    snapshots = retained_campaign(tmp_path)
    db = SimpleNamespace(close=AsyncMock())

    async def snapshot_reader(_, marker):
        """Exercise the real JSON-normalizing reader over storage-like values."""
        return [snapshots[marker]]

    async def retained(_, content_ref):
        """Return associated bytes or simulate retained content corruption."""
        data = next(
            example(NONCE, name)["data"]
            for name, _, media, _ in CASES
            if media and "sha256:" + example(NONCE, name)["sha256"] == content_ref
        )
        return SimpleNamespace(data=b"wrong" if attack == "wrong_bytes" else data)

    db.load_attachment_content = retained
    monkeypatch.setattr(
        module.SQLiteStorage, "open_readonly", AsyncMock(return_value=db)
    )
    monkeypatch.setattr(recovery, "snapshots", snapshot_reader)
    if attack == "nested_tuple_evidence":
        path = tmp_path / "results.json"
        value = json.loads(path.read_text())
        metadata = {"routing": {"route_trace": ("first", ("second",))}}
        for item in snapshots.values():
            item["event"]["metadata"] = deepcopy(metadata)
        for case in value["cases"]:
            case["core"]["event"]["metadata"] = deepcopy(metadata)
        path.write_text(json.dumps(value))
    elif attack == "missing_capture":
        (tmp_path / "runtime-1.stderr").unlink()
    elif attack == "resource_leak":
        (tmp_path / "peer.stderr").write_text("Unclosed client session\n")
    elif attack == "partial_cases":
        path = tmp_path / "results.json"
        value = json.loads(path.read_text())
        value["cases"].pop()
        path.write_text(json.dumps(value))
    elif attack == "device_changed":
        path = tmp_path / "peer.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["identities"]["matrix"]["curve25519"] = "rotated"
        path.write_text("\n".join(json.dumps(row) for row in rows))
    elif attack == "missing_runtime_ready":
        (tmp_path / "runtime-1.jsonl").write_text("")
    elif attack == "extra_native_delivery":
        path = tmp_path / "peer.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows.append(next(row for row in rows if row["kind"] == "received"))
        path.write_text("\n".join(json.dumps(row) for row in rows))
    if attack not in (None, "nested_tuple_evidence"):
        with pytest.raises((AssertionError, FileNotFoundError)):
            await module.analyze(tmp_path)
    else:
        assert (await module.analyze(tmp_path))["status"] == "passed"
        db.close.assert_awaited_once()
    if attack == "wrong_bytes":
        db.close.assert_awaited_once()


async def test_reader_normalizes_evidence_before_final_restart_comparison(monkeypatch):
    """Storage tuples and the JSON report compare equally through the real reader."""
    import json
    from unittest.mock import AsyncMock

    from scripts.adapter_campaign import recovery

    case, item, rows, topology = evidence()
    item["event"]["metadata"] = {"routing": {"route_trace": ("route", ("nested",))}}
    case["core"] = json.loads(json.dumps(item))
    monkeypatch.setattr(recovery, "snapshots", AsyncMock(return_value=[item]))
    current = await recovery.read_event(object(), example(NONCE, case["name"])["text"])
    assert current == case["core"] and current != item
    verify_case(case, current, rows, topology, NONCE, [10, 20])
