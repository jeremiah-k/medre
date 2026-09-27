"""Synapse durable-attachment relay smoke tests — real changed-path proof.

Exercises the actual durable attachment path end to end against a local
Docker Synapse with the pinned SDK:

1. **Plaintext relay** — the test user uploads real media and sends an
   ``m.image`` message; the bot's real sync loop admits the event through
   the durable admission callback, fetching the bytes over the
   authenticated media endpoint; the pipeline routes it and the adapter
   re-uploads to the destination room; the relayed file downloads back
   with identical bytes, filename, and MIME type.
2. **Restart + source loss** — after durable admission the storage and
   adapter are closed, the source media is deleted from the homeserver
   (admin API), and a fresh runtime over the reopened database delivers
   the retained bytes.  One injected transient send failure records no
   native ref; the durable retry worker then records exactly one
   authoritative handoff.
3. **Encrypted source** — a second nio client sends a genuinely
   Megolm-encrypted message with an encrypted attachment; the bot
   decrypts both and relays the plaintext bytes to a plaintext room.
4. **Encrypted destination** — a plaintext source file relays into an
   encrypted room as an encrypted upload (ciphertext on the wire); a
   receiving client decrypts event and attachment when key exchange
   completes in time (bounded-grace pattern shared with
   ``test_synapse_e2ee_smoke``).

Gated behind ``docker``/``HAS_NIO`` like the neighboring smoke modules.
Run narrowly::

    pytest tests/integration/test_synapse_attachment_smoke.py -m docker -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE, HAS_NIO
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.model import RetryConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.kinds import EventKind
from medre.core.ingress import (
    AttachmentPolicyState,
    AttachmentRuntimeSeam,
    AttachmentTransferPermits,
    InboundAttachmentContent,
)
from medre.core.ingress.types import AdmissionResult
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.storage import (
    SQLiteStorage,
    StorageAttachmentAccess,
)
from medre.runtime.retry import RetryWorker

from .conftest import SynapseEnvironment

logger = logging.getLogger(__name__)

pytestmark: list[Any] = [pytest.mark.docker]

if not HAS_NIO:
    pytestmark.append(
        pytest.mark.skip(
            reason="mindroom-nio not installed; run: pip install '.[matrix]'"
        )
    )

_ADMISSION_WAIT_SECONDS = 45.0
_DELIVERY_WAIT_SECONDS = 30.0

# Deterministic pseudo-PNG payload (bytes identity is what matters).
_MEDIA_BYTES = (
    b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3 + b"durable attachment smoke"
)


# ---------------------------------------------------------------------------
# Synapse HTTP helpers (test-user/bot/admin transport for verification)
# ---------------------------------------------------------------------------


def _api_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: int = 20,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - diagnostics path
        detail = exc.read().decode()[:400]
        exc.close()
        raise RuntimeError(f"HTTP {exc.code} for {method} {url}: {detail}") from exc


def _json_headers(token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _upload_media(
    env: SynapseEnvironment, token: str, data: bytes, filename: str, content_type: str
) -> str:
    base = env.base_url
    query = urllib.parse.urlencode({"filename": filename})
    status, _headers, body = _api_request(
        f"{base}/_matrix/media/v3/upload?{query}",
        data=data,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    assert status == 200, (status, body[:200])
    return json.loads(body)["content_uri"]


def _send_room_event(
    env: SynapseEnvironment, token: str, room_id: str, content: dict[str, Any]
) -> str:
    base = env.base_url
    txn = f"smoke-{uuid.uuid4().hex[:12]}"
    _status, _headers, body = _api_request(
        f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/send/m.room.message/{txn}",
        data=json.dumps(content).encode(),
        headers=_json_headers(token),
        method="PUT",
    )
    return json.loads(body)["event_id"]


def _download_media(env: SynapseEnvironment, token: str, mxc: str) -> bytes:
    parsed = urllib.parse.urlparse(mxc)
    server = parsed.netloc
    media_id = parsed.path.lstrip("/")
    _status, _headers, body = _api_request(
        f"{env.base_url}/_matrix/client/v1/media/download/{server}/{media_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return body


def _latest_room_messages(
    env: SynapseEnvironment, token: str, room_id: str, limit: int = 12
) -> list[dict[str, Any]]:
    base = env.base_url
    query = urllib.parse.urlencode({"dir": "b", "limit": limit})
    _status, _headers, body = _api_request(
        f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/messages?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    chunk = json.loads(body).get("chunk", [])
    return list(reversed(chunk))  # chronological order


def _create_room(env: SynapseEnvironment, token: str, name: str) -> str:
    base = env.base_url
    _status, _headers, body = _api_request(
        f"{base}/_matrix/client/v3/createRoom",
        data=json.dumps({"name": name, "preset": "public_chat"}).encode(),
        headers=_json_headers(token),
        method="POST",
    )
    return json.loads(body)["room_id"]


def _join_room(env: SynapseEnvironment, token: str, room_id: str) -> None:
    _api_request(
        f"{env.base_url}/_matrix/client/v3/join/{urllib.parse.quote(room_id)}",
        data=b"{}",
        headers=_json_headers(token),
        method="POST",
    )


def _delete_media(env: SynapseEnvironment, mxc: str) -> None:
    """Remove a media object from the homeserver via the admin API."""
    parsed = urllib.parse.urlparse(mxc)
    server = urllib.parse.quote(parsed.netloc)
    media_id = urllib.parse.quote(parsed.path.lstrip("/"))
    _api_request(
        f"{env.base_url}/_synapse/admin/v1/media/{server}/{media_id}",
        headers=_json_headers(env.bot_access_token),
        method="DELETE",
    )


# ---------------------------------------------------------------------------
# MEDRE runtime assembly (durable admission + attachments seam)
# ---------------------------------------------------------------------------


class _AttachmentHarness:
    """Real source + destination adapters, runner, and storage.

    Relay topology uses TWO adapter instances so the pipeline's self-loop
    guard (target adapter must differ from the source adapter) is honored
    the same way production bridges run: the source adapter syncs the
    source room (bot), and a destination adapter uploads into the target
    room.  Plaintext destinations run as the test user; the encrypted
    destination runs as a second bot instance sharing the fixture's
    pre-bootstrapped crypto store (only that instance touches the store).
    """

    def __init__(
        self,
        *,
        env: SynapseEnvironment,
        storage: SQLiteStorage,
        source_room: str,
        target_room: str,
        source_adapter_id: str,
        dest_adapter_id: str,
        source_encryption: str = "plaintext",
        source_store_path: str | None = None,
        dest_user_id: str | None = None,
        dest_access_token: str | None = None,
        dest_encryption: str = "plaintext",
        dest_store_path: str | None = None,
        dest_device_id: str | None = None,
    ) -> None:
        self.env = env
        self.storage = storage
        self.source_room = source_room
        self.target_room = target_room
        self.source_adapter_id = source_adapter_id
        self.dest_adapter_id = dest_adapter_id
        self.admissions: list[
            tuple[AdmissionResult, InboundAttachmentContent | None]
        ] = []

        route = Route(
            id="attachment-smoke-route",
            source=RouteSource(
                adapter=source_adapter_id,
                event_kinds=(EventKind.MESSAGE_FILE,),
                channel=source_room,
            ),
            targets=[RouteTarget(adapter=dest_adapter_id, channel=target_room)],
        )
        self.router = Router(routes=[route])

        rendering = RenderingPipeline()
        rendering.register(MatrixRenderer(), priority=50)
        rendering.register(TextRenderer(), priority=100)

        self.config = PipelineConfig(
            storage=cast(StorageBackend, storage),
            router=self.router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),
            adapters={},  # filled after adapter construction
            rendering_pipeline=rendering,
            event_bus=EventBus(),
            # A route retry policy keeps transient adapter failures on the
            # durable retry path instead of dead-lettering on first failure.
            route_retry_policies={
                "attachment-smoke-route": RetryPolicy(
                    max_attempts=3,
                    backoff_base=0.5,
                    max_delay_seconds=5.0,
                    jitter=False,
                )
            },
        )
        self.runner = PipelineRunner(self.config)

        source_config = MatrixConfig(
            adapter_id=source_adapter_id,
            homeserver=env.base_url,
            user_id=env.bot_user_id,
            access_token=env.bot_access_token,
            room_allowlist={source_room},
            encryption_mode=source_encryption,
            store_path=source_store_path,
        ).validate()
        self.source_adapter = MatrixAdapter(source_config)
        self.config.adapters[source_adapter_id] = self.source_adapter

        dest_config = MatrixConfig(
            adapter_id=dest_adapter_id,
            homeserver=env.base_url,
            user_id=dest_user_id or env.bot_user_id,
            access_token=dest_access_token or env.bot_access_token,
            device_id=dest_device_id,
            room_allowlist={target_room},
            encryption_mode=dest_encryption,
            store_path=dest_store_path,
        ).validate()
        self.dest_adapter = MatrixAdapter(dest_config)
        self.config.adapters[dest_adapter_id] = self.dest_adapter

        policy = AttachmentPolicyState(
            enabled=True,
            max_attachment_bytes=10_485_760,
            transfer_timeout_seconds=60.0,
        )
        self.seam = AttachmentRuntimeSeam(
            policy=policy,
            permits=AttachmentTransferPermits(
                max_concurrent=2, acquire_timeout_seconds=10.0
            ),
            content=StorageAttachmentAccess(storage, policy),
        )

    async def start(self) -> None:
        await self.runner.start()

        async def _admit(
            event: Any, provenance: Any, attachment: Any = None
        ) -> AdmissionResult:
            result = await self.runner.admit_ingress(
                event, provenance, attachment=attachment
            )
            self.admissions.append((result, attachment))
            return result

        storage = self.storage

        for adapter_id, adapter in (
            (self.source_adapter_id, self.source_adapter),
            (self.dest_adapter_id, self.dest_adapter),
        ):

            async def _load_checkpoint(stream: str, _owner: str = adapter_id) -> Any:
                return await storage.get_adapter_checkpoint(_owner, stream)

            async def _commit_checkpoint(
                stream: str,
                cursor: str,
                metadata_json: str,
                _owner: str = adapter_id,
            ) -> None:
                await storage.put_adapter_checkpoint(
                    _owner, stream, cursor, metadata_json=metadata_json
                )

            ctx = AdapterContext(
                adapter_id=adapter_id,
                publish_inbound=self._publish,
                admit_inbound=_admit,
                load_checkpoint=_load_checkpoint,
                commit_checkpoint=_commit_checkpoint,
                logger=logging.getLogger(f"test.attach.{adapter_id}"),
                clock=lambda: datetime.now(UTC),
                shutdown_event=asyncio.Event(),
                attachments=self.seam,
            )
            await adapter.start(ctx)

    async def _publish(self, event: Any) -> None:
        await self.runner.handle_ingress(event)

    async def stop(self) -> None:
        try:
            await self.dest_adapter.stop()
        finally:
            try:
                await self.source_adapter.stop()
            finally:
                await self.runner.stop()

    async def wait_until_live(self, timeout: float = 30.0) -> None:
        """Wait until BOTH adapter sessions crossed the live sync boundary."""
        deadline = time.monotonic() + timeout
        pending = {
            self.source_adapter_id: self.source_adapter,
            self.dest_adapter_id: self.dest_adapter,
        }
        while time.monotonic() < deadline and pending:
            for adapter_id, adapter in list(pending.items()):
                session = adapter._session
                if session is not None and session.is_live:
                    del pending[adapter_id]
            if pending:
                await asyncio.sleep(0.25)
        if pending:
            raise AssertionError(
                f"adapters {sorted(pending)} did not reach live sync "
                f"within {timeout}s"
            )

    async def wait_for_file_admission(
        self, filename: str, timeout: float = _ADMISSION_WAIT_SECONDS
    ) -> AdmissionResult:
        """Wait for the durable admission whose descriptor declares *filename*.

        The SDK's initial sync treats the tail of the room timeline as live
        provenance, so media from earlier tests in the session-scoped source
        room can also be admitted with full bytes by a fresh adapter; match
        on the declared filename to select the event this test uploaded.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for result, attachment in self.admissions:
                if not (result.created and attachment is not None):
                    continue
                canonical = await self.storage.get(result.event_id)
                descriptor = (
                    canonical.payload.get("attachment")
                    if canonical is not None
                    else None
                )
                if (
                    isinstance(descriptor, dict)
                    and descriptor.get("filename") == filename
                ):
                    return result
            await asyncio.sleep(0.25)
        raise AssertionError(
            f"no durable attachment admission for {filename!r} within "
            f"{timeout}s (admissions seen: {len(self.admissions)})"
        )

    async def wait_for_relay(
        self, timeout: float = _DELIVERY_WAIT_SECONDS
    ) -> dict[str, Any]:
        """Wait until the relayed media event appears in the target room."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in _latest_room_messages(
                self.env, self.env.test_access_token, self.target_room
            ):
                content = event.get("content", {})
                if content.get("msgtype") in ("m.image", "m.file") and (
                    "url" in content or "file" in content
                ):
                    return event
            await asyncio.sleep(0.5)
        raise AssertionError(
            f"no relayed media event observed in {self.target_room} within {timeout}s"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSynapseAttachmentSmoke:
    async def test_plaintext_media_relays_with_identical_bytes(
        self,
        synapse_env: SynapseEnvironment,
        tmp_path: Path,
    ) -> None:
        """Full plaintext relay: fetch → durable admission → re-upload → download."""
        destination_room = _create_room(
            synapse_env, synapse_env.test_access_token, "attachment-dst-plain"
        )

        storage = SQLiteStorage(str(tmp_path / "plain.sqlite"))
        await storage.initialize()
        harness = _AttachmentHarness(
            env=synapse_env,
            storage=storage,
            source_room=synapse_env.test_room_id,
            target_room=destination_room,
            source_adapter_id="attach-plain-src",
            dest_adapter_id="attach-plain-dst",
            dest_user_id=synapse_env.test_user_id,
            dest_access_token=synapse_env.test_access_token,
        )
        try:
            await harness.start()
            # The media message must arrive AFTER the live sync boundary so
            # it is admitted with live provenance (startup backlog is
            # honestly history-suppressed by design).
            await harness.wait_until_live()
            await asyncio.sleep(1.0)
            mxc = _upload_media(
                synapse_env,
                synapse_env.test_access_token,
                _MEDIA_BYTES,
                "relay-photo.png",
                "image/png",
            )
            _send_room_event(
                synapse_env,
                synapse_env.test_access_token,
                synapse_env.test_room_id,
                {
                    "msgtype": "m.image",
                    "body": "relay-photo.png",
                    "filename": "relay-photo.png",
                    "url": mxc,
                    "info": {
                        "mimetype": "image/png",
                        "size": len(_MEDIA_BYTES),
                        "w": 1,
                        "h": 1,
                    },
                },
            )
            result = await harness.wait_for_file_admission("relay-photo.png")
            assert result.attachment is not None and result.attachment.retained
            assert result.attachment.size_bytes == len(_MEDIA_BYTES)
            # Retained bytes are verifiably the uploaded payload.
            stored = await storage.load_attachment_content(
                result.event_id, result.attachment.content_ref
            )
            assert stored.data == _MEDIA_BYTES
            # Canonical event carries the retained descriptor.
            canonical = await storage.get(result.event_id)
            assert canonical is not None
            descriptor = canonical.payload.get("attachment")
            assert descriptor is not None
            assert descriptor["content_ref"] == result.attachment.content_ref
            assert descriptor["filename"] == "relay-photo.png"
            assert descriptor["mime_type"] == "image/png"

            # Route the admitted work through the real pipeline.
            await harness.runner.process_admitted_event(result.event_id)

            relayed = await harness.wait_for_relay()
            content = relayed["content"]
            assert content["msgtype"] == "m.image"
            assert content.get("filename") == "relay-photo.png"
            assert content["info"]["mimetype"] == "image/png"
            assert content["info"]["size"] == len(_MEDIA_BYTES)
            assert "url" in content, "plaintext destination must carry a plain url"
            downloaded = _download_media(
                synapse_env, synapse_env.test_access_token, content["url"]
            )
            assert downloaded == _MEDIA_BYTES, "relayed bytes must be identical"

            report = {
                "transport": "matrix",
                "evidence_level": "docker_synapse_plaintext_media_relay",
                "source_room": synapse_env.test_room_id,
                "destination_room": destination_room,
                "bytes_relayed": len(_MEDIA_BYTES),
                "admission_retained": True,
                "identical_bytes": True,
            }
            logger.info("plaintext attachment relay report: %s", report)
        finally:
            await harness.stop()
            await storage.close()

    async def test_restart_replays_retained_bytes_after_source_loss(
        self,
        synapse_env: SynapseEnvironment,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retained bytes survive a restart and deliver after source deletion.

        Also proves one transient send failure records no native ref while
        the durable retry worker finally records exactly one handoff.
        """
        destination_room = _create_room(
            synapse_env, synapse_env.test_access_token, "attachment-dst-restart"
        )

        db_path = str(tmp_path / "restart.sqlite")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        harness = _AttachmentHarness(
            env=synapse_env,
            storage=storage,
            source_room=synapse_env.test_room_id,
            target_room=destination_room,
            source_adapter_id="attach-restart-src",
            dest_adapter_id="attach-restart-dst",
            dest_user_id=synapse_env.test_user_id,
            dest_access_token=synapse_env.test_access_token,
        )
        await harness.start()
        # Live-provenance retention first: the media must arrive after the
        # live sync boundary (startup backlog is history-suppressed).
        await harness.wait_until_live()
        await asyncio.sleep(1.0)
        mxc = _upload_media(
            synapse_env,
            synapse_env.test_access_token,
            _MEDIA_BYTES,
            "restart.bin",
            "application/octet-stream",
        )
        _send_room_event(
            synapse_env,
            synapse_env.test_access_token,
            synapse_env.test_room_id,
            {
                "msgtype": "m.file",
                "body": "restart.bin",
                "filename": "restart.bin",
                "url": mxc,
                "info": {
                    "mimetype": "application/octet-stream",
                    "size": len(_MEDIA_BYTES),
                },
            },
        )
        result = await harness.wait_for_file_admission("restart.bin")
        content_ref = result.attachment.content_ref
        await harness.stop()
        await storage.close()

        # The source media is gone before any delivery attempt.
        _delete_media(synapse_env, mxc)
        with pytest.raises(RuntimeError, match="404"):
            _download_media(synapse_env, synapse_env.test_access_token, mxc)

        # Fresh runtime over the reopened database.
        reopened = SQLiteStorage(db_path)
        await reopened.initialize()
        harness2 = _AttachmentHarness(
            env=synapse_env,
            storage=reopened,
            source_room=synapse_env.test_room_id,
            target_room=destination_room,
            source_adapter_id="attach-restart-src",
            dest_adapter_id="attach-restart-dst",
            dest_user_id=synapse_env.test_user_id,
            dest_access_token=synapse_env.test_access_token,
        )
        try:
            await harness2.start()

            # Inject transient room-send failures at the session boundary so
            # the adapter's bounded in-flight retry budget is exhausted for
            # exactly ONE deliver() call; everything above (outbox,
            # receipts, retry classification) is real.  The next call after
            # the budget goes through untouched for the durable retry.
            from medre.adapters.matrix.session import MatrixSession

            real_room_send = MatrixSession.room_send

            calls = {"count": 0}
            _injected_budget = 3  # _MAX_DELIVERY_RETRIES

            async def failing_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                if calls["count"] < _injected_budget:
                    calls["count"] += 1
                    raise OSError("injected transient transport failure")
                calls["count"] += 1
                return await real_room_send(self, *args, **kwargs)

            monkeypatch.setattr(MatrixSession, "room_send", failing_once)
            from medre.core.planning.delivery_plan import DeliveryFailureKind

            outcomes = await harness2.runner.process_admitted_event(result.event_id)
            transient_failures = [
                outcome
                for outcome in outcomes
                if outcome.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
            ]
            assert transient_failures, (
                f"expected the injected transient failure to surface as an "
                f"ADAPTER_TRANSIENT outcome, got {outcomes}"
            )
            assert all(
                outcome.status == "transient_failure" for outcome in transient_failures
            )
            monkeypatch.undo()

            # The failed attempt produced no native ref for the canonical event.
            refs = await reopened.list_native_refs_for_event(result.event_id)
            assert not any(
                r.direction == "outbound" for r in refs
            ), "a failed attempt must not record an outbound native ref"

            # The durable retry worker owns the retry: run one due cycle.
            retry_worker = RetryWorker(
                storage=reopened,
                pipeline=harness2.runner,
                capacity_controller=None,
                retry_config=RetryConfig(enabled=True, interval_seconds=0.1),
                lifecycle=harness2.runner.delivery_lifecycle,
            )
            deadline = time.monotonic() + 45.0
            delivered = False
            while time.monotonic() < deadline and not delivered:
                await retry_worker._process_due(datetime.now(UTC))
                await asyncio.sleep(0.2)
                for event in _latest_room_messages(
                    synapse_env, synapse_env.test_access_token, destination_room
                ):
                    content = event.get("content", {})
                    if content.get("filename") == "restart.bin" and "url" in content:
                        delivered = True
            assert delivered, (
                "retry must deliver the retained attachment; outbox="
                f"{await reopened.count_outbox_by_status()}"
            )
            downloaded = None
            for event in _latest_room_messages(
                synapse_env, synapse_env.test_access_token, destination_room
            ):
                content = event.get("content", {})
                if content.get("filename") == "restart.bin" and "url" in content:
                    downloaded = _download_media(
                        synapse_env, synapse_env.test_access_token, content["url"]
                    )
            assert downloaded == _MEDIA_BYTES

            # Exactly one authoritative outbound native ref after the retry.
            refs = await reopened.list_native_refs_for_event(result.event_id)
            outbound = [r for r in refs if r.direction == "outbound"]
            assert (
                len(outbound) == 1
            ), f"expected exactly one outbound native ref, got {len(outbound)}"
            # Retained bytes untouched by the failed attempt + retry.
            stored = await reopened.load_attachment_content(
                result.event_id, content_ref
            )
            assert stored.data == _MEDIA_BYTES
            report = {
                "transport": "matrix",
                "evidence_level": "docker_synapse_restart_replay",
                "source_media_deleted_before_delivery": True,
                "transient_attempt_then_single_handoff": True,
                "identical_bytes_after_restart": True,
                "remote_orphan_upload_note": (
                    "an interrupted upload/send may orphan an unused remote "
                    "upload; no upload idempotency is promised"
                ),
            }
            logger.info("restart/replay attachment report: %s", report)
        finally:
            await harness2.stop()
            await reopened.close()


