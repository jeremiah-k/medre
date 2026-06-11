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
:class:`~medre.config.adapters.meshcore.MeshCoreConfig`:

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
import contextlib
import importlib
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, cast

from medre.adapters.meshcore.compat import HAS_MESHCORE
from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
)
from medre.config.adapters.meshcore import MeshCoreConfig

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

# Timeout for external SDK lifecycle calls (auto-fetch start/stop).
_SDK_LIFECYCLE_TIMEOUT: float = 5.0  # seconds

# suggested_timeout clamping bounds (seconds).
# The firmware returns milliseconds; we convert to seconds and clamp.
# Floor: never sleep less than 0.5 s even if firmware suggests very short.
# Ceil: never sleep more than 30 s (matches reconnect max delay).
_SUGGESTED_TIMEOUT_FLOOR: float = 0.5  # seconds
_SUGGESTED_TIMEOUT_CEIL: float = 30.0  # seconds


def _retry_delay_contact_key(contact_id: str) -> str:
    """Normalize a contact_id for retry-delay cache lookups.

    Strips leading/trailing whitespace and lowercases.
    This prevents cached timeout hints from fragmenting when
    equivalent contact IDs are passed with case or whitespace
    differences (e.g. pubkey hex prefixes).
    """
    return contact_id.strip().lower()


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
    device_name: str | None = None
    public_key_prefix: str | None = None
    radio_freq: float | None = None
    # suggested_timeout observability.
    sdk_suggested_timeouts_used: int = 0
    # Contact/self-info observability (diagnostics only).
    known_contact_count: int = 0
    last_contact_update_time: datetime | None = None


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


def _extract_expected_ack(raw: Any) -> str | None:
    """Extract deterministic hex string from expected_ack bytes.

    Per meshcore_py, expected_ack is a 4-byte hash. Returns lowercase
    hex string, or None if raw is not 4 bytes (which would indicate
    an SDK API change).
    """
    if isinstance(raw, bytes):
        if len(raw) != 4:
            # Possible SDK API change (e.g., extended to 8 bytes in a
            # future meshcore_py version).  Surface a warning so this
            # doesn't silently break native_id extraction.
            logger = logging.getLogger(__name__)
            logger.warning(
                "MeshCore expected_ack length %d != 4 (SDK API change?); "
                "falling back to message_id or None",
                len(raw),
            )
            return None
        return raw.hex()
    return None


def _extract_suggested_timeout(source: Any) -> float | None:
    """Extract and clamp suggested_timeout from SDK send result.

    Per meshcore_py, ``suggested_timeout`` is a 4-byte little-endian
    unsigned int in the MSG_SENT payload (units: milliseconds).

    Returns the value in **seconds**, clamped to
    ``[_SUGGESTED_TIMEOUT_FLOOR, _SUGGESTED_TIMEOUT_CEIL]``.  Returns
    ``None`` when the value is absent or not a valid positive number.

    Parameters
    ----------
    source:
        A dict-like object that may contain ``"suggested_timeout"``.
    """
    if not isinstance(source, dict):
        return None
    raw = source.get("suggested_timeout")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float)):
        return None
    # The SDK returns milliseconds; convert to seconds.
    val = float(raw) / 1000.0
    if not math.isfinite(val) or val <= 0.0:
        return None
    return max(_SUGGESTED_TIMEOUT_FLOOR, min(val, _SUGGESTED_TIMEOUT_CEIL))


