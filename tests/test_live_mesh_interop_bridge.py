"""Live mesh<->mesh interop bridge tests over the real MEDRE runtime.

Opt-in ONLY: ``MEDRE_MESH_INTEROP=1`` plus explicit owned radio endpoints.
Skipped by default; never runs in CI.  NO Matrix adapter participates —
this suite is designed around the mesh transports themselves (per
operator steering), and every stimulus and verdict is automated.

Topology (lab defaults):
- ONE MEDRE runtime owns three radio adapters: meshtastic (MT-A serial),
  meshcore (MC-A BLE), lxmf (LX-A via isolated RNS config).
- Three bidirectional radio<->radio routes (six directed edges):
  MT<->MC, MT<->LX, MC<->LX.
- Per leg: the SOURCE transport's peer board (MT-B / MC-B / LX-B) sends a
  nonce; the two FAR peers listen and are the actual far-side radio
  oracle.  The runtime's canonical admission + delivery receipts are the
  in-run transport evidence; far-peer capture is the RF-delivery proof.

Ownership rules (hardware exclusivity):
- The runtime owns MT-A / MC-A / LX-A exclusively.
- Each peer board is driven by exactly one peer helper at a time: a leg's
  sender owns the source peer, listeners own the two far peers.  Listener
  context managers are closed before a peer becomes a sender in a later
  leg.  RF budgets: one nonce per leg, 2.5 s pacing, private channels.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.live_harness import (
    PINNED_SDK_UNRAISABLE_FILTERS,
    bounded,
    launch_bounded,
)
from tests.helpers.lxmf_live_peer import delivery_dest_hash as _lx_dest_hash
from tests.helpers.lxmf_live_peer import run_lxmf_peer as _lx_peer
from tests.helpers.meshcore_live_peer import MeshCorePeerListener as _McListener
from tests.helpers.meshcore_live_peer import run_meshcore_peer as _mc_peer
from tests.helpers.meshtastic_live_peer import MeshtasticPeerListener as _MtListener
from tests.helpers.meshtastic_live_peer import run_meshtastic_peer as _mt_peer

_INTEROP = os.environ.get("MEDRE_MESH_INTEROP", "") == "1"

_MT_MEDRE = os.environ.get("MESHTASTIC_MEDRE_SERIAL_PORT", "")
_MT_PEER = os.environ.get("MESHTASTIC_PEER_SERIAL_PORT", "")
_MC_MEDRE = os.environ.get("MESHCORE_MEDRE_BLE_ADDRESS", "")
_MC_PEER = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_MC_MEDRE_NAME = os.environ.get("MESHCORE_MEDRE_NODE_NAME", "MEDRE-MC-A")

_LX_MEDRE_RNS = os.environ.get("LXMF_MEDRE_RNS_CONFIG", "")
_LX_MEDRE_IDENT = os.environ.get("LXMF_MEDRE_IDENTITY", "")
_LX_MEDRE_STORAGE = os.environ.get("LXMF_MEDRE_STORAGE", "")
_LX_PEER_RNS = os.environ.get("LXMF_PEER_RNS_CONFIG", "")
_LX_PEER_IDENT = os.environ.get("LXMF_PEER_IDENTITY", "")

_RADIOS_OK = all(
    [
        _MT_MEDRE,
        _MT_PEER,
        _MC_MEDRE,
        _MC_PEER,
        _LX_MEDRE_RNS,
        _LX_MEDRE_IDENT,
        _LX_MEDRE_STORAGE,
        _LX_PEER_RNS,
        _LX_PEER_IDENT,
    ]
)

_REQUIRE = pytest.mark.skipif(
    not (_INTEROP and _RADIOS_OK),
    reason=(
        "opt-in mesh<->mesh interop: set MEDRE_MESH_INTEROP=1, "
        "MESHTASTIC_MEDRE_SERIAL_PORT, MESHTASTIC_PEER_SERIAL_PORT, "
        "MESHCORE_MEDRE_BLE_ADDRESS, MESHCORE_PEER_BLE_ADDRESS, "
        "LXMF_MEDRE_RNS_CONFIG, LXMF_MEDRE_IDENTITY, LXMF_MEDRE_STORAGE, "
        "LXMF_PEER_RNS_CONFIG, LXMF_PEER_IDENTITY"
    ),
)

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 90.0
# Far-side RF capture windows (measured-conservative): LX direct delivery
# includes first-path discovery; MT/MC propagation is typically seconds
# but prior collector windows never captured MT-B/MC-B — the windows stay
# generous so a miss is a real negative, not a deadline artifact.
_CAPTURE_WINDOWS = {"mt": 90.0, "mc": 90.0, "lx": 120.0}

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
    pytest.mark.filterwarnings(
        "ignore:setDaemon\\(\\) is deprecated:DeprecationWarning"
    ),
    *(pytest.mark.filterwarnings(spec) for spec in PINNED_SDK_UNRAISABLE_FILTERS),
]


def _nonce(tag: str) -> str:
    return f"MSHX-{tag}-{uuid.uuid4().hex[:8]}"


def _build_runtime(db_path: Path, lx_storage: Path):
    """Three real radio adapters, three bidirectional mesh<->mesh routes.

    No Matrix adapter: this suite is mesh-first by design.
    """
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
            origin_label="medre-lab",
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
            origin_label="medre-lab",
        ).validate(),
    )
    lx = LxmfRuntimeConfig(
        adapter_id="lx_radio",
        enabled=True,
        adapter_kind="real",
        config=LxmfConfig(
            adapter_id="lx_radio",
            connection_type="reticulum",
            identity_path=_LX_MEDRE_IDENT,
            storage_path=str(lx_storage),
            reticulum_config_dir=_LX_MEDRE_RNS,
            display_name="MEDRE-LX-A",
            announce_interval_seconds=8.0,
            message_delay_seconds=_TX_PACING,
            stamp_cost=0,
            default_delivery_method="direct",
            origin_label="medre-lab",
        ).validate(),
    )
    lx_peer_dest = _lx_dest_hash(_LX_PEER_IDENT)
    route_list = [
        RouteConfig(
            route_id="mt_mc_bridge",
            source_adapters=("mt_radio",),
            dest_adapters=("mc_radio",),
            source_channel="0",
            dest_channel="1",
            directionality="bidirectional",
        ),
        RouteConfig(
            route_id="mt_lx_bridge",
            source_adapters=("mt_radio",),
            dest_adapters=("lx_radio",),
            source_channel="0",
            dest_channel=lx_peer_dest,
            directionality="bidirectional",
        ),
        RouteConfig(
            route_id="mc_lx_bridge",
            source_adapters=("mc_radio",),
            dest_adapters=("lx_radio",),
            source_channel="1",
            dest_channel=lx_peer_dest,
            directionality="bidirectional",
        ),
    ]
    routes = RouteConfigSet(routes=tuple(route_list))
    routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="mesh-interop-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            meshtastic={"mt_radio": mt},
            meshcore={"mc_radio": mc},
            lxmf={"lx_radio": lx},
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


async def _launch(db_path: Path, lx_storage: Path):
    """Build and start the runtime with race-free bounded cleanup."""
    return await launch_bounded(
        lambda: _build_runtime(db_path, lx_storage),
        start_timeout=180.0,
        stop_timeout=45.0,
        label="mesh interop runtime",
    )


async def _stop(app):
    await bounded(app.stop(), 60.0, "mesh interop runtime stop")


async def _stop_preserving_primary(app) -> None:
    """Stop without allowing cleanup failure to replace an active failure."""
    primary = sys.exc_info()[1]
    try:
        await _stop(app)
    except BaseException as cleanup_exc:
        if primary is None:
            raise
        print(
            "mesh interop runtime cleanup also failed while preserving "
            f"primary {primary!r}: {cleanup_exc!r}",
            flush=True,
        )


async def _wait_for_receipt(app, nonce: str, target: str, timeout: float):
    """Wait for a sent target receipt, retaining terminal failure evidence."""
    evidence: list[object] = [None, []]

    async def _probe() -> bool:
        ids = await app.storage.list_event_ids_page(after_event_id=None, limit=200)
        for eid in ids:
            ev = await app.storage.get(eid)
            if ev and nonce in (ev.payload or {}).get("body", ""):
                receipts = await app.storage.list_receipts_for_event(eid)
                targeted = [r for r in receipts if r.target_adapter == target]
                if not targeted:
                    continue
                latest = max(targeted, key=lambda r: r.sequence)
                evidence[:] = [ev, targeted]
                return latest.status in {"sent", "failed", "dead_lettered", "suppressed"}
        return False

    await wait_until(_probe, timeout=timeout, interval=0.5)
    return evidence[0], evidence[1]


_LEGS = ("mt", "mc", "lx")
_LEG_ADAPTER = {"mt": "mt_radio", "mc": "mc_radio", "lx": "lx_radio"}
# A leg's source peer sends; the other two transports' peers listen.
_FAR_OF = {"mt": ("mc", "lx"), "mc": ("mt", "lx"), "lx": ("mt", "mc")}


class _FarListeners:
    """Own the two far-peer listeners for one leg, hardware-exclusive."""

    def __init__(self, far_tags: tuple[str, str]) -> None:
        self._far_tags = far_tags
        self._mt: _MtListener | None = None
        self._mc: _McListener | None = None
        self._lx = None

    def __enter__(self) -> "_FarListeners":
        # Listener lifetime must cover send + sequential receipt waits
        # (worst case 2 x _RECEIPT_TIMEOUT) before capture polling begins.
        # Cover the longest source-peer send, both sequential receipt waits,
        # and this listener's own capture poll. Context exit still terminates
        # the child immediately once evidence is complete.
        windows = {
            tag: _CAPTURE_WINDOWS[tag] + 2 * _RECEIPT_TIMEOUT + 120.0 + 30.0
            for tag in self._far_tags
        }
        try:
            for tag in self._far_tags:
                if tag == "mt":
                    self._mt = _MtListener(windows["mt"]).__enter__()
                elif tag == "mc":
                    self._mc = _McListener(windows["mc"]).__enter__()
                else:
                    from tests.helpers.lxmf_live_peer import LxmfPeerListener

                    self._lx = LxmfPeerListener(windows["lx"]).__enter__()
        except BaseException:
            # A context whose __enter__ raises never receives __exit__ from
            # Python, so explicitly unwind listeners that already acquired
            # hardware-exclusive peer endpoints.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        for listener in (self._mt, self._mc, self._lx):
            if listener is not None:
                try:
                    listener.__exit__(*exc)
                except Exception:  # pragma: no cover - live-only teardown
                    pass
        self._mt = self._mc = self._lx = None

    def captured_texts(self, tag: str, nonce: str) -> list[str]:
        if tag == "mt" and self._mt is not None:
            packets = self._mt.packets_until(
                lambda rows: any(nonce in (row.get("text") or "") for row in rows),
                _CAPTURE_WINDOWS[tag],
            )
            return [row.get("text") or "" for row in packets]
        if tag == "mc" and self._mc is not None:
            packets = self._mc.packets_until(
                lambda rows: any(nonce in (row.get("text") or "") for row in rows),
                _CAPTURE_WINDOWS[tag],
            )
            return [row.get("text") or "" for row in packets]
        if tag == "lx" and self._lx is not None:
            packets = self._lx.packets_until(
                lambda rows: any(nonce in (row.get("content") or "") for row in rows),
                _CAPTURE_WINDOWS[tag],
            )
            return [row.get("content") or "" for row in packets]
        return []


async def _send_from_peer(tag: str, nonce: str) -> None:
    """Send *nonce* from the SOURCE peer board of leg *tag*."""
    if tag == "mt":
        sent = await asyncio.to_thread(
            _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce])], 60
        )
        assert any(
            isinstance(e, dict) and e.get("sent_id") for e in sent
        ), "MT-B send not accepted"
    elif tag == "mc":
        # sendn takes a JSON ARRAY of texts.
        sent = await asyncio.to_thread(
            _mc_peer, ["sendn", _MC_PEER, json.dumps([nonce])], 90
        )
        assert sent.get("sent"), f"MC-B send not accepted: {sent!r}"
    else:
        dest = _lx_dest_hash(_LX_MEDRE_IDENT)
        sent = await asyncio.to_thread(
            _lx_peer,
            ["send", dest, json.dumps([nonce + " / ünïcode ✓\nline2"])],
            120,
        )
        assert sent.get("sent"), f"LX-B send not accepted: {sent!r}"


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_mesh_to_mesh_six_edges_relayed_over_rf(tmp_path: Path) -> None:
    """Six directed mesh<->mesh edges: RF far-side capture + receipts.

    Every leg is ATTEMPTED before the verdict.  Verdict rules mirror the
    matrix bridge suite: all adapters degraded -> xfail with evidence; an
    attempted leg failing -> loud failure; degraded-not-exercised legs
    disclosed without masking green paths.
    """
    app = await _launch(tmp_path / "lab.db", tmp_path / "lxmf_storage")

    adapter_health: dict[str, str] = {}
    for aid in ("mt_radio", "mc_radio", "lx_radio"):
        try:
            info = await app.adapters[aid].health_check()
            adapter_health[aid] = info.health
        except Exception as exc:  # pragma: no cover - live-only
            adapter_health[aid] = f"error:{exc}"

    degraded: list[str] = []
    failures: list[str] = []
    ran_tags: list[str] = []
    nonces: dict[str, str] = {}
    try:
        for tag in _LEGS:
            health = adapter_health[_LEG_ADAPTER[tag]]
            if health != "healthy":
                degraded.append(
                    f"{tag}: MEDRE adapter {_LEG_ADAPTER[tag]} not healthy "
                    f"({health!r}) -- leg not exercised"
                )
                continue
            ran_tags.append(tag)
            nonce = _nonce(f"{tag}2mesh")
            nonces[tag] = nonce
            try:
                # Far listeners armed BEFORE the send (capture window
                # covers send + relay + RF propagation).
                with _FarListeners(_FAR_OF[tag]) as far:
                    await _send_from_peer(tag, nonce)

                    # In-run transport evidence first (bounded): canonical
                    # admission + 'sent' receipt on BOTH far adapters.
                    for far_tag in _FAR_OF[tag]:
                        ev, receipts = await _wait_for_receipt(
                            app, nonce, _LEG_ADAPTER[far_tag], _RECEIPT_TIMEOUT
                        )
                        assert ev is not None, (
                            f"{tag} leg: canonical event for {nonce!r} "
                            "never appeared"
                        )
                        latest = max(receipts, key=lambda r: r.sequence)
                        assert latest.status == "sent", (
                            f"{tag}->{far_tag}: receipt status " f"{latest.status!r}"
                        )

                    # Far-side RF oracle: the nonce (or its key for the
                    # unicode LX body) lands on both far peers.
                    far_tags = _FAR_OF[tag]
                    captured = await asyncio.gather(
                        *(
                            asyncio.to_thread(far.captured_texts, far_tag, nonce)
                            for far_tag in far_tags
                        )
                    )
                    for far_tag, texts in zip(far_tags, captured, strict=True):
                        if not any(nonce in t for t in texts):
                            failures.append(
                                f"{tag}->{far_tag}: far peer never captured "
                                f"{nonce!r} within {_CAPTURE_WINDOWS[far_tag]}s "
                                "(transport receipt was 'sent')"
                            )
            except (AssertionError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{tag} leg: {exc}")

        for tag, nonce in nonces.items():
            print(f"LEG-NONCE {tag} {nonce}", flush=True)
        if degraded:
            print("DEGRADED-NOT-EXERCISED " + " | ".join(degraded), flush=True)
    finally:
        await _stop_preserving_primary(app)

    if not ran_tags:
        pytest.xfail(
            "no mesh leg could be exercised -- "
            + ("; ".join(degraded) if degraded else "no legs ran")
        )
    assert not failures, f"{len(failures)} mesh interop failure(s): " + " | ".join(
        failures
    )


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_mesh_interop_restart_preserves_state(tmp_path: Path) -> None:
    """One controlled restart: same stores, fresh relay, no replay.

    Pre-restart: MT-B nonce relayed to MC/LX with receipts.  Restart the
    runtime from the SAME database + LX storage.  Post-restart: MC-B nonce
    relays to MT/LX (fresh ingress+egress through different adapters), the
    pre-restart nonce is NOT re-admitted or re-delivered, and adapter
    health is stable.
    """
    db_path = tmp_path / "lab.db"

    app = await _launch(db_path, tmp_path / "lxmf_storage")
    nonce1 = _nonce("RST-A")
    pre_receipt_sequences: dict[str, set[int]] = {}
    pre_event_id: str | None = None
    try:
        sent = await asyncio.to_thread(
            _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce1])], 60
        )
        assert any(
            isinstance(event, dict) and event.get("sent_id") for event in sent
        ), "pre-restart MT-B send not accepted"
        for far_tag in ("mc", "lx"):
            ev, receipts = await _wait_for_receipt(
                app, nonce1, _LEG_ADAPTER[far_tag], _RECEIPT_TIMEOUT
            )
            assert ev is not None and receipts, f"pre-restart {far_tag} leg missed"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.status == "sent", (
                f"pre-restart {far_tag} receipt status {latest.status!r}"
            )
            pre_event_id = ev.event_id
            pre_receipt_sequences[far_tag] = {r.sequence for r in receipts}
    finally:
        await _stop_preserving_primary(app)

    app2 = await _launch(db_path, tmp_path / "lxmf_storage")
    nonce2 = _nonce("RST-B")
    try:
        # The LXMF ratchets fd ResourceWarning is a pinned RNS release
        # boundary (see live_harness docs); a second launch over existing
        # storage is exactly its trigger condition.
        await asyncio.to_thread(_mc_peer, ["sendn", _MC_PEER, json.dumps([nonce2])], 90)
        for far_tag in ("mt", "lx"):
            ev, receipts = await _wait_for_receipt(
                app2, nonce2, _LEG_ADAPTER[far_tag], _RECEIPT_TIMEOUT
            )
            assert ev is not None and receipts, f"post-restart {far_tag} leg missed"
            latest = max(receipts, key=lambda r: r.sequence)
            assert latest.status == "sent", (
                f"post-restart {far_tag} receipt status {latest.status!r}"
            )

        assert pre_event_id is not None
        replay_receipts = await app2.storage.list_receipts_for_event(pre_event_id)
        for far_tag, expected in pre_receipt_sequences.items():
            observed = {
                r.sequence
                for r in replay_receipts
                if r.target_adapter == _LEG_ADAPTER[far_tag]
            }
            assert observed == expected, (
                f"pre-restart {far_tag} receipt sequence changed after restart: "
                f"{expected!r} -> {observed!r}"
            )

        ids = await app2.storage.list_event_ids_page(after_event_id=None, limit=300)
        counts = {nonce1: 0, nonce2: 0}
        for eid in ids:
            ev = await app2.storage.get(eid)
            if ev is None:
                continue
            body = (ev.payload or {}).get("body", "")
            for probe in counts:
                if probe in body:
                    counts[probe] += 1
        assert counts[nonce1] == 1, "pre-restart nonce replayed after restart"
        assert counts[nonce2] == 1, "post-restart nonce admitted more than once"
    finally:
        await _stop_preserving_primary(app2)
