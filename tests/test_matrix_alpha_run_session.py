"""Opt-in Matrix alpha live tests.

Two test classes exercise the Matrix adapter against a live homeserver:

- ``TestMatrixAlphaRunSession`` — exercises the full
  :func:`~medre.runtime.run_session.orchestration.run_bridge_session`
  orchestration path with a fake Matrix source adapter (``fake_matrix``)
  and a real Matrix destination adapter (``matrix_alpha``).  Validates the
  complete config→build→run→report lifecycle including storage receipts,
  native refs, and report shape.

- ``TestMatrixAlphaDirectAdapter`` — constructs
  :class:`~medre.adapters.matrix.adapter.MatrixAdapter` directly and
  exercises ``deliver`` / ``start`` / ``stop`` / ``diagnostics`` methods.
  These do **not** exercise the ``run_bridge_session`` orchestration.

**Opt-in only.**  Skipped unless all four required ``MATRIX_*`` env vars
are set.  Also tagged with the ``live`` marker so it is excluded from
default pytest runs.

This test does NOT test Meshtastic, MeshCore, LXMF, or any non-Matrix
connectivity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Module-level marker
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Environment gate
# ---------------------------------------------------------------------------
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.environ.get("MATRIX_USER_ID")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

_LIVE_ENV_SET = all(
    [MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID]
)

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=(
        "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
        "and MATRIX_ROOM_ID env vars to run Matrix alpha live tests"
    ),
)

# Timeout bounds for live operations (seconds).
_START_TIMEOUT: float = 30.0
_STOP_TIMEOUT: float = 10.0
_DELIVER_TIMEOUT: float = 15.0
_RUN_SESSION_TIMEOUT: float = 60.0


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _write_run_session_config(tmp_path: Path) -> str:
    """Write a TOML config for ``run_bridge_session``.

    The route goes from a fake Matrix source adapter (``fake_matrix``) to
    the real Matrix destination adapter (``matrix_alpha``).  Because
    :func:`~medre.runtime.run_session.evidence._pick_source_adapter`
    prefers matrix-platform adapters and picks the first alphabetically,
    it selects ``fake_matrix`` — injecting the event there and routing it
    to the real adapter for actual delivery.

    A non-secret placeholder token is sufficient — the real token is
    injected via environment override at test runtime.  ``load_config()``
    validates :class:`~medre.config.adapters.matrix.MatrixConfig`
    (requiring non-empty credentials) **before** ``apply_env_overrides``
    runs, so the placeholder satisfies validation.  The file is written
    under ``tmp_path`` only and is deleted with the temporary directory.
    """
    assert MATRIX_HOMESERVER and MATRIX_USER_ID
    assert MATRIX_ROOM_ID

    db_path = (tmp_path / "alpha-session.db").as_posix()
    config_content = f"""\
[runtime]
name = "matrix-alpha-run-session"
shutdown_timeout_seconds = 10

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{db_path}"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@fake:local"
access_token = "fake_token"
room_allowlist = ["!fake:local"]
encryption_mode = "plaintext"

[adapters.matrix.matrix_alpha]
enabled = true
adapter_kind = "real"
homeserver = {json.dumps(MATRIX_HOMESERVER)}
user_id = {json.dumps(MATRIX_USER_ID)}
access_token = {json.dumps("placeholder_token_for_validation_only")}
room_allowlist = [{json.dumps(MATRIX_ROOM_ID)}]
encryption_mode = "plaintext"

[routes.alpha_bridge]
source_adapters = ["fake_matrix"]
dest_adapters = ["matrix_alpha"]
directionality = "source_to_dest"
dest_room = {json.dumps(MATRIX_ROOM_ID)}
enabled = true
"""
    config_file = tmp_path / "alpha-run-session.toml"
    config_file.write_text(config_content, encoding="utf-8")
    return str(config_file)


def _write_direct_adapter_config(tmp_path: Path) -> str:
    """Write a TOML config for direct adapter tests.

    Uses real Matrix + fake Meshtastic with a bridge route.
    A non-secret placeholder token is sufficient — the real token is
    injected via environment override at test runtime.
    """
    assert MATRIX_HOMESERVER and MATRIX_USER_ID
    assert MATRIX_ROOM_ID

    db_path = (tmp_path / "alpha-direct.db").as_posix()
    config_content = f"""\
[runtime]
name = "matrix-alpha-direct-adapter"
shutdown_timeout_seconds = 10

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{db_path}"

