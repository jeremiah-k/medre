"""Operator workflow, safe writes, noninteractive use and cancellation."""

import io
import json
import os
from dataclasses import replace
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from tests.helpers.bridge_setup import CONFIG, ChatConfig, install_provider
from tests.helpers.cli import _run_cli_raw


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("sys.stdin", io.StringIO())


def flags(tmp_path, *, config=True):
    result = [
        "config",
        "bridge",
        "--output",
        str(tmp_path / "bridge.yaml"),
        "--source",
        "left",
        "--dest",
        "right",
        "--direction",
        "bidirectional",
        "--map",
        "room=α",
        "0",
        "--json",
    ]
    if config:
        path = tmp_path / "input.yaml"
        path.write_text(CONFIG)
        result.extend(["--config", str(path)])
    return result


def test_noninteractive_draft_is_valid_private_and_does_not_expose_secrets(tmp_path):
    stdout, stderr, code = _run_cli_raw(*flags(tmp_path))
    assert code == 0, stderr
    assert json.loads(stdout)["total_legs"] == 2
    assert "keep-this-private" not in stdout + stderr
    assert "Saved" in stderr
    path = tmp_path / "bridge.yaml"
    assert "keep-this-private" in path.read_text()
    assert (tmp_path / "input.yaml").read_text() == CONFIG
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "home").exists()


def test_preview_writes_nothing(tmp_path):
    stdout, stderr, code = _run_cli_raw(*flags(tmp_path), "--preview")
    assert code == 0, stderr
    assert json.loads(stdout)["total_legs"] == 2
    assert not (tmp_path / "bridge.yaml").exists()
    assert "Preview only" in stderr


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_existing_output_is_never_followed_or_overwritten(kind, tmp_path):
    args = flags(tmp_path)
    output = tmp_path / "bridge.yaml"
    target = tmp_path / "original.yaml"
    target.write_text("unchanged")
    if kind == "file":
        output.write_text("unchanged")
    elif kind == "directory":
        output.mkdir()
    else:
        try:
            output.symlink_to(target)
        except OSError:
            pytest.skip("Symlinks require privileges on this host")
    _, stderr, code = _run_cli_raw(*args)
    assert code == 2 and "Traceback" not in stderr
    assert target.read_text() == "unchanged"
    if kind != "directory":
        assert output.read_text() == "unchanged"


def test_failed_write_removes_only_its_incomplete_file(monkeypatch, tmp_path):
    args = flags(tmp_path)
    monkeypatch.setattr(
        "medre.cli.bridge_commands.os.fsync",
        lambda fd: (_ for _ in ()).throw(OSError("disk full")),
    )
    _, stderr, code = _run_cli_raw(*args)
    assert code == 2 and "disk full" in stderr
    assert not (tmp_path / "bridge.yaml").exists()


def test_failed_write_preserves_a_concurrent_replacement(monkeypatch, tmp_path):
    args = flags(tmp_path)
    output = tmp_path / "bridge.yaml"
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("concurrent replacement")
    original_stat = Path.stat
    replaced = False

    def replace_before_cleanup(path, *args, **kwargs):
        nonlocal replaced
        if path == output and not replaced:
            # Windows requires closing the descriptor before replacing a file.
            # Inject precisely between write failure/close and cleanup's stat.
            replacement.replace(output)
            replaced = True
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", replace_before_cleanup)
    monkeypatch.setattr(
        "medre.cli.bridge_commands.os.fsync",
        lambda fd: (_ for _ in ()).throw(OSError("write interrupted")),
    )
    _, stderr, code = _run_cli_raw(*args)
    assert code == 2 and "write interrupted" in stderr
    assert output.read_text() == "concurrent replacement"


def test_provider_samples_work_without_a_transport_allowlist(tmp_path):
    args = [
        "config",
        "bridge",
        "--output",
        str(tmp_path / "bridge.yaml"),
        "--source",
        "sample_1",
        "--dest",
        "sample_2",
        "--direction",
        "source_to_dest",
        "--map",
        "opaque-room",
        "another-room",
    ]
    _, stderr, code = _run_cli_raw(*args)
    assert code == 0, stderr
    document = parse_yaml_config((tmp_path / "bridge.yaml").read_text())
    assert set(document["adapters"]) == {"independent_chat"}
    assert set(document["routes"]) == {"bridge"}


def test_no_installed_provider_gives_actionable_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(()))
    _, stderr, code = _run_cli_raw(
        "config", "bridge", "--output", str(tmp_path / "bridge.yaml")
    )
    assert code == 2 and "Install a provider" in stderr
    assert not (tmp_path / "bridge.yaml").exists()


def test_missing_provider_is_diagnosed_before_native_config_parsing(tmp_path):
    path = tmp_path / "input.yaml"
    path.write_text("adapters:\n  not_installed:\n    bot: {}\n")
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
    )
    assert code == 2 and "Unavailable providers" in stderr
    assert "medre adapters check" in stderr


