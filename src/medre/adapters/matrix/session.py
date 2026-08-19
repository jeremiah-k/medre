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
import hashlib
import importlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, cast

import medre.adapters.matrix.compat as _compat_mod
from medre.adapters.matrix.errors import (
    MATRIX_PERMANENT_ERRCODES,
    MatrixConnectionError,
)
from medre.adapters.matrix.identity import (
    MatrixCrossSigningDiagnostics,
    MatrixCrossSigningService,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.ingress import INGRESS_PROVENANCE_VALUES, IngressProvenance

_logger = logging.getLogger(__name__)
_sleep = asyncio.sleep

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

# Missing Megolm room-key recovery.  mindroom-nio handles transport-level
# timeout retries, but homeserver/federation to-device failures can still
# return error responses.  Keep this best-effort recovery bounded so one
# undecryptable event never stalls the sync loop indefinitely.
_ROOM_KEY_REQUEST_MAX_ATTEMPTS: int = 3
_ROOM_KEY_REQUEST_BASE_DELAY_SECONDS: float = 2.0
_ROOM_KEY_REQUEST_TIMEOUT_SECONDS: float = 10.0


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
    megolm_recovery_attempts: int
    megolm_recovery_successes: int
    megolm_recovery_failures: int
    # Sync recovery diagnostics
    sync_running: bool
    reconnecting: bool
    reconnect_attempts: int
    last_successful_sync: float | None
    checkpoint_owned_by_medre: bool
    committed_checkpoint_present: bool
    recovered_event_count: int
    history_event_count: int
    recovery_abandoned_room_count: int
    recovery_last_abandonment: str | None
    # Crypto-store continuity
    crypto_store_loaded: bool
    # Room-state tracking counts (no room names/IDs)
    encrypted_room_count: int
    plaintext_room_count: int
    # E2EE key management diagnostics
    olm_loaded: bool
    store_loaded: bool
    device_keys_uploaded: bool
    key_query_needed: bool
    device_id_in_use: str | None
    store_path_exists: bool
    initial_sync_completed: bool
    # Own-device cross-signing diagnostics (never peer-device trust)
    cross_signing_provider_supported: bool
    cross_signing_local_identity_present: bool
    cross_signing_server_identity_present: bool | None
    cross_signing_current_device_self_signed: bool | None
    cross_signing_chain_status: str
    cross_signing_repair_required: bool
    cross_signing_reset_required: bool
    cross_signing_last_failure_category: str | None


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
        "_admission_callback",
        "_checkpoint_loader",
        "_checkpoint_committer",
        "_durable_sync_enabled",
        "_committed_sync_token",
        "_recovered_event_count",
        "_history_event_count",
        "_recovery_abandoned_rooms",
        "_recovery_last_abandonment",
        "_closed",
        "_logger",
        "_crypto_enabled",
        "_encrypted_room_seen",
        "_undecryptable_event_count",
        "_room_key_request_attempts",
        "_room_key_request_successes",
        "_room_key_request_failures",
        "_room_key_request_tasks",
        "_last_crypto_error",
        # Sync recovery
        "_reconnect_attempts",
        "_reconnecting",
        "_last_reconnect_error",
        "_last_successful_sync",
        "_stop_requested",
        # Crypto-store continuity
        "_crypto_store_loaded",
        # Room-state tracking
        "_room_states",
        # Auto-join
        "_auto_join_rooms",
        "_joining_rooms",
        # Sync boundary / history suppression
        "_live_sync_started",
        "_suppressed_backlog_undecryptable",
        # Live undecryptable dedup
        "_undecryptable_dedup",
        "_suppressed_rate_limited_undecryptable",
        # RoomEncryptionEvent logging dedup
        "_encryption_event_seen_rooms",
        # E2EE key management — initial sync tracking
        "_initial_sync_done",
        # Own-device cross-signing lifecycle
        "_cross_signing_service",
        "_cross_signing_diagnostics",
    )

    _UNDECRYPTABLE_DEDUP_WINDOW_SECS: float = 60.0

    def __init__(
        self,
        config: MatrixConfig,
        message_callback: Callable[..., Any] | None = None,
        admission_callback: Callable[..., Any] | None = None,
        checkpoint_loader: Callable[..., Any] | None = None,
        checkpoint_committer: Callable[..., Any] | None = None,
        logger: logging.Logger | None = None,
        auto_join_rooms: tuple[str, ...] = (),
    ) -> None:
        self._config = config
        self._client: Any = None
        self._sync_task: asyncio.Task | None = None
        self._sync_failure: Exception | None = None
        self._message_callback = message_callback
        self._admission_callback = admission_callback
        self._checkpoint_loader = checkpoint_loader
        self._checkpoint_committer = checkpoint_committer
        self._durable_sync_enabled = all(
            callback is not None
            for callback in (
                admission_callback,
                checkpoint_loader,
                checkpoint_committer,
            )
        )
        self._committed_sync_token: str | None = None
        self._recovered_event_count = 0
        self._history_event_count = 0
        self._recovery_abandoned_rooms: dict[str, tuple[str, ...]] = {}
        self._recovery_last_abandonment: str | None = None
        self._closed = False
        self._logger: logging.Logger = logger if logger is not None else _logger
        self._crypto_enabled: bool = False
        self._encrypted_room_seen: bool = False
        self._undecryptable_event_count: int = 0
        self._last_crypto_error: str | None = None
        self._room_key_request_attempts: int = 0
        self._room_key_request_successes: int = 0
        self._room_key_request_failures: int = 0
        self._room_key_request_tasks: dict[str, asyncio.Task[None]] = {}
        # Sync recovery
        self._reconnect_attempts: int = 0
        self._reconnecting: bool = False
        self._last_reconnect_error: str | None = None
        self._last_successful_sync: float | None = None
        self._stop_requested: bool = False
        # Crypto-store continuity
        self._crypto_store_loaded: bool = False
        # Room-state tracking
        self._room_states: dict[str, RoomEncryptionState] = {}
        # Auto-join
        self._auto_join_rooms = auto_join_rooms
        self._joining_rooms: dict[str, asyncio.Task[bool]] = {}
        # Sync boundary / history suppression
        self._live_sync_started: bool = False
        self._suppressed_backlog_undecryptable: int = 0
        # Live undecryptable dedup
        self._undecryptable_dedup: dict[str, float] = {}
        self._suppressed_rate_limited_undecryptable: int = 0
        # RoomEncryptionEvent logging dedup
        self._encryption_event_seen_rooms: set[str] = set()
        # E2EE key management — initial sync tracking
        self._initial_sync_done: bool = False
        # Cross-signing is a separate identity policy component.  The session
        # owns its runtime lifetime but never supplies a password or permits
        # master/self-signing bootstrap or rotation during ordinary startup.
        self._cross_signing_service: MatrixCrossSigningService | None = None
        self._cross_signing_diagnostics = MatrixCrossSigningDiagnostics()

    # -- Properties -----------------------------------------------------------

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

    # Sync recovery properties

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

    @property
    def is_live(self) -> bool:
        """``True`` after the first successful sync with a ``next_batch`` token.

        Before this point, inbound events are considered backlog / history
        and are suppressed from the adapter pipeline.
        """
        return self._live_sync_started

    # Crypto-store continuity

    @property
    def crypto_store_loaded(self) -> bool:
        """``True`` when the crypto store was loaded at startup.

        This is a cached flag set during ``start()``/``restore_login()``.
        :meth:`diagnostics` recomputes from live client state
        (``olm_loaded and store_loaded``) for operational freshness.
        The two sources are intentionally separate: the property answers
        "was it ever loaded?" while diagnostics answers "is it loaded
        right now?"
        """
        return self._crypto_store_loaded

    # Room-state tracking

    def room_state(self, room_id: str) -> RoomEncryptionState:
        """Return the tracked encryption state for a room.

        Returns ``"unknown"`` for rooms not yet seen.
        """
        return self._room_states.get(room_id, "unknown")

    # -- Session-boundary public query methods (per §31 §7.2) ----------------

    def is_logged_in(self) -> bool:
        """Return ``True`` if the underlying client reports ``logged_in``.

        Safe to call when the client is not initialised (returns ``False``).
        """
        return self.logged_in

    def has_access_token(self) -> bool:
        """Return ``True`` if the underlying client has an ``access_token``.

        Safe to call when the client is not initialised (returns ``False``).
        """
        if self._client is None:
            return False
        return getattr(self._client, "access_token", None) is not None

    def is_room_member(self, room_id: str) -> bool:
        """Return ``True`` if the session has joined the given room.

        Checks ``self._client.rooms`` for the room ID.  Returns ``False``
        when the client is not initialised or the room is not present.
        """
        if self._client is None:
            return False
        rooms = getattr(self._client, "rooms", None)
        if rooms is None or not isinstance(rooms, dict):
            return False
        return room_id in rooms

    def is_room_encrypted(self, room_id: str) -> bool:
        """Return ``True`` if the room is known to be encrypted.

        Checks the session's room-state tracking cache first, then
        falls back to ``self._client.rooms[room_id].encrypted`` for
        rooms not yet tracked.
        """
        state = self.room_state(room_id)
        if state == "encrypted":
            return True
        if state == "plaintext":
            return False
        # "unknown" — fall back to client.rooms
        if self._client is not None:
            rooms = getattr(self._client, "rooms", None)
            if rooms is not None and isinstance(rooms, dict):
                room_obj = rooms.get(room_id)
                if room_obj is not None and getattr(room_obj, "encrypted", False):
                    return True
        return False

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
        # Guard against double-start.
        if self._client is not None and not self._closed:
            self._logger.warning("MatrixSession.start() called while already running")
            return

        self._sync_failure = None
        self._closed = False
        self._crypto_enabled = False
        self._encrypted_room_seen = False
        self._undecryptable_event_count = 0
        self._last_crypto_error = None
        self._room_key_request_attempts = 0
        self._room_key_request_successes = 0
        self._room_key_request_failures = 0
        # Reset reconnect state
        self._reconnect_attempts = 0
        self._reconnecting = False
        self._last_reconnect_error = None
        self._last_successful_sync = None
        self._committed_sync_token = None
        self._recovered_event_count = 0
        self._history_event_count = 0
        self._recovery_abandoned_rooms = {}
        self._recovery_last_abandonment = None
        self._stop_requested = False
        # Reset crypto store state
        self._crypto_store_loaded = False
        # Reset room states
        self._room_states = {}
        # Sync boundary / history suppression
        self._live_sync_started = False
        self._suppressed_backlog_undecryptable = 0
        self._undecryptable_dedup = {}
        self._suppressed_rate_limited_undecryptable = 0
        # RoomEncryptionEvent logging dedup
        self._encryption_event_seen_rooms = set()
        # E2EE key management — initial sync tracking
        self._initial_sync_done = False
        # Each start performs a fresh server-visible cross-signing check.
        self._cross_signing_service = None
        self._cross_signing_diagnostics = MatrixCrossSigningDiagnostics()

        mode = self._config.encryption_mode
        if mode == "e2ee_required":
            await self._start_e2ee_required()
        elif mode == "e2ee_optional":
            await self._start_e2ee_optional()
        else:
            await self._start_plaintext()

    def _build_client_config(self, nio_module: Any, *, encryption_enabled: bool) -> Any:
        """Build the pinned mindroom-nio sync and peer-device trust policy.

        MEDRE owns durable Classic Sync checkpoints when storage callbacks are
        available.  The pinned mindroom-nio provider exposes
        ``replace_rotated_device_keys`` and the installed-SDK contract requires
        it.  The defensive attribute/update guards remain intentional for
        lightweight test doubles and provider configuration objects that may be
        immutable.  Encrypted sessions permit rotated peer device keys, matching
        MEDRE's existing permissive bot trust policy
        (``ignore_unverified_devices=True`` on send).  Own-device cross-signing
        remains a separate policy and is never rotated here.
        """
        config = nio_module.AsyncClientConfig(
            encryption_enabled=encryption_enabled,
            max_timeouts=3,
            backfill_limited_timelines=self._durable_sync_enabled,
            store_sync_tokens=not self._durable_sync_enabled,
            backfill_persist_recovery=False,
        )
        if not encryption_enabled or not hasattr(config, "replace_rotated_device_keys"):
            return config

        try:
            config.replace_rotated_device_keys = True
        except (AttributeError, TypeError):
            try:
                config = replace(config, replace_rotated_device_keys=True)
            except (TypeError, ValueError):
                self._logger.warning(
                    "Matrix client exposes rotated device-key recovery but "
                    "its configuration could not be updated"
                )
        return config

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

        client_config = self._build_client_config(nio, encryption_enabled=False)
        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=self._config.device_id or None,
            store_path=self._config.store_path,
            config=client_config,
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
            client_config: Any = self._build_client_config(nio, encryption_enabled=True)
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
            # Partial startup cleanup: close client on login failure.
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
        # Verify Olm and store loaded after restore_login.
        # If Olm/store are None despite E2EE deps, the crypto subsystem
        # is broken and we must not claim crypto is operational.
        if self._client.olm is None:
            olm_missing = True
            self._logger.error(
                "E2EE: olm is None after restore_login — "
                "crypto subsystem not initialised"
            )
            self._crypto_enabled = False
            self._crypto_store_loaded = False
        elif self._client.store is None:
            olm_missing = False
            self._logger.error(
                "E2EE: store is None after restore_login — " "crypto store not loaded"
            )
            self._crypto_enabled = False
            self._crypto_store_loaded = False
        else:
            olm_missing = False
            self._crypto_store_loaded = True

        # Fail-closed: e2ee_required mode must not silently downgrade
        # when the crypto subsystem is broken.  For e2ee_optional the
        # caller (_start_e2ee_optional) catches exceptions and falls
        # back to plaintext.
        if not self._crypto_enabled and self._config.encryption_mode == "e2ee_required":
            if self._client:
                try:
                    await self._client.close()
                except Exception:
                    pass
                self._client = None
            if olm_missing:
                raise MatrixConnectionError(
                    "E2EE required but Olm subsystem failed to initialise"
                )
            raise MatrixConnectionError("E2EE required but crypto store failed to load")

        await self._reconcile_cross_signing_runtime()
        await self._finalize_start()

    async def _reconcile_cross_signing_runtime(self) -> None:
        """Verify/repair own-device cross-signing without identity rotation.

        Runtime startup deliberately has no password and does not opt into
        bootstrap.  The identity policy may verify an existing chain or repair
        the current-device self-signature when the persisted master/self-signing
        identity matches the homeserver.  Missing or mismatched identity material
        is diagnostic state only; recovery/rotation belongs to the authenticated
        Matrix auth workflow.
        """
        if not self._crypto_enabled or self._client is None:
            return

        service = MatrixCrossSigningService(self._client, logger=self._logger)
        self._cross_signing_service = service
        try:
            result = await service.reconcile()
        finally:
            # Snapshot the secret-free state even when cancellation interrupts
            # startup.  The service itself propagates CancelledError.
            self._cross_signing_diagnostics = service.diagnostics()

        diagnostics = self._cross_signing_diagnostics
        if diagnostics.chain_status == "valid":
            self._logger.debug(
                "Matrix own-device cross-signing verified (%s)", result or "verified"
            )
        elif diagnostics.reset_required:
            self._logger.warning(
                "Matrix own-device cross-signing requires authenticated recovery; "
                "runtime startup will not rotate identity material"
            )
        elif diagnostics.repair_required:
            self._logger.info(
                "Matrix own-device cross-signing needs operator bootstrap/repair; "
                "continuing without identity rotation"
            )

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
        resolved_user_id = getattr(resp, "user_id", None)
        if (
            isinstance(resolved_user_id, str)
            and resolved_user_id
            and resolved_user_id != self._config.user_id
        ):
            raise MatrixConnectionError(
                "whoami() authenticated as "
                f"{resolved_user_id}, not configured user_id {self._config.user_id}"
            )

        device_id = getattr(resp, "device_id", None)
        if not device_id:
            raise MatrixConnectionError(
                "whoami() did not return a device_id — the access token "
                "may not be associated with a device"
            )
        self._logger.debug(
            "Discovered device_id via whoami(): %s",
            device_id,
        )
        return str(device_id)

    def _normalize_event(self, room: Any, event: Any) -> dict[str, Any]:
        """Normalize a raw nio event + room pair into a plain dict.

        Extracts proto-CanonicalEvent fields so that the adapter callback
        never receives raw nio objects.  The returned dict contains:

        * ``room_id`` — from ``room.room_id``
        * ``sender`` — from ``event.sender``
        * ``body`` — from ``event.body``
        * ``event_id`` — from ``event.event_id``
        * ``source`` — from ``event.source`` (raw Matrix event JSON dict)
        * ``msgtype`` — from content or ``event.msgtype``
        * ``server_timestamp`` — from ``event.server_timestamp`` or ``origin_server_ts``
        * ``sender_display_name`` — pre-resolved display name for the sender

        Per §31 §7.1 the session-to-adapter boundary only carries plain
        dicts, never raw SDK objects.
        """
        source = getattr(event, "source", None)
        content = source.get("content", {}) if isinstance(source, dict) else {}
        msgtype = content.get("msgtype") or getattr(event, "msgtype", None)
        server_timestamp = getattr(event, "server_timestamp", None) or getattr(
            event, "origin_server_ts", None
        )
        sender = getattr(event, "sender", "")

        # Pre-resolve display name from the room object so the adapter
        # never needs to hold a reference to the raw nio Room.
        sender_display_name = self._resolve_display_name(room, sender)

        return {
            "room_id": getattr(room, "room_id", ""),
            "sender": sender,
            "body": getattr(event, "body", ""),
            "event_id": getattr(event, "event_id", ""),
            "source": source if isinstance(source, dict) else {},
            "msgtype": msgtype if isinstance(msgtype, str) else None,
            "server_timestamp": server_timestamp,
            "sender_display_name": sender_display_name,
        }

    @staticmethod
    def _resolve_display_name(room: Any, sender: str) -> str:
        """Resolve the display name for *sender* from a nio Room object.

        Preference order:
        1. ``room.user_name(sender)`` when callable and returns non-empty.
        2. ``room.users[sender]`` dict fields: ``display_name``, ``displayname``, ``name``.
        3. ``room.users[sender]`` object attributes: ``.display_name``, ``.displayname``, ``.name``.
        4. *sender* MXID as final fallback.

        ``None`` and blank / whitespace-only values are treated as missing.
        """
        # 1. room.user_name(sender)
        user_name_fn = getattr(room, "user_name", None)
        if callable(user_name_fn):
            try:
                val = str(user_name_fn(sender) or "").strip()
                if val:
                    return val
            except Exception:
                pass

        # 2 & 3. room.users[sender] — dict fields then object attributes.
        users = getattr(room, "users", None)
        if users is None:
            return sender
        user_info = users.get(sender) if isinstance(users, dict) else None
        if user_info is None:
            return sender

        # Dict path
        if isinstance(user_info, dict):
            for key in ("display_name", "displayname", "name"):
                raw = user_info.get(key)
                if raw is not None:
                    val = str(raw).strip()
                    if val:
                        return val
        else:
            # Object path
            for attr in ("display_name", "displayname", "name"):
                raw = getattr(user_info, attr, None)
                if raw is not None:
                    val = str(raw).strip()
                    if val:
                        return val

        return sender

    async def _on_nio_event(self, room: Any, event: Any) -> None:
        """nio callback wrapper that normalizes raw events to plain dicts.

        Receives raw nio ``RoomMessage*`` / ``ReactionEvent`` objects,
        converts them to plain dicts via :meth:`_normalize_event`, and
        forwards the dict to the adapter-provided ``_message_callback``.
        The adapter never sees raw nio objects.
        """
        if self._message_callback is None:
            return
        normalized = self._normalize_event(room, event)
        room_id = normalized.get("room_id")
        if isinstance(room_id, str) and room_id:
            self._track_room(room_id)
        await self._message_callback(normalized)

    async def _on_nio_admission(self, room: Any, event: Any, provenance: Any) -> None:
        """Durably admit a nio timeline event before ordinary callback fanout."""
        if self._admission_callback is None:
            return
        normalized = self._normalize_event(room, event)
        room_id = normalized.get("room_id")
        if isinstance(room_id, str) and room_id:
            self._track_room(room_id)
        provenance_value = getattr(provenance, "value", str(provenance)).lower()
        try:
            if provenance_value not in INGRESS_PROVENANCE_VALUES:
                raise ValueError(
                    f"unsupported Matrix ingress provenance: {provenance_value!r}"
                )
            ingress_provenance = cast(IngressProvenance, provenance_value)
            if provenance_value == "recovered":
                self._recovered_event_count += 1
            elif provenance_value == "history":
                self._history_event_count += 1
            await self._admission_callback(normalized, ingress_provenance)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            import nio

            rejection = getattr(nio, "CallbackNotAcceptedError", None)
            if rejection is None:
                try:
                    from nio.exceptions import CallbackNotAcceptedError as rejection
                except ImportError:
                    raise exc from None
            if isinstance(exc, rejection):
                raise
            raise rejection(f"MEDRE durable ingress admission failed: {exc}") from exc

    @staticmethod
    def _abandonment_metadata(response: Any) -> dict[str, tuple[str, ...]]:
        """Return durable recovery evidence keyed by native Matrix room ID."""
        raw = getattr(response, "abandoned_rooms", None) or {}
        result: dict[str, tuple[str, ...]] = {}
        for room_id, reasons in raw.items():
            values = []
            for reason in reasons:
                value = getattr(reason, "value", None)
                values.append(str(value if value is not None else reason))
            result[str(room_id)] = tuple(sorted(values))
        return result

    @staticmethod
    def _abandonment_diagnostic(
        abandoned: dict[str, tuple[str, ...]],
    ) -> str | None:
        """Return identifier-free abandonment diagnostics for operators."""
        if not abandoned:
            return None
        cause_counts: dict[str, int] = {}
        for reasons in abandoned.values():
            for reason in reasons:
                cause_counts[reason] = cause_counts.get(reason, 0) + 1
        return json.dumps(
            {"causes": cause_counts, "room_count": len(abandoned)},
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _on_sync_response(self, response: Any) -> None:
        """Commit MEDRE's Classic cursor, then acknowledge it to nio."""
        next_batch = getattr(response, "next_batch", None)
        if not isinstance(next_batch, str) or not next_batch:
            return

        abandoned = self._abandonment_metadata(response)
        merged = dict(self._recovery_abandoned_rooms)
        for room_id, reasons in abandoned.items():
            existing = set(merged.get(room_id, ()))
            existing.update(reasons)
            merged[room_id] = tuple(sorted(existing))
        metadata_json = json.dumps(
            {"abandoned_rooms": merged},
            sort_keys=True,
            separators=(",", ":"),
        )

        if self._durable_sync_enabled:
            client = self._client
            if client is None or self._stop_requested:
                return
            committer = self._checkpoint_committer
            if committer is None:
                raise RuntimeError("durable Matrix sync has no checkpoint committer")
            await committer("classic_sync", next_batch, metadata_json)
            client.acknowledge_classic_sync(next_batch)
            self._committed_sync_token = next_batch
            if abandoned:
                settle = getattr(client, "acknowledge_unrecovered_rooms", None)
                if callable(settle):
                    try:
                        settle(abandoned)
                    except Exception:
                        self._logger.warning(
                            "Failed to settle recorded Matrix recovery abandonment",
                            exc_info=True,
                        )

        self._recovery_abandoned_rooms = merged
        self._recovery_last_abandonment = self._abandonment_diagnostic(merged)
        if abandoned:
            self._logger.warning(
                "Matrix gap recovery abandoned history in %d room(s)", len(abandoned)
            )
        if not self._live_sync_started and self._suppressed_backlog_undecryptable:
            self._logger.debug(
                "Sync boundary reached — suppressed %d undecryptable backlog events",
                self._suppressed_backlog_undecryptable,
            )
        self._initial_sync_done = True
        self._live_sync_started = True
        self._last_successful_sync = time.monotonic()
        if self._reconnect_attempts:
            self._logger.info(
                "Sync recovered after %d reconnect attempts", self._reconnect_attempts
            )
        self._reconnect_attempts = 0
        self._last_reconnect_error = None

    async def _load_classic_checkpoint(self) -> None:
        """Restore MEDRE's committed Classic cursor and recovery evidence."""
        if not self._durable_sync_enabled:
            return
        loader = self._checkpoint_loader
        if loader is None:
            raise RuntimeError("durable Matrix sync has no checkpoint loader")
        checkpoint = await loader("classic_sync")
        self._committed_sync_token = (
            checkpoint.cursor if checkpoint is not None else None
        )
        self._recovery_abandoned_rooms = {}
        self._recovery_last_abandonment = None
        if checkpoint is not None and checkpoint.metadata_json:
            try:
                stored = json.loads(checkpoint.metadata_json)
                if not isinstance(stored, dict):
                    raise ValueError("checkpoint metadata must be an object")
                raw_rooms = stored.get("abandoned_rooms", {})
                if not isinstance(raw_rooms, dict):
                    raise ValueError("abandoned_rooms must be an object")
                restored: dict[str, tuple[str, ...]] = {}
                for room_id, reasons in raw_rooms.items():
                    if not isinstance(reasons, (list, tuple)) or not all(
                        isinstance(reason, str) for reason in reasons
                    ):
                        raise ValueError("abandoned room reasons must be strings")
                    restored[str(room_id)] = tuple(sorted(reasons))
                self._recovery_abandoned_rooms = restored
                self._recovery_last_abandonment = self._abandonment_diagnostic(restored)
            except (TypeError, ValueError):
                self._logger.warning(
                    "Ignoring malformed Matrix checkpoint recovery metadata"
                )
        client = self._client
        clear_recovery = (
            getattr(client, "clear_persisted_sync_recovery", None)
            if client is not None
            else None
        )
        if callable(clear_recovery) and getattr(client, "store", None) is not None:
            clear_recovery()

    async def _finalize_start(self) -> None:
        """Common post-client-creation steps: validate login, register callbacks, start sync task."""
        if not getattr(self._client, "logged_in", False):
            # Partial startup cleanup: close client on login failure.
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        import nio

        message_classes: tuple[type, ...] = (
            nio.RoomMessageText,
            nio.RoomMessageNotice,
            nio.RoomMessageEmote,
        )
        reaction_classes = _reaction_event_classes(nio)
        admission_classes = message_classes + reaction_classes

        if self._durable_sync_enabled and self._admission_callback is not None:
            self._client.add_event_admission_callback(
                self._on_nio_admission, admission_classes
            )
        elif self._message_callback is not None:
            self._client.add_event_callback(self._on_nio_event, message_classes)
            if reaction_classes:
                self._client.add_event_callback(self._on_nio_event, reaction_classes)

        sync_response_cls = getattr(nio, "SyncResponse", None)
        if sync_response_cls is not None:
            self._client.add_response_callback(
                self._on_sync_response, sync_response_cls
            )
        await self._load_classic_checkpoint()

        # Register MegolmEvent callback for undecryptable encrypted events.
        self._register_megolm_callback()

        # Register invite callback for auto-join.
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

    # Invite callback registration
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

    # ensure_joined helper
    async def ensure_joined(self, room_id: str) -> bool:
        """Ensure the session has joined the given room.

        Returns ``True`` if already joined or join succeeds, ``False``
        on failure.  Does **not** raise on join failure — callers that
        need hard failures should check the return value.

        Uses ``_joining_rooms`` (``dict[str, asyncio.Task[bool]]``) to
        avoid duplicate concurrent joins for the same room.  Concurrent
        callers await the leader's task via ``asyncio.shield`` so that
        cancelling a waiter does **not** cancel the underlying join.
        """
        if self._stop_requested or self._closed:
            self._logger.debug(
                "ensure_joined: session stopping/closed, skipping join for %s",
                room_id,
            )
            return False

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

        # Deduplicate concurrent joins using a Task per room.
        # Waiters await the leader's task via shield so their
        # cancellation cannot propagate to the join itself.
        if room_id in self._joining_rooms:
            return await asyncio.shield(self._joining_rooms[room_id])

        async def _join_once() -> bool:
            try:
                response = await self._client.join(room_id)
                if hasattr(response, "room_id"):
                    self._logger.info("Joined room %s", room_id)
                    return True
                else:
                    self._logger.warning(
                        "Failed to join room %s: %s", room_id, str(response)
                    )
                    return False
            except Exception as exc:
                self._logger.warning("Exception joining room %s: %s", room_id, exc)
                return False
            finally:
                if self._joining_rooms.get(room_id) is task:
                    self._joining_rooms.pop(room_id, None)

        task = asyncio.create_task(_join_once())
        self._joining_rooms[room_id] = task
        return await asyncio.shield(task)

    # ensure_joined_rooms batch helper
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

    # Invite handler
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
            self._logger.info("Accepting invitation to configured room %s", room_id)
            await self.ensure_joined(room_id)
        else:
            self._logger.debug("Ignoring invitation to unconfigured room %s", room_id)

    async def _on_megolm_event(self, room: Any, event: Any) -> None:
        """Handle an undecryptable MegolmEvent.

        Counts the event, records the last crypto error, logs a warning,
        but does NOT crash or forward to the adapter message callback.

        History suppression: before the first successful sync
        (``is_live`` is ``False``), events are considered backlog and
        logged at DEBUG only.  After going live, a 60-second dedup
        window suppresses repeated warnings for the same room+session
        key.
        """
        self._undecryptable_event_count += 1
        event_id = getattr(event, "event_id", "<unknown>")
        room_id = getattr(room, "room_id", "<unknown>") if room else "<unknown>"

        self._last_crypto_error = f"Undecryptable MegolmEvent {event_id} in {room_id}"

        self._encrypted_room_seen = True

        # Mark room as encrypted (shared helper)
        self._track_room_encrypted(room, room_id)

        # History suppression: suppress backlog undecryptable events.
        if not self.is_live:
            self._suppressed_backlog_undecryptable += 1
            self._logger.debug(
                "Suppressed backlog undecryptable MegolmEvent %s in room %s",
                event_id,
                room_id,
            )
            return

        # Live undecryptable dedup (60-second window per room:session_id).
        session_id = getattr(event, "session_id", "?")
        key = f"{room_id}:{session_id}"
        # Hashed session_id for logging — never log raw Megolm session IDs.
        session_id_tag = (
            hashlib.sha256(session_id.encode()).hexdigest()[:8]
            if session_id != "?"
            else "unknown"
        )
        now = time.monotonic()
        self._prune_undecryptable_dedup(now)
        prev = self._undecryptable_dedup.get(key)
        if prev is not None and now - prev < self._UNDECRYPTABLE_DEDUP_WINDOW_SECS:
            self._suppressed_rate_limited_undecryptable += 1
            self._logger.debug(
                "Rate-limited undecryptable MegolmEvent %s in room %s "
                "(session=%s, %.1fs since last)",
                event_id,
                room_id,
                session_id_tag,
                now - prev,
            )
            return

        self._logger.warning(
            "Undecryptable MegolmEvent %s in room %s",
            event_id,
            room_id,
        )
        self._undecryptable_dedup[key] = now

        # nio awaits async event callbacks while processing a sync response.
        # Missing-key recovery may spend tens of seconds in bounded network
        # retries, so detach it from the sync callback and track the task for
        # deterministic shutdown.  The warning dedup key also prevents a second
        # task for the same room/session during the dedup window.
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            self._logger.debug(
                "Skipping missing room-key request for event %s without a valid room ID",
                event_id,
            )
            return
        task = asyncio.create_task(
            self._request_missing_room_key(
                event=event,
                event_id=event_id,
                room_id=room_id,
                session_id_tag=session_id_tag,
            )
        )
        self._room_key_request_tasks[key] = task
        task.add_done_callback(
            lambda done, request_key=key: self._discard_room_key_request_task(
                request_key, done
            )
        )

    def _discard_room_key_request_task(
        self, request_key: str, task: asyncio.Task[None]
    ) -> None:
        """Forget a completed Megolm recovery task without removing a replacement."""
        if self._room_key_request_tasks.get(request_key) is task:
            self._room_key_request_tasks.pop(request_key, None)

    async def _request_missing_room_key(
        self,
        *,
        event: Any,
        event_id: str,
        room_id: str,
        session_id_tag: str,
    ) -> None:
        """Best-effort missing-key request with bounded retry.

        The callback must never make the sync loop unbounded.  Communication
        failures and explicit to-device error responses are retried with
        bounded exponential backoff.  Cancellation always propagates.
        """
        if not self._crypto_enabled or self._client is None:
            return
        if not isinstance(room_id, str) or not room_id.startswith("!"):
            return

        device_id = getattr(self._client, "device_id", None)
        user_id = getattr(self._client, "user_id", None)
        if not device_id or not user_id or not hasattr(event, "as_key_request"):
            return

        event.room_id = room_id  # nio workaround: MegolmEvents may lack room_id
        try:
            key_request = event.as_key_request(user_id, device_id)
        except Exception:
            self._room_key_request_failures += 1
            self._logger.debug(
                "Could not construct missing room-key request for %s",
                event_id,
                exc_info=True,
            )
            return

        for attempt in range(_ROOM_KEY_REQUEST_MAX_ATTEMPTS):
            self._room_key_request_attempts += 1
            retryable = False
            try:
                response = await asyncio.wait_for(
                    self._client.to_device(key_request),
                    timeout=_ROOM_KEY_REQUEST_TIMEOUT_SECONDS,
                )
                response_name = type(response).__name__
                errcode = getattr(response, "errcode", None)
                normalized_errcode = (
                    errcode.upper() if isinstance(errcode, str) else None
                )
                if normalized_errcode in MATRIX_PERMANENT_ERRCODES:
                    self._room_key_request_failures += 1
                    self._logger.debug(
                        "Missing room-key request permanently rejected for %s (%s)",
                        event_id,
                        normalized_errcode,
                    )
                    return
                retryable = bool(
                    response_name.endswith("Error")
                    or (isinstance(errcode, str) and errcode)
                )
                if not retryable:
                    self._room_key_request_successes += 1
                    self._logger.debug(
                        "Requested missing room key for session %s in %s "
                        "(attempt %d/%d)",
                        session_id_tag,
                        room_id,
                        attempt + 1,
                        _ROOM_KEY_REQUEST_MAX_ATTEMPTS,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError, OSError, ConnectionError):
                retryable = True
            except Exception:
                # Provider/programming errors are not transport recovery
                # signals.  Record the failure but do not spin on them.
                self._room_key_request_failures += 1
                self._logger.debug(
                    "Missing room-key request failed for %s",
                    event_id,
                    exc_info=True,
                )
                return

            if attempt >= _ROOM_KEY_REQUEST_MAX_ATTEMPTS - 1:
                self._room_key_request_failures += 1
                self._logger.warning(
                    "Missing room-key request exhausted %d attempts for event %s",
                    _ROOM_KEY_REQUEST_MAX_ATTEMPTS,
                    event_id,
                )
                return

            delay = _ROOM_KEY_REQUEST_BASE_DELAY_SECONDS * (2**attempt)
            await _sleep(delay)

    def _prune_undecryptable_dedup(self, now: float) -> None:
        """Evict expired entries from the live undecryptable dedup cache.

        Removes entries older than ``_UNDECRYPTABLE_DEDUP_WINDOW_SECS``
        to prevent unbounded growth over long-lived sessions.
        """
        cutoff = now - self._UNDECRYPTABLE_DEDUP_WINDOW_SECS
        self._undecryptable_dedup = {
            key: ts for key, ts in self._undecryptable_dedup.items() if ts >= cutoff
        }

    def _track_room_encrypted(self, room: Any, room_id: str) -> None:
        """Mark a room as encrypted in the room-state tracking cache.

        Extracted from _on_megolm_event / _on_room_encryption_event to
        avoid duplication.
        """
        if room is not None:
            rid = getattr(room, "room_id", None) or room_id
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

        Logging is deduplicated per room_id: the first event for a given
        room emits a DEBUG record; subsequent events for the same room
        are silently suppressed.  No INFO record is emitted by default.
        """
        self._encrypted_room_seen = True
        room_id = getattr(room, "room_id", "<unknown>") if room else "<unknown>"

        # Mark room as encrypted (always, regardless of logging)
        self._track_room_encrypted(room, room_id)

        # Deduped logging: first event per room at DEBUG, rest silent.
        if room_id not in self._encryption_event_seen_rooms:
            self._encryption_event_seen_rooms.add(room_id)
            self._logger.debug(
                "RoomEncryptionEvent received for room %s — room encryption enabled",
                room_id,
            )

    # Track rooms seen via sync (called by message callback wrapper)
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

    # -- Sync loop (Automatic Sync Recovery) -----------------------

    async def _run_sync(self) -> None:
        """Wrap ``_sync_with_reconnect`` — entry point for the sync task."""
        try:
            await self._sync_with_reconnect()
        except asyncio.CancelledError:
            return

    async def _sync_with_reconnect(self) -> None:
        """Supervise mindroom-nio ``sync_forever`` with bounded restarts.

        mindroom-nio owns Classic request retries, key sequencing, timeline
        parsing/decryption, limited-timeline recovery, and event provenance.
        MEDRE owns process-level restart/liveness plus the committed cursor.
        """
        while not self._stop_requested:
            try:
                self._reconnecting = False
                await self._client.sync_forever(
                    timeout=self._config.sync_timeout_ms,
                    since=self._committed_sync_token,
                    full_state=self._committed_sync_token is None,
                )
                if self._stop_requested:
                    return
                raise RuntimeError("Matrix sync_forever exited unexpectedly")
            except asyncio.CancelledError:
                self._reconnecting = False
                raise
            except Exception as exc:
                if self._stop_requested:
                    self._sync_failure = exc
                    self._reconnecting = False
                    return

                try:
                    if self._durable_sync_enabled:
                        if getattr(
                            self._client, "has_uncommitted_classic_sync_state", False
                        ):
                            await self._client.reset_classic_sync_state()
                        self._client.next_batch = self._committed_sync_token
                except asyncio.CancelledError:
                    raise
                except Exception as reset_exc:
                    self._logger.error(
                        "Failed to reset uncommitted Matrix sync state: %s",
                        reset_exc,
                    )
                    self._sync_failure = reset_exc
                    self._reconnecting = False
                    return

                self._reconnect_attempts += 1
                self._last_reconnect_error = str(exc)
                if self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
                    self._logger.error(
                        "Max sync reconnect attempts (%d) reached, giving up: %s",
                        _MAX_RECONNECT_ATTEMPTS,
                        exc,
                    )
                    self._sync_failure = exc
                    self._reconnecting = False
                    return

                self._reconnecting = True
                raw_delay = min(
                    _BACKOFF_BASE * (2 ** (self._reconnect_attempts - 1)),
                    _BACKOFF_CAP,
                )
                jitter = raw_delay * _BACKOFF_JITTER_FRACTION
                delay = max(0.0, raw_delay + random.uniform(-jitter, jitter))
                self._logger.warning(
                    "Matrix sync failed (attempt %d/%d); retrying in %.1fs: %s",
                    self._reconnect_attempts,
                    _MAX_RECONNECT_ATTEMPTS,
                    delay,
                    exc,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    self._reconnecting = False
                    raise

        self._reconnecting = False

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing, close the client.  Idempotent."""
        # Signal both MEDRE's supervisor and nio's inner sync loop.
        self._stop_requested = True
        if self._client is not None:
            stop_sync = getattr(self._client, "stop_sync_forever", None)
            if callable(stop_sync):
                stop_sync()

        # Cancel detached Megolm recovery before closing the client.
        recovery_tasks = list(self._room_key_request_tasks.values())
        if recovery_tasks:
            for task in recovery_tasks:
                task.cancel()
            self._room_key_request_tasks.clear()
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
            self._logger.debug(
                "Cancelled %d outstanding Megolm recovery task(s)",
                len(recovery_tasks),
            )

        # Cancel outstanding join tasks before closing the client.
        join_tasks = list(self._joining_rooms.values())
        if join_tasks:
            for t in join_tasks:
                t.cancel()
            self._joining_rooms.clear()
            for t in join_tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            self._logger.debug("Cancelled %d outstanding join task(s)", len(join_tasks))

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

        # Release the service's provider reference after caching its latest
        # secret-free diagnostics so a stopped session does not retain the SDK
        # client solely through the identity-policy object.
        if self._cross_signing_service is not None:
            self._cross_signing_diagnostics = self._cross_signing_service.diagnostics()
            self._cross_signing_service = None

        self._closed = True
        self._reconnecting = False
        self._live_sync_started = False
        # Reset reconnect counter so diagnostics are truthful after stop.
        self._reconnect_attempts = 0

    # -- Outbound send (per §31 §7.2 session owns all SDK interaction) -------

    async def room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[str, Any],
        ignore_unverified_devices: bool = False,
        tx_id: str | None = None,
    ) -> Any:
        """Send a message to a Matrix room through the session's client.

        Per §31 §7.2 the session is the sole owner of the SDK client.
        The adapter delegates all ``room_send`` calls through this method
        instead of accessing the client directly.

        Parameters
        ----------
        room_id:
            Target room ID.
        message_type:
            Matrix message type (e.g. ``"m.room.message"``).
        content:
            Event content dict.
        ignore_unverified_devices:
            Whether to send to unverified devices (E2EE workaround).
        tx_id:
            Transaction ID for idempotent sends.

        Returns
        -------
        Any
            The nio ``RoomSendResponse`` (or equivalent from test fakes).

        Raises
        ------
        MatrixConnectionError
            If the client is not initialised.
        """
        if self._client is None:
            raise MatrixConnectionError("cannot send: client is not connected")
        return await self._client.room_send(
            room_id=room_id,
            message_type=message_type,
            content=content,
            ignore_unverified_devices=ignore_unverified_devices,
            tx_id=tx_id,
        )

    # -- Diagnostics ----------------------------------------------------------

    def diagnostics(self) -> MatrixSessionDiagnostics:
        """Return a read-only snapshot of session state.

        Never exposes secrets, access tokens, keys, or private device
        material.
        """
        # Compute E2EE diagnostics from live client state.
        # Only inspect nio crypto internals when crypto is enabled;
        # in plaintext mode (or with mock clients that auto-create
        # attributes) these would give false positives.
        if self._crypto_enabled and self._client is not None:
            olm_loaded = self._client.olm is not None
            store_loaded = self._client.store is not None
            device_keys_uploaded = (
                not self._client.should_upload_keys
                if hasattr(self._client, "should_upload_keys")
                else False
            )
            key_query_needed = (
                self._client.should_query_keys
                if hasattr(self._client, "should_query_keys")
                else False
            )
            device_id_in_use = (
                str(self._client.device_id)
                if getattr(self._client, "device_id", None)
                else None
            )
        else:
            olm_loaded = False
            store_loaded = False
            device_keys_uploaded = False
            key_query_needed = False
            device_id_in_use = (
                str(self._client.device_id)
                if self._client and getattr(self._client, "device_id", None)
                else None
            )
        store_path_exists = (
            os.path.isdir(self._config.store_path) if self._config.store_path else False
        )
        cross_signing = (
            self._cross_signing_service.diagnostics()
            if self._cross_signing_service is not None
            else self._cross_signing_diagnostics
        )

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
            megolm_recovery_attempts=self._room_key_request_attempts,
            megolm_recovery_successes=self._room_key_request_successes,
            megolm_recovery_failures=self._room_key_request_failures,
            # Sync recovery
            sync_running=self.sync_running,
            reconnecting=self._reconnecting,
            reconnect_attempts=self._reconnect_attempts,
            last_successful_sync=self._last_successful_sync,
            checkpoint_owned_by_medre=self._durable_sync_enabled,
            committed_checkpoint_present=self._committed_sync_token is not None,
            recovered_event_count=self._recovered_event_count,
            history_event_count=self._history_event_count,
            recovery_abandoned_room_count=len(self._recovery_abandoned_rooms),
            recovery_last_abandonment=self._recovery_last_abandonment,
            # Truthful crypto_store_loaded based on live state
            crypto_store_loaded=olm_loaded and store_loaded,
            # Room-state tracking
            encrypted_room_count=self.encrypted_room_count,
            plaintext_room_count=self.plaintext_room_count,
            # E2EE key management diagnostics
            olm_loaded=olm_loaded,
            store_loaded=store_loaded,
            device_keys_uploaded=device_keys_uploaded,
            key_query_needed=key_query_needed,
            device_id_in_use=device_id_in_use,
            store_path_exists=store_path_exists,
            initial_sync_completed=self._initial_sync_done,
            cross_signing_provider_supported=cross_signing.provider_supported,
            cross_signing_local_identity_present=cross_signing.local_identity_present,
            cross_signing_server_identity_present=cross_signing.server_identity_present,
            cross_signing_current_device_self_signed=(
                cross_signing.current_device_self_signed
            ),
            cross_signing_chain_status=cross_signing.chain_status,
            cross_signing_repair_required=cross_signing.repair_required,
            cross_signing_reset_required=cross_signing.reset_required,
            cross_signing_last_failure_category=(cross_signing.last_failure_category),
        )
