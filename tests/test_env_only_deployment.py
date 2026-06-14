"""End-to-end test proving env-only fake deployment works.

Demonstrates that a complete MEDRE runtime — config loading, adapter creation,
route wiring, event injection, and storage persistence — can be driven entirely
from environment variables with a minimal TOML skeleton (no adapter or route
stanzas).

Covers:
  1. Config loading + env overrides produce correct adapter/route structures.
  2. RuntimeBuilder builds and starts adapters created from env vars.
  3. A CanonicalEvent injected from a Meshtastic source adapter is routed
     through the pipeline to a Matrix destination adapter, with delivery
     receipts and native refs persisted in SQLite storage.
  4. Secret values (access_token) are never leaked into storage output.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from msgspec.structs import asdict as struct_asdict

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.events.canonical import CanonicalEvent
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all env vars between tests to prevent shell/CI contamination."""
    import os

    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Minimal TOML config — no adapters, no routes
# ---------------------------------------------------------------------------

_ENV_ONLY_TOML_TEMPLATE = """\
[runtime]
name = "env-only-deployment-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{db_path}"
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET_TOKEN = "fake-secret-token-here"


def _write_config(tmp_path: Path, db_path: str) -> Path:
    """Write the minimal TOML config and return its path."""
    config_path = tmp_path / "env_only.toml"
    config_path.write_text(_ENV_ONLY_TOML_TEMPLATE.format(db_path=db_path))
    return config_path


def _set_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all env vars for the env-only deployment scenario."""
    # --- Matrix fake adapter (token: MATRIX_FAKE) ---
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT", "matrix")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND", "fake")
    monkeypatch.setenv(
        "MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER", "https://matrix.example.test"
    )
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__USER_ID", "@bot:example.test")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv(
        "MEDRE_ADAPTER__MATRIX_FAKE__ROOM_ALLOWLIST", "!room:example.test"
    )

    # --- Meshtastic fake adapter (token: RADIO_A) ---
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ORIGIN_LABEL", "RadioA")

    # --- Route (token: RADIO_TO_MATRIX) ---
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS", "radio-a")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS", "matrix-fake")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY", "source_to_dest")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED", "true")


def _load_with_env(
    tmp_path: Path,
    db_path: str,
) -> tuple[Any, Any, Any]:
    """Write config, load TOML, apply env overrides, return (config, source, paths)."""
    config_path = _write_config(tmp_path, db_path)
    config, source, paths = load_config(str(config_path))
    config = apply_env_overrides(config)
    return config, source, paths


