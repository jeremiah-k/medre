"""Provider-owned stopped-state declarations with core maintenance consumers.

This module is the trust boundary of the declaration seam. A provider
declaration is provider knowledge shipped inside its adapter distribution and
referenced from its :class:`medre.adapter_spec.AdapterSpec` through
``stopped_state_declaration``. Configuration-only coverage imports no native
transport SDK, resolves paths only from prepared configuration, and performs
no filesystem discovery. An optional native-state resolver may read bounded
metadata only during configured capture after local exclusion is retained.
Neither surface starts or stops a process, opens an SDK profile, or touches
the network. Core combines selections without importing native SDKs.

An inventory produced here is an operator-declared selection, not a
provider-completeness certificate. Providers whose state is not file-capturable
(migrated exports, device firmware, server-side credentials) are represented
honestly as export-only or stateless instead of being silently omitted, and a
distribution that ships no declaration is reported as unsupported.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, cast, runtime_checkable

from medre.adapter_spec import AdapterSpec, SymbolRef
from medre.config.model import RuntimeConfig
from medre.config.paths import MedrePaths
from medre.maintenance.stopped_snapshot import (
    SnapshotEntry,
    SnapshotFormat,
    SnapshotRole,
)

__all__ = [
    "DeclaredFile",
    "ExclusionRefused",
    "LocalExclusion",
    "NativeStateResolver",
    "ProviderCoverage",
    "StoppedCapturePlan",
    "StoppedPlanError",
    "StoppedStateDeclaration",
    "build_stopped_capture_plan",
    "capture_configured_stopped_state",
    "declaration_from_spec",
    "declared",
    "instance_coverages",
    "plan_from_coverages",
    "retain_local_exclusion",
]

Presence = Literal["required", "export_only", "disposable"]
StateClass = Literal[
    "file_backed",
    "externally_managed",
    "device_attached",
    "stateless_local",
    "unsupported",
]

_INVENTORY_FORMAT = "medre-stopped-inventory"
_INVENTORY_VERSION = 1


class StoppedPlanError(ValueError):
    """A configured installation cannot be described as a closed selection."""


class ExclusionRefused(RuntimeError):
    """A provider's ownership primitive is held or unavailable for capture."""


@dataclass(frozen=True, slots=True)
class DeclaredFile:
    """One provider-owned file with its capture semantics.

    ``required`` files must exist inside the common state root and become
    inventory entries. ``export_only`` files cannot be captured by the generic
    machinery (outside the root, not file-enumerable, or encrypted) and are
    reported rather than assumed. ``disposable`` files are recorded so caches
    are never mistaken for identity state.
    """

    path: Path
    role: SnapshotRole
    format: SnapshotFormat
    presence: Presence = "required"
    purpose: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("declared file requires an absolute configured path")
        if not self.path.name:
            raise ValueError("declared file requires a file path, not a directory")
        if self.presence not in ("required", "export_only", "disposable"):
            raise ValueError("invalid declared file presence")
        # Reuse manifest-entry validation so declarations cannot drift from
        # what capture will accept.
        SnapshotEntry("candidate", self.role, self.format)


