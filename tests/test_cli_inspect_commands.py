"""Tests for 'medre inspect' basic subcommands: parser, event, receipts, native-ref, read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.cli import (
    CONFIG_INSPECT_MEMORY,
    CONFIG_INSPECT_SQLITE,
    _run_cli,
    _run_cli_both,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _seed_inspect_db(
    db_path: str,
    event_id: str = "evt-inspect-1",
    source_adapter: str = "test_adapter",
    replay_run_id: str | None = None,
    native_adapter: str | None = None,
    native_channel_id: str | None = None,
    native_message_id: str | None = None,
) -> None:
    """Synchronously seed an inspect test database with an event + receipt + native ref."""
    import asyncio
    from datetime import datetime, timezone

    from medre.core.events import (
        CanonicalEvent,
        DeliveryReceipt,
        EventMetadata,
        NativeMessageRef,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                source_adapter=source_adapter,
                source_transport_id="test-transport",
                source_channel_id="ch-inspect",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "inspect test message"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            receipt_kwargs: dict = dict(
                sequence=1,
                receipt_id="rcpt-inspect-1",
                event_id=event_id,
                delivery_plan_id="plan-inspect-1",
                target_adapter="dest_adapter",
                route_id="route-inspect",
                status="sent",
                created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
            )
            if replay_run_id is not None:
                receipt_kwargs["source"] = "replay"
                receipt_kwargs["replay_run_id"] = replay_run_id

            await storage.append_receipt(DeliveryReceipt(**receipt_kwargs))

            if native_adapter is not None and native_message_id is not None:
                await storage.store_native_ref(
                    NativeMessageRef(
                        id="nref-inspect-1",
                        event_id=event_id,
                        adapter=native_adapter,
                        native_channel_id=native_channel_id,
                        native_message_id=native_message_id,
                        native_thread_id=None,
                        native_relation_id=None,
                        direction="outbound",
                        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                    )
                )
        finally:
            await storage.close()

    asyncio.run(_seed())


@pytest.fixture()
def db_inspect_sqlite(tmp_path: Path) -> str:
    """Seeded SQLite DB path for inspect tests."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(db_path)
    return db_path


@pytest.fixture()
def config_inspect_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage pointing at a temp MEDRE_HOME, with seeded DB."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(db_path)
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def config_inspect_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with memory backend."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_MEMORY)
    return p


@pytest.fixture()
def db_inspect_replay(tmp_path: Path) -> str:
    """Seeded SQLite DB with replay receipts."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-replay-1",
        replay_run_id="run-42",
    )
    return db_path


@pytest.fixture()
def config_inspect_with_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage and seeded replay receipts."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-replay-1",
        replay_run_id="run-42",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def db_inspect_with_native_ref(tmp_path: Path) -> str:
    """Seeded SQLite DB with native ref."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-1",
        native_adapter="matrix",
        native_channel_id="!room:test",
        native_message_id="$native-msg-1",
    )
    return db_path


