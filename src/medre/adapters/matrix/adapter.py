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
import time
from typing import Any, Callable

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.errors import (
    MATRIX_PERMANENT_ERRCODES,
    MatrixConnectionError,
    MatrixSendError,
)
from medre.adapters.matrix.errors import (
    is_nio_rate_limited_response as _is_nio_rate_limited_response,
)
from medre.adapters.matrix.errors import (
    retry_after_seconds_from_ms as _retry_after_seconds_from_ms,
)
from medre.adapters.matrix.event_shape import MATRIX_NATIVE_SCHEMA_VERSION
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.outbound import (
    MatrixOutboundEnvelopeError,
    MatrixOutboundOperation,
)
from medre.adapters.matrix.relations import MatrixRelationHandler
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    MAX_ADAPTER_RETRY_AFTER_SECONDS,
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.ingress import IngressProvenance
from medre.core.rendering.renderer import RenderingResult

_logger = logging.getLogger(__name__)

# Capabilities for the Matrix presentation adapter.
_MATRIX_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=False,
    replies="native",
    threads="native",
    reactions="native",
    edits="native",
    deletes="native",
    attachments=False,
    metadata_fields=False,
    store_and_forward=False,
    direct_messages=True,
    channels=True,
    topic_rooms=True,
)

# Delivery retry constants
_MAX_DELIVERY_RETRIES: int = 3
_DELIVERY_BACKOFF_BASE: float = 0.5  # 500ms
_DELIVERY_BACKOFF_JITTER: float = 0.25


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


def _is_nio_permanent_response(response: Any) -> bool:
    """Return True if a nio response indicates a permanent error.

    Checks for errcodes in :data:`MATRIX_PERMANENT_ERRCODES` (including
    ``M_FORBIDDEN``, ``M_NOT_FOUND``, ``M_DUPLICATE_ANNOTATION``, etc.)
    or ``NOT_FOUND``/``FORBIDDEN`` in the string representation, on
    response objects that lack an ``event_id``.
    """
    if hasattr(response, "event_id"):
        return False
    errcode = str(getattr(response, "errcode", "") or "").upper()
    if errcode in MATRIX_PERMANENT_ERRCODES:
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


