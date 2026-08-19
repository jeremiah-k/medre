"""LXMF SDK parity regression tests against LXMF 1.1.1 / RNS 1.4.2."""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from medre.adapters.lxmf.errors import LxmfSendError
from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig, LxmfConfigError


def _real_config(tmp_path: Path, **overrides: Any) -> LxmfConfig:
    values: dict[str, Any] = {
        "adapter_id": "lxmf-sdk-parity",
        "connection_type": "reticulum",
        "storage_path": str(tmp_path / "lxmf-router"),
        "announce_interval_seconds": 0,
    }
    values.update(overrides)
    return LxmfConfig(**values)


def _sdk_environment() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    destination = MagicMock()
    destination.hash = b"\xaa" * 16
    router = MagicMock()
    router.register_delivery_identity.return_value = destination

    rns = MagicMock()
    rns.Reticulum.get_instance.return_value = None
    rns.Reticulum.return_value = MagicMock()
    rns.Identity.return_value = MagicMock()

    lxmf = MagicMock()
    lxmf.LXMRouter.return_value = router
    return rns, lxmf, router, destination


def test_outbound_propagation_node_accepts_exact_destination_hash(
    tmp_path: Path,
) -> None:
    """The configured node is the 16-byte destination hash LXMF expects."""
    node = "ab" * 16
    validated = _real_config(tmp_path, outbound_propagation_node=node).validate()
    assert validated.outbound_propagation_node == node


def test_outbound_propagation_node_rejects_wrong_length(tmp_path: Path) -> None:
    """Reject propagation hashes that are not 16 bytes / 32 hex characters."""
    with pytest.raises(LxmfConfigError, match="16-byte"):
        _real_config(tmp_path, outbound_propagation_node="ab" * 15).validate()


def test_outbound_propagation_node_rejects_non_hex(tmp_path: Path) -> None:
    """Reject a correctly sized value containing non-hexadecimal characters."""
    with pytest.raises(LxmfConfigError, match="hexadecimal"):
        _real_config(tmp_path, outbound_propagation_node="z" * 32).validate()


def test_outbound_propagation_node_rejects_non_string(tmp_path: Path) -> None:
    """Reject non-string propagation-node identifiers before length checks."""
    with pytest.raises(LxmfConfigError, match="hex string or None"):
        _real_config(tmp_path, outbound_propagation_node=123).validate()  # type: ignore[arg-type]


def test_outbound_propagation_node_rejects_whitespace(tmp_path: Path) -> None:
    """Config validation matches the schema's exact hexadecimal shape."""
    with pytest.raises(LxmfConfigError, match="hexadecimal"):
        _real_config(tmp_path, outbound_propagation_node=("ab" * 15) + "  ").validate()


def test_real_propagated_default_requires_outbound_node(tmp_path: Path) -> None:
    """A real session cannot default to propagated delivery without a node."""
    with pytest.raises(LxmfConfigError, match="outbound_propagation_node"):
        _real_config(tmp_path, default_delivery_method="propagated").validate()


async def test_connect_configures_outbound_propagation_node(tmp_path: Path) -> None:
    """Real startup selects the configured LXMF propagation node exactly once."""
    node = "12" * 16
    config = _real_config(tmp_path, outbound_propagation_node=node)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()

    previous_int = MagicMock(name="previous_sigint")
    previous_term = MagicMock(name="previous_sigterm")
    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(rns, lxmf),
        ),
        patch(
            "medre.adapters.lxmf.session.signal.getsignal",
            side_effect=[previous_int, previous_term],
        ),
        patch("medre.adapters.lxmf.session.signal.signal") as set_signal,
    ):
        await session.start()
        router.set_outbound_propagation_node.assert_called_once_with(
            bytes.fromhex(node)
        )
        assert set_signal.call_args_list == [
            call(signal.SIGINT, previous_int),
            call(signal.SIGTERM, previous_term),
        ]
        await session.stop()


