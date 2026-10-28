"""Private, atomic publication of explicitly requested CLI output files."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def _sync_directory(path: Path) -> None:
    """Best-effort persistence of a published name where directory sync exists."""
    if os.name == "nt":
        return
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_private_output(path: Path, payload: bytes) -> None:
    """Publish a complete private file without replacing an existing path.

    Write and sync a mode-0600 staging file in the target directory, then
    create the destination name with an exclusive hard link. A failure before
    publication cannot expose a partial final output, and a concurrent writer
    retains its existing file. The staging name is always cleaned up on normal
    failures, including cancellation; Windows inherits its parent ACLs.
    """
    descriptor, staging = tempfile.mkstemp(prefix=".medre-export-", dir=path.parent)
    owned_descriptor: int | None = descriptor
    try:
        stream = os.fdopen(descriptor, "wb")
        owned_descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # The target appears only after its complete contents are synced.
        # os.link fails with FileExistsError for existing files and symlinks.
        os.link(staging, path)
        _sync_directory(path.parent)
    finally:
        if owned_descriptor is not None:
            with suppress(OSError):
                os.close(owned_descriptor)
        with suppress(OSError):
            os.unlink(staging)
