"""SQLite-backed storage package for medre.

Submodules decompose the storage layer into focused concerns:

* :mod:`constants` — public tuning constants
* :mod:`schema` — DDL, indexes, and schema-version metadata
* :mod:`statements` — prepared SQL statement strings
* :mod:`storage` — :class:`SQLiteStorage` implementation
"""

from __future__ import annotations

from medre.core.storage.sqlite.storage import SQLiteStorage

__all__ = ["SQLiteStorage"]
