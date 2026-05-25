"""Shared helpers for Synapse integration tests.

Contains inbound path constants, IngressResult, and the sync/fallback
polling helper used by multiple integration test files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.core.contracts.adapter import AdapterContext

from .conftest import E2EETestEnvironment
from .conftest import SynapseEnvironment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inbound path constants — used in report.inbound_path
# ---------------------------------------------------------------------------

INBOUND_SYNC_LOOP = "sync_loop"
INBOUND_FALLBACK = "direct_on_room_message_fallback"


# ---------------------------------------------------------------------------
# Inbound path result
# ---------------------------------------------------------------------------


class IngressResult:
    """Records which ingress path delivered the event and related metadata.

    Attributes
    ----------
    ingress_path:
        ``"sync_loop"`` if the real nio sync_forever callback delivered the
        event, or ``"direct_on_room_message_fallback"`` if the fallback path
        was used.
    native_event_id:
        The Synapse-assigned Matrix event_id (``$...``).
    body_text:
        The plain-text body of the test message.
    fallback_reason:
        Human-readable reason why fallback was used, or ``""`` if sync_loop
        succeeded.  One of: ``"sync_timeout"``, ``"sync_error"``,
        ``"sync_not_running"``, or ``""`` (no fallback).
    sync_health:
        Snapshot of adapter diagnostics at the time fallback was triggered.
        Empty dict if sync_loop succeeded.
    """

    __slots__ = (
        "ingress_path",
        "native_event_id",
        "body_text",
        "fallback_reason",
        "sync_health",
    )

    def __init__(
        self,
        ingress_path: str,
        native_event_id: str,
        body_text: str,
        fallback_reason: str = "",
        sync_health: dict[str, Any] | None = None,
    ) -> None:
        self.ingress_path = ingress_path
        self.native_event_id = native_event_id
        self.body_text = body_text
        self.fallback_reason = fallback_reason
        self.sync_health = sync_health or {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def capture_sync_health(adapter: MatrixAdapter) -> dict[str, Any]:
    """Snapshot adapter diagnostics for fallback reason analysis."""
    try:
        return adapter.diagnostics()
    except Exception:
        return {"error": "diagnostics() raised"}


def classify_fallback_reason(adapter: MatrixAdapter) -> str:
    """Classify WHY fallback was needed based on adapter diagnostics.

    Returns one of:
    - ``"sync_not_running"``: sync task is not alive.
    - ``"sync_error"``: sync task ran but last_sync_error is set.
    - ``"sync_timeout"``: sync task alive, no error, but no event delivered.
    """
    diag = capture_sync_health(adapter)
    if not diag.get("sync_task_running", False):
        return "sync_not_running"
    if diag.get("last_sync_error") is not None:
        return "sync_error"
    return "sync_timeout"


def send_message_as_test_user(
    env: SynapseEnvironment,
    body: str,
    txn_id: str,
) -> str:
    """Send a message as the test user via Synapse HTTP API.

    Returns the Matrix event_id assigned by Synapse.
    """
    payload = json.dumps(
        {
            "msgtype": "m.text",
            "body": body,
        }
    ).encode()
    url = (
        f"{env.base_url}/_matrix/client/v3/rooms/{env.test_room_id}"
        f"/send/m.room.message/{txn_id}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env.test_access_token}",
        },
        method="PUT",
    )
    with urllib.request.urlopen(
        req, timeout=10
    ) as resp:  # nosec: localhost test container
        resp_body = json.loads(resp.read())
    return resp_body["event_id"]


async def wait_for_sync_or_fallback(
    *,
    synapse_env: SynapseEnvironment,
    matrix_adapter: MatrixAdapter,
    fake_out: FakeMatrixAdapter,
    body_text: str,
    txn_id: str,
    timeout: float = 25.0,
    poll_interval: float = 0.3,
    fallback_grace: float = 0.5,
) -> IngressResult:
    """Send a message and wait for delivery; return the ingress path used.

    1. Sends a message as the test user via Synapse HTTP API.
    2. Polls ``fake_out.delivered_payloads`` for up to *timeout* seconds.
       During polling, periodically logs sync health diagnostics.
    3. If the sync loop delivers within *timeout*, returns
       :pyattr:`ingress_path` ``"sync_loop"``.
    4. Otherwise classifies the fallback reason, logs it, calls
       ``_on_room_message`` directly with a real Synapse ``event_id``,
       and returns :pyattr:`ingress_path`
       ``"direct_on_room_message_fallback"``.

    The caller can inspect ``result.ingress_path`` to distinguish full
    SDK-boundary proof from codec+pipeline-only proof, and
    ``result.fallback_reason`` to understand WHY fallback was needed.
    """
    # 1. Send via Synapse HTTP API.
    native_event_id = send_message_as_test_user(
        synapse_env,
        body_text,
        txn_id,
    )
    assert native_event_id.startswith("$"), (
        f"Synapse should return event_id starting with '$', " f"got {native_event_id!r}"
    )

    # 2. Poll for the sync loop to deliver through the pipeline.
    deadline = time.monotonic() + timeout
    found = False
    last_health_log = 0.0
    while time.monotonic() < deadline:
        if len(fake_out.delivered_payloads) >= 1:
            found = True
            break
        # Periodically log sync health (every ~3s) for diagnostics.
        now = time.monotonic()
        if now - last_health_log >= 3.0:
            health = capture_sync_health(matrix_adapter)
            logger.info(
                "Sync poll: waiting for delivery. "
                "sync_task_running=%s sync_running=%s "
                "last_sync_error=%s last_successful_sync=%s "
                "inbound_published=%d elapsed=%.1fs",
                health.get("sync_task_running"),
                health.get("sync_running"),
                health.get("last_sync_error"),
                health.get("last_successful_sync"),
                health.get("inbound_published", 0),
                timeout - (deadline - now),
            )
            last_health_log = now
        await asyncio.sleep(poll_interval)

    # 3. Record which ingress path was used.
    if found:
        logger.info(
            "Sync loop delivered event %s within timeout",
            native_event_id,
        )
        return IngressResult(
            ingress_path=INBOUND_SYNC_LOOP,
            native_event_id=native_event_id,
            body_text=body_text,
        )

    # 4. Fallback: classify why and log it.
    reason = classify_fallback_reason(matrix_adapter)
    sync_health = capture_sync_health(matrix_adapter)

    logger.warning(
        "Matrix bridge smoke: sync loop did not deliver within %.1fs; "
        "using direct _on_room_message fallback. "
        "reason=%s native_event_id=%s sync_task_running=%s "
        "last_sync_error=%s inbound_published=%d",
        timeout,
        reason,
        native_event_id,
        sync_health.get("sync_task_running"),
        sync_health.get("last_sync_error"),
        sync_health.get("inbound_published", 0),
    )

    from types import SimpleNamespace

    room = SimpleNamespace(room_id=synapse_env.test_room_id)
    event = SimpleNamespace(
        sender=synapse_env.test_user_id,
        event_id=native_event_id,
        body=body_text,
        source={
            "content": {"msgtype": "m.text", "body": body_text},
            "event_id": native_event_id,
            "sender": synapse_env.test_user_id,
            "type": "m.room.message",
        },
    )
    await matrix_adapter._on_room_message(room, event)
    await asyncio.sleep(fallback_grace)

    return IngressResult(
        ingress_path=INBOUND_FALLBACK,
        native_event_id=native_event_id,
        body_text=body_text,
        fallback_reason=reason,
        sync_health=sync_health,
    )


def make_context(adapter_id: str = "synapse-bridge-bot") -> AdapterContext:
    """Build an AdapterContext wired to a mock publish_inbound."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def send_encrypted_message_as_test_user(
    e2ee_env: E2EETestEnvironment,
    body: str,
    txn_id: str,
) -> str:
    """Send a plaintext message to the encrypted room via Synapse HTTP API.

    Sends an unencrypted ``m.room.message`` via the Matrix CS API.
    The message arrives at the bot as a plaintext ``RoomMessageText``
    (Synapse does **not** encrypt client-submitted messages server-side,
    so this does NOT exercise the Megolm decryption path).

    Use this helper to verify the adapter receives messages in an
    encrypted room and that crypto infrastructure initialises correctly.
    For genuine Megolm decryption validation, send from a second
    E2EE-capable nio client.

    Returns the Matrix event_id assigned by Synapse.
    """
    payload = json.dumps(
        {
            "msgtype": "m.text",
            "body": body,
        }
    ).encode()
    url = (
        f"{e2ee_env.base_url}/_matrix/client/v3/rooms/"
        f"{e2ee_env.encrypted_room_id}/send/m.room.message/{txn_id}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {e2ee_env.test_access_token}",
        },
        method="PUT",
    )
    with urllib.request.urlopen(
        req, timeout=10
    ) as resp:  # nosec: localhost test container
        resp_body = json.loads(resp.read())
    return resp_body["event_id"]
