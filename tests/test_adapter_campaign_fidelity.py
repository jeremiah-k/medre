"""Native fidelity uses independent peers, scoped IDs, and committed evidence."""

import io
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from scripts.adapter_campaign import worker
from scripts.adapter_campaign.fidelity_evidence import (
    message_body,
    native_hits,
    verify_case,
)
from scripts.analyze_fidelity_campaign import analyze, main


def fixture():
    case = {
        "name": "parent",
        "marker": "nonce ",
        "text": "nonce café 🦉",
        "source": "matrix",
        "target": "xmpp",
        "parent": None,
        "mode": "native",
        "native_target": None,
        "source_ack": "$source",
    }
    topology = {
        "matrix": {
            "transport": "matrix",
            "channel": "!room:example.org",
            "peer_identity": "@peer:example.org",
            "bot_identity": "@bot:example.org",
            "prefixes": {"xmpp": "[peer]: "},
        },
        "xmpp": {
            "transport": "xmpp",
            "channel": "room@conference.example.org",
            "peer_identity": "room@conference.example.org/peer",
            "bot_identity": "room@conference.example.org/bot",
            "prefixes": {"matrix": "[Peer]: "},
        },
    }
    source = {
        "kind": "received",
        "sender": "@peer:example.org",
        "channel": "!room:example.org",
        "text": case["text"],
        "native_id": "$source",
        "native_details": {"reply_to": None},
    }
    target = {
        "kind": "received",
        "sender": topology["xmpp"]["bot_identity"],
        "channel": topology["xmpp"]["channel"],
        "text": "[Peer]: " + case["text"],
        "native_id": "client-id",
        "native_details": {"room_stanza_id": "room-id", "reply_to": None},
    }
    fields = {
        "event_id": "event",
        "delivery_plan_id": "plan",
        "outbox_id": "outbox",
        "route_id": "matrix_to_xmpp",
        "target_adapter": "xmpp",
        "target_channel": topology["xmpp"]["channel"],
        "attempt_number": 1,
        "receipt_id": "receipt",
        "replay_run_id": None,
        "status": "sent",
    }
    item = {
        "event": {
            "event_kind": "message.created",
            "schema_version": 1,
            "event_id": "event",
            "payload": {"body": case["text"]},
            "source_adapter": "matrix",
            "source_transport_id": source["sender"],
            "source_channel_id": source["channel"],
            "source_native_ref": {
                "adapter": "matrix",
                "native_channel_id": source["channel"],
                "native_message_id": "$source",
            },
            "relations": [],
        },
        "outbox": [
            {
                **fields,
                "dispatch_source": "live",
                "worker_id": None,
                "active_attempt": None,
            }
        ],
        "receipts": [{**fields, "source": "live", "adapter_message_id": "room-id"}],
        "refs": [
            {
                "adapter": "xmpp",
                "native_channel_id": target["channel"],
                "native_message_id": "room-id",
                "event_id": "event",
                "direction": "outbound",
            }
        ],
    }
    return case, topology, source, target, item


def test_parent_verifies_native_room_scope_instead_of_local_client_id():
    case, topology, source, target, item = fixture()
    assert verify_case(case, item, [source], [target], topology, {}) == {
        "source_id": "$source",
        "target_id": "room-id",
    }


@pytest.mark.parametrize("broken_html", [False, True])
def test_xmpp_source_ack_uses_origin_id_even_when_the_stanza_id_is_rewritten(
    broken_html,
):
    case, topology, source, target, item = fixture()
    case.update(source="xmpp", target="matrix", source_ack="peer-origin")
    source.update(
        sender=topology["xmpp"]["peer_identity"],
        channel=topology["xmpp"]["channel"],
        native_id="rewritten-by-server",
        native_details={
            "origin_id": "peer-origin",
            "room_stanza_id": "source-room-id",
            "reply_to": None,
        },
    )
    target.update(
        sender=topology["matrix"]["bot_identity"],
        channel=topology["matrix"]["channel"],
        text="[peer]: " + case["text"],
        native_id="$target",
        native_details={"reply_to": None},
        formatted_body="<p>[peer]: " + case["text"] + "</p>",
    )
    item["event"].update(
        source_adapter="xmpp",
        source_channel_id=source["channel"],
        source_transport_id=source["sender"],
        source_native_ref={
            "adapter": "xmpp",
            "native_channel_id": source["channel"],
            "native_message_id": "source-room-id",
        },
    )
    for row in (*item["outbox"], *item["receipts"]):
        row.update(
            target_adapter="matrix",
            target_channel=target["channel"],
            route_id="xmpp_to_matrix",
        )
    item["receipts"][0]["adapter_message_id"] = "$target"
    item["refs"][0].update(
        adapter="matrix",
        native_channel_id=target["channel"],
        native_message_id="$target",
    )
    if broken_html:
        target["formatted_body"] = "<p>missing reply content</p>"
        with pytest.raises(AssertionError, match="HTML"):
            verify_case(case, item, [source], [target], topology, {})
        return
    assert verify_case(case, item, [source], [target], topology, {}) == {
        "source_id": "source-room-id",
        "target_id": "$target",
    }


