"""Tests for env-created adapters and routes in the MEDRE config system.

Ensures that MEDRE_ADAPTER__<TOKEN>__<FIELD> and
MEDRE_ROUTE__<TOKEN>__<FIELD> env var patterns correctly create
and override adapters and routes.

Split from test_config_env.py to stay under the 1 500-line limit.
"""

from __future__ import annotations

import os

import pytest

from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.env import (
    MedreEnvConfig,
    apply_env_overrides,
    apply_instance_env_overrides,
)
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.runtime.routes import (
    BridgePolicy,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
    RouteRetryConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_base_config() -> RuntimeConfig:
    """Create a minimal RuntimeConfig for env override tests."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
    )


def _make_config_with_matrix_and_meshtastic() -> RuntimeConfig:
    """RuntimeConfig with one Matrix and one Meshtastic adapter."""
    matrix_cfg = MatrixConfig(
        adapter_id="matrix-main",
        homeserver="https://matrix.toml",
        user_id="@bot:toml",
        access_token="mat-token",
        encryption_mode="plaintext",
    )
    meshtastic_cfg = MeshtasticConfig(
        adapter_id="radio-a",
        connection_type="tcp",
        host="192.168.1.100",
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            matrix={
                "matrix-main": MatrixRuntimeConfig(
                    adapter_id="matrix-main",
                    config=matrix_cfg,
                ),
            },
            meshtastic={
                "radio-a": MeshtasticRuntimeConfig(
                    adapter_id="radio-a",
                    config=meshtastic_cfg,
                ),
            },
        ),
    )


def _make_config_with_route() -> RuntimeConfig:
    """RuntimeConfig with a single TOML-defined route."""
    route = RouteConfig(
        route_id="toml-route",
        source_adapters=("adapter-a",),
        dest_adapters=("adapter-b",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route,)),
    )


# ---------------------------------------------------------------------------
# Env-first adapter creation
# ---------------------------------------------------------------------------


class TestEnvCreatedAdapters:
    """Env tokens with TRANSPORT field create new adapters from scratch."""

    # (a) Single Matrix adapter created from env.
    def test_create_matrix_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__TRANSPORT", "matrix")
        monkeypatch.setenv(
            "MEDRE_ADAPTER__MATRIX_MAIN__HOMESERVER", "https://matrix.env"
        )
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__USER_ID", "@bot:env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__ACCESS_TOKEN", "env-token")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-main" in result.adapters.matrix
        adapter = result.adapters.matrix["matrix-main"]
        assert adapter.adapter_id == "matrix-main"
        assert adapter.enabled is True
        assert adapter.config.homeserver == "https://matrix.env"
        assert adapter.config.user_id == "@bot:env"
        assert adapter.config.access_token == "env-token"

    # (b) Two Matrix adapters created from env.
    def test_create_two_matrix_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__HOMESERVER", "https://a.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__USER_ID", "@bot:a")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__ACCESS_TOKEN", "tok-a")

        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__HOMESERVER", "https://b.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__USER_ID", "@bot:b")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__ACCESS_TOKEN", "tok-b")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-a" in result.adapters.matrix
        assert "matrix-b" in result.adapters.matrix
        assert result.adapters.matrix["matrix-a"].config.homeserver == "https://a.env"
        assert result.adapters.matrix["matrix-b"].config.homeserver == "https://b.env"

    # (c) Meshtastic serial adapter created from env.
    def test_create_meshtastic_serial_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__CONNECTION_TYPE", "serial")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__SERIAL_PORT", "/dev/ttyUSB0")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-serial" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-serial"]
        assert adapter.adapter_id == "radio-serial"
        assert adapter.config.connection_type == "serial"
        assert adapter.config.serial_port == "/dev/ttyUSB0"

    # (d) Meshtastic TCP adapter created from env.
    def test_create_meshtastic_tcp_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__HOST", "192.168.1.100")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-tcp" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-tcp"]
        assert adapter.config.connection_type == "tcp"
        assert adapter.config.host == "192.168.1.100"

    # (e) MeshCore BLE adapter created from env.
    def test_create_meshcore_ble_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__CONNECTION_TYPE", "ble")
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__BLE_ADDRESS", "AA:BB:CC:DD:EE:FF")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "mc-ble" in result.adapters.meshcore
        adapter = result.adapters.meshcore["mc-ble"]
        assert adapter.config.connection_type == "ble"
        assert adapter.config.ble_address == "AA:BB:CC:DD:EE:FF"

    # (f) LXMF adapter created from env.
    def test_create_lxmf_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_MAIN__TRANSPORT", "lxmf")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "lxmf-main" in result.adapters.lxmf
        adapter = result.adapters.lxmf["lxmf-main"]
        assert adapter.adapter_id == "lxmf-main"
        assert adapter.config.connection_type == "fake"

    # (i) Two MeshCore adapters created from env (tbeam BLE + lab TCP).
    def test_create_two_meshcore_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two MeshCore adapters created from env vars independently."""
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_TBEAM__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_TBEAM__CONNECTION_TYPE", "ble")
        monkeypatch.setenv(
            "MEDRE_ADAPTER__MESHCORE_TBEAM__BLE_ADDRESS", "C4:4F:33:6A:B0:23"
        )

        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__HOST", "192.168.1.50")
        monkeypatch.setenv("MEDRE_ADAPTER__MESHCORE_LAB__PORT", "4403")

        base = _make_base_config()
        result = apply_env_overrides(base)

        # Both adapters exist with correct IDs
        assert "meshcore-tbeam" in result.adapters.meshcore
        assert "meshcore-lab" in result.adapters.meshcore

        tbeam = result.adapters.meshcore["meshcore-tbeam"]
        assert tbeam.adapter_id == "meshcore-tbeam"
        assert tbeam.enabled is True
        assert tbeam.config.connection_type == "ble"
        assert tbeam.config.ble_address == "C4:4F:33:6A:B0:23"

        lab = result.adapters.meshcore["meshcore-lab"]
        assert lab.adapter_id == "meshcore-lab"
        assert lab.config.connection_type == "tcp"
        assert lab.config.host == "192.168.1.50"
        assert lab.config.port == 4403

        # No cross-contamination
        assert getattr(tbeam.config, "host", None) is None

    # (j) Two LXMF adapters created from env (sender + receiver).
    def test_create_two_lxmf_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two LXMF adapters created from env vars independently."""
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME", "sender")

        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__TRANSPORT", "lxmf")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__CONNECTION_TYPE", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__DISPLAY_NAME", "receiver")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "lxmf-sender" in result.adapters.lxmf
        assert "lxmf-receiver" in result.adapters.lxmf

        sender = result.adapters.lxmf["lxmf-sender"]
        assert sender.adapter_id == "lxmf-sender"
        assert sender.config.connection_type == "fake"
        assert sender.config.display_name == "sender"

        receiver = result.adapters.lxmf["lxmf-receiver"]
        assert receiver.adapter_id == "lxmf-receiver"
        assert receiver.config.connection_type == "fake"
        assert receiver.config.display_name == "receiver"

        # No cross-contamination
        assert sender.config.display_name == "sender"
        assert receiver.config.display_name == "receiver"

    # (g) Unknown token without TRANSPORT raises with new error format.
    def test_unknown_token_without_transport_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__ORPHAN__HOMESERVER", "https://nope.test")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="Unknown adapter token"):
            apply_env_overrides(base)

    # (h) Invalid TRANSPORT value raises with supported transports.
    def test_invalid_transport_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__BAD__TRANSPORT", "carrier-pigeon")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD__HOMESERVER", "https://x.test")

        base = _make_base_config()
        with pytest.raises(
            ConfigValidationError, match="Invalid TRANSPORT"
        ) as exc_info:
            apply_env_overrides(base)
        msg = str(exc_info.value)
        assert "carrier-pigeon" in msg
        assert "matrix" in msg

    # (i) ENABLED=false works for created adapter.
    def test_created_adapter_enabled_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__TRANSPORT", "matrix")
        monkeypatch.setenv(
            "MEDRE_ADAPTER__MATRIX_OFF__HOMESERVER", "https://matrix.env"
        )
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__USER_ID", "@bot:off")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__ACCESS_TOKEN", "tok")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__ENABLED", "false")

        base = _make_base_config()
        result = apply_env_overrides(base)

        adapter = result.adapters.matrix["matrix-off"]
        assert adapter.enabled is False

    # (j) Explicit ADAPTER_ID works for new env adapter.
    def test_created_adapter_explicit_adapter_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__ADAPTER_ID", "my-custom-id")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__HOMESERVER", "https://custom.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__USER_ID", "@bot:custom")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "my-custom-id" in result.adapters.matrix
        assert result.adapters.matrix["my-custom-id"].adapter_id == "my-custom-id"

    # (k) Two env-created adapters resolving to same adapter_id raises.
    def test_created_adapter_id_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__ADAPTER_ID", "shared")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__HOMESERVER", "https://a.env")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__USER_ID", "@bot:a")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__ACCESS_TOKEN", "tok-a")

        monkeypatch.setenv("MEDRE_ADAPTER__BETA__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__BETA__ADAPTER_ID", "shared")
        monkeypatch.setenv("MEDRE_ADAPTER__BETA__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match=r"collision.*SHARED"):
            apply_env_overrides(base)

    # (l) Provenance redacts ACCESS_TOKEN for created adapter.
    def test_created_adapter_secrets_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__TRANSPORT", "matrix")
        monkeypatch.setenv(
            "MEDRE_ADAPTER__MATRIX_SEC__HOMESERVER", "https://matrix.env"
        )
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__USER_ID", "@bot:sec")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__ACCESS_TOKEN", "super-secret")

        env = MedreEnvConfig.from_environ()
        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_ADAPTER__MATRIX_SEC__ACCESS_TOKEN"] == "***REDACTED***"
        assert redacted["MEDRE_ADAPTER__MATRIX_SEC__HOMESERVER"] == "https://matrix.env"

    # (m) TOML radio-a + env-created radio_a collision raises.
    def test_toml_and_env_created_normalized_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TOML adapter_id='radio-a' and env-created adapter_id='radio_a'
        both normalize to RADIO_A — must raise."""
        base = _make_config_with_matrix_and_meshtastic()

        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__ADAPTER_ID", "radio_a")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__CONNECTION_TYPE", "fake")

        with pytest.raises(ConfigValidationError, match="RADIO_A"):
            apply_env_overrides(base)

    # (n) Two env-created adapters with normalizing adapter_ids collision raises.
    def test_env_created_normalized_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two env-created adapters whose adapter_ids normalize to same token."""
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")

        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__ADAPTER_ID", "radio_a")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="RADIO_A"):
            apply_env_overrides(base)

    # (o) Env-created Matrix adapter with ADAPTER_KIND=fake.
    def test_created_matrix_adapter_kind_fake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER", "https://fake.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__USER_ID", "@bot:fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-fake" in result.adapters.matrix
        adapter = result.adapters.matrix["matrix-fake"]
        assert adapter.adapter_kind == "fake"

    # (p) Env-created Meshtastic adapter with ADAPTER_KIND=fake.
    def test_created_meshtastic_adapter_kind_fake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-fake" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-fake"]
        assert adapter.adapter_kind == "fake"

    # (q) Invalid ADAPTER_KIND raises.
    def test_invalid_adapter_kind_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__ADAPTER_KIND", "invalid")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__HOMESERVER", "https://bad.env")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__USER_ID", "@bot:bad")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="ADAPTER_KIND"):
            apply_env_overrides(base)

    # (r) Default adapter_id becomes map key on created adapter.
    def test_default_adapter_id_is_map_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__HOST", "10.0.0.1")

        base = _make_base_config()
        result = apply_env_overrides(base)

        # Default adapter_id from token MY_RADIO → "my-radio"
        assert "my-radio" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["my-radio"]
        assert adapter.adapter_id == "my-radio"

    # (s) Explicit ADAPTER_ID becomes map key on created adapter.
    def test_explicit_adapter_id_is_map_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__ADAPTER_ID", "custom-radio")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__HOST", "10.0.0.1")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "custom-radio" in result.adapters.meshtastic
        assert "radio-x" not in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["custom-radio"]
        assert adapter.adapter_id == "custom-radio"

    # (t) Adapter-only env vars do not affect routes.
    def test_adapter_env_vars_do_not_affect_routes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter-only env vars do not modify routes."""
        base = _make_config_with_matrix_and_meshtastic()
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__HOST", "10.0.0.99")

        result = apply_env_overrides(base)

        # Routes are unchanged — only MEDRE_ROUTE__ vars affect routes.
        assert result.routes == base.routes

    # (u) Unsupported field validation for env-created adapters.
    def test_env_created_unsupported_field_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unsupported fields on env-created adapters raise ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_BAD__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_BAD__UNSUPPORTED_FIELD", "x")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError) as excinfo:
            apply_env_overrides(base)
        msg = str(excinfo.value)
        assert "unsupported_field" in msg

    # (v) Dict/tuple field rejection for env-created adapters.
    def test_env_created_adapter_rejects_dict_tuple_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dict/tuple fields rejected on env-created adapters."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__HOMESERVER", "https://env.test")
        # auto_join_rooms is a tuple field — should be rejected
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__auto_join_rooms", "!room:test")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="cannot be set through env"):
            apply_env_overrides(base)


