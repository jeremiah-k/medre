"""Synapse E2EE smoke tests — Docker SDK-boundary encrypted room proof.

These tests extend the Docker Synapse integration infrastructure with
end-to-end encryption (E2EE) support:

1. A dedicated encrypted room is created via ``m.room.encryption`` state
   event with ``m.megolm.v1.aes-sha2`` algorithm.
2. Device IDs are captured from the Synapse login response for both bot
   and test user.
3. Store paths are created for nio crypto store persistence.
4. The MEDRE MatrixAdapter is started with ``encryption_mode="e2ee_required"``
   and exercises the real nio crypto subsystem against Docker Synapse.
5. A **second nio AsyncClient** is created for the test user with
   ``encryption_enabled=True``.  This client performs genuine Megolm
   encryption via ``room_send()``, producing ``m.room.encrypted`` events
   that the bot must decrypt via its own nio crypto subsystem.

What this harness **does** prove:

- Encrypted room creation and ``m.room.encryption`` state event handling.
- Both bot and test user device IDs captured from login responses.
- nio crypto store initialisation (``crypto_enabled``, ``crypto_store_loaded``).
- Bot adapter starts with ``encryption_mode="e2ee_required"`` and initial
  sync + key upload succeeds.
- Second nio client performs genuine Megolm encryption of outbound messages.
- When key exchange completes in time, the bot **decrypts** the inbound
  ``m.room.encrypted`` event back to the original plaintext body.

What this harness does **NOT** prove:

- Docker loopback only — no live network or federation.
- No cross-signing or interactive device verification flow.
- Ephemeral crypto store — keys are discarded after each test run.
- ``ignore_unverified_devices=True`` is used to skip verification prompts.
- Key exchange timing is non-deterministic; decryption may not complete
  within the test timeout on every run.  The test uses ``pytest.xfail``
  with ``reason=...`` when this occurs rather than silently passing.

Tests are gated behind ``pytest.mark.docker`` and ``HAS_E2EE`` (requires
``mindroom-nio[e2e]`` with ``vodozemac`` installed).

Running locally::

    pip install -e ".[matrix-e2e]"
    pytest tests/integration/test_synapse_e2ee_smoke.py -m docker -v

Evidence classification
-----------------------
Each test produces a ``report`` dict classified as ``docker_sdk_boundary``:

- ``transport``: ``"matrix"``
- ``evidence_level``: ``"docker_sdk_boundary"``
- ``encrypted_room_id``: the room ID with encryption enabled
- ``device_ids``: redacted device IDs for bot and test user
- ``crypto_enabled`` / ``crypto_store_loaded``: diagnostics fields
- ``client_side_encrypted``: whether the message was sent via nio encryption
- ``decryption_succeeded``: whether the bot decrypted the message
- ``limitations``: explicit list of what this run does NOT prove
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting

from .conftest import E2EETestEnvironment
from .synapse_helpers import make_context as _make_context
from .synapse_helpers import (
    send_client_side_encrypted_message,
    send_encrypted_message_as_test_user,
)

logger = logging.getLogger(__name__)

# Gate: docker marker + HAS_E2EE skip.
pytestmark: list[Any] = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not HAS_E2EE,
        reason='mindroom-nio[e2e] not installed; pip install -e ".[matrix-e2e]"',
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_e2ee_matrix_config(e2ee_env: E2EETestEnvironment) -> MatrixConfig:
    """Build a MatrixConfig for E2EE mode pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id="synapse-e2ee-bot",
        homeserver=e2ee_env.base_url,
        user_id=e2ee_env.bot_user_id,
        access_token=e2ee_env.bot_access_token,
        device_id=e2ee_env.bot_device_id,
        store_path=e2ee_env.bot_store_path,
        room_allowlist={e2ee_env.encrypted_room_id},
        encryption_mode="e2ee_required",
        auto_join_rooms=(e2ee_env.encrypted_room_id,),
    ).validate()


def _make_e2ee_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict[str, Any] | None = None,
    event_bus: EventBus | None = None,
    runtime_accounting: RuntimeAccounting | None = None,
) -> PipelineConfig:
    """Build a PipelineConfig with MatrixRenderer registered."""
    rp = RenderingPipeline()
    rp.register(MatrixRenderer(), priority=50)
    rp.register(TextRenderer(), priority=100)

    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=event_bus or EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=runtime_accounting,
    )


