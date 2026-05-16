"""Meshtastic session lifecycle boundary.

:class:`MeshtasticSession` owns the raw Meshtastic transport lifecycle:
client construction, connection establishment, inbound-packet callback
registration, bounded reconnection, and graceful teardown.

The adapter delegates all client ownership to this session object.
The session owns raw transport; the adapter owns semantic conversion.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshtastic.errors import (
    MeshtasticConnectionError,
    MeshtasticSendError,
)

_logger = logging.getLogger(__name__)

# Maximum consecutive reconnect attempts before giving up.
_MAX_RECONNECT_ATTEMPTS: int = 10

# Exponential backoff base and cap (seconds).
_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 30.0
_BACKOFF_JITTER_FRACTION: float = 0.25

# Maximum transient retry attempts for outbound send.
_MAX_SEND_RETRIES: int = 3


@dataclass(frozen=True)
class MeshtasticSessionDiagnostics:
    """Read-only snapshot of session operational state.

    No secrets, private keys, raw protobuf dumps, or sensitive radio
    identifiers beyond what is public.
    """

    connected: bool
    reconnecting: bool
    reconnect_attempts: int
    last_packet_time: float | None
    node_id: str | None
    channel_count: int
    transient_delivery_failures: int
    permanent_delivery_failures: int
    last_error: str | None


class MeshtasticSession:
    """Transport-owned session boundary for Meshtastic connections.

    Owns the raw client interface and manages its full lifecycle:
    creation, callback registration, inbound message forwarding,
    bounded reconnection, and graceful teardown.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.meshtastic.config.MeshtasticConfig`.
    adapter_id:
        The adapter identifier (for logging).
    platform:
        Platform name (always ``"meshtastic"``).
    logger:
        Optional :class:`logging.Logger`.  When ``None`` a module-level
        fallback logger is used.
    """

    __slots__ = (
        "__weakref__",
        "_config",
        "_adapter_id",
        "_platform",
        "_client",
        "_message_callback",
        "_logger",
        "_started",
        "_subscribed",
        "_stop_requested",
        # Reconnect state
        "_reconnecting",
        "_reconnect_attempts",
        "_reconnect_task",
        # Diagnostics
        "_last_packet_time",
        "_node_id",
        "_channel_count",
        "_transient_delivery_failures",
        "_permanent_delivery_failures",
        "_last_error",
    )

    def __init__(
        self,
        config: MeshtasticConfig,
        adapter_id: str,
        platform: str,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._adapter_id = adapter_id
        self._platform = platform
        self._client: Any = None
        self._message_callback: Callable[[dict[str, Any]], None] | None = None
        self._logger: logging.Logger = logger if logger is not None else _logger
        self._started: bool = False
        self._subscribed: bool = False
        self._stop_requested: bool = False
        # Reconnect state
        self._reconnecting: bool = False
        self._reconnect_attempts: int = 0
        self._reconnect_task: asyncio.Task | None = None
        # Diagnostics
        self._last_packet_time: float | None = None
        self._node_id: str | None = None
        self._channel_count: int = 0
        self._transient_delivery_failures: int = 0
        self._permanent_delivery_failures: int = 0
        self._last_error: str | None = None

    # -- Properties -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        """``True`` if the client is created and session is started."""
        return self._client is not None and self._started

    @property
    def reconnecting(self) -> bool:
        """``True`` when the session is in a reconnect backoff phase."""
        return self._reconnecting

    @property
    def reconnect_attempts(self) -> int:
        """Number of consecutive reconnect attempts in the current cycle."""
        return self._reconnect_attempts

    @property
    def last_packet_time(self) -> float | None:
        """Monotonic time of last received packet, or ``None``."""
        return self._last_packet_time

    @property
    def node_id(self) -> str | None:
        """Our node ID extracted from the interface, if available."""
        return self._node_id

    @property
    def channel_count(self) -> int:
        """Count of known channels, if available."""
        return self._channel_count

    @property
    def transient_delivery_failures(self) -> int:
        """Number of transient outbound send failures."""
        return self._transient_delivery_failures

    @property
    def permanent_delivery_failures(self) -> int:
        """Number of permanent outbound send failures."""
        return self._permanent_delivery_failures

    @property
    def last_error(self) -> str | None:
        """Description of the most recent error, if any."""
        return self._last_error

    @property
    def client(self) -> Any:
        """The underlying client interface, or ``None``."""
        return self._client

    # -- Lifecycle ------------------------------------------------------------

    async def start(
        self,
        message_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Create the Meshtastic client and begin receiving packets.

        Parameters
        ----------
        message_callback:
            Callback invoked with raw packet dicts for inbound messages.

        Raises
        ------
        MeshtasticConnectionError
            If ``mtjk`` is not installed and connection_type is not ``"fake"``,
            or if the connection fails.
        """
        if self._started:
            self._logger.warning(
                "MeshtasticSession.start() called while already running"
            )
            return

        self._stop_requested = False
        self._reconnect_attempts = 0
        self._reconnecting = False
        self._last_error = None
        self._message_callback = message_callback

        conn = self._config.connection_type

        if conn == "fake":
            self._client = None
        else:
            if not HAS_MESHTASTIC:
                raise MeshtasticConnectionError(
                    "mtjk not installed; pip install 'medre[meshtastic]'"
                )
            self._client = self._create_client()

            try:
                self._subscribe_callbacks()
            except Exception:
                self._subscribed = False
                try:
                    close_fn = getattr(self._client, "close", None)
                    if close_fn is not None:
                        close_fn()
                except Exception:
                    pass
                self._client = None
                raise

        self._started = True
        self._logger.info(
            "MeshtasticSession %s started (mode=%s)",
            self._adapter_id,
            conn,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the Meshtastic node.  Idempotent.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        # Signal stop to prevent reconnect loops
        self._stop_requested = True
        self._reconnecting = False
        # Track 3 — reset reconnect counter so diagnostics are truthful after stop
        self._reconnect_attempts = 0

        # Cancel reconnect task if running
        if self._reconnect_task is not None:
            if not self._reconnect_task.done():
                self._reconnect_task.cancel()
                try:
                    await asyncio.wait_for(self._reconnect_task, timeout=timeout)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            self._reconnect_task = None

        self._unsubscribe_callbacks()

        if self._client is not None:
            try:
                close_fn = getattr(self._client, "close", None)
                if close_fn is not None:
                    close_fn()
            except Exception:
                pass

        self._client = None
        self._started = False
        self._logger.info(
            "MeshtasticSession %s stopped", self._adapter_id
        )

    # -- Outbound send --------------------------------------------------------

    async def send(
        self,
        packet_dict: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Send a text packet via the Meshtastic client with bounded retry.

        Parameters
        ----------
        packet_dict:
            Dict with at least ``text`` and ``channel_index`` keys.

        Returns
        -------
        dict | None
            Result from the client's send method, or ``None`` in fake mode.

        Raises
        ------
        MeshtasticSendError
            On permanent send failure or after max transient retries.
        """
        if self._client is None:
            # Fake mode — no real send
            return None

        text = str(packet_dict.get("text", ""))
        channel_index = packet_dict.get("channel_index", 0)

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_SEND_RETRIES + 1):
            try:
                result = await asyncio.to_thread(
                    self._client.sendText,
                    text,
                    channelIndex=channel_index,
                )
                return result
            except asyncio.CancelledError:
                raise
            except ConnectionError as exc:
                last_exc = exc
                self._transient_delivery_failures += 1
                self._last_error = f"Transient send failure (attempt {attempt}): {exc}"
                self._logger.warning(
                    "MeshtasticSession %s transient send failure "
                    "(attempt %d/%d): %s",
                    self._adapter_id, attempt, _MAX_SEND_RETRIES, exc,
                )
                if attempt < _MAX_SEND_RETRIES:
                    await asyncio.sleep(0.1 * attempt)
            except OSError as exc:
                last_exc = exc
                self._transient_delivery_failures += 1
                self._last_error = f"Transient send failure (attempt {attempt}): {exc}"
                self._logger.warning(
                    "MeshtasticSession %s transient send failure "
                    "(attempt %d/%d): %s",
                    self._adapter_id, attempt, _MAX_SEND_RETRIES, exc,
                )
                if attempt < _MAX_SEND_RETRIES:
                    await asyncio.sleep(0.1 * attempt)
            except (ValueError, TypeError) as exc:
                # Non-transient: raise immediately
                self._permanent_delivery_failures += 1
                self._last_error = f"Permanent send failure: {exc}"
                raise MeshtasticSendError(
                    f"Permanent send failure: {exc}",
                    transient=False,
                ) from exc
            except Exception as exc:
                last_exc = exc
                self._transient_delivery_failures += 1
                self._last_error = f"Send failure (attempt {attempt}): {exc}"
                self._logger.warning(
                    "MeshtasticSession %s send failure "
                    "(attempt %d/%d): %s",
                    self._adapter_id, attempt, _MAX_SEND_RETRIES, exc,
                )
                if attempt < _MAX_SEND_RETRIES:
                    await asyncio.sleep(0.1 * attempt)

        # All retries exhausted
        self._permanent_delivery_failures += 1
        self._last_error = f"Send failed after {_MAX_SEND_RETRIES} attempts: {last_exc}"
        raise MeshtasticSendError(
            f"Send failed after {_MAX_SEND_RETRIES} attempts: {last_exc}"
        ) from last_exc

    # -- Diagnostics ----------------------------------------------------------

    def diagnostics(self) -> MeshtasticSessionDiagnostics:
        """Return a read-only snapshot of session state.

        Never exposes secrets, private keys, raw protobuf dumps, or
        sensitive radio identifiers beyond what is public.
        """
        return MeshtasticSessionDiagnostics(
            connected=self.connected,
            reconnecting=self._reconnecting,
            reconnect_attempts=self._reconnect_attempts,
            last_packet_time=self._last_packet_time,
            node_id=self._node_id,
            channel_count=self._channel_count,
            transient_delivery_failures=self._transient_delivery_failures,
            permanent_delivery_failures=self._permanent_delivery_failures,
            last_error=self._last_error,
        )

    # -- Client creation (protected, overridable for testing) -----------------

    def _create_client(self) -> Any:
        """Create a Meshtastic interface client based on config.

        Uses the real ``meshtastic`` library interfaces.

        Returns
        -------
        object
            A Meshtastic interface instance.

        Raises
        ------
        MeshtasticConnectionError
            If the client cannot be created.
        """
        try:
            conn = self._config.connection_type
            if conn == "tcp":
                from meshtastic.tcp_interface import TCPInterface

                assert self._config.host is not None  # validated by config
                return TCPInterface(
                    hostname=self._config.host,
                    portNumber=self._config.port
                    if self._config.port is not None
                    else 4403,
                )
            elif conn == "serial":
                from meshtastic.serial_interface import SerialInterface

                return SerialInterface(devPath=self._config.serial_port)
            elif conn == "ble":
                from meshtastic.ble_interface import BLEInterface  # type: ignore[attr-defined]

                assert self._config.ble_address is not None
                return BLEInterface(address=self._config.ble_address)
            else:
                raise MeshtasticConnectionError(
                    f"Unsupported connection_type: {conn!r}"
                )
        except MeshtasticConnectionError:
            raise
        except Exception as exc:
            raise MeshtasticConnectionError(
                f"Failed to create {self._config.connection_type} client: {exc}"
            ) from exc

    # -- Callback subscription ------------------------------------------------

    def _subscribe_callbacks(self) -> None:
        """Subscribe to Meshtastic pubsub callbacks for inbound packets.

        Raises
        ------
        MeshtasticConnectionError
            If callback registration fails.
        """
        try:
            from pubsub import pub

            pub.subscribe(self._on_receive, "meshtastic.receive")
        except Exception as exc:
            raise MeshtasticConnectionError(
                f"Failed to subscribe to meshtastic.receive: {exc}"
            ) from exc
        self._subscribed = True

    def _unsubscribe_callbacks(self) -> None:
        """Unsubscribe from Meshtastic pubsub callbacks."""
        if not self._subscribed:
            return
        try:
            from pubsub import pub

            pub.unsubscribe(self._on_receive, "meshtastic.receive")
        except Exception:
            pass
        self._subscribed = False

    def _on_receive(
        self, packet: dict[str, Any], interface: Any = None
    ) -> None:
        """Pubsub callback for inbound packets.

        Records diagnostics and forwards to the adapter's message callback.
        """
        self._last_packet_time = time.monotonic()
        if self._message_callback is not None:
            self._message_callback(packet)

    # -- Reconnection ---------------------------------------------------------

    def notify_connection_lost(self) -> None:
        """Called when a connection loss is detected.

        Starts the bounded reconnect loop if not already reconnecting
        and not stopping.
        """
        if self._stop_requested or self._reconnecting:
            return
        self._last_error = "Connection lost"
        self._logger.warning(
            "MeshtasticSession %s connection lost", self._adapter_id
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Bounded exponential backoff reconnect loop.

        Backoff: 1s, 2s, 4s, 8s, 16s capped at 30s, with +-25% jitter.
        Max 10 consecutive attempts.  On success, resets counters.
        On max attempts, sets final connection failure and stops retrying.
        """
        self._reconnecting = True
        self._reconnect_attempts = 0

        try:
            while not self._stop_requested:
                self._reconnect_attempts += 1

                if self._reconnect_attempts > _MAX_RECONNECT_ATTEMPTS:
                    self._logger.error(
                        "MeshtasticSession %s max reconnect attempts "
                        "(%d) reached, giving up",
                        self._adapter_id,
                        _MAX_RECONNECT_ATTEMPTS,
                    )
                    self._last_error = (
                        f"Max reconnect attempts ({_MAX_RECONNECT_ATTEMPTS}) "
                        "reached"
                    )
                    self._reconnecting = False
                    return

                # Compute backoff with jitter
                delay = min(
                    _BACKOFF_BASE
                    * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_CAP,
                )
                jitter = delay * _BACKOFF_JITTER_FRACTION
                actual_delay = max(
                    0.0, delay + random.uniform(-jitter, jitter)
                )

                self._logger.warning(
                    "MeshtasticSession %s reconnect attempt %d/%d "
                    "in %.1fs",
                    self._adapter_id,
                    self._reconnect_attempts,
                    _MAX_RECONNECT_ATTEMPTS,
                    actual_delay,
                )

                try:
                    await asyncio.sleep(actual_delay)
                except asyncio.CancelledError:
                    if self._stop_requested:
                        self._reconnecting = False
                        return
                    raise

                if self._stop_requested:
                    self._reconnecting = False
                    return

                # Attempt reconnect
                try:
                    # Close old client if present
                    if self._client is not None:
                        try:
                            close_fn = getattr(self._client, "close", None)
                            if close_fn is not None:
                                close_fn()
                        except Exception:
                            pass
                        self._client = None

                    self._unsubscribe_callbacks()
                    self._client = self._create_client()
                    self._subscribe_callbacks()

                    # Reconnect success
                    self._logger.info(
                        "MeshtasticSession %s reconnected after %d attempts",
                        self._adapter_id,
                        self._reconnect_attempts,
                    )
                    self._reconnect_attempts = 0
                    self._reconnecting = False
                    self._last_error = None
                    return
                except asyncio.CancelledError:
                    if self._stop_requested:
                        self._reconnecting = False
                        return
                    raise
                except Exception as exc:
                    self._last_error = f"Reconnect failed: {exc}"
                    self._logger.warning(
                        "MeshtasticSession %s reconnect attempt %d "
                        "failed: %s",
                        self._adapter_id,
                        self._reconnect_attempts,
                        exc,
                    )
                    # Continue loop for next attempt
        except asyncio.CancelledError:
            pass
        finally:
            self._reconnecting = False
