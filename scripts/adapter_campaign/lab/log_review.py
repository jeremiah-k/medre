"""Review runtime logs without copying message text into public summaries."""

from __future__ import annotations

import re
from pathlib import Path

from .sources import write_json

_RECORD = re.compile(
    r"\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]|\b(DEBUG|INFO|WARNING|ERROR|CRITICAL):"
)
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_LEAKS = (
    "Unclosed client session",
    "Unclosed connector",
    "Task was destroyed but it is pending",
    "was never awaited",
)


def analyze(text: str) -> dict:
    counts = {}
    missing = []
    leaks = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _RECORD.search(line)
        if match:
            level = match.group(1) or match.group(2)
            counts[level] = counts.get(level, 0) + 1
            if not _STAMP.match(line):
                missing.append(number)
        if any(marker in line for marker in _LEAKS):
            leaks.append(number)
    return {
        "levels": counts,
        "records_without_timestamp": missing,
        "resource_warning_lines": leaks,
    }


def review(root: Path) -> None:
    candidates = sorted(
        (
            path
            for path in (root / "logs").glob("native-campaign-*.log")
            if re.fullmatch(r"native-campaign-\d{15,}\.log", path.name)
        ),
        key=lambda path: path.stat().st_mtime,
    )
    files = candidates[-1:]
    runs = sorted(
        (root / "results").glob("mmrelay-native-*"),
        key=lambda path: path.stat().st_mtime,
    )
    if runs:
        files.append(runs[-1] / "runtime.log")
    if not files:
        raise ValueError("No runtime log evidence exists")
    results = {
        path.relative_to(root).as_posix(): analyze(path.read_text(errors="replace"))
        for path in files
    }
    failed = any(
        not row["levels"]
        or row["records_without_timestamp"]
        or row["resource_warning_lines"]
        for row in results.values()
    )
    write_json(
        root / "results/log-review.json",
        {"status": "failed" if failed else "passed", "logs": results},
    )
    if failed:
        raise RuntimeError(
            "Runtime logs contain missing timestamps or resource warnings"
        )
