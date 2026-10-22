"""Native activity evidence cannot borrow identity or count runtime claims."""

from copy import deepcopy
from contextlib import closing
import io
import json
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest

from scripts.adapter_campaign import worker
from scripts.adapter_campaign.typing_campaign import TABLES, counts, matches


@pytest.mark.parametrize("transport", ["matrix", "xmpp"])
@pytest.mark.parametrize("change", [None, "identity", "channel", "stale", "message"])
def test_native_witness_requires_independent_expected_scope(transport, change):
    """Wrong actors, conversations, old observations and messages cannot qualify."""
    expected = dict(transport=transport, channel="conversation", bot_identity="bot")
    row = dict(kind="typing", at=10, channel="conversation")
    if transport == "matrix":
        row["users"] = ["bot", "peer"]
    else:
        row.update(sender="bot", state="composing")
    if change == "identity":
        row.update(users=["wrong"], sender="wrong")
    elif change == "channel":
        row["channel"] = "wrong"
    elif change == "stale":
        row["at"] = 5
    elif change == "message":
        row["kind"] = "received"
    assert matches(row, expected, True, 6) is (change is None)
    assert matches(row, expected, False, 6) is (
        change == "identity" and transport == "matrix"
    )


def test_clear_uses_whole_membership_and_direct_native_stop_state():
    """Another actor typing in Matrix does not keep this relay's indicator alive."""
    row = dict(kind="typing", at=10, channel="conversation", users=["peer"])
    expected = dict(transport="matrix", channel="conversation", bot_identity="bot")
    assert matches(row, expected, False, 6)
    assert not matches({**row, "users": None}, expected, False, 6)
    assert not matches({**row, "users": "bot"}, expected, True, 6)
    xmpp = {**expected, "transport": "xmpp"}
    clear = {**row, "sender": "bot", "state": "paused"}
    assert matches(clear, xmpp, False, 6)
    assert not matches({**clear, "state": "gone"}, xmpp, False, 6)
    assert not matches({**clear, "sender": "peer"}, xmpp, False, 6)


def test_durable_counts_read_actual_database_rows_without_creating_a_database(tmp_path):
    """A missing store fails; a changed durable row count remains visible."""
    with pytest.raises(sqlite3.OperationalError):
        counts(tmp_path)
    assert not (tmp_path / "medre.sqlite3").exists()
    with closing(sqlite3.connect(tmp_path / "medre.sqlite3")) as db:
        for table in TABLES:
            db.execute(f"CREATE TABLE {table} (id TEXT)")
    baseline = counts(tmp_path)
    assert baseline == dict.fromkeys(TABLES, 0)
    with closing(sqlite3.connect(tmp_path / "medre.sqlite3")) as db:
        db.execute("INSERT INTO delivery_outbox VALUES ('unwanted')")
        db.commit()
    final = counts(tmp_path)
    assert final != deepcopy(baseline)
    assert final["delivery_outbox"] == 1


async def test_peer_typing_command_uses_its_explicit_native_seam(monkeypatch):
    """Activity commands cannot accidentally send a message body."""
    send, typing = AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(dict(op="typing", id="case", typing=True)) + '\n{"op":"stop"}\n'
        ),
    )
    rows = []
    monkeypatch.setattr(worker, "emit", lambda kind, **data: rows.append((kind, data)))
    await worker.commands(send, typing=typing)
    typing.assert_awaited_once_with(True)
    send.assert_not_awaited()
    assert rows[0][0] == "sent" and rows[0][1]["command"] == "case"


def test_typing_log_audit_requires_all_generation_streams(tmp_path):
    """A missing stderr capture cannot hide failures in another generation."""
    from scripts.adapter_campaign.lab.log_review import review

    evidence = tmp_path / "results/typing-123/evidence"
    evidence.mkdir(parents=True)
    for number in range(3):
        (evidence / f"runtime-{number}.jsonl").write_text('{"kind":"ready"}\n')
        (evidence / f"runtime-{number}.stdout").write_text(
            "2026-10-22T10:00:00+0000 [INFO] medre.runtime.app: ready\n"
        )
        (evidence / f"runtime-{number}.stderr").write_text("")
    review(tmp_path)
    report = json.loads((tmp_path / "results/log-review.json").read_text())
    assert report["status"] == "passed" and len(report["logs"]) == 6
    (evidence / "runtime-2.stderr").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        review(tmp_path)
    report = json.loads((tmp_path / "results/log-review.json").read_text())
    assert report["status"] == "failed"
    assert report["missing_streams"] == ["results/typing-123/evidence/runtime-2.stderr"]