def _make_pipeline_event(adapter: Any, text: str) -> CanonicalEvent:
    """Create a CanonicalEvent with both 'body' and 'text' payload keys.

    FakeMeshtasticAdapter.make_text_event decodes a Meshtastic packet which
    stores the text under ``payload["body"]``.  TextRenderer reads
    ``payload["text"]``.  This helper bridges the gap so the rendered output
    is non-empty.
    """
    base = adapter.make_text_event(body=text)
    merged = dict(base.payload)
    merged["text"] = text
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvOnlyDeployment:
    """End-to-end: env-only fake deployment through the full runtime stack."""

    # -- Test 1: Config loading -----------------------------------------------

    def test_env_only_config_loads(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Env overrides create correct adapter and route configs."""
        db_path = str(tmp_path / "config_test.db")
        _set_env_vars(monkeypatch)
        config, _source, _paths = _load_with_env(tmp_path, db_path)

        # Matrix adapter "matrix-fake" exists with correct fields.
        assert "matrix-fake" in config.adapters.matrix
        matrix_cfg = config.adapters.matrix["matrix-fake"]
        assert matrix_cfg.config.homeserver == "https://matrix.example.test"
        assert matrix_cfg.config.user_id == "@bot:example.test"
        assert matrix_cfg.config.access_token == _SECRET_TOKEN
        assert matrix_cfg.config.room_allowlist == {"!room:example.test"}

        # Meshtastic adapter "radio-a" exists with correct fields.
        assert "radio-a" in config.adapters.meshtastic
        mesh_cfg = config.adapters.meshtastic["radio-a"]
        assert mesh_cfg.config.connection_type == "fake"
        assert mesh_cfg.config.origin_label == "RadioA"

        # Route created from env with correct source/dest.
        assert len(config.routes.routes) == 1
        route = config.routes.routes[0]
        assert route.route_id == "radio-to-matrix"
        assert route.source_adapters == ("radio-a",)
        assert route.dest_adapters == ("matrix-fake",)
        assert route.enabled is True

    # -- Test 2: Build and start lifecycle ------------------------------------

    @pytest.mark.asyncio
    async def test_env_only_builds_and_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """RuntimeBuilder builds and starts adapters created from env vars."""
        db_path = str(tmp_path / "build_test.db")
        _set_env_vars(monkeypatch)
        config, _source, paths = _load_with_env(tmp_path, db_path)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await app.start()

            assert "radio-a" in app.adapters
            assert "matrix-fake" in app.adapters
            assert app.adapters["radio-a"].platform == "meshtastic"
            assert app.adapters["matrix-fake"].platform == "matrix"
        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    # -- Test 3: Full pipeline with event injection ---------------------------

    @pytest.mark.asyncio
    async def test_env_only_pipeline_inject_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Event flows from radio-a through pipeline to matrix-fake with storage."""
        db_path = str(tmp_path / "pipeline_test.db")
        _set_env_vars(monkeypatch)
        config, _source, paths = _load_with_env(tmp_path, db_path)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await asyncio.wait_for(
                _run_pipeline_session(app),
                timeout=30.0,
            )
        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    # -- Test 4: Secrets not leaked -------------------------------------------

    @pytest.mark.asyncio
    async def test_env_only_secrets_not_in_report(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Secret access_token does not appear in storage output or reports."""
        db_path = str(tmp_path / "secret_test.db")
        _set_env_vars(monkeypatch)
        config, _source, paths = _load_with_env(tmp_path, db_path)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await app.start()

            # Inject an event to populate storage.
            source_adapter = app.adapters["radio-a"]
            event = _make_pipeline_event(source_adapter, "secret-leak-check")
            await app.pipeline_runner.handle_ingress(event)

            # Collect storage data as JSON strings.
            storage = app.storage
            assert storage is not None

            stored_event = await storage.get(event.event_id)
            assert stored_event is not None

            # Serialize the stored event to JSON and check for leaks.
            event_json = json.dumps(
                {k: str(v) for k, v in stored_event.payload.items()},
            )
            assert (
                _SECRET_TOKEN not in event_json
            ), f"Secret token leaked in stored event payload: {event_json[:200]}"

            # Check receipts.
            receipts = await storage.list_receipts_for_event(event.event_id)
            for receipt in receipts:
                receipt_data = struct_asdict(receipt)
                receipt_json = json.dumps(
                    {k: str(v) for k, v in receipt_data.items()},
                )
                assert (
                    _SECRET_TOKEN not in receipt_json
                ), f"Secret token leaked in receipt: {receipt_json[:200]}"

            # Check native refs.
            native_refs = await storage.list_native_refs_for_event(event.event_id)
            for nr in native_refs:
                nr_data = struct_asdict(nr)
                nr_json = json.dumps(
                    {k: str(v) for k, v in nr_data.items()},
                )
                assert (
                    _SECRET_TOKEN not in nr_json
                ), f"Secret token leaked in native ref: {nr_json[:200]}"

            # Check adapter diagnostics don't leak the token.
            for adapter_id, adapter in app.adapters.items():
                diag = adapter.diagnostics()
                diag_json = json.dumps(diag)
                assert (
                    _SECRET_TOKEN not in diag_json
                ), f"Secret token leaked in adapter {adapter_id} diagnostics"

            # Config repr should also be clean (MatrixConfig redacts access_token).
            for _transport, _adapter_id, rtc in config.adapters.all_configs():
                if hasattr(rtc, "config") and rtc.config is not None:
                    config_repr = repr(rtc.config)
                    assert (
                        _SECRET_TOKEN not in config_repr
                    ), f"Secret token leaked in config repr: {config_repr[:200]}"

            # Verify that access_token-related text is redacted.
            matrix_cfg = config.adapters.matrix["matrix-fake"].config
            config_repr = repr(matrix_cfg)
            assert (
                "…" in config_repr or "***" in config_repr
            ), f"Expected redaction in MatrixConfig repr, got: {config_repr}"

        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")


# ---------------------------------------------------------------------------
# Pipeline session driver
# ---------------------------------------------------------------------------


async def _run_pipeline_session(app: Any) -> None:
    """Start app, inject event, verify delivery, storage, receipts, and refs."""
    await app.start()

    # Pick the Meshtastic source adapter.
    source_adapter = app.adapters["radio-a"]

    # Create and inject event.
    event = _make_pipeline_event(source_adapter, "env-only deployment smoke")

    outcomes = await app.pipeline_runner.handle_ingress(event)

    # -- Verify delivery outcomes ---------------------------------------------
    assert len(outcomes) >= 1, "Expected at least one delivery outcome"
    successful = [o for o in outcomes if o.status == "success"]
    assert (
        len(successful) >= 1
    ), f"No successful deliveries; outcomes: {[o.status for o in outcomes]}"

    target = successful[0].target_adapter
    assert target == "matrix-fake"

    # Verify route_id on the delivery outcome.
    assert getattr(successful[0], "route_id", None) == "radio-to-matrix"

    # -- Verify storage: event persisted --------------------------------------
    storage = app.storage
    assert storage is not None

    stored_event = await storage.get(event.event_id)
    assert stored_event is not None, f"Event {event.event_id!r} not found in storage"
    assert stored_event.event_id == event.event_id

    # -- Verify storage: delivery receipts ------------------------------------
    receipts = await storage.list_receipts_for_event(event.event_id)
    assert len(receipts) >= 1, "Expected at least one delivery receipt"
    sent_receipts = [r for r in receipts if r.status == "sent"]
    assert len(sent_receipts) >= 1, "No receipt with status 'sent'"

    # -- Verify storage: native refs ------------------------------------------
    native_refs = await storage.list_native_refs_for_event(event.event_id)
    outbound_refs = [
        nr for nr in native_refs if getattr(nr, "direction", None) == "outbound"
    ]
    assert (
        len(outbound_refs) >= 1
    ), "Expected at least one outbound native ref for matrix-fake"

    dest_ref = outbound_refs[0]
    assert getattr(dest_ref, "adapter", None) == "matrix-fake"
    assert getattr(dest_ref, "native_message_id", None) is not None

    # Resolve the outbound native ref back to the event.
    resolved = await storage.resolve_native_ref(
        adapter="matrix-fake",
        native_channel_id=dest_ref.native_channel_id,
        native_message_id=dest_ref.native_message_id,
    )
    assert (
        resolved == event.event_id
    ), f"Native ref resolved to {resolved!r}, expected {event.event_id!r}"
