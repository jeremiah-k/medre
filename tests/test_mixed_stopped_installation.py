"""Stopped mixed-installation capture preserves operational authority as one unit.

This campaign uses real core SQLite and the SDK-free provider declarations.
It does not claim to copy radio firmware or open a Matrix encryption store;
the pinned-SDK native reopen is qualified in the Matrix adapter's own suite.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from medre.config.model import RuntimeConfig
from medre.config.paths import MedrePaths
from medre.core.events import NativeMessageRef
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.maintenance import stopped_declare as maintenance
from medre.maintenance.stopped_snapshot import (
    restore_stopped_snapshot,
    verify_stopped_snapshot,
)
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)

pytestmark = pytest.mark.local_integration


def _provider(module: str) -> ModuleType:

    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name == module or module.startswith(f"{exc.name}."):
            pytest.skip(f"optional adapter checkout unavailable: {module}")
        raise


@pytest.mark.skipif(os.name == "nt", reason="Matrix native leases require POSIX")
async def test_mixed_matrix_radio_restore_retains_correlation_and_uncertain_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = _provider("medre.adapters.matrix.stopped_declaration")
    _provider("medre.adapters.meshtastic.stopped_declaration")
    _provider("medre.adapters.meshcore.stopped_declaration")
    root = tmp_path / "source"
    store_dir = root / "matrix-store"
    store_dir.mkdir(parents=True)
    account = store_dir / "@bridge:example.org_DEVICE.db"
    # sqlite3's context manager commits but never closes; an unclosed handle
    # raises an unraisable ResourceWarning at GC time.
    with contextlib.closing(sqlite3.connect(account)) as db, db:
        db.execute("CREATE TABLE identity(material TEXT)")
        db.execute("INSERT INTO identity VALUES ('native identity fixture')")
    (store_dir / f"{account.name}.ingest.lock").touch(mode=0o600)
    credentials = root / "config" / "credentials.json"
    credentials.parent.mkdir()
    credentials.write_text(json.dumps({"access_token": "synthetic"}))
    monkeypatch.setattr(matrix, "_credentials_sidecar", lambda: credentials)

    core_path = root / "core.sqlite"
    core = SQLiteStorage(str(core_path))
    await core.initialize()
    radio = await create_outbox_item_with_parent(
        core,
        make_outbox_item(
            delivery_plan_id="plan-radio",
            target_adapter="meshtastic",
            target_channel="0",
            attempt_number=0,
            event_id="mixed-event",
        ),
    )
    await create_outbox_item_with_parent(
        core,
        make_outbox_item(
            delivery_plan_id="plan-matrix",
            target_adapter="matrix",
            target_channel="!room:example.org",
            attempt_number=0,
            event_id="mixed-event",
        ),
    )
    reference = NativeMessageRef(
        id="native-correlation-1",
        event_id="mixed-event",
        adapter="matrix",
        native_channel_id="!room:example.org",
        native_message_id="$source-event",
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
    )
    await core.store_native_ref(reference)
    await core.put_adapter_checkpoint("matrix", "classic_sync", "s123")
    await core.put_adapter_checkpoint(
        "matrix",
        "nio_durable",
        "synthetic-stream-position",
        metadata_json='{"schema_version":1,"abandoned_rooms":{}}',
    )
    await core.put_adapter_checkpoint("meshcore", "radio", "radio-generation-3")
    now = datetime.now(timezone.utc).isoformat()
    claimed = await core.claim_due_outbox_items(now, "interrupted-owner", limit=2)
    assert len(claimed) == 2
    reserved = await core.reserve_outbox_attempt(
        radio.outbox_id, "interrupted-owner", 0
    )
    assert reserved == 1
    claim = DeliveryAttemptProvenance(
        "mixed-event", "plan-radio", "meshtastic", "0", radio.outbox_id, 1, "live"
    )
    plan = plan_radio_text("Uncertain radio packet before restore", max_text_bytes=80)
    bound = await core.bind_radio_attempt(claim, "interrupted-owner", plan)
    assert bound is not None
    submitting = await core.begin_radio_part(bound, "interrupted-owner", 1)
    assert submitting is not None
    assert submitting.progress.parts[0].state == "submitting"
    original = await core.list_outbox_items_for_event("mixed-event")
    await core.close()

    matrix_spec = _provider("medre.adapters.matrix.plugin").adapter_spec
    meshtastic_spec = _provider("medre.adapters.meshtastic.plugin").adapter_spec
    meshcore_spec = _provider("medre.adapters.meshcore.plugin").adapter_spec

    configured = (
        (
            "matrix",
            "matrix",
            "real",
            matrix_spec(),
            SimpleNamespace(
                user_id="@bridge:example.org",
                device_id="DEVICE",
                store_path=str(store_dir),
            ),
        ),
        (
            "meshtastic",
            "FXLF",
            "real",
            meshtastic_spec(),
            SimpleNamespace(connection_type="ble"),
        ),
        (
            "meshcore",
            "meshcore",
            "real",
            meshcore_spec(),
            SimpleNamespace(connection_type="ble"),
        ),
    )
    monkeypatch.setattr(maintenance, "_prepare_instances", lambda *_: iter(configured))
    snapshot = tmp_path / "snapshot"
    capture = maintenance.capture_configured_stopped_state(
        cast(
            RuntimeConfig,
            SimpleNamespace(storage=SimpleNamespace(path=str(core_path))),
        ),
        cast(MedrePaths, SimpleNamespace()),
        common_root=root,
        snapshot=snapshot,
        runtime_stopped=True,
    )
    assert {entry.path for entry in capture.entries} == {
        "core.sqlite",
        "matrix-store/@bridge:example.org_DEVICE.db",
        "config/credentials.json",
    }
    classes = {
        coverage.provider: coverage.state_class for coverage in capture.coverages
    }
    assert classes["matrix"] == "file_backed"
    assert classes["meshtastic"] == classes["meshcore"] == "device_attached"
    verify_stopped_snapshot(snapshot)
    restored = tmp_path / "restored"
    restore_stopped_snapshot(snapshot, restored, runtime_stopped=True)
    assert not (
        restored / "matrix-store/@bridge:example.org_DEVICE.db.ingest.lock"
    ).exists()
    with contextlib.closing(
        sqlite3.connect(restored / "matrix-store/@bridge:example.org_DEVICE.db")
    ) as db:
        assert db.execute("SELECT material FROM identity").fetchone() == (
            "native identity fixture",
        )
    recovered = SQLiteStorage(str(restored / "core.sqlite"))
    await recovered.initialize()
    try:
        assert (
            await recovered.resolve_native_ref(
                "matrix", "!room:example.org", "$source-event"
            )
            == "mixed-event"
        )
        matrix_cursor = await recovered.get_adapter_checkpoint("matrix", "classic_sync")
        durable_cursor = await recovered.get_adapter_checkpoint("matrix", "nio_durable")
        meshcore_cursor = await recovered.get_adapter_checkpoint("meshcore", "radio")
        assert matrix_cursor is not None and matrix_cursor.cursor == "s123"
        assert durable_cursor is not None
        assert durable_cursor.cursor == "synthetic-stream-position"
        assert json.loads(durable_cursor.metadata_json) == {
            "schema_version": 1,
            "abandoned_rooms": {},
        }
        assert meshcore_cursor is not None
        assert meshcore_cursor.cursor == "radio-generation-3"
        assert await recovered.list_outbox_items_for_event("mixed-event") == original
        assert await recovered.get_radio_attempt(radio.outbox_id, 1) == submitting
        latest = await recovered.get_outbox_item(radio.outbox_id)
        assert latest is not None and latest.active_attempt == 1
        assert latest.status == "in_progress"
        # No restored row itself supplies a new submission grant.
        assert (
            await recovered.bind_radio_attempt(claim, "interrupted-owner", plan) is None
        )
    finally:
        await recovered.close()
