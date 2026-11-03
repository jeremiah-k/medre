# medre-test-tier: compatibility
"""A failed LXMF harness cannot leave its independent receiver running."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.lxmf_scenario import run_owned_scenario

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX harness ownership")

_RECEIVER = """
import socket, sys, threading
from pathlib import Path
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    Path(sys.argv[1]).write_text(str(listener.getsockname()[1]))
    threading.Event().wait()
"""


def _command(tmp_path: Path) -> list[str]:
    receiver = tmp_path / "receiver.py"
    receiver.write_text(_RECEIVER)
    return [
        sys.executable,
        "-c",
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]]).wait()",
        str(receiver),
        str(tmp_path / "port"),
    ]


def _assert_receiver_stopped(tmp_path: Path) -> None:
    # A live child owns this port even if the outer sender was killed.
    port = int((tmp_path / "port").read_text())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


async def test_timeout_stops_the_sender_and_its_receiver(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        await run_owned_scenario(_command(tmp_path), env=os.environ, timeout=5)
    _assert_receiver_stopped(tmp_path)


async def test_cancellation_stops_the_sender_and_its_receiver(tmp_path: Path) -> None:
    task = asyncio.create_task(
        run_owned_scenario(_command(tmp_path), env=os.environ, timeout=30)
    )
    try:
        assert await wait_until(lambda: (tmp_path / "port").is_file())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    _assert_receiver_stopped(tmp_path)
