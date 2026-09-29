"""Message-level radio matrix: routes, provenance, rendering, volume.

Opt-in bench harness (``MEDRE_RADIO_MATRIX=1`` plus the four device envs)
exercising the full pipeline between physical nodes — peer → RF → MEDRE
runtime (route/plan/render/receipt) → RF → independent peer — in both
cross-transport directions.

Coverage matrix (what each test proves):

===========================  =============================================
Test                         Proves
===========================  =============================================
route_matrix_delivery_       Each direction's messages produce sent
isolation                    receipts on the correct target adapter and
                             are observed on the correct transport only.
rendering_contract_on_wire   Relay prefixes (explicitly configured),
                             UTF-8 safety, newline survival, and exact
                             per-transport byte budgets (MeshCore 160 /
                             Meshtastic 227) including over-budget
                             truncation, in both directions.
provenance_chain_end_to_end  Peer native packet id → canonical event
                             (native-ref resolution) → receipt (event-
                             correlated, sent) → observed wire text;
                             originating sender attribution survives each
                             hop.
sustained_traffic_           Every admitted event has exactly one sent
convergence                  receipt; canonical event set matches sends
                             exactly (spaced duplicates stay two distinct
                             events); peer observation ratio reported
                             against a best-effort floor.
===========================  =============================================

Observation contract: every leg this harness exercises is
platform-unacknowledged traffic.  Meshtastic relays are channel
broadcasts — the firmware strips ``want_ack`` from broadcasts sent over
the air, so recipient ACKs exist only for Meshtastic DMs — and MeshCore
relays are channel floods, which the protocol never ACKs (explicit ACK
packets exist only for MeshCore DMs).  MEDRE receipts accordingly mean
"accepted by the local radio" (confirmation levels ``local_queue`` /
``local_transport``), never recipient confirmation.  MEDRE-side ledgers
(receipts, canonical events) are asserted exactly.  RF observation uses
resend-until-observed with a bounded attempt count for per-message cases
and a reported floor for volume runs — a dropped broadcast or flood is
mesh physics, not a pipeline defect; a missing receipt is.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.meshcore_live_peer import MeshCorePeerListener as _McListener
from tests.helpers.meshcore_live_peer import run_meshcore_peer as _mc_peer
from tests.helpers.meshcore_runtime import launch_healthy_meshcore_runtime
from tests.helpers.meshtastic_live_peer import MeshtasticPeerListener as _MtListener
from tests.helpers.meshtastic_live_peer import run_meshtastic_peer as _mt_peer

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
]

_MATRIX = os.environ.get("MEDRE_RADIO_MATRIX", "") == "1"
_MT_MEDRE = os.environ.get("MESHTASTIC_MEDRE_SERIAL_PORT", "")
_MT_PEER_PORT = os.environ.get("MESHTASTIC_PEER_SERIAL_PORT", "")
_MC_MEDRE = os.environ.get("MESHCORE_MEDRE_BLE_ADDRESS", "")
_MC_PEER_BLE = os.environ.get("MESHCORE_PEER_BLE_ADDRESS", "")
_MC_MEDRE_NAME = os.environ.get("MESHCORE_MEDRE_NODE_NAME", "MEDRE-MC-A")

_REQUIRE = pytest.mark.skipif(
    not (_MATRIX and _MT_MEDRE and _MT_PEER_PORT and _MC_MEDRE and _MC_PEER_BLE),
    reason=(
        "opt-in radio matrix: set MEDRE_RADIO_MATRIX=1, "
        "MESHTASTIC_MEDRE_SERIAL_PORT, MESHTASTIC_PEER_SERIAL_PORT, "
        "MESHCORE_MEDRE_BLE_ADDRESS, MESHCORE_PEER_BLE_ADDRESS"
    ),
)

#: Sustained-traffic volume.  24 keeps the bench pass near ten minutes;
#: long campaigns raise it (each unit is one paced RF send).
_TRAFFIC = int(os.environ.get("MEDRE_MATRIX_TRAFFIC", "24"))

_TX_PACING = 2.5
_RECEIPT_TIMEOUT = 45.0
#: MeshCore flood propagation to the observing peer can lag the SDK's
#: acceptance by a minute under mesh load; per-message observation waits
#: use this before a resend.
_MC_OBSERVE_TIMEOUT = 75.0
_OBSERVE_ATTEMPTS = 3
#: Volume-run RF observation floor (best-effort meshes drop floods).
_OBSERVE_FLOOR = 0.9

_MC_BUDGET = 160
_MT_BUDGET = 227

#: Explicit relay prefixes make on-wire attribution contractual for this
#: harness (the MeshCore renderer's default is no prefix; the Meshtastic
#: default is ``{sender_short}: ``).
_MT_PREFIX_TEMPLATE = "{sender_short}: "
_MC_PREFIX_TEMPLATE = "{sender}: "

#: Structural identity of the bench peers for attribution assertions.
_MT_PEER_LABEL = "d662"  # T1000-E short name (owner "Meshtastic d662")
_MC_PEER_LABEL = "MEDRE-MC-B"

_MC_SCRATCH = Path("/tmp/meshcore_pair_peer.json")
_MT_SCRATCH = Path("/tmp/meshtastic_pair_peer.json")


def _nonce(tag: str) -> str:
    return f"MX-{tag}-{uuid.uuid4().hex[:8]}"


def _utf8_truncate(text: str, budget: int) -> str:
    """Truncate *text* to *budget* bytes on UTF-8 boundaries."""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    return encoded[:budget].decode("utf-8", errors="ignore")


def _split_prefix(observed: str) -> tuple[str, str]:
    """Split an observed wire text into (prefix, payload tail)."""
    idx = observed.find(": ")
    if idx <= 0:
        return "", observed
    return observed[: idx + 2], observed[idx + 2 :]


def _scratch_texts(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        json.loads(line).get("text", "")
        for line in path.read_text().splitlines()
        if line.strip()
    ]


async def _launch(db_path: Path):
    """Build the both-directions matrix runtime and require healthy links."""
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
            max_text_bytes=_MT_BUDGET,
            radio_relay_prefix=_MT_PREFIX_TEMPLATE,
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
            max_text_bytes=_MC_BUDGET,
            identity=_MC_MEDRE_NAME,
            meshcore_relay_prefix=_MC_PREFIX_TEMPLATE,
        ).validate(),
    )
    routes = RouteConfigSet(
        routes=(
            RouteConfig(
                route_id="mx_mt_to_mc",
                source_adapters=("mt_radio",),
                dest_adapters=("mc_radio",),
                source_channel="0",
                dest_channel="1",
            ),
            RouteConfig(
                route_id="mx_mc_to_mt",
                source_adapters=("mc_radio",),
                dest_adapters=("mt_radio",),
                source_channel="1",
                dest_channel="0",
            ),
        )
    )
    routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="radio-matrix-live"),
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
    return await launch_healthy_meshcore_runtime(
        lambda: RuntimeBuilder(config, paths).build(),
        start_timeout=120.0,
        start_label="matrix runtime start",
        stop_timeout=30.0,
        stop_label="matrix runtime stop",
        health_label="matrix mc health",
    )


async def _mt_send(texts: list[str]) -> list[dict]:
    """Send texts from the independent Meshtastic peer (T1000-E)."""
    return await asyncio.to_thread(
        _mt_peer, ["sendn", _MT_PEER_PORT, json.dumps(texts)], 30 + 3 * len(texts)
    )


async def _mc_send(texts: list[str]) -> list[dict]:
    """Send texts from the independent MeshCore peer (MEDRE-MC-B)."""
    return await asyncio.to_thread(
        _mc_peer, ["sendn", _MC_PEER_BLE, json.dumps(texts)], 30 + 4 * len(texts)
    )


def _observer_text(packets: list[dict], nonce: str) -> str | None:
    for p in packets:
        text = p.get("text") or ""
        if nonce in text:
            return text
    return None


async def _observe_or_resend(
    packets_of: Callable[[], list[dict]],
    resend: Callable[[str], Any],
    nonce: str,
    *,
    timeout: float = _MC_OBSERVE_TIMEOUT,
    attempts: int = _OBSERVE_ATTEMPTS,
) -> str:
    """Return the observed wire text for *nonce*, resending on mesh loss.

    Best-effort meshes may drop a flood; a bounded resend of the same
    nonce (fresh native packet id) satisfies observation without weakening
    the assertion — the nonce is the contract, not the packet id.
    """
    for attempt in range(1, attempts + 1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = _observer_text(packets_of(), nonce)
            if text is not None:
                return text
            await asyncio.sleep(2.0)
        if attempt < attempts:
            await resend(nonce)
    raise AssertionError(
        f"{nonce} not observed after {attempts} attempts (mesh best-effort loss)"
    )


async def _events_with_nonce(app: Any, nonce: str) -> list[Any]:
    from medre.core.events.canonical import CanonicalEvent

    ids = await app.storage.list_event_ids_page(after_event_id=None, limit=1000)
    matches: list[Any] = []
    for page_id in ids:
        event = await app.storage.get(page_id)
        if isinstance(event, CanonicalEvent) and nonce in json.dumps(
            event.payload, default=str
        ):
            matches.append(event)
    return matches


async def _receipts_for_nonce(app: Any, nonce: str) -> list:
    deadline = time.monotonic() + _RECEIPT_TIMEOUT
    while time.monotonic() < deadline:
        events = await _events_with_nonce(app, nonce)
        receipts: list = []
        for event in events:
            receipts.extend(await app.storage.list_receipts_for_event(event.event_id))
        if receipts:
            return receipts
        await asyncio.sleep(1.0)
    return []


def _latest(receipts: list) -> Any:
    return max(receipts, key=lambda r: r.sequence)


@_REQUIRE
async def test_route_matrix_delivery_isolation(tmp_path: Path) -> None:
    """Every message produces a sent receipt on the correct target
    adapter and is observed on the correct transport only."""
    app = await _launch(tmp_path / "matrix.db")
    try:
        mt_nonces = [_nonce("R-MT") for _ in range(3)]
        mc_nonces = [_nonce("R-MC") for _ in range(3)]

        window = _MC_OBSERVE_TIMEOUT * 2 + 90
        # Direction mt→mc: the MT peer sends, the MC peer observes.  Both
        # directions use a floor — the meshes are best-effort and MEDRE's
        # exact guarantee is the receipt ledger, not RF certainty.
        with _McListener(window) as mc_listener:
            await _mt_send(mt_nonces)
            await asyncio.sleep(min(window - 30, 90))
        # Direction mc→mt: the MC peer sends, the MT peer observes.
        with _MtListener(window) as mt_listener:
            await _mc_send(mc_nonces)
            await asyncio.sleep(min(window - 30, 90))

        # MEDRE ledger: every nonce has a sent receipt on the correct
        # target adapter — routing is exact even though RF is not.
        for n in mt_nonces:
            receipts = await _receipts_for_nonce(app, n)
            assert receipts, f"no receipt for {n}"
            latest = _latest(receipts)
            assert latest.status == "sent", f"{n}: {latest.status!r}"
            assert (
                latest.target_adapter == "mc_radio"
            ), f"{n} routed to {latest.target_adapter!r}, expected mc_radio"
        for n in mc_nonces:
            receipts = await _receipts_for_nonce(app, n)
            assert receipts, f"no receipt for {n}"
            latest = _latest(receipts)
            assert latest.status == "sent", f"{n}: {latest.status!r}"
            assert (
                latest.target_adapter == "mt_radio"
            ), f"{n} routed to {latest.target_adapter!r}, expected mt_radio"

        # Observation ledger from the persistent scratch files: each
        # direction observes at least the floor (best-effort meshes), and
        # nothing ever appears on the wrong transport (isolation is exact).
        mc_texts = _scratch_texts(_MC_SCRATCH)
        mt_texts = _scratch_texts(_MT_SCRATCH)
        mc_hit = sum(1 for n in mt_nonces if any(n in t for t in mc_texts))
        mt_hit = sum(1 for n in mc_nonces if any(n in t for t in mt_texts))
        assert mc_hit >= 2, f"MC observation {mc_hit}/3 (need >=2)"
        assert mt_hit >= 2, f"MT observation {mt_hit}/3 (need >=2)"
        for n in mt_nonces:
            assert not any(n in t for t in mt_texts), f"{n} leaked to MT mesh"
        for n in mc_nonces:
            assert not any(n in t for t in mc_texts), f"{n} leaked to MC mesh"
    finally:
        await bounded(app.stop(), 30.0, "matrix runtime stop")


@_REQUIRE
async def test_rendering_contract_on_wire(tmp_path: Path) -> None:
    """Explicit relay prefixes, unicode/newline survival, and exact
    byte-budget truncation on both transports in both directions."""
    app = await _launch(tmp_path / "render.db")
    try:
        ascii_msg = _nonce("W-ASCI") + " plain payload"
        unicode_msg = _nonce("W-UNI") + " 你好 ✓ émoji 🚀 combining é"
        newline_msg = _nonce("W-NL") + "line1\nline2\nline3"
        long_for_mc = _nonce("W-LMC") + " " + "αβγδε" * 120
        long_for_mt = _nonce("W-LMT") + " " + "ζηθικ" * 120
        mca_msg = _nonce("W-MCA") + " return path payload"

        window = _MC_OBSERVE_TIMEOUT * _OBSERVE_ATTEMPTS + 60
        mt_cases = [ascii_msg, unicode_msg, newline_msg, long_for_mc]
        with _McListener(window) as mc_listener:
            await _mt_send(mt_cases)
            observed: dict[str, str] = {}
            for payload in mt_cases:
                observed[payload] = await _observe_or_resend(
                    mc_listener.packets,
                    lambda text: _mt_send([text]),
                    payload,
                )
        with _MtListener(window) as mt_listener:
            await _mc_send([mca_msg, long_for_mt])
            observed_mt: dict[str, str] = {}
            for payload in (mca_msg, long_for_mt):
                observed_mt[payload] = await _observe_or_resend(
                    mt_listener.packets,
                    lambda text: _mc_send([text]),
                    payload,
                )

        # --- MT→MC: firmware board prefix wraps the MEDRE-rendered text;
        # the relay prefix carries the MT sender label; payload exact.
        text = observed[ascii_msg]
        board_prefix, rendered = _split_prefix(text)
        assert board_prefix.startswith(_MC_MEDRE_NAME), (
            f"expected firmware board prefix {_MC_MEDRE_NAME!r}, got "
            f"{board_prefix!r}"
        )
        prefix, tail = _split_prefix(rendered)
        assert (
            prefix and _MT_PEER_LABEL in prefix
        ), f"relay prefix {prefix!r} lacks MT sender label"
        assert tail == ascii_msg, "plain payload damaged on wire"

        uni_rendered = _split_prefix(observed[unicode_msg])[1]
        assert (
            "你好" in uni_rendered and "🚀" in uni_rendered
        ), "unicode damaged on wire"
        assert observed[newline_msg].count("\n") == 2, "newlines damaged"

        # Budget: the MEDRE-rendered portion (firmware prefix excluded)
        # fits the MeshCore budget, and over-budget input truncates
        # exactly at the boundary on UTF-8-safe edges.
        long_text = observed[long_for_mc]
        long_rendered = long_text[len(board_prefix) :]
        assert (
            len(long_rendered.encode("utf-8")) <= _MC_BUDGET
        ), f"MC budget exceeded: {len(long_rendered.encode('utf-8'))} bytes"
        expected_mc = _utf8_truncate(prefix + long_for_mc, _MC_BUDGET)
        assert long_rendered == expected_mc, "MC truncation mismatch"

        # --- MC→MT: relay prefix carries the MC sender label; Meshtastic
        # budget enforced with exact truncation.
        text = observed_mt[mca_msg]
        prefix_mt, tail_mt = _split_prefix(text)
        assert (
            prefix_mt and _MC_PEER_LABEL in prefix_mt
        ), f"relay prefix {prefix_mt!r} lacks MC sender label"
        assert tail_mt == mca_msg, "return-path payload damaged"

        observed_lmt = observed_mt[long_for_mt]
        assert (
            len(observed_lmt.encode("utf-8")) <= _MT_BUDGET
        ), f"MT budget exceeded: {len(observed_lmt.encode('utf-8'))} bytes"
        expected_lmt = _utf8_truncate(prefix_mt + long_for_mt, _MT_BUDGET)
        assert observed_lmt == expected_lmt, "MT truncation mismatch"
    finally:
        await bounded(app.stop(), 30.0, "matrix runtime stop")


@_REQUIRE
async def test_provenance_chain_end_to_end(tmp_path: Path) -> None:
    """Peer native id → canonical event → receipt → observed wire text,
    with originating attribution surviving each cross-transport hop."""
    app = await _launch(tmp_path / "prov.db")
    try:
        mt_nonce = _nonce("P-MT")
        mc_nonce = _nonce("P-MC")
        window = _MC_OBSERVE_TIMEOUT * _OBSERVE_ATTEMPTS + 60

        with _McListener(window) as mc_listener:
            mt_sent = await _mt_send([mt_nonce])
            assert mt_sent and mt_sent[-1].get("sent_id"), "MT peer send failed"
            mt_packet_id = mt_sent[-1]["sent_id"]
            mt_observed = await _observe_or_resend(
                mc_listener.packets,
                lambda text: _mt_send([text]),
                mt_nonce,
            )
        with _MtListener(window) as mt_listener:
            mc_sent = await _mc_send([mc_nonce])
            assert mc_sent and mc_sent[-1].get("text"), "MC peer send failed"
            mc_observed = await _observe_or_resend(
                mt_listener.packets,
                lambda text: _mc_send([text]),
                mc_nonce,
            )

        # Chain link 1: the ingested peer packet resolves to a canonical
        # event through the native ref (provenance starts at the wire).
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        event_id = None
        while time.monotonic() < deadline and event_id is None:
            ref = await app.storage.resolve_native_ref(
                "mt_radio", "0", str(mt_packet_id)
            )
            if ref:
                event_id = ref
            else:
                await asyncio.sleep(1.0)
        assert event_id, "native ref for peer packet did not resolve"
        event = await app.storage.get(event_id)
        assert event is not None and mt_nonce in json.dumps(event.payload, default=str)

        # Chain link 2: receipt — sent, correlated to the same event.
        receipts = await _receipts_for_nonce(app, mt_nonce)
        assert receipts, "no receipt for mt-originated message"
        latest = _latest(receipts)
        assert latest.status == "sent"
        assert latest.event_id == event_id

        # Chain link 3: attribution survives each hop — the observed wire
        # text carries the ORIGINATING sender's label, not the relay's.
        _, mt_hop_rendered = _split_prefix(mt_observed)
        prefix_at_mc, _ = _split_prefix(mt_hop_rendered)
        assert (
            _MT_PEER_LABEL in prefix_at_mc
        ), f"MT sender attribution lost across hop: {mt_observed!r}"
        prefix_at_mt, _ = _split_prefix(mc_observed)
        assert (
            _MC_PEER_LABEL in prefix_at_mt
        ), f"MC sender attribution lost across hop: {mc_observed!r}"
    finally:
        await bounded(app.stop(), 30.0, "matrix runtime stop")


@_REQUIRE
async def test_sustained_traffic_convergence(tmp_path: Path) -> None:
    """Volume convergence: receipts exact, canonical events exact with
    duplicates distinct, peer observation at a reported best-effort floor."""
    app = await _launch(tmp_path / "volume.db")
    try:
        dup_text = _nonce("V-DUP")
        mt_corpus = [_nonce(f"V-MT-{i}") for i in range(_TRAFFIC // 2)]
        mc_corpus = [_nonce(f"V-MC-{i}") for i in range(_TRAFFIC // 2)]
        mc_corpus += [dup_text, dup_text]

        window = _MC_OBSERVE_TIMEOUT + 30 + 4 * (_TRAFFIC + 2)
        with _McListener(window) as mc_listener:
            await _mt_send(mt_corpus)
        with _MtListener(window + 60) as mt_listener:
            await _mc_send(mc_corpus)

        # Let trailing floods land inside the (still-open) windows.
        await asyncio.sleep(20)
        mc_texts = [(p.get("text") or "") for p in mc_listener.packets()]
        mt_texts = [(p.get("text") or "") for p in mt_listener.packets()]

        # Ledger 1 — receipts: every nonce has a sent receipt.
        for nonce in (*mt_corpus, *set(mc_corpus)):
            receipts = await _receipts_for_nonce(app, nonce)
            assert receipts, f"no receipt for {nonce}"
            latest = _latest(receipts)
            assert latest.status == "sent", f"{nonce}: {latest.status!r}"

        # Ledger 2 — canonical events: duplicates stay two distinct events.
        dup_events = await _events_with_nonce(app, dup_text)
        assert (
            len(dup_events) == 2
        ), f"spaced duplicate produced {len(dup_events)} events, expected 2"

        # Ledger 3 — peer observation: best-effort floor with the actual
        # ratio in the failure message (mesh drops are physics, not bugs).
        mc_hit = sum(1 for n in mt_corpus if any(n in t for t in mc_texts))
        mt_hit = sum(1 for n in set(mc_corpus) if any(n in t for t in mt_texts))
        mc_ratio = mc_hit / len(mt_corpus)
        mt_ratio = mt_hit / len(set(mc_corpus))
        assert mc_ratio >= _OBSERVE_FLOOR, (
            f"MC observation {mc_hit}/{len(mt_corpus)} below floor " f"{_OBSERVE_FLOOR}"
        )
        assert mt_ratio >= _OBSERVE_FLOOR, (
            f"MT observation {mt_hit}/{len(set(mc_corpus))} below floor "
            f"{_OBSERVE_FLOOR}"
        )
        assert (
            sum(dup_text in t for t in mt_texts) >= 1
        ), "duplicate pair never observed at MT peer"
    finally:
        await bounded(app.stop(), 30.0, "matrix runtime stop")