@pytest.fixture()
def config_inspect_with_native_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Config with sqlite storage and seeded native ref."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-1",
        native_adapter="matrix",
        native_channel_id="!room:test",
        native_message_id="$native-msg-1",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def db_inspect_native_null_channel(tmp_path: Path) -> str:
    """Seeded SQLite DB with native ref with null channel."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-nullch",
        native_adapter="meshtastic",
        native_channel_id=None,
        native_message_id="radio-msg-42",
    )
    return db_path


@pytest.fixture()
def config_inspect_native_null_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Config with sqlite storage and native ref with null channel."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-nullch",
        native_adapter="meshtastic",
        native_channel_id=None,
        native_message_id="radio-msg-42",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


# ---------------------------------------------------------------------------
# inspect parser
# ---------------------------------------------------------------------------


class TestInspectParser:
    """Tests for 'medre inspect' argument parsing and dispatch."""

    def test_inspect_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect"])

    def test_inspect_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "nonexistent"])

    def test_inspect_event_requires_event_id(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "event", "--storage-path", "/dev/null"])

    def test_inspect_receipts_requires_event_or_replay_run(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "receipts", "--storage-path", "/dev/null"])

    def test_inspect_receipts_event_and_replay_run_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    "evt-1",
                    "--replay-run",
                    "run-1",
                    "--storage-path",
                    "/dev/null",
                ]
            )

    def test_inspect_native_ref_requires_adapter_and_message(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "native-ref", "--storage-path", "/dev/null"])

    def test_inspect_native_ref_adapter_only_is_insufficient(self) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "inspect",
                    "native-ref",
                    "--adapter",
                    "matrix",
                    "--storage-path",
                    "/dev/null",
                ]
            )


# ---------------------------------------------------------------------------
# inspect event
# ---------------------------------------------------------------------------


class TestInspectEvent:
    """Tests for 'medre inspect event' command."""

    def test_event_found_returns_json(self, db_inspect_sqlite: str) -> None:
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-inspect-1"
        assert parsed["event_kind"] == "message.created"
        assert parsed["payload"]["text"] == "inspect test message"

    def test_event_json_is_deterministic(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_event_not_found_exits_not_found(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "event",
                "nonexistent-event",
                "--storage-path",
                db_inspect_sqlite,
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_event_not_found_stderr_message(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        _stdout, stderr = _run_cli_both(
            "inspect",
            "event",
            "nonexistent-event",
            "--storage-path",
            db_inspect_sqlite,
        )
        assert "event not found" in stderr
        assert "nonexistent-event" in stderr

    def test_event_missing_storage_path_exits(self) -> None:
        """inspect event without --storage-path exits with parse error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["inspect", "event", "evt-1"])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# inspect receipts
# ---------------------------------------------------------------------------


