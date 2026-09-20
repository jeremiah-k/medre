"""Live cross-transport bridge tests over the real MEDRE runtime with LXMF.

Opt-in ONLY: ``MEDRE_LX_BRIDGE=1`` with the explicit owned endpoints of the
Meshtastic, MeshCore and LXMF transports.

Topology:
- The MEDRE runtime (in-process, real adapters, real sqlite) owns MT-A
  (serial), MC-A (BLE) and LX-A (RNode via the isolated Reticulum config
  dir from ``LXMF_MEDRE_RNS_CONFIG``).
- Independent native peers: MT-B (serial SDK), MC-B (BLE SDK) and LX-B
  (own Reticulum instance/identity/storage on ``LXMF_PEER_RNS_CONFIG``).

Covers the four directed routes that involve LXMF (B1) and a controlled
fan-out from one native source event to two different native destination
peers with per-target receipt evidence (B2). Existing MT<->MC routes keep
their own module and are not re-proven here.

Evidence layers stay distinct: MEDRE durable admission (A), native
acceptance (B — Meshtastic/MeshCore receipts are local acceptance; LXMF
receipts are local_queue), and the independent peer's RF observation (C).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.lxmf_live_peer import (
    LxmfPeerListener as _LxListener,
    delivery_dest_hash as _delivery_dest_hash,
    run_lxmf_peer as _lx_peer,
)
from tests.helpers.meshcore_live_peer import (
    MeshCorePeerListener as _McListener,
    run_meshcore_peer as _mc_peer,
)
from tests.helpers.meshtastic_live_peer import (
    MeshtasticPeerListener as _MtListener,
    run_meshtastic_peer as _mt_peer,
)

_BRIDGE = os.environ.get("MEDRE_LX_BRIDGE", "") == "1"
_MT_MEDRE = os.environ.get("MESHTASTIC_MEDRE_SERIAL_PORT", "")
_MC_MEDRE = os.environ.get("MESHCORE_MEDRE_BLE_ADDRESS", "")
_MC_PEER = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_MEDRE_RNS = os.environ.get("LXMF_MEDRE_RNS_CONFIG", "")
_MEDRE_IDENTITY = os.environ.get("LXMF_MEDRE_IDENTITY", "")
_PEER_IDENTITY = os.environ.get("LXMF_PEER_IDENTITY", "")

_REQUIRE = pytest.mark.skipif(
    not (
        _BRIDGE
        and _MT_MEDRE
        and _MC_MEDRE
        and _MC_PEER
        and _MEDRE_RNS
        and _MEDRE_IDENTITY
        and _PEER_IDENTITY
        and os.environ.get("LXMF_PEER_RNS_CONFIG", "")
        and os.environ.get("MESHTASTIC_PEER_SERIAL_PORT", "")
    ),
    reason=(
        "opt-in LXMF bridge: set MEDRE_LX_BRIDGE=1, MESHTASTIC_MEDRE_SERIAL_PORT, "
        "MESHTASTIC_PEER_SERIAL_PORT, MESHCORE_MEDRE_BLE_ADDRESS, "
        "MESHCORE_PEER_BLE_ADDRESS, LXMF_MEDRE_RNS_CONFIG, LXMF_MEDRE_IDENTITY, "
        "LXMF_PEER_IDENTITY, LXMF_PEER_RNS_CONFIG"
    ),
)

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 45.0
_LX_DELIVERY_TIMEOUT = 90.0
_MC_MAX_TEXT = 160

_QUICK = os.environ.get("MEDRE_LIVE_QUICK", "") == "1"
_QUICK_SKIP = pytest.mark.skipif(
    _QUICK, reason="quick iteration mode: directed LXMF routes only"
)

pytestmark = [
    pytest.mark.filterwarnings(
        # Regex: literal parens must be escaped or the pattern silently
        # never matches the actual message text (the pinned RNS release on py3.14).
        r"ignore:setDaemon\(\) is deprecated:DeprecationWarning",
    ),
    pytest.mark.filterwarnings(
        # the pinned meshcore dispatcher calls asyncio.iscoroutinefunction;
        # under the suite-wide error filter the DeprecationWarning kills
        # the BLE adapter before it can serve the bridge routes.
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning",
    ),
]


def _peer_dest() -> str:
    return _delivery_dest_hash(_PEER_IDENTITY)


def _nonce(tag: str) -> str:
    return f"B-X{tag}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _fresh_rns_state():
    """Wipe the lab RNS storage dirs once before any instance starts."""
    if not (_BRIDGE and _MEDRE_RNS):
        yield
        return
    for cfg_dir in (_MEDRE_RNS, os.environ.get("LXMF_PEER_RNS_CONFIG", "")):
        storage = Path(cfg_dir) / "storage" if cfg_dir else None
        if storage is not None and storage.exists():
            shutil.rmtree(storage)
    yield


def _build_runtime(db_path: Path, *, routes: tuple[str, ...]):
    from medre.config.adapters.lxmf import LxmfConfig
    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        LxmfRuntimeConfig,
        MeshCoreRuntimeConfig,
        MeshtasticRuntimeConfig,
        RuntimeConfig,
        RuntimeOptions,
        StorageConfig,
    )
    from medre.config.paths import MedrePaths
    from medre.config.routes import RouteConfig, RouteConfigSet
    from medre.runtime.builder import RuntimeBuilder

    mt = MeshtasticRuntimeConfig(
        adapter_id="mt_radio",
        enabled=True,
        adapter_kind="real",
        config=MeshtasticConfig(
            adapter_id="mt_radio",
            connection_type="serial",
            serial_port=_MT_MEDRE,
            default_channel=0,
            message_delay_seconds=_TX_PACING,
        ).validate(),
    )
    mc = MeshCoreRuntimeConfig(
        adapter_id="mc_radio",
        enabled=True,
        adapter_kind="real",
        config=MeshCoreConfig(
            adapter_id="mc_radio",
            connection_type="ble",
            ble_address=_MC_MEDRE,
            default_channel=1,
            message_delay_seconds=_TX_PACING,
            max_text_bytes=_MC_MAX_TEXT,
            identity="MEDRE-MC-A",
        ).validate(),
    )
    lx = LxmfRuntimeConfig(
        adapter_id="lx_radio",
        enabled=True,
        adapter_kind="real",
        config=LxmfConfig(
            adapter_id="lx_radio",
            connection_type="reticulum",
            identity_path=_MEDRE_IDENTITY,
            storage_path=str(db_path.parent / "lxmf_medre_storage"),
            reticulum_config_dir=_MEDRE_RNS,
            display_name="MEDRE-LX-A",
            announce_interval_seconds=8.0,
            message_delay_seconds=_TX_PACING,
            stamp_cost=0,
            default_delivery_method="direct",
        ).validate(),
    )
    specs = {
        "mt_to_lx": ("mt_radio", "lx_radio", "0", _peer_dest()),
        "lx_to_mt": ("lx_radio", "mt_radio", None, "0"),
        "mc_to_lx": ("mc_radio", "lx_radio", "1", _peer_dest()),
        "lx_to_mc": ("lx_radio", "mc_radio", None, "1"),
        # Fan-out pair: both routes share the mt_radio source so one native
        # source event fans out to two different transports.
        "mt_to_mc": ("mt_radio", "mc_radio", "0", "1"),
    }
    route_list = [
        RouteConfig(
            route_id=f"bridge_{key}",
            source_adapters=(src,),
            dest_adapters=(dst,),
            source_channel=src_ch,
            dest_channel=dst_ch,
        )
        for key, (src, dst, src_ch, dst_ch) in specs.items()
        if key in routes
    ]
    routes_set = RouteConfigSet(routes=tuple(route_list))
    routes_set.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="lxmf-bridge-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            meshtastic={"mt_radio": mt},
            meshcore={"mc_radio": mc},
            lxmf={"lx_radio": lx},
        ),
        routes=routes_set,
    )
    home = db_path.parent
    paths = MedrePaths(
        config_dir=home / "config",
        config_file=home / "config" / "config.yaml",
        state_dir=home / "state",
        data_dir=home / "data",
        cache_dir=home / "cache",
        log_dir=home / "logs",
        database_path=db_path,
    )
    return RuntimeBuilder(config, paths).build()


async def _stop(app):
    await bounded(app.stop(), 30.0, "bridge runtime stop")


async def _launch(db_path: Path, routes: tuple[str, ...]):
    app = _build_runtime(db_path, routes=routes)
    try:
        await bounded(app.start(), 150.0, "bridge runtime start")
    except Exception:
        try:
            await _stop(app)
        except Exception:
            pass
        raise
    deadline = time.monotonic() + 30.0
    last = None
    while time.monotonic() < deadline:
        infos = []
        for aid in ("mt_radio", "mc_radio", "lx_radio"):
            info = await bounded(
                app.adapters[aid].health_check(), 15.0, f"{aid} health_check"
            )
            infos.append(info.health)
        last = infos
        if infos == ["healthy", "healthy", "healthy"]:
            return app
        await asyncio.sleep(1.0)
    await _stop(app)
    raise RuntimeError(f"bridge runtime never reached healthy ({last})")


async def _await_peer_recall(app, dest_hex: str, timeout: float = 60.0) -> bool:
    import RNS

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if RNS.Identity.recall(bytes.fromhex(dest_hex)) is not None:
            return True
        await asyncio.sleep(1.0)
    return False


async def _receipts_for(app, needle: str, timeout: float = _RECEIPT_TIMEOUT):
    """Poll durable receipts for the admitted event whose body has needle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ids = await app.storage.list_event_ids_page(after_event_id=None, limit=300)
        for eid in ids:
            ev = await app.storage.get(eid)
            if ev and needle in (ev.payload or {}).get("body", ""):
                receipts = await app.storage.list_receipts_for_event(eid)
                if any(
                    r.status in ("sent", "failed", "dead_lettered") for r in receipts
                ):
                    return ev, receipts
        await asyncio.sleep(0.5)
    return None, []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
