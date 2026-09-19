"""Live physical-pair tests for the Meshtastic adapter (two real nodes).

This module is the opt-in harness for **native pair evidence** (campaign
cases N3/N4/N5 in the Meshtastic direction).  It is skipped by default and
requires two physical Meshtastic nodes on the private lab mesh:

- **MEDRE side** — one node owned by a real in-process MEDRE runtime
  (real serial adapter, real storage, real route/render/delivery
  pipeline), built exactly like ``medre run`` builds it.
- **Peer side** — a second node driven as an **independent native peer**
  by the pinned mtjk SDK in a subprocess.  The peer never uses MEDRE
  code, so its receive/send observations are independent evidence
  (evidence layer C in the docs evidence-levels model).

Ingress source caveat (honest labelling): the MEDRE-egress cases drive
ingress through a ``fake`` Meshtastic adapter wired into the same runtime
with a real route to the real adapter.  This is a *controlled local
source* — required because a single-transport pair cannot route
``mt_a → mt_a`` (config forbids source/dest overlap).  Everything after
the source event is fully real: durable storage, route matching,
rendering, delivery, SDK send, RF transmission, and independent peer
receipt.  Cross-transport bridge cases (B*) provide fully-native egress
sources once a second transport is commissioned.

Environment (all required for this module to run):

===============================  =============================================
Variable                         Description
===============================  =============================================
``MESHTASTIC_CONNECTION_TYPE``   Must be ``serial``
``MESHTASTIC_SERIAL_PORT``       Serial device of the MEDRE-owned node
``MESHTASTIC_PEER_SERIAL_PORT``  Serial device of the independent native peer
``MESHTASTIC_LIVE_SEND``         Must be ``1`` (these tests transmit RF)
``MESHTASTIC_PAIR_TX_BUDGET``    Optional max RF sends per test (default 6)
===============================  =============================================

RF safety: all tests run on the private lab channel at bench power, use
small non-secret nonce payloads, pace outbound traffic at >= 2.2 s per
message (empirical firmware/airtime requirement for the lab pair), and
are bounded by ``MESHTASTIC_PAIR_TX_BUDGET``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.meshtastic import make_meshtastic_text_packet

# ---------------------------------------------------------------------------
# Environment gate
# ---------------------------------------------------------------------------
_CONNECTION_TYPE = os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower()
_MEDRE_PORT = os.environ.get("MESHTASTIC_SERIAL_PORT")
_PEER_PORT = os.environ.get("MESHTASTIC_PEER_SERIAL_PORT")
_LIVE_SEND = os.environ.get("MESHTASTIC_LIVE_SEND", "") == "1"
_TX_BUDGET = int(os.environ.get("MESHTASTIC_PAIR_TX_BUDGET", "6"))

_REQUIRE_PAIR = pytest.mark.skipif(
    _CONNECTION_TYPE != "serial" or not _MEDRE_PORT or not _PEER_PORT or not _LIVE_SEND,
    reason=(
        "Set MESHTASTIC_CONNECTION_TYPE=serial, MESHTASTIC_SERIAL_PORT, "
        "MESHTASTIC_PEER_SERIAL_PORT and MESHTASTIC_LIVE_SEND=1 to run "
        "physical-pair Meshtastic tests"
    ),
)

# Empirical pacing for the lab pair (see docs/ops/live-validation/meshtastic.md):
# below ~2.2 s between sends the firmware drops every second message.
_TX_PACING_SECONDS: float = 2.5

# Bounded waits (seconds).
_RECEIPT_TIMEOUT: float = 45.0
_PEER_STARTUP_GRACE: float = 8.0


# ---------------------------------------------------------------------------
# Independent native peer (pinned mtjk SDK, no MEDRE code)
# ---------------------------------------------------------------------------
_PEER_SCRIPT = r"""
import json, sys, time
mode, port = sys.argv[1], sys.argv[2]
from meshtastic.serial_interface import SerialInterface
iface = SerialInterface(devPath=port, noProto=False, debugOut=None)
from pubsub import pub
got = []
def on_packet(packet, interface=None):
    d = packet.get("decoded", {}) or {}
    if d.get("portnum") == "TEXT_MESSAGE_APP" or "text" in d:
        got.append({
            "ts": time.time(),
            "id": packet.get("id"),
            "from": packet.get("fromId"),
            "to": packet.get("toId"),
            "channel": packet.get("channel"),
            "text": d.get("text"),
            "rx_snr": packet.get("rxSnr"),
        })
pub.subscribe(on_packet, "meshtastic.receive")
if mode == "listen":
    secs = float(sys.argv[3])
    deadline = time.time() + secs
    while time.time() < deadline:
        time.sleep(0.2)
