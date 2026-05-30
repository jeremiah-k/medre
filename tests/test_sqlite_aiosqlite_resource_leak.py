"""Regression tests: aiosqlite branches of initialize() and open_readonly()
close connections on failure — covering the ``except BaseException: await db.close()``
paths in ``SQLiteStorage``.
"""

from __future__ import annotations

import gc
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _temp_db_path(tmp_path: Path) -> str:
    """Return a unique temporary database path under *tmp_path*."""
    return str(tmp_path / "test_aiosqlite_resource_leak.db")


# ---------------------------------------------------------------------------
# Force aiosqlite code paths by patching the module-level ``aiosqlite``
# reference and ``_HAS_AIOSQLITE`` flag.  Uses autouse so every test below
# exercises the aiosqlite branch regardless of whether the real ``aiosqlite``
# package is installed.
# ---------------------------------------------------------------------------

#: Global mock aiosqlite *module* — individual tests set ``.connect`` to
#: return per-scenario connection mocks.
_mock_aiosqlite_module = MagicMock(name="aiosqlite")


@pytest.fixture(autouse=True)
def _force_aiosqlite_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("medre.core.storage.sqlite._HAS_AIOSQLITE", True)
    monkeypatch.setattr("medre.core.storage.sqlite.aiosqlite", _mock_aiosqlite_module)


# ---------------------------------------------------------------------------
# Tests: aiosqlite initialize() — execution failure (lines 736-746)
# ---------------------------------------------------------------------------


class TestAiosqliteInitializeFailure:
    """aiosqlite ``initialize()`` must close the connection when DDL fails."""

    async def test_aiosqlite_initialize_executescript_failure_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """When ``executescript(_SCHEMA)`` raises, ``db.close()`` is awaited
        and the exception propagates."""
        db_path = _temp_db_path(tmp_path)

        mock_conn = MagicMock()
        mock_conn.close = AsyncMock()
        mock_conn.commit = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executescript = AsyncMock(
            side_effect=sqlite3.OperationalError("injected executescript failure")
        )

        _mock_aiosqlite_module.connect = AsyncMock(return_value=mock_conn)

        storage = SQLiteStorage(db_path=db_path)

        with pytest.raises(sqlite3.OperationalError, match="executescript failure"):
            await storage.initialize()

        mock_conn.close.assert_awaited_once()
        gc.collect()

    async def test_aiosqlite_initialize_commit_failure_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """When ``commit()`` raises, ``db.close()`` is awaited and the
        exception propagates."""
        db_path = _temp_db_path(tmp_path)

        mock_conn = MagicMock()
        mock_conn.close = AsyncMock()
        mock_conn.executescript = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock(
            side_effect=sqlite3.OperationalError("injected commit failure")
        )

        _mock_aiosqlite_module.connect = AsyncMock(return_value=mock_conn)

        storage = SQLiteStorage(db_path=db_path)

        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            await storage.initialize()

        mock_conn.close.assert_awaited_once()
        gc.collect()


# ---------------------------------------------------------------------------
# Tests: aiosqlite open_readonly() — row_factory failure (lines 897-904)
# ---------------------------------------------------------------------------


class TestAiosqliteOpenReadonlyRowFactoryFailure:
    """aiosqlite ``open_readonly()`` must close the connection when
    ``db.row_factory = sqlite3.Row`` fails."""

    async def test_aiosqlite_open_readonly_row_factory_failure_closes_connection(
        self, tmp_path: Path
    ) -> None:
        """When setting ``row_factory`` raises inside the aiosqlite branch
        of ``open_readonly()``, ``db.close()`` is awaited and the exception
        propagates."""
        db_path = _temp_db_path(tmp_path)

        # Create a valid database file so open_readonly() passes the
        # file-existence guard.
        # Use the sync fallback to create the db (override the autouse fixture
        # for this setup step).
        with patch("medre.core.storage.sqlite._HAS_AIOSQLITE", False), patch(
            "medre.core.storage.sqlite.aiosqlite", None
        ):
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()
            await storage.close()
            del storage

        gc.collect()

        # Now test the aiosqlite path — mock aiosqlite.connect to return a
        # connection whose ``row_factory`` setter raises.
        class _ConnWithFailingRowFactory:
            def __init__(self):
                self.close = AsyncMock()

            @property
            def row_factory(self):
                return None

            @row_factory.setter
            def row_factory(self, _value):
                raise RuntimeError("simulated row_factory assignment failure")

        mock_conn = _ConnWithFailingRowFactory()

        _mock_aiosqlite_module.connect = AsyncMock(return_value=mock_conn)

        # --- The _force_aiosqlite_path fixture is active here, so
        #     SQLiteStorage will use the aiosqlite branch.
        with pytest.raises(RuntimeError, match="simulated row_factory"):
            await SQLiteStorage.open_readonly(db_path)

        mock_conn.close.assert_awaited_once()
        gc.collect()
