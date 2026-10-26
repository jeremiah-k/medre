"""Stopped-state operator inventory must never silently omit provider state."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import jsonschema
import pytest

from medre.maintenance.stopped_cli import main
from medre.maintenance.stopped_inventory import StoppedInventory


def _inventory(tmp_path):
    data = {
        "format": "medre-stopped-inventory",
        "version": 1,
        "providers": ["core", "simplexchat"],
        "files": [
            {
                "provider": "core",
                "path": "state/medre.sqlite",
                "role": "core_sqlite",
                "format": "sqlite",
            },
            {
                "provider": "simplexchat",
                "path": "simplex/profile-owner.sqlite",
                "role": "adapter_ledger",
                "format": "sqlite",
            },
            {
                "provider": "simplexchat",
                "path": "simplex/profile.key",
                "role": "native_identity",
                "format": "opaque",
            },
        ],
    }
    filename = tmp_path / "inventory.json"
    filename.write_text(json.dumps(data))
    return filename, data


def test_capture_verify_restore_with_explicit_provider_files(tmp_path):
    filename, _ = _inventory(tmp_path)
    root = tmp_path / "live-stopped"
    (root / "state").mkdir(parents=True)
    (root / "simplex").mkdir()
    for database in [
        root / "state/medre.sqlite",
        root / "simplex/profile-owner.sqlite",
    ]:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE retained(value TEXT)")
            connection.execute("INSERT INTO retained VALUES('owned')")
    (root / "simplex/profile.key").write_bytes(b"native-identity")
    snap = tmp_path / "snapshot"
    restored = tmp_path / "restored"
    assert (
        main(
            [
                "capture",
                "--inventory",
                str(filename),
                "--root",
                str(root),
                "--snapshot",
                str(snap),
            ]
        )
        == 1
    )
    assert not snap.exists()
    assert (
        main(
            [
                "capture",
                "--inventory",
                str(filename),
                "--root",
                str(root),
                "--snapshot",
                str(snap),
                "--stopped",
            ]
        )
        == 0
    )
    assert main(["verify", "--snapshot", str(snap)]) == 0
    assert (
        main(
            [
                "restore",
                "--snapshot",
                str(snap),
                "--destination",
                str(restored),
                "--stopped",
            ]
        )
        == 0
    )
    assert (restored / "simplex/profile.key").read_bytes() == b"native-identity"
    with closing(
        sqlite3.connect(restored / "simplex/profile-owner.sqlite")
    ) as connection:
        assert connection.execute("SELECT value FROM retained").fetchone() == ("owned",)
    assert (
        main(
            [
                "restore",
                "--snapshot",
                str(snap),
                "--destination",
                str(restored),
                "--stopped",
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["providers"].append("meshtastic"),
        lambda d: d["files"][0].update(provider="simplexchat"),
        lambda d: d["files"].append(d["files"][0]),
        lambda d: d["files"][2].update(path="../escape"),
        lambda d: d["providers"].append("core"),
        lambda d: d["files"][1].update(format="unexpected"),
    ],
)
def test_inventory_rejects_incomplete_or_ambiguous_selection(tmp_path, mutation):
    filename, data = _inventory(tmp_path)
    mutation(data)
    filename.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        StoppedInventory.load(filename)


def test_inventory_rejects_duplicate_json_keys(tmp_path):
    filename, _ = _inventory(tmp_path)
    filename.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError, match="duplicate"):
        StoppedInventory.load(filename)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no-follow open unavailable")
def test_inventory_replacement_symlink_is_refused_at_open(tmp_path, monkeypatch):
    """Replacing a checked inventory path cannot redirect the descriptor read."""
    filename, _ = _inventory(tmp_path)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(filename.read_bytes())
    original_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        """Replace the final component immediately before the actual OS open."""
        if Path(path) == filename:
            filename.unlink()
            filename.symlink_to(replacement)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced_open)
    with pytest.raises(OSError):
        StoppedInventory.load(filename)


def test_inventory_rejects_oversized_input(tmp_path):
    """A regular input cannot make the loader read beyond its documented bound."""
    filename = tmp_path / "oversized.json"
    filename.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="maximum size"):
        StoppedInventory.load(filename)


def test_inventory_schema_matches_operator_example(tmp_path):
    """Documented entries use the same roles and path rules as snapshot capture."""
    _, data = _inventory(tmp_path)
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "docs/schemas/stopped-state-inventory.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(data)
    data["files"][0]["provider"] = "simplexchat"
    assert list(validator.iter_errors(data))


def test_cli_refusal_does_not_disclose_private_inventory_path(tmp_path, capsys):
    """Malformed private input produces a structured failure without path text."""
    filename = tmp_path / "private-profile-secret.json"
    filename.write_text("not JSON")
    assert (
        main(
            [
                "capture",
                "--inventory",
                str(filename),
                "--root",
                str(tmp_path),
                "--snapshot",
                str(tmp_path / "unused"),
                "--stopped",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert str(filename) not in output and filename.name not in output
    assert json.loads(output) == {
        "operation": "capture",
        "status": "refused",
        "error_type": "JSONDecodeError",
    }


@pytest.mark.parametrize(
    "args",
    [
        ["capture", "--inventorx", "/secret/private-native-profile.key"],
        ["verify", "--snapshot", "/secret/backup", "--extra", "/secret/passphrase"],
        ["capture", "--root", "/secret/private-state"],
    ],
)
def test_cli_argument_errors_never_echo_private_paths(args, capsys):
    """Argument parsing itself must respect the no-private-path output rule."""
    with pytest.raises(SystemExit) as failure:
        main(args)
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "invalid arguments" in output.err
    assert "/secret/" not in output.err
    assert "--inventorx" not in output.err
    assert "--extra" not in output.err