@pytest.mark.parametrize(
    "change",
    [
        "sender",
        "source_channel",
        "destination",
        "prefix",
        "duplicate",
        "local_id",
        "receipt",
        "quote_leak",
        "source_ack",
    ],
)
def test_consistently_wrong_or_incomplete_evidence_cannot_qualify(change):
    case, topology, source, target, item = fixture()
    rows = [target]
    if change == "sender":
        source["sender"] = item["event"]["source_transport_id"] = "@wrong:example.org"
    elif change == "source_channel":
        source["channel"] = item["event"]["source_channel_id"] = item["event"][
            "source_native_ref"
        ]["native_channel_id"] = "!wrong:example.org"
    elif change == "destination":
        target["channel"] = item["outbox"][0]["target_channel"] = item["receipts"][0][
            "target_channel"
        ] = "wrong@conference.example.org"
    elif change == "prefix":
        target["text"] = "wrong " + case["text"]
    elif change == "duplicate":
        rows.append(deepcopy(target))
    elif change == "local_id":
        item["receipts"][0]["adapter_message_id"] = "client-id"
        item["refs"][0]["native_message_id"] = "client-id"
    elif change == "receipt":
        item["outbox"][0]["receipt_id"] = None
    elif change == "quote_leak":
        item["event"]["payload"]["body"] += " > quoted"
    else:
        case["source_ack"] = "$wrong"
    with pytest.raises(AssertionError):
        verify_case(case, item, [source], rows, topology, {})


def test_parent_marker_inside_a_native_reply_quote_is_not_an_extra_parent_delivery():
    case, topology, source, target, item = fixture()
    quote = "> " + case["text"] + "\n"
    reply = deepcopy(target)
    reply["text"] = quote + "[Peer]: another nonce"
    reply["native_details"].update(
        reply_to="room-id", fallback_start="0", fallback_end=str(len(quote))
    )
    assert native_hits([target, reply], case["marker"], "xmpp") == [target]
    assert message_body(reply, "xmpp") == "[Peer]: another nonce"


@pytest.mark.parametrize("corruption", [None, "native_parent", "canonical_parent"])
def test_bound_reply_matches_both_canonical_and_native_parent(corruption):
    case, topology, source, target, item = fixture()
    parent = {
        "source": "matrix",
        "source_id": "$original-parent",
        "target_id": "room-parent",
        "core": {"event": {"event_id": "canonical-parent"}},
    }
    case.update(parent="parent", native_target="$original-parent")
    source["native_details"]["reply_to"] = "$original-parent"
    target["native_details"]["reply_to"] = "room-parent"
    item["event"]["relations"] = [
        {"relation_type": "reply", "target_event_id": "canonical-parent"}
    ]
    if corruption == "native_parent":
        target["native_details"]["reply_to"] = "wrong-room-parent"
    elif corruption == "canonical_parent":
        item["event"]["relations"][0]["target_event_id"] = "wrong-parent"
    if corruption:
        with pytest.raises(AssertionError, match="parent"):
            verify_case(case, item, [source], [target], topology, {"parent": parent})
    else:
        verify_case(case, item, [source], [target], topology, {"parent": parent})


@pytest.mark.parametrize("end", ["-1", "99999", "garbage", "٠", "9999999999999999999"])
def test_malformed_native_fallback_range_fails_instead_of_hiding_text(end):
    case, topology, source, target, item = fixture()
    target["native_details"].update(
        reply_to="parent", fallback_start="0", fallback_end=end
    )
    with pytest.raises(AssertionError):
        message_body(target, "xmpp")


async def test_peer_reply_command_preserves_explicit_target(monkeypatch):
    send, reply = AsyncMock(), AsyncMock(return_value="$reply")
    records = []
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {"op": "reply", "id": "request", "text": "answer", "target": "$parent"}
            )
            + '\n{"op":"stop"}\n'
        ),
    )
    monkeypatch.setattr(
        worker, "emit", lambda kind, **fields: records.append({"kind": kind, **fields})
    )
    await worker.commands(send, reply=reply)
    reply.assert_awaited_once_with("answer", "$parent")
    send.assert_not_awaited()
    assert records == [{"kind": "sent", "command": "request", "native_id": "$reply"}]


async def test_unsupported_peer_reply_does_not_become_a_plain_send(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO('{"op":"reply","text":"answer","target":"parent"}\n'),
    )
    with pytest.raises(ValueError, match="Unknown peer command"):
        await worker.commands(send)
    send.assert_not_awaited()


