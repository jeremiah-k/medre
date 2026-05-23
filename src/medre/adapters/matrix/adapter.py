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
import hashlib
import logging
import random
from types import MappingProxyType
from typing import Any

import msgspec

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.errors import MatrixConnectionError, MatrixSendError
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import MatrixRelationHandler
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.events.metadata import NativeMetadata
from medre.core.rendering.renderer import RenderingResult

_logger = logging.getLogger(__name__)

# Capabilities for the Matrix presentation adapter.
_MATRIX_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    reactions="native",
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

# Track 5 — delivery retry constants
_MAX_DELIVERY_RETRIES: int = 3
_DELIVERY_BACKOFF_BASE: float = 0.5  # 500ms
_DELIVERY_BACKOFF_JITTER: float = 0.25


class _NioRateLimitError(Exception):
    """Internal sentinel for nio rate-limit responses.

    Raised inside the retry loop when ``room_send`` returns a response
    with ``M_LIMIT_EXCEEDED`` or HTTP 429.  Caught by the transient
    handler and retried with backoff.  Not exposed outside this module.
    """


def _is_transient_error(exc: BaseException) -> bool:
    """Classify an exception as transient (retry-able) or permanent.

    Network-level errors from nio / aiohttp are considered transient.
    Internal rate-limit sentinals are transient.
    ``asyncio.TimeoutError``, ``TimeoutError``, ``OSError``,
    ``ConnectionError``, and ``aiohttp.ClientError`` subclasses are
    all transient.

    ``MatrixSendError`` and other application-level errors are **not**
    transient and fall through to the permanent path.
    """
    # Internal rate-limit sentinel
    if isinstance(exc, _NioRateLimitError):
        return True

    # asyncio.TimeoutError / TimeoutError
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True

    # OSError and its subclasses (ConnectionError, etc.)
    if isinstance(exc, OSError):
        return True

    # Common nio/aiohttp transient error patterns
    exc_name = type(exc).__name__
    exc_module = type(exc).__module__ or ""

    # nio network errors
    if exc_name in (
        "TransportProtocolError",
        "LocalProtocolError",
        "ClientConnectorError",
        "ServerDisconnectedError",
        "ClientOSError",
        "ServerTimeoutError",
    ):
        return True

    # aiohttp transport errors
    if "aiohttp" in exc_module and "Error" in exc_name:
        return True

    return False


def _is_nio_rate_limited_response(response: Any) -> bool:
    """Return True if a nio response indicates a rate-limit error.

    Checks for ``M_LIMIT_EXCEEDED`` errcode or HTTP 429 status on
    response objects that lack an ``event_id`` (i.e. nio ErrorResponse
    or similar).
    """
    # Already a success response
    if hasattr(response, "event_id"):
        return False
    errcode = getattr(response, "errcode", None) or ""
    if isinstance(errcode, str) and "M_LIMIT_EXCEEDED" in errcode.upper():
        return True
    status = getattr(response, "status_code", None)
    if status == 429:
        return True
    return False


def _is_nio_permanent_response(response: Any) -> bool:
    """Return True if a nio response indicates a permanent error.

    Checks for ``M_FORBIDDEN``, ``M_NOT_FOUND``, or invalid-room
    errcodes on response objects that lack an ``event_id``.
    """
    if hasattr(response, "event_id"):
        return False
    errcode = str(getattr(response, "errcode", "") or "").upper()
    if errcode in ("M_FORBIDDEN", "M_NOT_FOUND", "M_UNKNOWN"):
        return True
    msg = str(response).upper()
    if "NOT_FOUND" in msg or "FORBIDDEN" in msg:
        return True
    return False


