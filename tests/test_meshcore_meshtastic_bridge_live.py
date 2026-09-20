"""Live cross-transport MT<->MC bridge tests over the real MEDRE runtime.

Opt-in ONLY: ``MEDRE_MC_BRIDGE=1`` with explicit owned endpoints.

Topology (lab defaults, reversible via env):
- MEDRE runtime owns the Meshtastic radio (MT-A, serial) and the MeshCore
  radio (MC-A, BLE) with real ingress/admission/planning/delivery.
- Independent native peers: MT-B (serial SDK) and MC-B (BLE SDK).

Covers B1 directed routes (both directions), B3 nonmatching-route negative
control, B4 bounded bidirectional echo, and F1 bounded stop/restart with
F6-style isolation evidence while the other transport stays up.

Fast iteration: ``MEDRE_LIVE_QUICK=1`` runs only the MT->MC directed
route (~2 min); the reverse/negative/echo/fault cases are the
full-mode proof gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.meshcore_live_peer import (
    MeshCorePeerListener as _PeerListener,
    run_meshcore_peer as _mc_peer,
)
from tests.helpers.meshtastic_live_peer import (
    MeshtasticPeerListener as _MtListener,
    run_meshtastic_peer as _mt_peer,
)

_BRIDGE = os.environ.get("MEDRE_MC_BRIDGE", "") == "1"
_MT_MEDRE = os.environ.get("MESHTASTIC_MEDRE_SERIAL_PORT", "")
_MT_PEER = os.environ.get("MESHTASTIC_PEER_SERIAL_PORT", "")
_MC_MEDRE = os.environ.get("MESHCORE_MEDRE_BLE_ADDRESS", "")
_MC_PEER = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_MC_MEDRE_NAME = os.environ.get("MESHCORE_MEDRE_NODE_NAME", "MEDRE-MC-A")

_REQUIRE = pytest.mark.skipif(
    not (_BRIDGE and _MT_MEDRE and _MT_PEER and _MC_MEDRE and _MC_PEER),
    reason=(
        "opt-in bridge: set MEDRE_MC_BRIDGE=1, MESHTASTIC_MEDRE_SERIAL_PORT, "
        "MESHTASTIC_PEER_SERIAL_PORT, MESHCORE_MEDRE_BLE_ADDRESS, "
        "MESHCORE_PEER_BLE_ADDRESS"
    ),
)

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 40.0

# Same pinned-SDK constraint as the pair module: the pinned meshcore SDK calls the
# deprecated asyncio.iscoroutinefunction inside its dispatcher; with the
# project's ``filterwarnings = ["error"]`` that DeprecationWarning raises
# inside the SDK task and kills event delivery in the runtime's MeshCore
# session.  Ignore exactly that warning until the pin moves to a release
# with the fix (meshcore_py fix/events-py314-iscoroutinefunction).
pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
]

# Fast-iteration tier: MEDRE_LIVE_QUICK=1 runs only the MT->MC directed
# route; the reverse/negative/echo/fault cases are the full-mode proof.
_QUICK = os.environ.get("MEDRE_LIVE_QUICK", "") == "1"
_QUICK_SKIP = pytest.mark.skipif(
    _QUICK, reason="quick iteration mode: MT->MC directed route only"
)



def _build_runtime(db_path: Path, *, direction: str):
    """direction: 'mt_to_mc', 'mc_to_mt', or 'both'."""
    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
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
            max_text_bytes=160,
            identity=_MC_MEDRE_NAME,
        ).validate(),
    )
    specs = {
        "mt_to_mc": (("mt_radio",), ("mc_radio",), "0", "1"),
        "mc_to_mt": (("mc_radio",), ("mt_radio",), "1", "0"),
    }
    route_list = []
    for key in (
        [direction]
        if direction in specs
        else ["mt_to_mc", "mc_to_mt"] if direction == "both" else []
    ):
        src, dst, src_ch, dst_ch = specs[key]
        route_list.append(
            RouteConfig(
                route_id=f"bridge_{key}",
                source_adapters=src,
                dest_adapters=dst,
                source_channel=src_ch,
                dest_channel=dst_ch,
            )
        )
    routes = RouteConfigSet(routes=tuple(route_list))
    routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="mt-mc-bridge-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            meshtastic={"mt_radio": mt}, meshcore={"mc_radio": mc}
        ),
        routes=routes,
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


async def _start(app):
    # A failed start leaves the app in state 'failed' — starting the same
    # object again is invalid; _launch retries with a fresh runtime.
    await bounded(app.start(), 120.0, "bridge runtime start")


async def _launch(db_path: Path, direction: str):
    """Build and start a runtime, verifying the MC link actually came up.

    A start can either raise or silently come up DEGRADED (which
    dead-letters deliveries with ``Session not initialised``).  Both cases
    retry with a FRESH runtime after a settle; the same app object is
    never started twice.
    """
    health = None
    last_error: str | None = None
    for _attempt in range(2):
        app = _build_runtime(db_path, direction=direction)
        try:
            await bounded(app.start(), 120.0, "bridge runtime start")
        except RuntimeError as exc:
            last_error = f"start raised: {exc}"
            try:
                await _stop(app)
            except Exception:
                pass
            await asyncio.sleep(6.0)
            continue
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            info = await bounded(
                app.adapters["mc_radio"].health_check(), 15.0, "mc health"
            )
            health = info.health
            if health == "healthy":
                return app
            await asyncio.sleep(1.0)
        last_error = f"health stayed {health!r}"
        try:
            await _stop(app)
        except Exception:
            pass
        await asyncio.sleep(6.0)
    raise RuntimeError(f"bridge runtime never reached healthy ({last_error})")


async def _stop(app):
    await bounded(app.stop(), 30.0, "bridge runtime stop")


def _nonce(tag):
    return f"B-X{tag}-{uuid.uuid4().hex[:8]}"


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_directed_route_mt_to_mc(tmp_path: Path) -> None:
    app = await _launch(tmp_path / "lab.db", "mt_to_mc")
    try:
        nonce = _nonce("MT2MC")
        with _PeerListener(_RECEIPT_TIMEOUT + 25) as mc_listener:
            sent = await asyncio.to_thread(
                _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce])], 60
            )
            assert sent and sent[-1].get("sent_id"), "MT send not accepted"
            deadline = time.monotonic() + _RECEIPT_TIMEOUT
            receipts = []
            while time.monotonic() < deadline:
                ids = await app.storage.list_event_ids_page(
                    after_event_id=None, limit=200
                )
                for eid in ids:
                    ev = await app.storage.get(eid)
                    if ev and nonce in (ev.payload or {}).get("body", ""):
                        receipts = await app.storage.list_receipts_for_event(eid)
                        if receipts:
                            break
                if receipts:
                    break
                await asyncio.sleep(0.5)
            assert receipts, "MC-side delivery receipt never appeared"
            out = mc_listener.packets_until(
                lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                45.0,
            )
        hits = [p for p in out if nonce in (p.get("text") or "")]
        assert hits, (
            f"MC-B peer did not observe the crossed message; "
            f"raw={out!r} receipts={[(r.status) for r in receipts]!r}"
        )
        pkt = hits[-1]
        assert pkt["channel"] == 1, "crossed message on wrong MC channel"
        latest = max(receipts, key=lambda r: r.sequence)
        assert latest.target_adapter == "mc_radio"
        assert latest.status == "sent"
    finally:
        await _stop(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
@_QUICK_SKIP
async def test_directed_route_mc_to_mt_with_negative(tmp_path: Path) -> None:
    app = await _launch(tmp_path / "lab.db", "mc_to_mt")
    try:
        # -- B3 negative: MC-B probes on ch2 (a key MEDRE's board does
        # not share).  Nothing may fan out to the MT side.  The ch1
        # positive control uses a DISTINCT nonce so its (expected)
        # crossing cannot mask the negative assertion.
        probe = _nonce("NEG")
        positive = _nonce("NEG-POS")
        # 45s window: MC RF hop + MEDRE 2.5s pacing + LongTurbo MT hop.
        # Drain the listener INSIDE the with block: __exit__ kills the
        # process, and it only prints its JSON after the window ends.
        with _MtListener(45) as mt_listener:
            result = await asyncio.to_thread(
                _mc_peer, ["n6probe", _MC_PEER, probe, positive], 120
            )
            assert result.get("restore_ok"), "peer ch2 not restored"
            await asyncio.sleep(12.0)  # bounded arrival window
            mt_out = mt_listener.packets()
        negative = [p for p in mt_out if probe in (p.get("text") or "")]
        assert negative == [], "nonmatching-route probe crossed the bridge"
        restored_positive = [p for p in mt_out if positive in (p.get("text") or "")]
        assert (
            restored_positive
        ), "positive ch1 control did not cross after ch2 restore"

        # -- B1 positive: ch1 message crosses MC -> MT.
        nonce = _nonce("MC2MT")
        text = nonce + " mc-to-mt"
        with _MtListener(45) as mt_listener:
            result = await asyncio.to_thread(
                _mc_peer, ["sendn", _MC_PEER, json.dumps([text])], 90
            )
            assert all(
                not item["error"] for item in result["sent"]
            ), "MC peer send rejected"
            out = mt_listener.packets_until(
                lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                45.0,
            )
        hits = [p for p in out if nonce in (p.get("text") or "")]
        assert hits, f"MT-B peer did not observe the crossed message; raw={out!r}"
    finally:
        await _stop(app)




@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
@_QUICK_SKIP
async def test_bidirectional_echo_is_bounded(tmp_path: Path) -> None:
    app = await _launch(tmp_path / "lab.db", "both")
    try:
        nonce = _nonce("ECHO")
        # MT-B is a single serial owner per phase (pyserial flock):
        # send first, then watch a bounded bounce window.
        with _PeerListener(_RECEIPT_TIMEOUT + 25) as mc_listener:
            await asyncio.to_thread(
                _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce])], 60
            )
            # Durable chain first: exactly one admission and one MC-side
            # delivery attempt for this event (the bounded-echo
            # contract is about the route, not about RF exactly-once).
            deadline = time.monotonic() + _RECEIPT_TIMEOUT
            receipts = []
            while time.monotonic() < deadline and not receipts:
                ids = await app.storage.list_event_ids_page(
                    after_event_id=None, limit=200
                )
                for eid in ids:
                    ev = await app.storage.get(eid)
                    if ev and nonce in (ev.payload or {}).get("body", ""):
                        receipts = await app.storage.list_receipts_for_event(eid)
                        break
                if not receipts:
                    await asyncio.sleep(0.5)
            assert receipts, "MC-side delivery receipt never appeared"
            assert (
                len(receipts) == 1
            ), f"expected a single delivery attempt, saw {receipts!r}"
            mc_out = mc_listener.packets_until(
                lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                45.0,
            )
        mc_hits = [p for p in mc_out if nonce in (p.get("text") or "")]
        assert len(mc_hits) == 1, (
            f"expected exactly one crossing, saw {len(mc_hits)}; "
            f"receipts={[(r.status,) for r in receipts]!r} raw={mc_out!r}"
        )
        # Loop prevention: the crossed message must not re-cross
        # MC->MT back to the originating peer within the bounded window
        # (window covers the observed BLE session-settle delivery lag).
        with _MtListener(30) as mt_listener:
            await asyncio.sleep(15)
            mt_out = mt_listener.packets()
        bounced = [p for p in mt_out if nonce in (p.get("text") or "")]
        assert bounced == [], "message re-crossed MT->MC->MT (amplification)"
    finally:
        await _stop(app)

@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
@_QUICK_SKIP
async def test_mc_stop_restart_and_mt_isolation(tmp_path: Path) -> None:
    """F1 (MC): 3 bounded stop/restart cycles with link release.

    Each cycle builds a fresh runtime on the proven adapter pattern
    (same as ``TestMeshtasticLiveSmoke.test_repeated_start_stop_cycle``):
    a full MEDRE restart is a new runtime, not a reused app object.

    F6 (scoped): the Meshtastic adapter stays healthy while the
    MeshCore link cycles; MT ingress keeps being admitted.
    """
    for cycle in range(1, 4):
        app = await _launch(tmp_path / f"lab{cycle}.db", "mc_to_mt")
        try:
            pass
        finally:
            await _stop(app)
        probe = await asyncio.to_thread(
            subprocess.run,
            ["bluetoothctl", "info", _MC_MEDRE],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert probe.returncode == 0 and probe.stdout.strip(), (
            f"cycle {cycle}: bluetoothctl probe failed "
            f"(rc={probe.returncode}): {probe.stderr[-300:]}"
        )
        assert (
            "Connected: yes" not in probe.stdout
        ), f"cycle {cycle}: BLE link not released"
    # Final start: MT route still usable after MC recovery cycles.
    app = _build_runtime(tmp_path / "lab-final.db", direction="mc_to_mt")
    await _start(app)
    try:
        nonce = _nonce("F6")
        sent = await asyncio.to_thread(
            _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce])], 60
        )
        assert sent and sent[-1].get("sent_id")
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        admitted = False
        while time.monotonic() < deadline and not admitted:
            ids = await app.storage.list_event_ids_page(
                after_event_id=None, limit=200
            )
            for eid in ids:
                ev = await app.storage.get(eid)
                if ev and nonce in (ev.payload or {}).get("body", ""):
                    admitted = True
                    break
            await asyncio.sleep(0.5)
        assert admitted, "MT ingress not admitted after MC fault cycles"
    finally:
        await _stop(app)
