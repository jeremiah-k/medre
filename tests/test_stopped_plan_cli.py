# medre-test-tier: compatibility
"""Declared stopped-state captures through the maintenance CLI.

Provider knowledge ships with the adapter distributions; this file exercises
the core CLI against fake declarations resolved through the spec symbol, over
synthetic native state shaped by those fake declarations. No native process,
server, or radio is involved. The full CLI path is covered: plan derivation,
inventory publication, capture, verification, restore into an absent
destination, exclusion retention across capture, lock refusal, missing state
refusal, and secret-free operational output.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

import jsonschema
import pytest

from medre.adapter_spec import SymbolRef
from medre.maintenance.stopped_cli import main
from medre.maintenance.stopped_declare import (
    DeclaredFile,
    ProviderCoverage,
    LocalExclusion,
    StoppedStateDeclaration,
    declared,
)


def _sqlite_database(path: Path, table: str = "kept") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"CREATE TABLE {table}(value TEXT)")
        connection.execute(f"INSERT INTO {table} VALUES('committed')")


def _write(path: Path, payload: bytes = b"native") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


# ---------------------------------------------------------------------------
# Fake declarations used only to drive the core CLI end to end
# ---------------------------------------------------------------------------


class _FakeSettings(Protocol):
    """Structural settings view consumed by the fake declarations."""

    state_dir: Path


class _FakeFileBacked:
    """Journal + marker + flock guard, for the file-backed CLI scenario."""

    provider = "simplexchat"

    def coverage(self, config: _FakeSettings, *, adapter_id: str) -> ProviderCoverage:
        state_dir = Path(config.state_dir)
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(
                DeclaredFile(state_dir / "journal.sqlite", "adapter_ledger", "sqlite"),
                DeclaredFile(state_dir / "marker.bin", "native_identity", "opaque"),
            ),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        exclusion = LocalExclusion(self.provider, note="uninitialized")
        exclusion.retain_flock(
            Path(config.state_dir) / "guard.lock", purpose="fake guard"
        )
        exclusion.note = "local flock exclusion retained"
        return exclusion


class _FakeStateless:
    provider = "xmpp"

    def coverage(self, config: object, *, adapter_id: str) -> object:
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(),
            state_class="stateless_local",
            notes=("credential lives in configuration",),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        return LocalExclusion(self.provider, note="nothing local to exclude")


class _FakeDevice:
    provider = "meshtastic"

    def coverage(self, config: object, *, adapter_id: str) -> object:
        return declared(
            provider=self.provider,
            adapter_id=adapter_id,
            files=(),
            state_class="device_attached",
            notes=("firmware state is outside capture",),
        )

    def local_exclusion(self, config: object, *, adapter_id: str) -> LocalExclusion:
        return LocalExclusion(
            self.provider, note="device ownership cannot be proven from files"
        )


def _simplexchat_declaration() -> StoppedStateDeclaration:
    return _FakeFileBacked()


def _xmpp_declaration() -> StoppedStateDeclaration:
    return _FakeStateless()


def _meshtastic_declaration() -> StoppedStateDeclaration:
    return _FakeDevice()


@pytest.fixture()
def overlaid_specs(monkeypatch):
    """Attach this file's fake declarations to the installed specs."""
    from medre.adapter_registry import get_adapter_spec

    factories = {
        "simplexchat": "_simplexchat_declaration",
        "xmpp": "_xmpp_declaration",
        "meshtastic": "_meshtastic_declaration",
    }

    def resolved(transport: str):
        real = get_adapter_spec(transport)
        if transport not in factories:
            return real
        return replace(
            real,
            stopped_state_declaration=SymbolRef(
                "tests.test_stopped_plan_cli", factories[transport]
            ),
        )

    monkeypatch.setattr("medre.adapter_registry.get_adapter_spec", resolved)
    # Sanity: the settings object shape the fake declarations consume.
    return SimpleNamespace(factories=tuple(sorted(factories)))


@pytest.fixture()
def installation(tmp_path: Path) -> Path:
    """Synthetic stopped state matching the fake file-backed declaration."""
    root = tmp_path / "state"
    _sqlite_database(root / "medre.sqlite", "events")
    # Committed WAL content must survive the backup protocol.
    _sqlite_database(root / "simplexchat" / "journal.sqlite", "journal")
    _write(root / "simplexchat" / "marker.bin", b"identity-marker")
    _write(root / "simplexchat" / "guard.lock", b"guard-inode")
    return root