async def test_incomplete_passing_label_does_not_substitute_for_native_evidence(
    tmp_path,
):
    (tmp_path / "results.json").write_text(
        json.dumps(
            {
                "version": 1,
                "status": "passed",
                "cleanup_errors": [],
                "run_prefix": "MF-0123456789ab-",
                "expectations": {},
            }
        )
    )
    with pytest.raises(AssertionError, match="topology"):
        await analyze(tmp_path)


def test_analyzer_failure_summary_does_not_expose_private_input(tmp_path, capsys):
    (tmp_path / "results.json").write_text('{"secret-token":broken')
    assert main([str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "secret-token" not in output
    assert json.loads(output)["status"] == "failed"


@pytest.mark.parametrize("campaign", ["fidelity", "presentation"])
def test_fidelity_logs_require_both_runtime_generations(tmp_path, campaign):
    from scripts.adapter_campaign.lab.log_review import review

    (tmp_path / "logs").mkdir()
    run = tmp_path / ("results/" + campaign + "-123")
    run.mkdir(parents=True)
    (run / "runtime-0.jsonl").write_text("{}\n")
    (run / "runtime-0.stdout").write_text("2026-10-22T01:00:00Z [INFO] medre: ready\n")
    (run / "runtime-0.stderr").write_text("")
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    report = json.loads((tmp_path / "results/log-review.json").read_text())
    assert report["expected_generations"] == 2
    assert report["missing_controls"] == [f"results/{campaign}-123/runtime-1.jsonl"]


async def test_restart_does_not_consume_the_native_reply_deadline(
    tmp_path, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager
    from pathlib import Path

    from scripts.adapter_campaign import fidelity
    from scripts.adapter_campaign.plan import Plan

    monkeypatch.setattr(
        fidelity,
        "CASES",
        (
            ("matrix_parent", "matrix", None),
            ("matrix_after_restart", "matrix", "matrix_parent"),
        ),
    )
    endpoints = {
        "matrix": {
            "transport": "matrix",
            "channel": "!room:example.org",
            "expected_sender": "@bot:example.org",
            "expected_prefixes": {"xmpp": "[Peer]: "},
            "peer": {"user_id": "@peer:example.org"},
        },
        "xmpp": {
            "transport": "xmpp",
            "channel": "room@conference.example.org",
            "expected_sender": "room@conference.example.org/bot",
            "expected_prefixes": {"matrix": "[peer]: "},
            "peer": {"nickname": "peer"},
        },
    }
    cases = {}
    restarts = []

    active_deadlines = []
    real_timeout = asyncio.timeout

    @asynccontextmanager
    async def track_timeout(seconds):
        active_deadlines.append(seconds)
        try:
            async with real_timeout(seconds):
                yield
        finally:
            active_deadlines.pop()

    class Witness:
        def __init__(self, name, directory, timeout):
            self.name = name
            self.records = []

        async def start(self, settings, *, argv=None):
            if self.name == "runtime-1":
                assert active_deadlines == [120]
                restarts.append("started")
            identity = (
                "@peer:example.org"
                if self.name == "matrix"
                else (
                    "room@conference.example.org/peer"
                    if self.name == "xmpp"
                    else "fidelity-runtime"
                )
            )
            self.records = [{"kind": "ready", "identity": identity}]

        async def stop(self):
            if self.name == "runtime-0":
                assert active_deadlines == [120]
                restarts.append("stopped")

        async def send(self, text):
            return {"native_id": "$source"}

        async def reply(self, text, target):
            assert active_deadlines == [90]
            return {"native_id": "$source"}

        async def wait(self, predicate):
            return {"kind": "received"}

    class Storage:
        async def close(self):
            pass

    async def open_readonly(path):
        return Storage()

    async def wait_event(storage, marker, predicate, peers):
        item = {"event": {"event_id": marker}, "outbox": [{"status": "sent"}]}
        cases[marker] = item
        return item

    async def read_event(storage, marker):
        return cases[marker]

    async def observe(peers, settle):
        pass

    def verify_case(case, item, incoming, outgoing, topology, parents):
        return {"source_id": "$source", "target_id": "room-id"}

    monkeypatch.setattr(fidelity.asyncio, "timeout", track_timeout)
    monkeypatch.setattr(fidelity, "Peer", Witness)
    monkeypatch.setattr(fidelity.SQLiteStorage, "open_readonly", open_readonly)
    monkeypatch.setattr(fidelity, "wait_event", wait_event)
    monkeypatch.setattr(fidelity, "read_event", read_event)
    monkeypatch.setattr(fidelity, "observe", observe)
    monkeypatch.setattr(fidelity, "verify_case", verify_case)
    plan = Plan(Path("/unused.yaml"), endpoints, 90, 0, 0)
    report = await fidelity.run(plan, tmp_path / "fidelity")
    assert report["status"] == "passed"
    assert restarts == ["stopped", "started"]
    assert [row["name"] for row in report["cases"]] == [
        "matrix_parent",
        "matrix_after_restart",
    ]
