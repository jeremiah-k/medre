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
        "independent_chat_1",
        "--dest",
        "independent_chat_2",
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


def test_context_prompts_surface_provider_sample_channels(monkeypatch, tmp_path):
    install_provider(monkeypatch)
    path = tmp_path / "input.yaml"
    path.write_text(CONFIG)
    monkeypatch.setattr(
        "sys.stdin",
        Terminal("left\nright\nbidirectional\nroom-A\nroom-B\n\n\n\nyes\n"),
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
    assert code == 0, stderr
    assert "left context (e.g. 'opaque-room')" in stderr
    assert "right context (e.g. 'opaque-room')" in stderr


def test_sample_instances_are_labeled_offline_samples_and_yaml_stays_fake(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "sys.stdin",
        Terminal(
            "independent_chat_1\nindependent_chat_2\nbidirectional\n"
            "room-A\nroom-B\n\n\n\nyes\n"
        ),
    )
    _, stderr, code = _run_cli_raw(
        "config", "bridge", "--output", str(tmp_path / "bridge.yaml")
    )
    assert code == 0, stderr
    assert (
        "No input configuration given: starting from installed providers' "
        "offline samples." in stderr
    )
    assert "independent_chat_1: independent_chat (offline sample)" in stderr
    assert "(fake)" not in stderr
    document = parse_yaml_config((tmp_path / "bridge.yaml").read_text())
    assert (
        document["adapters"]["independent_chat"]["independent_chat_1"]["adapter_kind"]
        == "fake"
    )


def test_real_adapter_kind_label_is_shown_as_is(tmp_path):
    args = flags(tmp_path)
    document = parse_yaml_config(CONFIG)
    for values in document["adapters"]["independent_chat"].values():
        values["adapter_kind"] = "real"
    (tmp_path / "input.yaml").write_text(dump_yaml_config(document))
    _, stderr, code = _run_cli_raw(*args, "--preview")
    assert code == 0, stderr
    assert "left: independent_chat (real)" in stderr


def test_duplicate_source_context_is_rejected_and_authoring_continues(
    monkeypatch, tmp_path
):
    path = tmp_path / "input.yaml"
    path.write_text(CONFIG)
    monkeypatch.setattr(
        "sys.stdin",
        Terminal(
            "left\nright\nbidirectional\n"
            "room-A\nroom-B\n"
            "room-A\nroom-C\n"
            "room-D\nroom-E\n"
            "\n\n\nyes\n"
        ),
    )
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
    )
    assert code == 0, stderr
    assert "Context rejected: Duplicate source context 'room-A'" in stderr
    document = parse_yaml_config((tmp_path / "bridge.yaml").read_text())
    assert set(document["routes"]["bridge"]["context_map"]) == {"room-A", "room-D"}


def test_unstripped_context_entry_gets_an_actionable_message(monkeypatch, tmp_path):
    path = tmp_path / "input.yaml"
    path.write_text(CONFIG)
    monkeypatch.setattr(
        "sys.stdin",
        Terminal(
            "left\nright\nbidirectional\n"
            " room\nroom-B\n"
            "room-A\nroom-B\n"
            "\n\n\nyes\n"
        ),
    )
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
    )
    assert code == 0, stderr
    assert "Contexts cannot be empty or begin or end with whitespace." in stderr
    assert "must already be stripped" not in stderr
    document = parse_yaml_config((tmp_path / "bridge.yaml").read_text())
    assert document["routes"]["bridge"]["context_map"] == {
        "room-A": {"dest_context": "room-B"}
    }


def test_existing_route_id_fails_before_context_prompts(monkeypatch, tmp_path):
    document = parse_yaml_config(CONFIG)
    document["routes"] = {
        "bridge": {
            "source_adapters": ["left"],
            "dest_adapters": ["right"],
            "directionality": "bidirectional",
            "context_map": {"kept": {"dest_context": "other"}},
            "policy": {"allowed_event_types": ["message.created", "message.text"]},
        }
    }
    path = tmp_path / "input.yaml"
    path.write_text(dump_yaml_config(document))
    monkeypatch.setattr("sys.stdin", Terminal("left\nright\nbidirectional\n"))
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(path),
        "--output",
        str(tmp_path / "bridge.yaml"),
    )
    assert code == 2, stderr
    assert "Route 'bridge' already exists" in stderr
    assert "A context is a platform's conversation address" not in stderr
    assert not (tmp_path / "bridge.yaml").exists()


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