def _matrix_txn_id(result: RenderingResult, room_id: str) -> str:
    """Compute a deterministic Matrix transaction ID for idempotent sends.

    Deterministic inputs: ``result.event_id``, ``result.target_adapter``,
    ``result.target_channel``, ``room_id``.  Produces a ``medre_``-prefixed
    38-character identifier (6-character prefix + 32 hex chars / first 32 of sha256).

    The transaction ID does **not** include the message body, ensuring that
    content changes do not affect the idempotency key.

    .. note::
       nio's ``AsyncClient.room_send()`` accepts the transaction ID as
       ``tx_id``, not ``txn_id``.  The local variable is still named
       ``txn_id`` for readability but is passed as ``tx_id=txn_id``.
    """
    parts = [
        result.event_id,
        result.target_adapter,
        result.target_channel or "",
        room_id,
    ]
    digest = hashlib.sha256(
        "".join(f"{len(p)}:{p}|" for p in parts).encode("utf-8")
    ).hexdigest()
    return f"medre_{digest[:32]}"


def _matrix_display_name(room: Any, sender: str) -> str:
    """Resolve the display name for *sender* from a nio Room object.

    Preference order:
    1. ``room.user_name(sender)`` when callable and returns non-empty.
    2. ``room.users[sender]`` dict fields: ``display_name``, ``displayname``, ``name``.
    3. ``room.users[sender]`` object attributes: ``.display_name``, ``.displayname``, ``.name``.
    4. *sender* MXID as final fallback.

    ``None`` and blank / whitespace-only values are treated as missing.
    """
    # 1. room.user_name(sender)
    user_name_fn = getattr(room, "user_name", None)
    if callable(user_name_fn):
        try:
            val = str(user_name_fn(sender) or "").strip()
            if val:
                return val
        except Exception:
            pass

    # 2 & 3. room.users[sender] — dict fields then object attributes.
    users = getattr(room, "users", None)
    if users is None:
        return sender
    user_info = users.get(sender) if isinstance(users, dict) else None
    if user_info is None:
        return sender

    # Dict path
    if isinstance(user_info, dict):
        for key in ("display_name", "displayname", "name"):
            raw = user_info.get(key)
            if raw is not None:
                val = str(raw).strip()
                if val:
                    return val
    else:
        # Object path
        for attr in ("display_name", "displayname", "name"):
            raw = getattr(user_info, attr, None)
            if raw is not None:
                val = str(raw).strip()
                if val:
                    return val

    return sender