@dataclass(frozen=True, slots=True)
class ProviderCoverage:
    """Truthful per-instance representation; zero files may be complete."""

    provider: str
    adapter_id: str
    state_class: StateClass
    files: tuple[DeclaredFile, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state_class not in (
            "file_backed",
            "externally_managed",
            "device_attached",
            "stateless_local",
            "unsupported",
        ):
            raise ValueError("invalid provider state class")
        for note in self.notes:
            if not isinstance(note, str) or not note or len(note) > 500:
                raise ValueError("coverage notes must be short text")


def declared(
    *,
    provider: str,
    adapter_id: str,
    files: Sequence[DeclaredFile],
    state_class: StateClass = "file_backed",
    notes: Sequence[str] = (),
) -> ProviderCoverage:
    """Build a coverage entry with required-file bookkeeping for one instance."""
    required = sum(item.presence == "required" for item in files)
    if state_class == "file_backed" and required == 0:
        raise StoppedPlanError(
            f"{provider}/{adapter_id}: a file-backed provider must declare "
            "at least one required file"
        )
    if state_class in ("device_attached", "stateless_local") and required:
        raise StoppedPlanError(
            f"{provider}/{adapter_id}: {state_class} providers cannot "
            "declare required files"
        )
    return ProviderCoverage(
        provider=provider,
        adapter_id=adapter_id,
        state_class=state_class,
        files=tuple(files),
        notes=tuple(notes),
    )


class StoppedStateDeclaration(Protocol):
    """The seam every provider-owned declaration implements.

    Implementations live in adapter distributions and receive the provider's
    *prepared* configuration object, so path resolution stays with the
    provider. A provider owns partially acquired exclusion resources until
    it returns them to core. If acquisition fails, it must close its
    LocalExclusion before re-raising, including on cancellation. Core owns
    cleanup only after a valid LocalExclusion is returned. See the module
    docstring for the authoritative contract.
    """

    provider: str

    def coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage: ...

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion: ...


@runtime_checkable
class NativeStateResolver(Protocol):
    """Optional read-only metadata resolution during exclusion-retained capture.

    Configuration-only planning never invokes this callback. The caller must
    retain the provider's supported local exclusion before resolving and keep
    it through snapshot publication. Missing native exports remain unsupported.
    """

    def resolve_coverage(
        self, config: object, *, adapter_id: str
    ) -> ProviderCoverage: ...


StoppedDeclarationFactory = Callable[[], StoppedStateDeclaration]


class _UnsupportedDeclaration:
    """The typed, honest result of a missing or unloadable reference."""

    def __init__(self, provider: str, *, note: str) -> None:
        self.provider = provider
        self._note = note

    def coverage(self, config: object, *, adapter_id: str) -> ProviderCoverage:
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(),
            state_class="unsupported",
            notes=(self._note,),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        return LocalExclusion(self.provider, note=self._note)


def declaration_from_spec(spec: AdapterSpec) -> StoppedStateDeclaration:
    """Resolve the optional spec reference; failure is honest, never guessed.

    A distribution without the reference, with an unloadable module, or with
    a missing factory symbol yields the typed unsupported declaration so
    planning reports the gap instead of silently omitting provider state.
    """
    reference: SymbolRef | None = getattr(spec, "stopped_state_declaration", None)
    if reference is None:
        return _UnsupportedDeclaration(
            spec.transport,
            note=(
                "the installed distribution ships no stopped-state "
                "declaration; capture would silently omit its state"
            ),
        )
    from medre.core.observability.sanitization import sanitize_error

    try:
        factory = reference.load()
        declaration: Any = factory()
    except Exception as error:
        return _UnsupportedDeclaration(
            spec.transport,
            note="stopped-state declaration failed to load: "
            + sanitize_error(f"{type(error).__name__}"),
        )
    if not callable(getattr(declaration, "coverage", None)) or not callable(
        getattr(declaration, "local_exclusion", None)
    ):
        return _UnsupportedDeclaration(
            spec.transport,
            note="stopped-state declaration does not implement the seam",
        )
    return cast(StoppedStateDeclaration, declaration)


def _coverage_of(
    declaration: StoppedStateDeclaration, prepared: object, adapter_id: str
) -> ProviderCoverage:
    """Contain provider bugs before maintenance callers expose raw failures."""
    try:
        coverage = declaration.coverage(prepared, adapter_id=adapter_id)
    except (StoppedPlanError, ValueError):
        raise
    except Exception:
        raise StoppedPlanError("provider stopped-state declaration failed") from None
    if not isinstance(coverage, ProviderCoverage):
        raise StoppedPlanError("provider returned invalid stopped-state coverage")
    return coverage


def _exclusion_of(
    declaration: StoppedStateDeclaration, prepared: object, adapter_id: str
) -> LocalExclusion:
    """Accept cleanup ownership only for a valid returned exclusion object."""
    try:
        exclusion = declaration.local_exclusion(prepared, adapter_id=adapter_id)
    except (ExclusionRefused, ValueError, OSError, sqlite3.Error):
        raise
    except Exception:
        raise ExclusionRefused("provider stopped-state exclusion failed") from None
    if not isinstance(exclusion, LocalExclusion):
        raise ExclusionRefused("provider returned invalid stopped-state exclusion")
    return exclusion


@dataclass(frozen=True, slots=True)
class StoppedCapturePlan:
    """A closed capture selection plus honest per-provider coverage."""

    common_root: Path
    entries: tuple[SnapshotEntry, ...]
    coverages: tuple[ProviderCoverage, ...]
    _owners: Mapping[str, str] = field(default_factory=dict, repr=False)

    def inventory_document(self) -> dict[str, Any]:
        """Render the version-1 operator inventory for capture."""
        files = [
            {
                "provider": self._owners[entry.path],
                "path": entry.path,
                "role": entry.role,
                "format": entry.format,
            }
            for entry in sorted(self.entries, key=lambda item: item.path)
        ]
        return {
            "format": _INVENTORY_FORMAT,
            "version": _INVENTORY_VERSION,
            "providers": sorted(set(self._owners.values()) | {"core"}),
            "files": files,
        }

    def summary(self) -> dict[str, Any]:
        """Secret-free operational view: no paths, only classes and counts."""
        return {
            "providers": [
                {
                    "provider": coverage.provider,
                    "adapter_id": coverage.adapter_id,
                    "state_class": coverage.state_class,
                    "required_files": sum(
                        item.presence == "required" for item in coverage.files
                    ),
                    "export_only_files": sum(
                        item.presence == "export_only" for item in coverage.files
                    ),
                    "disposable_files": sum(
                        item.presence == "disposable" for item in coverage.files
                    ),
                    "notes": list(coverage.notes),
                }
                for coverage in sorted(
                    self.coverages, key=lambda item: (item.provider, item.adapter_id)
                )
            ],
            "selected_files": len(self.entries),
        }

    def write_inventory(self, destination: Path) -> None:
        """Publish the inventory atomically with private permissions."""
        destination = Path(destination).absolute()
        if destination.is_symlink() or destination.exists():
            raise FileExistsError("inventory destination must not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".medre-inventory-", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.inventory_document(), stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.chmod(temporary, 0o600)
            # Link publishes only if the destination is still absent. A
            # preflight exists() followed by replace() can overwrite a racer.
            os.link(temporary, destination)
            os.unlink(temporary)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


class _CorePaths:
    """Minimal settings view consumed by the core declaration."""

    def __init__(self, *, database_path: Path) -> None:
        self.database_path = database_path


class _CoreDeclaration:
    """Core state is core knowledge: resolved internally, not via the registry.

    Capture itself holds ``BEGIN IMMEDIATE`` on every selected SQLite file, so
    a separate core lock here would deadlock the very guard that excludes
    concurrent writers during the SQLite backup.
    """

    provider = "core"

    def coverage(self, config: _CorePaths, *, adapter_id: str) -> ProviderCoverage:
        database = Path(config.database_path)
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(
                DeclaredFile(
                    database,
                    "core_sqlite",
                    "sqlite",
                    purpose="core canonical event and outbox database",
                ),
            ),
        )

    def local_exclusion(self, config: _CorePaths, *, adapter_id: str) -> LocalExclusion:
        return LocalExclusion(
            self.provider,
            note=(
                "capture retains BEGIN IMMEDIATE on the core database; no "
                "separate core primitive exists"
            ),
        )


_CORE_DECLARATION = _CoreDeclaration()


def _core_database_path(config: RuntimeConfig, paths: MedrePaths) -> Path:
    return Path(config.storage.path) if config.storage.path else paths.database_path


def _prepare_instances(
    config: RuntimeConfig, paths: MedrePaths
) -> Iterator[tuple[str, str, str, AdapterSpec, object]]:
    """Yield ``(transport, adapter_id, kind, spec, prepared)`` without I/O."""
    from medre.adapter_registry import get_adapter_spec
    from medre.runtime.adapter_preparation import prepare_adapter_config
    from medre.runtime.route_engine import build_runtime_routes

    routes = tuple(build_runtime_routes(config.routes))
    for transport, adapter_id, wrapper in config.adapters.all_configs():
        if not wrapper.enabled:
            continue
        spec = get_adapter_spec(transport)
        if spec is None or wrapper.config is None:
            raise StoppedPlanError(
                f"configured instance {adapter_id!r} ({transport}) has no "
                "installed provider specification"
            )
        prepared = prepare_adapter_config(
            spec, adapter_id, wrapper.config, paths=paths, routes=routes
        )
        yield transport, adapter_id, wrapper.adapter_kind, spec, prepared


def instance_coverages(
    config: RuntimeConfig, paths: MedrePaths
) -> tuple[ProviderCoverage, ...]:
    """Resolve truthful coverage for core and every enabled configured instance."""
    coverages: list[ProviderCoverage] = [
        _CORE_DECLARATION.coverage(
            _CorePaths(database_path=_core_database_path(config, paths)),
            adapter_id="core",
        )
    ]
    for transport, adapter_id, adapter_kind, spec, prepared in _prepare_instances(
        config, paths
    ):
        if adapter_kind == "fake":
            coverages.append(
                declared(
                    provider=transport,
                    adapter_id=adapter_id,
                    files=(),
                    state_class="stateless_local",
                    notes=("fake instance uses no native state",),
                )
            )
            continue
        coverages.append(
            _coverage_of(declaration_from_spec(spec), prepared, adapter_id)
        )
    return tuple(coverages)


def build_stopped_capture_plan(
    config: RuntimeConfig,
    paths: MedrePaths,
    *,
    common_root: Path,
) -> StoppedCapturePlan:
    """Combine declarations into a closed, conflict-free capture selection."""
    return plan_from_coverages(common_root, instance_coverages(config, paths))


def plan_from_coverages(
    common_root: Path,
    coverages: Sequence[ProviderCoverage],
) -> StoppedCapturePlan:
    """Enforce a closed, conflict-free selection over resolved coverages.

    Rejects configured instances without declarations, required files outside
    the common root, conflicting ownership of one resolved file, and coverage
    that cannot honestly appear in a version-1 inventory.
    """
    common_root = Path(common_root).absolute()
    unsupported = sorted(
        f"{coverage.provider}/{coverage.adapter_id}"
        for coverage in coverages
        if coverage.state_class == "unsupported"
    )
    if unsupported:
        raise StoppedPlanError(
            "no stopped-state declaration for: " + ", ".join(unsupported)
        )
    owners: dict[str, tuple[str, SnapshotEntry]] = {}
    for coverage in coverages:
        for item in coverage.files:
            if item.presence != "required":
                continue
            relative = _relative_under(common_root, item.path, coverage.provider)
            if relative in owners:
                raise StoppedPlanError(
                    "conflicting state ownership for one selected file: "
                    f"{owners[relative][0]} and {coverage.provider} both "
                    "declare it"
                )
            try:
                entry = SnapshotEntry(relative, item.role, item.format)
            except ValueError as error:
                raise StoppedPlanError(
                    f"{coverage.provider}/{coverage.adapter_id}: the "
                    "selected path cannot be represented in the inventory "
                    "grammar"
                ) from error
            owners[relative] = (coverage.provider, entry)
    entries = tuple(
        value[1] for _, value in sorted(owners.items(), key=lambda item: item[0])
    )
    if sum(entry.role == "core_sqlite" for entry in entries) != 1:
        raise StoppedPlanError("a stopped capture requires exactly one core database")
    selected: dict[str, set[str]] = {}
    for entry in entries:
        selected.setdefault(owners[entry.path][0], set()).add(entry.path)
    for coverage in coverages:
        if coverage.provider == "core":
            continue
        owns = selected.get(coverage.provider, set())
        if coverage.state_class == "file_backed" and not owns:
            raise StoppedPlanError(
                f"{coverage.provider}/{coverage.adapter_id}: file-backed "
                "coverage owns no selected file"
            )
    mapping = {entry.path: owners[entry.path][0] for entry in entries}
    return StoppedCapturePlan(
        common_root=common_root,
        entries=entries,
        coverages=tuple(coverages),
        _owners=mapping,
    )


def capture_configured_stopped_state(
    config: RuntimeConfig,
    paths: MedrePaths,
    *,
    common_root: Path,
    snapshot: Path,
    runtime_stopped: bool,
) -> StoppedCapturePlan:
    """Capture one complete *supported* selection with exclusion held throughout.

    Dynamic provider resolution, if implemented, is an explicit read-only
    operation that runs after the provider's local owner primitive is retained.
    The ordinary ``plan`` API remains configuration-only and never performs
    that resolution. Providers requiring unimplemented exports are refused:
    this operation is not a partial/select-by-hand inventory capture.
    """
    if not runtime_stopped:
        raise StoppedPlanError("stopped runtime assertion is required")
    from medre.maintenance.stopped_snapshot import create_stopped_snapshot

    with ExitStack() as retained:
        coverages = [
            _CORE_DECLARATION.coverage(
                _CorePaths(database_path=_core_database_path(config, paths)),
                adapter_id="core",
            )
        ]
        for transport, adapter_id, kind, spec, prepared in _prepare_instances(
            config, paths
        ):
            if kind == "fake":
                coverages.append(
                    declared(
                        provider=transport,
                        adapter_id=adapter_id,
                        files=(),
                        state_class="stateless_local",
                        notes=("fake mode uses no native state",),
                    )
                )
                continue
            declaration = declaration_from_spec(spec)
            preview = _coverage_of(declaration, prepared, adapter_id)
            resolver = (
                declaration if isinstance(declaration, NativeStateResolver) else None
            )
            if preview.state_class == "unsupported" and resolver is None:
                raise StoppedPlanError(
                    "provider has no safe stopped-state capture selection"
                )
            if preview.state_class == "file_backed" or resolver is not None:
                exclusion = _exclusion_of(declaration, prepared, adapter_id)
                retained.callback(exclusion.close)
                if exclusion.scope != "local_only":
                    raise ExclusionRefused("provider has no supported local exclusion")
            coverage = (
                _coverage_of_resolved(resolver, prepared, adapter_id)
                if resolver is not None
                else preview
            )
            if coverage.provider != transport or coverage.adapter_id != adapter_id:
                raise StoppedPlanError(
                    "provider returned coverage for another instance"
                )
            if coverage.state_class == "file_backed" and not any(
                item.presence == "required" for item in coverage.files
            ):
                raise StoppedPlanError(
                    "file-backed coverage contains no required state"
                )
            if coverage.state_class in ("unsupported", "externally_managed") or any(
                item.presence == "export_only" for item in coverage.files
            ):
                raise StoppedPlanError("provider requires an unsupported native export")
            coverages.append(coverage)
        plan = plan_from_coverages(common_root, coverages)
        create_stopped_snapshot(
            common_root, snapshot, plan.entries, runtime_stopped=True
        )
        return plan


def _coverage_of_resolved(
    resolver: NativeStateResolver, prepared: object, adapter_id: str
) -> ProviderCoverage:
    """Contain native metadata parsing and I/O errors without exposing paths."""
    try:
        coverage = resolver.resolve_coverage(prepared, adapter_id=adapter_id)
    except (StoppedPlanError, ValueError):
        raise
    except Exception:
        raise StoppedPlanError("provider stopped-state resolution failed") from None
    if not isinstance(coverage, ProviderCoverage):
        raise StoppedPlanError("provider returned invalid resolved coverage")
    return coverage


def _relative_under(root: Path, path: Path, provider: str) -> str:
    """Translate one configured absolute path into a root-relative selection."""
    root = root.absolute()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError:
        raise StoppedPlanError(
            f"{provider} declares required state outside the common state root"
        ) from None
    cursor = root
    for component in (None, *relative.parts):
        if component is not None:
            cursor = cursor / component
        if cursor.is_symlink():
            raise StoppedPlanError("declared state cannot traverse a symlink")
    return relative.as_posix()


class LocalExclusion:
    """A held provider ownership primitive with honest scope claims.

    ``scope`` is ``local_only`` while a primitive is actually retained, or
    ``unsupported`` when no primitive exists; the note states what was and was
    not proven. Machine-local exclusion never proves that another host with
    the same native identity is inactive.
    """

    def __init__(self, provider: str, *, note: str) -> None:
        self.provider = provider
        self.scope: Literal["local_only", "unsupported"] = "unsupported"
        self.method = "none"
        self.note = note
        self._resources: list[tuple[str, Any]] = []

    # -- acquisition helpers used by provider declarations -------------------

    def retain_flock(self, path: Path, *, purpose: str) -> None:
        """Acquire the provider's own flock file without creating or seizing."""
        try:
            import fcntl
        except ImportError:
            raise ExclusionRefused(
                f"{self.provider}: {purpose} cannot be retained because this "
                "platform provides no flock primitive; exclusion is "
                "unsupported here"
            ) from None
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise ExclusionRefused(
                f"{self.provider}: {purpose} file is absent; the profile was "
                "never owned on this host, so exclusion cannot be claimed"
            ) from None
        except OSError as error:
            raise ExclusionRefused(
                f"{self.provider}: {purpose} could not be opened "
                f"({type(error).__name__})"
            ) from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ExclusionRefused(
                    f"{self.provider}: {purpose} is not a regular file"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ExclusionRefused:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            raise ExclusionRefused(
                f"{self.provider}: {purpose} is held by another process"
            ) from None
        self._resources.append(("flock", descriptor))
        self.method = "flock"
        self.scope = "local_only"

    def retain_sqlite_exclusive(self, path: Path, *, purpose: str) -> None:
        """Hold the provider's own SQLite lease database exclusively."""
        try:
            path = Path(path).absolute()
            if path.is_symlink() or not path.is_file():
                raise ExclusionRefused(
                    f"{self.provider}: {purpose} requires an existing regular lease"
                )
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=rw", uri=True, timeout=0, isolation_level=None
            )
        except sqlite3.Error as error:
            raise ExclusionRefused(
                f"{self.provider}: {purpose} could not be opened "
                f"({type(error).__name__})"
            ) from None
        try:
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error:
            connection.close()
            raise ExclusionRefused(
                f"{self.provider}: {purpose} is held by another process"
            ) from None
        self._resources.append(("sqlite", connection))
        self.method = "sqlite_exclusive"
        self.scope = "local_only"

    def close(self) -> None:
        """Release every retained primitive; never unlock on behalf of others."""
        for kind, resource in reversed(self._resources):
            if kind == "flock":
                os.close(resource)
            else:
                try:
                    resource.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                resource.close()
        self._resources.clear()
        self.scope = "unsupported"
        self.method = "none"


def unsupported_exclusion(provider: str, note: str) -> LocalExclusion:
    """Build the honest no-primitive exclusion report for one provider."""
    return LocalExclusion(provider, note=note)


@contextmanager
def retain_local_exclusion(
    config: RuntimeConfig,
    paths: MedrePaths,
    *,
    strict: bool = True,
) -> Iterator[tuple[LocalExclusion, ...]]:
    """Retain every provider's ownership primitive for the capture duration.

    Acquisition is all-or-nothing: one refusal releases the already held
    primitives. File-backed providers must supply a real retained primitive;
    with ``strict=False`` an honestly unsupported file-backed primitive is
    tolerated for that provider only and stays recorded as unsupported.
    Device-attached, stateless, and externally managed providers never block.
    """
    held: list[LocalExclusion] = []
    try:
        held.append(
            _CORE_DECLARATION.local_exclusion(
                _CorePaths(database_path=_core_database_path(config, paths)),
                adapter_id="core",
            )
        )
        for transport, adapter_id, kind, spec, prepared in _prepare_instances(
            config, paths
        ):
            if kind == "fake":
                continue
            declaration = declaration_from_spec(spec)
            coverage = _coverage_of(declaration, prepared, adapter_id)
            if coverage.state_class != "file_backed":
                continue
            exclusion = _exclusion_of(declaration, prepared, adapter_id)
            held.append(exclusion)
            if strict and transport != "core" and exclusion.scope == "unsupported":
                raise ExclusionRefused(
                    f"{coverage.provider}/{coverage.adapter_id}: " + exclusion.note
                )
        yield tuple(held)
    finally:
        for exclusion in held:
            exclusion.close()
