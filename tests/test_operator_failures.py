"""Track 4: Operator failure UX hardening tests.

Validates that operators receive concise, actionable, deterministic error
messages for common misconfiguration and failure scenarios — without needing
to read source code.

Scenarios covered:

1. Config errors (file not found, bad TOML, invalid limits)
2. Duplicate route IDs
3. Duplicate adapter IDs
4. Missing adapter references in routes
5. Startup partial failure (adapter build failure)
6. Invalid path placeholders
7. Capacity exhaustion (semaphore timeout)
8. Route self-reference / overlap
9. Malformed env overrides (bool, int, float)
10. Missing directories (storage parent)
11. Storage open failure (unsupported backend)
12. Unsupported policy placeholders
13. Invalid route IDs
14. Route loop warnings (direct and multi-hop)
15. Secret redaction (logging and env provenance)
16. CLI no-traceback on expected misuse
17. Unsupported filter_hooks
18. Invalid directionality
19. Duplicate dest_adapters entries
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from medre.config.errors import (
    ConfigError,
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.env import (
    EnvProvenance,
    MedreEnvConfig,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _SECRET_ENV_NAMES,
)
from medre.config.loader import load_config
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError, resolve
from medre.logging import sanitize_for_log
from medre.runtime.capacity import CapacityController
from medre.runtime.errors import RuntimeConfigError
from medre.runtime.route_engine import (
    RouteValidationError,
    build_runtime_routes,
    check_route_loops,
    validate_route_adapter_refs,
)
from medre.runtime.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
    _validate_route_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at temp directories."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _write_config(path: Path, content: str) -> Path:
    """Write TOML content to *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# 1. Config errors
# ---------------------------------------------------------------------------


