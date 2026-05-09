"""Matrix session lifecycle boundary.

:class:`MatrixSession` owns the nio ``AsyncClient`` lifecycle: construction,
login restoration, event-callback registration, sync task management, and
graceful teardown.  The adapter delegates all client ownership to this
session object.

E2EE support: when ``HAS_E2EE`` is ``True`` and ``store_path``/``device_id``
are configured, the session enables crypto via nio's built-in encryption.
Decrypted inbound text events pass through the normal message callback;
undecryptable encrypted events are counted and logged but not forwarded.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from medre.adapters.matrix import compat as _compat_mod
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixConnectionError

_logger = logging.getLogger(__name__)


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


class MatrixSession:
    """Adapter-owned Matrix session lifecycle boundary.

    Owns the ``nio.AsyncClient`` and manages its full lifecycle:
    creation, login restoration, callback registration, sync loop,
    and graceful teardown.

    Parameters
    ----------
    config:
        Validated :class:`~medre.adapters.matrix.config.MatrixConfig`.
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
    )

    def __init__(
        self,
        config: MatrixConfig,
        message_callback: Callable[..., Any] | None = None,
        logger: logging.Logger | None = None,
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

    # -- Properties -----------------------------------------------------------

    @property
    def client(self) -> Any:
        """The underlying ``nio.AsyncClient``, or ``None`` if not started."""
        return self._client

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

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Create the nio client, restore login, register callbacks, start sync.

        E2EE startup depends on ``encryption_mode``:

        * ``plaintext`` — standard client, no crypto.
        * ``e2ee_required`` — asserts ``HAS_E2EE``, ``store_path``,
          ``device_id`` and enables encryption.  Raises on any missing
          prerequisite.
        * ``e2ee_optional`` — enables crypto when deps/store/device are
          present; falls back to plaintext otherwise.

        Raises
        ------
        MatrixConnectionError
            If the client cannot authenticate, E2EE prerequisites are
            unmet in ``e2ee_required`` mode, or the sync task cannot
            be created.
        """
        self._sync_failure = None
        self._closed = False
        self._crypto_enabled = False
        self._encrypted_room_seen = False
        self._undecryptable_event_count = 0
        self._last_crypto_error = None

        mode = self._config.encryption_mode
        if mode == "e2ee_required":
            await self._start_e2ee_required()
        elif mode == "e2ee_optional":
            await self._start_e2ee_optional()
        else:
            await self._start_plaintext()

    async def _start_plaintext(self) -> None:
        """Standard plaintext startup — no crypto."""
        import nio

        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=self._config.device_id or "",
            store_path=self._config.store_path,
        )
        self._client.restore_login(
            user_id=self._config.user_id,
            device_id=self._config.device_id or "",
            access_token=self._config.access_token,
        )
        await self._finalize_start()

    async def _start_e2ee_required(self) -> None:
        """E2EE-required startup.

        Pre-conditions (already validated by config but re-checked):
        * ``HAS_E2EE`` is ``True``
        * ``store_path`` is set
        * ``device_id`` is set

        Enables crypto via ``nio.AsyncClient(encryption_enabled=True)``.
        """
        if not _compat_mod.HAS_E2EE:
            raise MatrixConnectionError(
                "mindroom-nio[e2e] not installed; "
                "e2ee_required mode requires crypto dependencies"
            )
        if not self._config.store_path:
            raise MatrixConnectionError(
                "e2ee_required mode requires a store_path"
            )
        if not self._config.device_id:
            raise MatrixConnectionError(
                "e2ee_required mode requires a device_id"
            )

        import nio

        try:
            client_config: Any = nio.ClientConfig(encryption_enabled=True)
        except Exception as exc:
            raise MatrixConnectionError(
                f"Failed to configure E2EE: {exc}"
            ) from exc

        self._client = nio.AsyncClient(
            homeserver=self._config.homeserver,
            user=self._config.user_id,
            device_id=self._config.device_id,
            store_path=self._config.store_path,
            config=client_config,
        )
        self._client.restore_login(
            user_id=self._config.user_id,
            device_id=self._config.device_id,
            access_token=self._config.access_token,
        )

        if not getattr(self._client, "logged_in", False):
            await self._client.close()
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        self._crypto_enabled = True
        await self._finalize_start()

    async def _start_e2ee_optional(self) -> None:
        """E2EE-optional startup.

        If ``HAS_E2EE`` is ``True`` and ``store_path``/``device_id`` are
        configured, attempt crypto setup.  On failure, log a warning and
        fall back to plaintext with ``crypto_enabled=False``.
        """
        can_attempt_crypto = (
            _compat_mod.HAS_E2EE
            and self._config.store_path is not None
            and self._config.device_id is not None
        )

        if can_attempt_crypto:
            try:
                await self._start_e2ee_required()
                return  # crypto start succeeded
            except Exception as exc:
                self._logger.warning(
                    "E2EE optional setup failed, falling back to "
                    "plaintext: %s", exc,
                )
                self._crypto_enabled = False
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

    async def _finalize_start(self) -> None:
        """Common post-client-creation steps: validate login, register
        callbacks, start sync task."""
        if not getattr(self._client, "logged_in", False):
            await self._client.close()
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

        # Register MegolmEvent callback for undecryptable encrypted events.
        self._register_megolm_callback()

        sync_coro = self._run_sync()
        try:
            self._sync_task = asyncio.create_task(sync_coro)
        except Exception as exc:
            sync_coro.close()
            await self._client.close()
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

    async def _on_megolm_event(self, room: Any, event: Any) -> None:
        """Handle an undecryptable MegolmEvent.

        Counts the event, records the last crypto error, logs a warning,
        but does NOT crash or forward to the adapter message callback.
        """
        self._undecryptable_event_count += 1
        event_id = getattr(event, "event_id", "<unknown>")
        room_id = getattr(room, "room_id", "<unknown>") if room else "<unknown>"

        self._last_crypto_error = (
            f"Undecryptable MegolmEvent {event_id} in {room_id}"
        )

        self._encrypted_room_seen = True
        self._logger.warning(
            "Undecryptable MegolmEvent %s in room %s",
            event_id, room_id,
        )

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

    async def _run_sync(self) -> None:
        """Wrap ``sync_forever`` and record any failure."""
        try:
            await self._client.sync_forever(
                timeout=self._config.sync_timeout_ms,
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._logger.error(
                "Matrix sync task failed: %s", exc,
            )
            self._sync_failure = exc

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop syncing, close the client.  Idempotent."""
        if self._sync_task is not None:
            if not self._sync_task.done():
                self._sync_task.cancel()
                try:
                    await asyncio.wait_for(self._sync_task, timeout=timeout)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    self._logger.warning(
                        "Sync task did not stop within %.1fs", timeout,
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
                    "Error stopping sync_forever: %s", exc,
                )
            try:
                await self._client.close()
            except Exception as exc:
                self._logger.warning(
                    "Error closing client: %s", exc,
                )
            self._client = None

        self._closed = True

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
        )
