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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.startup_info import (
    CACHE_FILENAME,
    AdapterStartupInfo,
    StartupInfoField,
    collect_startup_info,
    read_cache_file,
    render_startup_info_line,
    startup_info_callable,
    write_cache_file,
)
from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_one_fake_adapter,
)

FAKE_ADAPTER_ID = "fake_matrix"


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


class TestDiscovery:
    def test_absent_symbol_discovers_nothing(self) -> None:
        class Plain:
            pass

        assert startup_info_callable(Plain()) is None

    def test_non_callable_symbol_discovers_nothing(self) -> None:
        class Broken:
            startup_info = "not-a-callable"

        assert startup_info_callable(Broken()) is None

    def test_callable_symbol_is_discovered(self) -> None:
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


class TestCollectStartupInfo:
    async def test_emits_redacted_fields(self) -> None:
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
            emitted_at="2026-11-19T00:00:00+00:00",
        )
        assert info is not None
        values = {field.name: field.value for field in info.fields}
        assert values == {"board": "HELTEC_V3", "firmware": "2.5.4"}
        assert all(not field.cached for field in info.fields)
        assert info.adapter_id == "node"
        assert info.transport == "meshtastic"

    async def test_absent_provider_yields_none(self) -> None:
        assert await collect_startup_info("a", "t", object(), emitted_at="now") is None

    async def test_non_mapping_result_yields_none(self) -> None:
        class Provider:
            async def startup_info(self) -> str:
                return "nope"

        assert (
            await collect_startup_info("a", "t", Provider(), emitted_at="now") is None
        )

    async def test_empty_fields_yield_none(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"identity_key": "k" * 64}

        assert (
            await collect_startup_info("a", "t", Provider(), emitted_at="now") is None
        )

    async def test_timeout_yields_none(self) -> None:
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
            emitted_at="now",
        )
        release.set()
        assert info is None

    async def test_failure_yields_none(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                raise RuntimeError("provider bug")

        assert (
            await collect_startup_info("a", "t", Provider(), emitted_at="now") is None
        )

    async def test_caller_cancellation_propagates(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                await asyncio.Event().wait()

        task = asyncio.ensure_future(
            collect_startup_info("a", "t", Provider(), emitted_at="now")
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Core: render-boundary redaction
# ---------------------------------------------------------------------------


class TestRenderBoundaryRedaction:
    async def test_secret_and_pairing_names_never_render(self) -> None:
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

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is None, "every field was secret-like; nothing may render"

    async def test_public_keys_render_in_full(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"public_key": "K" * 43}

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is not None
        assert info.fields[0].value == "K" * 43

    async def test_addresses_render_in_full(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {
                    "node_address": "!a4cf1214e5f6a7b8",
                    "user_id": "@bridge:example.org",
                    "mac": "aa:bb:cc:dd:ee:ff",
                }

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is not None
        values = {field.name: field.value for field in info.fields}
        assert values["node_address"] == "!a4cf1214e5f6a7b8"
        assert values["user_id"] == "@bridge:example.org"
        assert values["mac"] == "aa:bb:cc:dd:ee:ff"

    async def test_shared_secrets_drop_even_under_unlisted_names(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"channel_psk": "a" * 64}

        # Every field dropping yields no record at all rather than an
        # empty one: a provider whose only startup fact is a secret
        # reports nothing.
        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is None

    async def test_device_identifiers_render_in_full(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, object]:
                return {
                    "device_id": "!a4b3c2d1e5f6a7b8",
                    "deviceId": 2460232048,
                }

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is not None
        values = {field.name: field.value for field in info.fields}
        assert values["device_id"] == "!a4b3c2d1e5f6a7b8"
        assert values["deviceId"] == "2460232048"

    async def test_credential_bearing_url_is_dropped(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"endpoint": "https://user:pass@matrix.example.org"}

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is None

    async def test_control_characters_drop_the_field(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"board": "HELTEC\x1b[31m"}

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is None

    async def test_value_token_scrub_applies(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {"banner": "logged in with syt_abc123"}

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is not None
        assert "[REDACTED]" in info.fields[0].value
        assert "syt_abc123" not in info.fields[0].value

    async def test_render_line_is_idempotent(self) -> None:
        class Provider:
            async def startup_info(self) -> dict[str, str]:
                return {
                    "board": "HELTEC_V3",
                    "public_key": "a" * 64,
                    "user_id": "@bridge:example.org",
                }

        info = await collect_startup_info("a", "t", Provider(), emitted_at="now")
        assert info is not None
        first = render_startup_info_line(info)
        second = render_startup_info_line(info)
        assert first == second
        cached = info.with_cached_flags(True)
        cached_line = render_startup_info_line(cached)
        assert cached_line == first + " (cached)"


# ---------------------------------------------------------------------------
# Core: durable display-only cache
# ---------------------------------------------------------------------------


class TestStartupInfoCache:
    def _sample(self) -> AdapterStartupInfo:
        return AdapterStartupInfo(
            adapter_id="main",
            transport="matrix",
            fields=(StartupInfoField(name="board", value="HELTEC_V3"),),
            emitted_at="2026-11-19T00:00:00+00:00",
        )

    def test_round_trip_marks_cached(self, tmp_path: Path) -> None:
        path = tmp_path / CACHE_FILENAME
        write_cache_file(path, self._sample())
        cached = read_cache_file(path)
        assert cached is not None
        assert all(field.cached for field in cached.fields)
        assert cached.fields[0].value == "HELTEC_V3"
        assert cached.emitted_at == "2026-11-19T00:00:00+00:00"

    def test_missing_cache_reads_none(self, tmp_path: Path) -> None:
        assert read_cache_file(tmp_path / "absent.json") is None

    def test_malformed_cache_reads_none(self, tmp_path: Path) -> None:
        path = tmp_path / CACHE_FILENAME
        path.write_text("{not json")
        assert read_cache_file(path) is None
        path.write_text('{"adapter_id": 3}')
        assert read_cache_file(path) is None

    def test_tampered_cache_cannot_inject_key_material(self, tmp_path: Path) -> None:
        path = tmp_path / CACHE_FILENAME
        path.write_text(
            json.dumps(
                {
                    "adapter_id": "main",
                    "transport": "matrix",
                    "emitted_at": "now",
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


class TestReadyTransitionEmission:
    async def test_emitted_at_ready_and_persisted(self, tmp_paths: MedrePaths) -> None:
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
            cache = _cache_path(tmp_paths)
            stored = read_cache_file(cache)
            assert stored is not None
            assert {f.name: f.value for f in stored.fields} == {
                "board": "FakeBoard",
                "firmware": "1.2.3",
            }
        finally:
            await app.stop()

    async def test_adapter_without_symbol_reports_nothing(
        self, tmp_paths: MedrePaths
    ) -> None:
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
        self, tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
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
        self, tmp_paths: MedrePaths
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


class TestRestartCachedMarker:
    async def test_cached_until_fresh_replaces(self, tmp_paths: MedrePaths) -> None:
        first = _build_app(_config_with_one_fake_adapter(), tmp_paths)
        _install_startup_info(first, {"board": "OLD_BOARD"})
        await first.start()
        await first.stop()

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
            # Releasing the blocked start completes the ready transition;
            # the fresh emission replaces the cached record.
            release.set()
            await starting
            fresh = second._startup_info[FAKE_ADAPTER_ID]
            assert fresh.fields[0].value == "NEW_BOARD"
            assert fresh.fields[0].cached is False
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
                        {"name": "homeserver", "value": "https://matrix.example.org"},
                        {"name": "board", "value": "HELTEC_V3"},
                    ],
                }
            )
        )
    return tmp_path


@pytest.mark.usefixtures("isolated_config_env")
class TestAdaptersCliRendering:
    def test_cached_line_renders_with_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.helpers.cli import _run_cli

        _plant_cli_state(monkeypatch, tmp_path, with_cache=True)
        output = _run_cli("adapters")
        assert "Configured adapters:" in output
        assert "  matrix.main: enabled (kind=fake)" in output
        assert (
            "    startup: homeserver=https://matrix.example.org board=HELTEC_V3 (cached)"
            in output
        )

    def test_without_info_no_startup_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.helpers.cli import _run_cli

        _plant_cli_state(monkeypatch, tmp_path, with_cache=False)
        output = _run_cli("adapters")
        assert "Configured adapters:" in output
        assert "startup:" not in output
        assert "(cached)" not in output

    def test_json_carries_structured_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            "homeserver": "https://matrix.example.org",
            "board": "HELTEC_V3",
        }
        assert all(f["cached"] is True for f in fields)
