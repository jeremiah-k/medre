"""Small, opt-in process heartbeat; never sample adapters or query storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from medre.runtime.app import MedreApp

SCHEMA = "medre.runtime-status.v1"
INTERVAL_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 1.0
_MAX_EXISTING_BYTES = 16_384
logger = logging.getLogger(__name__)


class RuntimeStatusFile:
    """Own one output file for the duration of a CLI run.

    The heartbeat describes process progress, not fresh transport sampling.
    Existing unrelated files are never intentionally replaced. Use a dedicated,
    private directory with one writer; atomic replacement is not a writer lock.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.run_id = uuid.uuid4().hex
        self.phase = "starting"
        self.app: MedreApp | None = None
        self._write_failed = False
        self._initialized = False
        self._publication_lock = asyncio.Lock()
        self._pending_write: asyncio.Future[Exception | None] | None = None
        self._retired = threading.Event()

    def _check_destination(self) -> None:
        # Open without following the final symlink or blocking on a FIFO
        # substituted between the path check and the open. The directory must
        # still be private and have a single writer.
        if self.path.is_symlink():
            raise ValueError("status file must not be a symlink")
        try:
            fd = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("status destination must be a regular file")
                data = stream.read(_MAX_EXISTING_BYTES + 1)
            previous = json.loads(data) if len(data) <= _MAX_EXISTING_BYTES else None
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ValueError(
                "status destination is not a regular status JSON file"
            ) from exc
        if not isinstance(previous, dict) or previous.get("schema") != SCHEMA:
            raise ValueError("status destination contains unrelated data")

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "process_id": os.getpid(),
            "updated_at": time.time(),
            "phase": self.phase,
            "health_scope": "unavailable",
            "runtime_health": None,
            "health_observed_at": None,
            "adapters_started": None,
            "startup_routes_registered": None,
            "startup_routes_blocked": None,
        }
        app = self.app
        if app is None:
            return result
        eligibility, readiness = app.route_eligibility, app.startup_readiness
        if eligibility is not None and readiness is not None:
            registered = set(eligibility.registered)
            result["startup_routes_registered"] = len(registered)
            result["startup_routes_blocked"] = len(
                registered & {route.route_id for route in readiness.skipped}
            )
        if self.phase != "running":
            return result
        result["adapters_started"] = len(app.started_adapter_ids)
        live = app.live_health
        if live is not None:
            result["health_scope"] = "retained"
            result["runtime_health"] = live.runtime_health
            # The aggregate publication time is not the freshness of each adapter.
            result["health_observed_at"] = datetime.fromisoformat(
                live.poll_timestamp_wall
            ).timestamp()
        elif app.boot_summary is not None:
            result["health_scope"] = "startup"
            result["runtime_health"] = app.boot_summary.runtime_health
        return result

    def _write(self, observation: dict[str, Any]) -> None:
        self._check_destination()
        payload = json.dumps(observation, allow_nan=False, separators=(",", ":"))
        fd, name = tempfile.mkstemp(prefix=".medre-status-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            # Windows refuses replacing a destination an open reader holds.
            if not self._retired.is_set():
                os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    async def _write_observation(self) -> None:
        # A filesystem can stall indefinitely. Keep at most one worker and
        # never join it on the runtime's event loop or default executor.
        # Snapshot on the owner loop before passing JSON-shaped data to it.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WRITE_TIMEOUT_SECONDS
        if self._pending_write is not None:
            await asyncio.wait_for(
                asyncio.shield(self._pending_write),
                timeout=max(0, deadline - loop.time()),
            )
        observation = self.snapshot()
        pending: asyncio.Future[Exception | None] = loop.create_future()
        self._pending_write = pending

        def write() -> None:
            error: Exception | None = None
            try:
                self._write(observation)
            except Exception as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(pending.set_result, error)
            except RuntimeError:
                # The loop may have closed while the filesystem was stalled.
                pass

        threading.Thread(target=write, name="medre-status", daemon=True).start()
        error = await asyncio.wait_for(
            asyncio.shield(pending), timeout=max(0, deadline - loop.time())
        )
        if error is not None:
            raise error

    def close(self) -> None:
        """Retire outstanding work; a stalled filesystem cannot hold shutdown."""
        self._retired.set()

    async def initialize(self) -> None:
        """Fail before opening any adapter if the requested output is unusable."""
        async with self._publication_lock:
            await self._write_observation()
            self._initialized = True

    async def publish(self, phase: str | None = None) -> None:
        async with self._publication_lock:
            if self._retired.is_set():
                return
            if phase is not None:
                self.phase = phase
            if not self._initialized:
                return
            try:
                await self._write_observation()
            except Exception:
                if not self._write_failed:
                    logger.warning(
                        "Runtime status file update failed; heartbeat may be stale"
                    )
                self._write_failed = True
            else:
                if self._write_failed:
                    logger.info("Runtime status file updates resumed")
                self._write_failed = False

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            await self.publish()
