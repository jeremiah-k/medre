"""Live physical-pair tests for the Meshtastic adapter (two real nodes).

This module is the opt-in harness for **native pair evidence** (campaign
cases N2/N3/N4/N5 in the Meshtastic direction).  It is skipped by default and
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
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.meshtastic_live_peer import (
    MeshtasticPeerListener as _PeerListener,
    run_meshtastic_peer as _peer,
)
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
                len(cases) * (_TX_PACING_SECONDS + 0.6) + 40.0
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
            from medre.config.adapters.meshtastic import MeshtasticConfig

            max_bytes = MeshtasticConfig(adapter_id="budget-probe").max_text_bytes
            expected_long = long_msg.encode("utf-8")[:max_bytes].decode(
                "utf-8", errors="ignore"
            )
            assert long_rx == expected_long, (
                "peer did not observe the exact UTF-8-safe configured truncation"
            )
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
            # The peer script paces its own sends (2.5 s apart).  Enforce
            # the operator-defined RF budget before the peer transmits.
            texts = [uni, nl, identical, identical]
            assert len(texts) <= _TX_BUDGET
            sent = _peer(
                ["sendn", _PEER_PORT, json.dumps(texts)],
                timeout=120,
            )
            sent_ids = [item["sent_id"] for item in sent]
            assert all(sent_ids), "peer failed to submit a send"
            sender_ids = {item.get("sender_id") for item in sent}
            assert len(sender_ids) == 1 and None not in sender_ids, (
                f"peer did not report one native sender id: {sender_ids!r}"
            )
            expected_sender = next(iter(sender_ids))

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
                assert ev.source_transport_id == expected_sender
            # Identical text, distinct packet ids -> distinct durable events.
            ev1 = await _admitted(str(sent_ids[2]))
            ev2 = await _admitted(str(sent_ids[3]))
            assert ev1 is not None and ev2 is not None
            assert ev1.source_transport_id == expected_sender
            assert ev2.source_transport_id == expected_sender
            assert ev1.event_id != ev2.event_id
        finally:
            await _stop_app(app)