class MatrixAdapter(AdapterContract):
    """Presentation adapter for Matrix chat rooms.

    Connects to a Matrix homeserver using ``mindroom-nio``, receives
    room messages, and publishes them as canonical events.  Outbound
    rendered payloads are sent via ``room_send``.

    Client lifecycle is delegated to :class:`MatrixSession`.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.matrix.MatrixConfig`.
    """

    __slots__ = (
        "_config",
        "_capabilities",
        "_session",
        "_client",
        "_sync_task",
        "_sync_failure_stored",
        "_codec",
        "_relation_handler",
        "_envelope_handler",
        "ctx",
        # Track 5 — delivery retry stats
        "_transient_delivery_failures",
        "_permanent_delivery_failures",
        # Inbound diagnostics counters
        "_inbound_published",
        "_inbound_suppressed_self",
        "_inbound_suppressed_envelope",
        "_inbound_filtered_allowlist",
        "_inbound_suppressed_startup",
    )

    adapter_id: str
    platform: str = "matrix"
    role: AdapterRole = AdapterRole.PRESENTATION

    def __init__(self, config: MatrixConfig) -> None:
        super().__init__()
        self._config = config.validate()
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
        # Track 5
        self._transient_delivery_failures: int = 0
        self._permanent_delivery_failures: int = 0
        # Inbound diagnostics counters
        self._inbound_published: int = 0
        self._inbound_suppressed_self: int = 0
        self._inbound_suppressed_envelope: int = 0
        self._inbound_filtered_allowlist: int = 0
        self._inbound_suppressed_startup: int = 0

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
        # Track 5 — reset delivery stats on start
        self._transient_delivery_failures = 0
        self._permanent_delivery_failures = 0
        # Inbound diagnostics — reset on start
        self._inbound_published = 0
        self._inbound_suppressed_self = 0
        self._inbound_suppressed_envelope = 0
        self._inbound_filtered_allowlist = 0
        self._inbound_suppressed_startup = 0
        self.ctx = ctx
        self._mark_started(ctx)

        if not HAS_NIO:
            raise MatrixConnectionError(
                "mindroom-nio not installed; pip install 'medre[matrix]'"
            )

        # E2EE mode guards are now handled inside MatrixSession.start().
        # The adapter simply creates the session and delegates.

        # Stop previous session if still active (idempotent double-start guard).
        # Without this, calling start() twice orphans the old MatrixSession,
        # leaking its nio AsyncClient and the internal aiohttp.ClientSession.
        if self._session is not None and not self._session.closed:
            await self._session.stop()

        session_logger = ctx.logger.getChild("session")
        self._session = MatrixSession(
            config=self._config,
            message_callback=self._on_room_message,
            logger=session_logger,
            auto_join_rooms=self._config.auto_join_rooms,
        )
        await self._session.start()

        # Mirror session state onto adapter for convenient access.
        self._client = self._session.client
        self._sync_task = self._session._sync_task

        ctx.logger.info("MatrixAdapter %s started", self.adapter_id)

        # Part D — auto-join configured rooms after startup.
        if self._config.auto_join_rooms:
            join_results = await self._session.ensure_joined_rooms(
                self._config.auto_join_rooms
            )
            joined_count = sum(1 for v in join_results.values() if v)
            failed_count = len(join_results) - joined_count
            ctx.logger.info(
                "Auto-join: %d configured, %d joined, %d failed",
                len(self._config.auto_join_rooms),
                joined_count,
                failed_count,
            )

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

    def _should_ignore_unverified_devices(self) -> bool:
        """Determine whether to pass ``ignore_unverified_devices=True`` to nio.

        MEDRE internally sets this to ``True`` when E2EE is active (i.e.
        ``encryption_mode`` is not ``"plaintext"``).  This is required by the
        upstream nio client, which lacks cross-signing support (MSC1756) and
        provides no API for programmatic device verification.  For plaintext
        mode the flag is ``False`` (nio strict default).

        This is **not** an operator-configurable toggle — it is an internal
        nio workaround.
        """
        return self._config.encryption_mode != "plaintext"

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

        Uses the session's room-state tracking cache first (Track 4),
        then falls back to ``client.rooms[room_id].encrypted`` for
        rooms not yet tracked.

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
            The error message is operator-readable and ``transient=False``.
        """
        if self._session is None:
            return
        if self._session.crypto_enabled:
            return

        _encrypted_msg = (
            "Matrix room is encrypted but E2EE crypto is not active; "
            "cannot send encrypted message"
        )

        # Track 4 — use session room-state cache first
        room_state = self._session.room_state(room_id)
        if room_state == "encrypted":
            raise MatrixSendError(
                _encrypted_msg,
                transient=False,
            )
        if room_state == "plaintext":
            # Room is known plaintext — allow send
            return

        # "unknown" — fall back to client.rooms check
        rooms = getattr(client, "rooms", None)
        if rooms is not None and isinstance(rooms, dict):
            room_obj = rooms.get(room_id)
            if room_obj is not None and getattr(room_obj, "encrypted", False):
                raise MatrixSendError(
                    _encrypted_msg,
                    transient=False,
                )

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Send a pre-rendered payload to a Matrix room.

        The *result.payload* is expected to be an ``m.room.message``
        content dict already rendered by :class:`~medre.adapters.matrix.renderer.MatrixRenderer`.

        On success, returns an :class:`AdapterDeliveryResult` populated
        with the ``event_id`` from the homeserver's ``RoomSendResponse``.
        If the response lacks an ``event_id``, the result is returned without one (the
        pipeline will not store a native ref in that case).

        Implements bounded retry (Track 5) for transient network errors:
        up to 3 attempts with exponential backoff (500ms, 1s, 2s, +-25% jitter).
        Non-transient errors raise immediately without retry.

        .. note::
            Retry may cause duplicate messages if the first attempt
            succeeded on the server but the response was lost.

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
        AdapterSendError
            If a transient error occurs (network, timeout) after
            exhausting retries.  ``transient`` is ``True``.
        AdapterPermanentError
            If a permanent error occurs (encrypted-room rejection,
            missing client, invalid room, non-transient session error).
            ``transient`` is ``False``.
        asyncio.CancelledError
            Propagates without swallowing task cancellation.
        """
        client = self._client
        if client is None:
            # Lifecycle/startup state missing — cannot be repaired by retry.
            raise AdapterPermanentError("client is not connected")

        payload_room_id = result.payload.get("room_id")
        room_id = result.target_channel or (
            payload_room_id if isinstance(payload_room_id, str) else ""
        )
        if not room_id:
            raise AdapterPermanentError("no room_id in result")

        # Part D — auto-join configured target room if not already joined.
        if (
            self._config.auto_join_rooms
            and room_id in self._config.auto_join_rooms
            and self._session is not None
        ):
            rooms = getattr(client, "rooms", None)
            already_joined = (
                rooms is not None and isinstance(rooms, dict) and room_id in rooms
            )
            if not already_joined:
                joined = await self._session.ensure_joined(room_id)
                if not joined:
                    raise AdapterPermanentError(
                        f"Failed to auto-join configured room {room_id}"
                    )

        try:
            self._check_encrypted_room_safety(room_id, client)
        except MatrixSendError as exc:
            if exc.transient:
                raise AdapterSendError(str(exc), transient=True) from exc
            else:
                raise AdapterPermanentError(str(exc)) from exc

        # Create a clean copy and strip routing metadata so room_id
        # does not leak into the Matrix event content.
        content = dict(result.payload)
        content.pop("room_id", None)

        # Pop the internal _matrix_event_type key that the renderer uses
        # to signal non-default event types (e.g. m.reaction).  The key
        # must never leak into the homeserver content.
        message_type = content.pop("_matrix_event_type", "m.room.message")

        # Compute a deterministic transaction ID once before the retry
        # loop so all retry attempts reuse the same txn_id.  This allows
        # the Matrix homeserver to deduplicate retries.
        txn_id = _matrix_txn_id(result, room_id)

        # Track 5 — bounded retry for transient errors
        last_exc: BaseException | None = None
        for attempt in range(_MAX_DELIVERY_RETRIES):
            try:
                response = await client.room_send(
                    room_id=room_id,
                    message_type=message_type,
                    content=content,
                    ignore_unverified_devices=self._should_ignore_unverified_devices(),
                    tx_id=txn_id,
                )

                # Check for nio error responses (no event_id)
                if not hasattr(response, "event_id"):
                    # Rate-limit response → transient, retry
                    if _is_nio_rate_limited_response(response):
                        raise _NioRateLimitError(str(response))

                    # Permanent error response (M_FORBIDDEN, M_NOT_FOUND, etc.)
                    if _is_nio_permanent_response(response):
                        err_msg = str(response)
                        if hasattr(response, "errcode") and response.errcode:
                            err_msg = f"{response.errcode}: {err_msg}"
                        raise AdapterPermanentError(err_msg)

                    # Unknown error response — treat as permanent
                    err_msg = str(response)
                    if hasattr(response, "errcode") and response.errcode:
                        err_msg = f"{response.errcode}: {err_msg}"
                    raise AdapterPermanentError(err_msg)

                event_id = response.event_id
                if not event_id:
                    raise AdapterPermanentError(
                        "homeserver returned empty/missing event_id; "
                        "delivery may not have been recorded"
                    )
                return AdapterDeliveryResult(
                    native_message_id=event_id,
                    native_channel_id=room_id,
                    metadata=MappingProxyType({"matrix_txn_id": txn_id}),
                )

            except MatrixSendError as exc:
                # Session-layer error → convert to runtime boundary error.
                if exc.transient:
                    self._transient_delivery_failures += 1
                    raise AdapterSendError(str(exc), transient=True) from exc
                else:
                    self._permanent_delivery_failures += 1
                    raise AdapterPermanentError(str(exc)) from exc
            except AdapterPermanentError:
                # Non-transient — raise immediately
                self._permanent_delivery_failures += 1
                raise
            except asyncio.CancelledError:
                # CancelledError must propagate — never swallow task cancellation.
                raise
            except Exception as exc:
                last_exc = exc
                if _is_transient_error(exc):
                    self._transient_delivery_failures += 1
                    if attempt < _MAX_DELIVERY_RETRIES - 1:
                        delay = _DELIVERY_BACKOFF_BASE * (2**attempt)
                        jitter = delay * _DELIVERY_BACKOFF_JITTER
                        actual_delay = max(0.0, delay + random.uniform(-jitter, jitter))
                        await asyncio.sleep(actual_delay)
                        continue
                    # Exhausted retries — still transient so pipeline may
                    # retry at its own level.  Do NOT increment the
                    # permanent-delivery counter; this is a transient
                    # exhaustion, not a permanent failure.
                    raise AdapterSendError(
                        f"Delivery failed after {_MAX_DELIVERY_RETRIES} "
                        f"transient retries: {exc}",
                        transient=True,
                    ) from exc
                else:
                    # Non-transient unexpected error
                    self._permanent_delivery_failures += 1
                    raise AdapterPermanentError(str(exc)) from exc

        # Should not reach here, but safety net
        raise AdapterPermanentError(f"Delivery failed: {last_exc}") from last_exc

    # -- Inbound callback ---------------------------------------------------

    async def _on_room_message(self, room: Any, event: Any) -> None:
        """nio callback for inbound room events.

        Decodes the native event (messages, reactions, etc.) into a
        canonical event and publishes it into the framework's inbound
        stream.  Self-messages (where the sender matches
        ``config.user_id``) are suppressed to prevent echo loops.
        Events carrying a MEDRE metadata envelope whose
        ``source_adapter`` equals this adapter's ID are also suppressed
        as loop-origin hints.

        Parameters
        ----------
        room:
            The nio ``Room`` object.
        event:
            The nio event object (``RoomMessage*``, ``ReactionEvent``,
            etc.).
        """
        if self.ctx is None:
            return

        # Track 4 — track room as seen
        if self._session is not None:
            room_id_val = getattr(room, "room_id", None)
            if room_id_val is not None:
                self._session._track_room(room_id_val)

        # Apply room allowlist filter
        if self._config.room_allowlist is not None:
            if room.room_id not in self._config.room_allowlist:
                self._inbound_filtered_allowlist += 1
                return

        # Startup history suppression: before the first successful sync,
        # inbound timeline events are considered backlog / history and are
        # dropped.  This check must happen before self-message suppression
        # so that pre-live self-messages are counted as startup-suppressed,
        # not self-suppressed.
        sender = getattr(event, "sender", "")
        if self._session is not None and not self._session.is_live:
            self._inbound_suppressed_startup += 1
            self.ctx.logger.debug(
                "MatrixAdapter %s: suppressing startup backlog event from %s",
                self.adapter_id,
                sender,
            )
            return

        # Self-message suppression: skip events sent by our own user.
        if sender == self._config.user_id:
            self._inbound_suppressed_self += 1
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
                self._inbound_suppressed_envelope += 1
                self.ctx.logger.debug(
                    "MatrixAdapter %s: suppressing MEDRE-origin event "
                    "from same adapter",
                    self.adapter_id,
                )
                return

            # -- Enrich native metadata with Matrix display name -----------
            # When the event has no existing MMRelay longname/shortname
            # (populated by the codec from meshtastic_longname /
            # meshtastic_shortname content keys), fill them from the
            # Matrix room member display name so that downstream
            # renderers (e.g. Meshtastic radio_relay_prefix {longname})
            # show a human-readable name instead of a bare MXID.
            # CanonicalEvent and its metadata are frozen (msgspec.Struct
            # frozen=True), so we build replacement structs via
            # msgspec.structs.replace instead of mutating in place.
            if canonical.metadata and canonical.metadata.native:
                ndata = canonical.metadata.native.data
                existing_longname = ndata.get("longname") or ndata.get(
                    "meshtastic_longname"
                )
                existing_shortname = ndata.get("shortname") or ndata.get(
                    "meshtastic_shortname"
                )
                if not existing_longname and not existing_shortname:
                    display_name = _matrix_display_name(room, sender)

                    # shortname: first 5 chars of display name, or
                    # localpart of MXID if display_name is just the MXID.
                    if display_name != sender:
                        shortname = display_name[:5]
                    else:
                        localpart = sender.lstrip("@").split(":")[0]
                        shortname = localpart[:5]

                    enriched = dict(ndata)
                    enriched["displayname"] = display_name
                    enriched["longname"] = display_name
                    enriched["shortname"] = shortname

                    new_native = NativeMetadata(data=enriched)
                    new_metadata = msgspec.structs.replace(
                        canonical.metadata, native=new_native
                    )
                    canonical = msgspec.structs.replace(
                        canonical, metadata=new_metadata
                    )

            await self.publish_inbound(canonical)
            self._inbound_published += 1
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
        No access tokens, room keys, session IDs, user secrets,
        or room-name dumps.
        """
        if self._session is not None:
            diag = self._session.diagnostics()
            return {
                "connected": diag.connected,
                "logged_in": diag.logged_in,
                "sync_task_running": diag.sync_task_running,
                "last_sync_error": (
                    str(diag.last_sync_error) if diag.last_sync_error else None
                ),
                "store_path_configured": diag.store_path_configured,
                "device_id_configured": diag.device_id_configured,
                "encryption_mode": diag.encryption_mode,
                "crypto_enabled": diag.crypto_enabled,
                "last_crypto_error": diag.last_crypto_error,
                "encrypted_room_seen": diag.encrypted_room_seen,
                "undecryptable_event_count": diag.undecryptable_event_count,
                # Track 1 — sync recovery
                "sync_running": diag.sync_running,
                "reconnecting": diag.reconnecting,
                "reconnect_attempts": diag.reconnect_attempts,
                "last_successful_sync": diag.last_successful_sync,
                # Track 2 — crypto-store continuity
                "crypto_store_loaded": diag.crypto_store_loaded,
                # Track 4 — room counts (no room IDs)
                "encrypted_room_count": diag.encrypted_room_count,
                "plaintext_room_count": diag.plaintext_room_count,
                # Track 5 — delivery stats
                "transient_delivery_failures": self._transient_delivery_failures,
                "permanent_delivery_failures": self._permanent_delivery_failures,
                # Inbound diagnostics counters
                "inbound_published": self._inbound_published,
                "inbound_suppressed_self": self._inbound_suppressed_self,
                "inbound_suppressed_envelope": self._inbound_suppressed_envelope,
                "inbound_filtered_allowlist": self._inbound_filtered_allowlist,
                "inbound_suppressed_startup": self._inbound_suppressed_startup,
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
            # Track 1
            "sync_running": False,
            "reconnecting": False,
            "reconnect_attempts": 0,
            "last_successful_sync": None,
            # Track 2
            "crypto_store_loaded": False,
            # Track 4
            "encrypted_room_count": 0,
            "plaintext_room_count": 0,
            # Track 5
            "transient_delivery_failures": self._transient_delivery_failures,
            "permanent_delivery_failures": self._permanent_delivery_failures,
            # Inbound diagnostics counters
            "inbound_published": self._inbound_published,
            "inbound_suppressed_self": self._inbound_suppressed_self,
            "inbound_suppressed_envelope": self._inbound_suppressed_envelope,
            "inbound_filtered_allowlist": self._inbound_filtered_allowlist,
            "inbound_suppressed_startup": self._inbound_suppressed_startup,
        }
