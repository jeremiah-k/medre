"""Synthetic previews use production route expansion and pure rendering only."""

from __future__ import annotations

import json

import pytest

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from medre.runtime.message_preview import SAMPLE_BODY, build_message_preview
from tests.helpers.bridge_setup import CONFIG, install_provider
from tests.helpers.cli import _run_cli_raw


def _snapshot_test_files(root):
    """Include nested paths and existing file contents, not just new siblings."""
    from hashlib import sha256

    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
    }


@pytest.fixture
def configured(monkeypatch, tmp_path):
    install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    draft = draft_bridge(
        CONFIG,
        BridgeRequest("left", "right", (("room α", "topic=0"),)),
        paths=resolve(),
    )
    path = tmp_path / "bridge.yaml"
    path.write_text(draft.yaml)
    config, _, paths = load_config(path)
    return path, apply_env_overrides(config, paths)


async def test_preview_expands_both_legs_using_registered_renderers(configured):
    _, config = configured
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "rendered"
    assert len(report["legs"]) == 2
    assert {item["renderer"] for item in report["legs"]} == {"text"}
    assert all(item["text"] == SAMPLE_BODY for item in report["legs"])
    assert all(item["fallback_applied"] is None for item in report["legs"])
    assert report["evidence_level"] == "offline_renderer_only"


async def test_relation_preview_is_explicit_fallback_not_capability_authority(
    configured,
):
    _, config = configured
    report = await build_message_preview(config, route_id="bridge", relation="reply")
    assert all(item["strategy"] == "fallback_text" for item in report["legs"])
    assert all(
        item["fallback_applied"] == "strategy_fallback_text" for item in report["legs"]
    )
    assert all("synthetic" in item["text"] for item in report["legs"])
    assert any(
        "not delivery planning" in item.lower() for item in report["limitations"]
    )


async def test_long_preview_uses_real_truncation_and_no_private_fields(configured):
    _, config = configured
    report = await build_message_preview(config, route_id="bridge", repeat=32)
    assert all(item["truncated"] for item in report["legs"])
    assert all(len(item["text"]) == 500 for item in report["legs"])
    assert "keep-this-private" not in json.dumps(report)
    assert "provider-declared-sensitive-value" not in json.dumps(report)
    assert "_matrix_operation" not in json.dumps(report)


@pytest.mark.parametrize("repeat", [-1, 0, 33, True, "10"])
async def test_preview_refuses_invalid_repetitions(configured, repeat):
    _, config = configured
    with pytest.raises(ValueError, match="repeat"):
        await build_message_preview(config, route_id="bridge", repeat=repeat)


async def test_preview_refuses_missing_route_without_guessing(configured):
    _, config = configured
    with pytest.raises(ValueError, match="Route is absent"):
        await build_message_preview(config, route_id="not-there")


async def test_preview_does_not_write_or_start_runtime(
    configured, monkeypatch, tmp_path
):
    _, config = configured
    monkeypatch.setattr(
        "medre.runtime.builder._build_real_adapter",
        lambda *a, **kw: pytest.fail("must not instantiate an adapter"),
    )
    before = _snapshot_test_files(tmp_path)
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "rendered"
    assert _snapshot_test_files(tmp_path) == before


async def test_renderer_exception_message_never_leaks(configured, monkeypatch):
    _, config = configured

    async def refuse(self, event, ctx):
        raise ValueError("keep-this-private /private/keys/identity")

    monkeypatch.setattr("medre.core.rendering.text.TextRenderer.render", refuse)
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "incomplete"
    assert all(row["reason"] == "renderer_refusal" for row in report["legs"])
    assert "keep-this-private" not in json.dumps(report)
    assert "/private/keys/identity" not in json.dumps(report)


def test_cli_renders_json_and_has_no_disk_side_effect(configured, tmp_path):
    path, _ = configured
    before = _snapshot_test_files(tmp_path)
    stdout, stderr, code = _run_cli_raw(
        "config",
        "preview-message",
        "--config",
        str(path),
        "--route",
        "bridge",
        "--json",
    )
    assert code == 0, stderr
    assert json.loads(stdout)["status"] == "rendered"
    assert "keep-this-private" not in stdout + stderr
    assert _snapshot_test_files(tmp_path) == before


def test_cli_refuses_invalid_route_without_leaking(configured):
    path, _ = configured
    stdout, stderr, code = _run_cli_raw(
        "config", "preview-message", "--config", str(path), "--route", "missing"
    )
    assert code == 2 and not stdout
    assert "configuration or pure renderer check failed" in stderr
    assert "keep-this-private" not in stderr


