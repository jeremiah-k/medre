# medre-test-tier: compatibility
"""The installed-image check consumes the settled banner, not obsolete logs."""

import logging

import pytest

from medre.core.observability.logging import _TextFormatter
from medre.runtime.startup_banner import (
    StartupBanner,
    render_outcome_line,
    render_startup_banner,
)
from scripts.docker.qualify import startup_ready


@pytest.mark.parametrize(
    ("count", "ready", "expected"),
    [(7, 7, True), (7, 6, False), (6, 6, False), (0, 0, False)],
)
def test_image_startup_gate_requires_seven_ready_adapters(
    count: int, ready: int, expected: bool
) -> None:
    banner = render_startup_banner(
        StartupBanner(runtime_name="medre", adapter_count=count)
    )
    outcome = render_outcome_line(ready=ready, total=count, failed=count - ready)
    formatter = _TextFormatter()
    messages = (
        (logging.INFO, banner),
        (logging.INFO if count == ready else logging.WARNING, outcome),
    )
    logs = "\n".join(
        formatter.format(logging.LogRecord("medre", level, "", 0, text, (), None))
        for level, text in messages
    )
    assert startup_ready(logs) is expected


def test_image_startup_gate_rejects_premature_and_unrelated_messages() -> None:
    assert not startup_ready(
        "2026-11-20 [INFO] medre: MEDRE medre — startup (7 adapter(s))"
    )
    assert not startup_ready(
        "2026-11-20 [INFO] medre: Runtime ready (7 adapter(s) running)"
    )
    assert not startup_ready(
        "2026-11-20 [INFO] medre: All adapters ready — MEDRE runtime running"
    )
