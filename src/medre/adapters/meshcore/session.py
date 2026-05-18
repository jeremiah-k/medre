"""Transport-owned session boundary for the MeshCore adapter.

:class:`MeshCoreSession` encapsulates the MeshCore SDK client lifecycle —
connection creation, event subscription, reconnection, and clean shutdown —
so that :class:`~medre.adapters.meshcore.adapter.MeshCoreAdapter` can
delegate all SDK interaction to this single owned object.

The session is the **sole owner** of the SDK ``MeshCore`` instance.  No
other module in the adapter package imports or touches the SDK directly.

Metadata Normalization Audit
----------------------------
MeshCore native message payloads (from the SDK reader) carry these fields:

* ``type`` — ``"PRIV"`` (direct/DM) or ``"CHAN"`` (channel broadcast)
* ``pubkey_prefix`` — 6-byte hex prefix of the sender's public key
* ``sender_timestamp`` — 4-byte little-endian Unix timestamp
* ``text`` — UTF-8 decoded payload text
* ``txt_type`` — message sub-type code (0=plain, 2=signed, etc.)
* ``channel_idx`` — channel index (CHAN messages only)
* ``path_len``, ``path_hash_mode`` — routing metadata (optional)
* ``SNR``, ``RSSI`` — radio metadata (V3 messages only)

Comparison with other transports:

* **Matrix**: uses ``event_id``, ``room_id``, ``sender`` (MXID), ``content``,
  ``origin_server_ts``, ``type`` (event type string).  Metadata is
  JSON-structured with ``m.relates_to`` for threading.
* **Meshtastic**: uses ``messageId`` (uint32), ``fromId``, ``toId``,
  ``channel``, ``portnum``, ``rxTime``, ``rxSnr``, ``rxRssi``, ``hopLimit``.
  Rich metadata in protobuf-structured ``decoded`` payloads.

MeshCore differs in that:

1. Sender identity is a **pubkey prefix** (6 hex bytes), not a human-readable
   MXID or numeric node ID.
2. Channel is an **index** (0–255), not a room ID or name.
3. No built-in reply/threading — ``txt_type`` carries sub-type info only.
4. Radio metadata (SNR/RSSI) is only available in V3 protocol messages.

The codec normalises all of these into
:class:`~medre.core.events.metadata.NativeMetadata` under a ``meshcore``
namespace, stripping SDK-specific structures before emitting canonical events.

Connection Modes
----------------
The session supports four connection types via
:class:`~medre.adapters.meshcore.config.MeshCoreConfig`:

``"fake"``
    No real SDK client.  Used for unit tests without hardware.

``"tcp"``
    Connects via TCP using ``MeshCore.create_tcp(host, port)`` factory.

``"serial"``
    Connects via serial using ``MeshCore.create_serial(port, baudrate)`` factory.

``"ble"``
    Connects via BLE using ``MeshCore.create_ble(address)`` factory.

Reconnect Policy
----------------
On unexpected disconnect the session attempts bounded exponential backoff:

* Base delays: 1 s, 2 s, 4 s, 8 s, … capped at 30 s.
* ±25 % jitter on each delay to avoid thundering-herd synchronisation.
* Maximum 10 consecutive attempts.
* On ``stop()`` a ``_stop_requested`` guard prevents further reconnects.

.. note::

   **Duplicate-send risk.**  The session's :meth:`send_text` method retries
   transient failures up to 3 times.  Because acknowledgements are not
   de-duplicated, a message that was received by the remote node but whose
   ACK was lost on the link may be sent again.  Consumers must be tolerant
   of duplicate deliveries.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Protocol, cast

from medre.adapters.meshcore.compat import HAS_MESHCORE
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
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
# The callback receives a plain dict (native payload), NOT an SDK Event object.
# Both sync and async callables are accepted.
MessageCallback = Callable[[dict[str, Any]], Any]


@dataclass
class _SessionDiagnostics:
    """Mutable diagnostics snapshot owned by the session."""

    connected: bool = False
    reconnecting: bool = False
    reconnect_attempts: int = 0
    last_message_time: datetime | None = None
    last_error: str | None = None
    transient_delivery_failures: int = 0
    permanent_delivery_failures: int = 0
    peer_count: int | None = None


class _MeshCoreModule(Protocol):
    """Structural type for the optional ``meshcore`` SDK package.

    Defines the subset of the SDK's public API used by
    :meth:`MeshCoreSession._connect_real`.  Because ``meshcore`` is an
    optional dependency whose package may be absent at type-check time,
    this Protocol gives Pyright a concrete shape without requiring the
    SDK's type stubs to be installed.

    The SDK exposes async factory methods on ``MeshCore`` —
    ``create_tcp``, ``create_serial``, ``create_ble`` — which handle
    connection construction and initial handshake internally.
    """

    MeshCore: type
    EventType: Any


class MeshCoreSession:
    """Transport-owned session boundary wrapping a MeshCore SDK client.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.meshcore.config.MeshCoreConfig`.
    adapter_id:
        Identifier of the owning adapter (for logging).
    platform:
        Platform string (``"meshcore"``).
    logger:
        Optional logger; defaults to ``logging.getLogger(...)``.
    """

    def __init__(
        self,
        config: MeshCoreConfig,
        adapter_id: str,
        platform: str = "meshcore",
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._adapter_id = adapter_id
        self._platform = platform
        self._logger = logger or logging.getLogger(
            f"medre.adapters.meshcore.session.{adapter_id}"
        )

        # SDK objects — only populated for real connection modes.
        self._meshcore: Any = None  # meshcore.MeshCore instance
        self._subscriptions: list[Any] = []  # Subscription handles

        # Inbound callback set via start().
        self._message_callback: MessageCallback | None = None

        # Lifecycle guards.
        self._started: bool = False
        self._stop_requested: bool = False
        self._reconnect_task: asyncio.Task | None = None

        # Diagnostics.
        self._diag = _SessionDiagnostics()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the session has an active SDK connection."""
        return self._diag.connected

    @property
    def reconnecting(self) -> bool:
        """Whether a reconnect loop is in progress."""
        return self._diag.reconnecting

    @property
    def reconnect_attempts(self) -> int:
        """Number of consecutive reconnect attempts since last disconnect."""
        return self._diag.reconnect_attempts

    @property
    def last_message_time(self) -> datetime | None:
        """UTC datetime of the last successfully processed inbound message."""
        return self._diag.last_message_time

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

    async def start(self, message_callback: MessageCallback) -> None:
        """Connect to MeshCore and begin receiving events.

        Parameters
        ----------
        message_callback:
            Async callable invoked with a plain dict for each inbound
            message.  The dict contains native MeshCore payload fields
            (``text``, ``pubkey_prefix``, ``sender_timestamp``, ``type``,
            ``channel_idx``, etc.) — **not** the SDK ``Event`` object.

        Raises
        ------
        MeshCoreConnectionError
            If the SDK is not installed (non-fake mode) or the connection
            fails.
        """
        if self._started:
            return

        self._message_callback = message_callback
        self._stop_requested = False

        if self._config.connection_type == "fake":
            # Fake mode: no real SDK client needed.
            self._diag.connected = True
        else:
            await self._connect_real()

        self._started = True
        self._logger.info(
            "MeshCoreSession %s started (mode=%s, connected=%s)",
            self._adapter_id,
            self._config.connection_type,
            self._diag.connected,
        )

    async def stop(self) -> None:
        """Disconnect from MeshCore and release all resources.

        Sets ``_stop_requested`` to prevent reconnect loops.
        Idempotent — safe to call multiple times.
        """
        if not self._started:
            return

        self._stop_requested = True

        # Cancel reconnect task if running.
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        # Unsubscribe all callbacks.
        await self._unsubscribe_all()

        # Disconnect SDK client.
        if self._meshcore is not None:
            try:
                await self._meshcore.disconnect()
            except Exception as exc:
                self._logger.warning(
                    "MeshCoreSession %s: error during disconnect: %s",
                    self._adapter_id,
                    exc,
                )
            self._meshcore = None

        self._diag.connected = False
        self._diag.reconnecting = False
        self._started = False
        self._logger.info(
            "MeshCoreSession %s stopped", self._adapter_id
        )

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_text(
        self,
        contact_id: str,
        text: str,
        *,
        channel_index: int | None = None,
    ) -> str | None:
        """Send a text message via the MeshCore SDK.

        Parameters
        ----------
        contact_id:
            Destination identifier.  For DMs this is a pubkey prefix
            (hex string).  Ignored when *channel_index* is provided
            (channel message).
        text:
            Message body.
        channel_index:
            If provided, sends a channel message on this index instead
            of a DM.

        Returns
        -------
        str | None
            A native message ID if available, else ``None``.

        Raises
        ------
        MeshCoreSendError
            On permanent failure or after exhausting retries.
        """
        if not self._diag.connected:
            raise MeshCoreSendError("Session is not connected", transient=False)

        if self._config.connection_type == "fake":
            # Fake mode — no real send.
            return None

        return await self._send_real(contact_id, text, channel_index=channel_index)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return a safe diagnostics snapshot.

        No secrets, private keys, or raw SDK internals are exposed.
        """
        return {
            "connected": self._diag.connected,
            "reconnecting": self._diag.reconnecting,
            "reconnect_attempts": self._diag.reconnect_attempts,
            "last_message_time": (
                self._diag.last_message_time.isoformat()
                if self._diag.last_message_time
                else None
            ),
            "last_error": self._diag.last_error,
            "transient_delivery_failures": self._diag.transient_delivery_failures,
            "permanent_delivery_failures": self._diag.permanent_delivery_failures,
            "peer_count": self._diag.peer_count,
            "mode": self._config.connection_type,
        }

    # ==================================================================
    # Private — real connection
    # ==================================================================

    async def _connect_real(self) -> None:
        """Create a real SDK client, connect, and subscribe to events."""
        if not HAS_MESHCORE:
            raise MeshCoreConnectionError(
                "meshcore SDK not installed; pip install 'medre[meshcore]' "
                "or use connection_type='fake'"
            )

        # Deferred import — the SDK is only touched inside this method.
        mc = cast(_MeshCoreModule, importlib.import_module("meshcore"))

        try:
            if self._config.connection_type == "tcp":
                self._meshcore = await mc.MeshCore.create_tcp(
                    self._config.host or "localhost",
                    self._config.port or 4403,
                )
                if self._meshcore is None:
                    raise MeshCoreConnectionError(
                        "No response from MeshCore node (TCP)"
                    )
            elif self._config.connection_type == "serial":
                self._meshcore = await mc.MeshCore.create_serial(
                    self._config.serial_port or "/dev/ttyUSB0",
                    self._config.serial_baudrate,
                )
                if self._meshcore is None:
                    raise MeshCoreConnectionError(
                        "No response from MeshCore node (serial)"
                    )
            elif self._config.connection_type == "ble":
                self._meshcore = await mc.MeshCore.create_ble(
                    address=self._config.ble_address or "",
                )
                if self._meshcore is None:
                    raise MeshCoreConnectionError(
                        "No response from MeshCore node (BLE)"
                    )
            else:
                raise MeshCoreConnectionError(
                    f"Unsupported connection_type: "
                    f"{self._config.connection_type!r}"
                )

        except MeshCoreConnectionError:
            # Clean up partially-initialised SDK client on failure.
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception:
                    pass
                self._meshcore = None
            raise
        except Exception as exc:
            # Clean up partially-initialised SDK client on failure.
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception:
                    pass
                self._meshcore = None
            self._diag.last_error = str(exc)
            raise MeshCoreConnectionError(
                f"Failed to connect ({self._config.connection_type}): {exc}"
            ) from exc

        self._diag.connected = True
        self._diag.reconnect_attempts = 0

        # Subscribe to inbound events.
        self._subscribe_events(mc)

    def _subscribe_events(self, mc: Any) -> None:
        """Subscribe to SDK event types for inbound messages + disconnects."""
        if self._meshcore is None:
            return

        # Inbound messages (direct).
        sub_dm = self._meshcore.subscribe(
            mc.EventType.CONTACT_MSG_RECV,
            self._on_sdk_event,
        )
        self._subscriptions.append(sub_dm)

        # Inbound messages (channel).
        sub_chan = self._meshcore.subscribe(
            mc.EventType.CHANNEL_MSG_RECV,
            self._on_sdk_event,
        )
        self._subscriptions.append(sub_chan)

        # Disconnect detection.
        sub_disc = self._meshcore.subscribe(
            mc.EventType.DISCONNECTED,
            self._on_disconnect_event,
        )
        self._subscriptions.append(sub_disc)

    async def _unsubscribe_all(self) -> None:
        """Unsubscribe all registered callbacks."""
        if self._meshcore is not None:
            for sub in self._subscriptions:
                try:
                    self._meshcore.unsubscribe(sub)
                except Exception:
                    pass
        self._subscriptions.clear()

    # ------------------------------------------------------------------
    # SDK event handlers
    # ------------------------------------------------------------------

    async def _on_sdk_event(self, event: Any) -> None:
        """Handle an inbound SDK event.

        Extracts the payload dict from the SDK ``Event`` object and
        forwards it to the registered message callback.  The payload
        is a plain dict — no SDK objects leak into the adapter layer.
        """
        if self._message_callback is None:
            return

        try:
            # SDK Event has .payload (dict) and .type (EventType).
            payload: dict[str, Any]
            if isinstance(event, dict):
                payload = event
            elif hasattr(event, "payload"):
                payload = dict(event.payload) if isinstance(event.payload, dict) else {}
            else:
                payload = {}

            self._diag.last_message_time = datetime.now(timezone.utc)
            await self._message_callback(payload)
        except Exception as exc:
            self._logger.exception(
                "MeshCoreSession %s: error processing inbound event: %s",
                self._adapter_id,
                exc,
            )

    async def _on_disconnect_event(self, event: Any) -> None:
        """Handle SDK disconnect event — trigger reconnect if appropriate."""
        self._diag.connected = False
        self._logger.warning(
            "MeshCoreSession %s: SDK disconnected", self._adapter_id
        )

        if self._stop_requested:
            return

        # Start reconnect loop in background if not already running.
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

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
                    _RECONNECT_BASE_DELAY * (2 ** self._diag.reconnect_attempts),
                    _RECONNECT_MAX_DELAY,
                )
                # Apply jitter.
                jitter = delay * _RECONNECT_JITTER_FRACTION
                delay = delay + random.uniform(-jitter, jitter)
                delay = max(0.0, delay)

                self._logger.info(
                    "MeshCoreSession %s: reconnect attempt %d/%d in %.1fs",
                    self._adapter_id,
                    self._diag.reconnect_attempts + 1,
                    _RECONNECT_MAX_ATTEMPTS,
                    delay,
                )

                await asyncio.sleep(delay)

                if self._stop_requested:
                    break

                try:
                    await self._connect_real()
                    self._logger.info(
                        "MeshCoreSession %s: reconnected successfully",
                        self._adapter_id,
                    )
                    self._diag.reconnecting = False
                    return
                except Exception as exc:
                    self._diag.reconnect_attempts += 1
                    self._diag.last_error = str(exc)
                    self._logger.warning(
                        "MeshCoreSession %s: reconnect failed (attempt %d): %s",
                        self._adapter_id,
                        self._diag.reconnect_attempts,
                        exc,
                    )

            # Exhausted attempts.
            self._diag.last_error = (
                f"Reconnect exhausted after {self._diag.reconnect_attempts} attempts"
            )
            self._diag.reconnecting = False
            self._logger.error(
                "MeshCoreSession %s: %s", self._adapter_id, self._diag.last_error
            )
        except asyncio.CancelledError:
            self._diag.reconnecting = False
            raise

    # ------------------------------------------------------------------
    # Real outbound
    # ------------------------------------------------------------------

    async def _send_real(
        self,
        contact_id: str,
        text: str,
        *,
        channel_index: int | None = None,
    ) -> str | None:
        """Send via real SDK with bounded retry.

        .. warning::

           **Duplicate-send risk.**  Retries may cause the same message
           to be delivered multiple times if the node received it but
           the ACK was lost.
        """
        if self._meshcore is None:
            raise MeshCoreSendError("SDK client not initialised", transient=False)

        last_exc: Exception | None = None
        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                if channel_index is not None:
                    result = await self._meshcore.commands.send_chan_msg(
                        channel_index, text
                    )
                else:
                    result = await self._meshcore.commands.send_msg(
                        contact_id, text
                    )

                # Check for SDK-level error.
                if hasattr(result, "is_error") and result.is_error():
                    reason = (
                        result.payload.get("reason", "unknown")
                        if isinstance(result.payload, dict)
                        else "unknown"
                    )
                    self._diag.permanent_delivery_failures += 1
                    raise MeshCoreSendError(
                        f"SDK send error: {reason}",
                        transient=False,
                    )

                # Extract native message ID if available.
                native_id: str | None = None
                if isinstance(result, dict):
                    native_id = result.get("message_id")
                elif hasattr(result, "payload") and isinstance(result.payload, dict):
                    native_id = result.payload.get("message_id")
                elif hasattr(result, "attributes") and isinstance(result.attributes, dict):
                    native_id = result.attributes.get("message_id")

                return str(native_id) if native_id is not None else None

            except MeshCoreSendError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                self._diag.transient_delivery_failures += 1
                self._logger.warning(
                    "MeshCoreSession %s: send attempt %d/%d failed: %s",
                    self._adapter_id,
                    attempt,
                    _SEND_MAX_RETRIES,
                    exc,
                )
                if attempt < _SEND_MAX_RETRIES:
                    await asyncio.sleep(0.1 * attempt)

        self._diag.permanent_delivery_failures += 1
        raise MeshCoreSendError(
            f"Send failed after {_SEND_MAX_RETRIES} attempts: {last_exc}"
        ) from last_exc
