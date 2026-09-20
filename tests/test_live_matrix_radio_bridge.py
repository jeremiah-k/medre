"""Live Matrix<->radio bridge tests over the real MEDRE runtime (E2EE room).

Opt-in ONLY: ``MEDRE_MX_BRIDGE=1`` plus explicit owned endpoints (Matrix
credentials, radio peer endpoints).  Skipped by default; never runs in CI.

Topology (lab defaults, reversible via env):
- ONE MEDRE runtime owns four adapters: matrix (e2ee_required, private
  encrypted room), meshtastic (MT-A serial), meshcore (MC-A BLE), lxmf
  (LX-A via isolated RNS config).  One Matrix client, one device, one
  crypto store — the room is observed INTERNALLY (canonical storage,
  delivery receipts, runtime sync/diagnostics counters).
- Three explicit bidirectional routes: matrix<->each radio.  No radio<->radio
  routes exist, so there are no all-to-all echo cycles; the six directed
  Matrix<->radio paths are the coverage target.
- Radio-side peers (MT-B / MC-B / LX-B) are driven exclusively by the peer
  helpers as senders or listeners; they are the far-side radio evidence.
- Runtime-originated Matrix events return to the runtime's own sync
  timeline; the self-guard must suppress them.  That loopback is the
  positive control for every suppression claim in this module.
- A second bot-account device was tried as an external "observer"; bot-to-bot
  key sharing is not achievable with the account's trust state, and a second
  device of the same account was never independent evidence.  Independent
  recipient confirmation comes from the invited human in the collector
  window instead.

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

from tests.helpers.live_harness import (
    PINNED_SDK_UNRAISABLE_FILTERS,
    bounded,
)
from tests.helpers.lxmf_live_peer import delivery_dest_hash as _lx_dest_hash
from tests.helpers.lxmf_live_peer import run_lxmf_peer as _lx_peer
from tests.helpers.meshcore_live_peer import MeshCorePeerListener as _McListener
from tests.helpers.meshcore_live_peer import run_meshcore_peer as _mc_peer
from tests.helpers.meshtastic_live_peer import run_meshtastic_peer as _mt_peer

_BRIDGE = os.environ.get("MEDRE_MX_BRIDGE", "") == "1"

_MATRIX_HS = os.environ.get("MATRIX_HOMESERVER", "")
_MATRIX_USER = os.environ.get("MATRIX_USER_ID", "")
_MATRIX_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN", "")
_MATRIX_ROOM = os.environ.get("MATRIX_ROOM_ID", "")
_MATRIX_STORE = os.environ.get("MATRIX_STORE_PATH", "")

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
        "MESHTASTIC_MEDRE_SERIAL_PORT, "
        "MESHTASTIC_PEER_SERIAL_PORT, MESHCORE_MEDRE_BLE_ADDRESS, "
        "MESHCORE_PEER_BLE_ADDRESS, LXMF_MEDRE_RNS_CONFIG, "
        "LXMF_MEDRE_IDENTITY, LXMF_MEDRE_STORAGE, LXMF_PEER_RNS_CONFIG, "
        "LXMF_PEER_IDENTITY"
    ),
)

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 60.0

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
    # Pinned-SDK unraisable boundaries (RNS ratchets fd leak, aiohttp
    # TLS-shutdown linger, bleak system-bus socket): canonical strings live
    # in tests/helpers/live_harness.py with the boundary documentation, and
    # tests/test_live_harness.py pins that each spec stays loadable by
    # pytest (a malformed spec is a run-fatal INTERNALERROR) and narrow.
    *(pytest.mark.filterwarnings(spec) for spec in PINNED_SDK_UNRAISABLE_FILTERS),
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


def _loopback_snapshot(app) -> dict[str, int]:
    """Internal room-observation counters for the runtime's Matrix adapter."""
    diag = app.adapters["matrix"].diagnostics()
    return {
        "self_suppressed": diag["inbound_suppressed_self"],
        "undecryptable": diag["undecryptable_event_count"],
    }


