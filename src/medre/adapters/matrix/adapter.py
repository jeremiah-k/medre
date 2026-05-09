"""Matrix presentation adapter for the MEDRE framework.

:class:`MatrixAdapter` connects to a Matrix homeserver via the
``mindroom-nio`` async client library and bridges inbound Matrix
messages into the MEDRE canonical event stream and outbound rendered
payloads back to Matrix rooms.

**Soft dependency**: all ``nio`` imports are guarded behind
:mod:`~medre.adapters.matrix.compat`.  If ``mindroom-nio`` is not
installed the adapter raises :class:`~medre.adapters.matrix.errors.MatrixConnectionError`
on :meth:`start`.
"""
from __future__ import annotations

import asyncio
from typing import Any

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixConnectionError, MatrixSendError
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import MatrixRelationHandler
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the Matrix presentation adapter.
_MATRIX_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=True,
    store_and_forward=False,
    direct_messages=True,
)


class MatrixAdapter(BaseAdapter):
    """Presentation adapter for Matrix chat rooms.

    Connects to a Matrix homeserver using ``mindroom-nio``, receives
    room messages, and publishes them as canonical events.  Outbound
    rendered payloads are sent via ``room_send``.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.matrix.config.MatrixConfig`.
    """

    __slots__ = (
        "_config", "_capabilities", "_client", "_sync_task",
        "_sync_failure", "_codec", "_relation_handler",
        "_envelope_handler", "ctx",
    )

    adapter_id: str
    platform: str = "matrix"
    role: AdapterRole = AdapterRole.PRESENTATION

    def __init__(self, config: MatrixConfig) -> None:
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _MATRIX_CAPABILITIES
        self._client: Any = None
        self._sync_task: asyncio.Task | None = None
        self._sync_failure: Exception | None = None
        self._codec = MatrixCodec(config.adapter_id, config)
        self._relation_handler = MatrixRelationHandler()
        self._envelope_handler = MatrixMetadataEnvelope
        self.ctx: AdapterContext | None = None

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the Matrix homeserver and begin syncing.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MatrixConnectionError
            If ``mindroom-nio`` is not installed or the client fails
            to connect.
        """
        self._sync_failure = None  # Reset from any previous failure
        self.ctx = ctx

        if not HAS_NIO:
            raise MatrixConnectionError(
                "mindroom-nio not installed; pip install mindroom-nio"
            )

        import nio

        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=self._config.device_id or "",
        )
        self._client.restore_login(
            user_id=self._config.user_id,
            device_id=self._config.device_id or "",
            access_token=self._config.access_token,
        )

        if not getattr(self._client, "logged_in", False):
            await self._client.close()
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        self._client.add_event_callback(
            self._on_room_message,
            (nio.RoomMessageText, nio.RoomMessageNotice, nio.RoomMessageEmote),
        )

        try:
            self._sync_task = asyncio.create_task(self._run_sync())
        except Exception as exc:
            await self._client.close()
            self._client = None
            raise MatrixConnectionError(
                f"failed to start sync for {self._config.user_id}: {exc}"
            ) from exc

        ctx.logger.info("MatrixAdapter %s started", self.adapter_id)

    async def _run_sync(self) -> None:
        """Wrap ``sync_forever`` and record any failure.

        If ``sync_forever`` raises (e.g. network error, auth expiry), the
        exception is captured in ``_sync_failure`` and logged so that
        :meth:`health_check` can report the degraded state and :meth:`stop`
        can clean up without unobserved-task-exception warnings.
        """
        try:
            await self._client.sync_forever(timeout=self._config.sync_timeout_ms)
        except asyncio.CancelledError:
            # Normal cancellation during stop().  Suppress so the task
            # completes cleanly without an unhandled CancelledError.
            return
        except Exception as exc:
            self._sync_failure = exc
            if self.ctx is not None:
                self.ctx.logger.error(
                    "MatrixAdapter %s: sync task failed: %s",
                    self.adapter_id, exc,
                )

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing and disconnect from the homeserver.

        Idempotent: safe to call multiple times or before start().
        """
        if self._sync_task is not None:
            if not self._sync_task.done():
                self._sync_task.cancel()
                try:
                    await asyncio.wait_for(self._sync_task, timeout=timeout)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    if self.ctx is not None:
                        self.ctx.logger.warning(
                            "MatrixAdapter %s: sync task did not cancel within %ss",
                            self.adapter_id, timeout,
                        )
            # Always retrieve any pending exception to fully drain the
            # task.  Without this, a CancelledError left in the underlying
            # _run_sync coroutine can trigger "RuntimeWarning: coroutine
            # was never awaited" during GC — an artifact of the concrete
            # Task/coroutine interaction on CPython, not a real bug.
            try:
                self._sync_task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            self._sync_task = None

        if self._client is not None:
            try:
                self._client.stop_sync_forever()
            except Exception:
                pass
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

        if self.ctx is not None:
            self.ctx.logger.info("MatrixAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.
        """
        if self._sync_failure is not None:
            health = "failed"
        elif self._client is None:
            health = "unknown"
        elif getattr(self._client, "logged_in", False):
            health = "healthy"
        else:
            health = "failed"

        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities,
            health=health,
        )

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Send a pre-rendered payload to a Matrix room.

        The *result.payload* is expected to be an ``m.room.message``
        content dict already rendered by :class:`~medre.adapters.matrix.renderer.MatrixRenderer`.

        On success, returns an :class:`AdapterDeliveryResult` populated
        with the ``event_id`` from the homeserver's ``RoomSendResponse``
        and the ``room_id`` as the native channel ID.  If the response
        lacks an ``event_id``, the result is returned without one (the
        pipeline will not store a native ref in that case).

        Parameters
        ----------
        result:
            The rendered payload to deliver.

        Returns
        -------
        AdapterDeliveryResult | None
            Native delivery metadata from the Matrix homeserver.

        Raises
        ------
        MatrixSendError
            If the homeserver rejects the message or the client is not
            connected.
        """
        if self._client is None:
            raise MatrixSendError("client is not connected")

        payload_room_id = result.payload.get("room_id")
        room_id = result.target_channel or (
            payload_room_id if isinstance(payload_room_id, str) else ""
        )
        if not room_id:
            raise MatrixSendError("no room_id in result")

        # Create a clean copy and strip routing metadata so room_id
        # does not leak into the Matrix event content.
        content = dict(result.payload)
        content.pop("room_id", None)

        response = await self._client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        # Check for nio error responses
        if hasattr(response, "event_id"):
            event_id = response.event_id
            return AdapterDeliveryResult(
                native_message_id=event_id,
                native_channel_id=room_id,
            )
        else:
            # Error response
            raise MatrixSendError(str(response))

    # -- Inbound callback ---------------------------------------------------

    async def _on_room_message(self, room: Any, event: Any) -> None:
        """nio callback for inbound room messages.

        Decodes the native event into a canonical event and publishes
        it into the framework's inbound stream.  Self-messages (where
        the sender matches ``config.user_id``) are suppressed to prevent
        echo loops.  Events carrying a MEDRE metadata envelope whose
        ``source_adapter`` equals this adapter's ID are also suppressed
        as loop-origin hints.

        Parameters
        ----------
        room:
            The nio ``Room`` object.
        event:
            The nio ``RoomMessage*`` event object.
        """
        if self.ctx is None:
            return

        # Apply room allowlist filter
        if self._config.room_allowlist is not None:
            if room.room_id not in self._config.room_allowlist:
                return

        # Self-message suppression: skip events sent by our own user.
        sender = getattr(event, "sender", "")
        if sender == self._config.user_id:
            self.ctx.logger.debug(
                "MatrixAdapter %s: suppressing self-message from %s",
                self.adapter_id,
                sender,
            )
            return

        try:
            canonical = self._codec.decode(event, room_id=room.room_id)

            # MEDRE-origin loop hint suppression: if the event carries a
            # MEDRE envelope whose source_adapter matches this adapter,
            # skip publishing to prevent echo loops.  Missing or corrupt
            # envelopes are tolerated (accepted normally).
            content = getattr(event, "source", {}).get("content", {})
            envelope = self._envelope_handler.from_content(content)
            if envelope is not None and envelope.source_adapter == self.adapter_id:
                self.ctx.logger.debug(
                    "MatrixAdapter %s: suppressing MEDRE-origin event "
                    "from same adapter",
                    self.adapter_id,
                )
                return

            await self.ctx.publish_inbound(canonical)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MatrixAdapter %s: error processing inbound event",
                    self.adapter_id,
                )

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MatrixCodec:
        """Return the adapter's codec.

        Returns
        -------
        MatrixCodec
            The codec instance.
        """
        return self._codec