def _matrix_redact_txn_id(
    result: RenderingResult, room_id: str, redacts_event_id: str
) -> str:
    """Deterministic Matrix transaction ID for redaction operations.

    Same input scheme as :func:`_matrix_txn_id`, plus the operation kind
    and ``redacts_event_id``.  Folding both in guarantees a redaction can
    never share a transaction id with a send, nor with a redaction of a
    different target, so homeserver deduplication can never collapse a
    redaction into a different operation.
    """
    parts = [
        result.event_id,
        result.target_adapter,
        result.target_channel or "",
        room_id,
        "redact_event",
        redacts_event_id,
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
        "_last_health",
        "_clock",
        "_codec",
        "_relation_handler",
        "_envelope_handler",
        "_started",
        "ctx",
        # Delivery retry / rate-limit stats
        "_transient_delivery_failures",
        "_permanent_delivery_failures",
        "_outbound_cooldown_until",
        "_outbound_rate_limit_events",
        "_outbound_cooldown_deferrals",
        # Inbound diagnostics counters
        "_inbound_published",
        "_inbound_duplicate_admissions",
        "_inbound_suppressed_self",
        "_inbound_suppressed_envelope",
        "_inbound_filtered_allowlist",
        "_inbound_filtered_encryption_policy",
        "_inbound_suppressed_startup",
    )

    adapter_id: str
    platform: str = "matrix"
    role: AdapterRole = AdapterRole.PRESENTATION

    # Matrix transport timestamps (origin_server_ts) are millisecond
    # granularity; the stale guard floors accordingly.
    _event_timestamp_granularity_us = 1_000

    def __init__(self, config: MatrixConfig) -> None:
        super().__init__()
        self._config = config.validate()
        self.adapter_id = config.adapter_id
        self._capabilities = _MATRIX_CAPABILITIES
        self._session: MatrixSession | None = None
        self._sync_failure_stored: Exception | None = None
        self._last_health: str | None = None
        self._clock: Callable[[], float] = time.monotonic
        self._codec = MatrixCodec(config.adapter_id, config)
        self._relation_handler = MatrixRelationHandler()
        self._envelope_handler = MatrixMetadataEnvelope
        self._started: bool = False
        self.ctx: AdapterContext | None = None
        # Delivery retry / server-directed cooldown stats
        self._transient_delivery_failures: int = 0
        self._permanent_delivery_failures: int = 0
        self._outbound_cooldown_until: float = 0.0
        self._outbound_rate_limit_events: int = 0
        self._outbound_cooldown_deferrals: int = 0
        # Inbound diagnostics counters
        self._inbound_published: int = 0
        self._inbound_duplicate_admissions: int = 0
        self._inbound_suppressed_self: int = 0
        self._inbound_suppressed_envelope: int = 0
        self._inbound_filtered_allowlist: int = 0
        self._inbound_filtered_encryption_policy: int = 0
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

        # Clear cached health at lifecycle boundary so diagnostics
        # never reports a stale health string from a previous session.
        self._last_health = None

        # Reset delivery / rate-limit stats on start
        self._transient_delivery_failures = 0
        self._permanent_delivery_failures = 0
        self._outbound_cooldown_until = 0.0
        self._outbound_rate_limit_events = 0
        self._outbound_cooldown_deferrals = 0
        # Inbound diagnostics — reset on start
        self._inbound_published = 0
        self._inbound_duplicate_admissions = 0
        self._inbound_suppressed_self = 0
        self._inbound_suppressed_envelope = 0
        self._inbound_filtered_allowlist = 0
        self._inbound_filtered_encryption_policy = 0
        self._inbound_suppressed_startup = 0
        self.ctx = ctx

        if not HAS_NIO:
            self.ctx = None
            self._start_time = None
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
            admission_callback=(
                self._on_room_message if ctx.admit_inbound is not None else None
            ),
            checkpoint_loader=ctx.load_checkpoint,
            checkpoint_committer=ctx.commit_checkpoint,
            logger=session_logger,
            auto_join_rooms=self._config.auto_join_rooms,
        )
        try:
            await self._session.start()
        except asyncio.CancelledError:
            # Synchronously clear adapter-owned fields before re-raising.
            # Best-effort session cleanup — shielded so it can't be cancelled.
            try:
                await asyncio.shield(self._session.stop())
            except Exception:
                pass
            self._session = None
            self._started = False
            self._start_time = None
            self.ctx = None
            raise
        except Exception as exc:
            self._sync_failure_stored = exc
            try:
                await self._session.stop()
            except Exception:
                pass  # best-effort cleanup
            self._session = None
            self._started = False
            self._start_time = None
            self.ctx = None
            raise

        # Auto-join configured rooms after startup.
        if self._config.auto_join_rooms:
            ctx.logger.debug("Matrix session connected; joining configured rooms")
            try:
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
            except Exception as exc:
                # Auto-join failed — capture actual exception, clean up.
                self._sync_failure_stored = exc
                try:
                    await self._session.stop()
                except Exception:
                    pass  # best-effort cleanup
                self._session = None
                self._started = False
                self._start_time = None
                self.ctx = None
                raise

        self._started = True
        self._mark_started(ctx)
        ctx.logger.info("MatrixAdapter %s started", self.adapter_id)

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing and disconnect from the homeserver.

        Idempotent: safe to call multiple times or before start().
        """
        self._started = False
        self._start_time = None

        if self._session is not None:
            # Capture failure before stopping for health_check.
            self._sync_failure_stored = self._session.last_sync_error
            await self._session.stop(timeout=timeout)
            self._session = None

        # Clear cached health at lifecycle boundary.
        self._last_health = None

        if self.ctx is not None:
            self.ctx.logger.info("MatrixAdapter %s stopped", self.adapter_id)

    def _should_ignore_unverified_devices(self) -> bool:
        """Determine whether to pass ``ignore_unverified_devices=True`` to nio.

        MEDRE internally sets this to ``True`` when E2EE is active (i.e.
        ``encryption_mode`` is not ``"plaintext"``).  This is an intentional
        bot peer-device trust policy, not a cross-signing workaround.
        Cross-signing authenticates MEDRE's own current device to other Matrix
        clients; it does not make MEDRE trust every peer device in a room.

        MEDRE does not yet expose a configurable peer-device verification
        policy, so encrypted sends remain permissive for compatibility.  For
        plaintext mode the flag is ``False``.
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

        # Sync readiness/watchdog: authentication alone is not enough to
        # declare the transport healthy.  Until one sync response succeeds,
        # report degraded; MMRelay likewise withholds runtime readiness until
        # its initial Matrix sync path completes.  After first progress, the
        # configured stale bound and active reconnect state govern degradation.
        # Uses a fakeable clock (``self._clock``) so tests can control time
        # without fixed sleeps.
        if health == "healthy" and self._session is not None:
            last_sync = self._session.last_successful_sync
            if self._session.reconnecting or last_sync is None:
                health = "degraded"
            else:
                stale_timeout = float(self._config.sync_stale_timeout_seconds)
                if stale_timeout > 0:
                    now = self._clock()
                    if (now - last_sync) > stale_timeout:
                        health = "degraded"

        self._last_health = health
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities,
            health=health,
        )

    # -- Outbound delivery --------------------------------------------------

    def _outbound_cooldown_remaining(self) -> float:
        """Return remaining server-directed outbound cooldown in seconds."""
        return max(0.0, self._outbound_cooldown_until - self._clock())

    def _remember_outbound_cooldown(self, retry_after_seconds: float | None) -> None:
        """Extend the shared outbound cooldown from a homeserver rate limit."""
        if retry_after_seconds is None or retry_after_seconds <= 0:
            return
        bounded = min(retry_after_seconds, MAX_ADAPTER_RETRY_AFTER_SECONDS)
        deadline = self._clock() + bounded
        if deadline > self._outbound_cooldown_until:
            self._outbound_cooldown_until = deadline

    def _defer_for_outbound_cooldown(self) -> None:
        """Fail fast with a retry hint while a shared Matrix cooldown is active."""
        remaining = self._outbound_cooldown_remaining()
        if remaining <= 0:
            return
        self._outbound_cooldown_deferrals += 1
        self._transient_delivery_failures += 1
        raise AdapterSendError(
            "Matrix outbound cooldown active after homeserver rate limit",
            transient=True,
            retry_after_seconds=remaining,
        )

    def _check_encrypted_room_safety(self, room_id: str) -> None:
        """Enforce the configured room-encryption send policy for *room_id*.

        Two layers, both delegating room-encryption detection to the
        session's :meth:`~MatrixSession.is_room_encrypted` authority
        (session room-state cache first, then the client's normalized
        room state):

        * ``require_encrypted_rooms=True`` — fail closed.  The send is
          refused unless crypto is active *and* the room is affirmatively
          established as encrypted.  Known plaintext rooms are rejected
          permanently.  Unknown-encryption rooms are refused transiently so
          durable delivery can retry after sync establishes room state; a
          crypto-unavailable session (``e2ee_optional`` fallback) never sends
          at all rather than silently downgrading to plaintext.
        * ``require_encrypted_rooms=False`` — the inverse safeguard
          only: an encrypted room must not be sent to with inactive
          crypto.  Plaintext/unknown rooms send as before.

        Parameters
        ----------
        room_id:
            The target room ID.

        Raises
        ------
        MatrixSendError
            If policy refuses the send.  Known plaintext/crypto-unavailable
            policy failures are permanent; unknown room-encryption state is
            reported with ``transient=True`` so durable work can retry.
        """
        if self._session is None:
            return

        if self._config.require_encrypted_rooms:
            if not self._session.crypto_enabled:
                raise MatrixSendError(
                    "require_encrypted_rooms=True but E2EE crypto is not "
                    "active; refusing to send",
                    transient=False,
                )
            if not self._session.is_room_encrypted(room_id):
                if self._session.encryption_state_known(room_id):
                    raise MatrixSendError(
                        f"Matrix room {room_id} is not established as encrypted; "
                        "require_encrypted_rooms=True refuses plaintext and "
                        "unverified rooms",
                        transient=False,
                    )
                # Startup race (run9): the state is not established YET. The
                # immediate send is still refused (fail closed), but the
                # refusal is transient so durable work is retried after the
                # session establishes room state instead of being lost.
                raise MatrixSendError(
                    f"Matrix room {room_id} encryption state is not yet "
                    "established; require_encrypted_rooms=True defers the "
                    "send until room state is established",
                    transient=True,
                )
            return

        if self._session.crypto_enabled:
            return

        if self._session.is_room_encrypted(room_id):
            raise MatrixSendError(
                "Matrix room is encrypted but E2EE crypto is not active; "
                "cannot send encrypted message",
                transient=False,
            )

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        """Deliver a rendered Matrix operation to a room.

        The *result.payload* must carry a closed ``_matrix_operation``
        envelope produced by :class:`~medre.adapters.matrix.renderer.MatrixRenderer`:

        * ``send_event`` — wire ``content`` is sent with ``event_type``
          through :meth:`MatrixSession.room_send` (existing retry /
          cooldown / rate-limit / txn path, unchanged);
        * ``redact_event`` — ``redacts_event_id`` (and optional neutral
          ``reason``) is sent through :meth:`MatrixSession.room_redact`
          with the same shared guards and a redaction-specific
          deterministic txn.

        On success, returns an :class:`AdapterHandoffResult` populated
        with the ``event_id`` from the homeserver's response (for a
        redaction, the redaction event's own ID, so its native ref
        records to the canonical mutation event).  If the response lacks
        an ``event_id``, the result is returned without one (the
        pipeline will not store a native ref in that case).

        Implements bounded retry for transient network errors:
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
            The rendered operation to deliver.

        Returns
        -------
        AdapterHandoffResult
            Native hand-off metadata from the Matrix homeserver.

        Raises
        ------
        AdapterSendError
            If a transient error occurs (network, timeout) after
            exhausting retries.  ``transient`` is ``True``.
        AdapterPermanentError
            If a permanent error occurs (missing/invalid envelope,
            encrypted-room rejection, missing client, invalid room,
            non-transient session error).  ``transient`` is ``False``.
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

        # Closed outbound-operation envelope (contract §4).  Every native
        # Matrix render wraps its wire content under the single
        # ``_matrix_operation`` key.  Missing or malformed envelopes are
        # permanent failures — deliver() never guesses intent and never
        # leaks envelope fields to the homeserver.
        try:
            operation = MatrixOutboundOperation.from_payload(result.payload)
        except MatrixOutboundEnvelopeError as exc:
            raise AdapterPermanentError(
                f"invalid Matrix outbound operation envelope: {exc}"
            ) from exc
        if operation is None:
            raise AdapterPermanentError(
                "missing _matrix_operation envelope: Matrix deliver() "
                "requires a closed MatrixOutboundOperation payload"
            )

        is_redaction = operation.kind == "redact_event"

        # Auto-join configured target room if not already joined.
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

        if is_redaction:
            # Encrypted-room safety, per actual redaction semantics: a
            # redaction event carries no message content (only the
            # ``redacts`` target and a neutral reason) and the pinned SDK
            # never encrypts the dedicated redaction endpoint, so the
            # content-leak policy enforced for room sends does not apply.
            # Membership, cooldown, retry classification, txn identity,
            # and handoff validation below are all shared with sends.
            redacts_event_id = operation.redacts_event_id or ""
            if not redacts_event_id:
                raise AdapterPermanentError(
                    "redact_event operation missing redacts_event_id"
                )
            txn_id = _matrix_redact_txn_id(result, room_id, redacts_event_id)
        else:
            wire_content = dict(operation.content or {})
            wire_content.pop("room_id", None)
            try:
                self._check_encrypted_room_safety(room_id)
            except MatrixSendError as exc:
                if exc.transient:
                    raise AdapterSendError(str(exc), transient=True) from exc
                raise AdapterPermanentError(str(exc)) from exc
            txn_id = _matrix_txn_id(result, room_id)

        # Fail fast while a shared server-directed cooldown is active: one
        # gate per delivery, outside the in-adapter retry loop, so the
        # structured retry hint propagates to the durable scheduler instead
        # of being misclassified by the generic retry handler below.
        self._defer_for_outbound_cooldown()

        # Bounded retry for transient errors
        last_exc: BaseException | None = None
        for attempt in range(_MAX_DELIVERY_RETRIES):
            try:
                if is_redaction:
                    response = await self._session.room_redact(
                        room_id=room_id,
                        event_id=redacts_event_id,
                        reason=operation.reason,
                        tx_id=txn_id,
                    )
                else:
                    response = await self._session.room_send(
                        room_id=room_id,
                        message_type=operation.event_type or "m.room.message",
                        content=wire_content,
                        ignore_unverified_devices=self._should_ignore_unverified_devices(),
                        tx_id=txn_id,
                    )

                # Check for nio error responses (no event_id).  Shared
                # classification: RoomSendResponse and RoomRedactResponse
                # both carry event_id; RoomSendError/RoomRedactError and
                # other error responses do not.
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
                operation_metadata: dict[str, object] = {
                    "schema_version": MATRIX_NATIVE_SCHEMA_VERSION,
                    "txn_id": txn_id,
                    "operation": operation.kind,
                }
                if is_redaction:
                    operation_metadata["redacts_event_id"] = redacts_event_id
                return AdapterHandoffResult(
                    native_message_id=event_id,
                    native_channel_id=room_id,
                    confirmation_level="remote_service",
                    metadata={
                        "matrix": operation_metadata,
                    },
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
                # Rate-limit (M_LIMIT_EXCEEDED / HTTP 429) — do NOT sleep here.
                # Record the server-directed window for sibling deliveries and
                # surface a structured retry hint so the durable retry lifecycle
                # schedules this delivery no earlier than the homeserver allows.
                self._transient_delivery_failures += 1
                self._outbound_rate_limit_events += 1
                retry_after_seconds = _retry_after_seconds_from_ms(exc.retry_after_ms)
                self._remember_outbound_cooldown(retry_after_seconds)
                retry_msg = str(exc)
                if exc.retry_after_ms is not None:
                    retry_msg = f"{retry_msg} (retry_after_ms={exc.retry_after_ms})"
                raise AdapterSendError(
                    f"Matrix rate-limited: {retry_msg}",
                    transient=True,
                    retry_after_seconds=retry_after_seconds,
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

    async def _on_room_message(
        self,
        event: dict[str, Any],
        provenance: IngressProvenance | None = None,
    ) -> None:
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
            ``server_timestamp``, ``sender_display_name``, Matrix event type,
            transaction ID, room encryption state, and safe decryption provenance.
        """
        if self.ctx is None or not self._started:
            return

        room_id = str(event.get("room_id", "") or "")
        sender = str(event.get("sender", "") or "")
        # Apply room allowlist filter
        if self._config.room_allowlist is not None:
            if room_id not in self._config.room_allowlist:
                self._inbound_filtered_allowlist += 1
                return

        # Encrypted-room-only policy: when require_encrypted_rooms is set,
        # events from rooms not established as encrypted are dropped before
        # decode or durable admission.  Room authority is the normalized
        # event's ``room_encrypted`` flag (nio room state at dispatch time)
        # or the session's encryption-state tracking — never a guess.
        # This is a plain return, never a raise, so the SDK consumes the
        # event and the durable sync checkpoint keeps advancing.
        if self._config.require_encrypted_rooms:
            room_encrypted = event.get("room_encrypted")
            established = room_encrypted is True or (
                self._session is not None and self._session.is_room_encrypted(room_id)
            )
            if not established:
                self._inbound_filtered_encryption_policy += 1
                self.ctx.logger.debug(
                    "MatrixAdapter %s: dropping event from room not "
                    "established as encrypted (require_encrypted_rooms=True)",
                    self.adapter_id,
                )
                return

        # Startup history suppression: before the first successful sync,
        # inbound timeline events are considered backlog / history and are
        # dropped.  This check must happen before self-message suppression
        # so that pre-live self-messages are counted as startup-suppressed,
        # not self-suppressed.
        if (
            provenance is None
            and self._session is not None
            and not self._session.is_live
        ):
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

            if provenance is None:
                await self.publish_inbound(canonical)
                self._inbound_published += 1
            else:
                result = await self.admit_inbound(canonical, provenance)
                if result.created:
                    self._inbound_published += 1
                else:
                    self._inbound_duplicate_admissions += 1
                    self.ctx.logger.debug(
                        "MatrixAdapter %s: duplicate durable admission mapped to %s",
                        self.adapter_id,
                        result.event_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            if provenance is not None:
                raise
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
                "health": self._last_health,
                "mode": "live",
                "logged_in": diag.logged_in,
                "sync_task_running": diag.sync_task_running,
                "last_sync_error": (
                    str(diag.last_sync_error) if diag.last_sync_error else None
                ),
                # alias: matches spec common-key name for cross-adapter tooling
                "last_error": (
                    str(diag.last_sync_error) if diag.last_sync_error else None
                ),
                "store_path_configured": diag.store_path_configured,
                "device_id_configured": diag.device_id_configured,
                "encryption_mode": diag.encryption_mode,
                "crypto_enabled": diag.crypto_enabled,
                "last_crypto_error": diag.last_crypto_error,
                "encrypted_room_seen": diag.encrypted_room_seen,
                "undecryptable_event_count": diag.undecryptable_event_count,
                "megolm_recovery_attempts": diag.megolm_recovery_attempts,
                "megolm_recovery_successes": diag.megolm_recovery_successes,
                "megolm_recovery_failures": diag.megolm_recovery_failures,
                "megolm_recovery_rate_limited": diag.megolm_recovery_rate_limited,
                "megolm_recovery_inflight_rejected": (
                    diag.megolm_recovery_inflight_rejected
                ),
                "megolm_recovery_inflight": diag.megolm_recovery_inflight,
                # Sync recovery
                "sync_running": diag.sync_running,
                "reconnecting": diag.reconnecting,
                "reconnect_attempts": diag.reconnect_attempts,
                "stale_sync_recoveries": diag.stale_sync_recoveries,
                "last_stale_sync_at": diag.last_stale_sync_at,
                "last_successful_sync": diag.last_successful_sync,
                "checkpoint_owned_by_medre": diag.checkpoint_owned_by_medre,
                "committed_checkpoint_present": diag.committed_checkpoint_present,
                "classic_ack_deferrals": diag.classic_ack_deferrals,
                "recovered_event_count": diag.recovered_event_count,
                "history_event_count": diag.history_event_count,
                "recovery_abandoned_room_count": (diag.recovery_abandoned_room_count),
                "recovery_last_abandonment": diag.recovery_last_abandonment,
                # Crypto-store continuity
                "crypto_store_loaded": diag.crypto_store_loaded,
                # E2EE key management diagnostics
                "olm_loaded": diag.olm_loaded,
                "store_loaded": diag.store_loaded,
                "device_keys_uploaded": diag.device_keys_uploaded,
                "key_query_needed": diag.key_query_needed,
                "device_id_in_use": diag.device_id_in_use,
                "store_path_exists": diag.store_path_exists,
                "initial_sync_completed": diag.initial_sync_completed,
                # Own-device cross-signing (separate from peer-device trust)
                "cross_signing_provider_supported": (
                    diag.cross_signing_provider_supported
                ),
                "cross_signing_local_identity_present": (
                    diag.cross_signing_local_identity_present
                ),
                "cross_signing_server_identity_present": (
                    diag.cross_signing_server_identity_present
                ),
                "cross_signing_current_device_self_signed": (
                    diag.cross_signing_current_device_self_signed
                ),
                "cross_signing_chain_status": diag.cross_signing_chain_status,
                "cross_signing_repair_required": (diag.cross_signing_repair_required),
                "cross_signing_reset_required": diag.cross_signing_reset_required,
                "cross_signing_last_failure_category": (
                    diag.cross_signing_last_failure_category
                ),
                # Room counts (no room IDs)
                "encrypted_room_count": diag.encrypted_room_count,
                "plaintext_room_count": diag.plaintext_room_count,
                # Delivery / server-directed rate-limit stats
                "transient_delivery_failures": self._transient_delivery_failures,
                "permanent_delivery_failures": self._permanent_delivery_failures,
                "outbound_rate_limit_events": self._outbound_rate_limit_events,
                "outbound_cooldown_deferrals": self._outbound_cooldown_deferrals,
                "outbound_cooldown_remaining_seconds": (
                    self._outbound_cooldown_remaining()
                ),
                # Inbound diagnostics counters
                "inbound_published": self._inbound_published,
                "inbound_duplicate_admissions": self._inbound_duplicate_admissions,
                "inbound_suppressed_self": self._inbound_suppressed_self,
                "inbound_suppressed_envelope": self._inbound_suppressed_envelope,
                "inbound_filtered_allowlist": self._inbound_filtered_allowlist,
                "inbound_filtered_encryption_policy": (
                    self._inbound_filtered_encryption_policy
                ),
                "inbound_suppressed_startup": self._inbound_suppressed_startup,
            }
        return {
            "connected": False,
            "health": self._last_health,
            "mode": "live",
            "logged_in": False,
            "sync_task_running": False,
            "last_sync_error": None,
            "last_error": None,
            "store_path_configured": self._config.store_path is not None,
            "device_id_configured": self._config.device_id is not None,
            "encryption_mode": self._config.encryption_mode,
            "crypto_enabled": False,
            "last_crypto_error": None,
            "encrypted_room_seen": False,
            "undecryptable_event_count": 0,
            "megolm_recovery_attempts": 0,
            "megolm_recovery_successes": 0,
            "megolm_recovery_failures": 0,
            "megolm_recovery_rate_limited": 0,
            "megolm_recovery_inflight_rejected": 0,
            "megolm_recovery_inflight": 0,
            # Sync recovery
            "sync_running": False,
            "reconnecting": False,
            "reconnect_attempts": 0,
            "stale_sync_recoveries": 0,
            "last_stale_sync_at": None,
            "last_successful_sync": None,
            "checkpoint_owned_by_medre": bool(
                self.ctx is not None
                and self.ctx.admit_inbound is not None
                and self.ctx.load_checkpoint is not None
                and self.ctx.commit_checkpoint is not None
            ),
            "committed_checkpoint_present": False,
            "classic_ack_deferrals": 0,
            "recovered_event_count": 0,
            "history_event_count": 0,
            "recovery_abandoned_room_count": 0,
            "recovery_last_abandonment": None,
            # Crypto-store continuity
            "crypto_store_loaded": False,
            # E2EE key management diagnostics
            "olm_loaded": False,
            "store_loaded": False,
            "device_keys_uploaded": False,
            "key_query_needed": False,
            "device_id_in_use": None,
            "store_path_exists": False,
            "initial_sync_completed": False,
            # Own-device cross-signing (separate from peer-device trust)
            "cross_signing_provider_supported": False,
            "cross_signing_local_identity_present": False,
            "cross_signing_server_identity_present": None,
            "cross_signing_current_device_self_signed": None,
            "cross_signing_chain_status": "unchecked",
            "cross_signing_repair_required": False,
            "cross_signing_reset_required": False,
            "cross_signing_last_failure_category": None,
            # Room counts
            "encrypted_room_count": 0,
            "plaintext_room_count": 0,
            # Delivery / server-directed rate-limit stats
            "transient_delivery_failures": self._transient_delivery_failures,
            "permanent_delivery_failures": self._permanent_delivery_failures,
            "outbound_rate_limit_events": self._outbound_rate_limit_events,
            "outbound_cooldown_deferrals": self._outbound_cooldown_deferrals,
            "outbound_cooldown_remaining_seconds": (
                self._outbound_cooldown_remaining()
            ),
            # Inbound diagnostics counters
            "inbound_published": self._inbound_published,
            "inbound_duplicate_admissions": self._inbound_duplicate_admissions,
            "inbound_suppressed_self": self._inbound_suppressed_self,
            "inbound_suppressed_envelope": self._inbound_suppressed_envelope,
            "inbound_filtered_allowlist": self._inbound_filtered_allowlist,
            "inbound_filtered_encryption_policy": (
                self._inbound_filtered_encryption_policy
            ),
            "inbound_suppressed_startup": self._inbound_suppressed_startup,
        }