elif mode == "sendn":
    texts = json.loads(sys.argv[3])
    for t in texts:
        p = iface.sendText(t, channelIndex=0, wantAck=False)
        got.append({"sent_id": p.id if p else None, "text": t})
        time.sleep(2.5)
iface.close()
print(json.dumps(got))
"""


def _peer(args: list[str], timeout: float) -> list[dict]:
    """Run the native peer script once and return its JSON payload."""
    proc = subprocess.run(
        [sys.executable, "-c", _PEER_SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"native peer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"native peer produced no JSON: {proc.stderr[-400:]}")
    return json.loads(lines[-1])


class _PeerListener:
    """Background native-peer listener with bounded collection."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "_PeerListener":
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                "listen",
                _PEER_PORT,
                str(self._seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Give the peer's serial connection time to establish before any TX.
        time.sleep(_PEER_STARTUP_GRACE)
        return self

    def packets(self, timeout: float | None = None) -> list[dict]:
        out, err = self._proc.communicate(timeout=timeout or self._seconds + 30)
        if self._proc.returncode != 0:
            raise AssertionError(f"native peer listener failed: {err[-800:]}")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else []

    def __exit__(self, *exc: object) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# In-process real runtime (built exactly like `medre run`)
# ---------------------------------------------------------------------------


def _build_runtime(db_path: Path, *, with_route: bool):
    from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter  # noqa: F401
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
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
    radio = MeshtasticRuntimeConfig(
        adapter_id="mt_radio",
        enabled=True,
        config=MeshtasticConfig(
            adapter_id="mt_radio",
            connection_type="serial",
            serial_port=_MEDRE_PORT,
            default_channel=0,
            # Empirical RF pacing for the lab pair (user-directed 2.2 s floor).
            message_delay_seconds=_TX_PACING_SECONDS,
        ).validate(),
    )
    routes: RouteConfigSet = RouteConfigSet()
    if with_route:
        routes = RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="lab_egress",
                    source_adapters=("lab_src",),
                    dest_adapters=("mt_radio",),
                    source_channel="0",
                    dest_channel="0",
                ),
            )
        )
        routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="mt-pair-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(meshtastic={"lab_src": src, "mt_radio": radio}),
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


async def _start_app(app) -> None:  # noqa: ANN001
    await bounded(app.start(), 30.0, "pair runtime app.start()")


async def _stop_app(app) -> None:  # noqa: ANN001
    await bounded(app.stop(), 30.0, "pair runtime app.stop()")


async def _await_receipts(storage, event_id: str) -> list:  # noqa: ANN001
    """Poll durable delivery receipts for one event, bounded."""

    async def _poll() -> list:
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        while time.monotonic() < deadline:
            receipts = await storage.list_receipts_for_event(event_id)
            if receipts:
                return receipts
            await asyncio.sleep(1.0)
        return []

    return await _poll()