# ---------------------------------------------------------------------------
# Route env creation
# ---------------------------------------------------------------------------


class TestRouteEnvCreation:
    """MEDRE_ROUTE__<TOKEN>__<FIELD> creates routes from env vars."""

    # (a) Create a basic route via env.
    def test_create_route_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "adapter-a")
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__DEST_ADAPTERS", "adapter-b")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.route_id == "my-route"
        assert route.source_adapters == ("adapter-a",)
        assert route.dest_adapters == ("adapter-b",)
        assert route.directionality == RouteDirectionality.SOURCE_TO_DEST
        assert route.enabled is True

    # (b) Override existing TOML route: ENABLED=false preserves other fields.
    def test_override_existing_toml_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")

        base = _make_config_with_route()
        result = apply_env_overrides(base)

        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.route_id == "toml-route"
        assert route.enabled is False
        # Other fields preserved from TOML.
        assert route.source_adapters == ("adapter-a",)
        assert route.dest_adapters == ("adapter-b",)
        assert route.directionality == RouteDirectionality.SOURCE_TO_DEST

    # (c) Comma-separated SOURCE_ADAPTERS parsed into a tuple.
    def test_route_comma_list_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__MULTI__SOURCE_ADAPTERS", "a,b,c")
        monkeypatch.setenv("MEDRE_ROUTE__MULTI__DEST_ADAPTERS", "d")

        base = _make_base_config()
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.source_adapters == ("a", "b", "c")
        assert len(route.source_adapters) == 3

    # (d) Missing SOURCE_ADAPTERS or DEST_ADAPTERS raises.
    def test_route_creation_requires_source_and_dest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only source, no dest.
        monkeypatch.setenv("MEDRE_ROUTE__INCOMPLETE__SOURCE_ADAPTERS", "a")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="dest_adapters"):
            apply_env_overrides(base)

    def test_route_creation_requires_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only dest, no source.
        monkeypatch.setenv("MEDRE_ROUTE__INCOMPLETE__DEST_ADAPTERS", "b")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="source_adapters"):
            apply_env_overrides(base)

    # (e) Invalid directionality raises.
    def test_route_invalid_directionality(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__BAD_DIR__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__BAD_DIR__DEST_ADAPTERS", "b")
        monkeypatch.setenv("MEDRE_ROUTE__BAD_DIR__DIRECTIONALITY", "sideways")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="directionality"):
            apply_env_overrides(base)

    # (f) Unknown field on a route raises.
    def test_route_unsupported_field_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__BAD__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__BAD__DEST_ADAPTERS", "b")
        monkeypatch.setenv("MEDRE_ROUTE__BAD__TOTALLY_FAKE", "nope")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="Unsupported route field"):
            apply_env_overrides(base)

    # (g) Explicit ROUTE_ID overrides the default.
    def test_route_explicit_route_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__X__ROUTE_ID", "custom-id")
        monkeypatch.setenv("MEDRE_ROUTE__X__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__X__DEST_ADAPTERS", "b")

        base = _make_base_config()
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.route_id == "custom-id"

    # (h) Create both an adapter and a route referencing it from env.
    def test_route_and_adapter_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create a Meshtastic adapter from env.
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
        # Create a route referencing it.
        monkeypatch.setenv("MEDRE_ROUTE__BRIDGE__SOURCE_ADAPTERS", "radio-a")
        monkeypatch.setenv("MEDRE_ROUTE__BRIDGE__DEST_ADAPTERS", "radio-b")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-a" in result.adapters.meshtastic
        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.source_adapters == ("radio-a",)
        assert route.dest_adapters == ("radio-b",)

    # (i) Hyphenated route token is rejected.
    def test_hyphenated_route_token_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hyphenated route token raises Invalid route token error."""
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__MY-ROUTE__DEST_ADAPTERS", "b")
        with pytest.raises(ConfigValidationError, match="Invalid route token"):
            MedreEnvConfig.from_environ()

    # (j) Basic valid route token with underscores works.
    def test_valid_route_token_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Basic valid route token with underscores works."""
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "adapter-a")
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__DEST_ADAPTERS", "adapter-b")
        base = _make_base_config()
        result = apply_env_overrides(base)
        assert len(result.routes.routes) == 1
        assert result.routes.routes[0].route_id == "my-route"

    # (k) Hyphen in route token raises ConfigValidationError.
    def test_hyphenated_route_token_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hyphen in route token raises ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ROUTE__MY-ROUTE__SOURCE_ADAPTERS", "a")
        with pytest.raises(ConfigValidationError, match="Invalid route token"):
            MedreEnvConfig.from_environ()

    # (l) Dot in route token raises ConfigValidationError.
    def test_dotted_route_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dot in route token raises ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ROUTE__MY.ROUTE__SOURCE_ADAPTERS", "a")
        with pytest.raises(ConfigValidationError, match="Invalid route token"):
            MedreEnvConfig.from_environ()

    # (m) Space in route token raises ConfigValidationError.
    def test_spaced_route_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Space in route token raises ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ROUTE__MY ROUTE__SOURCE_ADAPTERS", "a")
        with pytest.raises(ConfigValidationError, match="Invalid route token"):
            MedreEnvConfig.from_environ()

    # (n) MEDRE_ROUTE__ with nothing after prefix raises.
    def test_malformed_route_var_empty_after_prefix(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_ROUTE__"):
            MedreEnvConfig.from_environ({"MEDRE_ROUTE__": "v"})

    # (o) MEDRE_ROUTE__MAIN with no field raises.
    def test_malformed_route_var_no_field(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_ROUTE__"):
            MedreEnvConfig.from_environ({"MEDRE_ROUTE__MAIN": "v"})

    # (p) MEDRE_ROUTE____SOURCE_ADAPTERS with empty token raises.
    def test_malformed_route_var_empty_token(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_ROUTE__"):
            MedreEnvConfig.from_environ({"MEDRE_ROUTE____SOURCE_ADAPTERS": "v"})

    # (q) MEDRE_ROUTE__MAIN__ with empty field raises.
    def test_malformed_route_var_empty_field(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_ROUTE__"):
            MedreEnvConfig.from_environ({"MEDRE_ROUTE__MAIN__": "v"})

    # (r) MEDRE_ROUTE__MAIN__SOURCE__EXTRA raises (too many parts).
    def test_malformed_route_var_too_many_separators(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_ROUTE__"):
            MedreEnvConfig.from_environ({"MEDRE_ROUTE__MAIN__SOURCE__EXTRA": "v"})

    # (s) UPPER, lower, and Mixed CASE fields map to same internal field names.
    def test_route_case_insensitive_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS", "radio-a")
        monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__dest_adapters", "matrix-main")
        monkeypatch.setenv(
            "MEDRE_ROUTE__RADIO_TO_MATRIX__Directionality", "source_to_dest"
        )
        base = _make_base_config()
        result = apply_env_overrides(base)
        route = result.routes.routes[0]
        assert route.source_adapters == ("radio-a",)
        assert route.dest_adapters == ("matrix-main",)
        assert route.directionality == RouteDirectionality.SOURCE_TO_DEST

    # (t) Same token+field from different casing raises duplicate error.
    def test_route_duplicate_normalized_field_different_casing(self) -> None:
        with pytest.raises(ConfigValidationError, match="Duplicate normalized route"):
            MedreEnvConfig.from_environ(
                {
                    "MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS": "a",
                    "MEDRE_ROUTE__MY_ROUTE__source_adapters": "b",
                }
            )

    # (u) Two env-created routes with same explicit route_id raise.
    def test_route_id_collision_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_A__ROUTE_ID", "shared")
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_A__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_A__DEST_ADAPTERS", "b")
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_B__ROUTE_ID", "shared")
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_B__SOURCE_ADAPTERS", "c")
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_B__DEST_ADAPTERS", "d")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="Duplicate route ID"):
            apply_env_overrides(base)

    # (v) Route provenance records target_route_token (not adapter_token).
    def test_route_provenance_includes_route_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route provenance records target_route_token (not adapter_token)."""
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "a")
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__DEST_ADAPTERS", "b")
        env = MedreEnvConfig.from_environ()
        route_entries = [e for e in env.provenance.entries if e.source_kind == "route"]
        assert len(route_entries) == 2
        for entry in route_entries:
            assert entry.target_route_token == "MY_ROUTE"
            # Route entries should NOT set target_adapter_token
            assert entry.target_adapter_token is None

    # --- Override-mode validation tests ---
    # These tests override an existing TOML route and verify that
    # RouteConfig.from_toml_dict validation is applied.

    # (w) Empty source_adapters in override mode raises.
    def test_route_override_empty_source_adapters_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty source_adapters in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__SOURCE_ADAPTERS", "")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="must not be empty"):
            apply_env_overrides(base)

    # (x) Empty dest_adapters in override mode raises.
    def test_route_override_empty_dest_adapters_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty dest_adapters in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__DEST_ADAPTERS", "")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="must not be empty"):
            apply_env_overrides(base)

    # (y) Source room/channel alias conflict in override mode raises.
    def test_route_override_conflicting_source_room_channel_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source room/channel alias conflict in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__SOURCE_CHANNEL", "1")
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__SOURCE_ROOM", "2")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="source_room"):
            apply_env_overrides(base)

    # (z) Dest room/channel alias conflict in override mode raises.
    def test_route_override_conflicting_dest_room_channel_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dest room/channel alias conflict in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__DEST_CHANNEL", "chan-a")
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__DEST_ROOM", "room-b")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="dest_room"):
            apply_env_overrides(base)

    # (aa) Source/dest overlap in override mode raises.
    def test_route_override_source_dest_overlap_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source/dest overlap in override mode raises."""
        monkeypatch.setenv(
            "MEDRE_ROUTE__TOML_ROUTE__SOURCE_ADAPTERS", "adapter-a,adapter-b"
        )
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="overlap"):
            apply_env_overrides(base)

    # (ab) Duplicate source adapters in override mode raises.
    def test_route_override_duplicate_source_adapters_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate source adapters in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__SOURCE_ADAPTERS", "a,a")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="duplicate"):
            apply_env_overrides(base)

    # (ac) Duplicate dest adapters in override mode raises.
    def test_route_override_duplicate_dest_adapters_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate dest adapters in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__DEST_ADAPTERS", "b,b")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="duplicate"):
            apply_env_overrides(base)

    # (ad) Invalid directionality in override mode raises.
    def test_route_override_invalid_directionality_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid directionality in override mode raises."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__DIRECTIONALITY", "sideways")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="directionality"):
            apply_env_overrides(base)

    # (ae) route_id cannot be changed for existing TOML route.
    def test_route_override_route_id_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """route_id override on existing TOML route raises ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ROUTE_ID", "renamed")
        base = _make_config_with_route()
        with pytest.raises(ConfigValidationError, match="route_id"):
            apply_env_overrides(base)


# ---------------------------------------------------------------------------
# Area 1: Provenance back-fill of target_transport for newly created adapters
# ---------------------------------------------------------------------------


class TestProvenanceTargetTransportBackfill:
    """When apply_instance_env_overrides creates a brand-new adapter via env
    vars (token with TRANSPORT field), provenance entries get target_transport
    back-filled (lines 1041-1045 of env.py)."""

    def test_new_adapter_provenance_has_target_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance entries for a newly created adapter get target_transport."""
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__CONNECTION_TYPE", "fake")

        # Parse env — this records provenance entries with target_transport=None.
        env = MedreEnvConfig.from_environ()
        base = _make_base_config()

        # Apply instance overrides directly, passing our provenance so the
        # back-fill at lines 1041-1045 mutates entries we can inspect.
        result = apply_instance_env_overrides(
            base, env.instance_overrides, provenance=env.provenance
        )

        # Verify the adapter was created.
        assert "my-radio" in result.adapters.meshtastic

        # Instance entries for the MY_RADIO token should have target_transport
        # back-filled to "meshtastic".
        entries = [
            e
            for e in env.provenance.entries
            if e.source_kind == "instance" and e.target_adapter_token == "MY_RADIO"
        ]
        assert len(entries) >= 1, (
            f"Expected at least 1 instance provenance entry for MY_RADIO, "
            f"got {[e.env_var_name for e in env.provenance.entries]}"
        )
        for entry in entries:
            assert entry.target_transport == "meshtastic", (
                f"Entry {entry.env_var_name!r} has target_transport="
                f"{entry.target_transport!r}, expected 'meshtastic'"
            )

    def test_new_matrix_adapter_provenance_has_target_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance entries for a newly created Matrix adapter get target_transport."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_NEW__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_NEW__HOMESERVER", "https://env.test")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_NEW__USER_ID", "@bot:new")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_NEW__ACCESS_TOKEN", "tok")

        env = MedreEnvConfig.from_environ()
        base = _make_base_config()
        result = apply_instance_env_overrides(
            base, env.instance_overrides, provenance=env.provenance
        )

        assert "matrix-new" in result.adapters.matrix

        entries = [
            e
            for e in env.provenance.entries
            if e.source_kind == "instance" and e.target_adapter_token == "MATRIX_NEW"
        ]
        assert len(entries) >= 1
        for entry in entries:
            assert entry.target_transport == "matrix"


