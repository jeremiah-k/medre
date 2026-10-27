# medre-test-tier: compatibility
"""Core consumers of provider-owned stopped-state declarations.

The provider declarations themselves ship with each adapter distribution and
are exercised there. Core is tested against fake declarations and fake specs
built in this file, so the consumer machinery needs no adapter distribution:
resolution through the optional spec symbol, honest unsupported reporting,
closed-selection enforcement, and the ownership primitives.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import pytest

from medre.adapter_spec import AdapterSpec, SymbolRef
from medre.config.model import (
    AdapterConfigSet,
    GenericAdapterRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths
from medre.maintenance.stopped_declare import (
    DeclaredFile,
    ProviderCoverage,
    ExclusionRefused,
    LocalExclusion,
    StoppedPlanError,
    StoppedStateDeclaration,
    build_stopped_capture_plan,
    declaration_from_spec,
    declared,
    instance_coverages,
    plan_from_coverages,
    retain_local_exclusion,
)
from medre.maintenance.stopped_inventory import StoppedInventory


def _write(path: Path, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _sqlite(path: Path, table: str = "t") -> Path:
    _write(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"CREATE TABLE {table}(value TEXT)")
    return path


def _lease_database(path: Path) -> Path:
    return _sqlite(path, table="lease_holder")


# ---------------------------------------------------------------------------
# Fake declarations and specs (provider knowledge lives in the distributions)
# ---------------------------------------------------------------------------


class _FakeSettings(Protocol):
    """Structural settings view consumed by the fake declarations."""

    state_dir: Path


class _FileBackedDeclaration:
    """A minimal honest file-backed declaration used only by core tests."""

    provider = "acme"

    def coverage(self, config: _FakeSettings, *, adapter_id: str) -> ProviderCoverage:
        state_dir = Path(config.state_dir)
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(
                DeclaredFile(
                    state_dir / "journal.sqlite",
                    "adapter_ledger",
                    "sqlite",
                    purpose="fake journal",
                ),
                DeclaredFile(
                    state_dir / "marker.bin",
                    "native_identity",
                    "opaque",
                    purpose="fake identity marker",
                ),
            ),
            notes=("fake declaration note",),
        )

    def local_exclusion(
        self, config: _FakeSettings, *, adapter_id: str
    ) -> LocalExclusion:
        exclusion = LocalExclusion(self.provider, note="uninitialized")
        exclusion.retain_flock(
            Path(config.state_dir) / "guard.lock",
            purpose="fake guard",
        )
        exclusion.note = "local flock exclusion retained"
        return exclusion


class _LockedDeclaration:
    """File-backed shape with honestly no local ownership primitive."""

    provider = "locked"

    def coverage(self, config: _FakeSettings, *, adapter_id: str) -> ProviderCoverage:
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(
                DeclaredFile(
                    Path(config.state_dir) / "state.db",
                    "adapter_ledger",
                    "sqlite",
                    purpose="fake state database",
                ),
            ),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        return LocalExclusion(
            self.provider,
            note="no retained local primitive exists for this provider",
        )


def _fake_declaration() -> StoppedStateDeclaration:
    return _FileBackedDeclaration()


def _locked_declaration() -> StoppedStateDeclaration:
    return _LockedDeclaration()


def _not_a_declaration() -> int:
    return 4


def _fake_spec(transport: str, factory: SymbolRef | None = None) -> AdapterSpec:
    return AdapterSpec(
        transport=transport,
        config=SymbolRef("tests.test_stopped_declarations", "_never_loaded"),
        runtime_config=None,
        adapter=SymbolRef("tests.test_stopped_declarations", "_never_loaded"),
        renderer_factory=None,
        dependency_probe=None,
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
        native_namespace_reader=SymbolRef(
            "tests.test_stopped_declarations", "_never_loaded"
        ),
        versioned_namespace_reader=SymbolRef(
            "tests.test_stopped_declarations", "_never_loaded"
        ),
        attribution_projector=SymbolRef(
            "tests.test_stopped_declarations", "_never_loaded"
        ),
        stopped_state_declaration=factory,
    )


def _config_with(tmp_path: Path, groups: dict[str, dict[str, object]]) -> RuntimeConfig:
    wrappers = {
        transport: {
            name: GenericAdapterRuntimeConfig(
                adapter_id=name, enabled=True, config=config
            )
            for name, config in instances.items()
        }
        for transport, instances in groups.items()
    }
    return RuntimeConfig(
        storage=StorageConfig(path=str(_sqlite(tmp_path / "state" / "medre.sqlite"))),
        adapters=AdapterConfigSet(groups=wrappers),
    )


def _paths(tmp_path: Path) -> MedrePaths:
    return MedrePaths(
        config_dir=None,
        config_file=tmp_path / "config.yaml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        database_path=tmp_path / "state" / "medre.sqlite",
    )


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_missing_reference_reports_unsupported_instead_of_guessing():
    declaration = declaration_from_spec(_fake_spec("acme", None))
    coverage = declaration.coverage(SimpleNamespace(), adapter_id="one")
    assert coverage.state_class == "unsupported"
    assert coverage.files == ()
    assert "no stopped-state declaration" in coverage.notes[0]


def test_loadable_factory_resolves_the_declaration():
    spec = _fake_spec(
        "acme", SymbolRef("tests.test_stopped_declarations", "_fake_declaration")
    )
    declaration = declaration_from_spec(spec)
    assert declaration.provider == "acme"
    assert declaration is not declaration_from_spec(spec)


def test_unloadable_module_and_missing_symbol_stay_unsupported():
    broken_module = declaration_from_spec(
        _fake_spec("acme", SymbolRef("tests.no_such_module", "_fake_declaration"))
    )
    assert broken_module.coverage(SimpleNamespace(), adapter_id="a").state_class == (
        "unsupported"
    )
    missing_symbol = declaration_from_spec(
        _fake_spec("acme", SymbolRef("tests.test_stopped_declarations", "_absent"))
    )
    coverage = missing_symbol.coverage(SimpleNamespace(), adapter_id="a")
    assert coverage.state_class == "unsupported"


def test_nonconforming_object_is_reported_not_trusted():
    spec = _fake_spec(
        "acme", SymbolRef("tests.test_stopped_declarations", "_not_a_declaration")
    )
    coverage = declaration_from_spec(spec).coverage(SimpleNamespace(), adapter_id="a")
    assert coverage.state_class == "unsupported"
    assert "seam" in coverage.notes[0]


# ---------------------------------------------------------------------------
# Consumer machinery over resolved coverages
# ---------------------------------------------------------------------------


def _file_backed_config(tmp_path: Path, name: str) -> RuntimeConfig:
    return _config_with(
        tmp_path,
        {
            "meshtastic": {
                name: SimpleNamespace(state_dir=str(tmp_path / "state" / name))
            }
        },
    )


def test_instance_coverages_resolve_through_the_spec_symbol(tmp_path, monkeypatch):
    config = _file_backed_config(tmp_path, "one")
    spec = _fake_spec(
        "meshtastic", SymbolRef("tests.test_stopped_declarations", "_fake_declaration")
    )
    monkeypatch.setattr(
        "medre.adapter_registry.get_adapter_spec", lambda transport: spec
    )
    coverages = instance_coverages(config, _paths(tmp_path))
    by_provider = {item.provider: item for item in coverages}
    assert by_provider["core"].state_class == "file_backed"
    assert by_provider["acme"].state_class == "file_backed"
    required = {item.path.name for item in by_provider["acme"].files}
    assert required == {"journal.sqlite", "marker.bin"}


def test_installed_distribution_without_symbol_is_unsupported(tmp_path, monkeypatch):
    config = _file_backed_config(tmp_path, "one")
    monkeypatch.setattr(
        "medre.adapter_registry.get_adapter_spec",
        lambda transport: _fake_spec(transport, None),
    )
    with pytest.raises(StoppedPlanError, match="no stopped-state declaration"):
        build_stopped_capture_plan(config, _paths(tmp_path), common_root=tmp_path)


def test_fake_instances_own_no_native_state(tmp_path, monkeypatch):
    config = RuntimeConfig(
        storage=StorageConfig(path=str(_sqlite(tmp_path / "state" / "medre.sqlite"))),
        adapters=AdapterConfigSet(
            groups={
                "meshtastic": {
                    "fake": GenericAdapterRuntimeConfig(
                        adapter_id="fake",
                        enabled=True,
                        adapter_kind="fake",
                        config=SimpleNamespace(state_dir=str(tmp_path)),
                    )
                }
            }
        ),
    )
    monkeypatch.setattr(
        "medre.adapter_registry.get_adapter_spec",
        lambda transport: _fake_spec(transport, None),
    )
    coverages = instance_coverages(config, _paths(tmp_path))
    fake = next(item for item in coverages if item.adapter_id == "fake")
    assert fake.state_class == "stateless_local"
    assert fake.files == ()


def test_plan_from_coverages_rejects_conflicting_ownership(tmp_path):
    shared = tmp_path / "state" / "shared"

    def state_files(root: Path) -> tuple[DeclaredFile, ...]:
        return (
            DeclaredFile(root / "journal.sqlite", "adapter_ledger", "sqlite"),
            DeclaredFile(root / "marker.bin", "native_identity", "opaque"),
        )

    first = declared(
        provider="acme", adapter_id="one", files=state_files(shared / "one")
    )
    second = declared(
        provider="acme",
        adapter_id="two",
        files=state_files(shared / "two")
        + (
            DeclaredFile(shared / "one" / "journal.sqlite", "adapter_ledger", "sqlite"),
        ),
    )
    core = declared(
        provider="core",
        adapter_id="core",
        files=(
            DeclaredFile(tmp_path / "state" / "medre.sqlite", "core_sqlite", "sqlite"),
        ),
    )
    with pytest.raises(StoppedPlanError, match="conflicting"):
        plan_from_coverages(tmp_path, [core, first, second])


def test_plan_rejects_required_state_outside_common_root(tmp_path):
    core = declared(
        provider="core",
        adapter_id="core",
        files=(
            DeclaredFile(tmp_path / "state" / "medre.sqlite", "core_sqlite", "sqlite"),
        ),
    )
    outside = declared(
        provider="acme",
        adapter_id="one",
        files=(
            DeclaredFile(
                tmp_path / "elsewhere" / "journal.sqlite",
                "adapter_ledger",
                "sqlite",
            ),
        ),
    )
    with pytest.raises(StoppedPlanError, match="outside"):
        plan_from_coverages(tmp_path / "state", [core, outside])


def test_plan_distinguishes_state_classes_and_omits_stateless_providers(tmp_path):
    core = declared(
        provider="core",
        adapter_id="core",
        files=(
            DeclaredFile(tmp_path / "state" / "medre.sqlite", "core_sqlite", "sqlite"),
        ),
    )
    encrypted = declared(
        provider="locked",
        adapter_id="cipher",
        files=(),
        state_class="externally_managed",
        notes=("requires provider export",),
    )
    device = declared(
        provider="radio",
        adapter_id="ota",
        files=(),
        state_class="device_attached",
        notes=("firmware state is outside capture",),
    )
    stateless = declared(
        provider="air",
        adapter_id="relay",
        files=(),
        state_class="stateless_local",
        notes=("credential lives in configuration",),
    )
    plan = plan_from_coverages(tmp_path / "state", [core, encrypted, device, stateless])
    document = plan.inventory_document()
    assert document["providers"] == ["core"]
    classes = {
        row["provider"]: row["state_class"] for row in plan.summary()["providers"]
    }
    assert classes == {
        "air": "stateless_local",
        "core": "file_backed",
        "locked": "externally_managed",
        "radio": "device_attached",
    }
    assert plan.summary()["selected_files"] == 1


def test_declaration_helpers_reject_dishonest_shapes(tmp_path):
    with pytest.raises(StoppedPlanError):
        declared(provider="p", adapter_id="a", files=(), state_class="file_backed")
    with pytest.raises(StoppedPlanError):
        declared(
            provider="p",
            adapter_id="a",
            files=(DeclaredFile(tmp_path / "f", "native_identity", "opaque"),),
            state_class="stateless_local",
        )
    with pytest.raises(ValueError):
        DeclaredFile(Path("relative.db"), "native_identity", "opaque")


def test_inventory_document_round_trips_through_the_operator_loader(tmp_path):
    core = declared(
        provider="core",
        adapter_id="core",
        files=(
            DeclaredFile(tmp_path / "state" / "medre.sqlite", "core_sqlite", "sqlite"),
        ),
    )
    provider = declared(
        provider="acme",
        adapter_id="one",
        files=(
            DeclaredFile(
                tmp_path / "state" / "journal.sqlite", "adapter_ledger", "sqlite"
            ),
            DeclaredFile(
                tmp_path / "state" / "marker.bin", "native_identity", "opaque"
            ),
        ),
    )
    plan = plan_from_coverages(tmp_path / "state", [core, provider])
    document = plan.inventory_document()
    filename = tmp_path / "inventory.json"
    plan.write_inventory(filename)
    assert json.loads(filename.read_text(encoding="utf-8")) == document
    loaded = StoppedInventory.load(filename)
    assert loaded.providers == ("acme", "core")
    assert len(loaded.entries) == 3


# ---------------------------------------------------------------------------
# Ownership primitives
# ---------------------------------------------------------------------------


def test_flock_refusal_and_release_round_trip(tmp_path):
    guard = _write(tmp_path / "guard.lock")
    exclusion = LocalExclusion("acme", note="probe")
    exclusion.retain_flock(guard, purpose="guard")
    assert exclusion.scope == "local_only"
    competitor = LocalExclusion("acme", note="probe")
    with pytest.raises(ExclusionRefused, match="held"):
        competitor.retain_flock(guard, purpose="guard")
    exclusion.close()
    competitor.retain_flock(guard, purpose="guard")
    assert competitor.scope == "local_only"
    competitor.close()


def test_flock_refuses_to_seize_an_absent_guard(tmp_path):
    exclusion = LocalExclusion("acme", note="probe")
    with pytest.raises(ExclusionRefused, match="never owned"):
        exclusion.retain_flock(tmp_path / "absent.lock", purpose="guard")


def test_sqlite_lease_refusal_and_release(tmp_path):
    lease = tmp_path / "lease.sqlite"
    _lease_database(lease)
    holder = sqlite3.connect(lease, timeout=0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    exclusion = LocalExclusion("chatmail", note="probe")
    with pytest.raises(ExclusionRefused, match="held"):
        exclusion.retain_sqlite_exclusive(lease, purpose="lease")
    holder.execute("ROLLBACK")
    holder.close()
    exclusion.retain_sqlite_exclusive(lease, purpose="lease")
    assert exclusion.scope == "local_only"
    # A held lease must not block SQLite backup of OTHER captured files.
    captured = _sqlite(tmp_path / "db.sqlite")
    with closing(sqlite3.connect(captured, timeout=0)) as reader:
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (0,)
    exclusion.close()


def test_retained_exclusion_is_strict_for_file_backed_providers(tmp_path, monkeypatch):
    config = _config_with(
        tmp_path,
        {
            "meshtastic": {
                "one": SimpleNamespace(state_dir=str(tmp_path / "state")),
                "two": SimpleNamespace(state_dir=str(tmp_path / "state2")),
            }
        },
    )
    spec = _fake_spec(
        "meshtastic",
        SymbolRef("tests.test_stopped_declarations", "_locked_declaration"),
    )
    monkeypatch.setattr(
        "medre.adapter_registry.get_adapter_spec", lambda transport: spec
    )
    with pytest.raises(ExclusionRefused, match="locked/one"):
        with retain_local_exclusion(config, _paths(tmp_path)):
            pass
    # Lenient mode tolerates the honest unsupported primitive and records it.
    with retain_local_exclusion(config, _paths(tmp_path), strict=False) as held:
        scopes = {item.provider: item.scope for item in held}
    assert scopes["locked"] == "unsupported"
    assert scopes["core"] == "unsupported"


def test_retained_exclusion_holds_file_backed_primitives(tmp_path, monkeypatch):
    guard = _write(tmp_path / "state" / "one" / "guard.lock")
    config = _file_backed_config(tmp_path, "one")
    spec = _fake_spec(
        "meshtastic", SymbolRef("tests.test_stopped_declarations", "_fake_declaration")
    )
    monkeypatch.setattr(
        "medre.adapter_registry.get_adapter_spec", lambda transport: spec
    )
    with retain_local_exclusion(config, _paths(tmp_path)) as held:
        scopes = {item.provider: item.scope for item in held}
        assert scopes["acme"] == "local_only"
        competitor = LocalExclusion("acme", note="probe")
        with pytest.raises(ExclusionRefused):
            competitor.retain_flock(guard, purpose="guard")
    # Released after the context exits; the primitive is free again.
    free = LocalExclusion("acme", note="probe")
    free.retain_flock(guard, purpose="guard")
    free.close()


def test_retain_flock_refuses_without_fcntl_primitive(monkeypatch, tmp_path):
    """Platforms without a flock primitive get a typed refusal, not a crash."""
    guard = tmp_path / "guard"
    guard.write_bytes(b"")
    exclusion = LocalExclusion("acme", note="probe")
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(ExclusionRefused, match="flock primitive"):
        exclusion.retain_flock(guard, purpose="guard")
    assert exclusion.scope == "unsupported"


def test_inventory_publication_preserves_a_destination_created_during_staging(
    tmp_path, monkeypatch
):
    """A concurrent destination wins without being overwritten by publication."""
    core = declared(
        provider="core",
        adapter_id="core",
        files=(DeclaredFile(tmp_path / "medre.sqlite", "core_sqlite", "sqlite"),),
    )
    plan = plan_from_coverages(tmp_path, [core])
    destination = tmp_path / "inventory.json"
    import os

    link = os.link

    def race(source, target):
        destination.write_text("other administrator's inventory")
        return link(source, target)

    monkeypatch.setattr("medre.maintenance.stopped_declare.os.link", race)
    with pytest.raises(FileExistsError):
        plan.write_inventory(destination)
    assert destination.read_text() == "other administrator's inventory"
    assert not list(tmp_path.glob(".medre-inventory-*"))


def test_sqlite_exclusion_does_not_create_a_missing_lease(tmp_path):
    """An absent native lease is not manufactured as evidence of exclusion."""
    lease = tmp_path / "missing.sqlite"
    exclusion = LocalExclusion("chatmail", note="probe")
    with pytest.raises(ExclusionRefused):
        exclusion.retain_sqlite_exclusive(lease, purpose="lease")
    assert not lease.exists()
    assert exclusion.scope == "unsupported"


def test_released_exclusion_does_not_claim_a_retained_primitive(tmp_path):
    """Local exclusion exists only while its resource remains held."""
    lease = _lease_database(tmp_path / "lease.sqlite")
    exclusion = LocalExclusion("chatmail", note="probe")
    exclusion.retain_sqlite_exclusive(lease, purpose="lease")
    exclusion.close()
    assert exclusion.scope == "unsupported"
    assert exclusion.method == "none"


@pytest.mark.skipif(
    sys.platform == "win32", reason="requires unprivileged symlink creation"
)
def test_declaration_keeps_symlink_paths_from_becoming_capture_authority(tmp_path):
    """Resolving a configured alias must not bypass snapshot symlink refusal."""
    target = _write(tmp_path / "identity")
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    core = declared(
        provider="core",
        adapter_id="core",
        files=(DeclaredFile(tmp_path / "core.sqlite", "core_sqlite", "sqlite"),),
    )
    provider = declared(
        provider="acme",
        adapter_id="one",
        files=(DeclaredFile(alias, "native_identity", "opaque"),),
    )
    with pytest.raises(StoppedPlanError, match="symlink"):
        plan_from_coverages(tmp_path, [core, provider])
