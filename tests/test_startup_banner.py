# medre-test-tier: compatibility
"""Consolidated startup banner: layout, adapter blocks, and the outcome line.

The banner renders once every adapter has settled, so the tests pin what
operators can rely on at a glance: section order (identity, adapters,
stats), one aligned label column, plain text with no ANSI color, honest
per-instance status tags with facts beneath, and a final readiness line
that never claims all-ready while an adapter is degraded or failed.
"""

from __future__ import annotations

import re

from medre.core.startup_info import AdapterStartupInfo, StartupInfoField
from medre.runtime.startup_banner import (
    AdapterBannerEntry,
    StartupBanner,
    render_outcome_line,
    render_startup_banner,
)

_TIMESTAMP = "2026-11-20T00:00:00+00:00"

_ANSI_RE = re.compile(r"\x1b\[")


def _info(fields: list[tuple[str, str]], *, cached: bool = False) -> AdapterStartupInfo:
    return AdapterStartupInfo(
        adapter_id="adapter",
        transport="transport",
        fields=tuple(
            StartupInfoField(name=name, value=value, cached=cached, row=0)
            for name, value in fields
        ),
        emitted_at=_TIMESTAMP,
    )


def _banner(**overrides: object) -> StartupBanner:
    values: dict[str, object] = {
        "runtime_name": "mesh-bridge",
        "adapter_count": 2,
        "config_path": "/tmp/medre.yaml",
        "state_dir": "/tmp/state",
        "installation_lines": ("  Core:    medre 0.2.1 (pypi)",),
        "limits_text": "max_inflight_deliveries=100",
        "routes_text": "6 enabled, 0 disabled (6 configured)",
        "storage_text": "sqlite (/tmp/state/medre.sqlite)",
        "adapters": (
            AdapterBannerEntry(
                name="matrix.matrix",
                status="ready",
                duration_text="1.5s",
                info=_info([("user_id", "@bot:fake.local")]),
            ),
            AdapterBannerEntry(
                name="meshtastic.radio",
                status="ready",
                duration_text="935ms",
                info=_info([("node_name", "Ridge")]),
            ),
        ),
    }
    values.update(overrides)
    return StartupBanner(**values)  # type: ignore[arg-type]


def test_banner_orders_identity_adapters_stats() -> None:
    """Sections render in order: identity, adapters, stats, then nothing else."""
    text = render_startup_banner(_banner())
    lines = text.splitlines()
    assert lines[0] == "MEDRE mesh-bridge — startup (2 adapter(s))"
    assert "  Config:  /tmp/medre.yaml" in lines
    index_core = text.index("  Core:")
    index_matrix = text.index("matrix.matrix")
    index_routes = text.index("  Routes:")
    assert index_core < index_matrix < index_routes
    # Sections separate with exactly one blank line.
    assert "\n\n" in text
    assert "\n\n\n" not in text


def test_banner_adapter_blocks_show_status_duration_and_facts() -> None:
    """Each instance block is one status line with its facts beneath."""
    text = render_startup_banner(_banner())
    # Names align to the widest instance ("meshtastic.radio" = 16 chars);
    # statuses align to the widest tag present.
    expected_matrix = f"{'matrix.matrix':<16}  {'ready':<5}  1.5s"
    expected_radio = f"{'meshtastic.radio':<16}  {'ready':<5}  935ms"
    lines = text.splitlines()
    assert expected_matrix in lines
    assert expected_radio in lines
    facts = lines[lines.index(expected_matrix) + 1]
    assert facts == "  user id @bot:fake.local"


def test_banner_label_column_is_aligned() -> None:
    """Fact-row values share one column across identity and stats sections."""
    text = render_startup_banner(
        _banner(
            eligibility_text="40 live-eligible, 0 startup-blocked",
            persisted_events_text="71 persisted",
            installation_lines=(
                "  Core:    medre 0.2.1 (pypi)",
                "  Adapter: matrix 0.2.0 (pypi)",
            ),
        )
    )
    labels = (
        "Config",
        "State",
        "Core",
        "Python",
        "Adapter",
        "Limits",
        "Routes",
        "Routing",
        "Events",
        "Storage",
    )
    for line in text.splitlines():
        if not line.lstrip().startswith(labels):
            continue
        # The shared value column: label ends inside the first 11 columns
        # and the value begins exactly at column 11.
        assert line[:11].strip().endswith(":"), repr(line)
        assert line[11] != " ", repr(line)


def test_banner_is_plain_text_without_ansi() -> None:
    """No ANSI escapes: the banner must survive journald and file captures."""
    text = render_startup_banner(_banner())
    assert not _ANSI_RE.search(text)


def test_banner_marks_degraded_and_failed_honestly() -> None:
    """Degraded keeps facts; failed carries a reason and no readiness claim."""
    text = render_startup_banner(
        _banner(
            adapters=(
                AdapterBannerEntry(
                    name="matrix.matrix",
                    status="degraded",
                    duration_text="1.5s",
                    info=_info([("user_id", "@bot:fake.local")]),
                ),
                AdapterBannerEntry(
                    name="meshcore.radio",
                    status="failed",
                    reason="connection refused",
                ),
            )
        )
    )
    # "meshcore.radio" (14) sets the name column; "degraded" (8) the status column.
    assert f"{'matrix.matrix':<14}  {'degraded':<8}  1.5s" in text
    assert f"{'meshcore.radio':<14}  {'failed':<8}".rstrip() in text
    assert "  reason connection refused" in text


