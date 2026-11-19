"""Installed-package provenance lines for the runtime startup header."""

from __future__ import annotations

import importlib.metadata
import json
import logging
import platform
import re
from urllib.parse import urlsplit, urlunsplit

_logger = logging.getLogger(__name__)

_CORE_FALLBACK_VERSION = "0.1.2"
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")


def _version_label(value: str | None) -> str:
    return value if value and _VERSION.fullmatch(value) else "?"


def _core_version() -> str:
    try:
        return _version_label(importlib.metadata.version("medre"))
    except importlib.metadata.PackageNotFoundError:
        return _CORE_FALLBACK_VERSION
    except Exception:  # metadata display must not prevent startup
        return "?"


def _display_url(value: object) -> str | None:
    """Redact direct URL authority credentials, query and fragment for logs."""
    if (
        not isinstance(value, str)
        or not value
        or any(char.isspace() or not char.isprintable() for char in value)
    ):
        return None
    try:
        url = urlsplit(value)
        if not url.scheme or not value.partition(":")[2].startswith("//"):
            return None
        if url.scheme != "file" and not url.hostname:
            return None
        host = url.netloc.rsplit("@", 1)[-1]
        return urlunsplit(
            (url.scheme, host, url.path, "redacted" if url.query else "", "")
        )
    except ValueError:
        return None


def _source_label(dist: importlib.metadata.Distribution) -> str:
    """Short install-provenance label; displayed URLs are redacted."""
    try:
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return "index"
        direct = json.loads(raw)
        if not isinstance(direct, dict):
            return "unknown source"
        url = _display_url(direct.get("url"))
        if url is None:
            return "unknown source"
        # PEP 610 defines mutually exclusive directory, archive and VCS forms.
        kinds = [
            key for key in ("dir_info", "archive_info", "vcs_info") if key in direct
        ]
        if len(kinds) != 1 or not isinstance(direct[kinds[0]], dict):
            return "unknown source"
        if "vcs_info" in direct:
            vcs = direct["vcs_info"]
            kind, commit = vcs.get("vcs"), vcs.get("commit_id")
            if isinstance(commit, str):
                if kind in ("git", "hg") and re.fullmatch(
                    r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit
                ):
                    return f"{kind} {commit[:8]}"
                if kind == "svn" and re.fullmatch(r"[0-9]+", commit):
                    return f"svn {commit}"
            return "unknown source"
        directory = direct.get("dir_info", {})
        if "dir_info" in direct and (
            not url.startswith("file://") or not urlsplit(url).path.startswith("/")
        ):
            return "unknown source"
        if not isinstance(directory.get("editable", False), bool):
            return "unknown source"
        if directory.get("editable") is True:
            return f"editable {url}"
        return f"url {url}"
    except Exception:  # provenance display is fail-open
        return "unknown source"


def _dist_label(name: str) -> str:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return "source"
    except Exception:
        return "unknown source"
    return _source_label(dist)


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _provider_lines() -> list[str]:
    """One line per installed transport/distribution, without importing SDKs."""
    providers: dict[tuple[str, str], importlib.metadata.Distribution] = {}
    incomplete = False
    try:
        for entry in importlib.metadata.entry_points(group="medre.adapters"):
            try:
                dist = entry.dist
                if dist is None:
                    incomplete = True
                    continue
                name = dist.metadata["Name"]
                if (
                    not name
                    or not _NAME.fullmatch(name)
                    or not _NAME.fullmatch(entry.name)
                ):
                    incomplete = True
                    continue
                normalized = _normalized_name(name)
                if normalized == "medre":
                    continue
                providers[(entry.name, normalized)] = dist
            except Exception:
                incomplete = True
    except Exception:  # a discovery failure must not discard visited entries
        incomplete = True
    lines: list[str] = []
    for (transport, dist_name), dist in sorted(providers.items()):
        try:
            version = _version_label(dist.version)
        except Exception:
            version = "?"
        label = _source_label(dist)
        suffix = (
            ""
            if dist_name == f"medre-adapter-{_normalized_name(transport)}"
            else f" [{dist_name}]"
        )
        lines.append(f"  Adapter: {transport} {version} ({label}){suffix}")
    if incomplete:
        lines.append("  Adapter inventory incomplete (metadata unavailable)")
    return lines


def installation_lines() -> list[str]:
    """Best-effort startup header describing the running installation."""
    lines = [f"  Core:    medre {_core_version()} ({_dist_label('medre')})"]
    try:
        lines.append(
            f"  Python:  {platform.python_version()} "
            f"({platform.system()} {platform.machine()})"
        )
    except Exception:
        lines.append("  Python:  unknown")
    try:
        lines.extend(_provider_lines())
    except Exception:  # do not include exception details, which may contain URLs
        _logger.debug("Installed provider summary unavailable")
        lines.append("  Adapter inventory incomplete (metadata unavailable)")
    return lines