# ---------------------------------------------------------------------------
# Area 2: Preserving complex fields when overriding an existing route
# ---------------------------------------------------------------------------


def _make_config_with_route_complex() -> RuntimeConfig:
    """RuntimeConfig with a route that has channel_room_map, policy, and retry."""
    route = RouteConfig(
        route_id="toml-route",
        source_adapters=("adapter-a",),
        dest_adapters=("adapter-b",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
        channel_room_map={"0": "!room1:matrix.org"},
        policy=BridgePolicy(allowed_event_types=("message",)),
        retry=RouteRetryConfig(enabled=True, max_attempts=5),
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route,)),
    )


class TestRouteOverridePreservesComplexFields:
    """When overriding an existing route via env, complex fields (channel_room_map,
    policy, retry) that cannot be set via env vars are preserved (lines 1102-1107)."""

    def test_channel_room_map_preserved_on_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """channel_room_map is preserved when overriding a route via env."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")
        base = _make_config_with_route_complex()
        result = apply_env_overrides(base)

        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.enabled is False
        assert route.channel_room_map == {"0": "!room1:matrix.org"}

    def test_policy_preserved_on_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """policy is preserved when overriding a route via env."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")
        base = _make_config_with_route_complex()
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.policy is not None
        assert route.policy.allowed_event_types == ("message",)

    def test_retry_preserved_on_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """retry is preserved when overriding a route via env."""
        monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")
        base = _make_config_with_route_complex()
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.retry is not None
        assert route.retry.enabled is True
        assert route.retry.max_attempts == 5