async def _wait_self_suppressed_at_least(
    app, baseline: dict[str, int], count: int, timeout: float
) -> int:
    """Bounded wait until the runtime's own sync loopback has been suppressed
    at least *count* times; returns the observed delta."""
    deadline = time.monotonic() + timeout
    delta = 0
    while time.monotonic() < deadline:
        delta = _loopback_snapshot(app)["self_suppressed"] - baseline["self_suppressed"]
        if delta >= count:
            return delta
        await asyncio.sleep(1.0)
    return delta


async def _canonical_probe_count(app, nonce: str) -> int:
    ids = await app.storage.list_event_ids_page(after_event_id=None, limit=200)
    matches = 0
    for eid in ids:
        ev = await app.storage.get(eid)
        if ev and nonce in (ev.payload or {}).get("body", ""):
            matches += 1
    return matches


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
async def test_radio_to_matrix_three_legs_relayed_encrypted(
    tmp_path: Path,
) -> None:
    """MT-B / MC-B / LX-B -> MEDRE -> encrypted room, observed internally."""
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
    baseline = _loopback_snapshot(app)
    try:
        # NOTE: no radio-side listeners here -- the sender needs the
        # peer device exclusively (pyserial flock / single BLE central /
        # RNode serial).  The far-side evidence for the radio->matrix
        # legs is the matrix delivery receipt plus the runtime's own
        # internal room observation below.
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
                    assert any(
                        isinstance(e, dict) and e.get("sent_id") for e in sent
                    ), "MT send not accepted"
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
                            json.dumps([nonce + " / \u00fcn\u00efcode \u2713\nline2"]),
                        ],
                        120,
                    )
                    assert sent.get("sent"), f"LX send not accepted: {sent!r}"

                # LX direct delivery includes first-path discovery; the
                # established pair convention allows 90-120s.
                lx_timeout = 120.0 if transport == "LXMF" else _RECEIPT_TIMEOUT
                ev, receipts = await _wait_for_receipt(app, nonce, "matrix", lx_timeout)
                assert ev is not None, f"canonical event for {nonce!r} never appeared"
                latest = max(receipts, key=lambda r: r.sequence)
                assert (
                    latest.status == "sent"
                ), f"matrix receipt status {latest.status!r}"
            except AssertionError as exc:
                failures.append(f"{tag} leg: {exc}")

        # Internal room evidence: every relayed leg is live in the room AND
        # returns to the runtime's own sync timeline, where the self-guard
        # must suppress it. The suppressed-loopback delta is the positive
        # control that the runtime actually received its own traffic;
        # a rising undecryptable counter would mean Megolm decryption
        # degraded in-window.
        loopback_delta = await _wait_self_suppressed_at_least(
            app, baseline, len(ran_tags), 45.0
        )
        if ran_tags and loopback_delta < len(ran_tags):
            failures.append(
                f"own-loopback suppressed {loopback_delta} < "
                f"{len(ran_tags)} relayed leg(s); runtime never observed "
                "its own relays via sync"
            )
        undecryptable_delta = (
            _loopback_snapshot(app)["undecryptable"] - baseline["undecryptable"]
        )
        if undecryptable_delta:
            failures.append(
                f"runtime undecryptable events rose by {undecryptable_delta} "
                "in-window (Megolm decryption health)"
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
    assert not failures, f"{len(failures)} leg/loopback failure(s): " + " | ".join(
        failures
    )


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_own_relayed_message_loopback_is_suppressed_not_relayed(
    tmp_path: Path,
) -> None:
    """The runtime's own Matrix traffic must not loop back through the bridge.

    Positive control first: a radio nonce is relayed into the encrypted room
    (canonical event + ``sent`` receipt prove the relay).  That relayed event
    then returns to the runtime's OWN sync timeline; the self-guard must
    consume it — observable as an ``inbound_suppressed_self`` increment —
    without a second canonical admission and without fanning back out to the
    other radios.  Suppression claims are therefore anchored to an observed
    loopback, not to silence.
    """
    app = await _launch(tmp_path / "lab.db", tmp_path / "lxmf_storage")
    baseline = _loopback_snapshot(app)
    probe = _nonce("ECHO")
    try:
        sent = await asyncio.to_thread(
            _mt_peer, ["sendn", _MT_PEER, json.dumps([probe])], 60
        )
        assert any(
            isinstance(e, dict) and e.get("sent_id") for e in sent
        ), "MT send not accepted"
        ev, receipts = await _wait_for_receipt(app, probe, "matrix", _RECEIPT_TIMEOUT)
        assert ev is not None and receipts, "relay never delivered"
        assert await _canonical_probe_count(app, probe) == 1, (
            "relayed probe admitted more than once (duplicate ingress before "
            "the loopback even returned)"
        )

        # The loopback: own event arrives via sync while MC-B listens to
        # prove nothing fans back out over the radio.
        with _McListener(45) as mc_listener:
            loopback_delta = await _wait_self_suppressed_at_least(
                app, baseline, 1, 30.0
            )
            await asyncio.sleep(10)
            mc_out = mc_listener.packets()
        assert loopback_delta >= 1, (
            "own relayed event never returned via the runtime's own sync "
            "(loopback unobservable; suppression would be unproven)"
        )
        assert (
            await _canonical_probe_count(app, probe) == 1
        ), "loopback entered the canonical pipeline a second time"
        assert not [
            p for p in mc_out if probe in (p.get("text") or "")
        ], "own relayed message fanned back out to MeshCore"
        final = _loopback_snapshot(app)
        assert (
            final["undecryptable"] == baseline["undecryptable"]
        ), "runtime undecryptable count moved (Megolm decryption health)"
    finally:
        await _stop(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE
async def test_restart_preserves_crypto_and_device_identity(tmp_path: Path) -> None:
    """One controlled restart: same olm store, same device, fresh encrypted
    ingress and egress, observed internally."""
    db_path = tmp_path / "lab.db"

    app = await _launch(db_path, tmp_path / "lxmf_storage")
    device_before = app.adapters["matrix"].diagnostics()["device_id_in_use"]
    nonce1 = _nonce("RESTART-A")
    try:
        await asyncio.to_thread(_mt_peer, ["sendn", _MT_PEER, json.dumps([nonce1])], 60)
        ev, receipts = await _wait_for_receipt(app, nonce1, "matrix", _RECEIPT_TIMEOUT)
        assert ev is not None and receipts, "pre-restart leg never delivered"
    finally:
        await _stop(app)

    # Controlled stop/start: a fresh runtime is built from the SAME matrix
    # olm store and database.
    app2 = await _launch(db_path, tmp_path / "lxmf_storage")
    nonce2 = _nonce("RESTART-B")
    try:
        baseline = _loopback_snapshot(app2)
        sent = await asyncio.to_thread(
            _mt_peer, ["sendn", _MT_PEER, json.dumps([nonce2])], 60
        )
        assert any(
            isinstance(e, dict) and e.get("sent_id") for e in sent
        ), "post-restart MT send not accepted"
        ev, receipts = await _wait_for_receipt(app2, nonce2, "matrix", _RECEIPT_TIMEOUT)
        assert ev is not None and receipts, "post-restart leg never delivered"
        # Fresh encrypted egress is live: the post-restart relay returns to
        # the runtime's own sync and is suppressed exactly like pre-restart.
        loopback_delta = await _wait_self_suppressed_at_least(app2, baseline, 1, 45.0)
        assert loopback_delta >= 1, (
            "post-restart own-loopback unobserved; crypto/session continuity "
            "after restart is unproven"
        )
        final = _loopback_snapshot(app2)
        assert (
            final["undecryptable"] == baseline["undecryptable"]
        ), "post-restart runtime undecryptable count moved"
        # No unintended replay: the pre-restart nonce stays admitted exactly
        # once and the post-restart nonce exactly once.
        assert (
            await _canonical_probe_count(app2, nonce1) == 1
        ), "pre-restart nonce replayed into canonical storage after restart"
        assert (
            await _canonical_probe_count(app2, nonce2) == 1
        ), "post-restart nonce admitted more than once"
        device_after = app2.adapters["matrix"].diagnostics()["device_id_in_use"]
        assert device_after == device_before, (
            f"device identity drifted across restart: "
            f"{device_before!r} -> {device_after!r}"
        )
    finally:
        await _stop(app2)
