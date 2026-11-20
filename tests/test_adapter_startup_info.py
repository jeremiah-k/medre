# medre-test-tier: compatibility
"""Adapter startup-info contract: duck-typed discovery, bounded emission,
display-only cache, core-boundary redaction, and CLI rendering.

Runtime tests use fake providers. CLI tests load a fake Matrix instance
through the installed provider's configuration validator. See
``docs/spec/adapter-startup-info.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import UserList
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.lifecycle.states import AdapterState
from medre.core.startup_info import (
    CACHE_FILENAME,
    AdapterStartupInfo,
    StartupInfoField,
    collect_startup_info,
    read_cache_file,
    render_startup_info_block,
    run_cache_io,
    startup_info_callable,
    write_cache_file,
)
from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
)

FAKE_ADAPTER_ID = "fake_matrix"
_TIMESTAMP = "2026-11-19T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve(environ={"MEDRE_HOME": str(tmp_path)})


def _cache_path(paths: MedrePaths, adapter_id: str = FAKE_ADAPTER_ID) -> Path:
    return paths.adapter_state_dir(adapter_id) / CACHE_FILENAME


def _install_startup_info(app: Any, values: dict[str, Any] | Callable[[], Any]) -> None:
    """Attach an async ``startup_info`` to the built fake adapter."""

    async def startup_info() -> Any:
        if callable(values):
            return values()
        return values

    app.adapters[FAKE_ADAPTER_ID].startup_info = startup_info


# ---------------------------------------------------------------------------
# Core: duck-typed discovery
# ---------------------------------------------------------------------------


def test_absent_symbol_discovers_nothing() -> None:
    class Plain:
        pass

    assert startup_info_callable(Plain()) is None


def test_non_callable_symbol_discovers_nothing() -> None:
    class Broken:
        startup_info = "not-a-callable"

    assert startup_info_callable(Broken()) is None


def test_callable_symbol_is_discovered() -> None:
    async def startup_info() -> dict[str, str]:
        return {}

    class Provider:
        pass

    provider = Provider()
    provider.startup_info = startup_info  # type: ignore[attr-defined]
    assert startup_info_callable(provider) is startup_info


# ---------------------------------------------------------------------------
# Core: bounded, non-blocking emission with redaction
# ---------------------------------------------------------------------------


async def test_emits_redacted_fields() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {
                "board": "HELTEC_V3",
                "firmware": "2.5.4",
                "identity_key": "b" * 64,
            }

    info = await collect_startup_info(
        "node",
        "meshtastic",
        Provider(),
        emitted_at=_TIMESTAMP,
    )
    assert info is not None
    values = {field.name: field.value for field in info.fields}
    assert values == {"board": "HELTEC_V3", "firmware": "2.5.4"}
    assert all(not field.cached for field in info.fields)
    assert info.adapter_id == "node"
    assert info.transport == "meshtastic"


async def test_provider_rows_keep_order_and_skip_invalid_facts(tmp_path: Path) -> None:
    class Provider:
        def startup_info(self) -> dict[str, object]:
            return {
                "rows": [
                    [
                        {"name": "node_name", "value": "Ridge Relay (RIDG)"},
                        {"name": "node_id", "value": "!a4b3c2d1"},
                        {"name": "public_key_token", "value": "not-a-public-key"},
                        {"name": "unsafe\nname", "value": "bad"},
                    ],
                    [
                        {"name": "board", "value": "RAK4631", "label": "\x1b[31m"},
                        {
                            "name": "firmware",
                            "value": "2.7.15",
                            "label": "firmware",
                        },
                        {"name": "radio_channel", "value": None},
                    ],
                    [{"name": "extra", "value": "outside the startup block"}],
                ]
            }

    info = await collect_startup_info(
        "main", "meshtastic", Provider(), emitted_at=_TIMESTAMP
    )
    assert info is not None
    assert render_startup_info_block("meshtastic.main  ready", info) == (
        "meshtastic.main  ready\n"
        "  node name Ridge Relay (RIDG) · node id !a4b3c2d1\n"
        "  board RAK4631 · firmware 2.7.15\n"
        "  extra outside the startup block"
    )
    assert [(field.name, field.row) for field in info.fields] == [
        ("node_name", 0),
        ("node_id", 0),
        ("board", 1),
        ("firmware", 1),
        ("extra", 2),
    ]

    cache = tmp_path / CACHE_FILENAME
    write_cache_file(cache, info)
    saved = read_cache_file(cache, adapter_id="main", transport="meshtastic")
    assert saved == info.with_cached_flags(True)
    assert read_cache_file(cache, adapter_id="main", transport="meshcore") is None


async def test_absent_provider_yields_none() -> None:
    assert await collect_startup_info("a", "t", object(), emitted_at=_TIMESTAMP) is None


async def test_non_mapping_result_yields_none() -> None:
    class Provider:
        async def startup_info(self) -> str:
            return "nope"

    assert (
        await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP) is None
    )


async def test_empty_fields_yield_none() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"identity_key": "k" * 64}

    assert (
        await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP) is None
    )


async def test_timeout_yields_none() -> None:
    release = asyncio.Event()

    class Provider:
        async def startup_info(self) -> dict[str, str]:
            await release.wait()
            return {"board": "never"}

    info = await collect_startup_info(
        "a",
        "t",
        Provider(),
        timeout_seconds=0.05,
        emitted_at=_TIMESTAMP,
    )
    release.set()
    assert info is None


async def test_failure_yields_none() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            raise RuntimeError("provider bug")

    assert (
        await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP) is None
    )


async def test_provider_discovery_failure_yields_none() -> None:
    class Provider:
        @property
        def startup_info(self) -> object:
            raise RuntimeError("provider cannot expose startup facts")

    assert (
        await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP) is None
    )


async def test_caller_cancellation_propagates() -> None:
    entered, retired = asyncio.Event(), asyncio.Event()

    class Provider:
        async def startup_info(self) -> dict[str, str]:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                retired.set()

    task = asyncio.ensure_future(
        collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.wait_for(retired.wait(), timeout=5)


# ---------------------------------------------------------------------------
# Core: render-boundary redaction
# ---------------------------------------------------------------------------


async def test_secret_and_pairing_names_never_render() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, object]:
            return {
                "access_token": "syt_secret",
                "private_key": "k" * 64,
                "identity_key": "k" * 64,
                "pairing_token": "pair-material",
                "psk": "pre-shared",
                "credentials": "leak",
            }

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is None, "every field was secret-like; nothing may render"


async def test_public_keys_render_in_full() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"public_key": "K" * 43}

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is not None
    assert info.fields[0].value == "K" * 43


async def test_addresses_render_in_full() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {
                "node_address": "!a4cf1214e5f6a7b8",
                "user_id": "@bridge:example.org",
                "mac": "aa:bb:cc:dd:ee:ff",
            }

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is not None
    values = {field.name: field.value for field in info.fields}
    assert values["node_address"] == "!a4cf1214e5f6a7b8"
    assert values["user_id"] == "@bridge:example.org"
    assert values["mac"] == "aa:bb:cc:dd:ee:ff"


async def test_shared_secrets_drop_even_under_unlisted_names() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"channel_psk": "a" * 64}

    # Every field dropping yields no record at all rather than an
    # empty one: a provider whose only startup fact is a secret
    # reports nothing.
    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is None


async def test_device_identifiers_render_in_full() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, object]:
            return {
                "device_id": "!a4b3c2d1e5f6a7b8",
                "deviceId": 2460232048,
            }

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is not None
    values = {field.name: field.value for field in info.fields}
    assert values["device_id"] == "!a4b3c2d1e5f6a7b8"
    assert values["deviceId"] == "2460232048"


async def test_credential_bearing_url_is_dropped() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"endpoint": "https://user:pass@matrix.example.org"}

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is None


async def test_control_characters_drop_the_field() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"board": "HELTEC\x1b[31m"}

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is None


async def test_value_token_scrub_applies() -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {"banner": "logged in with syt_abc123"}

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is not None
    assert "[REDACTED]" in info.fields[0].value
    assert "syt_abc123" not in info.fields[0].value


async def test_render_line_is_idempotent(tmp_path: Path) -> None:
    class Provider:
        async def startup_info(self) -> dict[str, str]:
            return {
                "board": "HELTEC_V3",
                "public_key": "a" * 64,
                "user_id": "@bridge:example.org",
                "description": "Radio firmware version string " * 4,
            }

    info = await collect_startup_info("a", "t", Provider(), emitted_at=_TIMESTAMP)
    assert info is not None
    first = render_startup_info_block("t.a", info)
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, info)
    cached = read_cache_file(path)
    assert cached == info.with_cached_flags(True)
    assert cached is not None
    second = render_startup_info_block("t.a", cached.with_cached_flags(False))
    assert first == second
    assert len(info.fields[-1].value) == 64
    assert info.fields[-1].value.endswith("…")
    assert render_startup_info_block("t.a", cached) == first.replace(
        "t.a\n", "t.a (cached)\n", 1
    )


# ---------------------------------------------------------------------------
# Core: durable display-only cache
# ---------------------------------------------------------------------------


def _sample() -> AdapterStartupInfo:
    return AdapterStartupInfo(
        adapter_id="main",
        transport="matrix",
        fields=(StartupInfoField(name="board", value="HELTEC_V3"),),
        emitted_at=_TIMESTAMP,
    )


def test_round_trip_marks_cached(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, _sample())
    cached = read_cache_file(path)
    assert cached is not None
    assert all(field.cached for field in cached.fields)
    assert cached.fields[0].value == "HELTEC_V3"
    assert cached.emitted_at == "2026-11-19T00:00:00+00:00"


def test_missing_cache_reads_none(tmp_path: Path) -> None:
    assert read_cache_file(tmp_path / "absent.json") is None


def test_malformed_cache_reads_none(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILENAME
    path.write_text("{not json")
    assert read_cache_file(path) is None
    path.write_text('{"adapter_id": 3}')
    assert read_cache_file(path) is None


def test_tampered_cache_cannot_inject_key_material(tmp_path: Path) -> None:
    path = tmp_path / CACHE_FILENAME
    path.write_text(
        json.dumps(
            {
                "adapter_id": "main",
                "transport": "matrix",
                "emitted_at": "2026-11-19T00:00:00+00:00",
                "fields": [
                    {"name": "access_token", "value": "syt_leak"},
                    {"name": "board", "value": "HELTEC_V3"},
                ],
            }
        )
    )
    cached = read_cache_file(path)
    assert cached is not None
    assert [field.name for field in cached.fields] == ["board"]


# ---------------------------------------------------------------------------
# Runtime: emission at the ready transition
# ---------------------------------------------------------------------------


async def test_emitted_at_ready_and_persisted(
    tmp_paths: MedrePaths, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="medre.runtime.app")
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    _install_startup_info(
        app, {"board": "FakeBoard", "firmware": "1.2.3", "identity_key": "k" * 64}
    )
    await app.start()
    try:
        info = app._startup_info.get(FAKE_ADAPTER_ID)
        assert info is not None
        assert {f.name: f.value for f in info.fields} == {
            "board": "FakeBoard",
            "firmware": "1.2.3",
        }
        assert all(not f.cached for f in info.fields)
        # One readiness progress line at ready; the facts themselves render
        # in the consolidated banner at settle, not in per-adapter logs.
        assert f"Adapter matrix.{FAKE_ADAPTER_ID} started in" in " ".join(
            str(m) for m in caplog.messages
        )
        assert not any("board FakeBoard" in str(m) for m in caplog.messages)
        cache = _cache_path(tmp_paths)
        stored = read_cache_file(cache)
        assert stored is not None
        assert {f.name: f.value for f in stored.fields} == {
            "board": "FakeBoard",
            "firmware": "1.2.3",
        }
    finally:
        await app.stop()


async def test_adapter_without_symbol_reports_nothing(tmp_paths: MedrePaths) -> None:
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    assert startup_info_callable(app.adapters[FAKE_ADAPTER_ID]) is None
    await app.start()
    try:
        assert FAKE_ADAPTER_ID not in app._startup_info
        assert not _cache_path(tmp_paths).exists()
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
    finally:
        await app.stop()


async def test_slow_startup_info_is_bounded(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.runtime.app as app_module

    monkeypatch.setattr(app_module, "_STARTUP_INFO_TIMEOUT_SECONDS", 0.05)
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    release = asyncio.Event()

    async def startup_info() -> dict[str, str]:
        await release.wait()
        return {"board": "never"}

    app.adapters[FAKE_ADAPTER_ID].startup_info = startup_info
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert FAKE_ADAPTER_ID not in app._startup_info
        assert not _cache_path(tmp_paths).exists()
    finally:
        release.set()
        await app.stop()


async def test_failing_startup_info_does_not_fail_adapter(
    tmp_paths: MedrePaths,
) -> None:
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)

    async def startup_info() -> dict[str, str]:
        raise RuntimeError("provider bug")

    app.adapters[FAKE_ADAPTER_ID].startup_info = startup_info
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert FAKE_ADAPTER_ID not in app._startup_info
    finally:
        await app.stop()


# ---------------------------------------------------------------------------
# Runtime: restart renders cached until fresh emission replaces it
# ---------------------------------------------------------------------------


async def test_cached_until_fresh_replaces(
    tmp_paths: MedrePaths, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="medre.runtime.app")
    first = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    _install_startup_info(first, {"board": "OLD_BOARD"})
    await first.start()
    await first.stop()
    caplog.clear()

    second = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    original_start = second.adapters[FAKE_ADAPTER_ID].start
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_start(ctx: Any) -> None:
        entered.set()
        await release.wait()
        await original_start(ctx)

    second.adapters[FAKE_ADAPTER_ID].start = blocked_start
    _install_startup_info(second, {"board": "NEW_BOARD"})
    starting = asyncio.ensure_future(second.start())
    try:
        assert await wait_until(entered.is_set, timeout=5)
        assert await wait_until(
            lambda: FAKE_ADAPTER_ID in second._startup_info, timeout=5
        )
        cached = second._startup_info[FAKE_ADAPTER_ID]
        assert cached.fields[0].value == "OLD_BOARD"
        assert cached.fields[0].cached is True
        # Cached facts load silently: no preview block before adapters
        # start. They surface in the consolidated banner at settle.
        assert not any("board OLD_BOARD" in str(message) for message in caplog.messages)
        # Releasing the blocked start completes the ready transition;
        # the fresh emission replaces the cached record and the runtime
        # logs one readiness line (facts render in the banner, not here).
        release.set()
        await starting
        fresh = second._startup_info[FAKE_ADAPTER_ID]
        assert fresh.fields[0].value == "NEW_BOARD"
        assert fresh.fields[0].cached is False
        readiness = [
            message
            for message in caplog.messages
            if f"Adapter matrix.{FAKE_ADAPTER_ID} started in" in str(message)
        ]
        assert len(readiness) == 1
        assert not any("board NEW_BOARD" in str(m) for m in caplog.messages)
        stored = read_cache_file(_cache_path(tmp_paths))
        assert stored is not None
        assert stored.fields[0].value == "NEW_BOARD"
        assert stored.fields[0].cached is True
    finally:
        release.set()
        if not starting.done():
            await starting
        if second.state.value not in ("stopped", "failed"):
            await second.stop()


# ---------------------------------------------------------------------------
# CLI: medre adapters rendering
# ---------------------------------------------------------------------------

_CLI_CONFIG = """\
runtime:
  name: cli-startup-info
