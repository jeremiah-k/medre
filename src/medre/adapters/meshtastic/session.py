"""Meshtastic session lifecycle boundary.

:class:`MeshtasticSession` owns the raw Meshtastic transport lifecycle:
client construction, connection establishment, inbound-packet callback
registration, bounded reconnection, and graceful teardown.

The adapter delegates all client ownership to this session object.
The session owns raw transport; the adapter owns semantic conversion.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.errors import (
    MeshtasticConnectionError,
    MeshtasticSendError,
)
from medre.config.adapters.meshtastic import MeshtasticConfig

_logger = logging.getLogger(__name__)

# Maximum consecutive reconnect attempts before giving up.
_MAX_RECONNECT_ATTEMPTS: int = 10

# Exponential backoff base and cap (seconds).
_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 30.0
_BACKOFF_JITTER_FRACTION: float = 0.25

# Maximum transient retry attempts for outbound send.
_MAX_SEND_RETRIES: int = 3


def _normalize_emoji_flag(emoji: object) -> int | None:
    """Validate and normalize an emoji flag for Meshtastic structured send.

    Returns ``1``, ``None``, or raises ``MeshtasticSendError``.
    """
    if emoji is None:
        return None
    if isinstance(emoji, bool):
        return 1 if emoji else None
    if isinstance(emoji, int):
        if emoji in (0, 1):
            return 1 if emoji == 1 else None
        raise MeshtasticSendError(
            f"invalid Meshtastic emoji flag for structured send: {emoji!r}",
            transient=False,
        )
    if isinstance(emoji, str):
        stripped = emoji.strip()
        if stripped in ("0", "1"):
            return 1 if stripped == "1" else None
    raise MeshtasticSendError(
        f"invalid Meshtastic emoji flag for structured send: {emoji!r}",
        transient=False,
    )


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
        Validated :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`.
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

    def get_node_info(self, node_id: str) -> dict[str, str] | None:
        """Look up a node's longname and shortname from the SDK client.

        Returns a plain dict with ``longname`` and ``shortname`` keys, or
        ``None`` when the node is unknown or the client is unavailable.

        Parameters
        ----------
        node_id:
            The Meshtastic node ID to look up (e.g. ``"!abcdef12"``).

        Returns
        -------
        dict[str, str] | None
            ``{"longname": ..., "shortname": ...}`` or ``None``.
        """
        if self._client is None:
            return None
        client_nodes = getattr(self._client, "nodes", None)
        if not isinstance(client_nodes, dict):
            return None
        node_info = client_nodes.get(node_id)
        if not isinstance(node_info, dict):
            return None
        user_info = node_info.get("user")
        if not isinstance(user_info, dict):
            return None
        longname = str(user_info.get("longName", "") or "")
        shortname = str(user_info.get("shortName", "") or "")
        if not longname and not shortname:
            return None
        result: dict[str, str] = {}
        if longname:
            result["longname"] = longname
        if shortname:
            result["shortname"] = shortname
        return result

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
        self._logger.info("MeshtasticSession %s stopped", self._adapter_id)

    # -- Outbound send --------------------------------------------------------

    async def send(
        self,
        packet_dict: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Send a text packet via the Meshtastic client with bounded retry.

        When *packet_dict* contains a ``reply_id`` key, the method uses
        the protobuf ``_sendPacket`` path to build a structured
        ``MeshPacket`` with ``reply_id`` and optional ``emoji`` fields.
        Missing protobuf modules or ``_sendPacket`` raise
        :class:`MeshtasticSendError` with ``transient=False``.

        When no ``reply_id`` is present the existing ``sendText`` path is
        used with bounded transient retry.

        Parameters
        ----------
        packet_dict:
            Dict with at least ``text`` and ``channel_index`` keys.
            May include ``reply_id`` (int) and ``emoji`` (int) for
            structured reply / reaction sends.

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
        reply_id = packet_dict.get("reply_id")
        emoji = packet_dict.get("emoji")

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_SEND_RETRIES + 1):
            try:
                if reply_id is not None:
                    result = await self._send_structured(
                        text, channel_index, reply_id, emoji
                    )
                else:
                    result = await asyncio.to_thread(
                        self._client.sendText,
                        text,
                        channelIndex=channel_index,
                    )
                return result
            except asyncio.CancelledError:
                raise
            except MeshtasticSendError as exc:
                if not exc.transient:
                    self._permanent_delivery_failures += 1
                    self._last_error = f"Permanent send failure: {exc}"
                    raise
                # Transient MeshtasticSendError — retry
                last_exc = exc
                self._transient_delivery_failures += 1
                self._last_error = f"Transient send failure (attempt {attempt}): {exc}"
                self._logger.warning(
                    "MeshtasticSession %s send failure " "(attempt %d/%d): %s",
                    self._adapter_id,
                    attempt,
                    _MAX_SEND_RETRIES,
                    exc,
                )
                if attempt < _MAX_SEND_RETRIES:
                    await asyncio.sleep(0.1 * attempt)
            except ConnectionError as exc:
                last_exc = exc
                self._transient_delivery_failures += 1
                self._last_error = f"Transient send failure (attempt {attempt}): {exc}"
                self._logger.warning(
                    "MeshtasticSession %s transient send failure "
                    "(attempt %d/%d): %s",
                    self._adapter_id,
                    attempt,
                    _MAX_SEND_RETRIES,
                    exc,
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
                    self._adapter_id,
                    attempt,
                    _MAX_SEND_RETRIES,
                    exc,
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
                    "MeshtasticSession %s send failure " "(attempt %d/%d): %s",
                    self._adapter_id,
                    attempt,
                    _MAX_SEND_RETRIES,
                    exc,
                )
                if attempt < _MAX_SEND_RETRIES:
                    await asyncio.sleep(0.1 * attempt)

        # All retries exhausted
        self._permanent_delivery_failures += 1
        self._last_error = f"Send failed after {_MAX_SEND_RETRIES} attempts: {last_exc}"
        raise MeshtasticSendError(
            f"Send failed after {_MAX_SEND_RETRIES} attempts: {last_exc}"
        ) from last_exc

    async def _send_structured(
        self,
        text: str,
        channel_index: int,
        reply_id: int,
        emoji: int | None,
    ) -> Any:
        """Send a structured message via protobuf ``_sendPacket``.

        Builds a ``MeshPacket`` with ``Data`` payload containing
        ``TEXT_MESSAGE_APP``, the text payload, ``reply_id``, and
        optional ``emoji=1``.

        Raises
        ------
        MeshtasticSendError
            With ``transient=False`` when protobuf modules or
            ``_sendPacket`` are unavailable.
        """
        if isinstance(reply_id, bool):
            raise MeshtasticSendError(
                f"invalid Meshtastic reply_id for structured send: {reply_id!r}",
                transient=False,
            )

        try:
            from meshtastic.protobuf import mesh_pb2, portnums_pb2
        except ImportError as exc:
            raise MeshtasticSendError(
                f"meshtastic protobuf modules not available: {exc}",
                transient=False,
            ) from exc

        _send_packet = getattr(self._client, "_sendPacket", None)
        if _send_packet is None:
            raise MeshtasticSendError(
                "client does not expose _sendPacket for structured send",
                transient=False,
            )

        text_portnum = getattr(portnums_pb2, "TEXT_MESSAGE_APP", None)
        if text_portnum is None:
            portnum_enum = getattr(portnums_pb2, "PortNum", None)
            text_portnum = getattr(portnum_enum, "TEXT_MESSAGE_APP", None)
        if text_portnum is None:
            raise MeshtasticSendError(
                "meshtastic TEXT_MESSAGE_APP protobuf enum is unavailable",
                transient=False,
            )

        try:
            reply_id_int = int(reply_id)
        except (TypeError, ValueError) as exc:
            raise MeshtasticSendError(
                f"invalid Meshtastic reply_id for structured send: {reply_id!r}",
                transient=False,
            ) from exc

        data = mesh_pb2.Data()
        data.portnum = text_portnum
        data.payload = text.encode("utf-8")
        try:
            data.reply_id = reply_id_int
        except AttributeError as exc:
            raise MeshtasticSendError(
                "structured Meshtastic send requires Data.reply_id support",
                transient=False,
            ) from exc
        emoji_flag = _normalize_emoji_flag(emoji)
        if emoji_flag == 1:
            try:
                data.emoji = 1
            except AttributeError as exc:
                raise MeshtasticSendError(
                    "structured Meshtastic reaction requires Data.emoji support",
                    transient=False,
                ) from exc

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.CopyFrom(data)
        mesh_packet.channel = channel_index
        generate_packet_id = getattr(self._client, "_generatePacketId", None)
        if callable(generate_packet_id):
            try:
                mesh_packet.id = cast(int, generate_packet_id())
            except Exception:
                pass

        send_kwargs: dict[str, Any] = {"wantAck": False}
        try:
            import meshtastic

            broadcast_addr = getattr(meshtastic, "BROADCAST_ADDR", None)
            signature = inspect.signature(_send_packet)
            accepts_destination = "destinationId" in signature.parameters or any(
                param.kind is inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            )
            if broadcast_addr is not None and accepts_destination:
                send_kwargs["destinationId"] = broadcast_addr
        except Exception:
            pass

        result = await asyncio.to_thread(
            _send_packet,
            mesh_packet,
            **send_kwargs,
        )
        if result is not None:
            return result
        # _sendPacket may return None even after successfully sending
        # (SDK mutates the packet, setting id via _generatePacketId).
        # Fall back to the mesh_packet so the caller can still extract
        # the packet ID via getattr(obj, "id", None).
        packet_id = getattr(mesh_packet, "id", None)
        if packet_id:
            return mesh_packet
        return None

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
                if self._config.host is None:
                    raise RuntimeError("config.host must be set for TCP connection")

                from meshtastic.tcp_interface import TCPInterface

                return TCPInterface(
                    hostname=self._config.host,
                    portNumber=(
                        self._config.port if self._config.port is not None else 4403
                    ),
                )
            elif conn == "serial":
                if self._config.serial_port is None:
                    raise RuntimeError(
                        "config.serial_port must be set for serial connection"
                    )

                from meshtastic.serial_interface import SerialInterface

                return SerialInterface(devPath=self._config.serial_port)
            elif conn == "ble":
                if self._config.ble_address is None:
                    raise RuntimeError(
                        "config.ble_address must be set for BLE connection"
                    )

                from meshtastic.ble_interface import (
                    BLEInterface,  # no py.typed / pyi stubs
                )

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

    def _on_receive(self, packet: dict[str, Any], interface: Any = None) -> None:
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
        self._logger.warning("MeshtasticSession %s connection lost", self._adapter_id)
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
                        f"Max reconnect attempts ({_MAX_RECONNECT_ATTEMPTS}) " "reached"
                    )
                    self._reconnecting = False
                    return

                # Compute backoff with jitter
                delay = min(
                    _BACKOFF_BASE * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_CAP,
                )
                jitter = delay * _BACKOFF_JITTER_FRACTION
                actual_delay = max(0.0, delay + random.uniform(-jitter, jitter))

                self._logger.warning(
                    "MeshtasticSession %s reconnect attempt %d/%d " "in %.1fs",
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
                        "MeshtasticSession %s reconnect attempt %d " "failed: %s",
                        self._adapter_id,
                        self._reconnect_attempts,
                        exc,
                    )
                    # Continue loop for next attempt
        except asyncio.CancelledError:
            pass
        finally:
            self._reconnecting = False
