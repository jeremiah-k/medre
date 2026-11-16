"""Plan source changes through uv's saved tool request, without another lockfile."""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from medre.self_upgrade import UpgradePlan, _retain_extras, _verify_scoped_sources
from medre.tool_management import (
    _REQUIREMENT,
    _SELECTION,
    DEFAULT_CORE_SOURCE,
    DEFAULT_SOURCES,
    GIT_CORE_SOURCE,
    GIT_SOURCES,
    SourceCatalog,
    ToolInstallation,
    ToolManagementError,
    package_name,
    selected_source,
)


def _target(selection: str, catalog: SourceCatalog) -> tuple[str, str]:
    core = _SELECTION.fullmatch(selection)
    if core and core[1] in {"core", "medre"}:
        extras = "medre" + ("[" + core[2] + "]" if core[2] else "")
        return "medre", _retain_extras(catalog.core or DEFAULT_CORE_SOURCE, extras)
    if (
        _SELECTION.fullmatch(selection)
        and selection.split("[", 1)[0] in catalog.adapters
    ):
        return selected_source(selection, catalog.adapters)
    if _SELECTION.fullmatch(selection):
        return package_name(selection.split("[", 1)[0]), selection
    raise ToolManagementError(
        "Select core, an adapter alias or a distribution name with optional extras."
    )


def _requirement(
    name: str,
    selected: str,
    *,
    source: str | None,
    version: str | None,
    git_url: str | None,
    ref: str | None,
    path: str | None,
    editable: bool,
    requirement: str | None,
) -> str:
    match = _REQUIREMENT.match(selected)
    assert match is not None
    base = name + ("[" + match[2] + "]" if match[2] else "")
    if requirement is not None:
        if source or version or git_url or ref or path or editable:
            raise ToolManagementError(
                "--requirement cannot be combined with source shortcut options."
            )
        if package_name(requirement) != name:
            raise ToolManagementError(
                "The explicit requirement must name the selected distribution."
            )
        return _retain_extras(requirement, base)
    if source in {"index", "pypi"}:
        if git_url or ref or path or editable:
            raise ToolManagementError(
                "Index sources accept --version, not Git or local options."
            )
        if (
            version is not None
            and re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version) is None
        ):
            raise ToolManagementError(
                "--version requires an exact version; use --requirement for version ranges."
            )
        return base + ("==" + version if version is not None else "")
    if source == "git":
        if version or path or editable:
            raise ToolManagementError(
                "Git sources accept --git-url and --ref, not version or local options."
            )
        if git_url is None:
            defaults = {package_name(raw): raw for raw in GIT_SOURCES.values()}
            defaults["medre"] = GIT_CORE_SOURCE
            if name not in defaults:
                raise ToolManagementError(
                    "Community distributions require an explicit --git-url."
                )
            git_url = defaults[name].split(" @ git+", 1)[1].rsplit("@", 1)[0]
            ref = ref if ref is not None else "main"
        try:
            url = urlsplit(git_url.removeprefix("git+"))
        except ValueError:
            raise ToolManagementError("Invalid Git repository URL.") from None
        if (
            url.scheme not in {"https", "ssh", "file"}
            or (url.scheme != "file" and not url.hostname)
            or not url.path
            or url.query
            or url.fragment
            or url.password
            or (url.scheme == "https" and url.username)
            or any(c.isspace() or ord(c) < 32 for c in git_url)
        ):
            raise ToolManagementError(
                "Use a credential-free HTTPS, SSH or file Git URL; select its revision with --ref."
            )
        if ref is not None and (
            not ref or any(c.isspace() or ord(c) < 32 for c in ref)
        ):
            raise ToolManagementError(
                "A Git ref must be non-empty and contain no whitespace or control characters."
            )
        url_text = urlunsplit(url)
        return (
            base
            + " @ git+"
            + url_text
            + ("@" + quote(ref, safe="/._-") if ref is not None else "")
        )
    if source == "local":
        if version or git_url or ref or not path:
            raise ToolManagementError(
                "Local sources require --path and optionally --editable."
            )
        try:
            directory = Path(path).expanduser().resolve()
            exists = directory.is_dir()
        except (OSError, ValueError):
            raise ToolManagementError(
                "Cannot read the local project directory."
            ) from None
        if not exists:
            raise ToolManagementError(
                "The local source must be an existing project directory."
            )
        return base + " @ " + directory.as_uri()
    raise ToolManagementError(
        "Choose --from index, git or local, or an explicit --requirement."
    )


