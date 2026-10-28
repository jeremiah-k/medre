"""Private, exclusive publication of explicitly requested CLI output files."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def write_private_output(path: Path, payload: bytes) -> None:
    """Write one private file without replacing a path or deleting another writer.

    Exclusive creation rejects existing files, symlinks and concurrent creators.
    Descriptor ownership stays explicit until fdopen succeeds. Interrupted or
    failed writes close the descriptor and remove only the inode created here;
    a concurrent replacement is retained. Windows uses inherited directory ACLs.
    """
    opened_descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    descriptor: int | None = opened_descriptor
    identity: os.stat_result | None = None
    try:
        identity = os.fstat(opened_descriptor)
        stream = os.fdopen(opened_descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor is not None:
            if identity is None:
                with suppress(OSError):
                    identity = os.stat(descriptor)
            with suppress(OSError):
                os.close(descriptor)
        if identity is not None:
            with suppress(OSError):
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    path.unlink()
        raise
