"""Installed-package provenance lines for the runtime startup header."""

from __future__ import annotations

import importlib.metadata
import json
import logging
import platform
from typing import Any

from medre.tool_management import redact_source

_logger = logging.getLogger(__name__)

_CORE_FALLBACK_VERSION = "0.1.1"


def _core_version() -> str:
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return _CORE_FALLBACK_VERSION


def _source_label(dist: importlib.metadata.Distribution) -> str:
    """Short install-provenance label; displayed URLs are redacted."""
    raw = dist.read_text("direct_url.json")
    if raw is None:
        return "index"
    try:
        direct: dict[str, Any] = json.loads(raw)
    except ValueError:
        return "unknown source"
    url = redact_source(str(direct.get("url", "")))
    if (direct.get("dir_info") or {}).get("editable"):
        return f"editable {url}"
    vcs = direct.get("vcs_info") or {}
    commit = str(vcs.get("commit_id") or "")
    kind = str(vcs.get("vcs") or "vcs")
    if vcs:
        return f"{kind} {commit[:8]}".rstrip()
    return f"url {url}" if url else "unknown source"


def _dist_label(name: str) -> str:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return "source"
    try:
        return _source_label(dist)
    except Exception:  # provenance display is fail-open
        return "unknown source"


def _provider_lines() -> list[str]:
    """One line per installed provider distribution, without importing SDKs."""
    providers: dict[tuple[str, str], importlib.metadata.Distribution] = {}
    for entry in importlib.metadata.entry_points(group="medre.adapters"):
        if entry.dist is None or entry.dist.metadata["Name"] == "medre":
            # Core-owned built-ins are not separately installed distributions.
            continue
        providers[(entry.name, entry.dist.metadata["Name"])] = entry.dist
    lines: list[str] = []
    for (transport, dist_name), dist in sorted(providers.items()):
        try:
            version, label = dist.version, _source_label(dist)
        except Exception:  # provenance display is fail-open
            version, label = "?", "unknown source"
        suffix = "" if dist_name == f"medre-adapter-{transport}" else f" [{dist_name}]"
        lines.append(f"  Adapter: {transport} {version} ({label}){suffix}")
    return lines


def installation_lines() -> list[str]:
    """Startup header lines describing the running installation; never raise."""
    lines: list[str] = [
        f"  Core:    medre {_core_version()} ({_dist_label('medre')})",
        f"  Python:  {platform.python_version()} "
        f"({platform.system()} {platform.machine()})",
    ]
    try:
        lines.extend(_provider_lines())
    except Exception:  # a broken environment must not block startup
        _logger.debug("Installed provider summary unavailable", exc_info=True)
    return lines
