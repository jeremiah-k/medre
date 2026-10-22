"""Own native peers, their command channels, and private evidence streams."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

_STOP_TIMEOUT = 20
_DRAIN_TIMEOUT = 5


class Peer:
    def __init__(self, name: str, directory: Path, timeout: float) -> None:
        """Prepare a peer whose command and evidence waits use timeout seconds."""
        self.name = name
        self.directory = directory
        self.timeout = timeout
        self.records: list[dict] = []
        self.process = None
        self.reader = None
        self.log = None
        self.errors = None
        self.output = None
        self.changed = asyncio.Event()
        self.failure: Exception | None = None
        self._expected_exit: int | None = None

    async def start(self, settings: dict, *, argv: list[str] | None = None) -> None:
        """Send credentials over stdin, never the process command line."""
        self.log = (self.directory / f"{self.name}.jsonl").open("x")
        self.errors = (self.directory / f"{self.name}.stderr").open("x")
        self.output = (self.directory / f"{self.name}.stdout").open("x")
        self.process = await asyncio.create_subprocess_exec(
            *(argv or [sys.executable, "-m", "scripts.adapter_campaign.worker"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.errors,
            limit=64 * 1024,
            start_new_session=sys.platform != "win32",
        )
        self.reader = asyncio.create_task(self._read())
        await self._write(settings)
        await self.wait(lambda record: record["kind"] == "ready")

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                # SDK stdout is not evidence and may contain private data.
                if not line.startswith(b"MRPEER "):
                    self.output.write(line.decode(errors="replace"))
                    self.output.flush()
                    continue
                record = json.loads(line[7:])
                if not isinstance(record, dict) or "kind" not in record:
                    raise ValueError("Malformed native peer record")
                if len(self.records) >= 1024:
                    raise ValueError("Native peer evidence window exhausted")
                self.records.append(record)
                self.log.write(json.dumps(record) + "\n")
                self.log.flush()
                self.changed.set()
            await self.process.wait()
        except Exception:
            self.failure = RuntimeError(f"{self.name}: peer evidence stream failed")
        finally:
            self.changed.set()

    async def _write(self, command: dict) -> None:
        async with asyncio.timeout(self.timeout):
            self.process.stdin.write((json.dumps(command) + "\n").encode())
            await self.process.stdin.drain()

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure
        if any(record["kind"] == "error" for record in self.records):
            raise RuntimeError(f"{self.name}: native peer reported failure")
        if self.reader is not None and self.reader.done():
            raise RuntimeError(f"{self.name}: native peer exited during the campaign")

    async def wait(self, predicate: Callable[[dict], bool]) -> dict:
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    self.changed.clear()
                    self.check()
                    for record in self.records:
                        if predicate(record):
                            return record
                    await self.changed.wait()
        except TimeoutError as exc:
            raise TimeoutError(f"{self.name}: native evidence wait timed out") from exc

    async def send(self, text: str) -> dict:
        return await self.command("send", text=text)

    async def synchronize(self) -> None:
        await self.command("synchronize")

    async def command(self, operation: str, **data) -> dict:
        command = uuid4().hex
        await self._write({"op": operation, "id": command, **data})
        return await self.wait(
            lambda record: record["kind"] == "sent" and record["command"] == command
        )

    async def crash(self) -> int:
        """Kill an owned live process at a qualified fault barrier and reap it.

        Only a kill initiated here can become an expected nonzero exit. An
        already-failed child remains a qualification failure.
        """
        self.check()
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError(f"{self.name}: no live process to crash")
        requested_at = time.time()
        self._signal(force=True)
        code = await asyncio.wait_for(self.process.wait(), _DRAIN_TIMEOUT)
        expected = 1 if sys.platform == "win32" else -signal.SIGKILL
        if code != expected:
            raise RuntimeError(f"{self.name}: unexpected crash exit")
        if self.reader is not None:
            await asyncio.wait_for(asyncio.shield(self.reader), _DRAIN_TIMEOUT)
        if self.failure or any(record["kind"] == "error" for record in self.records):
            raise RuntimeError(f"{self.name}: evidence failed before intentional crash")
        self._expected_exit = code
        record = {
            "kind": "intentional_crash",
            "exit_code": code,
            "requested_at": requested_at,
            "at": time.time(),
        }
        self.records.append(record)
        self.log.write(json.dumps(record) + "\n")
        self.log.flush()
        return code

    async def stop(self) -> None:
        """Always reap the child; forced cleanup prevents a passing verdict.

        Accept a nonzero exit only when recorded by crash(). Close evidence
        streams even on failure. Cleanup failures raise RuntimeError; exhausted
        process or reader waits can propagate TimeoutError.
        """
        forced = False
        try:
            if self.process is not None:
                if self.process.returncode is None:
                    try:
                        async with asyncio.timeout(_STOP_TIMEOUT):
                            await self._write({"op": "stop"})
                            await self.process.wait()
                    except (TimeoutError, BrokenPipeError, ConnectionResetError):
                        forced = True
                        if self.process.returncode is None:
                            self._signal()
                            try:
                                await asyncio.wait_for(self.process.wait(), 5)
                            except TimeoutError:
                                self._signal(force=True)
                                await asyncio.wait_for(
                                    self.process.wait(), _DRAIN_TIMEOUT
                                )
                if self.reader is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self.reader), _DRAIN_TIMEOUT
                        )
                    except TimeoutError:
                        forced = True
                        self._signal(force=True)
                        await asyncio.wait_for(self.reader, _DRAIN_TIMEOUT)
                if (
                    forced
                    or self.process.returncode not in (0, self._expected_exit)
                    or self.failure
                    or any(record["kind"] == "error" for record in self.records)
                ):
                    raise RuntimeError(f"{self.name}: native peer cleanup failed")
        finally:
            if self.process is not None:
                # Cancellation and partial startup also leave owned children.
                # Reap them before closing evidence streams or returning.
                self._signal(force=True)
                if self.process.returncode is None:
                    await asyncio.wait_for(self.process.wait(), _DRAIN_TIMEOUT)
            if self.reader is not None and not self.reader.done():
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            for stream in (self.log, self.errors, self.output):
                if stream is not None:
                    stream.close()

    def _signal(self, *, force: bool = False) -> None:
        if sys.platform == "win32":
            if self.process.returncode is not None:
                return
            if force:
                self.process.kill()
            else:
                self.process.terminate()
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
