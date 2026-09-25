"""Process-isolated real LXMF/RNS probe used by local integration tests.

Scenarios:

- ``suite`` — repeated real router lifecycle, malformed-callback
  resilience, and injected startup-failure cleanup.
- ``soak`` — ten start/stop cycles with a stable delivery destination.
- ``relation`` — two-process relation roundtrip.  The parent process
  runs MEDRE instance A (real ``LxmfAdapter``, ``connection_type=
  "reticulum"``) and spawns ``relation-b`` as a child process running
  instance B.  The two instances are wired together exclusively over a
  loopback Reticulum ``UDPInterface`` pair (127.0.0.1, per-run free
  ports, per-process temp ``HOME`` and router storage).  A renders a
  relation-bearing canonical event through ``LxmfRenderer`` (MEDRE
  fields envelope under ``FIELD_CUSTOM_META``/``0xFD``) and delivers
  it via ``LxmfAdapter.deliver``; B decodes the same message through
  the real inbound adapter/codec path and records the canonical result
  for the parent to compare.  No external network, hardware, retained
  keys, or fixed sleeps — coordination is deadline-bounded polling of
  observable conditions (readiness files, ``RNS.Identity.recall``).
- ``relation-b`` — the receiver half of ``relation`` (child process).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


@dataclass(frozen=True)
class SuiteResult:
    cycles: int
    stable_destination: bool
    callback_count: int
    startup_failure: str | None


@dataclass(frozen=True)
class SoakResult:
    cycles: int
    stable_destination: bool


class _MalformedDelivery:
    @property
    def source_hash(self) -> bytes:
        raise ValueError("malformed SDK callback payload")


def _make_identity(path: Path) -> None:
    import RNS

    identity = RNS.Identity()
    if not identity.to_file(str(path)):
        raise RuntimeError("failed to persist local LXMF integration identity")


def _config(base: Path, identity_path: Path) -> Any:
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-local-integration",
        connection_type="reticulum",
        identity_path=str(identity_path),
        storage_path=str(base / "router"),
        announce_interval_seconds=0,
        message_delay_seconds=0,
    )


def _session(config: Any) -> Any:
    from medre.adapters.lxmf.session import LxmfSession

    return LxmfSession(
        adapter_id="lxmf-local-integration",
        config=config,
        logger=logging.getLogger("test.lxmf.local"),
    )


async def _run_suite(base: Path) -> SuiteResult:
    from medre.adapters.lxmf.errors import LxmfConnectionError

    identity_path = base / "identity"
    _make_identity(identity_path)
    hashes: list[str] = []
    callback_count = 0

    def on_message(_payload: dict[str, Any]) -> None:
        nonlocal callback_count
        callback_count += 1

    session = _session(_config(base, identity_path))
    for _ in range(3):
        await session.start(on_message)
        assert session.connected is True
        assert session.router_running is True
        destination_hash = session._delivery_destination_hash
        if destination_hash is None:
            raise AssertionError(
                "real LXMRouter did not register a delivery destination"
            )
        hashes.append(destination_hash.hex())

        # Malformed SDK callback payloads must be dropped without reaching the
        # adapter callback or destabilising the active router.
        session._on_lxmf_delivery(_MalformedDelivery())
        assert callback_count == 0

        await session.stop()
        assert session.connected is False
        assert session.router_running is False

        # Reticulum callbacks can race with teardown; late callbacks are dropped.
        session._on_lxmf_delivery(object())
        assert callback_count == 0

    # Inject a deterministic failure after the real LXMRouter constructor has
    # run. This exercises MEDRE's partial-start cleanup without depending on
    # incidental filesystem access inside a particular LXMF release.
    import LXMF

    def fail_registration(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected delivery identity registration failure")

    failed = _session(_config(base, identity_path))
    startup_failure = None
    with patch.object(
        LXMF.LXMRouter,
        "register_delivery_identity",
        new=fail_registration,
    ):
        try:
            await failed.start(on_message)
        except LxmfConnectionError as exc:
            startup_failure = type(exc).__name__
        else:
            raise AssertionError("injected LXMF startup failure unexpectedly started")

    assert failed.connected is False
    assert failed.router_running is False
    assert failed._router is None
    assert failed._identity is None

    return SuiteResult(
        cycles=len(hashes),
        stable_destination=len(set(hashes)) == 1,
        callback_count=callback_count,
        startup_failure=startup_failure,
    )


async def _run_soak(base: Path) -> SoakResult:
    identity_path = base / "identity"
    _make_identity(identity_path)
    session = _session(_config(base, identity_path))
    hashes: list[str] = []
    for _ in range(10):
        await session.start(lambda _payload: None)
        destination_hash = session._delivery_destination_hash
        if destination_hash is None:
            raise AssertionError(
                "real LXMRouter did not register a delivery destination"
            )
        hashes.append(destination_hash.hex())
        await session.stop()
    return SoakResult(cycles=len(hashes), stable_destination=len(set(hashes)) == 1)


# ===================================================================
# Two-instance relation roundtrip (real SDK, loopback Reticulum only)
# ===================================================================


@dataclass(frozen=True)
class RelationResult:
    """Verdict of the two-instance relation roundtrip."""

    b_received: bool
    b_content_match: bool
    b_title_match: bool
    b_envelope_event_id_match: bool
    b_relations_match: bool
    b_source_is_a: bool
    a_native_id_matches_b_message_id: bool
    lxmf_version: str
    rns_version: str


_RELATION_BODY = "medre two-instance relation roundtrip body"
_RELATION_TITLE = "medre relation roundtrip title"
_RELATION_TARGET_EVENT_ID = "evt-relation-target-7c41d9a2"
_RELATION_TARGET_NATIVE_MESSAGE_ID = (
    "4f1c0aa9d6e83b2157f0c6d92b48a1e0f3d5b7c8901a2e4f6d8b0c2a4e6f8d01"
)
_EXPECTED_RELATIONS = [
    {
        "relation_type": "reply",
        "target_event_id": _RELATION_TARGET_EVENT_ID,
        "target_native_ref": {
            "adapter": "lxmf-relation-a",
            "native_channel_id": None,
            "native_message_id": _RELATION_TARGET_NATIVE_MESSAGE_ID,
        },
        "key": None,
        "fallback_text": "reply to the earlier local roundtrip anchor message",
    }
]

_B_READY_TIMEOUT = 30.0
_RECALL_TIMEOUT = 30.0
_B_RECEIVED_TIMEOUT = 45.0
_B_RECEIVER_WAIT = 40.0
_POLL_INTERVAL = 0.05


def _pick_free_port_pair() -> tuple[int, int]:
    """Reserve a distinct pair of ephemeral loopback UDP ports.

    Both sockets remain bound until both numbers are selected, preventing
    the kernel from returning the same ephemeral port for the pair.
    """
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second,
    ):
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return int(first.getsockname()[1]), int(second.getsockname()[1])


def _write_reticulum_config(
    home: Path, interface_name: str, listen_port: int, forward_port: int
) -> None:
    """Write a loopback-only UDPInterface Reticulum config under *home*.

    The config pins both Reticulum instances to 127.0.0.1 with a fresh
    per-run port pair, disables transport mode and the shared-instance
    listener, and keeps logging terse.  Nothing here reaches a physical
    interface or an external network.
    """
    config_dir = home / ".reticulum"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = (
        "[reticulum]\n"
        "enable_transport = No\n"
        "share_instance = No\n"
        "\n"
        "[logging]\n"
        "loglevel = 4\n"
        "\n"
        "[interfaces]\n"
        f"  [[{interface_name}]]\n"
        "    type = UDPInterface\n"
        "    enabled = Yes\n"
        "    listen_ip = 127.0.0.1\n"
        f"    listen_port = {listen_port}\n"
        "    forward_ip = 127.0.0.1\n"
        f"    forward_port = {forward_port}\n"
    )
    (config_dir / "config").write_text(config, encoding="utf-8")


def _assert_loopback_config_active(expected_home: Path) -> None:
    """Fail fast if a system Reticulum config overrode HOME isolation."""
    import RNS

    configpath = getattr(RNS.Reticulum, "configpath", None)
    if not configpath or not configpath.startswith(str(expected_home)):
        raise RuntimeError(
            "Reticulum did not use the per-instance loopback config "
            f"(active configpath={configpath!r}, expected under "
            f"{str(expected_home)!r}); a system /etc/reticulum config "
            "may be overriding HOME-based isolation"
        )


def _sdk_versions() -> tuple[str, str]:
    import LXMF
    import RNS

    return str(LXMF.__version__), str(RNS.__version__)


def _relation_config(
    base: Path, side: str, display_name: str, announce_interval: float
) -> Any:
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(
        adapter_id=f"lxmf-relation-{side}",
        connection_type="reticulum",
        display_name=display_name,
        stamp_cost=0,
        default_delivery_method="opportunistic",
        storage_path=str(base / side / "router"),
        announce_interval_seconds=announce_interval,
        message_delay_seconds=0,
    )


def _relation_context(adapter_id: str, publish: Any) -> Any:
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=publish,
        logger=logging.getLogger(f"test.lxmf.local.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _child_log_tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<child log unavailable>"


async def _await_json_file(
    path: Path, timeout: float, child: subprocess.Popen[bytes] | None, log_path: Path
) -> dict[str, Any]:
    """Deadline-bounded wait for a JSON marker file written by a peer."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # partially written; keep polling
        if child is not None and child.poll() is not None:
            raise RuntimeError(
                f"receiver process exited with code {child.returncode} before "
                f"writing {path.name}; child log tail:\n" + _child_log_tail(log_path)
            )
        await asyncio.sleep(_POLL_INTERVAL)
    raise RuntimeError(
        f"timed out after {timeout}s waiting for {path}; child log tail:\n"
        + _child_log_tail(log_path)
    )


