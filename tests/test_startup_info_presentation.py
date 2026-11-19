# medre-test-tier: compatibility
"""Startup-info presentation: derived labels and the row-truncation marker.

Covers the renderer-side presentation rules in
``docs/spec/adapter-startup-info.md``: unlabeled facts render with a label
derived from their field name, explicit provider labels win, and rows beyond
the display cap truncate to the first ten behind one ``… +N more`` marker
line. See ``tests/test_adapter_startup_info.py`` for the rest of the
startup-info contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from medre.core.startup_info import (
    CACHE_FILENAME,
    AdapterStartupInfo,
    collect_startup_info,
    read_cache_file,
    render_startup_info_block,
    write_cache_file,
)

_TIMESTAMP = "2026-11-19T00:00:00+00:00"


async def _collect_values(values: object) -> AdapterStartupInfo | None:
    """Collect through the same fake-provider seam the contract tests use."""
    return await collect_startup_info(
        "main",
        "matrix",
        SimpleNamespace(startup_info=lambda: values),
        emitted_at=_TIMESTAMP,
    )


async def test_unlabeled_row_facts_render_derived_labels() -> None:
    info = await _collect_values(
        {
            "rows": [
                [
                    {"name": "node_id", "value": "!096ea188"},
                    {"name": "deviceId", "value": 2460232048},
                ],
                [{"name": "radio.freq", "value": "910.525"}],
            ]
        }
    )
    assert info is not None
    assert render_startup_info_block("meshtastic.main  ready", info) == (
        "meshtastic.main  ready\n"
        "  node id !096ea188 · device id 2460232048\n"
        "  radio freq 910.525"
    )


async def test_explicit_labels_win_over_derived_labels() -> None:
    info = await _collect_values(
        {
            "rows": [
                [
                    {"name": "node_id", "label": "node", "value": "!096ea188"},
                    {"name": "radio", "label": "radio (MHz)", "value": "910.525"},
                ]
            ]
        }
    )
    assert info is not None
    assert render_startup_info_block("meshtastic.main", info) == (
        "meshtastic.main\n  node !096ea188 · radio (MHz) 910.525"
    )


def _row(index: int) -> list[dict[str, object]]:
    return [{"name": f"fact_{index}", "value": index}]


async def test_ten_rows_render_without_marker() -> None:
    info = await _collect_values({"rows": [_row(i) for i in range(10)]})
    assert info is not None
    assert len(info.fields) == 10
    assert info.extra_rows == 0
    rendered = render_startup_info_block("t", info)
    assert len(rendered.splitlines()) == 11
    assert "more" not in rendered


async def test_rows_beyond_cap_truncate_behind_marker(tmp_path: Path) -> None:
    info = await _collect_values({"rows": [_row(i) for i in range(12)]})
    assert info is not None
    assert [f.name for f in info.fields] == [f"fact_{i}" for i in range(10)]
    assert info.extra_rows == 2
    rendered = render_startup_info_block("t", info)
    lines = rendered.splitlines()
    assert len(lines) == 12
    assert lines[-1] == "  … +2 more"
    # The marker survives the durable cache round trip.
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, info)
    cached = read_cache_file(path)
    assert cached == info.with_cached_flags(True)
    assert cached is not None
    assert render_startup_info_block("t", cached).endswith("  … +2 more")


def test_tampered_extra_rows_hint_degrades(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILENAME

    def planted(extra_rows: object) -> None:
        path.write_text(
            json.dumps(
                {
                    "adapter_id": "a",
                    "transport": "t",
                    "emitted_at": _TIMESTAMP,
                    "extra_rows": extra_rows,
                    "fields": [{"name": "board", "value": "RAK4631", "row": 0}],
                }
            )
        )

    planted(10**30)
    cached = read_cache_file(path)
    assert cached is not None
    # Hygiene clamp: the marker stays one bounded, readable line.
    assert cached.extra_rows == 1_000_000
    assert "1000000 or more additional rows" in render_startup_info_block("t", cached)
    planted("many")
    cached = read_cache_file(path)
    assert cached is not None
    assert cached.extra_rows == 0
    assert "more" not in render_startup_info_block("t", cached)


async def test_fact_packing_bounds_remain() -> None:
    flat = await _collect_values({f"fact_{i}": i for i in range(12)})
    assert flat is not None
    assert len(flat.fields) == 8
    assert {f.row for f in flat.fields} == {0}
    assert flat.extra_rows == 0
    packed = await _collect_values(
        {"rows": [[{"name": f"fact_{i}", "value": i} for i in range(200)]]}
    )
    assert packed is not None
    # 128 candidates examined per row; 8 accepted per row.
    assert len(packed.fields) == 8
    assert [f.name for f in packed.fields] == [f"fact_{i}" for i in range(8)]


async def test_oversized_raw_values_still_drop() -> None:
    info = await _collect_values({"blob": "a" * 5000, "board": "RAK4631"})
    assert info is not None
    assert [f.name for f in info.fields] == ["board"]
    assert await _collect_values({"blob": "a" * 5000}) is None


async def test_full_unicode_fact_inventory_roundtrips(tmp_path: Path) -> None:
    info = await _collect_values(
        {
            "rows": [
                [
                    {"name": f"fact_{r}_{i}", "label": "𐐀" * 64, "value": "𐐁" * 64}
                    for i in range(8)
                ]
                for r in range(10)
            ]
        }
    )
    assert info is not None and len(info.fields) == 80
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, info)
    assert path.stat().st_size <= 65536
    assert read_cache_file(path) == info.with_cached_flags(True)


async def test_derived_label_is_bounded() -> None:
    info = await _collect_values(
        {"boardModelVersion_boardModelVersion_boardModelVersion_boardModel": "x"}
    )
    assert info is not None
    line = render_startup_info_block("t", info).splitlines()[1].strip()
    assert line.endswith("… x") and len(line[:-2]) == 64