@pytest.fixture()
def config_file(tmp_path: Path, installation: Path) -> Path:
    text = f"""
runtime:
  name: stopped-maintenance

storage:
  path: "{installation / 'medre.sqlite'}"

adapters:
  simplexchat:
    main:
      enabled: true
      database_prefix: "{installation / 'simplexchat' / 'profile'}"
      state_dir: "{installation / 'simplexchat'}"
  xmpp:
    fallback:
      enabled: true
      jid: "relay@example.org"
  meshtastic:
    radio:
      enabled: true
      connection_type: tcp
      host: "192.0.2.1"
"""
    filename = tmp_path / "stopped-config.yaml"
    filename.write_text(text, encoding="utf-8")
    return filename


def _plan(tmp_path: Path, config_file: Path, capsys) -> tuple[int, Path]:
    inventory = tmp_path / "inventory.json"
    code = main(
        [
            "plan",
            "--config",
            str(config_file),
            "--root",
            str(tmp_path / "state"),
            "--inventory",
            str(inventory),
        ]
    )
    return code, inventory


def test_plan_writes_private_schema_valid_inventory(
    tmp_path, config_file, installation, capsys, overlaid_specs
):
    del installation, overlaid_specs
    code, inventory = _plan(tmp_path, config_file, capsys)
    assert code == 0
    mode = stat.S_IMODE(inventory.stat().st_mode)
    assert mode & 0o077 == 0
    document = json.loads(inventory.read_text(encoding="utf-8"))
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/schemas/stopped-state-inventory.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(document)
    assert sorted(document["providers"]) == ["core", "simplexchat"]
    relative_paths = {row["path"] for row in document["files"]}
    assert relative_paths == {
        "medre.sqlite",
        "simplexchat/journal.sqlite",
        "simplexchat/marker.bin",
    }
    # Operational output stays secret-free: provider classes only.
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    summary = json.loads(output)["summary"]
    classes = {row["provider"]: row["state_class"] for row in summary["providers"]}
    assert classes["simplexchat"] == "file_backed"
    assert classes["xmpp"] == "stateless_local"
    assert classes["meshtastic"] == "device_attached"
    assert "adapter_id" not in summary["providers"][0]


