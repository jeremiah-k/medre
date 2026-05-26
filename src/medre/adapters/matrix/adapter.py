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

_PERMANENT_ERRCODES = frozenset(
    {
        "M_FORBIDDEN",
        "M_NOT_FOUND",
        "M_UNKNOWN",
        "M_UNAUTHORIZED",
        "M_UNKNOWN_TOKEN",
        "M_USER_DEACTIVATED",
        "M_BAD_JSON",
        "M_NOT_JSON",
        "M_INVALID_PARAM",
    }
)


class _NioRateLimitError(Exception):
    """Internal sentinel for nio rate-limit responses.

    Raised inside the retry loop when ``room_send`` returns a response
    with ``M_LIMIT_EXCEEDED`` or HTTP 429.  Caught by an explicit
    handler that converts it to :class:`AdapterSendError(transient=True)`
    without sleeping, embedding ``retry_after_ms`` in the error message
    for diagnostic observability.  Not exposed outside this module.

    Attributes
    ----------
    retry_after_ms:
        The ``retry_after_ms`` value from the nio error response, or
        ``None`` if the homeserver did not include one.
    """

    retry_after_ms: int | None

    def __init__(self, message: str, *, retry_after_ms: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


def _is_transient_error(exc: BaseException) -> bool:
    """Classify an exception as transient (retry-able) or permanent.

    Network-level errors from nio / aiohttp are considered transient.
    ``asyncio.TimeoutError``, ``TimeoutError``, ``OSError``,
    ``ConnectionError``, and ``aiohttp.ClientError`` subclasses are
    all transient.

    ``_NioRateLimitError`` is handled by an explicit ``except`` clause
    in the retry loop and never reaches this function; the check is
    retained as a safety net.

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

    # All aiohttp errors on this code path are transport-related; broad matching is intentional.
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
    if errcode in _PERMANENT_ERRCODES:
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
        :func:`~medre.core.supervision.health.normalize_adapter_health`.
        """
        # Check for sync failure — from adapter-level captured failure,
        # from live session, or from _sync_failure attribute.
        # Propagate session failure to adapter attribute for test access.
        if self._session is not None and self._session.last_sync_error is not None:
            self._sync_failure = self._session.last_sync_error
        sync_failure = self._sync_failure

        if sync_failure is not None:
            health = "failed"
        elif self._session is None or not self._session.connected:
            health = "unknown"
        elif self._session.is_logged_in():
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

    def _check_encrypted_room_safety(self, room_id: str) -> None:
        """Raise if the room is encrypted but crypto is not active.

        Delegates room encryption detection to the session's
        :meth:`~MatrixSession.is_room_encrypted` method, which checks
        the session's room-state cache first and falls back to the
        underlying client's room data for rooms not yet tracked.

        Parameters
        ----------
        room_id:
            The target room ID.

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

        if self._session.is_room_encrypted(room_id):
            raise MatrixSendError(
                "Matrix room is encrypted but E2EE crypto is not active; "
                "cannot send encrypted message",
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
            A deterministic transaction ID (tx_id) is computed once per
            delivery and reused across retries, allowing the homeserver
            to deduplicate within its transaction-ID window.  This
            reduces but does not eliminate duplicate events — duplicates
            are still possible across restarts, replay, changed delivery
            identity, or outside the dedup window.

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
        if self._session is None:
            raise AdapterPermanentError("session is not initialized")

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
            already_joined = self._session.is_room_member(room_id)
            if not already_joined:
                joined = await self._session.ensure_joined(room_id)
                if not joined:
                    raise AdapterPermanentError(
                        f"Failed to auto-join configured room {room_id}"
                    )

        try:
            self._check_encrypted_room_safety(room_id)
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
        raw_message_type = content.pop("_matrix_event_type", None)
        if isinstance(raw_message_type, str):
            stripped = raw_message_type.strip()
            message_type = stripped if stripped else "m.room.message"
        else:
            message_type = "m.room.message"

        # Compute a deterministic transaction ID once before the retry
        # loop so all retry attempts reuse the same txn_id.  This allows
        # the Matrix homeserver to deduplicate retries.
        txn_id = _matrix_txn_id(result, room_id)

        # Track 5 — bounded retry for transient errors
        last_exc: BaseException | None = None
        for attempt in range(_MAX_DELIVERY_RETRIES):
            try:
                response = await self._session.room_send(
                    room_id=room_id,
                    message_type=message_type,
                    content=content,
                    ignore_unverified_devices=self._should_ignore_unverified_devices(),
                    tx_id=txn_id,
                )

                # Check for nio error responses (no event_id)
                if not hasattr(response, "event_id"):
                    # Rate-limit response → transient, surface immediately
                    if _is_nio_rate_limited_response(response):
                        retry_ms = getattr(response, "retry_after_ms", None)
                        raise _NioRateLimitError(str(response), retry_after_ms=retry_ms)

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
            except _NioRateLimitError as exc:
                # Rate-limit (M_LIMIT_EXCEEDED / HTTP 429) — do NOT sleep.
                # Raise transient error immediately so the pipeline's retry
                # worker can honour retry_after_ms and schedule backoff.
                self._transient_delivery_failures += 1
                retry_msg = str(exc)
                if exc.retry_after_ms is not None:
                    retry_msg = f"{retry_msg} (retry_after_ms={exc.retry_after_ms})"
                raise AdapterSendError(
                    f"Matrix rate-limited: {retry_msg}", transient=True
                ) from exc
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

        # Safety net: if loop exhausts without raising, classify as permanent. Currently unreachable.
        raise AdapterPermanentError(f"Delivery failed: {last_exc}") from last_exc

    # -- Inbound callback ---------------------------------------------------

    async def _on_room_message(self, event: dict[str, Any]) -> None:
        """Callback for inbound room events (normalized plain dict).

        Receives a normalized plain dict from the session boundary
        (per §31 §7.1) — never raw nio objects.  Decodes the event
        into a canonical event and publishes it into the framework's
        inbound stream.  Self-messages (where the sender matches
        ``config.user_id``) are suppressed to prevent echo loops.
        Events carrying a MEDRE metadata envelope whose
        ``source_adapter`` equals this adapter's ID are also suppressed
        as loop-origin hints.

        Parameters
        ----------
        event:
            Normalized plain dict with keys: ``room_id``, ``sender``,
            ``body``, ``event_id``, ``source``, ``msgtype``,
            ``server_timestamp``, ``sender_display_name``.
        """
        if self.ctx is None:
            return

        room_id = str(event.get("room_id", "") or "")
        sender = str(event.get("sender", "") or "")
        raw_display_name = event.get("sender_display_name")
        sender_display_name: str = (
            raw_display_name
            if isinstance(raw_display_name, str) and raw_display_name.strip()
            else sender
        )

        # Apply room allowlist filter
        if self._config.room_allowlist is not None:
            if room_id not in self._config.room_allowlist:
                self._inbound_filtered_allowlist += 1
                return

        # Startup history suppression: before the first successful sync,
        # inbound timeline events are considered backlog / history and are
        # dropped.  This check must happen before self-message suppression
        # so that pre-live self-messages are counted as startup-suppressed,
        # not self-suppressed.
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
            canonical = self._codec.decode(event, room_id=room_id)

            # MEDRE-origin loop hint suppression: if the event carries a
            # MEDRE envelope whose source_adapter matches this adapter,
            # skip publishing to prevent echo loops.  Missing or corrupt
            # envelopes are tolerated (accepted normally).
            content = (event.get("source") or {}).get("content", {})
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
                    display_name = sender_display_name or sender

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
                # E2EE key management diagnostics
                "olm_loaded": diag.olm_loaded,
                "store_loaded": diag.store_loaded,
                "device_keys_uploaded": diag.device_keys_uploaded,
                "key_query_needed": diag.key_query_needed,
                "device_id_in_use": diag.device_id_in_use,
                "store_path_exists": diag.store_path_exists,
                "initial_sync_completed": diag.initial_sync_completed,
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
            # E2EE key management diagnostics
            "olm_loaded": False,
            "store_loaded": False,
            "device_keys_uploaded": False,
            "key_query_needed": False,
            "device_id_in_use": None,
            "store_path_exists": False,
            "initial_sync_completed": False,
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
