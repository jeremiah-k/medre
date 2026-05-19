"""Matrix session lifecycle boundary.

:class:`MatrixSession` owns the nio ``AsyncClient`` lifecycle: construction,
login restoration, event-callback registration, sync task management, and
graceful teardown.  The adapter delegates all client ownership to this
session object.

E2EE support: when ``HAS_E2EE`` is ``True`` the session enables crypto
via nio's built-in encryption.  When ``device_id`` is not explicitly
configured the session discovers it via ``whoami()`` after setting the
access token.  ``store_path`` is derived by the runtime builder under
the resolved state directory (``{state}/adapters/{adapter_id}/matrix/store``).
Operators do not need to set either field.  Decrypted inbound text
events pass through the normal message callback; undecryptable encrypted
events are counted and logged but not forwarded.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from medre.adapters.matrix import compat as _compat_mod
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix import MatrixConfig

_logger = logging.getLogger(__name__)

# Type alias for room encryption state tracking.
RoomEncryptionState = Literal["unknown", "encrypted", "plaintext"]

# Maximum consecutive sync failures before giving up.
_MAX_RECONNECT_ATTEMPTS: int = 10

# Exponential backoff base and cap (seconds).
_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP: float = 60.0
_BACKOFF_JITTER_FRACTION: float = 0.25

# Maximum number of rooms tracked in _room_states.
# Prevents unbounded growth if a compromised or misconfigured
# homeserver exposes an extreme number of rooms.
_MAX_ROOM_STATES: int = 10_000


def _reaction_event_classes(nio_module: Any) -> tuple[type, ...]:
    """Discover ReactionEvent class(es) across nio versions.

    Different nio versions expose ``ReactionEvent`` at different module
    locations (top-level, ``nio.events``, or ``nio.events.room_events``).
    This helper probes each location and returns a de-duplicated tuple
    of discovered classes.

    Returns an empty tuple when no ``ReactionEvent`` class is found
    anywhere.
    """
    candidates: list[type] = []
    # 1. Top-level nio.ReactionEvent
    cls = getattr(nio_module, "ReactionEvent", None)
    if cls is not None:
        candidates.append(cls)
    # 2. nio.events.ReactionEvent
    try:
        events_mod = getattr(nio_module, "events", None)
        if events_mod is not None:
            cls = getattr(events_mod, "ReactionEvent", None)
            if cls is not None:
                candidates.append(cls)
    except (ImportError, AttributeError):
        pass
    # 3. nio.events.room_events.ReactionEvent
    try:
        events_mod = getattr(nio_module, "events", None)
        if events_mod is not None:
            room_events_mod = getattr(events_mod, "room_events", None)
            if room_events_mod is not None:
                cls = getattr(room_events_mod, "ReactionEvent", None)
                if cls is not None:
                    candidates.append(cls)
    except (ImportError, AttributeError):
        pass
    # 4. importlib fallback — probe submodules that may not be
    #    populated via top-level getattr traversal.
    for import_path in ("nio.events", "nio.events.room_events"):
        try:
            mod = importlib.import_module(import_path)
        except Exception:
            continue
        cls = getattr(mod, "ReactionEvent", None)
        if cls is not None:
            candidates.append(cls)
    # De-duplicate while preserving order
    return tuple(dict.fromkeys(candidates))


@dataclass(frozen=True)
class MatrixSessionDiagnostics:
    """Read-only snapshot of session operational state.

    No secrets, access tokens, keys, or private device material are exposed.
    """

    connected: bool
    logged_in: bool
    sync_task_running: bool
    last_sync_error: Exception | None
    store_path_configured: bool
    device_id_configured: bool
    encryption_mode: str
    crypto_enabled: bool
    last_crypto_error: str | None
    encrypted_room_seen: bool
    undecryptable_event_count: int
    # Track 1 — sync recovery diagnostics
    sync_running: bool
    reconnecting: bool
    reconnect_attempts: int
    last_successful_sync: float | None
    # Track 2 — crypto-store continuity
    crypto_store_loaded: bool
    # Track 4 — room-state tracking counts (no room names/IDs)
    encrypted_room_count: int
    plaintext_room_count: int


class MatrixSession:
    """Adapter-owned Matrix session lifecycle boundary.

    Owns the ``nio.AsyncClient`` and manages its full lifecycle:
    creation, login restoration, callback registration, sync loop,
    and graceful teardown.

    Parameters
    ----------
    config:
        Validated :class:`~medre.config.adapters.matrix.MatrixConfig`.
    message_callback:
        Callback for inbound decrypted text events.
    logger:
        Optional :class:`logging.Logger`.  When ``None`` a module-level
        fallback logger is used.
    """

    __slots__ = (
        "_config",
        "_client",
        "_sync_task",
        "_sync_failure",
        "_message_callback",
        "_closed",
        "_logger",
        "_crypto_enabled",
        "_encrypted_room_seen",
        "_undecryptable_event_count",
        "_last_crypto_error",
        # Track 1 — sync recovery
        "_reconnect_attempts",
        "_reconnecting",
        "_last_reconnect_error",
        "_last_successful_sync",
        "_stop_requested",
        # Track 2 — crypto-store continuity
        "_crypto_store_loaded",
        # Track 4 — room-state tracking
        "_room_states",
        # Part D — auto-join
        "_auto_join_rooms",
        "_joining_rooms",
    )

    def __init__(
        self,
        config: MatrixConfig,
        message_callback: Callable[..., Any] | None = None,
        logger: logging.Logger | None = None,
        auto_join_rooms: tuple[str, ...] = (),
    ) -> None:
        self._config = config
        self._client: Any = None
        self._sync_task: asyncio.Task | None = None
        self._sync_failure: Exception | None = None
        self._message_callback = message_callback
        self._closed = False
        self._logger: logging.Logger = logger if logger is not None else _logger
        self._crypto_enabled: bool = False
        self._encrypted_room_seen: bool = False
        self._undecryptable_event_count: int = 0
        self._last_crypto_error: str | None = None
        # Track 1
        self._reconnect_attempts: int = 0
        self._reconnecting: bool = False
        self._last_reconnect_error: str | None = None
        self._last_successful_sync: float | None = None
        self._stop_requested: bool = False
        # Track 2
        self._crypto_store_loaded: bool = False
        # Track 4
        self._room_states: dict[str, RoomEncryptionState] = {}
        # Part D — auto-join
        self._auto_join_rooms = auto_join_rooms
        self._joining_rooms: set[str] = set()

    # -- Properties -----------------------------------------------------------

    @property
    def client(self) -> Any:
        """The underlying ``nio.AsyncClient``, or ``None`` if not started."""
        return self._client

    @property
    def closed(self) -> bool:
        """``True`` after :meth:`stop` has completed."""
        return self._closed

    @property
    def connected(self) -> bool:
        """``True`` if the client has been created and is still open."""
        return self._client is not None and not self._closed

    @property
    def logged_in(self) -> bool:
        """``True`` if the client reports ``logged_in``."""
        return getattr(self._client, "logged_in", False) if self._client else False

    @property
    def sync_task_running(self) -> bool:
        """``True`` if the sync task exists and is not done."""
        return self._sync_task is not None and not self._sync_task.done()

    @property
    def last_sync_error(self) -> Exception | None:
        """The last exception raised by the sync loop, if any."""
        return self._sync_failure

    @property
    def crypto_enabled(self) -> bool:
        """``True`` when E2EE crypto is active for this session."""
        return self._crypto_enabled

    @property
    def encrypted_room_seen(self) -> bool:
        """``True`` when at least one encrypted room/event has been seen."""
        return self._encrypted_room_seen

    @property
    def undecryptable_event_count(self) -> int:
        """Number of inbound MegolmEvents that could not be decrypted."""
        return self._undecryptable_event_count

    @property
    def last_crypto_error(self) -> str | None:
        """Description of the most recent crypto error, if any."""
        return self._last_crypto_error

    # Track 1 — sync recovery properties

    @property
    def sync_running(self) -> bool:
        """``True`` if the sync task exists and is not done."""
        return self.sync_task_running

    @property
    def reconnecting(self) -> bool:
        """``True`` when the session is in a reconnect backoff phase."""
        return self._reconnecting

    @property
    def reconnect_attempts(self) -> int:
        """Number of consecutive reconnect attempts in the current cycle."""
        return self._reconnect_attempts

    @property
    def last_successful_sync(self) -> float | None:
        """Monotonic time of last successful sync, or ``None``."""
        return self._last_successful_sync

    # Track 2 — crypto-store continuity

    @property
    def crypto_store_loaded(self) -> bool:
        """``True`` when the crypto store was loaded/initialized."""
        return self._crypto_store_loaded

    # Track 4 — room-state tracking

    def room_state(self, room_id: str) -> RoomEncryptionState:
        """Return the tracked encryption state for a room.

        Returns ``"unknown"`` for rooms not yet seen.
        """
        return self._room_states.get(room_id, "unknown")

    @property
    def encrypted_room_count(self) -> int:
        """Number of rooms tracked as encrypted (no room IDs exposed)."""
        return sum(1 for s in self._room_states.values() if s == "encrypted")

    @property
    def plaintext_room_count(self) -> int:
        """Number of rooms tracked as plaintext (no room IDs exposed)."""
        return sum(1 for s in self._room_states.values() if s == "plaintext")

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Create the nio client, restore login, register callbacks, start sync.

        E2EE startup depends on ``encryption_mode``:

        * ``plaintext`` — standard client, no crypto.
        * ``e2ee_required`` — asserts ``HAS_E2EE`` and enables encryption.
          ``store_path`` is derived by the runtime builder under the
          resolved state directory; ``device_id`` is discovered via
          ``whoami()`` when not set.
        * ``e2ee_optional`` — enables crypto when deps are present;
          falls back to plaintext otherwise.

        Raises
        ------
        MatrixConnectionError
            If the client cannot authenticate, E2EE prerequisites are
            unmet in ``e2ee_required`` mode, or the sync task cannot
            be created.
        """
        # Track 3 — guard against double-start
        if self._client is not None and not self._closed:
            self._logger.warning("MatrixSession.start() called while already running")
            return

        self._sync_failure = None
        self._closed = False
        self._crypto_enabled = False
        self._encrypted_room_seen = False
        self._undecryptable_event_count = 0
        self._last_crypto_error = None
        # Track 1 — reset reconnect state
        self._reconnect_attempts = 0
        self._reconnecting = False
        self._last_reconnect_error = None
        self._last_successful_sync = None
        self._stop_requested = False
        # Track 2 — reset crypto store state
        self._crypto_store_loaded = False
        # Track 4 — reset room states
        self._room_states = {}

        mode = self._config.encryption_mode
        if mode == "e2ee_required":
            await self._start_e2ee_required()
        elif mode == "e2ee_optional":
            await self._start_e2ee_optional()
        else:
            await self._start_plaintext()

    async def _start_plaintext(self) -> None:
        """Standard plaintext startup — no explicit crypto.

        When ``vodozemac`` is installed, nio sets
        ``ENCRYPTION_ENABLED=True`` and ``restore_login`` calls
        ``load_store()`` which requires a valid ``device_id``.
        We discover the device_id via ``whoami()`` before
        ``restore_login``, matching mmrelay's pattern, so
        plaintext mode never uploads keys with a mismatched device_id.
        """
        import nio

        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=self._config.device_id or None,
            store_path=self._config.store_path,
        )
        # Discover the actual device_id from the authenticated session
        device_id = await self._discover_device_id()
        self._client.restore_login(
            user_id=self._config.user_id,
            device_id=device_id,
            access_token=self._config.access_token,
        )
        await self._finalize_start()

    async def _start_e2ee_required(self) -> None:
        """E2EE-required startup.

        Pre-conditions:
        * ``HAS_E2EE`` is ``True`` (checked)
        * ``store_path`` is set by the runtime builder under the resolved
          state directory (``{state}/adapters/{adapter_id}/matrix/store``).  When
          ``device_id`` is not set the session discovers it via ``whoami()``
          after establishing the access token context.

        Enables crypto via ``nio.AsyncClient(encryption_enabled=True)``.
        """
        if not _compat_mod.HAS_E2EE:
            raise MatrixConnectionError(
                "mindroom-nio[e2e] not installed; "
                "pip install 'medre[matrix-e2e]' — "
                "e2ee_required mode requires crypto dependencies"
            )

        import nio

        store_path = self._config.store_path
        if not store_path:
            raise MatrixConnectionError(
                "E2EE requires a store_path — the runtime builder derives "
                "this from the resolved state directory.  When constructing "
                "MatrixConfig directly, set store_path explicitly."
            )

        # Ensure the store directory exists.
        Path(store_path).mkdir(parents=True, exist_ok=True)

        try:
            client_config: Any = nio.ClientConfig(encryption_enabled=True)
        except Exception as exc:
            raise MatrixConnectionError(f"Failed to configure E2EE: {exc}") from exc

        # device_id may be None initially — we discover it via whoami().
        device_id = self._config.device_id
        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=device_id,
            store_path=store_path,
            config=client_config,
        )

        # Discover device_id via whoami() if not known.
        if not device_id:
            device_id = await self._discover_device_id()

        self._client.restore_login(
            user_id=self._config.user_id,
            device_id=device_id,
            access_token=self._config.access_token,
        )

        if not getattr(self._client, "logged_in", False):
            # Track 3 — partial startup cleanup
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        self._crypto_enabled = True
        # Track 2 — crypto store loaded
        self._crypto_store_loaded = True
        await self._finalize_start()

    async def _start_e2ee_optional(self) -> None:
        """E2EE-optional startup.

        If ``HAS_E2EE`` is ``True``, attempt crypto setup (deriving
        store_path/device_id internally as needed).  On failure, log a
        warning and fall back to plaintext with ``crypto_enabled=False``.
        """
        can_attempt_crypto = _compat_mod.HAS_E2EE

        if can_attempt_crypto:
            try:
                await self._start_e2ee_required()
                return  # crypto start succeeded
            except Exception as exc:
                self._logger.warning(
                    "E2EE optional setup failed, falling back to " "plaintext: %s",
                    exc,
                )
                self._crypto_enabled = False
                self._crypto_store_loaded = False
                self._last_crypto_error = str(exc)
                # Clean up any partial client from failed crypto start
                if self._client is not None:
                    try:
                        await self._client.close()
                    except Exception:
                        pass
                    self._client = None

        # Plaintext fallback
        await self._start_plaintext()

    async def _discover_device_id(self) -> str:
        """Discover the device ID via the Matrix ``whoami`` endpoint.

        The client must already be constructed with ``user_id`` and
        ``access_token`` set so that ``whoami()`` succeeds.  Returns
        the discovered ``device_id`` string.

        Raises :class:`MatrixConnectionError` on failure.
        """
        if self._client is None:
            raise MatrixConnectionError(
                "cannot discover device_id: client not initialised"
            )
        # Set the access token so whoami() can authenticate.
        self._client.access_token = self._config.access_token
        try:
            resp = await self._client.whoami()
        except Exception as exc:
            raise MatrixConnectionError(
                f"whoami() failed during device_id discovery: {exc}"
            ) from exc
        device_id = getattr(resp, "device_id", None)
        if not device_id:
            raise MatrixConnectionError(
                "whoami() did not return a device_id — the access token "
                "may not be associated with a device"
            )
        self._logger.info(
            "Discovered device_id via whoami(): %s",
            device_id,
        )
        return str(device_id)

    async def _finalize_start(self) -> None:
        """Common post-client-creation steps: validate login, register
        callbacks, start sync task."""
        if not getattr(self._client, "logged_in", False):
            # Track 3 — partial startup cleanup
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        if self._message_callback is not None:
            import nio

            self._client.add_event_callback(
                self._message_callback,
                (nio.RoomMessageText, nio.RoomMessageNotice, nio.RoomMessageEmote),
            )

            # Register reaction event callback so that Matrix reactions
            # (m.annotation) reach the same inbound handler.  Wrapped in
            # try/except so that older nio versions without ReactionEvent
            # degrade gracefully.
            try:
                reaction_classes = _reaction_event_classes(nio)
                if reaction_classes:
                    self._client.add_event_callback(
                        self._message_callback,
                        reaction_classes,
                    )
                else:
                    self._logger.debug(
                        "No ReactionEvent class found in nio; "
                        "reaction callback not registered"
                    )
            except (AttributeError, ImportError):
                pass

        # Register MegolmEvent callback for undecryptable encrypted events.
        self._register_megolm_callback()

        # Part D — register invite callback for auto-join.
        self._register_invite_callback()

        sync_coro = self._run_sync()
        try:
            self._sync_task = asyncio.create_task(sync_coro)
        except Exception as exc:
            sync_coro.close()
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            raise MatrixConnectionError(
                f"failed to start sync for {self._config.user_id}: {exc}"
            ) from exc

    def _register_megolm_callback(self) -> None:
        """Register callbacks for undecryptable MegolmEvent and RoomEncryptionEvent.

        When crypto is active nio auto-decrypts MegolmEvents to
        RoomMessageText.  The callback registered here fires for events
        that could *not* be decrypted (missing room key, etc.).

        RoomEncryptionEvent fires when a room's encryption state changes.
        This sets ``_encrypted_room_seen`` so the safety check can detect
        encrypted rooms.  The event is NOT forwarded to the canonical
        event pipeline.
        """
        if self._client is None:
            return

        try:
            from nio.events import MegolmEvent

            self._client.add_event_callback(
                self._on_megolm_event,
                (MegolmEvent,),
            )
        except ImportError:
            pass

        try:
            from nio.events import RoomEncryptionEvent

            self._client.add_event_callback(
                self._on_room_encryption_event,
                (RoomEncryptionEvent,),
            )
        except ImportError:
            pass

    # Part D — invite callback registration
    def _register_invite_callback(self) -> None:
        """Register an InviteMemberEvent callback for auto-join.

        Discovers ``InviteMemberEvent`` from nio and registers
        ``self._on_invite`` as the handler.  Wrapped in try/except for
        older nio versions that may not expose this class.
        """
        if self._client is None:
            return

        try:
            import nio

            invite_cls = getattr(nio, "InviteMemberEvent", None)
            if invite_cls is None:
                invite_cls = getattr(nio.events, "InviteMemberEvent", None)
            if invite_cls is not None:
                self._client.add_event_callback(
                    self._on_invite,
                    (invite_cls,),
                )
        except (ImportError, AttributeError):
            pass

    # Part D — ensure_joined helper
    async def ensure_joined(self, room_id: str) -> bool:
        """Ensure the session has joined the given room.

        Returns ``True`` if already joined or join succeeds, ``False``
        on failure.  Does **not** raise on join failure — callers that
        need hard failures should check the return value.

        Uses ``_joining_rooms`` to avoid duplicate concurrent joins for
        the same room.
        """
        if not isinstance(room_id, str) or not room_id:
            self._logger.warning("ensure_joined: invalid room_id %r", room_id)
            return False

        if self._client is None:
            self._logger.warning(
                "ensure_joined: client is None, cannot join %s", room_id
            )
            return False

        # Already joined — check client.rooms.
        rooms = getattr(self._client, "rooms", None)
        if rooms is not None and isinstance(rooms, dict) and room_id in rooms:
            return True

        # Deduplicate concurrent joins.
        if room_id in self._joining_rooms:
            return True
        self._joining_rooms.add(room_id)
        try:
            response = await self._client.join(room_id)
            # nio JoinResponse has room_id; error responses do not.
            if hasattr(response, "room_id"):
                self._logger.info("Joined room %s", room_id)
                return True
            else:
                err_detail = str(response)
                self._logger.warning(
                    "Failed to join room %s: %s", room_id, err_detail
                )
                return False
        except Exception as exc:
            self._logger.warning(
                "Exception joining room %s: %s", room_id, exc
            )
            return False
        finally:
            self._joining_rooms.discard(room_id)

    # Part D — ensure_joined_rooms batch helper
    async def ensure_joined_rooms(self, room_ids: Iterable[str]) -> dict[str, bool]:
        """Join multiple rooms, returning a mapping of room_id → success.

        Deduplicates while preserving deterministic order.  Failure to
        join one room does not prevent attempts for others.
        """
        unique = dict.fromkeys(room_ids)
        results: dict[str, bool] = {}
        for rid in unique:
            results[rid] = await self.ensure_joined(rid)
        return results

    # Part D — invite handler
    async def _on_invite(self, room: Any, event: Any) -> None:
        """Handle an InviteMemberEvent.

        Accepts invitations for rooms listed in ``_auto_join_rooms``.
        Unconfigured invitations are logged at debug level and ignored.
        """
        room_id = getattr(event, "room_id", None) or (
            getattr(room, "room_id", None) if room else None
        )
        if not room_id:
            return

        if room_id in self._auto_join_rooms:
            self._logger.info(
                "Accepting invitation to configured room %s", room_id
            )
            await self.ensure_joined(room_id)
        else:
            self._logger.debug(
                "Ignoring invitation to unconfigured room %s", room_id
            )

    async def _on_megolm_event(self, room: Any, event: Any) -> None:
        """Handle an undecryptable MegolmEvent.

        Counts the event, records the last crypto error, logs a warning,
        but does NOT crash or forward to the adapter message callback.
        """
        self._undecryptable_event_count += 1
        event_id = getattr(event, "event_id", "<unknown>")
        room_id = getattr(room, "room_id", "<unknown>") if room else "<unknown>"

        self._last_crypto_error = f"Undecryptable MegolmEvent {event_id} in {room_id}"

        self._encrypted_room_seen = True
        self._logger.warning(
            "Undecryptable MegolmEvent %s in room %s",
            event_id,
            room_id,
        )

        # Track 4 — mark room as encrypted
        if room is not None:
            rid = getattr(room, "room_id", None)
            if rid is not None:
                if (
                    len(self._room_states) >= _MAX_ROOM_STATES
                    and rid not in self._room_states
                ):
                    oldest = next(iter(self._room_states))
                    del self._room_states[oldest]
                    self._logger.warning(
                        "MatrixSession: room-state tracking hit cap (%d); "
                        "evicted room %s for encrypted room %s",
                        _MAX_ROOM_STATES,
                        oldest,
                        rid,
                    )
                self._room_states[rid] = "encrypted"

    async def _on_room_encryption_event(self, room: Any, event: Any) -> None:
        """Handle a RoomEncryptionEvent (m.room.encryption state event).

        Sets ``_encrypted_room_seen`` and logs.  Does NOT forward to the
        canonical event pipeline — this is a state-tracking callback only.
        """
        self._encrypted_room_seen = True
        room_id = getattr(room, "room_id", "<unknown>") if room else "<unknown>"
        self._logger.info(
            "RoomEncryptionEvent received for room %s — room encryption enabled",
            room_id,
        )

        # Track 4 — mark room as encrypted
        if room is not None:
            rid = getattr(room, "room_id", None)
            if rid is not None:
                if (
                    len(self._room_states) >= _MAX_ROOM_STATES
                    and rid not in self._room_states
                ):
                    oldest = next(iter(self._room_states))
                    del self._room_states[oldest]
                    self._logger.warning(
                        "MatrixSession: room-state tracking hit cap (%d); "
                        "evicted room %s for encrypted room %s",
                        _MAX_ROOM_STATES,
                        oldest,
                        rid,
                    )
                self._room_states[rid] = "encrypted"

    # Track 4 — track rooms seen via sync (called by message callback wrapper)
    def _track_room(self, room_id: str) -> None:
        """Track a room as seen.  Sets 'unknown' if not already tracked.

        Bounded by ``_MAX_ROOM_STATES`` — when the cap is reached, the
        oldest room entry is evicted.
        """
        if room_id in self._room_states:
            return
        if len(self._room_states) >= _MAX_ROOM_STATES:
            # Evict one oldest entry to make room.
            oldest = next(iter(self._room_states))
            del self._room_states[oldest]
            _logger.warning(
                "MatrixSession: room-state tracking hit cap (%d); "
                "evicted room %s to track new room %s",
                _MAX_ROOM_STATES,
                oldest,
                room_id,
            )
        self._room_states[room_id] = "unknown"

    # -- Sync loop (Track 1 — Automatic Sync Recovery) -----------------------

    async def _run_sync(self) -> None:
        """Wrap ``_sync_with_reconnect`` — entry point for the sync task."""
        try:
            await self._sync_with_reconnect()
        except asyncio.CancelledError:
            return

    async def _sync_with_reconnect(self) -> None:
        """Bounded reconnect loop around ``sync_forever``.

        On sync failure (transient), initiates reconnect with exponential
        backoff (1s, 2s, 4s, 8s, 16s capped at 60s) with +-25% jitter.
        After ``_MAX_RECONNECT_ATTEMPTS`` consecutive failures, gives up
        and sets ``_sync_failure``.

        On ``CancelledError``: stops reconnecting immediately, re-raises.
        On ``_stop_requested``: does not start new reconnect.
        """
        while not self._stop_requested:
            try:
                self._reconnecting = False

                await self._client.sync_forever(
                    timeout=self._config.sync_timeout_ms,
                )
                # sync_forever returned normally (clean shutdown / unusual)
                if self._reconnect_attempts > 0:
                    self._logger.info(
                        "Sync recovered after %d reconnect attempts",
                        self._reconnect_attempts,
                    )
                self._reconnect_attempts = 0
                self._last_reconnect_error = None
                self._last_successful_sync = time.monotonic()
                return
            except asyncio.CancelledError:
                self._reconnecting = False
                raise
            except Exception as exc:
                if self._stop_requested:
                    self._sync_failure = exc
                    self._reconnecting = False
                    return

                self._reconnect_attempts += 1
                self._last_reconnect_error = str(exc)

                if self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
                    self._logger.error(
                        "Max sync reconnect attempts (%d) reached, " "giving up: %s",
                        _MAX_RECONNECT_ATTEMPTS,
                        exc,
                    )
                    self._sync_failure = exc
                    self._reconnecting = False
                    return

                # Compute backoff with jitter
                delay = min(
                    _BACKOFF_BASE * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_CAP,
                )
                jitter = delay * _BACKOFF_JITTER_FRACTION
                actual_delay = max(0.0, delay + random.uniform(-jitter, jitter))

                self._reconnecting = True
                self._logger.warning(
                    "Sync failed (attempt %d/%d), reconnecting in %.1fs: %s",
                    self._reconnect_attempts,
                    _MAX_RECONNECT_ATTEMPTS,
                    actual_delay,
                    exc,
                )

                try:
                    await asyncio.sleep(actual_delay)
                except asyncio.CancelledError:
                    if self._stop_requested:
                        self._reconnecting = False
                        return
                    raise

        # _stop_requested was True
        self._reconnecting = False

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing, close the client.  Idempotent."""
        # Track 3 — signal stop to prevent reconnect loops
        self._stop_requested = True

        if self._sync_task is not None:
            if not self._sync_task.done():
                self._sync_task.cancel()
                try:
                    await asyncio.wait_for(self._sync_task, timeout=timeout)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    self._logger.warning(
                        "Sync task did not stop within %.1fs",
                        timeout,
                    )
            try:
                self._sync_task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            self._sync_task = None

        if self._client is not None:
            try:
                self._client.stop_sync_forever()
            except Exception as exc:
                self._logger.warning(
                    "Error stopping sync_forever: %s",
                    exc,
                )
            try:
                await self._client.close()
            except Exception as exc:
                self._logger.warning(
                    "Error closing client: %s",
                    exc,
                )
            # Yield to the event loop so aiohttp can finish closing its
            # internal connector and any in-flight responses.  Without
            # this drain, Python may garbage-collect the aiohttp
            # ClientSession before its __aexit__ completes, producing
            # ``ResourceWarning: Unclosed client session``.
            await asyncio.sleep(0)
            self._client = None

        self._closed = True
        self._reconnecting = False
        # Track 3 — reset reconnect counter so diagnostics are truthful after stop
        self._reconnect_attempts = 0

    # -- Diagnostics ----------------------------------------------------------

    def diagnostics(self) -> MatrixSessionDiagnostics:
        """Return a read-only snapshot of session state.

        Never exposes secrets, access tokens, keys, or private device
        material.
        """
        return MatrixSessionDiagnostics(
            connected=self.connected,
            logged_in=self.logged_in,
            sync_task_running=self.sync_task_running,
            last_sync_error=self.last_sync_error,
            store_path_configured=self._config.store_path is not None,
            device_id_configured=self._config.device_id is not None,
            encryption_mode=self._config.encryption_mode,
            crypto_enabled=self._crypto_enabled,
            last_crypto_error=self._last_crypto_error,
            encrypted_room_seen=self._encrypted_room_seen,
            undecryptable_event_count=self._undecryptable_event_count,
            # Track 1
            sync_running=self.sync_running,
            reconnecting=self._reconnecting,
            reconnect_attempts=self._reconnect_attempts,
            last_successful_sync=self._last_successful_sync,
            # Track 2
            crypto_store_loaded=self._crypto_store_loaded,
            # Track 4
            encrypted_room_count=self.encrypted_room_count,
            plaintext_room_count=self.plaintext_room_count,
        )
