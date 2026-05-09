"""Matrix presentation adapter for the MEDRE framework.

:class:`MatrixAdapter` connects to a Matrix homeserver via the
``mindroom-nio`` async client library and bridges inbound Matrix
messages into the MEDRE canonical event stream and outbound rendered
payloads back to Matrix rooms.

All client lifecycle (creation, login, sync, teardown) is delegated to
:class:`~medre.adapters.matrix.session.MatrixSession`.
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
from medre.adapters.matrix.session import MatrixSession
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
    channels=True,
    async_delivery=True,
    topic_rooms=True,
)


class MatrixAdapter(BaseAdapter):
    """Presentation adapter for Matrix chat rooms.

    Connects to a Matrix homeserver using ``mindroom-nio``, receives
    room messages, and publishes them as canonical events.  Outbound
    rendered payloads are sent via ``room_send``.

    Client lifecycle is delegated to :class:`MatrixSession`.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.matrix.config.MatrixConfig`.
    """

    __slots__ = (
        "_config", "_capabilities", "_session",
        "_client", "_sync_task",
        "_sync_failure_stored",
        "_codec", "_relation_handler",
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
        self._session: MatrixSession | None = None
        self._client: Any = None
        self._sync_task: asyncio.Task | None = None
        self._sync_failure_stored: Exception | None = None
        self._codec = MatrixCodec(config.adapter_id, config)
        self._relation_handler = MatrixRelationHandler()
        self._envelope_handler = MatrixMetadataEnvelope
        self.ctx: AdapterContext | None = None

    @property
    def _sync_failure(self) -> Exception | None:
        """Last sync error — reads from live session when available."""
        if self._session is not None and self._session.last_sync_error is not None:
            return self._session.last_sync_error
        return self._sync_failure_stored

    @_sync_failure.setter
    def _sync_failure(self, value: Exception | None) -> None:
        self._sync_failure_stored = value

    # -- Lifecycle ----------------------------------------------------------

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the Matrix homeserver and begin syncing.

        Delegates client lifecycle to :class:`MatrixSession`.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MatrixConnectionError
            If ``mindroom-nio`` is not installed, the client fails
            to connect, or E2EE preconditions are unmet.
        """
        self._sync_failure = None  # Reset from any previous failure
        self.ctx = ctx

        if not HAS_NIO:
            raise MatrixConnectionError(
                "mindroom-nio not installed; pip install mindroom-nio"
            )

        # E2EE mode guards are now handled inside MatrixSession.start().
        # The adapter simply creates the session and delegates.

        session_logger = ctx.logger.getChild("session")
        self._session = MatrixSession(
            config=self._config,
            message_callback=self._on_room_message,
            logger=session_logger,
        )
        await self._session.start()

        # Mirror session state onto adapter for backward-compatible access.
        self._client = self._session.client
        self._sync_task = self._session._sync_task

        ctx.logger.info("MatrixAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing and disconnect from the homeserver.

        Idempotent: safe to call multiple times or before start().
        """
        if self._session is not None:
            # Capture failure before stopping for health_check.
            self._sync_failure_stored = self._session.last_sync_error
            await self._session.stop(timeout=timeout)
            self._session = None

        self._client = None
        self._sync_task = None

        if self.ctx is not None:
            self.ctx.logger.info("MatrixAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.

        Operational diagnostics
        -----------------------
        Callers that need fine-grained operational state (connected,
        logged_in, sync_task_running, last_sync_error) should extract
        it from the adapter's internal attributes and pass it as the
        ``details`` dict to
        :func:`~medre.core.runtime.health.normalize_adapter_health`.
        See :func:`medre.runner.collect_diagnostics` for the canonical
        extraction pattern.
        """
        # Check for sync failure — from adapter-level captured failure,
        # from live session, or from _sync_failure attribute.
        # Propagate session failure to adapter attribute for test access.
        if self._session is not None and self._session.last_sync_error is not None:
            self._sync_failure = self._session.last_sync_error
        sync_failure = self._sync_failure

        if sync_failure is not None:
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

    def _check_encrypted_room_safety(self, room_id: str, client: Any) -> None:
        """Raise if the room is encrypted but crypto is not active.

        Checks the room-specific encryption state via
        ``client.rooms[room_id].encrypted`` when available.  Rooms not
        found in ``client.rooms`` are treated optimistically (send
        allowed).

        Parameters
        ----------
        room_id:
            The target room ID.
        client:
            The nio client instance.

        Raises
        ------
        MatrixSendError
            If the room is encrypted but ``crypto_enabled`` is ``False``.
        """
        if self._session is None:
            return
        if self._session.crypto_enabled:
            return

        # Room-specific check: inspect client.rooms for encryption state.
        rooms = getattr(client, "rooms", None)
        if rooms is not None and isinstance(rooms, dict):
            room_obj = rooms.get(room_id)
            if room_obj is not None and getattr(room_obj, "encrypted", False):
                raise MatrixSendError(
                    f"Room {room_id} is encrypted but E2EE crypto is not active; "
                    f"cannot send encrypted message"
                )

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Send a pre-rendered payload to a Matrix room.

        The *result.payload* is expected to be an ``m.room.message``
        content dict already rendered by :class:`~medre.adapters.matrix.renderer.MatrixRenderer`.

        On success, returns an :class:`AdapterDeliveryResult` populated
        with the ``event_id`` from the homeserver's ``RoomSendResponse``.
        If the response lacks an ``event_id``, the result is returned without one (the
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
        client = self._client
        if client is None:
            raise MatrixSendError("client is not connected")

        payload_room_id = result.payload.get("room_id")
        room_id = result.target_channel or (
            payload_room_id if isinstance(payload_room_id, str) else ""
        )
        if not room_id:
            raise MatrixSendError("no room_id in result")

        self._check_encrypted_room_safety(room_id, client)

        # Create a clean copy and strip routing metadata so room_id
        # does not leak into the Matrix event content.
        content = dict(result.payload)
        content.pop("room_id", None)

        response = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        # Check for nio error responses — a successful RoomSendResponse
        # carries event_id; anything else is an error.
        if hasattr(response, "event_id"):
            event_id = response.event_id
            # Guard against None / empty event_id — the homeserver should
            # always return one on success.  A missing or empty event_id
            # indicates a malformed response and must not produce a native
            # ref that the pipeline could persist.
            if not event_id:
                raise MatrixSendError(
                    "homeserver returned empty/missing event_id; "
                    "delivery may not have been recorded"
                )
            return AdapterDeliveryResult(
                native_message_id=event_id,
                native_channel_id=room_id,
            )
        else:
            # Error response (nio ErrorResponse or similar)
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

    # -- Diagnostics --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return a dict of adapter diagnostics (no secrets).

        Includes session diagnostics plus adapter-level fields.
        """
        if self._session is not None:
            diag = self._session.diagnostics()
            return {
                "connected": diag.connected,
                "logged_in": diag.logged_in,
                "sync_task_running": diag.sync_task_running,
                "last_sync_error": str(diag.last_sync_error) if diag.last_sync_error else None,
                "store_path_configured": diag.store_path_configured,
                "device_id_configured": diag.device_id_configured,
                "encryption_mode": diag.encryption_mode,
                "crypto_enabled": diag.crypto_enabled,
                "last_crypto_error": diag.last_crypto_error,
                "encrypted_room_seen": diag.encrypted_room_seen,
                "undecryptable_event_count": diag.undecryptable_event_count,
            }
        return {
            "connected": False,
            "logged_in": False,
            "sync_task_running": False,
            "last_sync_error": None,
            "store_path_configured": self._config.store_path is not None,
            "device_id_configured": self._config.device_id is not None,
            "encryption_mode": self._config.encryption_mode,
            "crypto_enabled": False,
            "last_crypto_error": None,
            "encrypted_room_seen": False,
            "undecryptable_event_count": 0,
        }
