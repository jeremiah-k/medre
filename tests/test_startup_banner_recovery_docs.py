# medre-test-tier: compatibility
"""Keep documented text-log recovery filters aligned with actual banner output."""

import logging
import re
import shlex
from pathlib import Path

from medre.core.observability.logging import _TextFormatter
from medre.core.startup_info import AdapterStartupInfo, StartupInfoField
from medre.runtime.startup_banner import (
    AdapterBannerEntry,
    StartupBanner,
    render_startup_banner,
)


def test_recovery_filters_include_every_settled_adapter_heading() -> None:
    cached = AdapterStartupInfo(
        adapter_id="broken",
        transport="meshtastic",
        fields=(StartupInfoField(name="board", value="old", cached=True, row=0),),
        emitted_at="2026-11-20T00:00:00+00:00",
    )
    banner = render_startup_banner(
        StartupBanner(
            runtime_name="recovery",
            adapter_count=4,
            adapters=(
                AdapterBannerEntry("matrix.main", "ready", "312ms"),
                AdapterBannerEntry("meshcore.radio", "degraded", "1.2s"),
                AdapterBannerEntry("meshtastic.broken", "failed", "42µs", cached),
                AdapterBannerEntry("matrix.missing", "build failed"),
            ),
        )
    )
    record = logging.LogRecord("medre", logging.INFO, "", 0, banner, (), None)
    lines = _TextFormatter().format(record).splitlines()
    docs = Path(__file__).resolve().parents[1] / "docs/ops/recovery-and-replay.md"
    filters = [
        shlex.split(line)[2]
        for line in docs.read_text(encoding="utf-8").splitlines()
        if line.startswith("grep -E ") and "medre: " in line and "build failed" in line
    ]
    assert len(filters) == 2
    for pattern in filters:
        matched = [
            line.split("medre: ", 1)[1] for line in lines if re.search(pattern, line)
        ]
        assert [heading.split()[0] for heading in matched] == [
            "matrix.main",
            "meshcore.radio",
            "meshtastic.broken",
            "matrix.missing",
        ]
        assert matched[2].endswith("42µs (cached)")