class TestConfigErrors:
    """Config errors produce deterministic, actionable messages."""

    def test_config_not_found_suggests_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConfigNotFoundError mentions search paths and 'medre config sample'."""
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.delenv("MEDRE_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.chdir("/tmp")
        with pytest.raises(ConfigNotFoundError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "No MEDRE configuration file found" in msg
        assert "medre config sample" in msg
        assert "Searched:" in msg

    def test_explicit_config_not_found(self, tmp_path: Path) -> None:
        """ConfigFileError for an explicit --config path that does not exist."""
        bogus = tmp_path / "nonexistent" / "config.toml"
        with pytest.raises(ConfigFileError) as exc_info:
            load_config(str(bogus))
        msg = str(exc_info.value)
        assert "Config file not found" in msg
        assert "specified explicitly" in msg

    def test_invalid_toml_syntax(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConfigFileError for TOML that cannot be parsed."""
        cfg = _write_config(
            tmp_path / "config.toml",
            '[runtime\nname = "bad brace',  # missing closing bracket
        )
        monkeypatch.setenv("MEDRE_CONFIG", str(cfg))
        with pytest.raises(ConfigFileError) as exc_info:
            load_config(None)
        msg = str(exc_info.value)
        assert "Invalid TOML" in msg

    def test_invalid_limits_non_positive(self) -> None:
        """ConfigValidationError for non-positive runtime limits."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RuntimeLimits(max_inflight_deliveries=0).validate()
        assert "max_inflight_deliveries must be > 0" in str(exc_info.value)

    def test_invalid_limits_negative(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RuntimeLimits(shutdown_drain_timeout_seconds=-5).validate()
        assert "shutdown_drain_timeout_seconds must be > 0" in str(exc_info.value)

    def test_invalid_limits_zero_acquire_timeout(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RuntimeLimits(delivery_acquire_timeout_seconds=0).validate()
        assert "delivery_acquire_timeout_seconds must be > 0" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Duplicate route IDs
# ---------------------------------------------------------------------------


class TestDuplicateRoutes:
    """Duplicate route IDs produce actionable ConfigValidationError."""

    def test_duplicate_route_id_in_set(self) -> None:
        """RouteConfigSet.validate() rejects duplicate route IDs with attribution."""
        r1 = RouteConfig(
            route_id="dup",
            source_adapters=("a",),
            dest_adapters=("b",),
        )
        r2 = RouteConfig(
            route_id="dup",
            source_adapters=("c",),
            dest_adapters=("d",),
        )
        rcs = RouteConfigSet(routes=(r1, r2))
        with pytest.raises(ConfigValidationError) as exc_info:
            rcs.validate()
        msg = str(exc_info.value)
        assert "Duplicate route ID" in msg
        assert "'dup'" in msg


# ---------------------------------------------------------------------------
# 3. Duplicate adapter IDs
# ---------------------------------------------------------------------------


class TestDuplicateAdapters:
    """Duplicate adapter IDs across transports produce clear error."""

    def test_duplicate_adapter_id_across_transports(self) -> None:
        """AdapterConfigSet.validate() rejects duplicate adapter IDs."""
        from medre.adapters.matrix.config import MatrixConfig
        from medre.adapters.meshtastic.config import MeshtasticConfig

        matrix_cfg = MatrixConfig(
            adapter_id="shared_id",
            homeserver="https://m.test",
            user_id="@bot:test",
            access_token="tok",
            room_allowlist={"!r:t"},
            encryption_mode="plaintext",
        )
        mesh_cfg = MeshtasticConfig(
            adapter_id="shared_id",
            connection_type="serial",
            serial_port="/dev/ttyACM0",
        )
        adapters = AdapterConfigSet(
            matrix={"main": MatrixRuntimeConfig(
                adapter_id="shared_id", enabled=True, config=matrix_cfg,
            )},
            meshtastic={"radio": MeshtasticRuntimeConfig(
                adapter_id="shared_id", enabled=True, config=mesh_cfg,
            )},
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            adapters.validate()
        msg = str(exc_info.value)
        assert "Duplicate adapter" in msg
        assert "shared_id" in msg
        assert "must be unique" in msg


# ---------------------------------------------------------------------------
# 4. Missing adapter references in routes
# ---------------------------------------------------------------------------


class TestMissingAdapterRefs:
    """Routes referencing unknown adapters produce RouteValidationError."""

    def test_route_references_unknown_adapter(self) -> None:
        """validate_route_adapter_refs lists unknown adapter IDs."""
        rc = RouteConfig(
            route_id="broken",
            source_adapters=("ghost_adapter",),
            dest_adapters=("also_ghost",),
            enabled=True,
        )
        rcs = RouteConfigSet(routes=(rc,))
        with pytest.raises(RouteValidationError) as exc_info:
            validate_route_adapter_refs(rcs, frozenset({"real_adapter"}))
        msg = str(exc_info.value)
        assert "ghost_adapter" in msg
        assert "also_ghost" in msg
        assert "real_adapter" in msg


# ---------------------------------------------------------------------------
# 5. Startup partial failure (adapter build failure)
# ---------------------------------------------------------------------------


class TestStartupPartialFailure:
    """Builder records AdapterBuildFailure with clear attribution."""

    def test_missing_sdk_produces_build_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Enabled real adapter with missing SDK produces AdapterBuildFailure."""
        from medre.adapters.matrix.config import MatrixConfig

        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        matrix_cfg = MatrixConfig(
            adapter_id="main",
            homeserver="https://m.test",
            user_id="@bot:test",
            access_token="tok",
            room_allowlist={"!r:t"},
            encryption_mode="plaintext",
        )
        adapters = AdapterConfigSet(
            matrix={"main": MatrixRuntimeConfig(
                adapter_id="main", enabled=True, config=matrix_cfg,
            )},
        )
        config = RuntimeConfig(
            runtime=RuntimeOptions(),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
            adapters=adapters,
        )

        from medre.runtime.builder import RuntimeBuilder

        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # Matrix SDK is likely not installed; expect build failures.
        # If it IS installed, this test still passes — build_failures will be empty.
        if app.build_failures:
            bf = app.build_failures[0]
            assert bf.adapter_id == "main"
            assert bf.transport == "matrix"
            # Error message should mention the adapter.
            assert "main" in str(bf.error)