async def _await_condition(
    condition: Callable[[], bool], timeout: float, description: str
) -> None:
    """Deadline-bounded wait on an observable predicate."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(_POLL_INTERVAL)
    raise RuntimeError(f"timed out after {timeout}s waiting for {description}")


def _relation_summary(event: Any) -> dict[str, Any]:
    """Project a received canonical event into comparable JSON scalars."""

    def _ref(ref: Any) -> dict[str, Any] | None:
        if ref is None:
            return None
        return {
            "adapter": ref.adapter,
            "native_channel_id": ref.native_channel_id,
            "native_message_id": ref.native_message_id,
        }

    envelope = {}
    custom = getattr(event.metadata, "custom", None) or {}
    if isinstance(custom, dict):
        raw = custom.get("medre_envelope")
        if isinstance(raw, dict):
            envelope = raw

    native_ref = getattr(event, "source_native_ref", None)
    return {
        "body": event.payload.get("body"),
        "title": event.payload.get("title"),
        "source_transport_id": event.source_transport_id,
        "message_id": getattr(native_ref, "native_message_id", None),
        "envelope_event_id": envelope.get("event_id"),
        "relations": [
            {
                "relation_type": r.relation_type,
                "target_event_id": r.target_event_id,
                "target_native_ref": _ref(r.target_native_ref),
                "key": r.key,
                "fallback_text": r.fallback_text,
            }
            for r in event.relations
        ],
    }


async def _run_relation_receiver(base: Path) -> int:
    """Instance B: receive via the real inbound adapter/codec path."""
    home = base / "home-b"
    received = asyncio.Event()
    events: list[Any] = []

    async def collect(event: Any) -> None:
        events.append(event)
        received.set()

    config = _relation_config(base, "b", "medre-relation-b", announce_interval=0.5)
    from medre.adapters.lxmf.adapter import LxmfAdapter

    adapter = LxmfAdapter(
        config,
        reticulum_config_dir=str(home / ".reticulum"),
    )
    await adapter.start(_relation_context("lxmf-relation-b", collect))
    _assert_loopback_config_active(home)

    destination_hash = adapter.session._delivery_destination_hash
    if destination_hash is None:
        await adapter.stop()
        raise RuntimeError("real LXMRouter did not register a delivery destination")
    _write_json(base / "b-ready.json", {"destination_hash": destination_hash.hex()})

    try:
        await asyncio.wait_for(received.wait(), timeout=_B_RECEIVER_WAIT)
    except (asyncio.TimeoutError, TimeoutError):
        _write_json(
            base / "b-received.json",
            {"received": False, "reason": "no inbound event before deadline"},
        )
        await adapter.stop()
        return 0

    summary = _relation_summary(events[0])
    lxmf_version, rns_version = _sdk_versions()
    _write_json(
        base / "b-received.json",
        {
            "received": True,
            "lxmf_version": lxmf_version,
            "rns_version": rns_version,
            **summary,
        },
    )
    await adapter.stop()
    return 0


def _collect_noop():
    """Instance A publisher: A only sends, so inbound publishes are no-ops."""

    async def _noop(event: Any) -> None:
        return None

    return _noop


async def _run_relation(base: Path) -> RelationResult:
    """Instance A: render, deliver over the local link, and judge."""
    home_a = base / "home-a"
    home_b = base / "home-b"
    work_a = base / "a"
    work_b = base / "b"
    for directory in (home_a, home_b, work_a, work_b):
        directory.mkdir(parents=True, exist_ok=True)

    port_a, port_b = _pick_free_port_pair()
    _write_reticulum_config(home_a, "Medre Relation A", port_a, port_b)
    _write_reticulum_config(home_b, "Medre Relation B", port_b, port_a)

    # Preserve HOME isolation for SDK-adjacent helpers; the adapter also
    # receives the explicit per-instance Reticulum config directory below.
    os.environ["HOME"] = str(home_a)

    repo_root = Path(__file__).resolve().parents[2]
    log_path = base / "relation-b.log"
    child_env = dict(os.environ)
    child_env["HOME"] = str(home_b)
    # The log handle must outlive the child: the child inherits the fd
    # and writes RNS/LXMF output to it for its whole lifetime.  Guard the
    # spawn separately so a Popen failure cannot leak the parent handle.
    child_log = open(log_path, "wb")
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.helpers.lxmf_local_probe",
                "relation-b",
                str(base),
            ],
            cwd=str(repo_root),
            env=child_env,
            stdout=child_log,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        child_log.close()
        raise

    adapter = None
    try:
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.renderer import LxmfRenderer
        from medre.core.events.canonical import (
            CanonicalEvent,
            EventRelation,
            NativeRef,
        )
        from medre.core.events.kinds import EventKind
        from medre.core.events.metadata import EventMetadata
        from medre.core.rendering.renderer import RenderingContext

        adapter = LxmfAdapter(
            _relation_config(base, "a", "medre-relation-a", announce_interval=0),
            reticulum_config_dir=str(home_a / ".reticulum"),
        )
        await adapter.start(_relation_context("lxmf-relation-a", _collect_noop()))
        _assert_loopback_config_active(home_a)

        a_dest = adapter.session._delivery_destination_hash
        if a_dest is None:
            raise RuntimeError("instance A registered no delivery destination")

        ready = await _await_json_file(
            base / "b-ready.json", _B_READY_TIMEOUT, child, log_path
        )
        b_hash = str(ready["destination_hash"])

        import RNS

        b_hash_bytes = bytes.fromhex(b_hash)
        await _await_condition(
            lambda: RNS.Identity.recall(b_hash_bytes) is not None,
            timeout=_RECALL_TIMEOUT,
            description="instance A to hear instance B's announce",
        )

        event_id = f"evt-relation-{uuid.uuid4().hex[:12]}"
        event = CanonicalEvent(
            event_id=event_id,
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-relation-a",
            source_transport_id=a_dest.hex(),
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id=_RELATION_TARGET_EVENT_ID,
                    target_native_ref=NativeRef(
                        adapter="lxmf-relation-a",
                        native_channel_id=None,
                        native_message_id=_RELATION_TARGET_NATIVE_MESSAGE_ID,
                    ),
                    key=None,
                    fallback_text="reply to the earlier local roundtrip anchor message",
                ),
            ),
            payload={"body": _RELATION_BODY, "title": _RELATION_TITLE},
            metadata=EventMetadata(),
            source_native_ref=NativeRef(
                adapter="lxmf-relation-a",
                native_channel_id=None,
                native_message_id=a_dest.hex(),
            ),
        )

        renderer = LxmfRenderer(metadata_embedding=True)
        rendered = await renderer.render(
            event,
            RenderingContext(
                delivery_strategy="direct",
                target_adapter="lxmf-relation-a",
                target_platform="lxmf",
                max_text_chars=16384,
            ),
        )
        payload = dict(rendered.payload)
        payload["destination_hash"] = b_hash
        delivery_result = await adapter.deliver(replace(rendered, payload=payload))
        if delivery_result is None or not delivery_result.native_message_id:
            raise RuntimeError("instance A deliver() returned no native message id")
        a_native_id = str(delivery_result.native_message_id)

        b_result = await _await_json_file(
            base / "b-received.json", _B_RECEIVED_TIMEOUT, child, log_path
        )

        lxmf_version, rns_version = _sdk_versions()
        if not b_result.get("received"):
            raise RuntimeError(
                "instance B reported no receipt: "
                f"{json.dumps(b_result, sort_keys=True)}"
            )

        return RelationResult(
            b_received=True,
            b_content_match=b_result.get("body") == _RELATION_BODY,
            b_title_match=b_result.get("title") == _RELATION_TITLE,
            b_envelope_event_id_match=(b_result.get("envelope_event_id") == event_id),
            b_relations_match=b_result.get("relations") == _EXPECTED_RELATIONS,
            b_source_is_a=b_result.get("source_transport_id") == a_dest.hex(),
            a_native_id_matches_b_message_id=(
                b_result.get("message_id") == a_native_id
            ),
            lxmf_version=lxmf_version,
            rns_version=rns_version,
        )
    finally:
        try:
            if adapter is not None:
                await adapter.stop()
        finally:
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            finally:
                child_log.close()


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: lxmf_local_probe <suite|soak|relation|relation-b> "
            "<working-directory>"
        )
    scenario = sys.argv[1]
    base = Path(sys.argv[2]).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if scenario == "suite":
        result = asyncio.run(_run_suite(base))
    elif scenario == "soak":
        result = asyncio.run(_run_soak(base))
    elif scenario == "relation":
        result = asyncio.run(_run_relation(base))
    elif scenario == "relation-b":
        return asyncio.run(_run_relation_receiver(base))
    else:
        raise SystemExit(f"unknown scenario: {scenario}")
    print(
        "MEDRE_LOCAL_INTEGRATION_RESULT=" + json.dumps(asdict(result), sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
