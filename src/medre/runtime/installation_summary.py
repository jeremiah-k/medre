"""Installed-package provenance lines for the runtime startup header."""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import logging
import platform
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

_logger = logging.getLogger(__name__)

_CORE_FALLBACK_VERSION = "0.2.1"
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")
_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_FORGE_HOSTS = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}


def version_label(value: object) -> str:
    """Display only a valid printable package version token."""
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else "?"


def _core_version() -> str:
    try:
        return version_label(importlib.metadata.version("medre"))
    except importlib.metadata.PackageNotFoundError:
        return _CORE_FALLBACK_VERSION
    except Exception:  # metadata display must not prevent startup
        return "?"


def _display_url(value: object) -> tuple[SplitResult, str] | None:
    """Validate a direct URL and return its parts plus a redacted display form.

    Redaction drops authority credentials and replaces the query; the fragment
    is always dropped. Non-absolute, whitespace-bearing or unprintable values
    are rejected outright.
    """
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
        # Accessing port validates malformed numeric authorities.
        _ = url.port
        if url.scheme != "file" and not url.hostname:
            return None
        host = url.netloc.rsplit("@", 1)[-1]
        return url, urlunsplit(
            (url.scheme, host, url.path, "redacted" if url.query else "", "")
        )
    except ValueError:
        return None


def _repo_segments(path: str, limit: int) -> str | None:
    """Join leading URL path segments, stripping a ``.git`` repository suffix."""
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return None
    if limit:
        segments = segments[:limit]
    repo = "/".join(segments)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo or None


def _vcs_label(url: SplitResult, vcs: object, commit: object) -> str:
    """Compact recorded-revision label; never includes credentials or queries."""
    if not isinstance(commit, str):
        return "unknown"
    if vcs in ("git", "hg") and _COMMIT.fullmatch(commit):
        short = commit[:8]
        if vcs == "hg":
            return f"hg:{short}"
        if url.scheme == "file":
            if not url.path.startswith("/") or not url.path.strip("/"):
                return "unknown"
            # Use the validated, credential-free file location (including UNC host).
            file_location = urlunsplit(
                ("file", url.netloc.rsplit("@", 1)[-1], url.path, "", "")
            )
            return f"git:{file_location}@{short}"
        hostname = url.hostname or ""
        forge = _FORGE_HOSTS.get(hostname)
        if forge is not None:
            repo = _repo_segments(url.path, 0 if forge == "gitlab" else 2)
            if repo:
                return f"{forge}:{repo}@{short}"
        location = _repo_segments(url.path, 0)
        if hostname and location:
            return f"git:{hostname}/{location}@{short}"
        return "unknown"
    if vcs == "svn" and re.fullmatch(r"[0-9]+", commit):
        return f"svn:{commit}"
    return "unknown"


def describe_direct_source(direct: object) -> tuple[str, dict[str, object] | None]:
    """One validated display identity for compact, full and JSON surfaces.

    Installed metadata is descriptive only; labels establish neither index
    identity nor source-tree cleanliness or qualification.
    """
    unknown = ("unknown", None)
    if not isinstance(direct, dict):
        return unknown
    validated = _display_url(direct.get("url"))
    if validated is None:
        return unknown
    url, redacted = validated
    # PEP 610 defines mutually exclusive directory, archive and VCS forms.
    kinds = [key for key in ("dir_info", "archive_info", "vcs_info") if key in direct]
    if len(kinds) != 1 or not isinstance(direct[kinds[0]], dict):
        return unknown
    record: dict[str, object] = {
        "kind": "url",
        "url": redacted,
        "commit": None,
        "editable": False,
    }
    if "vcs_info" in direct:
        vcs = direct["vcs_info"]
        label = _vcs_label(url, vcs.get("vcs"), vcs.get("commit_id"))
        if label == "unknown":
            return unknown
        record.update(kind=vcs["vcs"], commit=vcs["commit_id"])
    elif "dir_info" in direct:
        if url.scheme != "file" or not url.path.startswith("/"):
            return unknown
        directory = direct["dir_info"]
        editable = directory.get("editable", False)
        if not isinstance(editable, bool):
            return unknown
        record.update(kind="local", editable=editable)
        label = f"editable:{redacted}" if editable else f"local:{redacted}"
    else:
        label = f"url:{redacted}"
    return label, record


