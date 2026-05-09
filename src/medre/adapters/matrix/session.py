"""Matrix session lifecycle boundary.

:class:`MatrixSession` owns the nio ``AsyncClient`` lifecycle: construction,
login restoration, event-callback registration, sync task management, and
graceful teardown.  The adapter delegates all client ownership to this
session object.

E2EE is scaffolded but not yet implemented.  Crypto fields are exposed
as read-only diagnostics (always ``crypto_enabled=False``).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixConnectionError


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
    """

    __slots__ = (
        "_config",
        "_client",
        "_sync_task",
        "_sync_failure",
        "_message_callback",
        "_closed",
    )

    def __init__(
        self,
        config: MatrixConfig,
        message_callback: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config
        self._client: Any = None
        self._sync_task: asyncio.Task | None = None
        self._sync_failure: Exception | None = None
        self._message_callback = message_callback
        self._closed = False

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

    # -- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Create the nio client, restore login, register callbacks, start sync.

        Raises
        ------
        MatrixConnectionError
            If the client cannot authenticate or the sync task cannot
            be created.
        """
        self._sync_failure = None
        self._closed = False

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

        if not getattr(self._client, "logged_in", False):
            await self._client.close()
            self._client = None
            raise MatrixConnectionError(
                f"failed to authenticate as {self._config.user_id} "
                f"on {self._config.homeserver}"
            )

        if self._message_callback is not None:
            self._client.add_event_callback(
                self._message_callback,
                (nio.RoomMessageText, nio.RoomMessageNotice, nio.RoomMessageEmote),
            )

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

    async def _run_sync(self) -> None:
        """Wrap ``sync_forever`` and record any failure."""
        try:
            await self._client.sync_forever(
                timeout=self._config.sync_timeout_ms,
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
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
                    pass
            try:
                self._sync_task.exception()
            except (asyncio.CancelledError, Exception):
                pass
            self._sync_task = None

        if self._client is not None:
            try:
                self._client.stop_sync_forever()
            except Exception:
                pass
            try:
                await self._client.close()
            except Exception:
                pass
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
            crypto_enabled=False,
            last_crypto_error=None,
            encrypted_room_seen=False,
            undecryptable_event_count=0,
        )
