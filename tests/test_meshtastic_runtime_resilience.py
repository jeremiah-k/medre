"""Meshtastic session-level runtime resilience contracts."""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from medre.adapters.meshtastic.errors import MeshtasticConnectionError
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig
from tests.helpers.async_utils import wait_until


def _tcp_config(**overrides: object) -> MeshtasticConfig:
    values: dict[str, object] = {
        "adapter_id": "mesh-resilience",
        "connection_type": "tcp",
        "host": "127.0.0.1",
        "reconnect_backoff_initial_seconds": 1.0,
        "reconnect_backoff_max_seconds": 4.0,
        "tcp_liveness_interval_seconds": 60.0,
        "tcp_liveness_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return MeshtasticConfig(**values).validate()  # type: ignore[arg-type]


def _session(
    config: MeshtasticConfig | None = None,
    **config_overrides: object,
) -> MeshtasticSession:
    return MeshtasticSession(
        config or _tcp_config(**config_overrides),
        adapter_id="mesh-resilience",
        platform="meshtastic",
    )


def test_reconnect_delay_caps_without_large_exponent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session.random.uniform",
        lambda _a, _b: 0.0,
    )
    assert session._reconnect_delay(1) == 1.0
    assert session._reconnect_delay(2) == 2.0
    assert session._reconnect_delay(3) == 4.0
    assert session._reconnect_delay(10_000) == 4.0


def test_reconnect_delay_positive_jitter_never_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session.random.uniform",
        lambda _a, upper: upper,
    )

    assert session._reconnect_delay(10_000) == 4.0


async def test_reconnect_detaches_failed_client_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    closed: list[str] = []

    class FailedClient:
        isConnected = SimpleNamespace(is_set=lambda: True)

        def close(self) -> None:
            closed.append("failed")

    session._activate_client(FailedClient())

    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 60.0
    )

    reconnect_task = asyncio.create_task(session._reconnect_loop())
    session._reconnect_task = reconnect_task

    assert await wait_until(lambda: closed == ["failed"], timeout=1.0)
    assert session.client is None
    assert session.connected is False

    await session.stop(timeout=0.2)

    assert reconnect_task.done()
    assert session.reconnecting is False
    assert session.client is None


async def test_reconnect_continues_beyond_old_ten_attempt_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    attempts = 0
    replacement = SimpleNamespace(isConnected=SimpleNamespace(is_set=lambda: True))

    def create_client() -> object:
        nonlocal attempts
        attempts += 1
        if attempts <= 10:
            raise OSError("radio still offline")
        return replacement

    monkeypatch.setattr(
        MeshtasticSession, "_create_client", lambda _self: create_client()
    )
    monkeypatch.setattr(MeshtasticSession, "_subscribe_callbacks", lambda _self: None)
    monkeypatch.setattr(MeshtasticSession, "_refresh_node_id", lambda _self: None)
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 0.0
    )

    await session._reconnect_loop()

    assert attempts == 11
    assert session.reconnect_attempts == 0
    assert session.diagnostics().reconnect_total_attempts == 11
    assert session.client is replacement


