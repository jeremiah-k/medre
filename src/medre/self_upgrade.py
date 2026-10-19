"""Plan explicit updates while uv retains ownership of MEDRE's installation."""

from __future__ import annotations

import importlib.metadata
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from medre.tool_management import (
    _REQUIREMENT,
    _SELECTION,
    SourceCatalog,
    ToolInstallation,
    ToolManagementError,
    package_name,
    selected_source,
)


@dataclass(frozen=True)
class UpgradePlan:
    command: tuple[str, ...]
    packages: tuple[str, ...]
    notes: tuple[str, ...]


def installed_adapters() -> dict[str, set[str]]:
    """Read provider metadata without importing adapters or optional SDKs."""
    providers: dict[str, set[str]] = {}
    try:
        for entry in importlib.metadata.entry_points(group="medre.adapters"):
            if entry.dist is None:
                raise ToolManagementError(
                    "Cannot identify an installed adapter distribution."
                )
            name = package_name(entry.dist.metadata["Name"])
            if name == "medre":
                raise ToolManagementError(
                    "Core cannot be selected as an adapter package."
                )
            providers.setdefault(name, set()).add(entry.name)
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ToolManagementError):
            raise
        raise ToolManagementError(
            "Cannot read installed adapter metadata; repair the tool installation with uv."
        ) from None
    return providers


def _selection(
    selection: str, catalog: SourceCatalog, providers: dict[str, set[str]]
) -> str:
    match = _SELECTION.fullmatch(selection)
    if match is None or match[2]:
        raise ToolManagementError(
            "Upgrade accepts installed adapter aliases or distribution names; use 'medre adapters install' to change extras."
        )
    normalized = package_name(selection)
    if normalized in providers:
        return normalized
    matches = [name for name, aliases in providers.items() if selection in aliases]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolManagementError(
            f"Multiple installed providers use {selection!r}; select a distribution name."
        )
    if selection in catalog.adapters:
        name, _ = selected_source(selection, catalog.adapters)
        if name in providers:
            return name
    raise ToolManagementError(
        f"No installed adapter matches {selection!r}. Inspect 'medre adapters'; install source changes explicitly before upgrading."
    )


def _retain_extras(requirement: str, previous: str) -> str:
    """Changing source must not silently disable installed extras such as E2EE."""
    current, old = _REQUIREMENT.match(requirement), _REQUIREMENT.match(previous)
    assert current is not None and old is not None
    extras = sorted(
        (set((current[2] or "").split(",")) | set((old[2] or "").split(","))) - {""}
    )
    return (
        current[1]
        + ("[" + ",".join(extras) + "]" if extras else "")
        + requirement[current.end() :]
    )


def _verify_scoped_sources(installation: ToolInstallation, packages: list[str]) -> None:
    """uv re-resolves mutable Git/local requests even when they are not selected."""
    for raw in installation.receipt["requirements"]:
        name = package_name(raw if isinstance(raw, str) else raw["name"])
        if name in packages:
            continue
        mutable = False
        if isinstance(raw, dict):
            if "git" in raw:
                query = parse_qs(urlsplit(raw["git"]).query)
                ref = query.get("rev", [""])[0]
                mutable = (
                    re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", ref) is None
                )
            mutable |= "directory" in raw
        elif "git+" in raw:
            ref = urlsplit(raw.split("git+", 1)[1]).path.rsplit("@", 1)[-1]
            mutable = re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", ref) is None
        if mutable:
            raise ToolManagementError(
                f"A scoped upgrade cannot preserve the mutable source of {name}. "
                "uv can refresh unselected Git branches and local directories. "
                "Choose an immutable source for that requirement, or run 'medre upgrade' for the whole environment. No packages changed."
            )


def plan_upgrade(
    installation: ToolInstallation,
    *,
    selections: tuple[str, ...] = (),
    core_only: bool = False,
    adapters_only: bool = False,
    catalog: SourceCatalog | None = None,
    sync_sources: bool = False,
) -> UpgradePlan:
    """Choose a native bulk upgrade or a selective replay of the complete request."""
    if core_only and (adapters_only or selections):
        raise ToolManagementError("Core-only updates cannot select adapters.")
    if sync_sources and catalog is None:
        raise ToolManagementError("Source synchronization requires a source catalog.")
    if not (core_only or adapters_only or selections or sync_sources):
        return UpgradePlan(
            tuple(installation.update()),
            ("medre",),
            (
                "Update core, installed adapters and all tool dependencies using uv's saved request.",
                "Existing sources and constraints remain authoritative; pinned revisions stay pinned.",
            ),
        )
    providers = {} if core_only else installed_adapters()
    catalog = catalog or SourceCatalog(None, {})
    adapter_names = (
        sorted({_selection(selection, catalog, providers) for selection in selections})
        if selections
        else sorted(providers)
    )
    only_adapters = adapters_only or bool(selections)
    packages = ([] if only_adapters else ["medre"]) + adapter_names
    if core_only:
        packages = ["medre"]
    if not packages:
        return UpgradePlan((), (), ("No installed adapters to upgrade.",))
    requirements = installation.requirements()
    if core_only or only_adapters:
        _verify_scoped_sources(installation, packages)
    desired = {package_name(requirement): requirement for requirement in requirements}
    notes = [
        "Only selected packages are requested for upgrade; uv may adjust shared dependencies for compatibility."
    ]
    if sync_sources:
        changes = {}
        if "medre" in packages and catalog.core is not None:
            changes["medre"] = catalog.core
        for requirement in catalog.adapters.values():
            name = package_name(requirement)
            if name not in packages:
                continue
            if name in changes and changes[name] != requirement:
                raise ToolManagementError(
                    f"Conflicting source aliases for {name}; select one source in the catalog."
                )
            changes[name] = requirement
        for name, requirement in changes.items():
            desired[name] = (
                _retain_extras(requirement, desired[name])
                if name in desired
                else requirement
            )
        notes.append(
            "Apply catalog requirements to selected installed packages; retain unrelated requests and installed extras."
        )
    else:
        notes.append("Keep installed sources and version constraints, including pins.")
    command = installation.install_request(list(desired.values()))
    if core_only or only_adapters:
        for name in packages:
            command.extend(["--upgrade-package", name])
    else:
        command.append("--upgrade")
    return UpgradePlan(tuple(command), tuple(packages), tuple(notes))