def test_cli_skips_provider_cli_contribution_loading(configured, monkeypatch):
    path, _ = configured
    import importlib

    cli_main = importlib.import_module("medre.cli.main")

    original = cli_main._build_parser
    options = []

    def capture(*, include_adapter_cli=True):
        options.append(include_adapter_cli)
        return original(include_adapter_cli=include_adapter_cli)

    monkeypatch.setattr(cli_main, "_build_parser", capture)
    _run_cli_raw(
        "config", "preview-message", "--config", str(path), "--route", "bridge"
    )
    assert options == [False]


async def test_sample_event_kind_respects_only_message_text_route(configured):
    _, config = configured
    from dataclasses import replace

    route = replace(
        config.routes.routes[0],
        policy=replace(
            config.routes.routes[0].policy, allowed_event_types=("message.text",)
        ),
    )
    amended = replace(config, routes=replace(config.routes, routes=(route,)))
    report = await build_message_preview(amended, route_id="bridge")
    assert report["sample_event_kind"] == "message.text"
    assert report["status"] == "rendered"


async def test_sample_event_kind_refuses_route_that_filters_all_text(configured):
    _, config = configured
    from dataclasses import replace

    route = replace(
        config.routes.routes[0],
        policy=replace(
            config.routes.routes[0].policy, allowed_event_types=("message.deleted",)
        ),
    )
    amended = replace(config, routes=replace(config.routes, routes=(route,)))
    with pytest.raises(ValueError, match="does not permit"):
        await build_message_preview(amended, route_id="bridge")


async def test_misaddressed_renderer_result_is_refused(configured, monkeypatch):
    from dataclasses import replace
    from medre.core.rendering.renderer import RenderingPipeline

    _, config = configured
    original = RenderingPipeline.render

    async def misaddress(self, *args, **kwargs):
        actual = await original(self, *args, **kwargs)
        return replace(actual, target_channel="other-unrelated-channel")

    monkeypatch.setattr(RenderingPipeline, "render", misaddress)
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "incomplete"
    assert all(item["reason"] == "renderer_refusal" for item in report["legs"])


async def test_default_policy_does_not_require_explicit_event_filters(configured):
    from dataclasses import replace

    _, config = configured
    route = replace(config.routes.routes[0], policy=None)
    amended = replace(config, routes=replace(config.routes, routes=(route,)))
    report = await build_message_preview(amended, route_id="bridge")
    assert report["sample_event_kind"] == "message.created"
    assert report["status"] == "rendered"


@pytest.mark.parametrize("repeat", ["0", "33", "not-a-number"])
def test_cli_invalid_repeat_is_an_argument_error(configured, repeat):
    path, _ = configured
    stdout, stderr, code = _run_cli_raw(
        "config",
        "preview-message",
        "--config",
        str(path),
        "--route",
        "bridge",
        "--repeat",
        repeat,
    )
    assert code == 2 and not stdout
    assert "argument --repeat" in stderr
    assert "from 1 to 32" in stderr or "between 1 and 32" in stderr
    assert "configuration or pure renderer check failed" not in stderr


def test_file_snapshot_detects_nested_writes_and_rewrites(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    existing = home / "selection.json"
    existing.write_text("before")
    baseline = _snapshot_test_files(tmp_path)
    existing.write_text("after")
    assert _snapshot_test_files(tmp_path) != baseline
    existing.write_text("before")
    assert _snapshot_test_files(tmp_path) == baseline
    (home / "new.db").write_bytes(b"sqlite")
    assert _snapshot_test_files(tmp_path) != baseline


async def test_preview_ignores_unrelated_broken_renderer_factories(
    configured, monkeypatch
):
    from types import SimpleNamespace
    from medre.runtime import builder

    _, config = configured

    class BrokenFactory:
        def load(self):
            pytest.fail("A renderer for an unrelated transport was loaded")

    monkeypatch.setattr(
        builder,
        "iter_adapter_specs",
        lambda: (
            SimpleNamespace(transport="unrelated", renderer_factory=BrokenFactory()),
        ),
    )
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "rendered"


async def test_preview_refuses_non_send_matrix_operation_even_with_text(
    configured, monkeypatch
):
    from dataclasses import replace
    from medre.core.rendering.text import TextRenderer

    _, config = configured
    original = TextRenderer.render

    async def non_send(self, event, ctx):
        rendered = await original(self, event, ctx)
        return replace(
            rendered,
            payload={
                "body": "this must not be shown",
                "_matrix_operation": {
                    "kind": "redact_event",
                    "content": {"body": "no"},
                },
            },
        )

    monkeypatch.setattr(TextRenderer, "render", non_send)
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "incomplete"
    assert all(leg["reason"] == "non_text_payload" for leg in report["legs"])
    assert "this must not be shown" not in json.dumps(report)