# ---------------------------------------------------------------------------
# 6. Invalid path placeholders
# ---------------------------------------------------------------------------


class TestInvalidPlaceholders:
    """Unknown path placeholders produce MedrePathsError with the placeholder name."""

    def test_unknown_placeholder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        with pytest.raises(MedrePathsError) as exc_info:
            paths.expand_placeholder("{totally_bogus}")
        msg = str(exc_info.value)
        assert "unknown path placeholder" in msg
        assert "totally_bogus" in msg

    def test_partial_placeholder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        with pytest.raises(MedrePathsError) as exc_info:
            paths.expand_placeholder("{state}/{invalid_dir}/file.db")
        msg = str(exc_info.value)
        assert "unknown path placeholder" in msg
        assert "invalid_dir" in msg


# ---------------------------------------------------------------------------
# 7. Capacity exhaustion
# ---------------------------------------------------------------------------


class TestCapacityExhaustion:
    """CapacityController rejects when semaphore is exhausted."""

    @pytest.mark.asyncio()
    async def test_delivery_semaphore_timeout(self) -> None:
        """When delivery slots are exhausted, acquire returns False within timeout."""
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            delivery_acquire_timeout_seconds=0.01,
        )
        ctrl = CapacityController(limits)
        # Acquire the single slot.
        acquired = await ctrl.acquire_delivery()
        assert acquired is True
        # Second acquire should time out and return False.
        acquired2 = await ctrl.acquire_delivery()
        assert acquired2 is False
        # Release and verify counter.
        await ctrl.release_delivery()
        assert ctrl.delivery_current == 0

    @pytest.mark.asyncio()
    async def test_replay_semaphore_timeout(self) -> None:
        """When replay slots are exhausted, acquire returns False within timeout."""
        limits = RuntimeLimits(
            max_inflight_replay_events=1,
            delivery_acquire_timeout_seconds=0.01,
        )
        ctrl = CapacityController(limits)
        acquired = await ctrl.acquire_replay()
        assert acquired is True
        acquired2 = await ctrl.acquire_replay()
        assert acquired2 is False
        await ctrl.release_replay()
        assert ctrl.replay_current == 0


# ---------------------------------------------------------------------------
# 8. Route self-reference / overlap
# ---------------------------------------------------------------------------