def test_cli_preserves_direction_contexts_and_explicit_labels(tmp_path):
    stdout, stderr, code = _run_cli_raw(
        *flags(tmp_path), "--source-label", "Source label", "--dest-label", "Dest label"
    )
    assert code == 0, stderr
    legs = json.loads(stdout)["routes"][0]["legs"]
    assert {
        (
            leg["source_adapter_id"],
            leg["source_channel"],
            leg["dest_adapter_id"],
            leg["dest_channel"],
            leg["source_origin_label"],
        )
        for leg in legs
    } == {
        ("left", "room=α", "right", "0", "Source label"),
        ("right", "0", "left", "room=α", "Dest label"),
    }
    raw = parse_yaml_config((tmp_path / "bridge.yaml").read_text())["routes"]["bridge"]
    assert raw["source_adapters"] == ["left"]
    assert raw["dest_adapters"] == ["right"]
    assert raw["source_origin_label"] == "Source label"
    assert raw["dest_origin_label"] == "Dest label"


def test_output_suffix_is_validated_before_writing(tmp_path):
    args = flags(tmp_path)
    output = tmp_path / "notes.txt"
    args[args.index("--output") + 1] = str(output)
    _, stderr, code = _run_cli_raw(*args)
    assert code == 2 and "Output must use .yaml or .yml" in stderr
    assert not output.exists()


def test_output_created_after_preview_is_never_overwritten(monkeypatch, tmp_path):
    args = flags(tmp_path)
    output = tmp_path / "bridge.yaml"
    original_open = os.open

    def race_open(path, *args, **kwargs):
        output.write_text("concurrent writer")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("medre.cli.bridge_commands.os.open", race_open)
    _, _, code = _run_cli_raw(*args)
    assert code == 2
    assert output.read_text() == "concurrent writer"


@pytest.mark.parametrize("operation", ["fstat", "fdopen"])
def test_descriptor_setup_failure_closes_and_removes_partial_output(
    operation, monkeypatch, tmp_path
):
    from medre.cli.bridge_commands import _write_new_config

    output = tmp_path / "bridge.yaml"
    opened = []
    original_open = os.open
    original_fstat = os.fstat

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail(*args, **kwargs):
        raise OSError("descriptor setup failed")

    monkeypatch.setattr("medre.cli.bridge_commands.os.open", track_open)
    monkeypatch.setattr(f"medre.cli.bridge_commands.os.{operation}", fail)
    with pytest.raises(OSError, match="descriptor setup failed"):
        _write_new_config(output, "private config")
    try:
        with pytest.raises(OSError):
            original_fstat(opened[0])
    finally:
        try:
            os.close(opened[0])
        except OSError:
            pass
    assert not output.exists()


def test_interrupt_after_write_does_not_claim_no_configuration(monkeypatch, tmp_path):
    from medre.cli.bridge_commands import _write_new_config

    def interrupt_after_save(*args):
        _write_new_config(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "medre.cli.bridge_commands._write_new_config", interrupt_after_save
    )
    _, stderr, code = _run_cli_raw(*flags(tmp_path))
    assert code == 1
    assert (tmp_path / "bridge.yaml").exists()
    assert "no configuration written" not in stderr


def test_nonsecret_environment_values_do_not_shred_diagnostics(monkeypatch):
    from medre.cli.bridge_commands import _error_detail

    monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "1")
    monkeypatch.setenv("MEDRE_ADAPTER__LEFT__MAPPING", "a")
    detail = "Route 'bridge' has 1 leg and 1 conflict at line 1"
    assert _error_detail(ValueError(detail), {}) == detail


@pytest.mark.parametrize("secret", ["a", "1234"])
def test_short_secrets_are_redacted_without_replacing_parts_of_words(secret):
    from medre.cli.bridge_commands import _error_detail

    detail = f"Failed for adapter 'a1234b': invalid credential {secret!r}"
    actual = _error_detail(ValueError(detail), {"credential": secret})
    assert actual == "Failed for adapter 'a1234b': invalid credential [REDACTED]"


def test_sdk_free_cli_bridge_can_smoke_each_selected_context(tmp_path):
    _, stderr, code = _run_cli_raw(*flags(tmp_path))
    assert code == 0, stderr
    for adapter, context, target in (
        ("left", "room=α", "right"),
        ("right", "0", "left"),
    ):
        stdout, stderr, code = _run_cli_raw(
            "smoke",
            "--config",
            str(tmp_path / "bridge.yaml"),
            "--source-adapter",
            adapter,
            "--source-context",
            context,
            "--json",
        )
        report = json.loads(stdout)
        assert code == 0 and report["status"] == "passed", report
        assert report["source_adapter"] == adapter
        assert report["source_context"] == context
        assert report["target_adapters"] == [target]