async def test_propagated_send_without_node_fails_before_handoff(
    tmp_path: Path,
) -> None:
    """An explicit propagated send fails clearly when no node is selected."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()
    router.get_outbound_propagation_node.return_value = None

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(rns, lxmf),
        ),
    ):
        await session.start()
        with pytest.raises(LxmfSendError, match="outbound_propagation_node"):
            await session.send_text(
                "34" * 16,
                "propagated without node",
                delivery_method="propagated",
            )
        router.handle_outbound.assert_not_called()
        await session.stop()


async def test_propagated_override_is_case_insensitive(tmp_path: Path) -> None:
    """Uppercase propagated overrides still enforce propagation-node setup."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()
    router.get_outbound_propagation_node.return_value = None

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(rns, lxmf),
        ),
    ):
        await session.start()
        with pytest.raises(LxmfSendError, match="outbound_propagation_node"):
            await session.send_text(
                "34" * 16,
                "uppercase propagated without node",
                delivery_method="PROPAGATED",
            )
        router.handle_outbound.assert_not_called()
        await session.stop()


def test_propagation_diagnostic_reflects_boolean_router_state() -> None:
    """False local propagation-server state must not be reported as enabled."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-diag")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    session._router = MagicMock(propagation_node=False)
    session._refresh_safe_diagnostics()
    assert session.diagnostics().propagation_enabled is False

    session._router.propagation_node = True
    session._refresh_safe_diagnostics()
    assert session.diagnostics().propagation_enabled is True


async def test_persistent_identity_can_restart_with_same_transport_registry(
    tmp_path: Path,
) -> None:
    """stop() releases router-owned RNS registrations before the next start."""

    class FakeDestination:
        def __init__(self, destination_hash: bytes) -> None:
            self.hash = destination_hash

    class FakeTransport:
        destinations_map: dict[bytes, FakeDestination] = {}
        announce_handlers: list[Any] = []

        @classmethod
        def register_destination(cls, destination: FakeDestination) -> None:
            if destination.hash in cls.destinations_map:
                raise KeyError("Attempt to register an already registered destination")
            cls.destinations_map[destination.hash] = destination

        @classmethod
        def deregister_destination(cls, destination: FakeDestination) -> None:
            cls.destinations_map.pop(destination.hash, None)

        @classmethod
        def deregister_announce_handler(cls, handler: Any) -> None:
            if handler in cls.announce_handlers:
                cls.announce_handlers.remove(handler)

    class FakeRouter:
        def __init__(self, identity: Any, storagepath: str) -> None:
            del storagepath
            self.identity = identity
            self.propagation_destination = FakeDestination(b"p" * 16)
            self.control_destination = None
            self.delivery_destinations: dict[bytes, FakeDestination] = {}
            FakeTransport.register_destination(self.propagation_destination)
            FakeTransport.announce_handlers.extend(
                [
                    SimpleNamespace(lxmrouter=self),
                    SimpleNamespace(lxmrouter=self),
                ]
            )

        def register_delivery_callback(self, _callback: Any) -> None:
            return None

        def register_delivery_identity(
            self,
            _identity: Any,
            *,
            display_name: str | None,
            stamp_cost: int | None,
        ) -> FakeDestination:
            del display_name, stamp_cost
            destination = FakeDestination(b"d" * 16)
            FakeTransport.register_destination(destination)
            self.delivery_destinations[destination.hash] = destination
            return destination

        def exit_handler(self) -> None:
            return None

    persistent_identity = MagicMock(name="persistent_identity")
    reticulum_instance = MagicMock(name="reticulum_instance")
    fake_rns = SimpleNamespace(
        Transport=FakeTransport,
        Reticulum=SimpleNamespace(
            get_instance=lambda: reticulum_instance,
        ),
        Identity=SimpleNamespace(
            from_file=lambda _path: persistent_identity,
        ),
    )
    fake_lxmf = SimpleNamespace(LXMRouter=FakeRouter)
    identity_path = tmp_path / "identity"
    identity_path.write_bytes(b"persistent")
    config = _real_config(tmp_path, identity_path=str(identity_path))
    session = LxmfSession(config=config, adapter_id=config.adapter_id)

    hashes: list[bytes] = []
    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(fake_rns, fake_lxmf),
        ),
    ):
        for _ in range(3):
            await session.start()
            assert session._identity is persistent_identity
            assert session._delivery_destination_hash is not None
            hashes.append(session._delivery_destination_hash)
            await session.stop()
            assert FakeTransport.destinations_map == {}
            assert FakeTransport.announce_handlers == []

    assert hashes == [b"d" * 16] * 3


def test_teardown_quiesces_router_without_stopping_reticulum() -> None:
    """Release router-owned RNS registrations but keep global Reticulum alive."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-teardown")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    router = MagicMock()
    propagation_destination = MagicMock(name="propagation_destination")
    control_destination = MagicMock(name="control_destination")
    delivery_destination = MagicMock(name="delivery_destination")
    router.propagation_destination = propagation_destination
    router.control_destination = control_destination
    router.delivery_destinations = {b"delivery": delivery_destination}

    reticulum_instance = MagicMock()
    session._router = router
    session._reticulum = reticulum_instance
    session._delivery_destination = delivery_destination
    session._delivery_destination_hash = b"\xaa" * 16

    transport = MagicMock()
    owned_handler = MagicMock(name="owned_handler")
    owned_handler.lxmrouter = router
    other_handler = MagicMock(name="other_handler")
    other_handler.lxmrouter = MagicMock(name="other_router")
    transport.announce_handlers = [owned_handler, other_handler]
    session._rns_transport = transport

    with patch("medre.adapters.lxmf.session.atexit.unregister") as unregister:
        session._teardown_sdk()

    router.exit_handler.assert_called_once_with()
    assert transport.deregister_destination.call_args_list == [
        call(propagation_destination),
        call(control_destination),
        call(delivery_destination),
    ]
    transport.deregister_announce_handler.assert_called_once_with(owned_handler)
    unregister.assert_called_once_with(router.exit_handler)
    reticulum_instance.exit_handler.assert_not_called()
    assert session._router is None
    assert session._reticulum is None
    assert session._rns_transport is None
    assert session._delivery_destination is None