def test_capture_verify_restore_round_trip(
    tmp_path, config_file, installation, capsys, overlaid_specs
):
    del overlaid_specs
    _, inventory = _plan(tmp_path, config_file, capsys)
    snapshot = tmp_path / "snapshot"
    restored = tmp_path / "restored"
    assert (
        main(
            [
                "capture",
                "--inventory",
                str(inventory),
                "--root",
                str(installation),
                "--snapshot",
                str(snapshot),
            ]
        )
        == 1
    )
    assert not snapshot.exists()
    assert (
        main(
            [
                "capture",
                "--inventory",
                str(inventory),
                "--root",
                str(installation),
                "--snapshot",
                str(snapshot),
                "--stopped",
            ]
        )
        == 0
    )
    assert main(["verify", "--snapshot", str(snapshot)]) == 0
    assert (
        main(
            [
                "restore",
                "--snapshot",
                str(snapshot),
                "--destination",
                str(restored),
                "--stopped",
            ]
        )
        == 0
    )
    with closing(
        sqlite3.connect(restored / "simplexchat/journal.sqlite")
    ) as connection:
        assert connection.execute("SELECT value FROM journal").fetchone() == (
            "committed",
        )
    assert (restored / "simplexchat/marker.bin").read_bytes() == b"identity-marker"
    # Restoring again into the same destination is refused, not overwritten.
    assert (
        main(
            [
                "restore",
                "--snapshot",
                str(snapshot),
                "--destination",
                str(restored),
                "--stopped",
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_retained_exclusion_extends_over_library_capture(
    tmp_path, config_file, installation, overlaid_specs
):
    """Check-to-capture retention: hold primitives while the backup runs."""
    import fcntl

    from medre.config.env import apply_env_overrides
    from medre.config.loader import load_config
    from medre.maintenance.stopped_declare import (
        build_stopped_capture_plan,
        retain_local_exclusion,
    )
    from medre.maintenance.stopped_snapshot import create_stopped_snapshot

    del overlaid_specs
    config, _source, paths = load_config(config_file)
    config = apply_env_overrides(config, paths)
    built = build_stopped_capture_plan(config, paths, common_root=installation)
    snapshot = tmp_path / "held-capture"
    guard = installation / "simplexchat/guard.lock"

    def competing_flock() -> int:
        descriptor = os.open(guard, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise
        return descriptor

    with retain_local_exclusion(config, paths) as held:
        scopes = {item.provider: item.scope for item in held}
        assert scopes["simplexchat"] == "local_only"
        # A held flock must not block the SQLite backup of captured files.
        create_stopped_snapshot(
            installation, snapshot, built.entries, runtime_stopped=True
        )
        # While held, a competing owner (separate descriptor) cannot start.
        with pytest.raises(OSError):
            os.close(competing_flock())
    assert (snapshot / "manifest.json").is_file()
    # After release the primitive is free again.
    os.close(competing_flock())


def test_check_refuses_while_guard_is_held(config_file, installation, overlaid_specs):
    del overlaid_specs
    import fcntl

    descriptor = os.open(installation / "simplexchat/guard.lock", os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (
            main(["check", "--config", str(config_file), "--root", str(installation)])
            == 1
        )
    finally:
        os.close(descriptor)
    # With the primitive released the same check holds every lease.
    assert (
        main(["check", "--config", str(config_file), "--root", str(installation)]) == 0
    )


def test_capture_refuses_when_required_native_state_is_missing(
    tmp_path, config_file, installation, capsys, overlaid_specs
):
    del overlaid_specs
    _, inventory = _plan(tmp_path, config_file, capsys)
    (installation / "simplexchat/marker.bin").unlink()
    code = main(
        [
            "capture",
            "--inventory",
            str(inventory),
            "--root",
            str(installation),
            "--snapshot",
            str(tmp_path / "unused"),
            "--stopped",
        ]
    )
    assert code == 1
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output["status"] == "refused"
    assert "marker" not in json.dumps(output)


def test_plan_reflects_environment_database_override(
    tmp_path, config_file, installation, monkeypatch, capsys, overlaid_specs
):
    del overlaid_specs
    _sqlite_database(installation / "core-alt.sqlite", "events")
    monkeypatch.setenv("MEDRE_DB_PATH", str(installation / "core-alt.sqlite"))
    inventory = tmp_path / "inventory.json"
    code = main(
        [
            "plan",
            "--config",
            str(config_file),
            "--root",
            str(installation),
            "--inventory",
            str(inventory),
        ]
    )
    assert code == 0
    document = json.loads(inventory.read_text(encoding="utf-8"))
    core_rows = [row for row in document["files"] if row["provider"] == "core"]
    assert core_rows == [
        {
            "provider": "core",
            "path": "core-alt.sqlite",
            "role": "core_sqlite",
            "format": "sqlite",
        }
    ]
    capsys.readouterr()


@pytest.mark.parametrize(
    ("command", "failure"),
    [
        ("plan", "preparation"),
        ("plan", "coverage"),
        ("plan", "coverage_type"),
        ("check", "preparation"),
        ("check", "coverage"),
        ("check", "coverage_type"),
        ("check", "exclusion"),
        ("check", "exclusion_type"),
    ],
)
def test_provider_failures_use_private_maintenance_refusals(
    tmp_path,
    config_file,
    installation,
    overlaid_specs,
    monkeypatch,
    capsys,
    command,
    failure,
):
    """Provider bugs cannot turn native profile paths into raw CLI tracebacks."""
    from medre.runtime.errors import RuntimeConfigError

    private = "private-profile-token-must-not-appear"

    def broken(*args, **kwargs):
        raise AttributeError(private)

    def broken_preparation(*args, **kwargs):
        raise RuntimeConfigError(private)

    if failure == "preparation":
        monkeypatch.setattr(
            "medre.runtime.adapter_preparation.prepare_adapter_config",
            broken_preparation,
        )
    elif failure.startswith("coverage"):
        monkeypatch.setattr(
            _FakeFileBacked,
            "coverage",
            (lambda *a, **kw: object()) if failure.endswith("type") else broken,
        )
    else:
        monkeypatch.setattr(
            _FakeFileBacked,
            "local_exclusion",
            (lambda *a, **kw: object()) if failure.endswith("type") else broken,
        )
    arguments = [command, "--config", str(config_file), "--root", str(installation)]
    if command == "plan":
        arguments += ["--inventory", str(tmp_path / "inventory.json")]
    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    result = json.loads(captured.out.strip().splitlines()[-1])
    assert result["status"] == "refused"
    assert not (tmp_path / "inventory.json").exists()