class TestInspectReceipts:
    """Tests for 'medre inspect receipts' command."""

    def test_receipts_by_event_found(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "receipts",
            "--event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["event_id"] == "evt-inspect-1"
        assert parsed[0]["receipt_id"] == "rcpt-inspect-1"
        assert parsed[0]["status"] == "sent"

    def test_receipts_by_event_empty_list(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "receipts",
            "--event",
            "nonexistent-event",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 0

    def test_receipts_by_replay_run_found(
        self,
        db_inspect_replay: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "receipts",
            "--replay-run",
            "run-42",
            "--storage-path",
            db_inspect_replay,
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["source"] == "replay"
        assert parsed[0]["replay_run_id"] == "run-42"

    def test_receipts_by_replay_run_empty_list(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "receipts",
            "--replay-run",
            "nonexistent-run",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 0

    def test_receipts_json_is_deterministic(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "receipts",
            "--event",
            "evt-inspect-1",
            "--storage-path",
            db_inspect_sqlite,
        )
        parsed = json.loads(output)
        receipt = parsed[0]
        keys = list(receipt.keys())
        assert keys == sorted(keys)

    def test_receipts_missing_db_exits_build(
        self,
        tmp_path: Path,
    ) -> None:
        """inspect receipts with non-existent DB exits EXIT_BUILD."""
        from medre.cli import EXIT_BUILD

        missing_db = str(tmp_path / "missing.db")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "receipts",
                "--event",
                "evt-1",
                "--storage-path",
                missing_db,
            )
        assert exc_info.value.code == EXIT_BUILD


# ---------------------------------------------------------------------------
# inspect native-ref
# ---------------------------------------------------------------------------


class TestInspectNativeRef:
    """Tests for 'medre inspect native-ref' command."""

    def test_native_ref_found_returns_event(
        self,
        db_inspect_with_native_ref: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:test",
            "--message",
            "$native-msg-1",
            "--storage-path",
            db_inspect_with_native_ref,
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-nref-1"
        assert parsed["adapter"] == "matrix"
        assert parsed["native_message_id"] == "$native-msg-1"
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-nref-1"

    def test_native_ref_null_channel(
        self,
        db_inspect_native_null_channel: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "meshtastic",
            "--message",
            "radio-msg-42",
            "--storage-path",
            db_inspect_native_null_channel,
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-nref-nullch"
        assert parsed["native_channel_id"] is None

    def test_native_ref_not_found_exits_not_found(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "native-ref",
                "--adapter",
                "nonexistent",
                "--message",
                "nonexistent-msg",
                "--storage-path",
                db_inspect_sqlite,
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_native_ref_not_found_stderr_message(
        self,
        db_inspect_sqlite: str,
    ) -> None:
        stdout, stderr = _run_cli_both(
            "inspect",
            "native-ref",
            "--adapter",
            "nonexistent",
            "--message",
            "nonexistent-msg",
            "--storage-path",
            db_inspect_sqlite,
        )
        assert "native ref not found" in stderr

    def test_native_ref_json_is_deterministic(
        self,
        db_inspect_with_native_ref: str,
    ) -> None:
        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:test",
            "--message",
            "$native-msg-1",
            "--storage-path",
            db_inspect_with_native_ref,
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_native_ref_missing_db_exits_build(
        self,
        tmp_path: Path,
    ) -> None:
        """inspect native-ref with non-existent DB exits EXIT_BUILD."""
        from medre.cli import EXIT_BUILD

        missing_db = str(tmp_path / "missing.db")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "native-ref",
                "--adapter",
                "matrix",
                "--message",
                "$msg",
                "--storage-path",
                missing_db,
            )
        assert exc_info.value.code == EXIT_BUILD


# ---------------------------------------------------------------------------
# inspect read-only enforcement
# ---------------------------------------------------------------------------


class TestInspectReadOnly:
    """Verify that inspect commands never create DB files or mutate storage."""

    def test_missing_db_exits_build(self, tmp_path: Path) -> None:
        """inspect event with non-existent DB exits EXIT_BUILD (3)."""
        from medre.cli import EXIT_BUILD

        missing_db = str(tmp_path / "no_such_file.db")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "event",
                "evt-1",
                "--storage-path",
                missing_db,
            )
        assert exc_info.value.code == EXIT_BUILD

    def test_missing_db_not_created(self, tmp_path: Path) -> None:
        """inspect does not create the missing DB file."""
        db_path = tmp_path / "missing.db"
        assert not db_path.exists()

        with pytest.raises(SystemExit):
            _run_cli(
                "inspect",
                "event",
                "evt-1",
                "--storage-path",
                str(db_path),
            )
        assert not db_path.exists()

    def test_missing_db_stderr_has_storage_error(self, tmp_path: Path) -> None:
        """Missing DB error message mentions storage or does not exist."""
        db_path = str(tmp_path / "missing.db")

        _stdout, stderr = _run_cli_both(
            "inspect",
            "event",
            "evt-1",
            "--storage-path",
            db_path,
        )
        assert "Storage error" in stderr or "does not exist" in stderr

    def test_missing_db_receipts_exits_build(self, tmp_path: Path) -> None:
        """inspect receipts with non-existent DB also exits EXIT_BUILD."""
        from medre.cli import EXIT_BUILD

        missing_db = str(tmp_path / "missing.db")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "receipts",
                "--event",
                "evt-1",
                "--storage-path",
                missing_db,
            )
        assert exc_info.value.code == EXIT_BUILD

    def test_missing_db_native_ref_exits_build(self, tmp_path: Path) -> None:
        """inspect native-ref with non-existent DB also exits EXIT_BUILD."""
        from medre.cli import EXIT_BUILD

        missing_db = str(tmp_path / "missing.db")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect",
                "native-ref",
                "--adapter",
                "matrix",
                "--message",
                "$msg",
                "--storage-path",
                missing_db,
            )
        assert exc_info.value.code == EXIT_BUILD
