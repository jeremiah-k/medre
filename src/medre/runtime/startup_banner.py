"""Consolidated startup banner rendered after the runtime settles.

The banner is the single post-startup identity view for ``medre run``.
It is rendered once every adapter has settled — ready, degraded, or
terminally failed — so a failed adapter never advertises readiness and
successful startups report facts that were confirmed, not previewed.

Sections render in a fixed order: identity facts (config, state,
installation inventory, limits), one block per adapter instance in
deterministic start order, then runtime stats (routes, eligibility,
persisted events, storage). Plain log text only — no ANSI color — so the
banner survives journald and file captures. Rows share one aligned label
column and the packed ``label value · label value`` fact grammar already
used by adapter startup info.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.observability.sanitization import sanitize_error_line
from medre.core.startup_info import AdapterStartupInfo, render_startup_info_block

__all__ = [
    "AdapterBannerEntry",
    "StartupBanner",
    "render_outcome_line",
    "render_startup_banner",
]

#: Width of the ``Label:`` column shared by every fact row. The longest
#: banner label (``Adapter`` / ``Storage`` / ``Routing``) is seven
#: characters, so values align at one consistent column across the
#: identity, installation, and stats sections.
_LABEL_WIDTH = 8


@dataclass(frozen=True, slots=True)
class AdapterBannerEntry:
    """One adapter instance's block inside the banner adapters section.

    ``info`` carries the adapter's last-known startup facts; ``None``
    means the adapter reported none. ``reason`` carries an explanation
    for degraded or unsuccessful entries, sanitized at rendering.
    """

    name: str
    status: str
    duration_text: str | None = None
    info: AdapterStartupInfo | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StartupBanner:
    """Inputs for one consolidated startup banner rendering.

    Every text field is pre-formatted, display-only content owned by the
    caller; the renderer lays the rows out and sanitizes failure reasons. Absent facts (``None``
    or empty tuples) omit their rows cleanly.
    """

    runtime_name: str
    adapter_count: int
    config_path: str | None = None
    state_dir: str | None = None
    installation_lines: tuple[str, ...] = ()
    limits_text: str | None = None
    disabled_adapters: tuple[str, ...] = ()
    routes_text: str | None = None
    eligibility_text: str | None = None
    persisted_events_text: str | None = None
    storage_text: str | None = None
    adapters: tuple[AdapterBannerEntry, ...] = ()


def _fact_row(label: str, value: str) -> str:
    """Render one ``Label: value`` row in the shared aligned column."""
    return f"  {(label + ':'):<{_LABEL_WIDTH}} {value}"


def _adapter_blocks(entries: tuple[AdapterBannerEntry, ...]) -> list[str]:
    """Render per-instance blocks with aligned status columns.

    Names align across blocks so statuses and durations read as one
    table at a glance. Fact rows keep the shared two-space indent of the
    startup-info grammar; the ``(cached)`` marker, ``… +N more``
    truncation marker, and redaction rules are owned by the startup-info
    renderer and pass through unchanged.
    """
    if not entries:
        return []
    name_width = max(len(entry.name) for entry in entries)
    status_width = max(len(entry.status) for entry in entries)
    blocks: list[str] = []
    for entry in entries:
        heading = f"{entry.name:<{name_width}}  {entry.status:<{status_width}}"
        if entry.duration_text:
            heading += f"  {entry.duration_text}"
        else:
            # Padded status columns must not leave ragged line endings.
            heading = heading.rstrip()
        if entry.info is not None:
            block = render_startup_info_block(heading, entry.info)
        else:
            block = heading
        if entry.reason:
            safe_reason = sanitize_error_line(entry.reason)
            if safe_reason:
                block += f"\n  reason {safe_reason}"
        blocks.append(block)
    return blocks


def render_startup_banner(banner: StartupBanner) -> str:
    """Render the consolidated startup banner as one multi-line string.

    Sections (identity, adapters, stats) are separated by blank lines;
    the runtime outcome is *not* included — callers append
    :func:`render_outcome_line` so the final readiness claim always
    stands as one unmistakable line.
    """
    header: list[str] = [
        f"MEDRE {banner.runtime_name} — startup ({banner.adapter_count} adapter(s))"
    ]
    if banner.config_path:
        header.append(_fact_row("Config", banner.config_path))
    if banner.state_dir:
        header.append(_fact_row("State", banner.state_dir))
    header.extend(banner.installation_lines)
    if banner.limits_text:
        header.append(_fact_row("Limits", banner.limits_text))

    adapters = _adapter_blocks(banner.adapters)
    if banner.disabled_adapters:
        adapters.append("disabled, not started: " + ", ".join(banner.disabled_adapters))

    stats: list[str] = []
    if banner.routes_text:
        stats.append(_fact_row("Routes", banner.routes_text))
    if banner.eligibility_text:
        stats.append(_fact_row("Routing", banner.eligibility_text))
    if banner.persisted_events_text:
        stats.append(_fact_row("Events", banner.persisted_events_text))
    if banner.storage_text:
        stats.append(_fact_row("Storage", banner.storage_text))

    sections = [section for section in (header, adapters, stats) if section]
    return "\n\n".join("\n".join(section) for section in sections)


def render_outcome_line(
    *,
    ready: int,
    total: int,
    degraded: int = 0,
    failed: int = 0,
    build_failed: int = 0,
) -> str:
    """Render the one honest readiness line that follows the banner.

    ``ready`` counts adapters in the READY state only; degraded adapters
    started but never count as ready. The all-ready form is reserved for
    a fully healthy settle so the line never overclaims.
    """
    if (
        total > 0
        and ready == total
        and degraded == 0
        and failed == 0
        and build_failed == 0
    ):
        return "All adapters ready — MEDRE runtime running"
    parts = [f"{ready}/{total} adapter(s) ready"]
    if degraded:
        parts.append(f"{degraded} degraded")
    if failed:
        parts.append(f"{failed} failed")
    if build_failed:
        parts.append(f"{build_failed} failed to build")
    return "MEDRE runtime running — " + ", ".join(parts)