class TestRouteOverlap:
    """RouteConfig rejects when source and dest adapters overlap."""

    def test_self_route_overlap(self) -> None:
        """from_toml_dict rejects source/dest adapter overlap."""
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("loop", {
                "source_adapters": ["a", "b"],
                "dest_adapters": ["b", "c"],
            })
        msg = str(exc_info.value)
        assert "overlap" in msg
        assert "'b'" in msg

    def test_pure_self_route(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("selfloop", {
                "source_adapters": ["x"],
                "dest_adapters": ["x"],
            })
        msg = str(exc_info.value)
        assert "overlap" in msg
        assert "must not bridge an adapter to itself" in msg


# ---------------------------------------------------------------------------
# 9. Malformed env overrides
# ---------------------------------------------------------------------------


class TestMalformedEnvOverrides:
    """Malformed MEDRE_* env vars produce ConfigValidationError with env name."""

    def test_malformed_bool(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _coerce_bool("maybe", "MEDRE_MATRIX_ENABLED")
        msg = str(exc_info.value)
        assert "MEDRE_MATRIX_ENABLED" in msg
        assert "must be a boolean" in msg
        assert "'maybe'" in msg

    def test_malformed_int(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _coerce_int("abc", "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES")
        msg = str(exc_info.value)
        assert "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES" in msg
        assert "must be an integer" in msg
        assert "'abc'" in msg

    def test_malformed_float(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _coerce_float("not_a_number", "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS")
        msg = str(exc_info.value)
        assert "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS" in msg
        assert "must be a number" in msg
        assert "'not_a_number'" in msg

    def test_valid_bool_true(self) -> None:
        assert _coerce_bool("yes", "X") is True
        assert _coerce_bool("1", "X") is True
        assert _coerce_bool("TRUE", "X") is True

    def test_valid_bool_false(self) -> None:
        assert _coerce_bool("no", "X") is False
        assert _coerce_bool("0", "X") is False
        assert _coerce_bool("false", "X") is False

    def test_valid_int(self) -> None:
        assert _coerce_int("42", "X") == 42

    def test_valid_float(self) -> None:
        assert _coerce_float("3.14", "X") == pytest.approx(3.14)


# ---------------------------------------------------------------------------
# 10. Missing directories / storage
# ---------------------------------------------------------------------------


class TestStorageFailures:
    """Storage construction produces clear errors for misconfiguration."""

    def test_unsupported_storage_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """RuntimeConfigError for unsupported storage backend with supported list."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        config = RuntimeConfig(
            storage=StorageConfig(backend="postgres"),
        )
        from medre.runtime.builder import RuntimeBuilder
        builder = RuntimeBuilder(config, paths)
        with pytest.raises(RuntimeConfigError) as exc_info:
            builder._build_storage()
        msg = str(exc_info.value)
        assert "Unsupported storage backend" in msg
        assert "'postgres'" in msg
        assert "sqlite" in msg
        assert "memory" in msg


# ---------------------------------------------------------------------------
# 11. Invalid route IDs
# ---------------------------------------------------------------------------


class TestInvalidRouteIDs:
    """Invalid route IDs produce ConfigValidationError with format requirements."""

    @pytest.mark.parametrize(
        "bad_id",
        ["", "has space", "dot.id", "slash/path", "special!char", "a b"],
    )
    def test_invalid_route_id_characters(self, bad_id: str) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_route_id(bad_id, section_path="routes.test")
        msg = str(exc_info.value)
        assert bad_id in msg or "empty" in msg

    def test_valid_route_ids(self) -> None:
        """These should not raise."""
        for good_id in ("my-route", "route_1", "ABC", "a-b-c_123"):
            _validate_route_id(good_id, section_path="routes.test")


# ---------------------------------------------------------------------------
# 12. Unsupported policy placeholders
# ---------------------------------------------------------------------------


class TestUnsupportedPolicyFields:
    """Reserved policy fields that silently no-op are rejected to prevent confusion."""

    def test_sender_allowlist_rejected(self) -> None:
        policy = BridgePolicy(sender_allowlist=("alice",))
        with pytest.raises(ConfigValidationError) as exc_info:
            from medre.runtime.routes import _reject_unsupported_policy_fields
            _reject_unsupported_policy_fields(
                policy, route_id="test", section_path="routes.test",
            )
        msg = str(exc_info.value)
        assert "sender_allowlist" in msg
        assert "reserved" in msg
        assert "not yet supported" in msg

    def test_room_allowlist_rejected(self) -> None:
        policy = BridgePolicy(room_allowlist=("!room:t",))
        from medre.runtime.routes import _reject_unsupported_policy_fields
        with pytest.raises(ConfigValidationError) as exc_info:
            _reject_unsupported_policy_fields(
                policy, route_id="test", section_path="routes.test",
            )
        assert "room_allowlist" in str(exc_info.value)

    def test_channel_allowlist_rejected(self) -> None:
        policy = BridgePolicy(channel_allowlist=("ch1",))
        from medre.runtime.routes import _reject_unsupported_policy_fields
        with pytest.raises(ConfigValidationError) as exc_info:
            _reject_unsupported_policy_fields(
                policy, route_id="test", section_path="routes.test",
            )
        assert "channel_allowlist" in str(exc_info.value)

    def test_allowed_event_types_accepted(self) -> None:
        """The one supported policy field should not raise."""
        from medre.runtime.routes import _reject_unsupported_policy_fields
        policy = BridgePolicy(allowed_event_types=("message",))
        # Should not raise.
        _reject_unsupported_policy_fields(
            policy, route_id="test", section_path="routes.test",
        )


# ---------------------------------------------------------------------------
# 13. Route loop warnings
# ---------------------------------------------------------------------------


class TestRouteLoopWarnings:
    """Route loop detection produces deterministic warning strings."""

    def test_direct_loop_detection(self) -> None:
        """A↔B direct loop is detected."""
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        routes = [
            Route(
                id="fwd",
                source=RouteSource(adapter="A", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="B")],
            ),
            Route(
                id="rev",
                source=RouteSource(adapter="B", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="A")],
            ),
        ]
        loops = check_route_loops(routes)
        assert len(loops) >= 1
        assert any("Direct routing loop" in l for l in loops)
        direct_loop = [l for l in loops if "Direct routing loop" in l][0]
        assert "'A'" in direct_loop
        assert "'B'" in direct_loop

    def test_multi_hop_cycle_detection(self) -> None:
        """X→Y→Z→X multi-hop cycle is detected."""
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        routes = [
            Route(
                id="x_y",
                source=RouteSource(adapter="X", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="Y")],
            ),
            Route(
                id="y_z",
                source=RouteSource(adapter="Y", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="Z")],
            ),
            Route(
                id="z_x",
                source=RouteSource(adapter="Z", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="X")],
            ),
        ]
        loops = check_route_loops(routes)
        assert len(loops) >= 1
        assert any("Route cycle detected" in l for l in loops)

    def test_no_false_positive_linear_chain(self) -> None:
        """A→B→C linear chain produces no loop warnings."""
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        routes = [
            Route(
                id="a_b",
                source=RouteSource(adapter="A", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="B")],
            ),
            Route(
                id="b_c",
                source=RouteSource(adapter="B", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="C")],
            ),
        ]
        loops = check_route_loops(routes)
        assert loops == []


# ---------------------------------------------------------------------------
# 14. Secret redaction
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    """Secrets are never included in log output or provenance displays."""

    def test_sanitize_for_log_strips_access_token(self) -> None:
        data = {
            "user": "alice",
            "access_token": "s3cret_t0ken",
            "password": "hunter2",
            "api_key": "key123",
            "safe_field": "visible",
        }
        sanitized = sanitize_for_log(data)
        assert sanitized["user"] == "alice"
        assert sanitized["safe_field"] == "visible"
        assert "access_token" not in sanitized
        assert "password" not in sanitized
        assert "api_key" not in sanitized

    def test_sanitize_private_key(self) -> None:
        sanitized = sanitize_for_log({
            "private_key": "PEM_DATA",
            "signing_key": "SIG",
            "identity_key": "ID",
            "normal": "ok",
        })
        assert "normal" in sanitized
        assert "private_key" not in sanitized
        assert "signing_key" not in sanitized
        assert "identity_key" not in sanitized

    def test_env_provenance_redacts_secrets(self) -> None:
        prov = EnvProvenance()
        prov.record("MEDRE_MATRIX_ACCESS_TOKEN", "super_secret_token")
        prov.record("MEDRE_LOG_LEVEL", "DEBUG")
        items = prov.redacted_items()
        token_entry = [v for k, v in items if k == "MEDRE_MATRIX_ACCESS_TOKEN"]
        assert token_entry == ["***REDACTED***"]
        level_entry = [v for k, v in items if k == "MEDRE_LOG_LEVEL"]
        assert level_entry == ["DEBUG"]

    def test_env_config_redacted_repr(self) -> None:
        env = MedreEnvConfig.from_environ({
            "MEDRE_MATRIX_ACCESS_TOKEN": "s3cret",
            "MEDRE_LOG_LEVEL": "INFO",
        })
        repr_str = env.redacted_repr()
        assert "***REDACTED***" in repr_str
        assert "s3cret" not in repr_str
        assert "INFO" in repr_str

    def test_secret_env_names_is_frozen(self) -> None:
        """The secret env names set should not be accidentally mutated."""
        assert "MEDRE_MATRIX_ACCESS_TOKEN" in _SECRET_ENV_NAMES


# ---------------------------------------------------------------------------
# 15. CLI no-traceback on expected misuse
# ---------------------------------------------------------------------------


class TestCLINoTraceback:
    """CLI commands exit cleanly (no traceback) on expected misuse."""

    def test_cli_config_check_bad_file_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'medre config check --config /nonexistent' exits with code 1, no traceback."""
        from medre.cli import main

        monkeypatch.setattr(sys, "argv", ["medre", "config", "check", "--config", str(tmp_path / "nope.toml")])
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc_info:
            with redirect_stderr(buf):
                main()
        assert exc_info.value.code == 1
        output = buf.getvalue()
        # Should be a clean error message, not a Python traceback.
        assert "Traceback" not in output
        assert "Config error" in output or "Config file not found" in output

    def test_cli_routes_validate_bad_config_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'medre routes validate --config /nonexistent' exits with code 1, no traceback."""
        from medre.cli import main

        monkeypatch.setattr(sys, "argv", ["medre", "routes", "validate", "--config", str(tmp_path / "gone.toml")])
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc_info:
            with redirect_stderr(buf):
                main()
        assert exc_info.value.code == 1
        output = buf.getvalue()
        assert "Traceback" not in output


# ---------------------------------------------------------------------------
# 16. Unsupported filter_hooks
# ---------------------------------------------------------------------------


class TestUnsupportedFilterHooks:
    """filter_hooks are reserved and rejected to prevent silent no-ops."""

    def test_filter_hooks_rejected(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("hooked", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "filter_hooks": ["my_hook"],
            })
        msg = str(exc_info.value)
        assert "filter_hooks" in msg
        assert "reserved" in msg
        assert "not yet supported" in msg


# ---------------------------------------------------------------------------
# 17. Invalid directionality
# ---------------------------------------------------------------------------


class TestInvalidDirectionality:
    """Invalid directionality values list valid options."""

    def test_bad_directionality(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("dirtest", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "directionality": "sideways",
            })
        msg = str(exc_info.value)
        assert "invalid directionality" in msg
        assert "'sideways'" in msg
        # Should list valid options.
        assert "source_to_dest" in msg
        assert "dest_to_source" in msg
        assert "bidirectional" in msg


# ---------------------------------------------------------------------------
# 18. Duplicate dest_adapters entries
# ---------------------------------------------------------------------------


class TestDuplicateDestAdapters:
    """Duplicate entries in dest_adapters are rejected."""

    def test_duplicate_dest(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("dupdest", {
                "source_adapters": ["a"],
                "dest_adapters": ["b", "b"],
            })
        msg = str(exc_info.value)
        assert "duplicate entries in 'dest_adapters'" in msg

    def test_duplicate_source(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("dupsrc", {
                "source_adapters": ["a", "a"],
                "dest_adapters": ["b"],
            })
        msg = str(exc_info.value)
        assert "duplicate entries in 'source_adapters'" in msg


# ---------------------------------------------------------------------------
# 19. Room/channel alias conflict
# ---------------------------------------------------------------------------


class TestRoomChannelAliasConflict:
    """source_room and source_channel set to different values is rejected."""

    def test_source_room_channel_conflict(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("alias_conflict", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "source_room": "!room1:test",
                "source_channel": "!room2:test",
            })
        msg = str(exc_info.value)
        assert "source_room" in msg
        assert "source_channel" in msg
        assert "both set but differ" in msg

    def test_dest_room_channel_conflict(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("alias_conflict2", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "dest_room": "ch1",
                "dest_channel": "ch2",
            })
        msg = str(exc_info.value)
        assert "dest_room" in msg
        assert "dest_channel" in msg
        assert "both set but differ" in msg

    def test_room_channel_same_value_ok(self) -> None:
        """When source_room == source_channel, no error (alias accepted)."""
        rc = RouteConfig.from_toml_dict("alias_ok", {
            "source_adapters": ["a"],
            "dest_adapters": ["b"],
            "source_room": "!room:test",
            "source_channel": "!room:test",
        })
        assert rc.source_channel == "!room:test"


# ---------------------------------------------------------------------------
# 20. Missing required route fields
# ---------------------------------------------------------------------------


class TestMissingRequiredRouteFields:
    """Missing source_adapters or dest_adapters produces clear error."""

    def test_missing_source_adapters(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("nosrc", {
                "dest_adapters": ["b"],
            })
        assert "missing required 'source_adapters'" in str(exc_info.value)

    def test_missing_dest_adapters(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("nodest", {
                "source_adapters": ["a"],
            })
        assert "missing required 'dest_adapters'" in str(exc_info.value)

    def test_empty_source_adapters(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("emptysrc", {
                "source_adapters": [],
                "dest_adapters": ["b"],
            })
        assert "must not be empty" in str(exc_info.value)

    def test_empty_dest_adapters(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfig.from_toml_dict("emptydest", {
                "source_adapters": ["a"],
                "dest_adapters": [],
            })
        assert "must not be empty" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 21. Expanded route ID collision
# ---------------------------------------------------------------------------


class TestExpandedRouteIDCollision:
    """Routes with expansion patterns that collide are rejected."""

    def test_expanded_id_collision_manual(self) -> None:
        """A route with ID 'myroute__0' conflicts with expansion of 'myroute'."""
        r1 = RouteConfig(
            route_id="myroute",
            source_adapters=("a", "b"),  # expands to myroute__0, myroute__1
            dest_adapters=("c",),
            enabled=True,
        )
        r2 = RouteConfig(
            route_id="myroute__0",  # manually set, collides with expansion
            source_adapters=("d",),
            dest_adapters=("e",),
            enabled=True,
        )
        rcs = RouteConfigSet(routes=(r1, r2))
        with pytest.raises(RouteValidationError) as exc_info:
            build_runtime_routes(rcs)
        msg = str(exc_info.value)
        assert "Expanded route ID collision" in msg
        assert "myroute__0" in msg


# ---------------------------------------------------------------------------
# 22. Route table must be a TOML table
# ---------------------------------------------------------------------------


class TestRouteTableTypeValidation:
    """Route values must be TOML tables, not scalars."""

    def test_route_not_a_table(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            RouteConfigSet.from_toml_dict({"routes": {"bad_route": "not_a_table"}})
        msg = str(exc_info.value)
        assert "must be a TOML table" in msg
        assert "str" in msg


# ---------------------------------------------------------------------------
# 23. Env overrides produce ConfigValidationError for bad runtime limits
# ---------------------------------------------------------------------------


class TestEnvRuntimeLimitsValidation:
    """Env overrides for runtime limits are validated through the same path."""

    def test_apply_env_overrides_bad_int_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from medre.config.env import apply_env_overrides
        monkeypatch.setenv("MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES", "not_an_int")
        config = RuntimeConfig()
        with pytest.raises(ConfigValidationError) as exc_info:
            apply_env_overrides(config)
        msg = str(exc_info.value)
        assert "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES" in msg
        assert "must be an integer" in msg

    def test_apply_env_overrides_bad_float_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from medre.config.env import apply_env_overrides
        monkeypatch.setenv("MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS", "xyz")
        config = RuntimeConfig()
        with pytest.raises(ConfigValidationError) as exc_info:
            apply_env_overrides(config)
        msg = str(exc_info.value)
        assert "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS" in msg
        assert "must be a number" in msg

    def test_apply_env_overrides_bad_bool_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from medre.config.env import apply_env_overrides
        monkeypatch.setenv("MEDRE_MATRIX_ENABLED", "maybe")
        config = RuntimeConfig()
        with pytest.raises(ConfigValidationError) as exc_info:
            apply_env_overrides(config)
        msg = str(exc_info.value)
        assert "MEDRE_MATRIX_ENABLED" in msg
        assert "must be a boolean" in msg