def _nonce(prefix: str) -> str:
    return f"MEDRE {prefix}-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestMeshtasticPairEgress:
    """MEDRE → RF → independent native peer (controlled local source)."""

    async def test_egress_receipt_by_native_peer(self, tmp_path: Path) -> None:
        """N3: a routed event egresses MT radio and the peer receives it.

        Asserts three separately-observed layers: (A) durable canonical
        event + delivery receipt in storage, (B) SDK/native packet-id
        acceptance, (C) independent native peer RF receipt with the nonce.
        """
        app = _build_runtime(tmp_path / "lab.db", with_route=True)
        await _start_app(app)
        try:
            fake = app.adapters["lab_src"]
            nonce = _nonce("N3")
            packet = make_meshtastic_text_packet(
                text=nonce, sender="!peer0001", channel=0
            )
            with _PeerListener(_RECEIPT_TIMEOUT + 20) as peer:
                await fake.simulate_inbound(packet)
                event = fake.inbound_events[-1]
                receipts = await _await_receipts(app.storage, event.event_id)
                assert receipts, "no durable delivery receipt appeared"
                rx = [p for p in peer.packets() if nonce in (p.get("text") or "")]
            assert rx, "native peer did not receive the egress over RF"
            pkt = rx[-1]
            # RX packet dicts may omit `channel` for the primary channel.
            assert pkt["channel"] in (0, None)
            # Peer heard it from the MEDRE-owned node, not the fake sender.
            assert pkt["from"] != "!peer0001"
            # Layer B: native packet id recorded in the durable receipt.
            # Meshtastic egress tops out at "sent" (SDK acceptance) — RF
            # receipt is layer C, observed by the independent peer above.
            latest = max(receipts, key=lambda r: r.sequence)
            assert (
                latest.status == "sent"
            ), f"receipt status {latest.status!r}, error={latest.error!r}"
            assert (
                latest.adapter_message_id
            ), "receipt carries no native/adapter message id"
            assert latest.target_adapter == "mt_radio"
            assert latest.route_id == "lab_egress"
        finally:
            await _stop_app(app)

    async def test_egress_payload_boundaries(self, tmp_path: Path) -> None:
        """N4: unicode, newline, and the documented byte-boundary truncation.

        The renderer truncates final radio text to ``max_text_bytes``
        (default 227) on UTF-8 boundaries.  The peer must observe exactly
        that degradation, and a normal follow-up message must succeed
        (adapter remains usable after a boundary case).
        """
        app = _build_runtime(tmp_path / "lab.db", with_route=True)
        await _start_app(app)
        try:
            fake = app.adapters["lab_src"]
            unicode_msg = _nonce("N4-uni") + " héllo wörld ✓ 你好"
            newline_msg = _nonce("N4-nl") + "line1\nline2"
            long_msg = _nonce("N4-long") + " " + "αβγδε" * 120  # ~720 bytes
            normal_msg = _nonce("N4-ok")
            cases = [unicode_msg, newline_msg, long_msg, normal_msg]
            assert len(cases) <= _TX_BUDGET
            events: dict[str, str] = {}
            pid = 900_000
            window = (
                _PEER_STARTUP_GRACE + len(cases) * (_TX_PACING_SECONDS + 0.6) + 40.0
            )
            with _PeerListener(window) as peer:
                for text in cases:
                    pid += 1  # unique native packet id per message (dedup)
                    packet = make_meshtastic_text_packet(
                        text=text, sender="!peer0001", channel=0, packet_id=pid
                    )
                    await fake.simulate_inbound(packet)
                    event = fake.inbound_events[-1]
                    events[text] = event.event_id
                    # Pace ingress so outbound queue + airtime stay stable.
                    await asyncio.sleep(_TX_PACING_SECONDS + 0.6)
                received = peer.packets()
            by_nonce = {}
            for p in received:
                t = p.get("text") or ""
                for key in ("N4-uni", "N4-nl", "N4-long", "N4-ok"):
                    if key in t and key not in by_nonce:
                        by_nonce[key] = t
            # Unicode multibyte survives intact.
            assert "N4-uni" in by_nonce and "你好" in by_nonce["N4-uni"]
            # Newline content survives.
            assert "N4-nl" in by_nonce and "\n" in by_nonce["N4-nl"]
            # Long payload is delivered truncated (not dropped, not split).
            long_rx = by_nonce.get("N4-long")
            assert long_rx, "long payload not observed at peer"
            assert len(long_rx.encode("utf-8")) <= 240, "peer saw more than max bytes"
            assert long_rx.endswith(
                ("α", "β", "γ", "δ", "ε")
            ), "truncation split a multibyte codepoint"
            # Adapter remains usable after the boundary cases.
            assert "N4-ok" in by_nonce
            for text, eid in events.items():
                receipts = await app.storage.list_receipts_for_event(eid)
                assert receipts, f"no receipt for boundary case {text[:24]!r}"
        finally:
            await _stop_app(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestMeshtasticPairIngress:
    """Independent native peer → RF → MEDRE durable admission."""

    async def test_ingress_unicode_newline_and_identity(self, tmp_path: Path) -> None:
        """N4/N5 ingress: exact content, multibyte, newline, distinct ids.

        Two messages with identical text but different native packet ids
        must both be admitted (no false dedup).  Unicode and newline
        payloads must be preserved exactly in canonical content.
        """
        app = _build_runtime(tmp_path / "lab.db", with_route=False)
        await _start_app(app)
        try:
            base = _nonce("N45")
            uni = base + " uni ✓ 你好"
            nl = base + " nl a\nb"
            identical = _nonce("N5-same")
            # The peer script paces its own sends (2.5 s apart).
            sent = _peer(
                ["sendn", _PEER_PORT, json.dumps([uni, nl, identical, identical])],
                timeout=120,
            )
            sent_ids = [s["sent_id"] for s in sent]
            assert all(sent_ids), "peer failed to submit a send"

            async def _admitted(packet_id: str) -> dict | None:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    ref = await app.storage.resolve_native_ref(
                        "mt_radio", "0", str(packet_id)
                    )
                    if ref:
                        ev = await app.storage.get(ref)
                        return ev
                    await asyncio.sleep(1.0)
                return None

            for text, pid in zip((uni, nl), sent_ids[:2], strict=True):
                ev = await _admitted(str(pid))
                assert ev is not None, f"peer message {text[:20]!r} not admitted"
                assert ev.payload["body"] == text
            # Identical text, distinct packet ids -> distinct durable events.
            ev1 = await _admitted(str(sent_ids[2]))
            ev2 = await _admitted(str(sent_ids[3]))
            assert ev1 is not None and ev2 is not None
            assert ev1.event_id != ev2.event_id
        finally:
            await _stop_app(app)