[adapters.matrix.matrix_alpha]
enabled = true
adapter_kind = "real"
homeserver = {json.dumps(MATRIX_HOMESERVER)}
user_id = {json.dumps(MATRIX_USER_ID)}
access_token = {json.dumps("placeholder_token_for_validation_only")}
room_allowlist = [{json.dumps(MATRIX_ROOM_ID)}]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "AlphaTest"

[routes.alpha_bridge]
source_adapters = ["matrix_alpha"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = {json.dumps(MATRIX_ROOM_ID)}
dest_channel = "1"
"""
    config_file = tmp_path / "alpha-direct-config.toml"
    config_file.write_text(config_content, encoding="utf-8")
    return str(config_file)


# ---------------------------------------------------------------------------
# Tests: run_bridge_session orchestration
# ---------------------------------------------------------------------------


@require_live
class TestMatrixAlphaRunSession:
    """Matrix alpha run-session live tests.

    Exercises the full ``run_bridge_session`` orchestration path with a
    fake Matrix source adapter (``fake_matrix``) and a real Matrix
    destination adapter (``matrix_alpha``).  Validates the complete
    config→build→run→report lifecycle including storage receipts,
    native refs, and report shape.

    Requires all four ``MATRIX_*`` environment variables.
    """

    async def test_run_bridge_session_happy_path(self, tmp_path) -> None:
        """run_bridge_session with fake source → real Matrix dest produces valid report."""
        from medre.runtime.run_session.orchestration import run_bridge_session

        config_path = _write_run_session_config(tmp_path)
        storage_path = tmp_path / "matrix-alpha-session.db"

        report = await asyncio.wait_for(
            run_bridge_session(
                config_path=config_path,
                storage_path=str(storage_path),
                snapshot_dir=str(tmp_path),
                message_text="MEDRE Matrix run-session test — safe to ignore",
                message_count=1,
                scenario="happy_path",
            ),
            timeout=_RUN_SESSION_TIMEOUT,
        )

        # --- Core report shape ---
        assert report["status"] == "passed", (
            f"run_bridge_session failed: "
            f"{json.dumps(report, default=str, indent=2)}"
        )
        assert report["command"] == "run_session"
        assert report["source_adapter"] == "fake_matrix"
        assert "matrix_alpha" in report["target_adapters"]
        assert report["message_count"] == 1
        assert report["event_ids"]

        # --- Storage assertions ---
        assert Path(report["storage_path"]).exists()
        assert report["storage_path"] == str(storage_path)
        if report["final_snapshot_path"]:
            assert Path(report["final_snapshot_path"]).exists()
        assert report["storage_ephemeral"] is False

        # --- Delivery receipts ---
        receipts = report["delivery_receipts"]
        assert receipts, "No delivery_receipts in report"
        matrix_receipts = [
            r for r in receipts if r.get("target_adapter") == "matrix_alpha"
        ]
        assert len(matrix_receipts) > 0, (
            f"No matrix_alpha receipts in delivery_receipts: {receipts}"
        )
        assert any(r["status"] == "sent" for r in matrix_receipts), (
            f"No 'sent' receipt for matrix_alpha: {matrix_receipts}"
        )

        # --- Native refs ---
        native_refs = report["native_refs"]
        assert native_refs, "No native_refs in report"
        matrix_refs = [
            r for r in native_refs if r.get("adapter") == "matrix_alpha"
        ]
        assert len(matrix_refs) > 0, (
            f"No matrix_alpha entries in native_refs: {native_refs}"
        )
        for ref in matrix_refs:
            native_id = ref.get("native_message_id") or ref.get("native_id", "")
            assert native_id.startswith("$"), (
                f"Expected Matrix event_id starting with '$', got: {native_id!r}"
            )
            channel = ref.get("native_channel_id") or ref.get("channel", "")
            assert channel == MATRIX_ROOM_ID, (
                f"Expected channel {MATRIX_ROOM_ID!r}, got: {channel!r}"
            )

        # --- Redaction: access token must not appear in serialized report ---
        assert MATRIX_ACCESS_TOKEN is not None
        report_json = json.dumps(report, default=str)
        assert MATRIX_ACCESS_TOKEN not in report_json, (
            "MATRIX_ACCESS_TOKEN leaked into report JSON output"
        )


# ---------------------------------------------------------------------------
# Tests: direct adapter (not run-session orchestration)
# ---------------------------------------------------------------------------


@require_live
class TestMatrixAlphaDirectAdapter:
    """Direct MatrixAdapter live tests (not run-session orchestration).

    These tests construct :class:`MatrixAdapter` directly and exercise its
    ``deliver`` / ``start`` / ``stop`` / ``diagnostics`` methods against a
    live Matrix homeserver.  They do **not** call ``run_bridge_session``.

    Requires all four ``MATRIX_*`` environment variables.
    """

    async def test_direct_adapter_config_builds_and_starts(self, tmp_path) -> None:
        """Config with adapter_kind='real' for Matrix builds and starts."""
        from medre.config.env import apply_env_overrides
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder

        config_path = _write_direct_adapter_config(tmp_path)
        config, _source, paths = load_config(config_path)
        config = apply_env_overrides(config, paths)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert (
            len(app.adapters) > 0
        ), f"No adapters built. Build failures: {app.build_failures}"
        assert (
            "matrix_alpha" in app.adapters
        ), f"Matrix adapter not built. Available: {list(app.adapters.keys())}"

        try:
            await asyncio.wait_for(app.start(), timeout=_START_TIMEOUT)

            # Verify adapter is healthy
            adapter = app.adapters["matrix_alpha"]
            info = await adapter.health_check()
            assert (
                info.health == "healthy"
            ), f"Matrix adapter not healthy after start: {info.health!r}"
        finally:
            await asyncio.wait_for(app.stop(), timeout=_STOP_TIMEOUT)

    async def test_direct_adapter_send_and_verify_event_id(self) -> None:
        """Send via direct MatrixAdapter.deliver() and verify event_id."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        assert MATRIX_HOMESERVER is not None
        assert MATRIX_USER_ID is not None
        assert MATRIX_ACCESS_TOKEN is not None
        assert MATRIX_ROOM_ID is not None

        config = MatrixConfig(
            adapter_id="matrix-alpha-session",
            homeserver=MATRIX_HOMESERVER,
            user_id=MATRIX_USER_ID,
            access_token=MATRIX_ACCESS_TOKEN,
            room_allowlist={MATRIX_ROOM_ID},
        )

        ctx = AdapterContext(
            adapter_id="matrix-alpha-session",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.alpha-session"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        adapter = MatrixAdapter(config)
        await asyncio.wait_for(adapter.start(ctx), timeout=_START_TIMEOUT)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"alpha-session-{ts}",
                target_adapter="matrix-alpha-session",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE alpha session test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "alpha-session"},
            )
            delivery = await asyncio.wait_for(
                adapter.deliver(result), timeout=_DELIVER_TIMEOUT
            )
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, "native_message_id is None"
            assert delivery.native_message_id.startswith(
                "$"
            ), f"Expected event_id starting with '$', got {delivery.native_message_id!r}"
            assert delivery.native_channel_id == MATRIX_ROOM_ID
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_STOP_TIMEOUT)

    async def test_direct_adapter_diagnostics_after_send(self) -> None:
        """Verify MatrixAdapter diagnostics are complete after a direct send."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        assert MATRIX_HOMESERVER is not None
        assert MATRIX_USER_ID is not None
        assert MATRIX_ACCESS_TOKEN is not None
        assert MATRIX_ROOM_ID is not None

        config = MatrixConfig(
            adapter_id="matrix-alpha-diag",
            homeserver=MATRIX_HOMESERVER,
            user_id=MATRIX_USER_ID,
            access_token=MATRIX_ACCESS_TOKEN,
            room_allowlist={MATRIX_ROOM_ID},
        )

        ctx = AdapterContext(
            adapter_id="matrix-alpha-diag",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.alpha-diag"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        adapter = MatrixAdapter(config)
        await asyncio.wait_for(adapter.start(ctx), timeout=_START_TIMEOUT)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"alpha-diag-{ts}",
                target_adapter="matrix-alpha-diag",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE alpha diag test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "alpha-diag"},
            )
            await asyncio.wait_for(adapter.deliver(result), timeout=_DELIVER_TIMEOUT)

            diag = adapter.diagnostics()

            # Verify connected state
            assert diag["connected"] is True
            assert diag["logged_in"] is True
            assert diag["sync_task_running"] is True

            # Verify no secrets in diagnostics output
            diag_str = str(diag)
            assert MATRIX_ACCESS_TOKEN is not None
            assert MATRIX_ACCESS_TOKEN not in diag_str

            # Verify delivery counters are integers
            assert isinstance(diag["transient_delivery_failures"], int)
            assert isinstance(diag["permanent_delivery_failures"], int)
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_STOP_TIMEOUT)
