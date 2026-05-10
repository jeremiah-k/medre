"""Transport-owned session boundary for the LXMF adapter.

:class:`LxmfSession` encapsulates the Reticulum/LXMF SDK lifecycle —
identity loading, LXMRouter initialisation, delivery callback
registration, inbound message normalisation, outbound send, bounded
reconnection, and graceful teardown — so that
:class:`~medre.adapters.lxmf.adapter.LxmfAdapter` can delegate all
SDK interaction to this single owned object.

The session is the **sole owner** of ``RNS.Reticulum``,
``RNS.Identity``, and ``LXMF.LXMRouter`` instances.  No other module
in the adapter package imports or touches the SDK directly.

Metadata Normalisation Audit
----------------------------
LXMF native message payloads (``LXMF.LXMessage`` objects) carry:

* ``source_hash`` — 16-byte destination hash of the sender
* ``destination_hash`` — 16-byte destination hash of the recipient
* ``hash`` — unique message hash (bytes)
* ``content`` — UTF-8 text body (bytes or str)
* ``title`` — optional title (bytes or str)
* ``fields`` — dict of typed field key/value pairs
* ``timestamp`` — float seconds since epoch
* ``signature_validated`` — bool
* ``method`` — delivery method enum value
* ``state`` — delivery state enum value
* ``progress`` — float delivery progress (0.0–1.0)

The session normalises all of these into **plain dicts** before
forwarding to the adapter.  No raw ``LXMessage``, ``RNS.Destination``,
or ``RNS.Identity`` objects ever leave the session boundary.

Comparison with other transports:

* **Matrix**: ``event_id``, ``room_id``, ``sender`` (MXID),
  ``content``, ``origin_server_ts``.
* **Meshtastic**: ``messageId`` (uint32), ``fromId``, ``toId``,
  ``channel``, ``rxTime``, ``rxSnr``, ``rxRssi``.
* **MeshCore**: ``pubkey_prefix``, ``sender_timestamp``, ``text``,
  ``type``, ``channel_idx``.

LXMF differs in that:

1. Identity is a **16-byte hash** (hex-encoded), not a MXID or
   numeric node ID.
2. Delivery is inherently **asynchronous** — messages traverse the
   mesh over multiple hops with no guaranteed delivery time.
3. Delivery state is tracked by the LXMRouter through discrete
   states (generating → outbound → sending → sent → delivered, or
   failed/rejected/cancelled).
4. Supports direct, opportunistic, and propagated delivery methods.

The codec normalises all of these into
:class:`~medre.core.events.metadata.NativeMetadata` under an ``lxmf``
namespace, stripping SDK-specific structures before emitting canonical
events.

Connection Modes
----------------
The session supports two connection types:

``"fake"``
    No real SDK client.  Used for unit tests without hardware.

``"reticulum"``
    Connects to a locally-running Reticulum instance via the ``RNS``
    and ``lxmf`` packages.  Requires an identity (loaded from
    ``identity_path`` or auto-generated).

Reconnect Policy
----------------
On unexpected disconnect the session attempts bounded exponential
backoff:

* Base delays: 1 s, 2 s, 4 s, 8 s, … capped at 30 s.
* ±25 % jitter on each delay to avoid thundering-herd synchronisation.
* Maximum 10 consecutive attempts.
* On ``stop()`` a ``_stop_requested`` guard prevents further
  reconnects.

Delivery State Model
--------------------
LXMF delivery states are modelled truthfully:

* ``generating`` — message is being constructed
* ``outbound`` — queued for delivery
* ``sending`` — actively being transmitted
* ``sent`` — sent to the network (not yet confirmed delivered)
* ``delivered`` — confirmed delivered to the recipient
* ``failed`` — delivery failed permanently
* ``rejected`` — delivery was rejected by the recipient
* ``cancelled`` — delivery was cancelled by the sender

The session does **not** pretend real-time delivery success.  Outbound
messages start in ``generating``/``outbound`` and progress
asynchronously through the LXMF delivery pipeline.  The adapter
reports honest ``pending`` semantics.

Inbound Normalisation
---------------------
Raw ``LXMF.LXMessage`` objects are converted to plain dicts with these
fields:

* ``source_hash`` — hex string (32 chars)
* ``destination_hash`` — hex string (32 chars) if available
* ``message_id`` — hex string of the message hash
* ``timestamp`` — float epoch seconds
* ``title`` — str (may be empty)
* ``content`` — str
* ``fields`` — dict[int, Any] (raw field dict)
* ``signature_validated`` — bool
* ``has_fields`` — bool
* ``delivery_method`` — str ("direct"|"opportunistic"|"propagated"|None)

No raw ``LXMessage``, ``RNS.Destination``, or ``RNS.Identity``
objects are ever included in the normalised dict.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from medre.adapters.lxmf.compat import HAS_LXMF, _require_lxmf
from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.errors import (
    LxmfConnectionError,
    LxmfSendError,
    LxmfSessionError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Reconnect backoff parameters.
_RECONNECT_BASE_DELAY: float = 1.0  # seconds
_RECONNECT_MAX_DELAY: float = 30.0  # seconds
_RECONNECT_MAX_ATTEMPTS: int = 10
_RECONNECT_JITTER_FRACTION: float = 0.25  # ±25 %

# Outbound delivery retry.
_SEND_MAX_RETRIES: int = 3

# Type alias for the inbound message callback.
# The callback receives a plain dict (normalised native payload),
# NOT a raw LXMF.LXMessage object.
# Both sync and async callables are accepted.
MessageCallback = Callable[[dict[str, Any]], Any]


# ---------------------------------------------------------------------------
# Delivery states
# ---------------------------------------------------------------------------


class LxmfDeliveryState(str, Enum):
    """LXMF message delivery state, mapped from LXMF.LXMessage states.

    These are stringly-typed so they serialise cleanly and do not
    require the ``lxmf`` package at import time.
    """

    GENERATING = "generating"
    OUTBOUND = "outbound"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# Map of known LXMF.LXMessage state attribute names to our enum.
# The actual LXMF states are integer constants on the LXMessage class;
# we map them by numeric value when possible, with string fallback.
_LXMF_STATE_MAP: dict[int, LxmfDeliveryState] = {}


def _build_state_map() -> dict[int, LxmfDeliveryState]:
    """Build the LXMF state → LxmfDeliveryState mapping.

    Only callable when lxmf is installed.  Called once lazily.
    """
    if _LXMF_STATE_MAP:
        return _LXMF_STATE_MAP

    if not HAS_LXMF:
        return _LXMF_STATE_MAP

    try:
        _, lxmf = _require_lxmf()
        lxm_cls = getattr(lxmf, "LXMessage", None)
        if lxm_cls is None:
            return _LXMF_STATE_MAP

        # LXMF.LXMessage states are class-level constants.
        # Map known names to our enum values.
        _name_to_state = {
            "GENERATING": LxmfDeliveryState.GENERATING,
            "OUTBOUND": LxmfDeliveryState.OUTBOUND,
            "SENDING": LxmfDeliveryState.SENDING,
            "SENT": LxmfDeliveryState.SENT,
            "DELIVERED": LxmfDeliveryState.DELIVERED,
            "FAILED": LxmfDeliveryState.FAILED,
            "REJECTED": LxmfDeliveryState.REJECTED,
            "CANCELLED": LxmfDeliveryState.CANCELLED,
        }

        for name, state in _name_to_state.items():
            val = getattr(lxm_cls, name, None)
            if val is not None and isinstance(val, int):
                _LXMF_STATE_MAP[val] = state
    except Exception:
        pass

    return _LXMF_STATE_MAP


def _map_delivery_state(raw_state: Any) -> LxmfDeliveryState:
    """Map a raw LXMF delivery state value to our enum.

    Maps conservatively: unknown values become ``UNKNOWN``.
    """
    if isinstance(raw_state, int):
        mapping = _build_state_map()
        return mapping.get(raw_state, LxmfDeliveryState.UNKNOWN)
    if isinstance(raw_state, LxmfDeliveryState):
        return raw_state
    if isinstance(raw_state, str):
        try:
            return LxmfDeliveryState(raw_state.lower())
        except ValueError:
            return LxmfDeliveryState.UNKNOWN
    return LxmfDeliveryState.UNKNOWN


def _map_delivery_method(raw_method: Any) -> str | None:
    """Map a raw LXMF delivery method to a plain string.

    Returns ``"direct"``, ``"opportunistic"``, ``"propagated"``,
    or ``None`` for unknown values.
    """
    if raw_method is None:
        return None

    # LXMF.LXMessage defines method constants as class attributes.
    _method_names = {
        "DIRECT": "direct",
        "OPPORTUNISTIC": "opportunistic",
        "PROPAGATED": "propagated",
        "PAPER": "paper",
    }

    # Try by name match (if it's a string-like enum)
    if isinstance(raw_method, str):
        lower = raw_method.lower()
        if lower in ("direct", "opportunistic", "propagated", "paper"):
            return lower
        return None

    # Try by numeric value if lxmf is available
    if HAS_LXMF and isinstance(raw_method, int):
        try:
            _, lxmf = _require_lxmf()
            lxm_cls = getattr(lxmf, "LXMessage", None)
            if lxm_cls is not None:
                for name, mapped in _method_names.items():
                    val = getattr(lxm_cls, name, None)
                    if val is not None and raw_method == val:
                        return mapped
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass
class _SessionDiagnostics:
    """Mutable diagnostics snapshot owned by the session."""

    connected: bool = False
    router_running: bool = False
    reconnecting: bool = False
    reconnect_attempts: int = 0
    last_message_time: datetime | None = None
    last_error: str | None = None
    transient_delivery_failures: int = 0
    permanent_delivery_failures: int = 0
    known_path_count: int | None = None
    propagation_enabled: bool | None = None
    pending_delivery_count: int | None = None


@dataclass(frozen=True)
class LxmfSessionDiagnostics:
    """Read-only snapshot of session operational state.

    No secrets, private keys, identity material, raw RNS/LXMF objects,
    or unsafe peer dumps are exposed.
    """

    connected: bool
    router_running: bool
    reconnecting: bool
    reconnect_attempts: int
    last_message_time: str | None
    transient_delivery_failures: int
    permanent_delivery_failures: int
    last_error: str | None
    known_path_count: int | None
    propagation_enabled: bool | None
    pending_delivery_count: int | None
    mode: str


# ---------------------------------------------------------------------------
# Outbound delivery tracking
# ---------------------------------------------------------------------------


@dataclass
class _OutboundDelivery:
    """Tracks a pending outbound LXMF delivery."""

    native_message_id: str | None
    state: LxmfDeliveryState
    destination_hash: str
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_state_change: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class LxmfSession:
    """Transport-owned session boundary wrapping Reticulum/LXMF.

    Owns ``RNS.Reticulum``, ``RNS.Identity``, and ``LXMF.LXMRouter``
    instances.  Manages their full lifecycle: identity loading, router
    creation, delivery callback registration, inbound normalisation,
    outbound send with bounded retry, reconnect, and teardown.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.lxmf.config.LxmfConfig`.
    adapter_id:
        Identifier of the owning adapter (for logging).
    platform:
        Platform string (``"lxmf"``).
    logger:
        Optional logger; defaults to ``logging.getLogger(...)``.
    """

    __slots__ = (
        "__weakref__",
        "_config",
        "_adapter_id",
        "_platform",
        "_logger",
        # SDK objects
        "_reticulum",
        "_identity",
        "_router",
        # Inbound callback
        "_message_callback",
        # Lifecycle guards
        "_started",
        "_stop_requested",
        # Reconnect
        "_reconnect_task",
        # Diagnostics
        "_diag",
        # Outbound tracking
        "_outbound_deliveries",
        # Announce timer
        "_announce_task",
    )

    def __init__(
        self,
        config: LxmfConfig,
        adapter_id: str,
        platform: str = "lxmf",
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._adapter_id = adapter_id
        self._platform = platform
        self._logger: logging.Logger = logger or logging.getLogger(
            f"medre.adapters.lxmf.session.{adapter_id}"
        )

        # SDK objects — only populated for real connection modes.
        self._reticulum: Any = None  # RNS.Reticulum instance
        self._identity: Any = None  # RNS.Identity instance
        self._router: Any = None  # LXMF.LXMRouter instance

        # Inbound callback set via start().
        self._message_callback: MessageCallback | None = None

        # Lifecycle guards.
        self._started: bool = False
        self._stop_requested: bool = False
        self._reconnect_task: asyncio.Task | None = None
        self._announce_task: asyncio.Task | None = None

        # Diagnostics.
        self._diag = _SessionDiagnostics()

        # Outbound delivery tracking: message_id → _OutboundDelivery
        self._outbound_deliveries: dict[str, _OutboundDelivery] = {}

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the session has an active router."""
        return self._diag.connected

    @property
    def router_running(self) -> bool:
        """Whether the LXMRouter is operational."""
        return self._diag.router_running

    @property
    def reconnecting(self) -> bool:
        """Whether a reconnect loop is in progress."""
        return self._diag.reconnecting

    @property
    def reconnect_attempts(self) -> int:
        """Number of consecutive reconnect attempts since last disconnect."""
        return self._diag.reconnect_attempts

    @property
    def last_error(self) -> str | None:
        """Human-readable description of the last session-level error."""
        return self._diag.last_error

    @property
    def transient_delivery_failures(self) -> int:
        """Count of transient outbound delivery failures."""
        return self._diag.transient_delivery_failures

    @property
    def permanent_delivery_failures(self) -> int:
        """Count of permanent outbound delivery failures."""
        return self._diag.permanent_delivery_failures

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        message_callback: MessageCallback | None = None,
    ) -> None:
        """Initialise the LXMF session and begin receiving messages.

        Parameters
        ----------
        message_callback:
            Callback invoked with normalised message dicts for inbound
            messages.  The dict contains only plain Python types — no
            raw LXMF/RNS objects.

        Raises
        ------
        LxmfConnectionError
            If the SDK is not installed (non-fake mode) or the
            connection fails.
        """
        if self._started:
            return

        self._message_callback = message_callback
        self._stop_requested = False

        if self._config.connection_type == "fake":
            # Fake mode: no real SDK client needed.
            self._diag.connected = True
            self._diag.router_running = True
        else:
            await self._connect_real()

        self._started = True
        self._logger.info(
            "LxmfSession %s started (mode=%s, connected=%s)",
            self._adapter_id,
            self._config.connection_type,
            self._diag.connected,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from LXMF/Reticulum and release all resources.

        Sets ``_stop_requested`` to prevent reconnect loops.
        Idempotent — safe to call multiple times.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        self._stop_requested = True

        # Cancel announce task if running.
        if self._announce_task is not None and not self._announce_task.done():
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
            self._announce_task = None

        # Cancel reconnect task if running.
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await asyncio.wait_for(self._reconnect_task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._reconnect_task = None

        # Unsubscribe delivery callbacks.
        self._unsubscribe_callbacks()

        # Tear down router, identity, reticulum.
        self._teardown_sdk()

        # Clear outbound tracking.
        self._outbound_deliveries.clear()

        self._diag.connected = False
        self._diag.router_running = False
        self._diag.reconnecting = False
        self._started = False
        self._logger.info(
            "LxmfSession %s stopped", self._adapter_id
        )

    # ------------------------------------------------------------------
    # Outbound send
    # ------------------------------------------------------------------

    async def send_text(
        self,
        destination_hash: str,
        content: str,
        *,
        title: str = "",
        delivery_method: str | None = None,
        fields: dict[int, Any] | None = None,
    ) -> tuple[str | None, LxmfDeliveryState]:
        """Send a text message via the LXMF router.

        Parameters
        ----------
        destination_hash:
            Hex-encoded destination hash (32 hex chars).
        content:
            Message body text.
        title:
            Optional message title.
        delivery_method:
            Override for delivery method.  ``None`` uses config default.
        fields:
            Optional LXMF fields dict.

        Returns
        -------
        tuple[str | None, LxmfDeliveryState]
            ``(native_message_id, initial_state)``.  In fake mode,
            returns ``(fake_id, OUTBOUND)``.  In real mode, returns the
            message hash and the initial delivery state.

        Raises
        ------
        LxmfSendError
            On permanent failure or after exhausting retries.
        """
        if not self._diag.connected:
            raise LxmfSendError("Session is not connected")

        if self._config.connection_type == "fake":
            # Fake mode — no real send.  Return honest pending semantics.
            fake_id = f"fake-{id(self)}-{time.monotonic_ns()}"
            state = LxmfDeliveryState.OUTBOUND
            self._outbound_deliveries[fake_id] = _OutboundDelivery(
                native_message_id=fake_id,
                state=state,
                destination_hash=destination_hash,
            )
            return fake_id, state

        return await self._send_real(
            destination_hash=destination_hash,
            content=content,
            title=title,
            delivery_method=delivery_method,
            fields=fields,
        )

    # ------------------------------------------------------------------
    # Inbound simulation (for testing)
    # ------------------------------------------------------------------

    def inject_inbound(self, message_dict: dict[str, Any]) -> None:
        """Inject a normalised message dict for fake-mode testing.

        This bypasses the SDK entirely and calls the message callback
        directly.  Used by the adapter's ``simulate_inbound`` method.

        Parameters
        ----------
        message_dict:
            A normalised LXMF message payload dict.
        """
        if self._message_callback is None:
            return
        self._diag.last_message_time = datetime.now(timezone.utc)
        result = self._message_callback(message_dict)
        # Support async callbacks
        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(result)
            except RuntimeError:
                # No running loop; run synchronously
                asyncio.run(result)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> LxmfSessionDiagnostics:
        """Return a safe diagnostics snapshot.

        No secrets, private keys, identity material, raw RNS/LXMF
        objects, or unsafe peer dumps are exposed.
        """
        self._refresh_safe_diagnostics()

        return LxmfSessionDiagnostics(
            connected=self._diag.connected,
            router_running=self._diag.router_running,
            reconnecting=self._diag.reconnecting,
            reconnect_attempts=self._diag.reconnect_attempts,
            last_message_time=(
                self._diag.last_message_time.isoformat()
                if self._diag.last_message_time
                else None
            ),
            transient_delivery_failures=self._diag.transient_delivery_failures,
            permanent_delivery_failures=self._diag.permanent_delivery_failures,
            last_error=self._diag.last_error,
            known_path_count=self._diag.known_path_count,
            propagation_enabled=self._diag.propagation_enabled,
            pending_delivery_count=(
                len(self._outbound_deliveries)
                if self._outbound_deliveries
                else None
            ),
            mode=self._config.connection_type,
        )

    def delivery_state_counts(self) -> dict[str, int]:
        """Return counts of outbound deliveries per state.

        Useful for monitoring pending vs. completed deliveries.
        """
        counts: dict[str, int] = {}
        for delivery in self._outbound_deliveries.values():
            key = delivery.state.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ==================================================================
    # Private — real connection
    # ==================================================================

    async def _connect_real(self) -> None:
        """Create real Reticulum/LXMF objects and subscribe to events."""
        if not HAS_LXMF:
            raise LxmfConnectionError(
                "lxmf/RNS not installed; pip install lxmf "
                "or use connection_type='fake'"
            )

        RNS, lxmf = _require_lxmf()

        try:
            # 1. Initialise Reticulum.
            self._reticulum = RNS.Reticulum(None)

            # 2. Load or create identity.
            if self._config.identity_path:
                self._identity = RNS.Identity.from_file(self._config.identity_path)
                if self._identity is None:
                    raise LxmfConnectionError(
                        f"Failed to load identity from "
                        f"{self._config.identity_path!r}"
                    )
            else:
                self._identity = RNS.Identity()

            # 3. Create LXMRouter.
            self._router = lxmf.LXMRouter(
                identity=self._identity,
            )

            # 4. Register delivery callback.
            self._router.register_delivery_callback(self._on_lxmf_delivery)

            # 5. Optional: register announce callback.
            try:
                self._router.register_announce_callback(self._on_lxmf_announce)
            except (AttributeError, TypeError):
                # Not all LXMF versions support announce callbacks.
                pass

        except LxmfConnectionError:
            raise
        except Exception as exc:
            self._diag.last_error = str(exc)
            self._teardown_sdk()
            raise LxmfConnectionError(
                f"Failed to initialise LXMF session: {exc}"
            ) from exc

        self._diag.connected = True
        self._diag.router_running = True
        self._diag.reconnect_attempts = 0

    def _teardown_sdk(self) -> None:
        """Release all SDK objects in reverse order."""
        # Router → Identity → Reticulum
        self._router = None
        self._identity = None
        # Reticulum teardown: best-effort.
        if self._reticulum is not None:
            try:
                # RNS.Reticulum may have a stop method in newer versions.
                stop_fn = getattr(self._reticulum, "stop", None)
                if stop_fn is not None:
                    stop_fn()
            except Exception:
                pass
            self._reticulum = None

    # ------------------------------------------------------------------
    # SDK callbacks
    # ------------------------------------------------------------------

    def _on_lxmf_delivery(self, message: Any) -> None:
        """Handle an inbound LXMF.LXMessage from the router.

        Normalises the raw message into a plain dict and forwards
        it to the registered message callback.  No raw LXMF/RNS
        objects leak past this boundary.
        """
        try:
            normalised = self._normalise_inbound_message(message)
        except Exception as exc:
            self._logger.warning(
                "LxmfSession %s: failed to normalise inbound message: %s",
                self._adapter_id,
                exc,
            )
            return

        if self._message_callback is None:
            return

        self._diag.last_message_time = datetime.now(timezone.utc)

        try:
            result = self._message_callback(normalised)
            # Support async callbacks
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    asyncio.run(result)
        except Exception as exc:
            self._logger.warning(
                "LxmfSession %s: message callback error: %s",
                self._adapter_id,
                exc,
            )

    def _on_lxmf_announce(self, *args: Any, **kwargs: Any) -> None:
        """Handle LXMF announce events.

        Announce handling is informational only — it updates
        path/diagnostics but does not generate canonical events.
        """
        self._logger.debug(
            "LxmfSession %s: LXMF announce received",
            self._adapter_id,
        )

    def _on_delivery_state_update(self, message: Any) -> None:
        """Handle delivery state updates for outbound messages.

        Tracks state transitions in the outbound delivery tracking
        dict without exposing raw objects.
        """
        try:
            msg_hash = self._extract_message_hash(message)
            if msg_hash is None:
                return

            raw_state = getattr(message, "state", None)
            new_state = _map_delivery_state(raw_state)

            delivery = self._outbound_deliveries.get(msg_hash)
            if delivery is not None:
                old_state = delivery.state
                delivery.state = new_state
                delivery.last_state_change = datetime.now(timezone.utc)
                self._logger.debug(
                    "LxmfSession %s: delivery %s state %s → %s",
                    self._adapter_id,
                    msg_hash[:16],
                    old_state.value,
                    new_state.value,
                )

                # Clean up terminal states after a delay.
                if new_state in (
                    LxmfDeliveryState.DELIVERED,
                    LxmfDeliveryState.FAILED,
                    LxmfDeliveryState.REJECTED,
                    LxmfDeliveryState.CANCELLED,
                ):
                    if new_state == LxmfDeliveryState.FAILED:
                        self._diag.transient_delivery_failures += 1
                    elif new_state in (
                        LxmfDeliveryState.REJECTED,
                        LxmfDeliveryState.CANCELLED,
                    ):
                        self._diag.permanent_delivery_failures += 1
        except Exception as exc:
            self._logger.debug(
                "LxmfSession %s: error tracking delivery state: %s",
                self._adapter_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Inbound normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_inbound_message(message: Any) -> dict[str, Any]:
        """Convert a raw LXMF.LXMessage to a plain dict.

        The resulting dict contains only plain Python types — no
        raw LXMF or RNS objects.
        """
        # Source hash
        source_hash_bytes = getattr(message, "source_hash", None)
        if isinstance(source_hash_bytes, bytes):
            source_hash = source_hash_bytes.hex()
        elif isinstance(source_hash_bytes, str):
            source_hash = source_hash_bytes
        else:
            source_hash = ""

        # Destination hash
        dest_hash_bytes = getattr(message, "destination_hash", None)
        if isinstance(dest_hash_bytes, bytes):
            destination_hash = dest_hash_bytes.hex()
        elif isinstance(dest_hash_bytes, str):
            destination_hash = dest_hash_bytes
        else:
            destination_hash = ""

        # Message hash / ID
        msg_hash = LxmfSession._extract_message_hash(message)

        # Timestamp
        timestamp = getattr(message, "timestamp", None)
        if timestamp is not None:
            try:
                timestamp = float(timestamp)
            except (TypeError, ValueError):
                timestamp = None

        # Content
        raw_content = getattr(message, "content", None)
        if isinstance(raw_content, bytes):
            try:
                content = raw_content.decode("utf-8")
            except UnicodeDecodeError:
                content = raw_content.decode("utf-8", errors="replace")
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = ""

        # Title
        raw_title = getattr(message, "title", None)
        if isinstance(raw_title, bytes):
            try:
                title = raw_title.decode("utf-8")
            except UnicodeDecodeError:
                title = raw_title.decode("utf-8", errors="replace")
        elif isinstance(raw_title, str):
            title = raw_title
        else:
            title = ""

        # Fields
        raw_fields = getattr(message, "fields", None)
        fields: dict[int, Any] = raw_fields if isinstance(raw_fields, dict) else {}
        has_fields = bool(fields)

        # Signature validation
        signature_validated = getattr(message, "signature_validated", False)

        # Delivery method
        raw_method = getattr(message, "method", None)
        delivery_method = _map_delivery_method(raw_method)

        return {
            "source_hash": source_hash,
            "destination_hash": destination_hash,
            "message_id": msg_hash,
            "timestamp": timestamp,
            "title": title,
            "content": content,
            "fields": fields,
            "signature_validated": signature_validated,
            "has_fields": has_fields,
            "delivery_method": delivery_method,
        }

    @staticmethod
    def _extract_message_hash(message: Any) -> str | None:
        """Extract a deterministic message hash from an LXMF message.

        Tries ``message.hash`` (bytes → hex), then ``message.message_id``.
        Returns ``None`` if neither is available.
        """
        msg_hash = getattr(message, "hash", None)
        if isinstance(msg_hash, bytes):
            return msg_hash.hex()
        if isinstance(msg_hash, str):
            return msg_hash

        msg_id = getattr(message, "message_id", None)
        if isinstance(msg_id, bytes):
            return msg_id.hex()
        if isinstance(msg_id, str):
            return msg_id

        return None

    # ------------------------------------------------------------------
    # Outbound send (real)
    # ------------------------------------------------------------------

    async def _send_real(
        self,
        destination_hash: str,
        content: str,
        *,
        title: str = "",
        delivery_method: str | None = None,
        fields: dict[int, Any] | None = None,
    ) -> tuple[str | None, LxmfDeliveryState]:
        """Send via the real LXMF router with bounded retry."""
        RNS, lxmf = _require_lxmf()

        if self._router is None:
            raise LxmfSendError("LXMRouter is not initialised")

        # Determine delivery method.
        method_str = delivery_method or self._config.default_delivery_method
        method_const = self._resolve_method_constant(lxmf, method_str)

        # Parse destination hash.
        try:
            dest_bytes = bytes.fromhex(destination_hash)
        except (ValueError, TypeError) as exc:
            raise LxmfSendError(
                f"Invalid destination hash: {destination_hash!r}"
            ) from exc

        last_exc: Exception | None = None
        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                # Create destination object from hash.
                dest = RNS.Destination(
                    self._identity,
                    RNS.Destination.OUT,
                    RNS.Destination.SINGLE,
                    "lxmf",
                    "delivery",
                )
                # Override with the actual destination hash.
                # NOTE: In production, the destination lookup is more nuanced.
                # This is the standard pattern from LXMF examples.
                dest.hash = dest_bytes

                # Build the LXMessage — include fields so rendered
                # metadata (MEDRE envelope, provenance hints, etc.)
                # is preserved through serialisation (pack()).
                lxm = lxmf.LXMessage(
                    dest,
                    self._router,
                    content,
                    title=title,
                    fields=fields,
                    desired_method=method_const,
                )

                # Attach delivery state callback if supported.
                lxm.register_delivery_callback(
                    self._on_delivery_state_update
                )

                # Extract message hash BEFORE sending (if available).
                native_id = self._extract_message_hash(lxm)

                # Send via the router.
                self._router.handle_outbound(lxm)

                # Try to get message hash after sending.
                if native_id is None:
                    native_id = self._extract_message_hash(lxm)

                initial_state = _map_delivery_state(
                    getattr(lxm, "state", None)
                )

                # Track the delivery.
                if native_id is not None:
                    self._outbound_deliveries[native_id] = _OutboundDelivery(
                        native_message_id=native_id,
                        state=initial_state,
                        destination_hash=destination_hash,
                    )

                return native_id, initial_state

            except asyncio.CancelledError:
                raise
            except (ValueError, TypeError) as exc:
                self._diag.permanent_delivery_failures += 1
                self._diag.last_error = f"Permanent send failure: {exc}"
                raise LxmfSendError(
                    f"Permanent send failure: {exc}"
                ) from exc
            except Exception as exc:
                last_exc = exc
                self._diag.transient_delivery_failures += 1
                self._diag.last_error = (
                    f"Transient send failure (attempt {attempt}): {exc}"
                )
                self._logger.warning(
                    "LxmfSession %s transient send failure "
                    "(attempt %d/%d): %s",
                    self._adapter_id, attempt, _SEND_MAX_RETRIES, exc,
                )
                if attempt < _SEND_MAX_RETRIES:
                    await asyncio.sleep(0.1 * attempt)

        # All retries exhausted.
        self._diag.permanent_delivery_failures += 1
        self._diag.last_error = (
            f"Send failed after {_SEND_MAX_RETRIES} attempts: {last_exc}"
        )
        raise LxmfSendError(
            f"Send failed after {_SEND_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc

    @staticmethod
    def _resolve_method_constant(lxmf: Any, method_str: str) -> Any:
        """Resolve a delivery method string to an LXMF constant.

        Falls back to ``None`` (let the router decide) if the constant
        is not available.
        """
        lxm_cls = getattr(lxmf, "LXMessage", None)
        if lxm_cls is None:
            return None

        _method_attr = {
            "direct": "DIRECT",
            "opportunistic": "OPPORTUNISTIC",
            "propagated": "PROPAGATED",
            "paper": "PAPER",
        }

        attr_name = _method_attr.get(method_str.lower())
        if attr_name is not None:
            return getattr(lxm_cls, attr_name, None)

        return None

    # ------------------------------------------------------------------
    # Callback unsubscribe
    # ------------------------------------------------------------------

    def _unsubscribe_callbacks(self) -> None:
        """Unsubscribe all registered SDK callbacks."""
        if self._router is not None:
            # Best-effort: try to deregister delivery callback.
            try:
                # LXMRouter doesn't have a standard unregister method.
                # Setting to None/empty effectively stops callbacks
                # because the router reference is about to be dropped.
                pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Safe diagnostics refresh
    # ------------------------------------------------------------------

    def _refresh_safe_diagnostics(self) -> None:
        """Refresh diagnostics values from the SDK if safe.

        Only queries public, non-sensitive router state.  Never
        accesses identity material, secret keys, or peer dumps.
        """
        if self._router is None:
            return

        try:
            # Known path count — safe, non-sensitive.
            path_table = getattr(self._router, "path_table", None)
            if isinstance(path_table, (dict, list)):
                self._diag.known_path_count = len(path_table)
            elif path_table is not None:
                # Some versions use different structures.
                count_fn = getattr(path_table, "__len__", None)
                if count_fn is not None:
                    self._diag.known_path_count = count_fn()
        except Exception:
            pass

        try:
            # Propagation enabled — boolean, non-sensitive.
            prop_node = getattr(self._router, "propagation_node", None)
            if prop_node is not None:
                self._diag.propagation_enabled = True
            else:
                self._diag.propagation_enabled = False
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _trigger_reconnect(self) -> None:
        """Start a reconnect loop if not already running."""
        if self._stop_requested:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return

        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Bounded exponential backoff reconnect loop.

        Delays: 1 s → 2 s → 4 s → 8 s → … capped at 30 s.
        ±25 % jitter.  Max 10 attempts.
        """
        self._diag.reconnecting = True
        self._diag.reconnect_attempts = 0

        try:
            while (
                not self._stop_requested
                and self._diag.reconnect_attempts < _RECONNECT_MAX_ATTEMPTS
            ):
                delay = min(
                    _RECONNECT_BASE_DELAY
                    * (2 ** self._diag.reconnect_attempts),
                    _RECONNECT_MAX_DELAY,
                )
                # Apply jitter.
                jitter = delay * _RECONNECT_JITTER_FRACTION
                delay = delay + random.uniform(-jitter, jitter)
                delay = max(0.0, delay)

                self._logger.info(
                    "LxmfSession %s: reconnect attempt %d/%d in %.1fs",
                    self._adapter_id,
                    self._diag.reconnect_attempts + 1,
                    _RECONNECT_MAX_ATTEMPTS,
                    delay,
                )

                await asyncio.sleep(delay)

                if self._stop_requested:
                    break

                try:
                    self._teardown_sdk()
                    await self._connect_real()
                    self._logger.info(
                        "LxmfSession %s: reconnected successfully",
                        self._adapter_id,
                    )
                    self._diag.reconnecting = False
                    return
                except Exception as exc:
                    self._diag.reconnect_attempts += 1
                    self._diag.last_error = str(exc)
                    self._logger.warning(
                        "LxmfSession %s: reconnect failed (attempt %d): %s",
                        self._adapter_id,
                        self._diag.reconnect_attempts,
                        exc,
                    )

            # Exhausted attempts.
            self._diag.last_error = (
                f"Reconnect exhausted after "
                f"{self._diag.reconnect_attempts} attempts"
            )
            self._diag.reconnecting = False

        except asyncio.CancelledError:
            self._diag.reconnecting = False