def provenance_from_direct(direct: object) -> str:
    """Compact human provenance label from one parsed PEP 610 direct-URL value."""
    return describe_direct_source(direct)[0]


def configured_git_label(repository: object, ref: object) -> str:
    """Provenance label for a *configured* repository pin, in the shared grammar.

    Readiness renders the compatibility snapshot's configured refs (an
    ``owner/repo`` slug plus a recorded revision) through the same compact
    grammar as installed labels (``github:owner/repo@short8``). The label
    form converges; the fact stays distinct: this describes what the
    manifest configures, never what is installed (installed provenance is
    ``describe_direct_source``/``provenance_from_direct`` from direct_url
    metadata). The canonical forge host matches the built-in source
    authority that turns the same slug into ``git+https://github.com/...``.
    Invalid or unsupported shapes yield ``unknown`` like every other label.
    """
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        return "unknown"
    owner, repo = repository.split("/")
    if owner in (".", "..") or repo.removesuffix(".git") in ("", ".", ".."):
        return "unknown"
    validated = _display_url(f"https://github.com/{repository}")
    if validated is None:
        return "unknown"
    url, _redacted = validated
    return _vcs_label(url, "git", ref)


def _source_label(dist: importlib.metadata.Distribution) -> str:
    """Short install-provenance label; displayed URLs are redacted."""
    try:
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return "pypi"
        return provenance_from_direct(json.loads(raw))
    except Exception:  # provenance display is fail-open
        return "unknown"


def _dist_label(name: str) -> str:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return "source"
    except Exception:
        return "unknown"
    return _source_label(dist)


def core_source_label() -> str:
    """Installed provenance label for the medre core distribution."""
    return _dist_label("medre")


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def standard_distribution(transport: str) -> str:
    """The first-party distribution name expected for a transport."""
    return f"medre-adapter-{_normalized_name(transport)}"


@dataclasses.dataclass(frozen=True)
class ProviderIdentity:
    """One installed adapter entry point's display identity."""

    transport: str
    distribution: str
    version: str
    source: str


def provider_identities() -> tuple[list[ProviderIdentity], bool]:
    """Best-effort installed adapter identities, without importing SDKs.

    Returns the discovered rows and whether any entry-point metadata was
    unreadable; healthy rows are preserved when siblings fail.
    """
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
    rows: list[ProviderIdentity] = []
    for (transport, dist_name), dist in sorted(providers.items()):
        try:
            version = version_label(dist.version)
        except Exception:
            version = "?"
        label = _source_label(dist)
        rows.append(ProviderIdentity(transport, dist_name, version, label))
    return rows, incomplete


def _provider_lines() -> list[str]:
    """One line per installed transport/distribution, without importing SDKs."""
    lines: list[str] = []
    try:
        rows, incomplete = provider_identities()
        for row in rows:
            suffix = (
                ""
                if row.distribution == standard_distribution(row.transport)
                else f" [{row.distribution}]"
            )
            lines.append(
                f"  Adapter: {row.transport} {row.version} ({row.source}){suffix}"
            )
        if incomplete:
            lines.append("  Adapter inventory incomplete (metadata unavailable)")
    except Exception:  # do not include exception details, which may contain URLs
        _logger.debug("Installed provider summary unavailable")
        lines.append("  Adapter inventory incomplete (metadata unavailable)")
    return lines


def installation_lines() -> list[str]:
    """Best-effort startup header describing the running installation."""
    lines = [f"  Core:    medre {_core_version()} ({core_source_label()})"]
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