# ---------------------------------------------------------------------------
# Area 3: Normalized-token collision detection for routes
# ---------------------------------------------------------------------------


def _make_config_with_named_route(route_id: str) -> RuntimeConfig:
    """RuntimeConfig with a single TOML route using the given route_id."""
    route = RouteConfig(
        route_id=route_id,
        source_adapters=("sa",),
        dest_adapters=("da",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route,)),
    )


def _make_config_with_colliding_routes() -> RuntimeConfig:
    """RuntimeConfig with two routes whose IDs normalize to the same token."""
    route_a = RouteConfig(
        route_id="route-a",
        source_adapters=("sa",),
        dest_adapters=("da",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
    )
    route_b = RouteConfig(
        route_id="route_a",
        source_adapters=("sb",),
        dest_adapters=("db",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route_a, route_b)),
    )


class TestRouteNormalizedTokenCollision:
    """Two route IDs that normalize to the same token raise ConfigValidationError
    (lines 1249-1254 of env.py)."""

    def test_colliding_route_ids_in_toml_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two TOML routes whose IDs normalize to the same token raise."""
        # Set a benign route env var so apply_route_overrides is actually
        # invoked (it returns early when route_overrides is empty).
        monkeypatch.setenv("MEDRE_ROUTE__ROUTE_A__ENABLED", "true")
        base = _make_config_with_colliding_routes()
        with pytest.raises(ConfigValidationError, match="normali"):
            apply_env_overrides(base)

    def test_env_route_collides_with_toml_route_normalized_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env-created route with route_id that normalizes to same token as
        a TOML route raises ConfigValidationError (not duplicate-ID check)."""
        # TOML route with id "route-a" (normalizes to ROUTE_A)
        base = _make_config_with_named_route("route-a")

        # Env route with explicit route_id "route_a" — different string,
        # same normalized token (ROUTE_A).  Should hit normalized-token collision.
        monkeypatch.setenv("MEDRE_ROUTE__NEW__ROUTE_ID", "route_a")
        monkeypatch.setenv("MEDRE_ROUTE__NEW__SOURCE_ADAPTERS", "c")
        monkeypatch.setenv("MEDRE_ROUTE__NEW__DEST_ADAPTERS", "d")

        with pytest.raises(ConfigValidationError, match="normali"):
            apply_env_overrides(base)

    # (af) Route referencing unknown adapter IDs passes config but would fail build.
    def test_env_route_unknown_adapter_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route referencing nonexistent adapter passes config parsing.

        Env parsing does not validate adapter refs — that happens at
        RuntimeBuilder.build() / route_engine.register_routes() time.
        """
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "nonexistent")
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__DEST_ADAPTERS", "ghost")
        base = _make_base_config()
        result = apply_env_overrides(base)
        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.source_adapters == ("nonexistent",)
        assert route.dest_adapters == ("ghost",)

    # (ag) Route using env token format instead of adapter_id format is accepted.
    def test_env_route_token_instead_of_adapter_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route using env token (UPPERCASE) instead of adapter_id is accepted by parser.

        The env parser treats source_adapters as opaque strings.  Validation
        that no adapter has ID "MY_ADAPTER_TOKEN" happens at build time.
        """
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__SOURCE_ADAPTERS", "MY_ADAPTER_TOKEN")
        monkeypatch.setenv("MEDRE_ROUTE__MY_ROUTE__DEST_ADAPTERS", "adapter-b")
        base = _make_base_config()
        result = apply_env_overrides(base)
        assert result.routes.routes[0].source_adapters == ("MY_ADAPTER_TOKEN",)
