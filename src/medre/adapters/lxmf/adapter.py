"""LXMF transport adapter for the MEDRE framework.

:class:`LxmfAdapter` connects to an LXMF router/node and bridges
inbound message payloads into the MEDRE canonical event stream and
outbound rendered payloads back to the mesh.

**Soft dependency**: all ``lxmf`` / ``RNS`` imports are guarded behind
:mod:`~medre.adapters.lxmf.compat`.  If the packages are not installed
the adapter raises :class:`~medre.adapters.lxmf.errors.LxmfConnectionError`
on :meth:`start` when using non-fake connection types.

Connection modes
----------------
The adapter supports connection types configured via
:class:`~medre.config.adapters.lxmf.LxmfConfig`:

``"fake"``
    No real client.  Used for testing without hardware.  Inbound
    simulation via :meth:`simulate_inbound`; outbound via :meth:`deliver`
    returns an :class:`AdapterDeliveryResult` with honest
    ``outbound``/``pending`` delivery semantics.

``"reticulum"``
    Connects to a locally-running Reticulum instance via the ``RNS``
    and ``lxmf`` packages.  Requires ``lxmf`` optional dependency at
    runtime.  Lifecycle is owned by
    :class:`~medre.adapters.lxmf.session.LxmfSession`.

Lifecycle
---------
:meth:`start` and :meth:`stop` are idempotent — calling them multiple
times is safe.  The adapter tracks background :class:`asyncio.Task`
instances spawned by inbound packet callbacks and drains them on stop.

The adapter delegates all SDK interaction to its owned
:class:`~medre.adapters.lxmf.session.LxmfSession` instance.  The
session owns raw transport; the adapter owns semantic conversion.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.compat import HAS_LXMF
from medre.adapters.lxmf.errors import (
    LxmfConnectionError,
    LxmfSendError,
)
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig
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
from medre.core.rendering.renderer import RenderingResult

# Capabilities for the LXMF transport adapter.
_LXMF_CAPABILITIES = AdapterCapabilities(
    text=True,
    title=True,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    delivery_receipts=False,
    store_and_forward=True,
    direct_messages=True,
    channels=False,
    async_delivery=True,
    identity_encryption=True,
    mesh_routing=True,
    max_text_bytes=None,
    max_text_chars=16384,
)

# Maximum entries in the inbound dedup OrderedDict (LRU eviction).
_DEDUP_MAX_SIZE = 1024


class LxmfAdapter(AdapterContract):
    """Transport adapter for LXMF routers/nodes.

    Connects to an LXMF router, receives message payloads, and publishes
    them as canonical events.  Outbound rendered payloads are enqueued
    for paced delivery.

    All SDK interaction is delegated to the owned
    :class:`~medre.adapters.lxmf.session.LxmfSession` instance.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.lxmf.LxmfConfig`.
    """

    adapter_id: str
    platform: str = "lxmf"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: LxmfConfig) -> None:
        super().__init__()
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = _LXMF_CAPABILITIES
        self._session = LxmfSession(
            config=config,
            adapter_id=config.adapter_id,
            platform=self.platform,
        )
        self._codec = LxmfCodec(config.adapter_id, config)
        self._classifier = LxmfPacketClassifier(config)
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()

        # Cached health string from last health_check() call.
        self._last_health: str | None = None

        # Inbound evidence counters.  LXMF does not expose packet classes
        # like Meshtastic, so these count normalized message decisions.
        self._classifier_messages_seen: int = 0
        self._classifier_messages_relayed: int = 0
        self._classifier_messages_ignored: int = 0
        self._classifier_messages_ack_ignored: int = 0
        self._classifier_messages_non_text_ignored: int = 0
        self._inbound_duplicates_suppressed: int = 0
        self._inbound_published: int = 0

        # Inbound dedup: keyed by (message_id, content).
        # Prevents duplicate events from Reticulum redelivery.
        # Including content ensures distinct payloads sharing the same
        # message_id are both processed, while exact replays are suppressed.
        # Bounded OrderedDict — least-recently-seen entries evicted when full.
        # Cleared on stop/start boundaries.
        self._inbound_dedup: OrderedDict[tuple[str, str], None] = OrderedDict()

    # -- Lifecycle ----------------------------------------------------------

    def _reset_inbound_evidence(self) -> None:
        """Reset per-session inbound counters and dedup state."""
        self._classifier_messages_seen = 0
        self._classifier_messages_relayed = 0
        self._classifier_messages_ignored = 0
        self._classifier_messages_ack_ignored = 0
        self._classifier_messages_non_text_ignored = 0
        self._inbound_duplicates_suppressed = 0
        self._inbound_published = 0
        self._inbound_dedup.clear()

    def _classification_allows_relay(self, classification: dict[str, Any]) -> bool:
        """Record a classifier decision and return whether it should relay."""
        self._classifier_messages_seen += 1
        if classification["category"] != "text":
            self._classifier_messages_ignored += 1
            self._classifier_messages_non_text_ignored += 1
            return False
        if classification["is_ack"]:
            self._classifier_messages_ignored += 1
            self._classifier_messages_ack_ignored += 1
            return False
        return True

    def _record_duplicate_suppressed(self) -> None:
        """Record an inbound duplicate suppressed before publish."""
        self._classifier_messages_ignored += 1
        self._inbound_duplicates_suppressed += 1

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the LXMF router/node and begin receiving events.

        Idempotent: calling start on an already-started adapter is a no-op.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        LxmfConnectionError
            If ``lxmf`` / ``RNS`` are not installed and connection_type
            is not ``"fake"``, or if the session cannot connect.
        """
        if self._started:
            return

        # Clear cached health at lifecycle boundary so diagnostics
        # never reports a stale health string from a previous session.
        self._last_health = None

        self._reset_inbound_evidence()
        self.ctx = ctx
        # NOTE: _mark_started is deferred until after session.start succeeds.
        # Early call would leak _start_time past a failed startup.

        if self._config.connection_type != "fake":
            if not HAS_LXMF:
                self.ctx = None
                self._start_time = None
                raise LxmfConnectionError(
                    "lxmf/RNS not installed; pip install 'medre[lxmf]'. "
                    f"connection_type={self._config.connection_type!r}"
                )

        try:
            await self._session.start(
                message_callback=self._on_packet,
            )
        except asyncio.CancelledError:
            # Best-effort cleanup of partially-started session.
            try:
                await asyncio.shield(self._session.stop(timeout=2.0))
            except Exception:
                pass
            self._session = None
            self._started = False
            self._start_time = None
            self.ctx = None
            raise
        except LxmfConnectionError:
            # Best-effort cleanup of partially-started session.
            try:
                await self._session.stop(timeout=5.0)
            except Exception:
                pass
            self._started = False
            self._start_time = None
            self.ctx = None
            raise
        except Exception as exc:
            # Best-effort cleanup of partially-started session.
            try:
                await self._session.stop(timeout=5.0)
            except Exception:
                pass
            self._started = False
            self._start_time = None
            self.ctx = None
            raise LxmfConnectionError(f"LXMF session failed to start: {exc}") from exc

        # Wire delivery state callback for terminal state notifications.
        self._session.set_delivery_state_callback(self._on_delivery_state)

        self._mark_started(ctx)
        self._started = True
        ctx.logger.info(
            "LxmfAdapter %s started (mode=%s)",
            self.adapter_id,
            self._config.connection_type,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the LXMF router/node.

        Idempotent: calling stop on an already-stopped adapter is a no-op.
        Cancels all tracked background tasks before shutting down.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            # Still clean up any lingering session from a failed/cancelled start.
            if self._session is not None:
                await self._session.stop(timeout=timeout)
                self._session = None
            return

        # Gate callbacks immediately — prevents race between drain completing
        # and session.stop() unsubscribing.
        self._started = False
        self._start_time = None

        # Clear cached health at lifecycle boundary.
        self._last_health = None

        # Cancel all tracked background tasks and drain them.
        await self._drain_background_tasks(timeout)

        # Stop the session (which tears down SDK objects).
        await self._session.stop(timeout=timeout)

        self._inbound_dedup.clear()
        if self.ctx is not None:
            self.ctx.logger.info("LxmfAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state with a health
            string of ``"healthy"``, ``"unknown"``, or ``"failed"``.
        """
        if self._started:
            health = "healthy"
        elif self._session is not None and self._session.connected and not self._started:
            health = "failed"
        else:
            health = "unknown"
        self._last_health = health
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities,
            health=health,
        )

    # -- Diagnostics --------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Return adapter-level diagnostics composed from session state.

        No secrets, private keys, identity material, or raw RNS/LXMF
        objects are exposed.  All values are JSON-safe primitives
        (bool, int, str, None) by construction — session properties
        are scalar and the normalisation boundary guarantees no raw
        SDK objects leak.
        """
        base: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "mode": self._config.connection_type,
            "health": self._last_health,
            "classifier_messages_seen": self._classifier_messages_seen,
            "classifier_messages_relayed": self._classifier_messages_relayed,
            "classifier_messages_ignored": self._classifier_messages_ignored,
            "classifier_messages_ack_ignored": self._classifier_messages_ack_ignored,
            "classifier_messages_non_text_ignored": (
                self._classifier_messages_non_text_ignored
            ),
            "inbound_duplicates_suppressed": self._inbound_duplicates_suppressed,
            "inbound_published": self._inbound_published,
        }
        if self._session is not None:
            session_diag = self._session.diagnostics()
            base["session"] = {
                "connected": session_diag.connected,
                "router_running": session_diag.router_running,
                "reconnecting": session_diag.reconnecting,
                "reconnect_attempts": session_diag.reconnect_attempts,
                "last_message_time": session_diag.last_message_time,
                "transient_delivery_failures": session_diag.transient_delivery_failures,
                "permanent_delivery_failures": session_diag.permanent_delivery_failures,
                "last_error": session_diag.last_error,
                "known_path_count": session_diag.known_path_count,
                "propagation_enabled": session_diag.propagation_enabled,
                "pending_delivery_count": session_diag.pending_delivery_count,
                "mode": session_diag.mode,
                "announces_sent": session_diag.announces_sent,
                "announce_failures": session_diag.announce_failures,
                "last_announce_error": session_diag.last_announce_error,
            }
        return base

    # -- Background task management -----------------------------------------

    async def _drain_background_tasks(self, timeout: float = 5.0) -> None:
        """Cancel and await all tracked background tasks.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for tasks to finish after cancellation.
        """
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
        self._background_tasks.clear()

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Enqueue a pre-rendered payload for paced delivery.

        The *result.payload* is expected to be an LXMF-ready content
        dict already rendered by
        :class:`~medre.adapters.lxmf.renderer.LxmfRenderer`.

        In fake mode, returns an :class:`AdapterDeliveryResult` with a
        deterministic native_message_id and pending delivery state.

        In real mode, sends via the session's LXMF router and returns
        an honest result with the LXMF message hash and delivery state.

        **Honest delivery semantics**: the ``delivery_status`` is
        ``"sent"`` meaning the adapter handed the message to the
        LXMRouter (local acceptance).  This does **not** mean the
        message was confirmed delivered to the recipient.  LXMF
        delivery is asynchronous and multi-hop; the actual delivery
        state transitions are tracked per-message in the session and
        reflected in the ``metadata["lxmf"]["delivery_state"]`` field.

        Parameters
        ----------
        result:
            The rendered payload to deliver.

        Returns
        -------
        AdapterDeliveryResult | None
            Delivery result with native message ID and state metadata.

        Raises
        ------
        AdapterPermanentError
            If a permanent error occurs (invalid input type, adapter not
            started, invalid destination, not initialised).
        AdapterSendError
            If a transient error occurs (timeout, connection, transport).
            ``transient`` is ``True``.
        asyncio.CancelledError
            Propagates without swallowing task cancellation.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"LxmfAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Lifecycle/startup state missing — cannot be repaired by retry.
        if not self._started:
            raise AdapterPermanentError("Adapter not started")

        payload = result.payload
        if not isinstance(payload, dict):
            return None

        content = payload.get("content", "")
        title = payload.get("title", "")
        destination_hash = payload.get("destination_hash", "")
        delivery_method = payload.get("delivery_method")
        fields = payload.get("fields")

        if not content and not title:
            return None

        try:
            native_id, delivery_state = await self._session.send_text(
                destination_hash=str(destination_hash),
                content=str(content),
                title=str(title),
                delivery_method=(str(delivery_method) if delivery_method else None),
                fields=fields if isinstance(fields, dict) else None,
            )
        except asyncio.CancelledError:
            raise
        except LxmfSendError as exc:
            if exc.transient:
                raise AdapterSendError(str(exc), transient=True) from exc
            else:
                raise AdapterPermanentError(str(exc)) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise AdapterSendError(str(exc), transient=True) from exc

        resolved_delivery_method = (
            str(delivery_method) if delivery_method else None
        ) or self._config.default_delivery_method

        return AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=str(destination_hash) if destination_hash else None,
            delivery_note="accepted by LXMRouter — async delivery pending",
            metadata=MappingProxyType(
                {
                    "lxmf": MappingProxyType(
                        {
                            "delivery_state": delivery_state.value,
                            "delivery_method": resolved_delivery_method,
                        }
                    ),
                }
            ),
        )

    # -- Inbound callback ---------------------------------------------------

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Process an inbound LXMF message payload.

        Receives normalised message dicts from the session (never raw
        LXMF/RNS objects).  Classifies the packet, decodes it via the
        codec, and publishes the resulting canonical event inbound.

        Parameters
        ----------
        packet:
            Normalised LXMF message payload dict.
        """
        if not self._started:
            return
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            if not self._classification_allows_relay(classification):
                return

            # Dedup: suppress exact duplicate messages by message_id + content.
            # OrderedDict bounded to _DEDUP_MAX_SIZE (LRU eviction):
            # least-recently-seen entries evicted when full.
            dedup_key: tuple[str, str] | None = None
            msg_id = packet.get("message_id")
            if msg_id is not None:
                dedup_key = (str(msg_id), str(packet.get("content", "")))
                if dedup_key in self._inbound_dedup:
                    self._inbound_dedup.move_to_end(dedup_key)
                    self._record_duplicate_suppressed()
                    return

            self._classifier_messages_relayed += 1

            # Decode before committing dedup key so that decode failures
            # do not suppress redelivery of the same packet.
            canonical = self._codec.decode(packet)

            # Commit dedup key only after successful decode.  The key
            # guards the async publish window against concurrent
            # duplicates.  If publish fails _on_packet_async rolls it
            # back so redelivery is not suppressed.
            if dedup_key is not None:
                self._inbound_dedup[dedup_key] = None
                if len(self._inbound_dedup) > _DEDUP_MAX_SIZE:
                    self._inbound_dedup.popitem(last=False)

            task = asyncio.create_task(self._on_packet_async(canonical, dedup_key))
            task.add_done_callback(self._background_tasks.discard)
            self._background_tasks.add(task)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "LxmfAdapter %s: error processing inbound packet",
                    self.adapter_id,
                )

    async def _on_packet_async(
        self,
        canonical: CanonicalEvent,
        dedup_key: tuple[str, str] | None = None,
    ) -> None:
        """Async handler for packets received via :meth:`_on_packet`.

        Publishes the canonical event and logs exceptions from the
        background task.

        Re-checks ``_started`` before publishing to close the race
        window where a task was scheduled by :meth:`_on_packet` but
        has not yet executed when :meth:`stop` sets ``_started = False``.

        Parameters
        ----------
        canonical:
            The decoded canonical event to publish.
        dedup_key:
            The dedup key committed by :meth:`_on_packet` after
            successful decode.  Rolled back on publish failure so that
            redelivery is not suppressed.
        """
        try:
            if self.ctx is not None and self._started:
                is_stale = self._is_stale_event(canonical)
                await self.publish_inbound(canonical)
                if not is_stale:
                    self._inbound_published += 1
        except Exception:
            # Roll back dedup key so redelivery is not suppressed.
            if dedup_key is not None:
                self._inbound_dedup.pop(dedup_key, None)
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "LxmfAdapter %s: error in background publish",
                    self.adapter_id,
                )

    def _on_delivery_state(self, message_hash: str, state: str) -> None:
        """Handle terminal delivery state notifications from the session.

        Invoked on the asyncio loop when an outbound delivery reaches a
        terminal state (``delivered``, ``failed``, ``rejected``, or
        ``cancelled``).  Session-local observability only — logs the
        state transition for diagnostics.  Does not append durable MEDRE
        delivery receipts or update outbox lifecycle state.

        Parameters
        ----------
        message_hash:
            Hex-encoded LXMF message hash.
        state:
            Lowercase terminal state string.
        """
        if not self._started:
            return
        if self.ctx is not None:
            self.ctx.logger.info(
                "LxmfAdapter %s: delivery %s → %s",
                self.adapter_id,
                message_hash[:16],
                state,
            )

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound LXMF message payload for testing.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.

        Parameters
        ----------
        packet:
            Raw LXMF message payload dict.

        Raises
        ------
        RuntimeError
            If the adapter has not been started yet.
        """
        if self.ctx is None:
            raise RuntimeError(
                f"Adapter {self.adapter_id!r} has not been started; "
                f"call start() before simulate_inbound()"
            )

        # Lifecycle guard: refuse post-stop calls.  ctx is retained
        # after stop() but _started is cleared — a stale ctx must not
        # be sufficient to publish lifecycle-stale inbound messages.
        if not self._started:
            return

        classification = self._classifier.classify(packet)
        if not self._classification_allows_relay(classification):
            return

        # Dedup: suppress exact duplicate messages by message_id + content.
        dedup_key: tuple[str, str] | None = None
        msg_id = packet.get("message_id")
        if msg_id is not None:
            dedup_key = (str(msg_id), str(packet.get("content", "")))
            if dedup_key in self._inbound_dedup:
                self._inbound_dedup.move_to_end(dedup_key)
                self._record_duplicate_suppressed()
                return

        self._classifier_messages_relayed += 1

        # Decode and publish before committing dedup key so that
        # failures do not suppress redelivery.
        canonical = self._codec.decode(packet)
        is_stale = self._is_stale_event(canonical)
        await self.publish_inbound(canonical)
        if not is_stale:
            self._inbound_published += 1

        # Commit dedup key only after successful decode + publish.
        if dedup_key is not None:
            self._inbound_dedup[dedup_key] = None
            if len(self._inbound_dedup) > _DEDUP_MAX_SIZE:
                self._inbound_dedup.popitem(last=False)

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> LxmfCodec:
        """Return the adapter's codec.

        Returns
        -------
        LxmfCodec
            The codec instance.
        """
        return self._codec

    # -- Session access -----------------------------------------------------

    @property
    def session(self) -> LxmfSession:
        """The owned :class:`~medre.adapters.lxmf.session.LxmfSession`."""
        return self._session