def test_banner_marks_build_failures_with_reason() -> None:
    """A build-failed instance renders after started instances, with a reason."""
    text = render_startup_banner(
        _banner(
            adapter_count=3,
            adapters=(
                AdapterBannerEntry(
                    name="matrix.matrix",
                    status="ready",
                    duration_text="1.5s",
                    info=_info([("user_id", "@bot:fake.local")]),
                ),
                AdapterBannerEntry(
                    name="meshtastic.radio",
                    status="build failed",
                    reason="missing SDK distribution",
                ),
            ),
        )
    )
    assert f"{'meshtastic.radio':<16}  {'build failed':<12}".rstrip() in text
    assert "  reason missing SDK distribution" in text


def test_banner_keeps_cached_and_truncation_markers() -> None:
    """Startup-info provenance and truncation markers pass through unchanged."""
    text = render_startup_banner(
        _banner(
            adapters=(
                AdapterBannerEntry(
                    name="matrix.matrix",
                    status="ready",
                    duration_text="1.5s",
                    info=_info([("board", "OLD")], cached=True),
                ),
            )
        )
    )
    assert "matrix.matrix  ready  1.5s (cached)" in text
    assert "  board OLD" in text


def test_banner_omits_absent_facts_cleanly() -> None:
    """Missing optional facts omit their rows instead of rendering blanks."""
    text = render_startup_banner(
        _banner(
            routes_text=None,
            eligibility_text=None,
            persisted_events_text=None,
            storage_text=None,
            limits_text=None,
            installation_lines=(),
            adapters=(),
        )
    )
    assert "Routes" not in text
    assert "Routing" not in text
    assert "Events" not in text
    assert "Storage" not in text
    assert "Limits" not in text
    assert "  Core:" not in text


def test_banner_lists_disabled_adapters_without_label_break() -> None:
    """Disabled adapters render in the adapters section, not the label column."""
    text = render_startup_banner(_banner(disabled_adapters=("meshtastic.off",)))
    assert "disabled, not started: meshtastic.off" in text


def test_banner_lines_have_no_trailing_whitespace() -> None:
    """Aligned columns must not leave ragged line endings."""
    text = render_startup_banner(
        _banner(
            adapters=(
                AdapterBannerEntry(name="meshcore.radio", status="failed", reason="x"),
                AdapterBannerEntry(
                    name="matrix.matrix",
                    status="ready",
                    duration_text="1.5s",
                    info=_info([("user_id", "@bot:fake.local")]),
                ),
            )
        )
    )
    for line in text.splitlines():
        assert line == line.rstrip(), repr(line)


def test_outcome_line_all_ready() -> None:
    """A fully healthy settle uses the reserved all-ready form."""
    assert (
        render_outcome_line(ready=4, total=4)
        == "All adapters ready — MEDRE runtime running"
    )


def test_outcome_line_partial_counts_never_overclaim() -> None:
    """Degraded and failed adapters keep the N-of-M count honest."""
    assert render_outcome_line(ready=2, total=4, degraded=1, failed=1) == (
        "MEDRE runtime running — 2/4 adapter(s) ready, 1 degraded, 1 failed"
    )
    assert render_outcome_line(ready=1, total=2, degraded=1) == (
        "MEDRE runtime running — 1/2 adapter(s) ready, 1 degraded"
    )
    assert render_outcome_line(ready=3, total=4, failed=1) == (
        "MEDRE runtime running — 3/4 adapter(s) ready, 1 failed"
    )
    assert render_outcome_line(ready=2, total=4, failed=1, build_failed=1) == (
        "MEDRE runtime running — 2/4 adapter(s) ready, 1 failed, 1 failed to build"
    )


def test_outcome_line_degraded_adapter_is_not_ready() -> None:
    """A degraded adapter never satisfies the all-ready condition."""
    line = render_outcome_line(ready=0, total=1, degraded=1)
    assert "All adapters ready" not in line
    assert "0/1 adapter(s) ready, 1 degraded" in line



def test_failure_reason_cannot_create_extra_banner_rows() -> None:
    text = render_startup_banner(
        _banner(adapters=(AdapterBannerEntry(
            name="matrix.radio", status="failed",
            reason="token=private-fixture-token\nforged row\x1b[31m",
        ),))
    )
    assert "private-fixture-token" not in text
    assert "\nforged row" not in text
    assert "\x1b" not in text


def test_outcome_line_never_claims_all_ready_with_build_failure() -> None:
    assert "All adapters ready" not in render_outcome_line(
        ready=1, total=1, build_failed=1
    )


def test_text_log_formatter_prefixes_every_banner_line() -> None:
    import logging

    from medre.core.observability.logging import _TextFormatter

    banner = render_startup_banner(_banner())
    record = logging.LogRecord("medre", logging.INFO, "", 0, banner, (), None)
    rendered = _TextFormatter().format(record)
    assert [line.split("[INFO] medre: ", 1)[1] for line in rendered.splitlines()] == (
        banner.splitlines()
    )