adapters:
  matrix:
    main:
      adapter_id: main
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.example.org
      user_id: "@bridge:example.org"
      access_token: fake_token_for_test
"""


def _plant_cli_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, with_cache: bool
) -> Path:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(_CLI_CONFIG)
    if with_cache:
        adapter_dir = tmp_path / "state" / "adapters" / "main"
        adapter_dir.mkdir(parents=True)
        (adapter_dir / CACHE_FILENAME).write_text(
            json.dumps(
                {
                    "adapter_id": "main",
                    "transport": "matrix",
                    "emitted_at": "2026-11-19T00:00:00+00:00",
                    "fields": [
                        {"name": "user_id", "value": "@bridge:example.org", "row": 0},
                        {
                            "name": "device_id",
                            "value": "MEDREAB12CD",
                            "row": 0,
                            "label": "device",
                        },
                        {
                            "name": "homeserver",
                            "value": "https://matrix.example.org",
                            "row": 1,
                        },
                    ],
                }
            )
        )
    return tmp_path


@pytest.mark.usefixtures("isolated_config_env")
def test_cached_line_renders_with_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=True)
    output = _run_cli("adapters")
    assert "Configured adapters:" in output
    assert (
        "  matrix.main: enabled (kind=fake) (cached)\n"
        "    user id @bridge:example.org · device MEDREAB12CD\n"
        "    homeserver https://matrix.example.org" in output
    )


@pytest.mark.usefixtures("isolated_config_env")
def test_without_info_no_startup_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=False)
    output = _run_cli("adapters")
    assert "Configured adapters:" in output
    assert "  matrix.main: enabled (kind=fake)" in output
    assert "MEDREAB12CD" not in output
    assert "(cached)" not in output


@pytest.mark.usefixtures("isolated_config_env")
def test_json_carries_structured_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=True)
    output = _run_cli("adapters", "--json")
    document = json.loads(output)
    configured = document["configured_adapters"]
    assert len(configured) == 1
    entry = configured[0]
    assert entry["adapter_id"] == "main"
    record = entry["startup_info"]
    assert record["transport"] == "matrix"
    assert record["emitted_at"] == "2026-11-19T00:00:00+00:00"
    fields = record["fields"]
    assert {f["name"]: f["value"] for f in fields} == {
        "user_id": "@bridge:example.org",
        "device_id": "MEDREAB12CD",
        "homeserver": "https://matrix.example.org",
    }
    assert [field["row"] for field in fields] == [0, 0, 1]
    assert fields[1]["label"] == "device"
    assert all(f["cached"] is True for f in fields)


# Adversarial boundary regressions.


async def _collect_values(values: object) -> AdapterStartupInfo | None:
    return await collect_startup_info(
        "main",
        "matrix",
        SimpleNamespace(startup_info=lambda: values),
        emitted_at=_TIMESTAMP,
    )


@pytest.mark.parametrize("name", ["pubkey", "publicKey", "pub_key", "public_key"])
async def test_public_key_exception_only_exempts_generic_key_shape(name: str) -> None:
    info = await _collect_values({name: "a" * 64})
    assert info is not None
    assert info.fields[0].value == "a" * 64
    for credential in ("syt_abcd1234", "sk-" + "a" * 24, "password=opaque"):
        info = await _collect_values({name: credential})
        assert info is not None
        assert info.fields[0].value == "[REDACTED]"


@pytest.mark.parametrize(
    "name",
    [
        "public_key_secret",
        "pub_key_psk",
        "publicKeySecret",
        "pubKeyPSK",
        "public_key_pin",
        "board\x1b[31m",
        "syt_secret",
    ],
)
async def test_unsafe_field_names_do_not_reach_any_output(name: str) -> None:
    info = await _collect_values({name: "private-material", "board": "RAK4631"})
    assert info is not None
    assert [field.name for field in info.fields] == ["board"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://matrix.example.org/?access_token=opaque-test-secret",
        "ftp://user:opaque-test-secret@example.org/",
        "https://example.org/?p=opaque-test-secret",
        "https://example.org/#opaque-test-secret",
    ],
)
async def test_credential_url_omitted_from_collection_cache_and_cli(
    endpoint, tmp_path, monkeypatch
):
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=False)
    info = await _collect_values(
        {"endpoint": endpoint, "homeserver": "https://matrix.example.org"}
    )
    assert info is not None
    assert [field.name for field in info.fields] == ["homeserver"]
    path = tmp_path / "state" / "adapters" / "main" / CACHE_FILENAME
    write_cache_file(path, info)
    for result in (
        path.read_text(),
        _run_cli("adapters"),
        _run_cli("adapters", "--json"),
    ):
        assert "opaque-test-secret" not in result
        assert "https://matrix.example.org" in result
    # Simulate a tampered cache rather than relying only on the writer.
    data = info.to_dict()
    data["fields"] = [*data["fields"], {"name": "endpoint", "value": endpoint}]
    path.write_text(json.dumps(data))
    assert "opaque-test-secret" not in _run_cli("adapters", "--json")
    assert "opaque-test-secret" not in _run_cli("adapters")


async def test_scalar_rows_and_nonlist_sequence_are_supported():
    flat = await _collect_values(
        {"rows": 4, "board": "RAK4631", "firmware": None, "enabled": True}
    )
    assert flat is not None
    assert {f.name: f.value for f in flat.fields} == {
        "rows": "4",
        "board": "RAK4631",
        "enabled": "True",
    }
    grouped = await _collect_values(
        {"rows": UserList([UserList([{"name": "board", "value": "RAK4631"}])])}
    )
    assert grouped is not None
    assert grouped.fields[0].value == "RAK4631"


async def test_redaction_marker_survives_long_prefix_and_terminal_controls_drop():
    info = await _collect_values(
        {
            "description": "word " * 150 + "syt_hidden",
            "bad": "a\u202eb",
            "surrogate": "\ud800",
            "board": "RAK4631",
        }
    )
    assert info is not None
    assert {f.name: f.value for f in info.fields} == {
        "description": "[REDACTED]",
        "board": "RAK4631",
    }


def test_direct_record_serialization_reapplies_validation(tmp_path):
    info = replace(
        _sample(),
        fields=(
            StartupInfoField("board", "RAK4631"),
            StartupInfoField("public_key", "syt_secret"),
            StartupInfoField("psk", "private-psk"),
        ),
    )
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, info)
    assert "syt_secret" not in path.read_text()
    assert "private-psk" not in path.read_text()
    assert "[REDACTED]" in path.read_text()
    assert "syt_secret" not in render_startup_info_block("matrix.main", info)


@pytest.mark.parametrize(
    "payload",
    [
        b"[" * 2000 + b"]" * 2000,
        b"\xff",
        b" " * (64 * 1024 + 1),
        b'{"adapter_id":"main","transport":"matrix","emitted_at":"syt_secret","fields":[{"name":"board","value":"RAK4631"}]}',
    ],
)
def test_corrupt_or_oversize_cache_is_absent(tmp_path, payload):
    path = tmp_path / CACHE_FILENAME
    path.write_bytes(payload)
    assert read_cache_file(path) is None


def test_nonregular_cache_is_absent(tmp_path):
    path = tmp_path / CACHE_FILENAME
    path.mkdir()
    assert read_cache_file(path) is None
    if hasattr(os, "mkfifo"):
        path.rmdir()
        os.mkfifo(path)
        assert read_cache_file(path) is None


def test_concurrent_cache_writes_publish_complete_records(tmp_path):
    path = tmp_path / CACHE_FILENAME
    start = threading.Barrier(3, timeout=5)
    infos = [
        replace(_sample(), fields=(StartupInfoField("board", board),))
        for board in ("FIRST", "SECOND")
    ]

    def write(info):
        start.wait()
        write_cache_file(path, info)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = [pool.submit(write, info) for info in infos]
        start.wait()
        for future in writes:
            future.result(timeout=5)
    assert read_cache_file(path) in [info.with_cached_flags(True) for info in infos]
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0


async def test_expired_cache_write_cannot_overwrite_later_record(tmp_path, monkeypatch):
    from medre.core import startup_info as module

    path = tmp_path / CACHE_FILENAME
    old = _sample()
    new = replace(old, fields=(StartupInfoField("board", "NEW"),))
    entered, release = threading.Event(), threading.Event()
    original = module.tempfile.NamedTemporaryFile

    def blocked_temp(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test gate")
        return original(*args, **kwargs)

    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", blocked_temp)
    retired = threading.Event()
    deadline = time.monotonic() + 1
    writing = asyncio.create_task(
        run_cache_io(
            lambda: write_cache_file(path, old, expires_at=deadline, retired=retired),
            timeout_seconds=1,
            retire=retired.set,
        )
    )
    try:
        assert await wait_until(entered.is_set, timeout=5)
        with pytest.raises(TimeoutError):
            await writing
        assert retired.is_set()
        monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", original)
        release.set()
        await asyncio.to_thread(write_cache_file, path, new)
        assert read_cache_file(path) == new.with_cached_flags(True)
        assert list(tmp_path.iterdir()) == [path]
    finally:
        release.set()
        await asyncio.gather(writing, return_exceptions=True)


async def test_hook_that_suppresses_cancellation_does_not_hold_supervisor():
    entered, cancelled, release, retired = (asyncio.Event() for _ in range(4))

    async def hook():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise RuntimeError("late exception must be retrieved") from None
        finally:
            retired.set()

    collecting = asyncio.create_task(
        collect_startup_info(
            "a",
            "t",
            SimpleNamespace(startup_info=hook),
            emitted_at=_TIMESTAMP,
            timeout_seconds=0.5,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await asyncio.wait_for(collecting, timeout=2) is None
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert not retired.is_set()
    finally:
        release.set()
        await asyncio.wait_for(retired.wait(), timeout=5)


async def test_sync_hook_offloop_and_late_result_discarded(tmp_paths, monkeypatch):
    import medre.runtime.app as module

    app = _build_app(_config_with_two_fake_adapters(), tmp_paths)
    monkeypatch.setattr(module, "_STARTUP_INFO_TIMEOUT_SECONDS", 0.8)
    entered, release, retired = threading.Event(), threading.Event(), threading.Event()

    def stalled():
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("test gate")
            return {"board": "LATE"}
        finally:
            retired.set()

    monkeypatch.setattr(app.adapters["alpha"], "startup_info", stalled, raising=False)
    monkeypatch.setattr(
        app.adapters["beta"], "startup_info", lambda: {"board": "PEER"}, raising=False
    )
    starting = asyncio.create_task(app.start())
    try:
        assert await wait_until(entered.is_set, timeout=5)
        assert await wait_until(lambda: "beta" in app._startup_info, timeout=5)
        assert not release.is_set()
        await asyncio.wait_for(starting, timeout=3)
        assert sorted(app.started_adapter_ids) == ["alpha", "beta"]
        assert "alpha" not in app._startup_info
        release.set()
        assert await wait_until(retired.is_set, timeout=5)
        assert not _cache_path(tmp_paths, "alpha").exists()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await app.stop()


async def test_self_cancelled_hook_does_not_cancel_started_adapter(tmp_paths):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)

    async def cancelled():
        raise asyncio.CancelledError

    app.adapters[FAKE_ADAPTER_ID].startup_info = cancelled
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert not app._startup_info
    finally:
        await app.stop()


async def test_broken_mapping_normalization_is_optional():
    class Broken(dict):
        def items(self):
            raise RuntimeError("private failure")

    assert await _collect_values(Broken(board="RAK4631")) is None


async def test_failed_cache_write_retains_fresh_info_and_startup(
    tmp_paths, monkeypatch, caplog
):
    import medre.runtime.app as module

    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    _install_startup_info(app, {"board": "RAK4631"})

    def fail(*args, **kwargs):
        raise OSError("syt_hidden")

    monkeypatch.setattr(module, "write_cache_file", fail)
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert app._startup_info[FAKE_ADAPTER_ID].fields[0].value == "RAK4631"
        assert "startup-info cache unavailable (OSError)" in caplog.text
        assert "syt_hidden" not in caplog.text
    finally:
        await app.stop()


@pytest.mark.parametrize("operation", ["read_cache_file", "write_cache_file"])
async def test_slow_cache_io_cannot_hold_startup(tmp_paths, monkeypatch, operation):
    import medre.runtime.app as module

    monkeypatch.setattr(module, "_STARTUP_INFO_TIMEOUT_SECONDS", 0.4)
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    _install_startup_info(app, {"board": "RAK4631"})
    entered, release, retired = threading.Event(), threading.Event(), threading.Event()
    original = getattr(module, operation)

    def stalled(*args, **kwargs):
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("test gate")
            return original(*args, **kwargs)
        finally:
            retired.set()

    monkeypatch.setattr(module, operation, stalled)
    starting = asyncio.create_task(app.start())
    try:
        assert await wait_until(entered.is_set, timeout=5)
        await asyncio.wait_for(starting, timeout=3)
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert not retired.is_set()
        assert app._startup_info[FAKE_ADAPTER_ID].fields[0].value == "RAK4631"
    finally:
        release.set()
        assert await wait_until(retired.is_set, timeout=5)
        await asyncio.gather(starting, return_exceptions=True)
        await app.stop()
    if operation == "write_cache_file":
        assert not _cache_path(tmp_paths).exists()


async def test_initial_degraded_health_does_not_overwrite_later_hook_health(
    tmp_paths, monkeypatch, caplog
):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    adapter = app.adapters[FAKE_ADAPTER_ID]
    base_health = await adapter.health_check()
    current = "degraded"

    async def health():
        return replace(base_health, health=current)

    async def startup_info():
        nonlocal current
        current = "healthy"
        assert adapter.ctx is not None
        adapter.ctx.report_health(replace(base_health, health="healthy"))
        assert await wait_until(
            lambda: app.adapter_states[FAKE_ADAPTER_ID].value == "ready"
        )
        return {"board": "RAK4631"}

    monkeypatch.setattr(adapter, "health_check", health)
    monkeypatch.setattr(adapter, "startup_info", startup_info, raising=False)
    caplog.set_level(logging.INFO, logger="medre.runtime.app")
    await app.start()
    try:
        info = app.startup_info.get(FAKE_ADAPTER_ID)
        assert info is not None
        assert {f.name: f.value for f in info.fields} == {"board": "RAK4631"}
        assert app.adapter_states[FAKE_ADAPTER_ID] is AdapterState.READY
        assert app.live_health is not None
        assert app.live_health.adapters[FAKE_ADAPTER_ID].health == "healthy"
        assert any(
            f".{FAKE_ADAPTER_ID} started in " in record.message
            and "(health=healthy)" in record.message
            for record in caplog.records
        )
    finally:
        await app.stop()


async def test_cache_uses_configured_transport_not_display_platform(
    tmp_paths, monkeypatch
):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    monkeypatch.setattr(app.adapters[FAKE_ADAPTER_ID], "platform", "Matrix")
    _install_startup_info(app, {"board": "RAK4631"})
    await app.start()
    try:
        assert (
            read_cache_file(
                _cache_path(tmp_paths), adapter_id=FAKE_ADAPTER_ID, transport="matrix"
            )
            is not None
        )
    finally:
        await app.stop()


@pytest.mark.usefixtures("isolated_config_env")
@pytest.mark.parametrize("json_output", [False, True])
def test_explicit_config_selects_configured_inventory(
    tmp_path, monkeypatch, json_output
):
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=True)
    alternate = tmp_path / "beta.yaml"
    alternate.write_text(
        _CLI_CONFIG.replace("    main:", "    beta:").replace(
            "adapter_id: main", "adapter_id: beta"
        )
    )
    beta = replace(_sample(), adapter_id="beta")
    write_cache_file(tmp_path / "state" / "adapters" / "beta" / CACHE_FILENAME, beta)
    args = ["adapters", "--config", str(alternate)]
    if json_output:
        args.append("--json")
    output = _run_cli(*args)
    if json_output:
        rows = json.loads(output)["configured_adapters"]
        assert [r["adapter_id"] for r in rows] == ["beta"]
        assert rows[0]["startup_info"]["fields"][0]["value"] == "HELTEC_V3"
    else:
        assert "matrix.beta: enabled" in output and "HELTEC_V3" in output
        assert "matrix.main: enabled" not in output
    assert "MEDREAB12CD" not in output


@pytest.mark.parametrize(
    "action",
    [
        ("check",),
        ("install", "matrix"),
        ("remove", "matrix"),
        ("reset",),
        ("update",),
        ("upgrade",),
        ("sources",),
    ],
)
def test_json_rejected_for_nonlisting_actions(action):
    from tests.helpers.cli import _run_cli_raw

    _out, err, status = _run_cli_raw("adapters", *action, "--json")
    assert status == 2
    assert "--json is supported only for the adapter listing" in err


@pytest.mark.usefixtures("isolated_config_env")
@pytest.mark.parametrize("json_output", [False, True])
def test_garbage_cache_does_not_prevent_listing(tmp_path, monkeypatch, json_output):
    from tests.helpers.cli import _run_cli

    _plant_cli_state(monkeypatch, tmp_path, with_cache=True)
    (tmp_path / "state" / "adapters" / "main" / CACHE_FILENAME).write_text(
        "[" * 2000 + "]" * 2000
    )
    output = _run_cli("adapters", *(["--json"] if json_output else []))
    if json_output:
        row = json.loads(output)["configured_adapters"][0]
        assert row["adapter_id"] == "main"
        assert "startup_info" not in row
    else:
        assert "matrix.main: enabled" in output
        assert "(cached)" not in output


async def test_malformed_cache_cannot_fail_runtime_preparation(tmp_paths):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    path = _cache_path(tmp_paths)
    path.parent.mkdir(parents=True)
    path.write_text("[" * 2000 + "]" * 2000)
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert not app._startup_info
    finally:
        await app.stop()


async def test_discovery_failure_cannot_roll_back_started_adapter(
    tmp_paths, monkeypatch
):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)

    def broken(_self):
        raise RuntimeError("syt_hidden")

    monkeypatch.setattr(
        type(app.adapters[FAKE_ADAPTER_ID]),
        "startup_info",
        property(broken),
        raising=False,
    )
    await app.start()
    try:
        assert app.started_adapter_ids == [FAKE_ADAPTER_ID]
        assert not app._startup_info
    finally:
        await app.stop()


def test_failed_replace_cleans_staging_file(tmp_path, monkeypatch):
    from medre.core import startup_info as module

    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, _sample())
    before = path.read_bytes()

    def fail(*args):
        raise PermissionError("test failed publication")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(PermissionError):
        write_cache_file(
            path, replace(_sample(), fields=(StartupInfoField("board", "NEW"),))
        )
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


async def test_compact_fact_limit_survives_cache_roundtrip(tmp_path):
    info = await _collect_values(
        {
            "rows": [
                [{"name": f"field_{i}", "value": i} for i in range(100)]
                for _ in range(3)
            ]
        }
    )
    assert info is not None
    assert len(info.fields) == 24
    assert [f.row for f in info.fields] == [0] * 8 + [1] * 8 + [2] * 8
    path = tmp_path / CACHE_FILENAME
    write_cache_file(path, info)
    assert read_cache_file(path) == info.with_cached_flags(True)


async def test_live_startup_log_drops_endpoint_credentials_and_scrubs_public_key(
    tmp_paths, caplog
):
    app = _build_app(_config_with_one_fake_adapter(), tmp_paths)
    _install_startup_info(
        app,
        {
            "board": "RAK4631",
            "endpoint": "https://matrix.example.org/?access_token=opaque-test-secret",
            "public_key": "syt_hidden",
            "publicKeySecret": "private-test-secret",
        },
    )
    caplog.set_level(logging.INFO, logger="medre.runtime.app")
    await app.start()
    try:
        info = app.startup_info.get(FAKE_ADAPTER_ID)
        assert info is not None
        assert {f.name: f.value for f in info.fields} == {
            "board": "RAK4631",
            "public_key": "[REDACTED]",
        }
        stored = _cache_path(tmp_paths).read_text()
        for secret in ("opaque-test-secret", "syt_hidden", "private-test-secret"):
            assert secret not in stored
            assert secret not in caplog.text
    finally:
        await app.stop()


def test_waiting_cache_writer_expires_while_prior_replace_is_blocked(
    tmp_path, monkeypatch
):
    from medre.core import startup_info as module

    path = tmp_path / CACHE_FILENAME
    entered, release = threading.Event(), threading.Event()
    original = module.os.replace

    def blocked_replace(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test gate")
        return original(*args)

    monkeypatch.setattr(module.os, "replace", blocked_replace)
    old = _sample()
    new = replace(old, fields=(StartupInfoField("board", "NEW"),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_cache_file, path, old)
        try:
            assert entered.wait(5)
            queued = pool.submit(
                write_cache_file, path, new, expires_at=time.monotonic() + 0.1
            )
            with pytest.raises(TimeoutError, match="expired"):
                queued.result(timeout=2)
            assert not first.done()
        finally:
            release.set()
        first.result(timeout=5)
    assert read_cache_file(path) == old.with_cached_flags(True)
    assert list(tmp_path.iterdir()) == [path]
