"""Own the LXMF harness process tree through timeout and cancellation."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress


async def run_owned_scenario(
    command: Sequence[str], *, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Use the pinned provider's POSIX process-group lifetime reservation."""
    from medre.adapters.lxmf.errors import LxmfConnectionError
    from medre.adapters.lxmf.process_group import OwnedProcessGroup

    owner = OwnedProcessGroup()
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    startup = asyncio.create_task(owner.start())
    try:
        # Preserve ownership even if cancellation lands during child creation.
        await asyncio.shield(startup)
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                process_group=owner.pid,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=timeout
            )
        except TimeoutError:
            raise subprocess.TimeoutExpired(command, timeout) from None
        assert process.returncode is not None
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    finally:
        try:
            await startup
        finally:
            try:
                with suppress(LxmfConnectionError):
                    await owner.detach()
            finally:
                with suppress(LxmfConnectionError):
                    owner.kill()
                try:
                    if communication is not None:
                        await communication
                    if process is not None:
                        await process.wait()
                finally:
                    await owner.close()