def plan_source_switch(
    installation: ToolInstallation,
    catalog: SourceCatalog,
    selections: tuple[str, ...] = (),
    *,
    all_packages: bool = False,
    allow_refresh: bool = False,
    source: str | None = None,
    version: str | None = None,
    git_url: str | None = None,
    ref: str | None = None,
    path: str | None = None,
    editable: bool = False,
    requirement: str | None = None,
) -> UpgradePlan:
    """Replace selected sources while preserving every unrelated explicit request."""
    requirements = installation.requirements()
    desired = {package_name(raw): raw for raw in requirements}
    defaults = {package_name(raw): raw for raw in DEFAULT_SOURCES.values()}
    if all_packages:
        if selections:
            raise ToolManagementError("Select packages or --all, not both.")
        if source not in {"index", "pypi", "git"}:
            raise ToolManagementError(
                "--all supports index or Git sources, not per-project paths or requirements."
            )
        selected = {
            name: raw
            for name, raw in desired.items()
            if name == "medre" or name in defaults
        }
    else:
        if not selections:
            raise ToolManagementError(
                "Select core or installed adapters, or use --all."
            )
        selected = {}
        for selection in selections:
            name, raw = _target(selection, catalog)
            if name in selected:
                raise ToolManagementError("Select each distribution only once.")
            if name not in desired:
                raise ToolManagementError(
                    f"{name} is not explicitly installed; add adapters with 'medre adapters install' first."
                )
            selected[name] = raw
    if len(selected) != 1 and any((version, git_url, ref, path, editable, requirement)):
        raise ToolManagementError(
            "Version, ref, repository, local and explicit requirement options require one package."
        )
    if not allow_refresh:
        try:
            _verify_scoped_sources(installation, list(selected))
        except ToolManagementError as exc:
            raise ToolManagementError(
                f"{exc} For an intentional source switch, --allow-refresh explicitly permits unselected moving sources to refresh."
            ) from None
    editables = {}
    changes = []
    for name, raw in selected.items():
        target = _requirement(
            name,
            raw,
            source=source,
            version=version,
            git_url=git_url,
            ref=ref,
            path=path,
            editable=editable,
            requirement=requirement,
        )
        target = _retain_extras(target, desired[name])
        # Preserve conditional selection unless an explicit replacement supplies
        # its own marker. URL semicolons are not requirement marker separators.
        previous = desired[name]

        def marker_of(raw: str) -> list[str]:
            return (
                re.split(r"\s+;\s*", raw, maxsplit=1)[1:]
                if "@" in raw
                else raw.split(";", 1)[1:]
            )

        marker = marker_of(previous)
        if marker and not marker_of(target):
            target += " ; " + marker[0].strip()
        changes.append(f"{name}: {previous} -> {target}")
        desired[name] = target
        editables[name] = editable
    command = installation.install_request(list(desired.values()), editable=editables)
    for name in selected:
        command.extend(["--upgrade-package", name])
    return UpgradePlan(
        tuple(command),
        tuple(selected),
        tuple(changes)
        + (
            *(
                (
                    "Unselected moving sources may refresh because --allow-refresh was selected.",
                )
                if allow_refresh
                else ()
            ),
            "uv retains this source choice in its tool receipt; ordinary upgrades keep it.",
            "Source catalogs are unchanged; install and --sync-sources deliberately apply catalog choices again.",
            "uv may adjust shared dependencies. Source changes do not roll back stored data.",
        ),
    )


def _redact(value: str) -> str:
    """Display source locations without URL credentials or query secrets."""

    def replace(match: re.Match[str]) -> str:
        try:
            url = urlsplit(match[0])
        except ValueError:
            return "(invalid source URL)"
        host = url.netloc.rsplit("@", 1)[-1]
        return urlunsplit(
            (url.scheme, host, url.path, "redacted" if url.query else "", url.fragment)
        )

    return re.sub(r"(?:git\+)?(?:https?|ssh|file)://\S+", replace, value)


def source_inventory(catalog: SourceCatalog) -> dict[str, Any]:
    """Read configured, requested and installed sources without importing providers."""
    requested: dict[str, str] = {}
    receipt_error = None
    try:
        installation = ToolInstallation.inspect()
        requested = {package_name(raw): raw for raw in installation.requirements()}
    except ToolManagementError as exc:
        receipt_error = str(exc)
    configured = {"medre": [catalog.core or DEFAULT_CORE_SOURCE]}
    for raw in catalog.adapters.values():
        name = package_name(raw)
        configured.setdefault(name, [])
        if raw not in configured[name]:
            configured[name].append(raw)
    rows = []
    for name in sorted(configured.keys() | requested.keys()):
        row: dict[str, Any] = {
            "package": name,
            "configured": [_redact(raw) for raw in configured.get(name, [])],
            "requested": _redact(requested[name]) if name in requested else None,
            "installed_version": None,
            "source": None,
        }
        try:
            dist = importlib.metadata.distribution(name)
            row["installed_version"] = dist.version
            raw_url = dist.read_text("direct_url.json")
            if raw_url:
                direct = json.loads(raw_url)
                row["source"] = {
                    "kind": (
                        direct.get("vcs_info", {}).get("vcs", "vcs")
                        if direct.get("vcs_info")
                        else "local" if direct.get("dir_info") is not None else "url"
                    ),
                    "url": _redact(direct["url"]),
                    "commit": direct.get("vcs_info", {}).get("commit_id"),
                    "editable": bool(direct.get("dir_info", {}).get("editable")),
                }
            else:
                row["source"] = {"kind": "index", "url": None}
        except importlib.metadata.PackageNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            row["provenance_error"] = "Installed source metadata is unreadable."
        rows.append(row)
    return {"packages": rows, "receipt_error": receipt_error}
