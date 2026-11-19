"""Plan source changes through uv's saved tool request, without another lockfile."""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from medre.runtime.installation_summary import describe_direct_source, version_label
from medre.self_upgrade import (
    UpgradePlan,
    _retain_extras,
    _verify_scoped_sources,
    installed_adapters,
)
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
    redact_source,
    selected_source,
    split_requirement_marker,
)


def _target(
    selection: str, catalog: SourceCatalog, installed: dict[str, str]
) -> tuple[str, str]:
    match = _SELECTION.fullmatch(selection)
    if match is None:
        raise ToolManagementError(
            "Select core, an adapter alias or a distribution name with optional extras."
        )
    if match[1] in {"core", "medre"}:
        extras = "medre" + ("[" + match[2] + "]" if match[2] else "")
        return "medre", _retain_extras(catalog.core or DEFAULT_CORE_SOURCE, extras)
    name = package_name(match[1])
    # Match the upgrade selector: an explicit installed distribution name is
    # authoritative even when a catalog alias happens to use that name.
    if name in installed:
        return name, selection
    if match[1] in catalog.adapters:
        return selected_source(selection, catalog.adapters)
    return name, selection


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
            or "@" in url.path
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
            name, raw = _target(selection, catalog, desired)
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

        _, marker = split_requirement_marker(previous)
        if marker and split_requirement_marker(target)[1] is None:
            target += " ; " + marker
        changes.append(f"{name}: {redact_source(previous)} -> {redact_source(target)}")
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


def source_inventory(catalog: SourceCatalog) -> dict[str, Any]:
    """Read configured, requested and installed sources without importing providers."""
    requested: dict[str, str] = {}
    receipt_error = None
    try:
        installation = ToolInstallation.inspect()
        requested = {package_name(raw): raw for raw in installation.requirements()}
    except ToolManagementError as exc:
        receipt_error = redact_source(str(exc))
    configured = {"medre": [catalog.core or DEFAULT_CORE_SOURCE]}
    for raw in catalog.adapters.values():
        name = package_name(raw)
        configured.setdefault(name, [])
        if raw not in configured[name]:
            configured[name].append(raw)
    provider_error = None
    try:
        providers = set(installed_adapters())
    except ToolManagementError as exc:
        provider_error = redact_source(str(exc))
        providers = set()
    rows = []
    for name in sorted(configured.keys() | requested.keys() | providers):
        row: dict[str, Any] = {
            "package": name,
            "configured": [redact_source(raw) for raw in configured.get(name, [])],
            "requested": redact_source(requested[name]) if name in requested else None,
            "installed_version": None,
            "source": None,
            "source_label": None,
        }
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            rows.append(row)
            continue
        except Exception:  # metadata discovery is best effort
            row["source_label"] = "unknown"
            row["provenance_error"] = "Installed source metadata is unreadable."
            rows.append(row)
            continue
        row["source_label"] = "unknown"
        try:
            row["installed_version"] = version_label(dist.version)
        except Exception:
            row["installed_version"] = "?"
        try:
            raw_url = dist.read_text("direct_url.json")
            if raw_url is None:
                row["source"] = {"kind": "index", "url": None}
                row["source_label"] = "pypi"
            else:
                direct = json.loads(raw_url)
                row["source_label"], row["source"] = describe_direct_source(direct)
                if row["source"] is None:
                    raise ValueError("invalid direct source metadata")
        except Exception:  # never expose metadata exception details
            row["provenance_error"] = "Installed source metadata is unreadable."
        rows.append(row)
    return {
        "packages": rows,
        "receipt_error": receipt_error,
        "provider_error": provider_error,
    }
