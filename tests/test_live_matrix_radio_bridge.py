"""Live Matrix<->radio bridge tests over the real MEDRE runtime (E2EE room).

Opt-in ONLY: ``MEDRE_MX_BRIDGE=1`` plus explicit owned endpoints (Matrix
credentials, radio peer endpoints).  Skipped by default; never runs in CI.

Topology (lab defaults, reversible via env):
- ONE MEDRE runtime owns four adapters: matrix (e2ee_required, private
  encrypted room), meshtastic (MT-A serial), meshcore (MC-A BLE), lxmf
  (LX-A via isolated RNS config).
- Three explicit bidirectional routes: matrix<->each radio.  No radio<->radio
  routes exist, so there are no all-to-all echo cycles; the six directed
  Matrix<->radio paths are the coverage target.
- A second bot-account DEVICE (own crypto store) observes the room
  independently: proves real Megolm encryption and far-side decryption.
  Contract-disclosed limitation: same account, so it is NOT an
  independent-sender ingress test.  Own-account echo must be suppressed
  by the runtime's self-guard and never relayed (asserted explicitly).

Radio->Matrix direction is fully automated.  Matrix->radio with a genuine
independent sender requires the invited human user to post in the room; the
automated suite proves the echo guard instead and the interactive window
covers the user path.

RF budgets are bounded: one nonce per leg per test, 2.5 s pacing, private
channels only.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.lxmf_live_peer import delivery_dest_hash as _lx_dest_hash
from tests.helpers.lxmf_live_peer import run_lxmf_peer as _lx_peer
from tests.helpers.matrix_live_observer import (
    MatrixRoomObserver,
    observer_account_devices,
)
from tests.helpers.meshcore_live_peer import MeshCorePeerListener as _McListener
from tests.helpers.meshcore_live_peer import run_meshcore_peer as _mc_peer
from tests.helpers.meshtastic_live_peer import MeshtasticPeerListener as _MtListener
from tests.helpers.meshtastic_live_peer import run_meshtastic_peer as _mt_peer

_BRIDGE = os.environ.get("MEDRE_MX_BRIDGE", "") == "1"

_MATRIX_HS = os.environ.get("MATRIX_HOMESERVER", "")
_MATRIX_USER = os.environ.get("MATRIX_USER_ID", "")
_MATRIX_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN", "")
_MATRIX_ROOM = os.environ.get("MATRIX_ROOM_ID", "")
_MATRIX_STORE = os.environ.get("MATRIX_STORE_PATH", "")
_OBSERVER_TOKEN = os.environ.get("MATRIX_OBSERVER_TOKEN", "")
_OBSERVER_DEVICE = os.environ.get("MATRIX_OBSERVER_DEVICE_ID", "")
_OBSERVER_STORE = os.environ.get("MATRIX_OBSERVER_STORE_PATH", "")

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

_MATRIX_OK = all(
    [
        _MATRIX_HS,
        _MATRIX_USER,
        _MATRIX_TOKEN,
        _MATRIX_ROOM,
        _MATRIX_STORE,
        _OBSERVER_TOKEN,
        _OBSERVER_DEVICE,
        _OBSERVER_STORE,
    ]
)
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
    not (_BRIDGE and _MATRIX_OK and _RADIOS_OK),
    reason=(
        "opt-in Matrix<->radio bridge: set MEDRE_MX_BRIDGE=1, MATRIX_HOMESERVER, "
        "MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID, MATRIX_STORE_PATH, "
        "MATRIX_OBSERVER_TOKEN, MATRIX_OBSERVER_DEVICE_ID, "
        "MATRIX_OBSERVER_STORE_PATH, MESHTASTIC_MEDRE_SERIAL_PORT, "
        "MESHTASTIC_PEER_SERIAL_PORT, MESHCORE_MEDRE_BLE_ADDRESS, "
        "MESHCORE_PEER_BLE_ADDRESS, LXMF_MEDRE_RNS_CONFIG, "
        "LXMF_MEDRE_IDENTITY, LXMF_MEDRE_STORAGE, LXMF_PEER_RNS_CONFIG, "
        "LXMF_PEER_IDENTITY"
    ),
)

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 60.0
_OBSERVER_WINDOW = 90.0

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
    # RNS 1.5.4 threading.setDaemon Deprecation under filterwarnings=error
    # kills the LXMF router task (documented lab gotcha; see RUNBOOK).
    pytest.mark.filterwarnings(
        "ignore:setDaemon\\(\\) is deprecated:DeprecationWarning"
    ),
]


def _nonce(tag: str) -> str:
    return f"MX-X{tag}-{uuid.uuid4().hex[:8]}"


def _build_runtime(db_path: Path, lx_storage: Path):
    """Four real adapters, three explicit bidirectional matrix<->radio routes.

    ``lx_storage`` is the LX-A LXMF message/ratchet store.  It MUST be
    test-scoped (fresh per test): reusing a shared store both violates
    runtime ownership ("a new test must not inherit the previous
    runtime's store") and triggers a pinned-RNS unclosed-read defect
    (RNS 1.5.4 ``Destination._reload_ratchets`` opens an existing
    ``.ratchets`` file without closing it) whose GC-time ResourceWarning
    surfaces as a pytest-unraisable error in a *later* test.
    """
    from medre.config.adapters.lxmf import LxmfConfig
    from medre.config.adapters.matrix import MatrixConfig
    from medre.config.adapters.meshcore import MeshCoreConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        LxmfRuntimeConfig,
        MatrixRuntimeConfig,
        MeshCoreRuntimeConfig,
        MeshtasticRuntimeConfig,
        RuntimeConfig,
        RuntimeOptions,
        StorageConfig,
    )
    from medre.config.paths import MedrePaths
    from medre.config.routes import RouteConfig, RouteConfigSet
    from medre.runtime.builder import RuntimeBuilder

    matrix = MatrixRuntimeConfig(
        adapter_id="matrix",
        enabled=True,
        adapter_kind="real",
        config=MatrixConfig(
            adapter_id="matrix",
            homeserver=_MATRIX_HS,
            user_id=_MATRIX_USER,
            access_token=_MATRIX_TOKEN,
            room_allowlist={_MATRIX_ROOM},
            encryption_mode="e2ee_required",
            require_encrypted_rooms=True,
            store_path=_MATRIX_STORE,
            # Radio->Matrix attribution: sender + origin label prefix.
            relay_prefix="{sender}/{origin_label}: ",
            origin_label="medre-lab",
        ).validate(),
    )
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
    route_list = [
        RouteConfig(
            route_id="mx_mt_bridge",
            source_adapters=("matrix",),
            dest_adapters=("mt_radio",),
            source_room=_MATRIX_ROOM,
            dest_channel="0",
            directionality="bidirectional",
        ),
        RouteConfig(
            route_id="mx_mc_bridge",
            source_adapters=("matrix",),
            dest_adapters=("mc_radio",),
            source_room=_MATRIX_ROOM,
            dest_channel="1",
            directionality="bidirectional",
        ),
        RouteConfig(
            route_id="mx_lx_bridge",
            source_adapters=("matrix",),
            dest_adapters=("lx_radio",),
            source_room=_MATRIX_ROOM,
            dest_channel=_lx_dest_hash(_LX_PEER_IDENT),
            directionality="bidirectional",
        ),
    ]
    routes = RouteConfigSet(routes=tuple(route_list))
    routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="mx-radio-bridge-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            matrix={"matrix": matrix},
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
    """Build and start the runtime, guaranteeing cleanup on failed start.

    ``bounded`` cancellation of ``app.start()`` would otherwise abandon
    already-started adapters holding exclusive radio endpoints (serial
    flock, BLE central, RNode serial) — the next test would inherit a
    leaked owner.  On any start failure the partially-started app is
    stopped before the error propagates.
    """
    app = _build_runtime(db_path, lx_storage)
    try:
        await bounded(app.start(), 180.0, "matrix bridge runtime start")
    except BaseException:
        try:
            await bounded(app.stop(), 45.0, "matrix bridge runtime start cleanup")
        except Exception as cleanup_exc:  # pragma: no cover - live-only
            print(
                f"runtime cleanup after failed start also failed: {cleanup_exc!r}",
                flush=True,
            )
        raise
    return app


async def _stop(app):
    await bounded(app.stop(), 45.0, "matrix bridge runtime stop")


async def _wait_for_receipt(app, nonce: str, target: str, timeout: float):
    """Poll canonical storage for the event carrying *nonce* and a receipt."""
    deadline = time.monotonic() + timeout
    receipts = []
    while time.monotonic() < deadline:
        ids = await app.storage.list_event_ids_page(after_event_id=None, limit=200)
        for eid in ids:
            ev = await app.storage.get(eid)
            if ev and nonce in (ev.payload or {}).get("body", ""):
                receipts = await app.storage.list_receipts_for_event(eid)
                targeted = [r for r in receipts if r.target_adapter == target]
                if targeted:
                    return ev, targeted
        await asyncio.sleep(0.5)
    return None, []


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_radio_to_matrix_three_legs_decrypted_by_observer(
    tmp_path: Path,
) -> None:
    """MT-B / MC-B / LX-B -> MEDRE -> encrypted room -> observer decrypts."""
    legs = (
        ("mt", "MESHTASTIC"),
        ("mc", "MESHCORE"),
        ("lx", "LXMF"),
    )
    app = await _launch(tmp_path / "lab.db", tmp_path / "lxmf_storage")

    # Every configured leg is ATTEMPTED before the verdict: an early
    # xfail/abort would hide later legs behind the first one's failure.
    # Verdict rules:
    # - all adapters degraded (nothing exercised) -> xfail with evidence;
    # - an attempted leg failing -> loud failure (real delivery defect);
    # - attempted legs green -> pass; degraded-not-exercised legs are
    #   disclosed in the output but never mask green paths.
    adapter_health: dict[str, str] = {}
    for aid in ("mt_radio", "mc_radio", "lx_radio"):
        try:
            info = await app.adapters[aid].health_check()
            adapter_health[aid] = info.health
        except Exception as exc:  # pragma: no cover - live-only
            adapter_health[aid] = f"error:{exc}"
    leg_adapter = {"mt": "mt_radio", "mc": "mc_radio", "lx": "lx_radio"}

    degraded: list[str] = []
    failures: list[str] = []
    ran_tags: list[str] = []
    nonces: dict[str, str] = {}
    try:
        with MatrixRoomObserver(
            _OBSERVER_WINDOW * len(legs), "", str(tmp_path / "observer.jsonl")
        ) as observer:
            # NOTE: no radio-side listeners here -- the sender needs the
            # peer device exclusively (pyserial flock / single BLE central /
            # RNode serial).  The far-side evidence for the radio->matrix
            # legs is the matrix delivery receipt plus the observer device.
            for tag, transport in legs:
                health = adapter_health[leg_adapter[tag]]
                if health != "healthy":
                    degraded.append(
                        f"{tag}: MEDRE adapter {leg_adapter[tag]} not healthy "
                        f"({health!r}) -- leg not exercised"
                    )
                    continue
                ran_tags.append(tag)
                nonce = _nonce(f"{tag}2MX")
                nonces[tag] = nonce
                try:
                    if transport == "MESHTASTIC":
                        sent = await asyncio.to_thread(
                            _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce])], 60
                        )
                        assert sent and sent[-1].get("sent_id"), "MT send not accepted"
                    elif transport == "MESHCORE":
                        # sendn takes a JSON ARRAY of texts (peer script
                        # json.loads argv[3]); a bare nonce crashes the
                        # peer's parser and yields a non-JSON exit.
                        sent = await asyncio.to_thread(
                            _mc_peer,
                            ["sendn", _MC_PEER, json.dumps([nonce])],
                            90,
                        )
                        assert sent.get("sent"), f"MC send not accepted: {sent!r}"
                    else:
                        dest = _lx_dest_hash(_LX_MEDRE_IDENT)
                        sent = await asyncio.to_thread(
                            _lx_peer,
                            [
                                "send",
                                dest,
                                json.dumps(
                                    [nonce + " / \u00fcn\u00efcode \u2713\nline2"]
                                ),
                            ],
                            120,
                        )
                        assert sent.get("sent"), f"LX send not accepted: {sent!r}"

                    ev, receipts = await _wait_for_receipt(
                        app, nonce, "matrix", _RECEIPT_TIMEOUT
                    )
                    assert (
                        ev is not None
                    ), f"canonical event for {nonce!r} never appeared"
                    latest = max(receipts, key=lambda r: r.sequence)
                    assert (
                        latest.status == "sent"
                    ), f"matrix receipt status {latest.status!r}"
                except AssertionError as exc:
                    failures.append(f"{tag} leg: {exc}")

            # Observer evidence: every ATTEMPTED leg must arrive DECRYPTED
            # at the far end in this window.  A MegolmEvent entry means nio
            # could NOT decrypt it (in-window only; backlog that predates
            # the watch is not attributable to this run).
            result = observer.wait(timeout=_OBSERVER_WINDOW * len(legs) + 30)
            if result["undecryptable"] != 0:
                failures.append(
                    f"observer: {result['undecryptable']} in-window undecryptable "
                    f"Megolm events: {result!r}"
                )
            events = observer.events()
            seen_bodies = [e.get("body") or "" for e in events]
            for tag in ran_tags:
                leg_bodies = [b for b in seen_bodies if f"MX-X{tag}2MX-" in b]
                if not leg_bodies:
                    failures.append(
                        f"{tag}: observer never received the leg; " f"events={events!r}"
                    )
                    continue
                if "{sender}" in leg_bodies[0]:
                    failures.append(
                        f"{tag}: unrendered prefix template in {leg_bodies[0]!r}"
                    )
                elif not ("medre-lab" in leg_bodies[0] or "/" in leg_bodies[0]):
                    failures.append(
                        f"{tag}: attribution prefix missing from {leg_bodies[0]!r}"
                    )
            for tag, nonce in nonces.items():
                print(f"LEG-NONCE {tag} {nonce}", flush=True)
            if degraded:
                print("DEGRADED-NOT-EXERCISED " + " | ".join(degraded), flush=True)
    finally:
        await _stop(app)

    if not ran_tags:
        pytest.xfail(
            "no radio leg could be exercised -- "
            + ("; ".join(degraded) if degraded else "no legs ran")
        )
    assert not failures, f"{len(failures)} leg/observer failure(s): " + " | ".join(
        failures
    )


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_own_account_echo_is_suppressed_not_relayed(tmp_path: Path) -> None:
    """Same-account second device MUST NOT cross the bridge to any radio.

    Proves the self-echo guard holds on the new path: the observer device
    (same bot account) posts into the encrypted room; the runtime must
    suppress it (no canonical ingress, no radio-side delivery).
    """
    from nio import AsyncClient, AsyncClientConfig

    app = await _launch(tmp_path / "lab.db", tmp_path / "lxmf_storage")
    try:
        probe = _nonce("ECHO")
        client = AsyncClient(
            _MATRIX_HS,
            _MATRIX_USER,
            device_id=_OBSERVER_DEVICE,
            store_path=_OBSERVER_STORE,
            config=AsyncClientConfig(encryption_enabled=True),
        )
        client.restore_login(_MATRIX_USER, _OBSERVER_DEVICE, _OBSERVER_TOKEN)
        try:
            resp = await client.sync(timeout=8000, full_state=True)
            assert not type(resp).__name__.endswith(
                "Error"
            ), f"observer sync failed: {resp!r}"
            send = await client.room_send(
                room_id=_MATRIX_ROOM,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": probe},
                ignore_unverified_devices=True,
            )
            assert not type(send).__name__.endswith(
                "Error"
            ), f"observer send failed: {send!r}"
        finally:
            await client.close()

        # Bounded wait: nothing may cross to either radio peer, and no
        # canonical event carrying the probe may appear.
        with _MtListener(45) as mt_listener, _McListener(45) as mc_listener:
            await asyncio.sleep(45)
            mt_out = mt_listener.packets()
            mc_out = mc_listener.packets()
        assert not [
            p for p in mt_out if probe in (p.get("text") or "")
        ], "own-account echo crossed to Meshtastic"
        assert not [
            p for p in mc_out if probe in (p.get("text") or "")
        ], "own-account echo crossed to MeshCore"
        ids = await app.storage.list_event_ids_page(after_event_id=None, limit=200)
        for eid in ids:
            ev = await app.storage.get(eid)
            assert not (
                ev and probe in (ev.payload or {}).get("body", "")
            ), "own-account echo entered the canonical pipeline"
    finally:
        await _stop(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_restart_preserves_crypto_and_device_identity(tmp_path: Path) -> None:
    """One controlled restart: same olm store, same device, observer decrypts."""
    db_path = tmp_path / "lab.db"
    devices_before = observer_account_devices()

    app = await _launch(db_path, tmp_path / "lxmf_storage")
    nonce1 = _nonce("RESTART-A")
    try:
        await asyncio.to_thread(_mt_peer, ["sendn", _MT_PEER, json.dumps([nonce1])], 60)
        ev, receipts = await _wait_for_receipt(app, nonce1, "matrix", _RECEIPT_TIMEOUT)
        assert ev is not None and receipts, "pre-restart leg never delivered"
    finally:
        await _stop(app)

    # Genuine stop: the process boundary is crossed (launch builds a fresh
    # runtime from the SAME matrix olm store).
    app2 = await _launch(db_path, tmp_path / "lxmf_storage")
    nonce2 = _nonce("RESTART-B")
    try:
        with MatrixRoomObserver(
            _OBSERVER_WINDOW, "", str(tmp_path / "observer2.jsonl")
        ) as observer:
            sent = await asyncio.to_thread(
                _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce2])], 60
            )
            assert sent and sent[-1].get("sent_id"), "post-restart MT send not accepted"
            ev, receipts = await _wait_for_receipt(
                app2, nonce2, "matrix", _RECEIPT_TIMEOUT
            )
            assert ev is not None and receipts, "post-restart leg never delivered"
            result = observer.wait(timeout=_OBSERVER_WINDOW + 30)
            assert (
                result["undecryptable"] == 0
            ), f"post-restart observer could not decrypt: {result!r}"
            bodies = [e.get("body") or "" for e in observer.events()]
            assert any(
                nonce2 in b for b in bodies
            ), f"post-restart nonce never decrypted at observer: {bodies!r}"
            assert not any(
                nonce1 in b for b in bodies
            ), "pre-restart nonce replayed after restart"
    finally:
        await _stop(app2)

    devices_after = observer_account_devices()
    assert sorted(devices_after) == sorted(devices_before), (
        f"device identity drifted across restart: {devices_before!r} -> "
        f"{devices_after!r}"
    )
