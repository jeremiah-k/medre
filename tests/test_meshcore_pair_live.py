"""Live physical-pair tests for the MeshCore adapter (two real T-Beam nodes).

Opt-in ONLY: set ``MESHCORE_PAIR=1`` plus explicit owned BLE addresses.
The ordinary suite never opens a radio; nothing here provisions or pairs
as a fixture side effect (host-level bluetoothctl pairing is a documented
prerequisite, see docs/ops/live-validation/meshcore.md).

Roles (default lab allocation, reversible via env):
- MEDRE owns one board over BLE (``MESHCORE_MEDRE_BLE_ADDRESS``).
- The other board is an independent native peer driven by the pinned
  ``meshcore`` SDK directly — no MEDRE code in the peer path.

Run (opt-in): MESHCORE_PAIR=1 ... pytest tests/test_meshcore_pair_live.py
-m "live and hardware" -p no:unraisableexception
(``-p no:unraisableexception`` ignores bleak's cached BlueZ system-bus
socket being finalized after the session ends; it is an interpreter
cleanup artifact, not a test outcome.)

Fast iteration: add ``MEDRE_LIVE_QUICK=1`` to run each live test's core
positive evidence only (single messages, no absence/dedup/boundary
sections) — minutes become ~90 seconds. Full mode stays the proof gate.

Evidence layers asserted separately:
A. durable canonical events / native refs / delivery receipts in storage,
B. SDK/native acceptance semantics (MeshCore channel sends are
   local-accepted only — the firmware has no ACK protocol),
C. the independent peer's RF observation of correlated payloads.
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
from tests.helpers.meshcore_runtime import launch_healthy_meshcore_runtime
from tests.helpers.meshcore_live_peer import MeshCorePeerListener as _PeerListener
from tests.helpers.meshcore_live_peer import run_meshcore_peer as _peer
from tests.helpers.meshtastic import make_meshtastic_text_packet

# ---------------------------------------------------------------------------
# Environment gate and lab addressing
# ---------------------------------------------------------------------------
_PAIR_ENABLED = os.environ.get("MESHCORE_PAIR", "") == "1"

_MEDRE_BLE = os.environ.get("MESHCORE_MEDRE_BLE_ADDRESS", "")
_PEER_BLE = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_MEDRE_NODE_NAME = os.environ.get("MESHCORE_MEDRE_NODE_NAME", "MEDRE-MC-A")
# Fast-iteration tier: MEDRE_LIVE_QUICK=1 trims each live test to its core
# positive evidence (single messages, no absence/dedup/boundary sections).
# Absence windows and boundary matrices are the full-mode proof.
_QUICK = os.environ.get("MEDRE_LIVE_QUICK", "") == "1"
# The firmware prepends each sender's node name to group text on the wire;
# the peer board's name is asserted in ingress body attribution and must
# follow the lab role map (reversed roles rename both sides).
_PEER_NODE_NAME = os.environ.get("MESHCORE_PEER_NODE_NAME", "MEDRE-MC-B")

_REQUIRE_PAIR = pytest.mark.skipif(
    not (_PAIR_ENABLED and _MEDRE_BLE and _PEER_BLE),
    reason=(
        "opt-in physical pair: set MESHCORE_PAIR=1, MESHCORE_MEDRE_BLE_ADDRESS, "
        "MESHCORE_PEER_BLE_ADDRESS (owned boards; host-paired via bluetoothctl)"
    ),
)

# Bounded waits and lab pacing.  The MeshCore firmware imposes no Meshtastic
# style per-send floor, but the lab keeps a modest pacing to bound airtime.
_TX_PACING_SECONDS: float = 3.0
_RECEIPT_TIMEOUT: float = 30.0
_MEDRE_TEXT_BUDGET: int = 160  # MEDRE max_text_bytes (== firmware-visible span)


# ---------------------------------------------------------------------------
# In-process real runtime (built exactly like `medre run`)
# ---------------------------------------------------------------------------
def _build_runtime(db_path: Path, *, with_route: bool):
    from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter  # noqa: F401
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

    src = MeshtasticRuntimeConfig(
        adapter_id="lab_src",
        enabled=True,
        adapter_kind="fake",
        config=MeshtasticConfig(adapter_id="lab_src", connection_type="fake"),
    )
    radio = MeshCoreRuntimeConfig(
        adapter_id="mc_radio",
        enabled=True,
        adapter_kind="real",
        config=MeshCoreConfig(
            adapter_id="mc_radio",
            connection_type="ble",
            ble_address=_MEDRE_BLE,
            default_channel=1,
            message_delay_seconds=_TX_PACING_SECONDS,
            max_text_bytes=_MEDRE_TEXT_BUDGET,
            identity=_MEDRE_NODE_NAME,
        ).validate(),
    )
    routes = RouteConfigSet()
    if with_route:
        routes = RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="lab_egress",
                    source_adapters=("lab_src",),
                    dest_adapters=("mc_radio",),
                    source_channel="0",
                    dest_channel="1",
                ),
            )
        )
        routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="mc-pair-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            meshtastic={"lab_src": src}, meshcore={"mc_radio": radio}
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


async def _stop_app(app) -> None:  # noqa: ANN001
    await bounded(app.stop(), 30.0, "pair runtime app.stop()")


async def _launch(db_path: Path, *, with_route: bool):
    """Build a fresh runtime and require a healthy MeshCore link."""
    return await launch_healthy_meshcore_runtime(
        lambda: _build_runtime(db_path, with_route=with_route),
        start_timeout=100.0,
        start_label="pair runtime app.start()",
        stop_timeout=30.0,
        stop_label="pair runtime app.stop()",
        health_label="mc_radio health_check",
    )


# The pinned meshcore SDK still calls the deprecated
# asyncio.iscoroutinefunction inside its dispatcher; with the project's
# ``filterwarnings = ["error"]`` that raises *inside the SDK task* and
# kills event delivery.  Fix upstreamed on the meshcore_py fix branch;
# until the pin moves, ignore exactly that warning here.
pytestmark = [
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
]


def _nonce(prefix: str) -> str:
    return f"MEDRE {prefix}-{uuid.uuid4().hex[:10]}"


async def _preflight() -> None:
    """Fail fast with the exact remediation instead of burning BLE timeouts."""
    for addr, label in ((_MEDRE_BLE, "MEDRE"), (_PEER_BLE, "peer")):
        try:
            result = await asyncio.to_thread(_peer, ["probe", addr], 60)
        except AssertionError as exc:
            pytest.skip(
                f"{label} board {addr} not connectable "
                f"(single-connection slot busy or stale link): {exc}"
            )
        drift = result.get("drift_s")
        if drift is None or abs(drift) > 300:
            pytest.skip(
                f"{label} board {addr} clock not synced (drift {drift!r}s); "
                "run the lab clock-sync step before live MeshCore tests"
            )


async def _await_receipts(storage, event_id: str) -> list:  # noqa: ANN001
    """Poll durable delivery receipts for one event, bounded."""

    async def _poll() -> list:
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        while time.monotonic() < deadline:
            receipts = await storage.list_receipts_for_event(event_id)
            if receipts:
                return receipts
            await asyncio.sleep(0.5)
        return []

    return await _poll()


async def _events_with_body(
    app, needle: str, expected: int = 1  # noqa: ANN001
) -> list:
    """Boundedly poll durable canonical events whose body contains needle."""
    deadline = time.monotonic() + _RECEIPT_TIMEOUT
    hits: list = []
    while time.monotonic() < deadline:
        ids = await app.storage.list_event_ids_page(after_event_id=None, limit=200)
        hits = []
        for eid in ids:
            ev = await app.storage.get(eid)
            body = (ev.payload or {}).get("body", "") if ev else ""
            if needle in body:
                hits.append(ev)
        if len(hits) >= expected:
            return hits
        await asyncio.sleep(1.0)
    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
# Two runtime sessions total: ingress-core (no route) and egress-core (one
# route).  MeshCore BLE session start dominates wall time, so related native
# cases share one healthy runtime and separate concerns by unique nonces.


async def _event_count(app) -> int:  # noqa: ANN001
    ids = await app.storage.list_event_ids_page(after_event_id=None, limit=1000)
    return len(list(ids))


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestMeshCorePairIngress:
    """Independent native peer -> RF -> MEDRE durable admission (N1/N2/N5/N6)."""

    async def test_native_ingress_and_isolation_core(self, tmp_path: Path) -> None:
        await _preflight()
        app = await _launch(tmp_path / "lab.db", with_route=False)
        try:
            radio = app.adapters["mc_radio"]

            # -- N1: healthy lifecycle + bounded quiet window (no admission).
            info = await bounded(radio.health_check(), 15.0, "mc_radio health_check")
            assert info.health == "healthy", (
                f"mc_radio health {info.health!r}; "
                f"diagnostics {radio.diagnostics()!r}"
            )
            before = await _event_count(app)
            if not _QUICK:
                quiet = await asyncio.to_thread(_peer, ["listen", _PEER_BLE, "10"], 60)
                assert quiet["received"] == [], "unexpected RF traffic in quiet window"
                assert await _event_count(app) == before, "stale admission during quiet"

            # -- N2: exact content, unicode, newline; sender identity correlates.
            base = _nonce("N2")
            texts = [base + " plain"]
            if not _QUICK:
                texts = [
                    base + " plain",
                    base + " uni \u2713 \u4f60\u597d",
                    base + " nl a\nb",
                ]
            sent = await asyncio.to_thread(
                _peer, ["sendn", _PEER_BLE, json.dumps(texts)], 90
            )
            assert all(not item["error"] for item in sent["sent"]), "peer send rejected"
            for text in texts:
                hits = await _events_with_body(app, text)
                assert hits, f"message not durably admitted: {text[:24]!r}"
                ev = hits[-1]
                # MeshCore group text carries the sender node name on the
                # wire ("<peer-name>: <text>"); canonical body is the exact
                # wire text, so the nonce text must be the exact tail.
                assert ev.payload["body"].endswith(
                    text
                ), f"canonical body mismatch: {ev.payload['body']!r}"
                assert ev.payload["body"].startswith(
                    f"{_PEER_NODE_NAME}: "
                ), "firmware sender-name attribution missing"
                assert ev.source_adapter == "mc_radio"

            if _QUICK:
                # Quick tier: N2 core evidence only (single message).
                return

            # -- N5 controlled: sender-set wire timestamps, identical text.
            text_ident = _nonce("N5-ident")
            now = int(time.time())
            await asyncio.to_thread(
                _peer, ["sendts", _PEER_BLE, text_ident, str(now), str(now)], 90
            )
            hits = await _events_with_body(app, text_ident)
            assert (
                len(hits) == 1
            ), f"wire-identical same-second text produced {len(hits)} events"
            text_dist = _nonce("N5-distinct")
            now = int(time.time())
            await asyncio.to_thread(
                _peer, ["sendts", _PEER_BLE, text_dist, str(now), str(now + 1)], 90
            )
            hits = await _events_with_body(app, text_dist, expected=2)
            assert (
                len(hits) == 2
            ), f"distinct same-second timestamps produced {len(hits)} events"

            # -- N6: wrong-key channel probe is not admitted; restore + positive.
            probe = _nonce("N6-wrongkey")
            positive = _nonce("N6-positive")
            result = await asyncio.to_thread(
                _peer, ["n6probe", _PEER_BLE, probe, positive], 120
            )
            assert result.get("set_wrong"), "peer could not install wrong key"
            assert result.get("restore_ok"), "peer could not restore ch2"
            assert (
                result["ch2_after"]["secret_zeroed"]
                and not result["ch2_after"]["secret_is_wrong"]
            ), f"ch2 not restored to empty-channel default: {result['ch2_after']}"
            await asyncio.sleep(12.0)  # bounded absence window for the probe
            assert (
                await _events_with_body(app, probe) == []
            ), "wrong-key probe leaked into canonical admission"
            assert await _events_with_body(
                app, positive
            ), "positive ch1 delivery not admitted after restore"
        finally:
            await _stop_app(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestMeshCorePairEgress:
    """MEDRE -> RF -> independent native peer (controlled local source)."""

    async def test_native_egress_core(self, tmp_path: Path) -> None:
        """N3 routed egress with peer receipt + N4 documented boundaries."""
        await _preflight()
        app = await _launch(tmp_path / "lab.db", with_route=True)
        try:
            fake = app.adapters["lab_src"]

            # -- N3: routed event egresses; peer receives correlated nonce.
            nonce = _nonce("N3")
            packet = make_meshtastic_text_packet(
                text=nonce, sender="!peer0001", channel=0
            )
            with _PeerListener(_RECEIPT_TIMEOUT + 20) as peer:
                await fake.simulate_inbound(packet)
                event = fake.inbound_events[-1]
                receipts = await _await_receipts(app.storage, event.event_id)
                assert receipts, "no durable delivery receipt appeared"
                peer_out = peer.packets_until(
                    lambda ps: any(nonce in (p.get("text") or "") for p in ps),
                    45.0,
                )
                rx = [p for p in peer_out if nonce in (p.get("text") or "")]
            assert rx, (
                "native peer did not receive the egress over RF; "
                f"diagnostics={app.adapters['mc_radio'].diagnostics()!r} "
                f"receipts={[(r.status, r.adapter_message_id) for r in receipts]!r} "
                f"peer_out={peer_out!r}"
            )
            pkt = rx[-1]
            assert pkt["channel"] == 1, "egress arrived on the wrong channel"
            # Layer B: MeshCore channel sends are local-acceptance only
            # (documented: no ACK protocol).  RF receipt is layer C above.
            latest = max(receipts, key=lambda r: r.sequence)
            assert (
                latest.status == "sent"
            ), f"receipt status {latest.status!r}, error={latest.error!r}"
            assert latest.target_adapter == "mc_radio"
            assert latest.route_id == "lab_egress"

            if _QUICK:
                # Quick tier: N3 routed egress with peer receipt only.
                return

            # -- N4: unicode, newline, and the documented radio truncation.
            unicode_msg = _nonce("N4-uni") + " hello w\u00f6rld \u2713"
            newline_msg = _nonce("N4-nl") + " line1\nline2"
            long_msg = _nonce("N4-long") + " " + "x" * 300  # way over budget
            normal_msg = _nonce("N4-ok")
            cases = [unicode_msg, newline_msg, long_msg, normal_msg]
            events: dict[str, str] = {}
            pid = 800_000
            window = len(cases) * (_TX_PACING_SECONDS + 1.0) + 30.0
            with _PeerListener(window) as peer:
                for text in cases:
                    pid += 1  # unique native packet id per message (dedup)
                    packet = make_meshtastic_text_packet(
                        text=text, sender="!peer0001", channel=0, packet_id=pid
                    )
                    await fake.simulate_inbound(packet)
                    event = fake.inbound_events[-1]
                    events[text] = event.event_id
                    await asyncio.sleep(_TX_PACING_SECONDS + 1.0)
                received = peer.packets_until(
                    lambda ps: all(
                        key in "".join(p.get("text") or "" for p in ps)
                        for key in ("N4-uni", "N4-nl", "N4-long", "N4-ok")
                    ),
                    75.0,
                )
            by_nonce: dict[str, str] = {}
            for p in received:
                t = p.get("text") or ""
                for key in ("N4-uni", "N4-nl", "N4-long", "N4-ok"):
                    if key in t and key not in by_nonce:
                        by_nonce[key] = t
            assert "ö" in by_nonce.get("N4-uni", ""), "unicode payload degraded"
            assert "\n" in by_nonce.get("N4-nl", ""), "newline payload degraded"
            long_rx = by_nonce.get("N4-long")
            assert long_rx, "over-budget payload not observed at peer"
            assert (
                len(long_rx) <= 160
            ), f"peer saw {len(long_rx)} chars; firmware cap is 160"
            assert long_rx.startswith(
                f"{_MEDRE_NODE_NAME}: "
            ), "firmware attribution prefix missing"
            assert "N4-ok" in by_nonce, "adapter unusable after boundary cases"
            for text, eid in events.items():
                receipts = await app.storage.list_receipts_for_event(eid)
                assert receipts, f"no receipt for boundary case {text[:24]!r}"
        finally:
            await _stop_app(app)