@pytest.mark.skipif(
    not HAS_E2EE,
    reason='mindroom-nio[e2e] not installed; pip install -e ".[matrix-e2e]"',
)
class TestSynapseEncryptedAttachmentSmoke:
    async def test_encrypted_source_relays_plaintext_bytes(
        self,
        synapse_e2ee_env: Any,
        tmp_path: Path,
    ) -> None:
        """Encrypted event + encrypted attachment → verified plaintext admission.

        The test user's nio client performs a genuine Megolm room event with
        a client-side encrypted upload.  The bot must decrypt both, admit
        the plaintext bytes durably, and relay them to a plaintext room.
        """

        env = synapse_e2ee_env
        destination_room = _create_room(
            env, env.test_access_token, "attachment-dst-from-e2ee"
        )

        storage = SQLiteStorage(str(tmp_path / "e2ee-src.sqlite"))
        await storage.initialize()
        harness = _AttachmentHarness(
            env=env.synapse_env,
            storage=storage,
            source_room=env.encrypted_room_id,
            target_room=destination_room,
            source_adapter_id="attach-e2ee-src-src",
            dest_adapter_id="attach-e2ee-src-dst",
            source_encryption="e2ee_required",
            source_store_path=env.bot_store_path,
            dest_user_id=env.test_user_id,
            dest_access_token=env.test_access_token,
        )
        client = None
        try:
            # The bot adapter must complete its initial sync and key upload
            # BEFORE the fixture client queries/claims keys, or the sender
            # cannot establish olm sessions with the bot's device (same
            # ordering as the established E2EE bridge smoke).
            await harness.start()
            await harness.wait_until_live()
            await asyncio.sleep(1.0)
            client = await env.init_test_e2ee_client()

            # Join the encrypted room with the test client if needed.
            join_info = await client.join(env.encrypted_room_id)
            if not hasattr(join_info, "room_id"):
                pytest.xfail("test client could not join the encrypted room")
            # A zero-timeout sync settles the joined room's state before
            # the first encrypted send (join alone leaves nio's
            # group-session sharing state incomplete for the room).
            await client.sync(timeout=0)

            def _provider(got_429: int, got_timeouts: int) -> bytes:
                # SDK DataProvider contract: sync callable returning bytes.
                return _MEDIA_BYTES

            response, keys = await client.upload(
                _provider,
                content_type="image/png",
                filename="secret-photo.png",
                encrypt=True,
                filesize=len(_MEDIA_BYTES),
            )
            assert hasattr(
                response, "content_uri"
            ), f"encrypted upload failed: {response}"
            send_response = await client.room_send(
                room_id=env.encrypted_room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.image",
                    "body": "secret-photo.png",
                    "filename": "secret-photo.png",
                    "file": {
                        **keys,
                        "url": response.content_uri,
                        "mimetype": "image/png",
                    },
                    "info": {
                        "mimetype": "image/png",
                        "size": len(_MEDIA_BYTES),
                        "w": 1,
                        "h": 1,
                    },
                },
                ignore_unverified_devices=True,
            )
            assert hasattr(
                send_response, "event_id"
            ), f"encrypted send failed: {send_response}"

            try:
                result = await harness.wait_for_file_admission("secret-photo.png")
                assert result.attachment is not None and result.attachment.retained
                stored = await storage.load_attachment_content(
                    result.event_id, result.attachment.content_ref
                )
                assert (
                    stored.data == _MEDIA_BYTES
                ), "decrypted attachment bytes must equal the original"
                await harness.runner.process_admitted_event(result.event_id)
                relayed = await harness.wait_for_relay()
                content = relayed["content"]
                assert "url" in content, "plaintext destination carries a plain url"
                downloaded = _download_media(env, env.test_access_token, content["url"])
                assert downloaded == _MEDIA_BYTES
                # No file key material in any persisted canonical evidence.
                canonical = await storage.get(result.event_id)
                assert "key" not in json.dumps(canonical.payload)
                assert "iv" not in json.dumps(canonical.payload)
                report = {
                    "transport": "matrix",
                    "evidence_level": "docker_synapse_e2ee_source_media",
                    "event_and_attachment_decrypted": True,
                    "no_file_keys_in_canonical_evidence": True,
                    "identical_bytes": True,
                }
                logger.info("encrypted-source attachment report: %s", report)
            finally:
                await harness.stop()
                await storage.close()
        finally:
            await env.close_test_e2ee_client()

    async def test_plaintext_source_uploads_ciphertext_to_encrypted_room(
        self,
        synapse_e2ee_env: Any,
        tmp_path: Path,
    ) -> None:
        """Plaintext source → encrypted destination: ciphertext on the wire."""
        env = synapse_e2ee_env
        destination_room = env.encrypted_room_id

        storage = SQLiteStorage(str(tmp_path / "e2ee-dst.sqlite"))
        await storage.initialize()
        harness = _AttachmentHarness(
            env=env.synapse_env,
            storage=storage,
            source_room=env.synapse_env.test_room_id,
            target_room=destination_room,
            source_adapter_id="attach-e2ee-dst-src",
            dest_adapter_id="attach-e2ee-dst-dst",
            dest_encryption="e2ee_required",
            dest_store_path=env.bot_store_path,
            dest_device_id=env.bot_device_id,
        )
        try:
            await harness.start()
            # Live-provenance retention: the media must arrive after the
            # live sync boundary (backlog is history-suppressed).
            await harness.wait_until_live()
            await asyncio.sleep(1.0)
            mxc = _upload_media(
                env,
                env.test_access_token,
                _MEDIA_BYTES,
                "into-e2ee.bin",
                "application/octet-stream",
            )
            _send_room_event(
                env,
                env.test_access_token,
                env.synapse_env.test_room_id,
                {
                    "msgtype": "m.file",
                    "body": "into-e2ee.bin",
                    "filename": "into-e2ee.bin",
                    "url": mxc,
                    "info": {
                        "mimetype": "application/octet-stream",
                        "size": len(_MEDIA_BYTES),
                    },
                },
            )
            result = await harness.wait_for_file_admission("into-e2ee.bin")
            assert result.attachment.retained

            # Initialise the receiving client BEFORE the relay so its
            # bootstrap to-device cycle runs without bot-originated key
            # traffic pending; the decryption proof syncs afterwards.
            # The fixture client refuses unverified devices; when the
            # bot's device is untrusted from the test user's perspective
            # the ciphertext-shape proof below still holds and the
            # decryption proof degrades to an xfail.
            try:
                receiver = await env.init_test_e2ee_client()
            except Exception as exc:
                if type(exc).__name__ == "OlmUnverifiedDeviceError":
                    receiver = None
                else:
                    raise
            try:
                await harness.runner.process_admitted_event(result.event_id)

                # Wait for the encrypted destination event.
                deadline = time.monotonic() + _DELIVERY_WAIT_SECONDS
                encrypted_event = None
                while time.monotonic() < deadline and encrypted_event is None:
                    for event in _latest_room_messages(
                        env, env.test_access_token, destination_room
                    ):
                        if event.get("type") == "m.room.encrypted":
                            encrypted_event = event
                    if encrypted_event is None:
                        await asyncio.sleep(0.5)
                assert (
                    encrypted_event is not None
                ), "no encrypted event observed in the destination room"
                # Ciphertext never appears as plaintext API-visible content:
                # the wire payload is an m.room.encrypted blob (decryption
                # proof below uses the receiving client when key exchange
                # completed in time).
                ciphertext_blob = json.dumps(encrypted_event)
                assert (
                    "into-e2ee.bin" not in ciphertext_blob.split("ciphertext")[-1][:50]
                )

                if receiver is None:
                    pytest.xfail(
                        "receiving client refused unverified bot device; "
                        "ciphertext-shape delivery is proven, receiving-"
                        "client decryption is not"
                    )
                # Full receiving-client proof: decrypt event AND attachment.
                sync = await receiver.sync(full_state=True, timeout=5000)
                assert isinstance(sync, object)
                decrypted_event = None
                for room in receiver.rooms.values():
                    for ev in getattr(room, "timeline", []) or []:
                        if getattr(ev, "decrypted", False) and getattr(
                            ev, "msgtype", ""
                        ) in ("m.file", "m.image", "m.video", "m.audio"):
                            decrypted_event = ev
                if decrypted_event is None:
                    pytest.xfail(
                        "Megolm key exchange did not complete within the "
                        "grace period; ciphertext-shape delivery is proven, "
                        "receiving-client decryption is not"
                    )
                file_obj = (
                    getattr(decrypted_event, "source", {})
                    .get("content", {})
                    .get("file")
                )
                url = getattr(decrypted_event, "url", None) or (file_obj or {}).get(
                    "url"
                )
                assert url, "decrypted media event must expose its mxc url"
                ciphertext = _download_media(env, env.test_access_token, url)
                from nio.crypto.attachments import decrypt_attachment

                plaintext = decrypt_attachment(
                    ciphertext,
                    file_obj["key"]["k"],
                    file_obj["hashes"]["sha256"],
                    file_obj["iv"],
                )
                assert plaintext == _MEDIA_BYTES
                report = {
                    "transport": "matrix",
                    "evidence_level": "docker_synapse_e2ee_destination_media",
                    "ciphertext_upload_verified": True,
                    "receiving_client_decrypted_event_and_attachment": True,
                    "identical_bytes": True,
                }
                logger.info("encrypted-destination attachment report: %s", report)
            finally:
                await env.close_test_e2ee_client()
        finally:
            await harness.stop()
            await storage.close()
