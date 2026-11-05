"""Synthetic previews use production route expansion and pure rendering only."""

from __future__ import annotations

import json
import sys

import pytest

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from medre.runtime.message_preview import SAMPLE_BODY, build_message_preview
from tests.helpers.bridge_setup import CONFIG, install_provider
from tests.helpers.cli import _run_cli_raw


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
    sentinel = tmp_path / "missing-storage.sqlite3"
    report = await build_message_preview(config, route_id="bridge")
    assert report["status"] == "rendered"
    assert not sentinel.exists()


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
    before = sorted(x.name for x in tmp_path.iterdir())
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
    assert sorted(x.name for x in tmp_path.iterdir()) == before


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


async def test_installed_matrix_and_xmpp_use_real_pure_renderers(tmp_path):
    pytest.importorskip("medre.adapters.matrix.renderer")
    pytest.importorskip("medre.adapters.xmpp.renderer")
    # A small SDK-free authored config, not a real login or native session.
    config_path = tmp_path / "matrix-xmpp.yaml"
    config_path.write_text("""adapters:
  matrix:
    matrix_bot:
      adapter_kind: fake
      homeserver: https://matrix.example.org
      user_id: '@bot:example.org'
      access_token: synthetic-secret-never-display
      room_allowlist: ['!room:example.org']
      encryption_mode: plaintext
  xmpp:
    xmpp_bot:
      adapter_kind: fake
      jid: bot@example.org/bridge
      rooms: ['room@conference.example.org']
routes:
  bridge:
    source_adapters: [matrix_bot]
    dest_adapters: [xmpp_bot]
    directionality: bidirectional
    context_map:
      '!room:example.org': {dest_context: 'room@conference.example.org'}
    policy:
      allowed_event_types: ['message.created', 'message.text']
""")
    config, _, paths = load_config(config_path)
    before = set(sys.modules)
    report = await build_message_preview(
        apply_env_overrides(config, paths), route_id="bridge", relation="reply"
    )
    assert report["status"] == "rendered", report
    assert {item["renderer"] for item in report["legs"]} == {"xmpp", "matrix"}
    assert all(
        item["fallback_applied"] == "strategy_fallback_text" for item in report["legs"]
    )
    assert "synthetic-secret-never-display" not in json.dumps(report)
    sdk_roots = {"nio", "slixmpp"}
    assert not (set(sys.modules) - before) & sdk_roots


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