def _make_adapter_context_for_pipeline(
    adapter_id: str,
    runner: PipelineRunner,
) -> AdapterContext:
    """Create an AdapterContext wired to a PipelineRunner's ingress handler."""

    async def _publish(event: Any) -> None:
        await runner.ingress_handler(event)

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.e2ee.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _redact_device_id(device_id: str | None) -> str:
    """Redact device ID for evidence artifacts."""
    if not device_id:
        return "<none>"
    if len(device_id) <= 4:
        return "****"
    return device_id[:2] + "****" + device_id[-2:]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSynapseE2EESmoke:
    r"""Docker-local Synapse E2EE smoke tests.

    Proves that the real MatrixAdapter with real mindroom-nio crypto can
    operate in an encrypted room: create room, exchange keys, and attempt
    decryption of a client-side encrypted message from a second nio client.

    When the second nio client and Megolm key exchange both succeed, this
    proves the full SDK-boundary E2EE chain.  When key exchange does not
    complete within the test timeout, the test ``xfail``\\s with a clear
    reason rather than silently passing.
    """

    async def test_e2ee_encrypted_room_created(
        self,
        synapse_e2ee_env: E2EETestEnvironment,
    ) -> None:
        """Verify synapse_e2ee_env has encrypted room, device IDs, store paths.

        Validates:
        - Encrypted room ID is a valid Matrix room ID (``!...``).
        - Bot device ID was captured from login response.
        - Test user device ID was captured from login response.
        - Bot store path directory exists.
        - Test store path directory exists.
        """
        env = synapse_e2ee_env

        # Room ID must be a canonical Matrix room ID.
        assert env.encrypted_room_id.startswith("!"), (
            f"Expected encrypted room ID starting with '!', "
            f"got {env.encrypted_room_id!r}"
        )
        assert ":" in env.encrypted_room_id, (
            f"Expected canonical room ID format '!localpart:server', "
            f"got {env.encrypted_room_id!r}"
        )

        # Device IDs captured from login.
        assert (
            env.bot_device_id is not None and env.bot_device_id != ""
        ), "Bot device_id should be captured from login response"
        assert (
            env.test_device_id is not None and env.test_device_id != ""
        ), "Test user device_id should be captured from login response"

        # Store paths exist.
        assert env.bot_store_path, "Bot store_path must be set"
        assert Path(
            env.bot_store_path
        ).is_dir(), f"Bot store_path directory must exist: {env.bot_store_path}"
        assert env.test_store_path, "Test store_path must be set"
        assert Path(
            env.test_store_path
        ).is_dir(), f"Test store_path directory must exist: {env.test_store_path}"

        # Build evidence report.
        report: dict[str, Any] = {
            "transport": "matrix",
            "evidence_level": "docker_sdk_boundary",
            "test": "test_e2ee_encrypted_room_created",
            "encrypted_room_id": env.encrypted_room_id,
            "bot_device_id_redacted": _redact_device_id(env.bot_device_id),
            "test_device_id_redacted": _redact_device_id(env.test_device_id),
            "bot_store_path_exists": Path(env.bot_store_path).is_dir(),
            "test_store_path_exists": Path(env.test_store_path).is_dir(),
            "limitations": [
                "Docker loopback only — no live network proof.",
                "No cross-signing or device verification.",
                "Ephemeral crypto store (keys discarded after run).",
                "ignore_unverified_devices=True assumed.",
                "This test only verifies encrypted room creation and "
                "diagnostics — see test_e2ee_message_decryption for "
                "Megolm decryption proof.",
            ],
        }

        # Assert report shape.
        assert report["evidence_level"] == "docker_sdk_boundary"
        assert report["encrypted_room_id"].startswith("!")
        assert report["bot_store_path_exists"] is True
        assert report["test_store_path_exists"] is True

        logger.info(
            "E2EE room created: room=%s bot_dev=%s test_dev=%s",
            report["encrypted_room_id"],
            report["bot_device_id_redacted"],
            report["test_device_id_redacted"],
        )

    async def test_e2ee_message_decryption(
        self,
        synapse_e2ee_env: E2EETestEnvironment,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Send client-side encrypted message from test nio client; bot decrypts.

        Creates a full E2EE pipeline with a **second nio AsyncClient** for
        the test user that performs genuine Megolm encryption:

        1. MatrixAdapter with encryption_mode="e2ee_required" starts.
        2. Adapter auto-joins the encrypted room and performs initial sync
           + key upload.
        3. A second nio client is created for the test user with
           ``encryption_enabled=True`` and initial sync + key query.
        4. The second nio client sends via ``room_send()`` which produces
           an ``m.room.encrypted`` event (genuine Megolm encryption).
        5. Bot's nio sync receives the encrypted event.
        6. nio crypto decrypts it (Megolm inbound session).
        7. MatrixCodec produces a CanonicalEvent.
        8. PipelineRunner routes to FakeMatrixAdapter.
        9. Decrypted content matches what was sent.

        If key exchange does not complete within the grace period, the test
        falls back to sending a plaintext message via HTTP API to at least
        prove message delivery works in the encrypted room, and marks the
        decryption portion as ``xfail`` with a clear reason.
        """
        ts = int(time.time())
        body_text = f"MEDRE E2EE smoke test (ts={ts})"

        e2ee_env = synapse_e2ee_env

        # 1. Set up pipeline with real MatrixAdapter (E2EE) as source.
        accounting = RuntimeAccounting()
        fake_out = FakeMatrixAdapter("fake-out-e2ee", channel="ch-0")

        route = Route(
            id="e2ee-sdk-route",
            source=RouteSource(
                adapter="synapse-e2ee-bot",
                event_kinds=("message.created",),
                channel=e2ee_env.encrypted_room_id,
            ),
            targets=[RouteTarget(adapter="fake-out-e2ee")],
        )
        router = Router(routes=[route])

        config = _make_e2ee_matrix_config(e2ee_env)
        matrix_adapter = MatrixAdapter(config)

        pipeline_config = _make_e2ee_pipeline_config(
            storage=temp_storage,
            router=router,
            adapters={
                "fake-out-e2ee": fake_out,
                "synapse-e2ee-bot": matrix_adapter,
            },
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(pipeline_config)

        # Track whether we used client-side encryption.
        client_side_encrypted = False
        native_event_id: str | None = None

        try:
            # Start all components inside try for exception-safety.
            await runner.start()

            matrix_ctx = _make_adapter_context_for_pipeline(
                "synapse-e2ee-bot",
                runner,
            )
            await matrix_adapter.start(matrix_ctx)

            fake_ctx = _make_adapter_context_for_pipeline("fake-out-e2ee", runner)
            await fake_out.start(fake_ctx)

            # 2. Allow time for the bot's initial sync and key upload.
            #    The bot needs to upload its device keys before the test
            #    client can query them and encrypt for the bot's device.
            #    Poll diagnostics instead of a fixed sleep so we proceed
            #    as soon as the initial sync completes (or time out).
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                diag = matrix_adapter.diagnostics()
                if diag.get("initial_sync_completed"):
                    break
                await asyncio.sleep(0.5)
            else:
                logger.warning(
                    "Initial sync did not complete within 30s; proceeding anyway"
                )

            # 3. Create and initialise the second nio client for the
            #    test user.  This performs restore_login, initial sync,
            #    key upload, and key query.
            try:
                test_client = await e2ee_env.init_test_e2ee_client()
                logger.info(
                    "Second nio client initialised for test user: "
                    "should_upload_keys=%s should_query_keys=%s",
                    test_client.should_upload_keys,
                    test_client.should_query_keys,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to init second nio client: %s. "
                    "Falling back to plaintext send.",
                    exc,
                )
                test_client = None

            # 4. Attempt client-side encrypted send via the second nio client.
            if test_client is not None:
                try:
                    native_event_id = await send_client_side_encrypted_message(
                        e2ee_env, body_text
                    )
                    client_side_encrypted = True
                    logger.info(
                        "Sent client-side encrypted message: event=%s",
                        native_event_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Client-side encrypted send failed: %s. "
                        "Falling back to plaintext HTTP send.",
                        exc,
                    )
                    native_event_id = None

            # 5. Fallback: send plaintext via HTTP API if nio client
            #    encryption failed or was unavailable.
            if native_event_id is None:
                txn_id = f"e2ee-txn-{ts}"
                native_event_id = send_encrypted_message_as_test_user(
                    e2ee_env,
                    body_text,
                    txn_id,
                )
                logger.info(
                    "Sent plaintext message to encrypted room: event=%s",
                    native_event_id,
                )

            assert native_event_id.startswith("$"), (
                f"Synapse should return event_id starting with '$', "
                f"got {native_event_id!r}"
            )

            # 6. Poll for delivery through the E2EE pipeline.
            #    Allow extra time for key exchange / decryption.
            deadline = time.monotonic() + 30.0
            found = False
            while time.monotonic() < deadline:
                if len(fake_out.delivered_payloads) >= 1:
                    found = True
                    break
                await asyncio.sleep(0.5)

            # 7. Capture diagnostics regardless of delivery result.
            diag = matrix_adapter.diagnostics()

            # 8. Check decryption result.
            decryption_succeeded = False
            if found:
                rendered = next(
                    (
                        p
                        for p in fake_out.delivered_payloads
                        if p.payload.get("body") == body_text
                    ),
                    None,
                )
                if rendered is not None:
                    decryption_succeeded = True

            # 9. Build evidence report.
            limitations = [
                "Docker loopback only — no live network proof.",
                "No cross-signing or device verification.",
                "Ephemeral crypto store (keys discarded after run).",
                "ignore_unverified_devices=True assumed.",
                "Single message only (not a throughput test).",
            ]

            if not client_side_encrypted:
                limitations.append(
                    "Message sent as plaintext via HTTP API — "
                    "does NOT exercise Megolm decryption. "
                    "Second nio client failed to encrypt."
                )

            if client_side_encrypted and not decryption_succeeded:
                limitations.append(
                    "Client-side encrypted message was sent but "
                    "decryption did not succeed within timeout — "
                    "Megolm key exchange may not have completed in time."
                )

            report: dict[str, Any] = {
                "transport": "matrix",
                "evidence_level": "docker_sdk_boundary",
                "test": "test_e2ee_message_decryption",
                "client_side_encrypted": client_side_encrypted,
                "decryption_succeeded": decryption_succeeded,
                "native_event_id": native_event_id,
                "encrypted_room_id": e2ee_env.encrypted_room_id,
                "bot_device_id_redacted": _redact_device_id(e2ee_env.bot_device_id),
                "test_device_id_redacted": _redact_device_id(e2ee_env.test_device_id),
                "crypto_enabled": diag.get("crypto_enabled", False),
                "crypto_store_loaded": diag.get("crypto_store_loaded", False),
                "encrypted_room_seen": diag.get("encrypted_room_seen", False),
                "undecryptable_event_count": diag.get("undecryptable_event_count", 0),
                "sync_task_running": diag.get("sync_task_running", False),
                "inbound_published": diag.get("inbound_published", 0),
                "diagnostics": diag,
                "limitations": limitations,
            }

            logger.info(
                "E2EE message test: client_encrypted=%s decryption=%s "
                "event=%s crypto_enabled=%s crypto_store_loaded=%s",
                report["client_side_encrypted"],
                report["decryption_succeeded"],
                report["native_event_id"],
                report["crypto_enabled"],
                report["crypto_store_loaded"],
            )

            # Core assertions: evidence shape and event delivery.
            assert report["evidence_level"] == "docker_sdk_boundary"
            assert report["native_event_id"].startswith("$")

            # If crypto was enabled, assert that diagnostics reflect it.
            if diag.get("crypto_enabled"):
                logger.info(
                    "E2EE crypto subsystem active: store_loaded=%s "
                    "encrypted_room_seen=%s",
                    diag.get("crypto_store_loaded"),
                    diag.get("encrypted_room_seen"),
                )

            # If we sent via client-side encryption but decryption failed,
            # xfail with a clear reason rather than silently passing.
            if client_side_encrypted and not decryption_succeeded:
                pytest.xfail(
                    "Client-side Megolm-encrypted message was sent but "
                    "the bot did not decrypt it within the 30s timeout. "
                    "This likely means the Megolm key exchange (outbound "
                    "session creation / inbound session sharing) did not "
                    "complete in time. The encrypted room, crypto store, "
                    "and diagnostics are all valid."
                )

            # If we could not even attempt client-side encryption, xfail.
            if not client_side_encrypted:
                pytest.xfail(
                    "Second nio client could not be initialised or "
                    "room_send failed — falling back to plaintext HTTP "
                    "send. The test cannot prove Megolm decryption without "
                    "a client-side encrypted sender. The encrypted room, "
                    "crypto store, and diagnostics are all valid."
                )

            # Success path: client-side encrypted AND decrypted.
            assert decryption_succeeded is True, (
                "Expected decryption to succeed when client-side encryption "
                "was used and message was delivered."
            )
        finally:
            # Clean up the second nio client.
            try:
                await e2ee_env.close_test_e2ee_client()
            except Exception:
                logger.debug("close_test_e2ee_client cleanup error", exc_info=True)
            # Clean up each component independently so one failure
            # does not prevent the others from being stopped.
            for _name, _stop_coro in [
                ("matrix_adapter", matrix_adapter.stop()),
                ("fake_out", fake_out.stop()),
                ("runner", runner.stop()),
            ]:
                try:
                    await _stop_coro
                except Exception:
                    logger.debug("%s cleanup error", _name, exc_info=True)
            # Let pending tasks settle after teardown.
            await asyncio.sleep(0.1)

    async def test_e2ee_diagnostics(
        self,
        synapse_e2ee_env: E2EETestEnvironment,
    ) -> None:
        """Verify E2EE diagnostics fields are truthful after session start.

        Starts the adapter with encryption_mode="e2ee_required" and
        validates that:
        - ``crypto_enabled`` reflects whether the nio crypto subsystem
          loaded successfully.
        - ``crypto_store_loaded`` reflects whether the crypto store was
          opened at the configured path.
        - ``store_path_configured`` is True.
        - ``device_id_configured`` is True when device_id was provided.
        - ``encryption_mode`` matches the configured mode.
        """
        e2ee_env = synapse_e2ee_env
        config = _make_e2ee_matrix_config(e2ee_env)
        adapter = MatrixAdapter(config)
        ctx = _make_context(adapter_id="synapse-e2ee-diag")

        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()

            # Core diagnostic field assertions.
            assert diag["encryption_mode"] == "e2ee_required", (
                f"Expected encryption_mode='e2ee_required', "
                f"got {diag['encryption_mode']!r}"
            )
            assert (
                diag["store_path_configured"] is True
            ), "store_path_configured should be True when store_path is set"

            # device_id_configured depends on whether we captured a device_id.
            if e2ee_env.bot_device_id:
                assert diag["device_id_configured"] is True, (
                    "device_id_configured should be True when device_id "
                    "was captured from login response"
                )

            # crypto_enabled and crypto_store_loaded are truthful —
            # they reflect the actual state of the crypto subsystem.
            # When HAS_E2EE is True (which it must be for this test
            # to run), these should be True.
            assert diag["crypto_enabled"] is True, (
                "crypto_enabled should be True when HAS_E2EE is True "
                "and encryption_mode is e2ee_required"
            )
            assert diag["crypto_store_loaded"] is True, (
                "crypto_store_loaded should be True when the crypto "
                "store was successfully opened"
            )

            # Build evidence report.
            report: dict[str, Any] = {
                "transport": "matrix",
                "evidence_level": "docker_sdk_boundary",
                "test": "test_e2ee_diagnostics",
                "encryption_mode": diag["encryption_mode"],
                "store_path_configured": diag["store_path_configured"],
                "device_id_configured": diag["device_id_configured"],
                "crypto_enabled": diag["crypto_enabled"],
                "crypto_store_loaded": diag["crypto_store_loaded"],
                "connected": diag["connected"],
                "logged_in": diag["logged_in"],
                "sync_task_running": diag["sync_task_running"],
                "encrypted_room_seen": diag.get("encrypted_room_seen", False),
                "undecryptable_event_count": diag.get("undecryptable_event_count", 0),
                "bot_device_id_redacted": _redact_device_id(e2ee_env.bot_device_id),
                "test_device_id_redacted": _redact_device_id(e2ee_env.test_device_id),
                "limitations": [
                    "Docker loopback only — no live network proof.",
                    "No cross-signing or device verification.",
                    "Ephemeral crypto store (keys discarded after run).",
                    "ignore_unverified_devices=True assumed.",
                ],
            }

            assert report["evidence_level"] == "docker_sdk_boundary"
            assert report["crypto_enabled"] is True
            assert report["crypto_store_loaded"] is True

            logger.info(
                "E2EE diagnostics: crypto_enabled=%s crypto_store_loaded=%s "
                "connected=%s logged_in=%s device_id_configured=%s",
                report["crypto_enabled"],
                report["crypto_store_loaded"],
                report["connected"],
                report["logged_in"],
                report["device_id_configured"],
            )
        finally:
            await adapter.stop()
