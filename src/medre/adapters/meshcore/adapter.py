"""MeshCore transport adapter for the MEDRE framework.

:class:`MeshCoreAdapter` connects to a MeshCore node and bridges
inbound event payloads into the MEDRE canonical event stream and outbound
rendered payloads back to the mesh.

**Soft dependency**: all ``meshcore`` imports are guarded behind
:mod:`~medre.adapters.meshcore.compat`.  If the SDK is not installed
the adapter raises :class:`~medre.adapters.meshcore.errors.MeshCoreConnectionError`
on :meth:`start` when using non-fake connection types.

Connection modes
----------------
The adapter supports four connection types configured via
:class:`~medre.config.adapters.meshcore.MeshCoreConfig`:

``"fake"``
    No real client.  Used for testing without hardware.  Inbound
    simulation via :meth:`simulate_inbound`; outbound via :meth:`deliver`
    returns ``None`` (for fake mode, real via session for
    production modes).

``"tcp"``
    Connects via TCP using the MeshCore SDK.

``"serial"``
    Connects via serial using the MeshCore SDK.

``"ble"``
    Connects via BLE using the MeshCore SDK (future).

All non-fake modes require the ``meshcore`` package.  Connection lifecycle
is delegated to :class:`~medre.adapters.meshcore.session.MeshCoreSession`,
which owns the SDK client instance and manages reconnection.

Lifecycle
---------
:meth:`start` and :meth:`stop` are idempotent — calling them multiple
times is safe.  The adapter tracks background :class:`asyncio.Task`
instances spawned by inbound packet callbacks and drains them on stop.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import OrderedDict
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent

from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.errors import (
    MeshCoreSendError,
)
from medre.adapters.meshcore.packet_classifier import (
    REASON_ACK,
    REASON_EMPTY_TEXT,
    REASON_UNKNOWN,
    ClassificationResult,
    MeshCorePacketClassifier,
)
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
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
from medre.core.supervision.diagnostic_contract import sanitize_diagnostic_mapping

# Base capabilities for the MeshCore transport adapter.
# max_text_bytes is overridden per-instance from config.
# max_text_chars is None because UTF-8 bytes are enforced, not characters.
_MESHCORE_CAPS_BASE = AdapterCapabilities(
    text=True,
    title=False,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=False,
    store_and_forward=False,
    # direct_messages=False: MEDRE does not initiate outbound DMs. Inbound
    # PRIV packets are still relayed (relay != DM initiation). See
    # packet_classifier.py for the relay-side note.
    direct_messages=False,
    channels=True,
    async_delivery=True,
    mesh_routing=True,
    max_text_bytes=512,
    max_text_chars=None,
)

# Maximum entries in the inbound dedup OrderedDict (LRU eviction).
_DEDUP_MAX_SIZE = 1024


class _HasClassifierCounters(Protocol):
    """Structural type for objects with aggregate classifier counter attributes.

    Used by :func:`increment_classifier_counters` so that both
    :class:`MeshCoreAdapter` and :class:`~medre.adapters.fakes.meshcore.FakeMeshCoreAdapter`
    satisfy the protocol without a shared base class.
    """

    _classifier_packets_seen: int
    _classifier_packets_relayed: int
    _classifier_packets_ignored: int
    _classifier_packets_dropped: int
    _classifier_packets_deferred: int
    _classifier_packets_ack_ignored: int
    _classifier_packets_empty_text_ignored: int
    _classifier_packets_unknown_deferred: int
    _classifier_packets_dm_relayed: int
    _classifier_packets_malformed: int
    _inbound_published: int


def increment_classifier_counters(
    adapter: _HasClassifierCounters,
    classification: ClassificationResult,
) -> None:
    """Increment aggregate classifier counters based on a ClassificationResult.

    Shared by :class:`MeshCoreAdapter` and
    :class:`~medre.adapters.fakes.meshcore.FakeMeshCoreAdapter` to avoid
    duplicating the counter-switch logic.

    Parameters
    ----------
    adapter:
        An object satisfying :class:`_HasClassifierCounters` — it must
        expose the ``_classifier_packets_*`` and ``_inbound_published``
        integer attributes.
    classification:
        A :class:`~medre.adapters.meshcore.packet_classifier.ClassificationResult`.
    """
    adapter._classifier_packets_seen += 1

    action = classification.action
    if action == "relay":
        adapter._classifier_packets_relayed += 1
    elif action == "ignore":
        adapter._classifier_packets_ignored += 1
    elif action == "drop":
        adapter._classifier_packets_dropped += 1
    elif action == "deferred":
        adapter._classifier_packets_deferred += 1

    # Sub-counters (reason/action specific)
    if classification.reason == REASON_ACK:
        adapter._classifier_packets_ack_ignored += 1
    elif classification.reason == REASON_EMPTY_TEXT:
        adapter._classifier_packets_empty_text_ignored += 1
    elif classification.reason == REASON_UNKNOWN:
        adapter._classifier_packets_unknown_deferred += 1
    elif (
        classification.action == "relay" and classification.category == "direct_message"
    ):
        adapter._classifier_packets_dm_relayed += 1
    elif classification.category == "malformed":
        adapter._classifier_packets_malformed += 1


class MeshCoreAdapter(AdapterContract):
    """Transport adapter for MeshCore nodes.

    Connects to a MeshCore node, receives event payloads, and publishes
    them as canonical events.  Outbound rendered payloads are delivered
    directly via the session for local acceptance.

    The adapter delegates SDK client lifecycle to a
    :class:`~medre.adapters.meshcore.session.MeshCoreSession` instance.
    The session owns the connection, subscriptions, reconnect loop, and
    send operations.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.meshcore.MeshCoreConfig`.
    """

    adapter_id: str
    platform: str = "meshcore"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, config: MeshCoreConfig) -> None:
        super().__init__()
        config.validate()
        self._config = config
        self.adapter_id = config.adapter_id
        self._capabilities = dataclasses.replace(
            _MESHCORE_CAPS_BASE,
            max_text_bytes=config.max_text_bytes,
            max_text_chars=None,
        )
        self._codec = MeshCoreCodec(config.adapter_id, config)
        self._classifier = MeshCorePacketClassifier(config)
        self.ctx: AdapterContext | None = None
        self._started: bool = False
        self._background_tasks: set[asyncio.Task] = set()

        # Aggregate in-memory classifier counters (reset on restart).
        self._classifier_packets_seen: int = 0
        self._classifier_packets_relayed: int = 0
        self._classifier_packets_ignored: int = 0
        self._classifier_packets_dropped: int = 0
        self._classifier_packets_deferred: int = 0
        self._classifier_packets_ack_ignored: int = 0
        self._classifier_packets_empty_text_ignored: int = 0
        self._classifier_packets_unknown_deferred: int = 0
        self._classifier_packets_dm_relayed: int = 0
        self._classifier_packets_malformed: int = 0
        self._inbound_published: int = 0

        # Inbound dedup: keyed by (pubkey_prefix, sender_timestamp, channel_idx, text).
        # Prevents duplicate events from SDK redelivery (e.g., reconnect replay).
        # Including text ensures distinct payloads sharing the same packet_id are
        # both processed, while exact replays of the same packet are suppressed.
        # Bounded OrderedDict — least-recently-seen entries evicted when full.
        # Cleared on stop/start boundaries.
        self._inbound_dedup: OrderedDict[tuple[str, int, int | None, str], None] = (
            OrderedDict()
        )

        # Session boundary — owns SDK lifecycle.
        self._session: MeshCoreSession | None = None

        # Cached health string from last health_check() call.
        self._last_health: str | None = None

    # -- Lifecycle ----------------------------------------------------------

    def _reset_inbound_counters(self) -> None:
        """Zero all aggregate in-memory classifier counters.

        Called from :meth:`start` so that a reused adapter instance
        begins with a clean slate on every (re)start.
        """
        self._classifier_packets_seen = 0
        self._classifier_packets_relayed = 0
        self._classifier_packets_ignored = 0
        self._classifier_packets_dropped = 0
        self._classifier_packets_deferred = 0
        self._classifier_packets_ack_ignored = 0
        self._classifier_packets_empty_text_ignored = 0
        self._classifier_packets_unknown_deferred = 0
        self._classifier_packets_dm_relayed = 0
        self._classifier_packets_malformed = 0
        self._inbound_published = 0
        self._inbound_dedup.clear()

    async def start(self, ctx: AdapterContext) -> None:
        """Connect to the MeshCore node and begin receiving events.

        Idempotent: calling start on an already-started adapter is a no-op.

        Parameters
        ----------
        ctx:
            Runtime context supplied by the framework.

        Raises
        ------
        MeshCoreConnectionError
            If ``meshcore`` SDK is not installed and connection_type is
            not ``"fake"``, or if the real connection fails.
        """
        if self._started:
            return

        self._reset_inbound_counters()

        self.ctx = ctx
        self._mark_started(ctx)

        # Create and start the session.
        self._session = MeshCoreSession(
            config=self._config,
            adapter_id=self.adapter_id,
            platform=self.platform,
            logger=ctx.logger,
        )
        await self._session.start(message_callback=self._on_message)

        self._started = True
        ctx.logger.info(
            "MeshCoreAdapter %s started (mode=%s)",
            self.adapter_id,
            self._config.connection_type,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Disconnect from the MeshCore node.

        Idempotent: calling stop on an already-stopped adapter is a no-op.
        Cancels all tracked background tasks and stops the session.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait for a clean shutdown.
        """
        if not self._started:
            return

        # Gate callbacks immediately — prevents race between drain completing
        # and session.stop() unsubscribing.
        self._started = False

        # Cancel all tracked background tasks and drain them.
        await self._drain_background_tasks(timeout)

        # Stop the session (handles SDK disconnect + cleanup).
        if self._session is not None:
            await self._session.stop()
            self._session = None

        self._inbound_dedup.clear()
        if self.ctx is not None:
            self.ctx.logger.info("MeshCoreAdapter %s stopped", self.adapter_id)

    async def health_check(self) -> AdapterInfo:
        """Return a snapshot of the adapter's current health.

        Returns
        -------
        AdapterInfo
            Metadata describing the adapter's state.

        Health states:
            - ``"healthy"`` — adapter started and session connected.
            - ``"degraded"`` — adapter started but session disconnected.
            - ``"unknown"`` — adapter not yet started.
        """
        if self._started:
            if self._session is not None and self._session.connected:
                health = "healthy"
            elif self._session is not None and self._session.reconnecting:
                health = "degraded"
            else:
                health = "degraded"
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

    # -- Outbound delivery --------------------------------------------------

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Deliver a pre-rendered payload via the session for local acceptance.

        The *result.payload* is expected to be a MeshCore-ready content
        dict already rendered by
        :class:`~medre.adapters.meshcore.renderer.MeshCoreRenderer`.

        For **fake mode** this returns ``None`` (no real delivery).

        For **real modes** the delivery is delegated to the session's
        :meth:`~MeshCoreSession.send_text` method.

        Parameters
        ----------
        result:
            The rendered payload to deliver.  Must be a
            :class:`RenderingResult`, **not** a :class:`CanonicalEvent`.

        Returns
        -------
        AdapterDeliveryResult | None
            ``None`` for fake mode; delivery result for real modes.

        Raises
        ------
        AdapterPermanentError
            If the session raises a non-transient error (not initialised,
            SDK rejection, invalid input type).
        AdapterSendError
            If a transient error occurs (timeout, connection, transport).
            ``transient`` is ``True``.
        asyncio.CancelledError
            Propagates without swallowing task cancellation.
        """
        if not isinstance(result, RenderingResult):
            raise AdapterPermanentError(
                f"MeshCoreAdapter.deliver() accepts RenderingResult only, "
                f"got {type(result).__name__}. Use simulate_inbound() for "
                f"the inbound path."
            )

        # Fake mode: no real delivery result.
        if self._config.connection_type == "fake":
            return None

        # Real mode: delegate to session.
        if self._session is None:
            raise AdapterPermanentError("Session not initialised")

        payload = result.payload
        if not isinstance(payload, dict):
            return None

        text = payload.get("text", "")
        channel_index = payload.get("channel_index")
        contact_id = str(payload.get("contact_id", ""))

        resolved_channel_index = (
            channel_index
            if isinstance(channel_index, int) and not isinstance(channel_index, bool)
            else None
        )

        try:
            native_id = await self._session.send_text(
                contact_id=contact_id,
                text=str(text),
                channel_index=resolved_channel_index,
            )
        except asyncio.CancelledError:
            raise
        except MeshCoreSendError as exc:
            if exc.transient:
                raise AdapterSendError(str(exc), transient=True) from exc
            else:
                raise AdapterPermanentError(str(exc)) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise AdapterSendError(str(exc), transient=True) from exc

        if native_id is None:
            return None

        if resolved_channel_index is not None:
            delivery_note = (
                "MeshCore: channel send local-accepted only (no ACK protocol)"
            )
        else:
            delivery_note = (
                "MeshCore: DM sent with expected_ack captured as native_id; "
                "delivery confirmation not tracked"
            )

        return AdapterDeliveryResult(
            native_message_id=native_id,
            native_channel_id=(
                str(resolved_channel_index)
                if resolved_channel_index is not None
                else None
            ),
            delivery_note=delivery_note,
            metadata=MappingProxyType(
                {
                    "meshcore": MappingProxyType({"local_acceptance": True}),
                }
            ),
        )

    # -- Inbound callback ---------------------------------------------------

    def _on_message(self, packet: dict[str, Any]) -> None:
        """Process an inbound MeshCore event payload from the session.

        Classifies the packet, decodes it via the codec, and publishes
        the resulting canonical event inbound.

        This is the **session callback** — it receives plain dicts from
        :class:`~medre.adapters.meshcore.session.MeshCoreSession`, never
        SDK objects.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict.
        """
        # Guard: reject callbacks that arrive after stop() clears _started.
        # This closes the race window between _drain_background_tasks
        # completing and _session.stop() unsubscribing callbacks.
        if not self._started:
            return
        if self.ctx is None:
            return

        try:
            classification = self._classifier.classify(packet)
            self._increment_classifier_counters(classification)

            # Gate: only relay action packets enter the codec pipeline
            if classification.action != "relay":
                return

            # Dedup: suppress exact duplicate packets by identity + content.
            # Text is included so distinct payloads with reused packet_id
            # are both processed while exact replays are suppressed.
            # OrderedDict bounded to _DEDUP_MAX_SIZE (LRU eviction).
            # When packet_id is None there is no reliable native identity,
            # so adapter-level dedup is skipped entirely.
            if classification.packet_id is not None:
                dedup_key = (
                    classification.sender_id or "",
                    classification.packet_id,
                    classification.channel_index,
                    str(packet.get("text", "")),
                )
                if dedup_key in self._inbound_dedup:
                    self._inbound_dedup.move_to_end(dedup_key)
                    return
                self._inbound_dedup[dedup_key] = None
                if len(self._inbound_dedup) > _DEDUP_MAX_SIZE:
                    self._inbound_dedup.popitem(last=False)

            canonical = self._codec.decode(packet)
            # Schedule the async publish — _on_message is synchronous
            # so we create a tracked task that is cleaned up on stop().
            task = asyncio.create_task(self._on_message_async(canonical))
            task.add_done_callback(self._background_tasks.discard)
            self._background_tasks.add(task)
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshCoreAdapter %s: error processing inbound packet",
                    self.adapter_id,
                )

    async def _on_message_async(self, canonical: CanonicalEvent) -> None:
        """Async handler for messages received via :meth:`_on_message`.

        Publishes the canonical event and logs exceptions from the
        background task.

        Re-checks ``_started`` before publishing to close the race
        window where a task was scheduled by :meth:`_on_message` but
        has not yet executed when :meth:`stop` sets ``_started = False``.

        Parameters
        ----------
        canonical:
            The decoded canonical event to publish.
        """
        try:
            if self.ctx is not None and self._started:
                await self.publish_inbound(canonical)
                self._inbound_published += 1
        except Exception:
            if self.ctx is not None:
                self.ctx.logger.exception(
                    "MeshCoreAdapter %s: error in background publish",
                    self.adapter_id,
                )

    async def simulate_inbound(self, packet: dict[str, Any]) -> None:
        """Simulate an inbound MeshCore event payload for testing.

        Classifies, decodes, and publishes the packet through the same
        path as a real inbound packet.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict.

        Raises
        ------
        RuntimeError
            If the adapter has not been started yet.
        """
        if self.ctx is None:
            raise RuntimeError(
                f"Adapter {self.adapter_id!r} has not been started; "
                "call start() before simulate_inbound()."
            )

        # Lifecycle guard: refuse post-stop calls.  ctx is retained
        # after stop() but _started is cleared — a stale ctx must not
        # be sufficient to publish lifecycle-stale inbound messages.
        if not self._started:
            return

        classification = self._classifier.classify(packet)
        self._increment_classifier_counters(classification)

        # Gate: only relay action packets enter the codec pipeline
        if classification.action != "relay":
            return

        # Dedup: suppress exact duplicate packets by identity + content.
        # When packet_id is None there is no reliable native identity,
        # so adapter-level dedup is skipped entirely.
        if classification.packet_id is not None:
            dedup_key = (
                classification.sender_id or "",
                classification.packet_id,
                classification.channel_index,
                str(packet.get("text", "")),
            )
            if dedup_key in self._inbound_dedup:
                self._inbound_dedup.move_to_end(dedup_key)
                return
            self._inbound_dedup[dedup_key] = None
            if len(self._inbound_dedup) > _DEDUP_MAX_SIZE:
                self._inbound_dedup.popitem(last=False)

        canonical = self._codec.decode(packet)
        await self.publish_inbound(canonical)
        self._inbound_published += 1

    # -- Diagnostics --------------------------------------------------------

    def _increment_classifier_counters(
        self, classification: ClassificationResult
    ) -> None:
        """Increment aggregate classifier counters based on a ClassificationResult.

        Delegates to :func:`increment_classifier_counters`.
        """
        increment_classifier_counters(self, classification)

    def diagnostics(self) -> dict[str, Any]:
        """Return adapter-level diagnostics composed from session state.

        No secrets, private keys, or raw SDK internals are exposed.
        All values are guaranteed to be JSON-safe primitives.
        """
        base: dict[str, Any] = {
            "adapter_id": self.adapter_id,
            "platform": self.platform,
            "started": self._started,
            "mode": self._config.connection_type,
            "health": self._last_health,
            "classifier_packets_seen": self._classifier_packets_seen,
            "classifier_packets_relayed": self._classifier_packets_relayed,
            "classifier_packets_ignored": self._classifier_packets_ignored,
            "classifier_packets_dropped": self._classifier_packets_dropped,
            "classifier_packets_deferred": self._classifier_packets_deferred,
            "classifier_packets_ack_ignored": self._classifier_packets_ack_ignored,
            "classifier_packets_empty_text_ignored": self._classifier_packets_empty_text_ignored,
            "classifier_packets_unknown_deferred": self._classifier_packets_unknown_deferred,
            "classifier_packets_dm_relayed": self._classifier_packets_dm_relayed,
            "classifier_packets_malformed": self._classifier_packets_malformed,
            "inbound_published": self._inbound_published,
        }
        if self._session is not None:
            base["session"] = sanitize_diagnostic_mapping(self._session.diagnostics())
        else:
            base["session"] = {
                "connected": False,
                "reconnecting": False,
                "reconnect_attempts": 0,
                "last_message_time": None,
                "last_error": None,
                "transient_delivery_failures": 0,
                "permanent_delivery_failures": 0,
                "device_name": None,
                "public_key_prefix": None,
                "radio_freq": None,
                "mode": self._config.connection_type,
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

    # -- Codec access -------------------------------------------------------

    def get_codec(self) -> MeshCoreCodec:
        """Return the adapter's codec.

        Returns
        -------
        MeshCoreCodec
            The codec instance.
        """
        return self._codec
