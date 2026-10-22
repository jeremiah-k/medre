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
    "Unclosed transport",
    "ResourceWarning:",
    "exception was never retrieved",
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


def runtime_streams(root: Path, run: Path, generations: set[Path]) -> list[Path]:
    """Require both captures and control evidence for the declared generations."""
    streams = sorted(
        path
        for path in run.rglob("runtime-*.*")
        if path.suffix in {".stdout", ".stderr"} and path.is_file()
    )
    controls = {
        path.with_suffix("") for path in run.rglob("runtime-*.jsonl") if path.is_file()
    }
    missing_controls = sorted(
        base.with_suffix(".jsonl").relative_to(root).as_posix()
        for base in generations - controls
    )
    missing = sorted(
        base.with_suffix(suffix).relative_to(root).as_posix()
        for base in generations
        for suffix in (".stdout", ".stderr")
        if not base.with_suffix(suffix).is_file()
    )
    unexpected = sorted(
        {
            path.relative_to(root).as_posix()
            for path in streams
            if path.with_suffix("") not in generations & controls
        }
        | {
            base.with_suffix(".jsonl").relative_to(root).as_posix()
            for base in controls - generations
        }
    )
    if missing_controls or missing or unexpected:
        write_json(
            root / "results/log-review.json",
            {
                "status": "failed",
                "logs": {},
                "expected_generations": len(generations),
                "missing_controls": missing_controls,
                "missing_streams": missing,
                "unexpected_streams": unexpected,
            },
        )
        raise ValueError("Runtime stdout/stderr evidence is incomplete")
    return streams


def review(root: Path) -> None:
    """Audit the latest native, MMRelay, recovery, and conversation runtime logs.

    Write counts and line numbers to results/log-review.json without raw log
    text. Raise ValueError if no runtime evidence is selected or recovery
    generation evidence is missing or unexpected. Raise RuntimeError for resource
    warnings, missing timestamps, or streams without log records; stderr may
    have no records.
    File read and report write errors propagate.
    """
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
    recovery = sorted(
        (path for path in (root / "results").glob("resilience-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    if recovery:
        from ..recovery import SCENARIOS, runtime_generations

        generations = {
            recovery[-1] / scenario / name
            for scenario in SCENARIOS
            for name in runtime_generations(scenario)
        }
        files.extend(runtime_streams(root, recovery[-1], generations))
    for campaign in ("fidelity", "presentation", "encrypted", "typing"):
        runs = sorted(
            (
                path
                for path in (root / "results").glob(campaign + "-*")
                if path.is_dir()
            ),
            key=lambda path: path.stat().st_mtime,
        )
        if runs:
            evidence = runs[-1] / "evidence" if campaign == "typing" else runs[-1]
            generations = 3 if campaign == "typing" else 2
            files.extend(
                runtime_streams(
                    root,
                    evidence,
                    {evidence / f"runtime-{number}" for number in range(generations)},
                )
            )
    if not files:
        raise ValueError("No runtime log evidence exists")
    results = {
        path.relative_to(root).as_posix(): analyze(path.read_text(errors="replace"))
        for path in files
    }
    failed = any(
        (not row["levels"] and not name.endswith(".stderr"))
        or row["records_without_timestamp"]
        or row["resource_warning_lines"]
        for name, row in results.items()
    )
    write_json(
        root / "results/log-review.json",
        {"status": "failed" if failed else "passed", "logs": results},
    )
    if failed:
        raise RuntimeError(
            "Runtime logs contain missing timestamps or resource warnings"
        )