async def test_disconnect_during_reconnect_exit_schedules_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect observed in the reconnect exit window starts recovery.

    Cross-thread ``notify_connection_lost`` scheduling is covered separately by
    the behavior-reference/session tests.  This regression focuses on the
    ownership window itself without blocking the asyncio loop with threading
    primitives.
    """

    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    replacement = SimpleNamespace(isConnected=SimpleNamespace(is_set=lambda: True))

    monkeypatch.setattr(MeshtasticSession, "_create_client", lambda _self: replacement)
    monkeypatch.setattr(MeshtasticSession, "_subscribe_callbacks", lambda _self: None)
    monkeypatch.setattr(MeshtasticSession, "_refresh_node_id", lambda _self: None)
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 0.0
    )

    original_loop = MeshtasticSession._reconnect_loop
    loop_invocations = 0
    recovery_loops = 0

    async def dispatch_loop(current: MeshtasticSession) -> None:
        nonlocal loop_invocations, recovery_loops
        loop_invocations += 1
        if loop_invocations == 1:
            await original_loop(current)
        else:
            recovery_loops += 1

    monkeypatch.setattr(MeshtasticSession, "_reconnect_loop", dispatch_loop)

    class _ExitWindowLogger:
        def info(self, *_args: object, **_kwargs: object) -> None:
            # The success log executes after ownership has transferred to the
            # replacement but before the first reconnect task has returned.
            assert session.reconnecting is False
            assert session._reconnect_task is None
            session.notify_connection_lost(
                expected_generation=session.connection_generation
            )

        def warning(self, *_args: object, **_kwargs: object) -> None:
            pass

        def debug(self, *_args: object, **_kwargs: object) -> None:
            pass

    session._logger = _ExitWindowLogger()  # type: ignore[assignment]

    await session._reconnect_loop()

    assert await wait_until(lambda: recovery_loops == 1, timeout=1.0)


async def test_reconnect_loop_stops_only_when_session_stop_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    attempts = 0

    def fail_client() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 12:
            session._stop_requested = True
        raise OSError("offline")

    monkeypatch.setattr(
        MeshtasticSession, "_create_client", lambda _self: fail_client()
    )
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 0.0
    )

    await session._reconnect_loop()

    assert attempts == 12
    assert session.diagnostics().reconnect_total_attempts == 12
    assert session.reconnecting is False


def test_connected_is_false_while_session_recovery_is_active() -> None:
    session = _session()
    session._started = True
    session._activate_client(
        SimpleNamespace(isConnected=SimpleNamespace(is_set=lambda: True))
    )
    assert session.connected is True

    session._reconnecting = True

    assert session.connected is False


def test_tcp_liveness_enabled_only_for_tcp_with_positive_interval() -> None:
    assert _session(_tcp_config())._tcp_liveness_enabled is True
    assert (
        _session(_tcp_config(tcp_liveness_interval_seconds=0.0))._tcp_liveness_enabled
        is False
    )
    serial = MeshtasticConfig(
        adapter_id="serial",
        connection_type="serial",
        serial_port="/dev/null",
    ).validate()
    assert _session(serial)._tcp_liveness_enabled is False


async def test_liveness_failure_schedules_generation_guarded_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(tcp_liveness_interval_seconds=0.01)
    client = object()
    session._activate_client(client)
    session._started = True
    session._loop = asyncio.get_running_loop()
    generation = session.connection_generation
    calls: list[tuple[int | None, str]] = []

    async def fail_probe(_client: object, _generation: int) -> None:
        raise TimeoutError("probe timed out")

    def notify(*, expected_generation: int | None = None, reason: str = "") -> None:
        calls.append((expected_generation, reason))
        session._stop_requested = True

    monkeypatch.setattr(
        MeshtasticSession,
        "_probe_tcp_liveness",
        lambda _self, client, generation: fail_probe(client, generation),
    )
    monkeypatch.setattr(
        MeshtasticSession,
        "notify_connection_lost",
        lambda _self, **kwargs: notify(**kwargs),
    )
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session._LIVENESS_INITIAL_DELAY_SECONDS",
        0.0,
    )

    await session._liveness_loop()

    assert calls == [(generation, "TCP liveness probe failed: probe timed out")]
    diag = session.diagnostics()
    assert diag.liveness_probe_failures == 1
    assert diag.liveness_consecutive_failures == 1
    assert diag.last_liveness_error == "probe timed out"


async def test_stale_liveness_failure_cannot_reconnect_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(tcp_liveness_interval_seconds=0.01)
    original = object()
    replacement = object()
    session._activate_client(original)
    session._started = True
    session._loop = asyncio.get_running_loop()
    calls: list[object] = []

    async def stale_probe(_client: object, _generation: int) -> None:
        session._activate_client(replacement)
        session._stop_requested = True
        raise TimeoutError("old client")

    monkeypatch.setattr(
        MeshtasticSession,
        "_probe_tcp_liveness",
        lambda _self, client, generation: stale_probe(client, generation),
    )
    monkeypatch.setattr(
        MeshtasticSession,
        "notify_connection_lost",
        lambda _self, **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session._LIVENESS_INITIAL_DELAY_SECONDS",
        0.0,
    )

    await session._liveness_loop()

    assert calls == []
    assert session.client is replacement
    assert session.diagnostics().liveness_probe_failures == 0


async def test_tcp_probe_retires_mtjk_response_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    dropped: list[int] = []

    admin_module = types.ModuleType("meshtastic.protobuf.admin_pb2")

    class AdminMessage:
        def __init__(self) -> None:
            self.get_device_metadata_request = False

    admin_module.AdminMessage = AdminMessage  # type: ignore[attr-defined]
    protobuf_module = types.ModuleType("meshtastic.protobuf")
    protobuf_module.admin_pb2 = admin_module  # type: ignore[attr-defined]
    mesh_module = types.ModuleType("meshtastic")
    mesh_module.protobuf = protobuf_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", mesh_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", protobuf_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.admin_pb2", admin_module)

    def send_admin(request: object, **kwargs: object) -> object:
        assert isinstance(request, AdminMessage)
        assert request.get_device_metadata_request is True
        assert kwargs["wantResponse"] is True
        callback = kwargs["onResponse"]
        assert callable(callback)
        callback({"decoded": {}})
        return SimpleNamespace(id=777)

    client = SimpleNamespace(
        localNode=SimpleNamespace(_send_admin=send_admin),
        _request_wait_runtime=SimpleNamespace(
            drop_response_handler=lambda request_id: dropped.append(request_id)
        ),
    )
    session._activate_client(client)
    session._started = True

    await session._probe_tcp_liveness(client, session.connection_generation)

    assert dropped == [777]


async def test_tcp_probe_timeout_retires_response_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(tcp_liveness_timeout_seconds=0.01)
    dropped: list[int] = []

    admin_module = types.ModuleType("meshtastic.protobuf.admin_pb2")

    class AdminMessage:
        def __init__(self) -> None:
            self.get_device_metadata_request = False

    admin_module.AdminMessage = AdminMessage  # type: ignore[attr-defined]
    protobuf_module = types.ModuleType("meshtastic.protobuf")
    protobuf_module.admin_pb2 = admin_module  # type: ignore[attr-defined]
    mesh_module = types.ModuleType("meshtastic")
    mesh_module.protobuf = protobuf_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", mesh_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", protobuf_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.admin_pb2", admin_module)

    client = SimpleNamespace(
        localNode=SimpleNamespace(
            _send_admin=lambda *_args, **_kwargs: SimpleNamespace(id=888)
        ),
        _request_wait_runtime=SimpleNamespace(
            drop_response_handler=lambda request_id: dropped.append(request_id)
        ),
    )
    session._activate_client(client)
    session._started = True

    with pytest.raises(asyncio.TimeoutError):
        await session._probe_tcp_liveness(client, session.connection_generation)

    assert dropped == [888]


async def test_tcp_probe_requires_pinned_mtjk_admin_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()

    admin_module = types.ModuleType("meshtastic.protobuf.admin_pb2")

    class AdminMessage:
        def __init__(self) -> None:
            self.get_device_metadata_request = False

    admin_module.AdminMessage = AdminMessage  # type: ignore[attr-defined]
    protobuf_module = types.ModuleType("meshtastic.protobuf")
    protobuf_module.admin_pb2 = admin_module  # type: ignore[attr-defined]
    mesh_module = types.ModuleType("meshtastic")
    mesh_module.protobuf = protobuf_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", mesh_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", protobuf_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.admin_pb2", admin_module)

    client = SimpleNamespace(localNode=SimpleNamespace())
    session._activate_client(client)
    session._started = True

    with pytest.raises(
        MeshtasticConnectionError,
        match=r"localNode\._send_admin liveness seam is unavailable",
    ):
        await session._probe_tcp_liveness(client, session.connection_generation)


async def test_failed_reconnect_attempt_closes_partial_client_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    closed: list[str] = []
    attempts = 0

    class Client:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    def create_client() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Client("partial")
        return Client("replacement")

    def subscribe(current: MeshtasticSession) -> None:
        client = current.client
        if getattr(client, "name", None) == "partial":
            raise MeshtasticConnectionError("subscription failed")

    monkeypatch.setattr(
        MeshtasticSession, "_create_client", lambda _self: create_client()
    )
    monkeypatch.setattr(MeshtasticSession, "_subscribe_callbacks", subscribe)
    monkeypatch.setattr(MeshtasticSession, "_refresh_node_id", lambda _self: None)
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 0.0
    )

    await session._reconnect_loop()

    assert closed == ["partial"]
    assert getattr(session.client, "name", None) == "replacement"
    assert attempts == 2


async def test_stop_cancels_liveness_supervisor() -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    liveness_task = asyncio.create_task(asyncio.Event().wait())
    session._liveness_task = liveness_task

    await session.stop(timeout=0.1)

    assert liveness_task.cancelled()
    assert session._liveness_task is None
    assert session._started is False


async def test_liveness_success_records_diagnostics(monkeypatch) -> None:
    """A passing probe resets the failure streak and records success timing."""
    session = _session(tcp_liveness_interval_seconds=0.01)
    session._activate_client(object())
    session._started = True
    session._loop = asyncio.get_running_loop()

    probe_calls = 0

    async def ok_probe(_client: object, _generation: int) -> None:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls >= 2:
            session._stop_requested = True

    monkeypatch.setattr(
        MeshtasticSession,
        "_probe_tcp_liveness",
        lambda _self, client, generation: ok_probe(client, generation),
    )
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session._LIVENESS_INITIAL_DELAY_SECONDS",
        0.0,
    )

    await session._liveness_loop()

    diag = session.diagnostics()
    assert probe_calls == 2
    assert diag.liveness_probe_successes == 1
    assert diag.liveness_consecutive_failures == 0
    assert diag.last_liveness_success_time is not None
    assert diag.last_liveness_error is None


async def test_probe_skips_stale_generation_before_touching_the_client(
    monkeypatch,
) -> None:
    session = _session()
    session._activate_client(object())
    session._started = True
    stale_generation = session.connection_generation - 1

    seam_calls: list[object] = []

    def send_admin(_request: object, **_kwargs: object) -> object:
        seam_calls.append(_request)
        return SimpleNamespace(id=1)

    client = SimpleNamespace(localNode=SimpleNamespace(_send_admin=send_admin))
    monkeypatch.setattr(MeshtasticSession, "_create_client", lambda _self: client)

    await session._probe_tcp_liveness(client, stale_generation)

    assert seam_calls == []
    assert session._last_liveness_probe_time is None


async def test_probe_rejects_send_admin_returning_none(monkeypatch) -> None:
    """A wedged admin send (no packet id) is a connection error, not a pass."""
    admin_module = types.ModuleType("meshtastic.protobuf.admin_pb2")

    class AdminMessage:
        def __init__(self) -> None:
            self.get_device_metadata_request = False

    admin_module.AdminMessage = AdminMessage  # type: ignore[attr-defined]
    protobuf_module = types.ModuleType("meshtastic.protobuf")
    protobuf_module.admin_pb2 = admin_module  # type: ignore[attr-defined]
    mesh_module = types.ModuleType("meshtastic")
    mesh_module.protobuf = protobuf_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", mesh_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", protobuf_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.admin_pb2", admin_module)

    client = SimpleNamespace(
        localNode=SimpleNamespace(_send_admin=lambda *_a, **_kw: None),
        _request_wait_runtime=SimpleNamespace(
            drop_response_handler=lambda _request_id: None
        ),
    )
    session = _session()
    session._activate_client(client)
    session._started = True

    with pytest.raises(
        MeshtasticConnectionError,
        match="did not start the TCP liveness admin request",
    ):
        await session._probe_tcp_liveness(client, session.connection_generation)


async def test_probe_survives_handler_retire_failure(monkeypatch) -> None:
    """A failing drop_response_handler is logged, never raised."""
    admin_module = types.ModuleType("meshtastic.protobuf.admin_pb2")

    class AdminMessage:
        def __init__(self) -> None:
            self.get_device_metadata_request = False

    admin_module.AdminMessage = AdminMessage  # type: ignore[attr-defined]
    protobuf_module = types.ModuleType("meshtastic.protobuf")
    protobuf_module.admin_pb2 = admin_module  # type: ignore[attr-defined]
    mesh_module = types.ModuleType("meshtastic")
    mesh_module.protobuf = protobuf_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "meshtastic", mesh_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf", protobuf_module)
    monkeypatch.setitem(sys.modules, "meshtastic.protobuf.admin_pb2", admin_module)

    captured: dict[str, object] = {}

    def send_admin(_request: object, **kwargs: object) -> object:
        # Fire the response from a worker thread, as mtjk's reader thread does.
        on_response = kwargs["onResponse"]
        threading.Timer(0.05, on_response, args=({"decoded": {}},)).start()
        captured["on_response"] = on_response
        return SimpleNamespace(id=999)

    client = SimpleNamespace(
        localNode=SimpleNamespace(_send_admin=send_admin),
        _request_wait_runtime=SimpleNamespace(
            drop_response_handler=lambda _request_id: (_ for _ in ()).throw(
                OSError("runtime already torn down")
            )
        ),
    )
    session = _session(tcp_liveness_timeout_seconds=5.0)
    session._activate_client(client)
    session._started = True

    await session._probe_tcp_liveness(client, session.connection_generation)

    # A late response after retirement must not raise from the resolve guard.
    on_response = captured["on_response"]
    on_response({"decoded": {}})
    on_response({"decoded": {}})


async def test_reconnect_delay_constant_when_initial_meets_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(
        reconnect_backoff_initial_seconds=4.0,
        reconnect_backoff_max_seconds=4.0,
    )
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session.random.uniform",
        lambda _a, _b: 0.0,
    )
    assert session._reconnect_delay(7) == 4.0


async def test_liveness_cancelled_without_stop_propagates(monkeypatch) -> None:
    """External cancellation (not shutdown) must surface, not be swallowed."""
    session = _session(tcp_liveness_interval_seconds=5.0)
    session._activate_client(object())
    session._started = True
    session._loop = asyncio.get_running_loop()
    probe_entered = asyncio.Event()

    async def parked_probe(_client: object, _generation: int) -> None:
        probe_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        MeshtasticSession,
        "_probe_tcp_liveness",
        lambda _self, client, generation: parked_probe(client, generation),
    )
    monkeypatch.setattr(
        "medre.adapters.meshtastic.session._LIVENESS_INITIAL_DELAY_SECONDS",
        0.0,
    )

    task = asyncio.ensure_future(session._liveness_loop())
    await wait_until(probe_entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_reconnect_cancelled_without_stop_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        MeshtasticSession,
        "_create_client",
        lambda _self: (_ for _ in ()).throw(
            AssertionError("should not reach client creation")
        ),
    )
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 60.0
    )

    task = asyncio.ensure_future(session._reconnect_loop())
    assert await wait_until(lambda: session.reconnecting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_reconnect_cancelled_after_stop_request_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        MeshtasticSession,
        "_create_client",
        lambda _self: (_ for _ in ()).throw(
            AssertionError("should not reach client creation")
        ),
    )
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 60.0
    )

    task = asyncio.ensure_future(session._reconnect_loop())
    assert await wait_until(lambda: session.reconnecting)
    session._stop_requested = True
    task.cancel()
    await task  # returns without raising
    assert not task.cancelled()
    assert session.reconnecting is False


async def test_reconnect_close_failure_of_partial_client_is_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close() failure on a partial client never escapes the retry loop."""
    session = _session()
    session._started = True
    session._loop = asyncio.get_running_loop()
    # Silence the session logger: this test asserts containment of a failing
    # close(), and pytest's log-capture emit for per-attempt warnings can
    # otherwise serialize against the capture handler on slow CI schedulers.
    session._logger.disabled = True
    attempts = 0

    class PartialClient:
        def close(self) -> None:
            raise OSError("close failed")

    def create_client() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return PartialClient()
        session._stop_requested = True
        raise OSError("offline")

    def subscribe() -> None:
        if attempts == 1:
            raise MeshtasticConnectionError("pubsub unavailable")

    monkeypatch.setattr(
        MeshtasticSession, "_create_client", lambda _self: create_client()
    )
    monkeypatch.setattr(
        MeshtasticSession, "_subscribe_callbacks", lambda _self: subscribe()
    )
    monkeypatch.setattr(MeshtasticSession, "_refresh_node_id", lambda _self: None)
    monkeypatch.setattr(
        MeshtasticSession, "_reconnect_delay", lambda _self, _attempt: 0.0
    )

    await session._reconnect_loop()

    assert attempts == 2
    assert session.reconnecting is False
    assert session.client is None
