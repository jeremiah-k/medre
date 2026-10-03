"""Release the nio provider and its separately owned crypto database."""

from __future__ import annotations

import inspect
import logging


async def close_matrix_client(
    client: object, logger: logging.Logger | None = None
) -> None:
    """Drain provider close before releasing its store, including on failure.

    The pinned nio client closes HTTP and drains recovery callbacks, but
    leaves the store's SQLite connection open. Keep both operations in the
    same task so a bounded caller can retain cleanup ownership until close
    settles. nio re-raises cancellation after its recovery drain completes.
    """
    try:
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
    finally:
        close_matrix_store(client, logger)


def close_matrix_store(
    client: object, logger: logging.Logger | None = None
) -> None:
    """Close the database opened by nio's MatrixStore without deleting state."""
    database = getattr(getattr(client, "store", None), "database", None)
    if database is None:
        return
    try:
        stop = getattr(database, "stop", None)
        is_stopped = getattr(database, "is_stopped", None)
        if callable(stop) and callable(is_stopped) and not is_stopped():
            stop()
        close = getattr(database, "close", None)
        is_closed = getattr(database, "is_closed", None)
        if callable(close) and (not callable(is_closed) or not is_closed()):
            close()
    except Exception:
        (logger or logging.getLogger(__name__)).warning(
            "Matrix crypto store close failed", exc_info=True
        )