def test_missing_noninteractive_answers_fail_instead_of_reading_stdin(tmp_path):
    path = tmp_path / "input.yaml"
    path.write_text(CONFIG)
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
    )
    assert code == 2 and "--source is required" in stderr
    assert not (tmp_path / "bridge.yaml").exists()


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize(
    ("answer", "saved", "exit_code"),
    [("yes\n", True, 0), ("no\n", False, 0), ("", False, 1)],
)
def test_guided_session_previews_and_confirms_before_writing(
    answer, saved, exit_code, monkeypatch, tmp_path
):
    path = tmp_path / "input.yaml"
    path.write_text(CONFIG)
    # End context collection with a blank line; keep both adapter origin labels.
    monkeypatch.setattr(
        "sys.stdin",
        Terminal("left\nright\nbidirectional\nroom-A\nroom-B\n\n\n\n" + answer),
    )
    stdout, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
        "--json",
    )
    assert code == exit_code, stderr
    assert json.loads(stdout)["total_legs"] == 2
    assert "Direction" in stderr and "Save new configuration" in stderr
    assert (tmp_path / "bridge.yaml").exists() is saved


def test_native_adapter_implementations_are_not_loaded(monkeypatch, tmp_path):
    from medre.adapter_spec import SymbolRef

    original = SymbolRef.load

    def guarded(ref):
        assert not ref.module.startswith("medre.adapters."), ref
        return original(ref)

    monkeypatch.setattr(SymbolRef, "load", guarded)
    _, stderr, code = _run_cli_raw(*flags(tmp_path), "--preview")
    assert code == 0, stderr


@pytest.mark.parametrize("field", ["credential", "unlock_phrase"])
def test_provider_error_cannot_echo_a_known_config_secret(field, monkeypatch, tmp_path):
    def reject(config):
        raise ValueError(f"Could not use {getattr(config, field)}")

    monkeypatch.setattr(ChatConfig, "validate", reject)
    _, stderr, code = _run_cli_raw(*flags(tmp_path))
    assert code == 2
    assert "keep-this-private" not in stderr
    assert "provider-declared-sensitive-value" not in stderr
    assert "Could not use" in stderr
    assert not (tmp_path / "bridge.yaml").exists()


def test_provider_error_cannot_echo_environment_only_secrets(monkeypatch, tmp_path):
    secret = "env-only-phrase"
    monkeypatch.setenv("MEDRE_ADAPTER__LEFT__UNLOCK_PHRASE", secret)

    def reject(config):
        if config.unlock_phrase == secret:
            raise ValueError(f"Could not use {config.unlock_phrase}")
        return config

    monkeypatch.setattr(ChatConfig, "validate", reject)
    stdout, stderr, code = _run_cli_raw(*flags(tmp_path))
    assert code == 2
    assert secret not in stdout + stderr
    assert "Could not use" in stderr
    assert not (tmp_path / "bridge.yaml").exists()


@pytest.mark.parametrize(
    "encode", [repr, json.dumps, lambda value: json.dumps(value, ensure_ascii=False)]
)
def test_escaped_multiline_secrets_are_also_redacted(encode, monkeypatch, tmp_path):
    args = flags(tmp_path)
    secret = "sensitive first line\nsecond line — 'private'"
    document = parse_yaml_config(CONFIG)
    document["adapters"]["independent_chat"]["left"]["unlock_phrase"] = secret
    (tmp_path / "input.yaml").write_text(dump_yaml_config(document))

    def reject(config):
        raise ValueError(f"Could not use {encode(config.unlock_phrase)}")

    monkeypatch.setattr(ChatConfig, "validate", reject)
    stdout, stderr, code = _run_cli_raw(*args)
    assert code == 2
    assert "sensitive first line" not in stdout + stderr
    assert "Could not use" in stderr


def test_native_config_reports_missing_sdk_once_without_loading_implementations(
    monkeypatch, tmp_path
):
    from medre.adapter_spec import SymbolRef

    spec = replace(install_provider(monkeypatch), distribution="unavailable-sdk")
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    args = flags(tmp_path)
    document = parse_yaml_config(CONFIG)
    for item in document["adapters"]["independent_chat"].values():
        item["adapter_kind"] = "real"
    (tmp_path / "input.yaml").write_text(dump_yaml_config(document))
    original = SymbolRef.load

    def guarded(ref):
        assert not ref.module.startswith("medre.adapters."), ref
        return original(ref)

    calls = []

    def missing(distribution):
        calls.append(distribution)
        raise PackageNotFoundError(distribution)

    monkeypatch.setattr(SymbolRef, "load", guarded)
    monkeypatch.setattr("medre.cli.bridge_commands.version", missing)
    stdout, stderr, code = _run_cli_raw(*args, "--preview")
    assert code == 0 and json.loads(stdout)["total_legs"] == 2, stderr
    assert calls == ["unavailable-sdk"]
    assert stderr.count("Prerequisite missing") == 1
    assert not (tmp_path / "bridge.yaml").exists()
    assert not (tmp_path / "home").exists()