def test_teardown_registry_cleanup_is_best_effort(caplog: pytest.LogCaptureFixture) -> None:
    """Registry cleanup failures are logged without blocking session teardown."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-cleanup-errors")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    router = MagicMock()
    shared_destination = MagicMock(name="shared_destination")
    delivery_destination = MagicMock(name="delivery_destination")
    router.propagation_destination = shared_destination
    router.control_destination = shared_destination
    router.delivery_destinations = {b"delivery": delivery_destination}
    session._router = router

    transport = MagicMock()
    transport.deregister_destination.side_effect = RuntimeError("destination busy")
    owned_handler = MagicMock(name="owned_handler")
    owned_handler.lxmrouter = router
    transport.announce_handlers = [owned_handler]
    transport.deregister_announce_handler.side_effect = RuntimeError(
        "handler busy"
    )
    session._rns_transport = transport

    with patch("medre.adapters.lxmf.session.atexit.unregister"):
        session._teardown_sdk()

    assert transport.deregister_destination.call_args_list == [
        call(shared_destination),
        call(delivery_destination),
    ]
    transport.deregister_announce_handler.assert_called_once_with(owned_handler)
    assert "could not deregister owned Reticulum destination" in caplog.text
    assert "could not deregister owned LXMF announce handler" in caplog.text
    assert session._router is None
    assert session._rns_transport is None


async def test_connect_restores_custom_process_signal_handlers(tmp_path: Path) -> None:
    """Embedding LXMRouter must not permanently replace application signals."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, _router, _destination = _sdk_environment()
    previous_int = MagicMock(name="custom_sigint")
    previous_term = MagicMock(name="custom_sigterm")

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(rns, lxmf),
        ),
        patch(
            "medre.adapters.lxmf.session.signal.getsignal",
            side_effect=[previous_int, previous_term],
        ),
        patch("medre.adapters.lxmf.session.signal.signal") as set_signal,
    ):
        await session.start()
        assert set_signal.call_args_list == [
            call(signal.SIGINT, previous_int),
            call(signal.SIGTERM, previous_term),
        ]
        set_signal.reset_mock()
        await session.stop()
        set_signal.assert_not_called()


def test_adapter_schema_requires_node_for_real_propagated_default() -> None:
    """The published schema mirrors LxmfConfig's propagated-delivery invariant."""
    import json

    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[1] / "docs/schemas/adapter-config.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    invalid = {
        "adapter_id": "lxmf-schema",
        "connection_type": "reticulum",
        "default_delivery_method": "propagated",
    }
    assert list(validator.iter_errors(invalid))

    valid = dict(invalid, outbound_propagation_node="ab" * 16)
    assert list(validator.iter_errors(valid)) == []

    direct = {
        "adapter_id": "lxmf-schema",
        "connection_type": "reticulum",
        "default_delivery_method": "direct",
    }
    assert list(validator.iter_errors(direct)) == []