class MeshCoreSession:
    """Transport-owned session boundary wrapping a MeshCore SDK client.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.meshcore.MeshCoreConfig`.
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

        # Send serialization: ensures pacing sleeps are not bypassed when
        # multiple send_text() calls overlap via pipeline fan-out.
        # Created in __init__ (not start()) because MeshCoreSession does not
        # capture a running loop reference — asyncio.Lock() lazy-binds on
        # first use in Python 3.10+.
        self._send_lock = asyncio.Lock()

        # Per-contact cached SDK suggested_timeout for DM retry delays.
        # Keyed by contact_id so that each contact's timeout is tracked
        # independently.  Persisted across send_text() calls so that a
        # successful DM that captures a timeout can inform the retry delay
        # of a subsequent failing DM to the same contact.  Cleared on stop().
        self._contact_retry_delays: dict[str, float] = {}

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
            Sync or async callable invoked with a plain dict for each
            inbound message.  The dict contains native MeshCore payload
            fields (``text``, ``pubkey_prefix``, ``sender_timestamp``,
            ``type``, ``channel_idx``, etc.) — **not** the SDK ``Event``
            object.

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
            try:
                await self._connect_real()
            except Exception:
                # Connect failed — full cleanup so diagnostics and
                # late SDK events don't reference stale state.
                await self._cleanup_failed_start()
                raise

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
            # Stop auto message fetching if available.
            if hasattr(self._meshcore, "stop_auto_message_fetching"):
                try:
                    await asyncio.wait_for(
                        self._meshcore.stop_auto_message_fetching(),
                        timeout=_SDK_LIFECYCLE_TIMEOUT,
                    )
                except TimeoutError:
                    self._logger.warning(
                        "MeshCoreSession %s: timed out stopping auto_message_fetching",
                        self._adapter_id,
                    )
                except Exception as exc:
                    self._logger.debug(
                        "MeshCoreSession %s: error stopping auto_message_fetching: %s",
                        self._adapter_id,
                        exc,
                    )
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
        # Reset observability counters on stop so they don't leak across
        # lifecycles.  Contact/self-info data is per-session.
        self._diag.sdk_suggested_timeouts_used = 0
        self._diag.known_contact_count = 0
        self._diag.last_contact_update_time = None
        # Reset self-info diagnostics so stale values don't persist
        # across lifecycle boundaries.
        self._diag.device_name = None
        self._diag.public_key_prefix = None
        self._diag.radio_freq = None
        self._contact_retry_delays.clear()
        self._started = False
        self._logger.info("MeshCoreSession %s stopped", self._adapter_id)

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
            "device_name": self._diag.device_name,
            "public_key_prefix": self._diag.public_key_prefix,
            "radio_freq": self._diag.radio_freq,
            "mode": self._config.connection_type,
            "sdk_suggested_timeouts_used": self._diag.sdk_suggested_timeouts_used,
            "sdk_contact_timeout_count": len(self._contact_retry_delays),
            "known_contact_count": self._diag.known_contact_count,
            "last_contact_update_time": (
                self._diag.last_contact_update_time.isoformat()
                if self._diag.last_contact_update_time
                else None
            ),
        }

    # ==================================================================
    # Private — real connection
    # ==================================================================

    async def _cleanup_failed_start(self) -> None:
        """Full cleanup after a failed start().

        Clears all partial state so diagnostics and late SDK events
        cannot reference stale SDK objects or flags.  Preserves
        ``last_error`` for diagnostics.
        """
        self._message_callback = None
        self._diag.connected = False
        self._diag.reconnecting = False
        self._subscriptions.clear()
        self._contact_retry_delays.clear()
        if self._meshcore is not None:
            try:
                await self._meshcore.disconnect()
            except Exception as exc:
                self._logger.debug(
                    "MeshCoreSession %s: error during cleanup disconnect: %s",
                    self._adapter_id,
                    exc,
                )
            self._meshcore = None

    async def _cleanup_stale_sdk(self) -> None:
        """Best-effort cleanup of a stale SDK client before reconnect.

        Unsubscribes existing event subscriptions, disconnects the old
        client, and clears internal handles.  Failures are logged but
        do NOT prevent the subsequent reconnect from proceeding.

        Safe to call when ``_meshcore`` is ``None`` (no-op).
        Does NOT clear ``_message_callback`` or set ``_stop_requested``.
        """
        if self._meshcore is None:
            return

        # Unsubscribe all registered callbacks.
        for sub in list(self._subscriptions):
            try:
                self._meshcore.unsubscribe(sub)
            except Exception as exc:
                self._logger.debug(
                    "MeshCoreSession %s: error unsubscribing during "
                    "stale cleanup: %s",
                    self._adapter_id,
                    exc,
                )
        self._subscriptions.clear()

        # Stop auto message fetching if the stale client supports it.
        # Uses the same SDK lifecycle timeout policy as stop().
        if hasattr(self._meshcore, "stop_auto_message_fetching"):
            try:
                await asyncio.wait_for(
                    self._meshcore.stop_auto_message_fetching(),
                    timeout=_SDK_LIFECYCLE_TIMEOUT,
                )
            except TimeoutError:
                self._logger.warning(
                    "MeshCoreSession %s: timed out stopping "
                    "auto_message_fetching during stale cleanup",
                    self._adapter_id,
                )
            except Exception as exc:
                self._logger.debug(
                    "MeshCoreSession %s: error stopping "
                    "auto_message_fetching during stale cleanup: %s",
                    self._adapter_id,
                    exc,
                )

        # Disconnect the old client.
        try:
            await self._meshcore.disconnect()
        except Exception as exc:
            self._logger.debug(
                "MeshCoreSession %s: error disconnecting stale client: %s",
                self._adapter_id,
                exc,
            )
        self._meshcore = None

    async def _disconnect_stale_ble_client(self, address: str) -> None:
        """Best-effort stale BlueZ disconnect for *address*.

        After a BLE disconnect, BlueZ may retain a cached "connected"
        entry.  Subsequent ``BleakClient(address)`` calls then hit
        ``le-connection-abort-by-local`` because BlueZ tries to reuse
        the stale entry.

        Best-effort: if a ``BleakClient`` for *address* reports
        ``is_connected``, force a disconnect and let the adapter settle
        before creating a fresh one.

        IMPORTANT: we do NOT use ``async with BleakClient`` here
        because that would connect (and then disconnect), creating
        exactly the churn we want to avoid.
        Pattern adapted from mmrelay's ``_disconnect_ble_by_address()``.
        """
        try:
            from bleak import BleakClient  # type: ignore[import-untyped]

            stale = BleakClient(address, timeout=3.0)
            try:
                # Attempt disconnect unconditionally — if BlueZ has any
                # lingering state for this address, disconnect clears it.
                # On a client that never connected, disconnect is a no-op
                # or raises a harmless error.  We do NOT gate on
                # is_connected because that property is always False on
                # a client that was never .connect()-ed, making the old
                # check dead code.
                self._logger.debug(
                    "MeshCoreSession %s: best-effort stale BlueZ " "disconnect for %s",
                    self._adapter_id,
                    address,
                )
            finally:
                # Always attempt cleanup — BleakClient holds
                # D-Bus resources even when not connected.
                # This is the ONLY disconnect call; it handles both
                # clearing stale BlueZ state and releasing resources.
                with contextlib.suppress(Exception):
                    await stale.disconnect()
        except Exception:
            pass  # best-effort — proceed even if cleanup fails

    def _sanitize_ble_exc(self, exc: BaseException) -> str:
        """Return a safe string representation of *exc* for BLE paths.

        When ``ble_pin`` is configured, raw exception text may include the
        PIN in args or string representation.  This method replaces it with
        the exception class name and a redaction notice so that logs,
        diagnostics, and error messages never leak the PIN.
        """
        if self._config.ble_pin is not None:
            return f"{type(exc).__name__}: [details redacted]"
        return str(exc)

    async def _find_ble_device(self, address_or_name: str) -> object | None:
        """Pre-scan for a BLE device matching *address_or_name*.

        On some Linux BlueZ stacks, ``BleakClient(address)`` fails
        with ``le-connection-abort-by-local`` while passing a
        ``BLEDevice`` from a live scan succeeds.

        Returns the discovered device object, or ``None`` on failure.
        """
        try:
            from bleak import BleakScanner  # type: ignore[import-untyped]

            def _match(d: object, adv: object) -> bool:
                d_addr: str = getattr(d, "address", "")
                adv_name: str = getattr(adv, "local_name", "") or ""
                if d_addr and d_addr.lower() == address_or_name.lower():
                    return True
                if adv_name and address_or_name.lower() in adv_name.lower():
                    return True
                return False

            return await BleakScanner.find_device_by_filter(_match, timeout=5.0)
        except Exception:
            return None

    async def _create_ble_with_retries(
        self,
        meshcore_module: Any,
        address: str,
        ble_device: object | None,
        *,
        pin: str | None = None,
    ) -> Any:
        """Attempt ``MeshCore.create_ble()`` up to 3 times.

        Both exceptions and ``None`` returns from ``create_ble()`` are
        treated as retryable failures.  Between attempts the method
        sleeps 2 s and re-scans for the BLE device.

        Returns the connected MeshCore instance on success.
        Raises :class:`MeshCoreConnectionError` after all attempts
        are exhausted.
        """
        _max_attempts = 3
        _last_reason: str = "unknown"

        for attempt in range(1, _max_attempts + 1):
            try:
                _ble_kwargs: dict[str, Any] = {
                    "address": address,
                    "device": ble_device,
                }
                if pin is not None:
                    _ble_kwargs["pin"] = pin
                result = await meshcore_module.MeshCore.create_ble(
                    **_ble_kwargs,
                )
                if result is not None:
                    return result

                # create_ble returned None — treat as retryable.
                _last_reason = "create_ble returned None"
                self._logger.debug(
                    "MeshCoreSession %s: BLE connection attempt %d/%d "
                    "returned None; retrying in 2s",
                    self._adapter_id,
                    attempt,
                    _max_attempts,
                )
            except Exception as exc:
                _last_reason = self._sanitize_ble_exc(exc)
                if attempt == _max_attempts:
                    raise MeshCoreConnectionError(
                        f"BLE connection failed after {_max_attempts} "
                        f"attempt(s): {_last_reason}"
                    ) from exc
                self._logger.debug(
                    "MeshCoreSession %s: BLE connection attempt %d/%d "
                    "failed (%s); retrying in 2s",
                    self._adapter_id,
                    attempt,
                    _max_attempts,
                    self._sanitize_ble_exc(exc),
                )

            if attempt < _max_attempts:
                await asyncio.sleep(2.0)
                # Re-scan: the device may have stopped and restarted
                # advertising in the meantime.  Best-effort: if bleak
                # is unavailable (not installed), skip the re-scan and
                # proceed with ble_device as-is.
                try:
                    ble_device = await self._find_ble_device(address)
                except Exception:
                    ble_device = None

                # Best-effort stale BlueZ cleanup before retry so the
                # next create_ble() hits a clean adapter state.
                try:
                    await self._disconnect_stale_ble_client(address)
                except Exception:
                    pass  # best-effort — proceed with retry

        raise MeshCoreConnectionError(
            f"No response from MeshCore node after "
            f"{_max_attempts} BLE attempt(s): {_last_reason}"
        )

    async def _connect_real(self) -> None:
        """Create a real SDK client, connect, and subscribe to events."""
        if not HAS_MESHCORE:
            raise MeshCoreConnectionError(
                "meshcore SDK not installed; pip install 'medre[meshcore]' "
                "or use connection_type='fake'"
            )

        # Deferred import — the SDK is only touched inside this method.
        mc = cast(_MeshCoreModule, importlib.import_module("meshcore"))

        # Clean up any stale SDK client from a previous connection
        # (e.g. before reconnect).  On first connect _meshcore is None
        # so this is a no-op.
        await self._cleanup_stale_sdk()

        try:
            if self._config.connection_type == "tcp":
                self._meshcore = await mc.MeshCore.create_tcp(
                    self._config.host or "localhost",
                    self._config.port if self._config.port is not None else 4000,
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
                _addr = self._config.ble_address or ""

                # Guard: empty address matches any BLE device in the
                # substring filter, so fail fast instead.
                if not _addr:
                    raise MeshCoreConnectionError("BLE address not configured")

                # -- Stale BlueZ cleanup (reconnect safety) --
                await self._disconnect_stale_ble_client(_addr)

                # Let the BLE adapter settle before scanning/connecting.
                await asyncio.sleep(0.5)

                # Pre-scan for the BLE device to obtain a BLEDevice.
                ble_device = await self._find_ble_device(_addr)

                # -- BLE connection attempt with retry --
                self._meshcore = await self._create_ble_with_retries(
                    mc,
                    _addr,
                    ble_device,
                    pin=self._config.ble_pin,
                )
            else:
                raise MeshCoreConnectionError(
                    f"Unsupported connection_type: " f"{self._config.connection_type!r}"
                )

        except MeshCoreConnectionError:
            # Clean up partially-initialised SDK client on failure.
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception as disconnect_exc:
                    self._logger.debug(
                        "MeshCoreSession %s: error during cleanup disconnect: %s",
                        self._adapter_id,
                        disconnect_exc,
                    )
                self._meshcore = None
            raise
        except Exception as exc:
            # Avoid double-wrapping an existing MeshCoreConnectionError
            # (e.g. from _create_ble_with_retries) into a less useful
            # generic message.
            if isinstance(exc, MeshCoreConnectionError):
                if self._meshcore is not None:
                    try:
                        await self._meshcore.disconnect()
                    except Exception as disconnect_exc:
                        self._logger.debug(
                            "MeshCoreSession %s: error during cleanup disconnect: %s",
                            self._adapter_id,
                            disconnect_exc,
                        )
                    self._meshcore = None
                raise
            # Clean up partially-initialised SDK client on failure.
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception as disconnect_exc:
                    self._logger.debug(
                        "MeshCoreSession %s: error during cleanup disconnect: %s",
                        self._adapter_id,
                        disconnect_exc,
                    )
                self._meshcore = None
            _safe_reason = (
                self._sanitize_ble_exc(exc)
                if self._config.connection_type == "ble"
                else str(exc)
            )
            self._diag.last_error = _safe_reason
            raise MeshCoreConnectionError(
                f"Failed to connect ({self._config.connection_type}): "
                f"{_safe_reason}"
            ) from exc

        # Connection succeeded — now subscribe to events.
        # If subscription fails, clean up the client before propagating.
        if not self._diag.reconnecting:
            self._diag.reconnect_attempts = 0
        try:
            self._subscribe_events(mc)
        except Exception as exc:
            # Subscription failure after successful client creation.
            self._diag.last_error = str(exc)
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception as disconnect_exc:
                    self._logger.debug(
                        "MeshCoreSession %s: error during cleanup disconnect: %s",
                        self._adapter_id,
                        disconnect_exc,
                    )
                self._meshcore = None
            self._subscriptions.clear()
            raise MeshCoreConnectionError(
                f"Failed to subscribe to events: {exc}"
            ) from exc

        # Send APP_START so the firmware accepts further commands.
        # Per meshcore_py, this MUST be called after every successful
        # connect (and re-connect).
        try:
            appstart_result = await self._meshcore.commands.send_appstart()
            if hasattr(appstart_result, "is_error") and appstart_result.is_error():
                raise RuntimeError(
                    f"send_appstart rejected: {appstart_result.payload!r}"
                )
            # Capture self_info payload into diagnostics.
            self._capture_self_info(appstart_result)
        except Exception as exc:
            self._diag.last_error = str(exc)
            if self._meshcore is not None:
                try:
                    await self._meshcore.disconnect()
                except Exception as disconnect_exc:
                    self._logger.debug(
                        "MeshCoreSession %s: error during cleanup disconnect: %s",
                        self._adapter_id,
                        disconnect_exc,
                    )
                self._meshcore = None
            self._subscriptions.clear()
            raise MeshCoreConnectionError(f"send_appstart failed: {exc}") from exc

        # Start auto message fetching to drain buffered messages from the device.
        # Best-effort: failure is logged but does not prevent connection.
        try:
            if hasattr(self._meshcore, "start_auto_message_fetching"):
                await asyncio.wait_for(
                    self._meshcore.start_auto_message_fetching(),
                    timeout=_SDK_LIFECYCLE_TIMEOUT,
                )
        except TimeoutError:
            self._logger.warning(
                "MeshCoreSession %s: timed out starting auto_message_fetching",
                self._adapter_id,
            )
        except Exception as exc:
            self._logger.debug(
                "MeshCoreSession %s: auto_message_fetching failed (non-fatal): %s",
                self._adapter_id,
                exc,
            )

        # Only mark connected AFTER subscriptions + appstart succeed.
        self._diag.connected = True

    def _subscribe_events(self, mc: Any) -> None:
        """Subscribe to SDK event types for inbound messages + disconnects.

        Subscriptions:
          - ``CONTACT_MSG_RECV``: inbound DM messages
          - ``CHANNEL_MSG_RECV``: inbound channel messages
          - ``DISCONNECTED``: connection loss detection
          - ``CONTACTS``: contact list updates (diagnostics only)
          - ``SELF_INFO``: self-info updates (diagnostics only)

        Contact/self-info subscriptions are **diagnostics-only** — no
        topology canonical events are emitted, and contact lists are not
        stored in canonical metadata.  Only aggregate counts and
        timestamps are recorded.
        """
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

        # Contact list updates (diagnostics only).
        # Per meshcore_py, EventType.CONTACTS fires when the contact list
        # changes.  We record the count and timestamp for observability.
        if hasattr(mc.EventType, "CONTACTS"):
            sub_contacts = self._meshcore.subscribe(
                mc.EventType.CONTACTS,
                self._on_contacts_event,
            )
            self._subscriptions.append(sub_contacts)

        # Self-info updates (diagnostics only).
        # Per meshcore_py, EventType.SELF_INFO fires when the node's
        # own info changes.  We update device_name / public_key_prefix
        # for observability if the payload carries them.
        if hasattr(mc.EventType, "SELF_INFO"):
            sub_self = self._meshcore.subscribe(
                mc.EventType.SELF_INFO,
                self._on_self_info_event,
            )
            self._subscriptions.append(sub_self)

    def _capture_self_info(self, appstart_result: Any) -> None:
        """Extract device self_info from the send_appstart result payload.

        Per meshcore_py, send_appstart returns a result whose ``payload``
        dict contains fields like ``public_key``, ``name``, and radio
        parameters.  We extract safe diagnostic fields from it.
        """
        payload: dict[str, Any] | None = None
        if hasattr(appstart_result, "payload") and isinstance(
            appstart_result.payload, dict
        ):
            payload = appstart_result.payload
        elif isinstance(appstart_result, dict):
            payload = appstart_result

        # Reset to avoid stale values across reconnects when payload is partial.
        self._diag.device_name = None
        self._diag.public_key_prefix = None
        self._diag.radio_freq = None

        if payload is None:
            return

        # Device name.
        name = payload.get("name")
        if isinstance(name, str):
            self._diag.device_name = name

        # Public key prefix (first 6 hex bytes / 12 hex chars).
        pubkey = payload.get("public_key")
        if isinstance(pubkey, str) and len(pubkey) >= 12:
            self._diag.public_key_prefix = pubkey[:12].lower()
        elif isinstance(pubkey, bytes) and len(pubkey) >= 6:
            self._diag.public_key_prefix = pubkey[:6].hex()

        # Radio frequency (if present).
        freq = payload.get("freq")
        if freq is None:
            freq = payload.get("radio_freq")
        if isinstance(freq, (int, float)) and not isinstance(freq, bool):
            f = float(freq)
            if math.isfinite(f) and f > 0.0:
                self._diag.radio_freq = f

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
            result = self._message_callback(payload)
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(self._log_task_exception)
                # Yield once so short callbacks can complete within this
                # turn.  Long-running callbacks continue in the background
                # after the first internal await point.
                await asyncio.sleep(0)
        except Exception as exc:
            self._logger.exception(
                "MeshCoreSession %s: error processing inbound event: %s",
                self._adapter_id,
                exc,
            )

    def _log_task_exception(self, task: asyncio.Task) -> None:
        """Done callback for fire-and-forget tasks — logs exceptions
        to prevent 'Task exception was never retrieved'."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._logger.warning(
                "MeshCoreSession %s: unhandled exception in inbound callback task: %s",
                self._adapter_id,
                exc,
            )

    async def _on_disconnect_event(self, event: Any) -> None:
        """Handle SDK disconnect event — trigger reconnect if appropriate."""
        self._diag.connected = False
        self._logger.warning("MeshCoreSession %s: SDK disconnected", self._adapter_id)

        if self._stop_requested:
            return

        # Start reconnect loop in background if not already running.
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _on_contacts_event(self, event: Any) -> None:
        """Handle SDK CONTACTS event — diagnostics-only contact list update.

        Per meshcore_py, the CONTACTS event payload contains a list/dict
        of contacts.  We record the count and update timestamp for
        observability.  **No** topology canonical events are emitted,
        and contact lists are not stored in canonical metadata.
        """
        try:
            payload: dict[str, Any]
            if isinstance(event, dict):
                payload = event
            elif hasattr(event, "payload") and isinstance(event.payload, dict):
                payload = event.payload
            else:
                payload = {}

            # Extract contact count.  The SDK may return a list or a dict.
            contacts_raw = payload.get("contacts")
            if isinstance(contacts_raw, (list, tuple)):
                self._diag.known_contact_count = len(contacts_raw)
            elif isinstance(contacts_raw, dict):
                self._diag.known_contact_count = len(contacts_raw)
            # If neither, leave count unchanged.

            self._diag.last_contact_update_time = datetime.now(timezone.utc)
        except Exception as exc:
            self._logger.debug(
                "MeshCoreSession %s: error processing CONTACTS event: %s",
                self._adapter_id,
                exc,
            )

    async def _on_self_info_event(self, event: Any) -> None:
        """Handle SDK SELF_INFO event — diagnostics-only self-info update.

        Per meshcore_py, the SELF_INFO event payload contains fields like
        ``name``, ``public_key``, and radio parameters.  We delegate to
        :meth:`_capture_self_info` for safe extraction.
        """
        try:
            payload: dict[str, Any] | None = None
            if isinstance(event, dict):
                payload = event
            elif hasattr(event, "payload") and isinstance(event.payload, dict):
                payload = event.payload
            if payload is not None:
                self._capture_self_info(payload)
        except Exception as exc:
            self._logger.debug(
                "MeshCoreSession %s: error processing SELF_INFO event: %s",
                self._adapter_id,
                exc,
            )

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
                    _RECONNECT_BASE_DELAY * (2**self._diag.reconnect_attempts),
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
                    self._contact_retry_delays.clear()
                    self._logger.info(
                        "MeshCoreSession %s: reconnected successfully",
                        self._adapter_id,
                    )
                    self._diag.reconnecting = False
                    self._diag.reconnect_attempts = 0
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

        For DM sends, the SDK ``suggested_timeout`` (milliseconds) is
        extracted from the successful result and used as the retry delay
        for transient failures on subsequent attempts.  It is clamped to
        ``[_SUGGESTED_TIMEOUT_FLOOR, _SUGGESTED_TIMEOUT_CEIL]`` and
        converted to seconds.  Channel sends have no ACK identity and do
        not require or overclaim a suggested_timeout.
        """
        if self._meshcore is None:
            raise MeshCoreSendError("SDK client not initialised", transient=False)

        # Pacing + send are serialized so concurrent calls honour the
        # configured delay between transmissions.
        async with self._send_lock:
            # Pacing: sleep before the real send so consecutive sends are
            # spaced by message_delay_seconds.  The delay applies once per
            # send_text() call, not per retry attempt.
            if self._config.message_delay_seconds > 0:
                await asyncio.sleep(self._config.message_delay_seconds)

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

                    # Extract native message ID and suggested_timeout if available.
                    # Per meshcore_py, MSG_SENT returns {'expected_ack': bytes(4),
                    # 'suggested_timeout': int}. Channel sends return OK with no ID.
                    # Use expected_ack as the native_id for DMs (channel sends
                    # honestly have no canonical identity from the SDK).
                    native_id: str | None = None
                    timeout_extracted = False
                    if isinstance(result, dict):
                        raw_ack = result.get("expected_ack")
                        native_id = _extract_expected_ack(raw_ack)
                        if native_id is None:
                            # Defensive fallback for older SDKs.
                            raw_mid = result.get("message_id")
                            if isinstance(raw_mid, bytes):
                                native_id = raw_mid.hex()
                            elif raw_mid is not None:
                                native_id = str(raw_mid)
                        # Extract suggested_timeout for DM retry delay.
                        if channel_index is None:
                            st = _extract_suggested_timeout(result)
                            if st is not None:
                                self._contact_retry_delays[
                                    _retry_delay_contact_key(contact_id)
                                ] = st
                                self._diag.sdk_suggested_timeouts_used += 1
                                timeout_extracted = True
                    else:
                        payload = getattr(result, "payload", None)
                        if isinstance(payload, dict):
                            raw_ack = payload.get("expected_ack")
                            native_id = _extract_expected_ack(raw_ack)
                            if native_id is None:
                                raw_mid = payload.get("message_id")
                                if isinstance(raw_mid, bytes):
                                    native_id = raw_mid.hex()
                                elif raw_mid is not None:
                                    native_id = str(raw_mid)
                            # Extract suggested_timeout for DM retry delay.
                            if channel_index is None:
                                st = _extract_suggested_timeout(payload)
                                if st is not None:
                                    self._contact_retry_delays[
                                        _retry_delay_contact_key(contact_id)
                                    ] = st
                                    self._diag.sdk_suggested_timeouts_used += 1
                                    timeout_extracted = True
                        if native_id is None:
                            attrs = getattr(result, "attributes", None)
                            if isinstance(attrs, dict):
                                raw_ack = attrs.get("expected_ack")
                                native_id = _extract_expected_ack(raw_ack)
                                if native_id is None:
                                    raw_mid = attrs.get("message_id")
                                    if isinstance(raw_mid, bytes):
                                        native_id = raw_mid.hex()
                                    elif raw_mid is not None:
                                        native_id = str(raw_mid)

                        # Always try attributes for timeout, even if native_id
                        # was already found in payload.
                        if not timeout_extracted and channel_index is None:
                            attrs = getattr(result, "attributes", None)
                            if isinstance(attrs, dict):
                                st = _extract_suggested_timeout(attrs)
                                if st is not None:
                                    self._contact_retry_delays[
                                        _retry_delay_contact_key(contact_id)
                                    ] = st
                                    self._diag.sdk_suggested_timeouts_used += 1

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
                        # Use cached SDK suggested_timeout for DM retries
                        # when available, otherwise fall back to linear backoff.
                        # Only DM sends (channel_index is None) use cached delays.
                        if channel_index is None:
                            cached = self._contact_retry_delays.get(
                                _retry_delay_contact_key(contact_id)
                            )
                            if cached is not None:
                                await asyncio.sleep(cached)
                            else:
                                await asyncio.sleep(0.1 * attempt)
                        else:
                            await asyncio.sleep(0.1 * attempt)

            self._diag.permanent_delivery_failures += 1
            raise MeshCoreSendError(
                f"Send failed after {_SEND_MAX_RETRIES} attempts: {last_exc}"
            ) from last_exc
