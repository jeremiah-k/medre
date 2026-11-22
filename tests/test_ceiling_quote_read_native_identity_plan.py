"""The owned quote read probes indexed native identities, not a fallback scan.

The canonical source-alias fallback inside the excerpt read fires only for a
native tuple with no published reference row. On the supported schema that
correlated probe must use the native identity index declared with the
``native_message_refs`` uniqueness constraint, so routine owned reads do not
grow a scan with the event history.
"""

from __future__ import annotations

from medre.core.storage.sqlite._content_grants import _QUOTED_EXCERPT_READ_SQL
from medre.core.storage.sqlite.storage import SQLiteStorage

_FUTURE_READ_INSTANT = "2999-01-01T00:00:00.000000+00:00"


async def test_quote_read_targets_the_native_identity_index(
    temp_storage: SQLiteStorage,
) -> None:
    """The alias fallback is an indexed probe on the native identity tuple."""
    details = await temp_storage._read_all(
        "EXPLAIN QUERY PLAN " + _QUOTED_EXCERPT_READ_SQL,
        (_FUTURE_READ_INSTANT, "plan-probe-unrelated-event"),
    )
    plan = " ".join(str(row["detail"]) for row in details)
    assert "SCAN native_message_refs" not in plan
    assert "SCAN n" not in plan
    # The uniqueness constraint's implicit index serves the tuple probe.
    assert "sqlite_autoindex_native_message_refs_2" in plan
    assert "adapter=? AND native_channel_id=? AND native_message_id=?" in plan