def test_propagation_diagnostic_is_unavailable_without_router() -> None:
    """Missing router state is reported as unavailable instead of stale false."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-diag-none")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    session._diag.propagation_enabled = True
    session._router = None

    assert session.diagnostics().propagation_enabled is None


def test_propagation_diagnostic_is_unavailable_when_sdk_lookup_fails() -> None:
    """A failing SDK propagation lookup produces the documented None state."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-diag-error")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)

    class _Router:
        @property
        def propagation_node(self) -> bool:
            raise RuntimeError("unavailable")

    session._router = _Router()

    assert session.diagnostics().propagation_enabled is None


async def test_connect_survives_signal_snapshot_failure(tmp_path: Path) -> None:
    """Router construction still works when process signal state is unavailable."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch("medre.adapters.lxmf.session._require_lxmf", return_value=(rns, lxmf)),
        patch(
            "medre.adapters.lxmf.session.signal.getsignal",
            side_effect=OSError("signals unavailable"),
        ),
        patch("medre.adapters.lxmf.session.signal.signal") as set_signal,
    ):
        await session.start()
        set_signal.assert_not_called()
        assert session._router is router
        await session.stop()


async def test_connect_survives_signal_restore_failure(tmp_path: Path) -> None:
    """Failure to restore one process signal does not invalidate SDK startup."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch("medre.adapters.lxmf.session._require_lxmf", return_value=(rns, lxmf)),
        patch(
            "medre.adapters.lxmf.session.signal.getsignal",
            side_effect=[object(), object()],
        ),
        patch(
            "medre.adapters.lxmf.session.signal.signal",
            side_effect=[OSError("restore failed"), None],
        ) as set_signal,
    ):
        await session.start()
        assert set_signal.call_count == 2
        assert session._router is router
        await session.stop()


async def test_connect_rejects_delivery_registration_type_error(tmp_path: Path) -> None:
    """A broken delivery-registration API fails startup instead of half-connecting."""
    from medre.adapters.lxmf.errors import LxmfConnectionError

    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()
    router.register_delivery_identity.side_effect = TypeError("bad signature")

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch("medre.adapters.lxmf.session._require_lxmf", return_value=(rns, lxmf)),
        pytest.raises(
            LxmfConnectionError, match="Failed to register local LXMF delivery identity"
        ),
    ):
        await session.start()

    assert session.connected is False
    assert session._delivery_destination is None


async def test_propagated_send_reports_unavailable_node_lookup(tmp_path: Path) -> None:
    """An SDK without a usable propagation lookup fails before router handoff."""
    config = _real_config(tmp_path)
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    rns, lxmf, router, _ = _sdk_environment()
    router.get_outbound_propagation_node.side_effect = TypeError("missing API")

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch("medre.adapters.lxmf.session._require_lxmf", return_value=(rns, lxmf)),
    ):
        await session.start()
        with pytest.raises(
            LxmfSendError, match="propagation-node lookup is unavailable"
        ):
            await session.send_text(
                "34" * 16,
                "propagated without lookup",
                delivery_method="propagated",
            )
        router.handle_outbound.assert_not_called()
        await session.stop()


def test_teardown_continues_when_router_exit_handler_fails() -> None:
    """Best-effort router cleanup still drops all owned SDK references."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-teardown-error")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    router = MagicMock()
    router.exit_handler.side_effect = RuntimeError("cleanup failed")
    session._router = router
    session._reticulum = MagicMock()
    session._delivery_destination = MagicMock()
    session._delivery_destination_hash = b"\xaa" * 16

    with patch("medre.adapters.lxmf.session.atexit.unregister") as unregister:
        session._teardown_sdk()

    router.exit_handler.assert_called_once_with()
    unregister.assert_called_once_with(router.exit_handler)
    assert session._router is None
    assert session._reticulum is None
    assert session._delivery_destination is None


def test_teardown_tolerates_atexit_unregister_failure() -> None:
    """An unavailable atexit deregistration API does not break session teardown."""
    config = LxmfConfig(adapter_id="lxmf-sdk-parity-atexit-error")
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    router = MagicMock()
    session._router = router

    with patch(
        "medre.adapters.lxmf.session.atexit.unregister",
        side_effect=ValueError("not registered"),
    ):
        session._teardown_sdk()

    router.exit_handler.assert_called_once_with()
    assert session._router is None