class TestBridgeToLxmf:
    """B1: native sources -> MEDRE -> LX-A RNode RF -> LX-B observation."""

    async def test_directed_route_mt_to_lx(self, tmp_path: Path) -> None:
        app = await _launch(tmp_path / "lab.db", ("mt_to_lx",))
        try:
            nonce = _nonce("MT2LX")
            with _LxListener(_LX_DELIVERY_TIMEOUT) as peer:
                assert await _await_peer_recall(
                    app, _peer_dest()
                ), "runtime cannot recall the LX peer identity (no announce)"
                sent = await asyncio.to_thread(
                    _mt_peer,
                    [
                        "sendn",
                        os.environ["MESHTASTIC_PEER_SERIAL_PORT"],
                        json.dumps([nonce]),
                    ],
                    60,
                )
                assert sent and sent[-1].get("sent_id"), "MT send not accepted"
                ev, receipts = await _receipts_for(app, nonce)
                assert ev is not None, "nonce never durably admitted"
                assert receipts, "no durable receipt"
                rx = peer.packets_until(
                    lambda ps: any(nonce in (p.get("content") or "") for p in ps),
                    _LX_DELIVERY_TIMEOUT,
                )
            hits = [p for p in rx if nonce in (p.get("content") or "")]
            assert hits, f"LX peer did not observe the crossed message; raw={rx!r}"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.target_adapter == "lx_radio"
            assert latest.status == "sent"
            assert hits[-1]["hash"] == latest.adapter_message_id
            assert ev.source_adapter == "mt_radio"
        finally:
            await _stop(app)

    async def test_directed_route_mc_to_lx(self, tmp_path: Path) -> None:
        app = await _launch(tmp_path / "lab.db", ("mc_to_lx",))
        try:
            nonce = _nonce("MC2LX")
            with _LxListener(_LX_DELIVERY_TIMEOUT) as peer:
                assert await _await_peer_recall(
                    app, _peer_dest()
                ), "runtime cannot recall the LX peer identity (no announce)"
                sent = await asyncio.to_thread(
                    _mc_peer, ["sendn", _MC_PEER, json.dumps([nonce])], 90
                )
                assert all(
                    not item["error"] for item in sent["sent"]
                ), "MC send not accepted"
                ev, receipts = await _receipts_for(app, nonce)
                assert ev is not None, "nonce never durably admitted"
                assert ev.source_adapter == "mc_radio"
                assert receipts, "no durable receipt"
                rx = peer.packets_until(
                    lambda ps: any(nonce in (p.get("content") or "") for p in ps),
                    _LX_DELIVERY_TIMEOUT,
                )
            hits = [p for p in rx if nonce in (p.get("content") or "")]
            assert hits, f"LX peer did not observe the crossed message; raw={rx!r}"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.target_adapter == "lx_radio"
            assert latest.status == "sent"
            assert hits[-1]["hash"] == latest.adapter_message_id
        finally:
            await _stop(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
class TestBridgeFromLxmf:
    """B1 reverse: LX-B RNode RF -> MEDRE -> native MT/MC destinations."""

    async def test_directed_route_lx_to_mt(self, tmp_path: Path) -> None:
        app = await _launch(tmp_path / "lab.db", ("lx_to_mt",))
        try:
            nonce = _nonce("LX2MT")
            with _MtListener(240) as mt_listener:
                sent = await asyncio.to_thread(
                    _lx_peer,
                    ["send", _delivery_dest_hash(_MEDRE_IDENTITY), json.dumps([nonce])],
                    280,
                )
                assert sent["sent"], "peer LXMF send not accepted"
                assert (
                    sent["sent"][0]["state"] == 8
                ), f"peer-side delivery not confirmed: {sent['sent']!r}"
                ev, receipts = await _receipts_for(app, nonce)
                assert ev is not None, "nonce never durably admitted"
                assert ev.source_adapter == "lx_radio"
                out = mt_listener.packets_until(
                    lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                    60.0,
                )
            hits = [p for p in out if nonce in (p.get("text") or "")]
            assert hits, f"MT peer did not observe the crossed message; raw={out!r}"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.target_adapter == "mt_radio"
            assert latest.status == "sent"
        finally:
            await _stop(app)

    async def test_directed_route_lx_to_mc(self, tmp_path: Path) -> None:
        app = await _launch(tmp_path / "lab.db", ("lx_to_mc",))
        try:
            nonce = _nonce("LX2MC")
            with _McListener(240) as mc_listener:
                sent = await asyncio.to_thread(
                    _lx_peer,
                    ["send", _delivery_dest_hash(_MEDRE_IDENTITY), json.dumps([nonce])],
                    280,
                )
                assert sent["sent"], "peer LXMF send not accepted"
                assert (
                    sent["sent"][0]["state"] == 8
                ), f"peer-side delivery not confirmed: {sent['sent']!r}"
                ev, receipts = await _receipts_for(app, nonce)
                assert ev is not None, "nonce never durably admitted"
                assert ev.source_adapter == "lx_radio"
                out = mc_listener.packets_until(
                    lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                    75.0,
                )
            hits = [p for p in out if nonce in (p.get("text") or "")]
            assert hits, f"MC peer did not observe the crossed message; raw={out!r}"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.target_adapter == "mc_radio"
            assert latest.status == "sent"
        finally:
            await _stop(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
@_QUICK_SKIP
class TestBridgeFanOut:
    """B2: one native source event fans out to two different transports."""

    async def test_fanout_mt_source_to_lx_and_mc(self, tmp_path: Path) -> None:
        """MT-B -> MEDRE(MT-A) -> {LX-A -> LX-B, MC-A -> MC-B}.

        Two routes (mt_to_lx, mt_to_mc) share the mt_radio source so one
        native source event fans out to two different transports.  Each
        target asserts its own receipt and its own independent far-peer
        observation; one target failing never masks the other.
        """
        app = await _launch(tmp_path / "lab.db", ("mt_to_lx", "mt_to_mc"))
        try:
            nonce = _nonce("FAN")
            with _LxListener(_LX_DELIVERY_TIMEOUT) as lx_peer, _McListener(
                300
            ) as mc_listener:
                assert await _await_peer_recall(
                    app, _peer_dest()
                ), "runtime cannot recall the LX peer identity (no announce)"
                sent = await asyncio.to_thread(
                    _mt_peer,
                    [
                        "sendn",
                        os.environ["MESHTASTIC_PEER_SERIAL_PORT"],
                        json.dumps([nonce]),
                    ],
                    60,
                )
                assert sent and sent[-1].get("sent_id"), "MT send not accepted"
                ev = None
                receipts = []
                deadline = time.monotonic() + 120.0
                while time.monotonic() < deadline:
                    ids = await app.storage.list_event_ids_page(
                        after_event_id=None, limit=300
                    )
                    for eid in ids:
                        candidate = await app.storage.get(eid)
                        if candidate and nonce in (candidate.payload or {}).get(
                            "body", ""
                        ):
                            ev = candidate
                            receipts = await app.storage.list_receipts_for_event(eid)
                            targets = {
                                r.target_adapter
                                for r in receipts
                                if r.status in ("sent", "failed", "dead_lettered")
                            }
                            if {"lx_radio", "mc_radio"} <= targets:
                                break
                    if ev is not None and {"lx_radio", "mc_radio"} <= {
                        r.target_adapter
                        for r in receipts
                        if r.status in ("sent", "failed", "dead_lettered")
                    }:
                        break
                    await asyncio.sleep(0.5)
                assert ev is not None, "nonce never durably admitted"
                lx_rx = lx_peer.packets_until(
                    lambda ps: any(nonce in (p.get("content") or "") for p in ps),
                    _LX_DELIVERY_TIMEOUT,
                )
                mc_rx = mc_listener.packets_until(
                    lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                    75.0,
                )
            lx_hits = [p for p in lx_rx if nonce in (p.get("content") or "")]
            mc_hits = [p for p in mc_rx if nonce in (p.get("text") or "")]
            # Per-target evidence is asserted independently: one target
            # failing asserts on that target alone and never masks the other.
            assert lx_hits, f"LX-B missed the fan-out; raw={lx_rx!r}"
            assert mc_hits, f"MC-B missed the fan-out; raw={mc_rx!r}"
            by_target = {}
            for r in receipts:
                by_target.setdefault(r.target_adapter, []).append(r)
            assert "lx_radio" in by_target, f"no LX receipt: {sorted(by_target)!r}"
            assert "mc_radio" in by_target, f"no MC receipt: {sorted(by_target)!r}"
            for target, target_receipts in by_target.items():
                latest = max(target_receipts, key=lambda r: r.sequence)
                assert (
                    latest.status == "sent"
                ), f"{target} latest receipt {latest.status!r}"
            # Target separation: the LX native hash belongs to LX only.
            assert (
                lx_hits[0]["hash"]
                == max(
                    by_target["lx_radio"], key=lambda r: r.sequence
                ).adapter_message_id
            )
        finally:
            await _stop(app)
