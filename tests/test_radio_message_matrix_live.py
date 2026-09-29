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
                             byte-budget truncation at the MeshCore
                             relay boundary (160) for a payload that is
                             over MC budget yet sendable on the MT
                             sender's own ~230-byte text limit; MC→MT
                             payload/prefix exactness.  Over-budget
                             truncation at the MT boundary is covered by
                             the matrix-room test, whose source (room
                             history) has no size limit.
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
matrix_room_relay_three_     Radio peers' messages land in the Matrix
transport                    room with contractual attribution; room
                             messages fan out to both radio meshes; every
                             relayed event has exactly one sent receipt
                             per target adapter.  Room observation is
                             exact (durable server history) — only the
                             RF fan-out legs stay best-effort.
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
mesh physics, not a pipeline defect; a missing receipt is.  The Matrix
transport's own leg is different: the homeserver's room history is
durable, so room-side observation is asserted exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import urllib.request
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
from tests.helpers.synapse_starter import SynapseInstance
from tests.helpers.synapse_starter import start_synapse as _start_synapse
from tests.helpers.synapse_starter import stop_synapse as _stop_synapse

try:
    from medre.adapters.matrix.compat import HAS_NIO as _HAS_NIO
except Exception:  # pragma: no cover - optional extra absent
    _HAS_NIO = False

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.filterwarnings(
        "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
    ),
    # The pinned RNS release calls the deprecated threading.setDaemon in
    # its runtime; with the project's filterwarnings = ["error"] that
    # would kill the LXMF adapter at session start.
    pytest.mark.filterwarnings(
        r"ignore:setDaemon\(\) is deprecated:DeprecationWarning"
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
#: Meshtastic wire cap.  The theoretical preset maximum is ~230 bytes,
#: but clients cap near 200 to leave header headroom and stay clear of
#: the fragmentation edge; frames sent at the razor maximum are the
#: first to drop on marginal links.
_MT_BUDGET = 200

#: Explicit relay prefixes make on-wire attribution contractual for this
#: harness (the MeshCore renderer's default is no prefix; the Meshtastic
#: default is ``{sender_short}: ``).
_MT_PREFIX_TEMPLATE = "{sender_short}: "
_MC_PREFIX_TEMPLATE = "{sender}: "

#: Structural identity of the bench peers for attribution assertions.
_MT_PEER_LABEL = "d662"  # T1000-E short name (owner "Meshtastic d662")
_MC_PEER_LABEL = "MEDRE-MC-B"
_MX_PEER_LABEL = "MEDRE-MX-PEER"  # display name the starter assigns
_MX_PEER_LOCALPART = "medre-peer"  # MXID localpart; MT {sender_short} renders it

#: Matrix relay prefix template — same contractual pattern as the radio
#: prefixes, so room-side attribution is assertable, not incidental.
_MX_PREFIX_TEMPLATE = "{sender}: "

#: LXMF relay prefix template — the RNode leg's on-wire attribution.
_LX_PREFIX_TEMPLATE = "{sender}: "

_HAS_DOCKER = shutil.which("docker") is not None

# LXMF fourth transport: real RNode pair driven by the lab RNS configs
# and identities (the same endpoints the lxmf bridge suites use).
_LX_MEDRE_RNS = os.environ.get("LXMF_MEDRE_RNS_CONFIG", "")
_LX_MEDRE_ID = os.environ.get("LXMF_MEDRE_IDENTITY", "")
_LX_PEER_RNS = os.environ.get("LXMF_PEER_RNS_CONFIG", "")
_LX_PEER_ID = os.environ.get("LXMF_PEER_IDENTITY", "")

_REQUIRE_LX = pytest.mark.skipif(
    not (_LX_MEDRE_RNS and _LX_MEDRE_ID and _LX_PEER_RNS and _LX_PEER_ID),
    reason=(
        "lxmf matrix leg: set LXMF_MEDRE_RNS_CONFIG, LXMF_MEDRE_IDENTITY, "
        "LXMF_PEER_RNS_CONFIG, LXMF_PEER_IDENTITY"
    ),
)

try:
    from medre.adapters.lxmf.compat import HAS_LXMF as _HAS_LXMF
except Exception:  # pragma: no cover - optional extra absent
    _HAS_LXMF = False


# Observation scratch files are the listeners' own paths — duplicating
# them by hand once produced a path nobody writes and a vacuous read.
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


async def _launch(
    db_path: Path,
    *,
    synapse: SynapseInstance | None = None,
    lxmf: bool = False,
):
    """Build the both-directions matrix runtime and require healthy links.

    With *synapse*, a Matrix adapter joins as a third transport: both
    radio meshes relay into the room and room messages fan out to both
    radio meshes.  With *lxmf*, an LXMF adapter on the RNode pair joins
    the same way (radio meshes relay to the LXMF peer, and the LXMF
    peer's messages fan out to both radio meshes).
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
    route_list = [
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
    ]
    adapters: dict[str, dict[str, Any]] = {
        "meshtastic": {"mt_radio": mt},
        "meshcore": {"mc_radio": mc},
    }
    if synapse is not None:
        mx = MatrixRuntimeConfig(
            adapter_id="mx_radio",
            enabled=True,
            adapter_kind="real",
            config=MatrixConfig(
                adapter_id="mx_radio",
                homeserver=synapse.base_url,
                user_id=synapse.bot_user_id,
                access_token=synapse.bot_access_token,
                device_id=synapse.bot_device_id or None,
                room_allowlist={synapse.room_id},
                relay_prefix=_MX_PREFIX_TEMPLATE,
            ).validate(),
        )
        room = synapse.room_id
        route_list.extend(
            [
                RouteConfig(
                    route_id="mx_mt_to_room",
                    source_adapters=("mt_radio",),
                    dest_adapters=("mx_radio",),
                    source_channel="0",
                    dest_channel=room,
                ),
                RouteConfig(
                    route_id="mx_mc_to_room",
                    source_adapters=("mc_radio",),
                    dest_adapters=("mx_radio",),
                    source_channel="1",
                    dest_channel=room,
                ),
                RouteConfig(
                    route_id="mx_room_to_mt",
                    source_adapters=("mx_radio",),
                    dest_adapters=("mt_radio",),
                    source_channel=room,
                    dest_channel="0",
                ),
                RouteConfig(
                    route_id="mx_room_to_mc",
                    source_adapters=("mx_radio",),
                    dest_adapters=("mc_radio",),
                    source_channel=room,
                    dest_channel="1",
                ),
            ]
        )
        adapters["matrix"] = {"mx_radio": mx}
    if lxmf:
        from tests.helpers.lxmf_live_peer import delivery_dest_hash

        lx_dest = delivery_dest_hash(_LX_PEER_ID)
        lx = LxmfRuntimeConfig(
            adapter_id="lx_radio",
            enabled=True,
            adapter_kind="real",
            config=LxmfConfig(
                adapter_id="lx_radio",
                connection_type="reticulum",
                identity_path=_LX_MEDRE_ID,
                storage_path=str(db_path.parent / "lxmf_medre_storage"),
                reticulum_config_dir=_LX_MEDRE_RNS,
                display_name="MEDRE-LX-A",
                announce_interval_seconds=8.0,
                message_delay_seconds=_TX_PACING,
                stamp_cost=0,
                default_delivery_method="direct",
                lxmf_relay_prefix=_LX_PREFIX_TEMPLATE,
            ).validate(),
        )
        # LXMF inbound events carry the sender's hash as channel, so the
        # lxmf-source routes leave source_channel unset (match any).
        route_list.extend(
            [
                RouteConfig(
                    route_id="mx_mt_to_lx",
                    source_adapters=("mt_radio",),
                    dest_adapters=("lx_radio",),
                    source_channel="0",
                    dest_channel=lx_dest,
                ),
                RouteConfig(
                    route_id="mx_mc_to_lx",
                    source_adapters=("mc_radio",),
                    dest_adapters=("lx_radio",),
                    source_channel="1",
                    dest_channel=lx_dest,
                ),
                RouteConfig(
                    route_id="mx_lx_to_mt",
                    source_adapters=("lx_radio",),
                    dest_adapters=("mt_radio",),
                    dest_channel="0",
                ),
                RouteConfig(
                    route_id="mx_lx_to_mc",
                    source_adapters=("lx_radio",),
                    dest_adapters=("mc_radio",),
                    dest_channel="1",
                ),
            ]
        )
        adapters["lxmf"] = {"lx_radio": lx}
    routes = RouteConfigSet(routes=tuple(route_list))
    routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="radio-matrix-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(**adapters),
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
    """Send texts from the independent MeshCore peer (MEDRE-MC-B).

    The peer script retries BLE connects up to three times (each up to
    25s plus a disconnect remedy); the subprocess budget must cover the
    whole envelope or a slow first link wastes the retries.  The peer's
    JSON envelope is a dict; return just the per-text send results so
    callers index a list.
    """
    result = await asyncio.to_thread(
        _mc_peer, ["sendn", _MC_PEER_BLE, json.dumps(texts)], 95 + 8 * len(texts)
    )
    if isinstance(result, dict):
        return result.get("sent") or []
    return result


def _observer_text(packets: list[dict], nonce: str) -> str | None:
    for p in packets:
        text = p.get("text") or ""
        if nonce in text:
            return text
    return None


async def _observe_or_resend(
    listener: Any,
    resend: Callable[[str], Any],
    nonce: str,
    *,
    timeout: float = _MC_OBSERVE_TIMEOUT,
    attempts: int = _OBSERVE_ATTEMPTS,
) -> str:
    """Return the observed wire text for *nonce*, resending on mesh loss.

    Polls the listener's incremental scratch stream (``packets_until``),
    never its blocking full-window drain: the listener process must stay
    alive across attempts so resends land in the same capture window.

    Best-effort meshes may drop a flood or delay it behind the sender
    node's airtime queue; a bounded resend of the same nonce (fresh
    native packet id) satisfies observation without weakening the
    assertion — the nonce is the contract, not the packet id.
    """
    for attempt in range(1, attempts + 1):
        packets = await asyncio.to_thread(
            listener.packets_until,
            lambda pkts: _observer_text(pkts, nonce) is not None,
            timeout,
        )
        text = _observer_text(packets, nonce)
        if text is not None:
            return text
        if attempt < attempts:
            await resend(nonce)
    raise AssertionError(
        f"{nonce} not observed after {attempts} attempts (mesh best-effort loss)"
    )


async def _events_with_nonce(app: Any, nonce: str) -> list[Any]:
    """All canonical events whose payload contains *nonce*.

    Pages through the whole event ledger — long campaigns raise
    ``MEDRE_MATRIX_TRAFFIC`` and can exceed a single page.
    """
    from medre.core.events.canonical import CanonicalEvent

    matches: list[Any] = []
    after: str | None = None
    while True:
        ids = await app.storage.list_event_ids_page(after_event_id=after, limit=500)
        if not ids:
            return matches
        for page_id in ids:
            event = await app.storage.get(page_id)
            # ensure_ascii=False: an ASCII-escaped haystack can never
            # match a needle with non-ASCII characters (the long-message
            # nonce texts carry multibyte Greek).
            if isinstance(event, CanonicalEvent) and nonce in json.dumps(
                event.payload, default=str, ensure_ascii=False
            ):
                matches.append(event)
        after = ids[-1]


async def _await_events_with_nonce(
    app: Any, nonce: str, *, timeout: float = _RECEIPT_TIMEOUT
) -> list[Any]:
    """Poll the event ledger until *nonce* is admitted (bounded)."""
    deadline = time.monotonic() + timeout
    events: list[Any] = []
    while time.monotonic() < deadline:
        events = await _events_with_nonce(app, nonce)
        if events:
            return events
        await asyncio.sleep(1.0)
    return events


async def _sent_receipts(
    app: Any, event_id: str, *, timeout: float = _RECEIPT_TIMEOUT
) -> list:
    """Poll until *event_id* has a sent receipt; return its sent receipts."""
    deadline = time.monotonic() + timeout
    sent: list = []
    while time.monotonic() < deadline:
        receipts = await app.storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"]
        if sent:
            return sent
        await asyncio.sleep(1.0)
    return sent


async def _await_sent_receipt_targets(
    app: Any,
    event_id: str,
    expected: tuple[str, ...],
    *,
    timeout: float = _RECEIPT_TIMEOUT,
) -> list:
    """Poll until *event_id* has a sent receipt on every expected target.

    Delivery adapters complete at different speeds (paced radio sends
    against HTTP room posts), so the full target set must settle before
    exactness is asserted.
    """
    deadline = time.monotonic() + timeout
    sent: list = []
    while time.monotonic() < deadline:
        receipts = await app.storage.list_receipts_for_event(event_id)
        sent = [r for r in receipts if r.status == "sent"]
        if set(expected) <= {r.target_adapter for r in sent}:
            return sent
        await asyncio.sleep(1.0)
    return sent


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


def _mx_request(
    synapse: SynapseInstance,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: float = 10.0,
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{synapse.base_url}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(
        req, timeout=timeout
    ) as resp:  # nosec B310 - local Synapse test harness on a fixed loopback port
        raw = resp.read()
    return json.loads(raw) if raw else {}


async def _mx_room_send(synapse: SynapseInstance, texts: list[str]) -> None:
    """Send texts into the room as the independent peer user."""
    for text in texts:
        await asyncio.to_thread(
            _mx_request,
            synapse,
            "POST",
            f"/_matrix/client/v3/rooms/{synapse.room_id}/send/m.room.message",
            token=synapse.test_access_token,
            body={"msgtype": "m.text", "body": text},
        )


async def _mx_room_texts(synapse: SynapseInstance) -> list[str]:
    """Room message bodies from the durable server history (oldest first)."""

    def _fetch() -> list[str]:
        resp = _mx_request(
            synapse,
            "GET",
            f"/_matrix/client/v3/rooms/{synapse.room_id}/messages" "?dir=f&limit=200",
            token=synapse.test_access_token,
        )
        bodies = [
            event.get("content", {}).get("body", "")
            for event in resp.get("chunk", [])
            if event.get("type") == "m.room.message"
        ]
        return bodies  # dir=f without a from token is oldest-first

    return await asyncio.to_thread(_fetch)


async def _mx_await_room_text(
    synapse: SynapseInstance, nonce: str, *, timeout: float = 90.0
) -> str:
    """Return the room message body containing *nonce* (bounded relay wait).

    The room history is durable, so this is an exact channel: once the
    relay posts, the text is there for good.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for body in await _mx_room_texts(synapse):
            if nonce in body:
                return body
        await asyncio.sleep(2.0)
    raise AssertionError(f"{nonce} never appeared in the matrix room")


async def _await_adapter_healthy(
    app: Any, adapter_id: str, *, timeout: float = 90.0
) -> None:
    """Poll live health until *adapter_id* reports healthy.

    The Matrix adapter reports degraded until its first sync response,
    so room traffic must wait for this before it can be ingested.
    """
    deadline = time.monotonic() + timeout
    last = "never-polled"
    while time.monotonic() < deadline:
        snap = await app.refresh_live_health()
        entry = snap.adapters.get(adapter_id)
        if entry is not None:
            last = entry.health
            if entry.health == "healthy":
                return
        await asyncio.sleep(2.0)
    raise AssertionError(f"{adapter_id} health {last!r} after {timeout:.0f}s")


@_REQUIRE
async def test_route_matrix_delivery_isolation(tmp_path: Path) -> None:
    """Every message produces a sent receipt on the correct target
    adapter and is observed on the correct transport only."""
    app = await _launch(tmp_path / "matrix.db")
    try:
        mt_nonces = [_nonce("R-MT") for _ in range(3)]
        mc_nonces = [_nonce("R-MC") for _ in range(3)]

        window = _MC_OBSERVE_TIMEOUT * 2 + 90
        # One device link per transport means the sending peer and the
        # same-transport listener can never run together: each direction
        # phases its own listener, and the snapshot is taken inside the
        # listener context (each listener unlinks its scratch on open, so
        # evidence must not outlive the window it was captured in).
        # Direction mt→mc: the MT peer sends, the MC peer observes.  Both
        # directions use a floor — the meshes are best-effort and MEDRE's
        # exact guarantee is the receipt ledger, not RF certainty.
        with _McListener(window) as mc_listener:
            await _mt_send(mt_nonces)
            await asyncio.sleep(min(window - 30, 90))
            mc_texts = [
                (p.get("text") or "")
                for p in mc_listener.packets_until(lambda _: False, 0.0)
            ]
        # Direction mc→mt: the MC peer sends, the MT peer observes.  A
        # fresh MT connection also receives the peer's buffered texts, so
        # this window sees phase-one traffic that echoed onto the wrong
        # mesh as well.
        with _MtListener(window) as mt_listener:
            await _mc_send(mc_nonces)
            await asyncio.sleep(min(window - 30, 90))
            mt_texts = [
                (p.get("text") or "")
                for p in mt_listener.packets_until(lambda _: False, 0.0)
            ]
        # The mirrored MC-side leak drain is structurally unavailable:
        # the MC listener drains and discards the firmware's buffered
        # replay at connect (so one run's windows stay self-contained),
        # which would swallow exactly the evidence a post-hoc drain
        # exists to collect.  MC-side leak coverage is therefore limited
        # to its own window; the ledger-side receipt targets remain the
        # exact isolation proof on both sides.

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

        # Observation ledger: each direction observes at least the floor
        # (best-effort meshes), and nothing ever appears on the wrong
        # transport (isolation is exact).
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
    byte-budget truncation at the MeshCore relay boundary.

    The oversized input must stay within the MT sender's own payload
    limit (~230 bytes) — Meshtastic rejects text the local radio cannot
    carry — so MT-side over-budget truncation is exercised by the
    matrix-room test, whose source has no size limit.  The truncation
    itself is proven from receipt rendering evidence: runtime-relayed
    truncated frames are not dependably observable over RF on this
    bench (identical direct sends deliver)."""
    app = await _launch(tmp_path / "render.db")
    try:
        ascii_msg = _nonce("W-ASCI") + " plain payload"
        unicode_msg = _nonce("W-UNI") + " 你好 ✓ émoji 🚀 combining é"
        newline_msg = _nonce("W-NL") + "line1\nline2\nline3"
        # Over-budget for the MC relay budget (160) but within the MT
        # sender's own payload limit (~230): Meshtastic rejects text the
        # local radio cannot carry, so the oversized input must still be
        # sendable on the source transport.  ~219 bytes.
        long_for_mc = _nonce("W-LMC") + " " + "αβγδε" * 20
        mca_msg = _nonce("W-MCA") + " return path payload"

        window = _MC_OBSERVE_TIMEOUT * _OBSERVE_ATTEMPTS + 60
        # The short cases observe on-wire; the over-budget case rides
        # the truncated-frame anomaly (runtime-relayed truncated frames
        # are not dependably observable over RF on this bench) and is
        # proven from receipt rendering evidence instead.
        mt_cases = [ascii_msg, unicode_msg, newline_msg]
        with _McListener(window) as mc_listener:
            await _mt_send(mt_cases)
            observed: dict[str, str] = {}
            for payload in mt_cases:
                observed[payload] = await _observe_or_resend(
                    mc_listener,
                    lambda text: _mt_send([text]),
                    payload,
                )
            await _mt_send([long_for_mc])
        with _MtListener(window) as mt_listener:
            await _mc_send([mca_msg])
            observed_mt: dict[str, str] = {}
            for payload in (mca_msg,):
                observed_mt[payload] = await _observe_or_resend(
                    mt_listener,
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

        # Budget: the over-budget render truncates exactly at the
        # MeshCore budget on a UTF-8-safe edge, proven from the receipt's
        # rendering evidence (the same expected-value derivation as the
        # matrix-room test: a budget cut mid-multibyte-character lands a
        # byte under the nominal number).
        long_events = await _await_events_with_nonce(app, long_for_mc)
        assert long_events, f"no canonical event for {long_for_mc}"
        expected_long = len(
            _utf8_truncate(prefix + long_for_mc, _MC_BUDGET).encode("utf-8")
        )
        long_evidence = None
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        while time.monotonic() < deadline:
            long_evidence = None
            for ev in long_events:
                for r in await app.storage.list_receipts_for_event(ev.event_id):
                    if r.target_adapter != "mc_radio" or r.status != "sent":
                        continue
                    evidence = json.loads(r.rendering_evidence or "{}")
                    if evidence.get("rendered_text_bytes") == expected_long:
                        long_evidence = evidence
            if long_evidence is not None:
                break
            await asyncio.sleep(1.0)
        assert long_evidence is not None, (
            f"no exact-budget MC render recorded (expected {expected_long} " f"bytes)"
        )
        assert long_evidence.get("truncated") is True

        # --- MC→MT: relay prefix carries the MC sender label; the MeshCore
        # board prefix rides inside the payload (the sender's firmware
        # prepends it on the wire), so the nonce sits one prefix deeper.
        text = observed_mt[mca_msg]
        prefix_mt, tail_mt = _split_prefix(text)
        assert (
            prefix_mt and _MC_PEER_LABEL in prefix_mt
        ), f"relay prefix {prefix_mt!r} lacks MC sender label"
        board_mt, inner_tail = _split_prefix(tail_mt)
        assert board_mt.startswith(
            _MC_PEER_LABEL
        ), f"return path {text!r} lacks the MC board prefix"
        assert inner_tail == mca_msg, "return-path payload damaged"

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
            mt_packet_ids = [str(mt_sent[-1]["sent_id"])]

            # Observation resends create fresh native packet ids; keep
            # every id so the native-ref lookup can resolve whichever
            # packet the mesh actually delivered.
            async def _mt_resend(text: str) -> None:
                again = await _mt_send([text])
                if again and again[-1].get("sent_id"):
                    mt_packet_ids.append(str(again[-1]["sent_id"]))

            mt_observed = await _observe_or_resend(mc_listener, _mt_resend, mt_nonce)
        with _MtListener(window) as mt_listener:
            mc_sent = await _mc_send([mc_nonce])
            assert mc_sent and mc_sent[-1].get("text"), "MC peer send failed"
            mc_observed = await _observe_or_resend(
                mt_listener,
                lambda text: _mc_send([text]),
                mc_nonce,
            )

        # Chain link 1: the ingested peer packet resolves to a canonical
        # event through the native ref (provenance starts at the wire).
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        event_id = None
        while time.monotonic() < deadline and event_id is None:
            for packet_id in mt_packet_ids:
                ref = await app.storage.resolve_native_ref("mt_radio", "0", packet_id)
                if ref:
                    event_id = ref
                    break
            if event_id is None:
                await asyncio.sleep(1.0)
        assert event_id, "native ref for peer packet did not resolve"
        event = await app.storage.get(event_id)
        assert event is not None and mt_nonce in json.dumps(event.payload, default=str)

        # Chain link 2: receipt — exactly one sent receipt correlated to
        # that same event, on the routed target adapter.  Asserting per
        # event keeps resend-created siblings out of the chain.
        sent = await _sent_receipts(app, event_id)
        assert (
            len(sent) == 1
        ), f"{len(sent)} sent receipts for the provenance event, expected 1"
        assert sent[0].target_adapter == "mc_radio"

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
        # Each listener stays open through a drain period after its sends:
        # floods keep landing after the sender's last acceptance, and the
        # snapshot must be taken before the listener process exits.
        with _McListener(window) as mc_listener:
            await _mt_send(mt_corpus)
            await asyncio.sleep(_MC_OBSERVE_TIMEOUT)
            mc_texts = [
                (p.get("text") or "")
                for p in mc_listener.packets_until(lambda _: False, 0.0)
            ]
        with _MtListener(window + 60) as mt_listener:
            await _mc_send(mc_corpus)
            # MC flood ingest lag plus paced MT relays of the whole corpus.
            await asyncio.sleep(_MC_OBSERVE_TIMEOUT + _TX_PACING * len(mc_corpus))
            mt_texts = [
                (p.get("text") or "")
                for p in mt_listener.packets_until(lambda _: False, 0.0)
            ]

        # Ledger 1 — receipts: every admitted event has exactly one sent
        # receipt (the docstring contract, asserted per event).
        for nonce in (*mt_corpus, *sorted(set(mc_corpus))):
            events = await _await_events_with_nonce(app, nonce)
            assert events, f"no canonical event for {nonce}"
            for ev in events:
                sent = await _sent_receipts(app, ev.event_id)
                assert len(sent) == 1, (
                    f"{nonce}: {len(sent)} sent receipts for event "
                    f"{ev.event_id}, expected exactly 1"
                )

        # Ledger 2 — canonical events: duplicates stay two distinct events.
        dup_events = await _await_events_with_nonce(app, dup_text)
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


@_REQUIRE
@pytest.mark.skipif(
    not _HAS_NIO,
    reason="matrix transport requires mindroom-nio (pip install '.[matrix]')",
)
@pytest.mark.skipif(not _HAS_DOCKER, reason="matrix transport runs Synapse in Docker")
async def test_matrix_room_relay_three_transport(tmp_path: Path) -> None:
    """Third transport: Matrix/Synapse joins the radio matrix.

    Radio→room is observed exactly (durable server history) with
    contractual attribution; room→radio fans out to both meshes with
    bounded resend-until-observed on the RF legs; every relayed event
    has exactly one sent receipt per target adapter.
    """
    synapse = await asyncio.to_thread(_start_synapse, tmp_path / "synapse")
    # A failed launch (unhealthy links, start timeout) must not strand the
    # named container holding its port: stop it before re-raising.
    try:
        app = await _launch(tmp_path / "mx.db", synapse=synapse)
    except BaseException:
        await asyncio.to_thread(_stop_synapse, suppress_errors=True)
        raise
    try:
        await _await_adapter_healthy(app, "mx_radio")

        # --- Radio → room: exact observation, attribution contractual.
        mt_nonce = _nonce("X-MT")
        await _mt_send([mt_nonce])
        body = await _mx_await_room_text(synapse, mt_nonce)
        prefix, tail = _split_prefix(body)
        assert (
            prefix and _MT_PEER_LABEL in prefix
        ), f"room relay {body!r} lacks MT sender attribution"
        assert tail == mt_nonce, "room payload damaged"
        events = await _await_events_with_nonce(app, mt_nonce)
        assert events, f"no canonical event for {mt_nonce}"
        for ev in events:
            sent = await _await_sent_receipt_targets(
                app, ev.event_id, ("mc_radio", "mx_radio")
            )
            targets = sorted(r.target_adapter for r in sent)
            assert targets == [
                "mc_radio",
                "mx_radio",
            ], f"{mt_nonce}: receipt targets {targets}"

        mc_nonce = _nonce("X-MC")
        await _mc_send([mc_nonce])
        body = await _mx_await_room_text(synapse, mc_nonce)
        prefix, tail = _split_prefix(body)
        assert (
            prefix and _MC_PEER_LABEL in prefix
        ), f"room relay {body!r} lacks MC sender attribution"
        # MeshCore wire text carries the sender's firmware board prefix
        # inside the payload (the same double prefix the rendering test
        # observes on the mesh); the nonce sits under it, undamaged.
        board_prefix, inner_tail = _split_prefix(tail)
        assert board_prefix.startswith(
            _MC_PEER_LABEL
        ), f"room relay {body!r} lacks the MC board prefix"
        assert inner_tail == mc_nonce, "room payload damaged"
        events = await _await_events_with_nonce(app, mc_nonce)
        assert events, f"no canonical event for {mc_nonce}"
        for ev in events:
            # The MC-originated event relays to the other radio and the
            # room — never back to its own transport.
            sent = await _await_sent_receipt_targets(
                app, ev.event_id, ("mt_radio", "mx_radio")
            )
            targets = sorted(r.target_adapter for r in sent)
            assert targets == [
                "mt_radio",
                "mx_radio",
            ], f"{mc_nonce}: receipt targets {targets}"

        # --- Room → both radio meshes.  The listeners hold different
        # devices than anything the room send touches, so both stay open
        # across the send; RF legs use bounded resend-until-observed.
        rx_nonce = _nonce("X-RX")
        # Two sequential observes (the short fan-out on each listener),
        # each up to three bounded attempts; the window must cover both.
        window = 2 * _MC_OBSERVE_TIMEOUT * _OBSERVE_ATTEMPTS + 120
        with (
            _MtListener(window) as mt_listener,
            _McListener(window) as mc_listener,
        ):

            async def _room_resend(text: str) -> None:
                await _mx_room_send(synapse, [text])

            await _mx_room_send(synapse, [rx_nonce])
            mt_text = await _observe_or_resend(mt_listener, _room_resend, rx_nonce)
            mc_text = await _observe_or_resend(mc_listener, _room_resend, rx_nonce)

            # Over-budget truncation from the one source with no size
            # limit: the long relay goes out, and its exact-budget
            # renders are asserted from receipt rendering evidence
            # below.  Bench finding: runtime-relayed truncated frames
            # are not dependably observable over RF on either mesh
            # (identical direct sends deliver), so this harness does
            # not gate on their RF observation.
            long_rx = _nonce("X-LNG") + " " + "γχψωφ" * 40
            await _mx_room_send(synapse, [long_rx])

        # MC wire: the firmware board prefix wraps the MEDRE-rendered
        # text; inside it the relay prefix carries the Matrix sender's
        # display name ({sender} renders it) over an exact payload.  MT
        # wire: the relay prefix carries the sender's MXID localpart.
        mc_board, mc_rendered = _split_prefix(mc_text)
        assert mc_board.startswith(
            _MC_MEDRE_NAME
        ), f"MC fan-out {mc_text!r} lacks the MEDRE board prefix"
        mc_prefix, mc_tail = _split_prefix(mc_rendered)
        assert (
            _MX_PEER_LABEL in mc_prefix
        ), f"MC fan-out {mc_text!r} lacks Matrix sender attribution"
        assert mc_tail == rx_nonce, "MC fan-out payload damaged"
        mt_prefix, mt_tail = _split_prefix(mt_text)
        assert (
            _MX_PEER_LOCALPART in mt_prefix
        ), f"MT fan-out {mt_text!r} lacks Matrix sender attribution"
        assert mt_tail == rx_nonce, "MT fan-out payload damaged"

        # Long-message truncation: exact at both budgets, per the
        # receipts' rendering evidence (renderer, byte counts, and the
        # truncated flag are durable ledger facts).  The expected byte
        # count is computed with the same UTF-8-safe truncation the
        # renderers apply: a budget that cuts mid-multibyte-character
        # lands one byte under the nominal number (159, not 160), so
        # comparing against the nominal budget would never match.
        long_events = await _await_events_with_nonce(app, long_rx, timeout=90)
        assert long_events, "long room message never admitted"
        expected_long_bytes = {
            "mt_radio": len(_utf8_truncate(mt_prefix + long_rx, _MT_BUDGET).encode()),
            "mc_radio": len(_utf8_truncate(mc_prefix + long_rx, _MC_BUDGET).encode()),
        }
        # Receipts append as each paced radio send completes; poll
        # until both legs' exact renders land (bounded).
        deadline = time.monotonic() + _RECEIPT_TIMEOUT
        long_evidence: dict[str, dict] = {}
        while time.monotonic() < deadline:
            long_evidence = {}
            for ev in long_events:
                receipts = await app.storage.list_receipts_for_event(ev.event_id)
                for r in receipts:
                    if r.status != "sent":
                        continue
                    expected = expected_long_bytes.get(r.target_adapter)
                    if expected is None:
                        continue
                    evidence = json.loads(r.rendering_evidence or "{}")
                    if evidence.get("rendered_text_bytes") == expected:
                        long_evidence[r.target_adapter] = evidence
            if len(long_evidence) == len(expected_long_bytes):
                break
            await asyncio.sleep(1.0)
        missing = {"mt_radio", "mc_radio"} - set(long_evidence)
        assert not missing, (
            f"exact-budget renders missing for {missing} "
            f"(expected bytes {expected_long_bytes})"
        )
        for target, evidence in long_evidence.items():
            assert evidence.get("truncated") is True, target

        # Ledger: every room-originated event relays to exactly one sent
        # receipt on each radio target.
        events = await _await_events_with_nonce(app, rx_nonce)
        assert events, "room message never admitted"
        for ev in events:
            sent = await _await_sent_receipt_targets(
                app, ev.event_id, ("mc_radio", "mt_radio")
            )
            targets = sorted(r.target_adapter for r in sent)
            assert targets == [
                "mc_radio",
                "mt_radio",
            ], f"{rx_nonce}: receipt targets {targets}"
    finally:
        try:
            await bounded(app.stop(), 30.0, "matrix runtime stop")
        finally:
            await asyncio.to_thread(_stop_synapse, suppress_errors=True)


@_REQUIRE
@_REQUIRE_LX
def _lx_ingest_evidence(app: Any) -> str:
    """Stage attribution for an LXMF ingest failure, from adapter evidence.

    The chain is silent by design: the router proves an inbound packet to
    the sender before the app-side delivery callback runs, and every later
    drop (normalisation, classifier gate, dedup, publish) leaves no wire
    trace.  Adapter diagnostics count each stage; this renders them so a
    failed run names the stage that lost the message instead of only
    reporting the missing event.
    """
    try:
        diag = app.adapters["lx_radio"].diagnostics()
    except Exception as exc:
        return f"lxmf adapter diagnostics unavailable: {exc!r}"
    session = diag.get("session") or {}
    return (
        "lxmf ingest evidence: sdk_deliveries="
        f"{session.get('deliveries_received')} "
        f"last_delivery={session.get('last_message_time')} "
        f"classifier(seen={diag.get('classifier_messages_seen')}, "
        f"relayed={diag.get('classifier_messages_relayed')}, "
        f"ignored={diag.get('classifier_messages_ignored')}) "
        f"dedup_suppressed={diag.get('inbound_duplicates_suppressed')} "
        f"published={diag.get('inbound_published')} "
        f"connected={session.get('connected')} "
        f"last_error={session.get('last_error')!r}"
    )


@pytest.mark.skipif(
    not _HAS_LXMF,
    reason="lxmf matrix leg requires the pinned lxmf/rns SDKs "
    "(pip install 'medre[lxmf]')",
)
async def test_lxmf_fourth_transport_relay(tmp_path: Path) -> None:
    """Fourth transport: LXMF over the RNode pair joins the matrix.

    Radio→LXMF is observed at the independent LXMF peer with contractual
    attribution; LXMF→radios fans out to both radio meshes; every relayed
    event has exactly one sent receipt per target adapter.
    """
    from tests.helpers.lxmf_live_peer import LxmfPeerListener as _LxListener
    from tests.helpers.lxmf_live_peer import delivery_dest_hash
    from tests.helpers.lxmf_live_peer import run_lxmf_peer as _lx_peer

    medre_lx_dest = delivery_dest_hash(_LX_MEDRE_ID)
    # RNode LXMF legs need link establishment and announce discovery, so
    # observation waits are longer than the RF-mesh legs.
    _LX_OBSERVE_TIMEOUT = 120.0

    def _lx_texts(packets: list[dict]) -> list[str]:
        return [(p.get("content") or "") for p in packets]

    def _lx_observer_text(packets: list[dict], nonce: str) -> str | None:
        for text in _lx_texts(packets):
            if nonce in text:
                return text
        return None

    # The LXMF peer listener spans the whole test: it is the far-side
    # observer for radio→lxmf and the identity anchor for lxmf→radios.
    # The budget covers both radio→LXMF observation legs plus the
    # worst-case LXMF→radio envelope (initial send and resends,
    # each behind full observation budgets).
    lx_window = 1800.0
    with _LxListener(lx_window) as lx_listener:
        app = await _launch(tmp_path / "lx.db", lxmf=True)
        try:
            # --- MT → LXMF: observed at the peer, attribution contractual.
            mt_nonce = _nonce("L-MT")
            await _mt_send([mt_nonce])
            lx_packets = await asyncio.to_thread(
                lx_listener.packets_until,
                lambda pkts: _lx_observer_text(pkts, mt_nonce) is not None,
                _LX_OBSERVE_TIMEOUT,
            )
            lx_text = _lx_observer_text(lx_packets, mt_nonce)
            assert lx_text is not None, "MT relay never reached the LXMF peer"
            prefix, tail = _split_prefix(lx_text)
            assert (
                prefix and _MT_PEER_LABEL in prefix
            ), f"LXMF relay {lx_text!r} lacks MT sender attribution"
            assert tail == mt_nonce, "LXMF payload damaged"
            events = await _await_events_with_nonce(app, mt_nonce)
            assert events, f"no canonical event for {mt_nonce}"
            for ev in events:
                sent = await _await_sent_receipt_targets(
                    app, ev.event_id, ("mc_radio", "lx_radio")
                )
                targets = sorted(r.target_adapter for r in sent)
                assert targets == [
                    "lx_radio",
                    "mc_radio",
                ], f"{mt_nonce}: receipt targets {targets}"

            # --- MC → LXMF: same contract, board prefix inside the payload.
            mc_nonce = _nonce("L-MC")
            await _mc_send([mc_nonce])
            lx_packets = await asyncio.to_thread(
                lx_listener.packets_until,
                lambda pkts: _lx_observer_text(pkts, mc_nonce) is not None,
                _LX_OBSERVE_TIMEOUT,
            )
            lx_text = _lx_observer_text(lx_packets, mc_nonce)
            assert lx_text is not None, "MC relay never reached the LXMF peer"
            prefix, tail = _split_prefix(lx_text)
            assert (
                prefix and _MC_PEER_LABEL in prefix
            ), f"LXMF relay {lx_text!r} lacks MC sender attribution"
            assert (
                tail == f"{_MC_PEER_LABEL}: {mc_nonce}"
            ), "LXMF payload damaged (MC board prefix)"
            events = await _await_events_with_nonce(app, mc_nonce)
            assert events, f"no canonical event for {mc_nonce}"
            for ev in events:
                sent = await _await_sent_receipt_targets(
                    app, ev.event_id, ("mt_radio", "lx_radio")
                )
                targets = sorted(r.target_adapter for r in sent)
                assert targets == [
                    "lx_radio",
                    "mt_radio",
                ], f"{mc_nonce}: receipt targets {targets}"

            # --- LXMF → both radio meshes.  The RNode sender waits for
            # DIRECT delivery terminal state; that state proves only the
            # RNS-level receipt — the receiving router proves each packet
            # to the sender before the app-side callback runs, and every
            # downstream drop is silent.  Fan-out and admission failures
            # below carry the adapter's ingest evidence so the stage
            # that lost the message is legible.
            rx_nonce = _nonce("L-RX")
            # Worst-case envelope: the DIRECT-delivery send time once
            # for the initial send and once per resend, each behind a
            # full observation budget.
            _LX_SEND_TIMEOUT = 180.0
            window = (
                _LX_SEND_TIMEOUT
                + 2 * _MC_OBSERVE_TIMEOUT * _OBSERVE_ATTEMPTS
                + 2 * (_OBSERVE_ATTEMPTS - 1) * _LX_SEND_TIMEOUT
                + 60
            )
            with (
                _MtListener(window) as mt_listener,
                _McListener(window) as mc_listener,
            ):
                sent_lx = await asyncio.to_thread(
                    _lx_peer,
                    ["send", medre_lx_dest, json.dumps([rx_nonce])],
                    _LX_SEND_TIMEOUT,
                )
                assert sent_lx.get("sent"), f"LXMF peer send failed: {sent_lx}"

                async def _lx_resend(text: str) -> None:
                    await asyncio.to_thread(
                        _lx_peer,
                        ["send", medre_lx_dest, json.dumps([text])],
                        _LX_SEND_TIMEOUT,
                    )

                try:
                    mt_text = await _observe_or_resend(
                        mt_listener, _lx_resend, rx_nonce
                    )
                    mc_text = await _observe_or_resend(
                        mc_listener, _lx_resend, rx_nonce
                    )
                except AssertionError as exc:
                    raise AssertionError(f"{exc}; {_lx_ingest_evidence(app)}") from exc

            # MC wire: board prefix wraps the rendered text; the relay
            # prefix carries the LXMF peer's display name.  MT wire:
            # prefix + exact payload.
            mc_board, mc_rendered = _split_prefix(mc_text)
            assert mc_board.startswith(
                _MC_MEDRE_NAME
            ), f"MC fan-out {mc_text!r} lacks the MEDRE board prefix"
            mc_prefix, mc_tail = _split_prefix(mc_rendered)
            assert (
                "lx-b-peer" in mc_prefix
            ), f"MC fan-out {mc_text!r} lacks LXMF sender attribution"
            assert mc_tail == rx_nonce, "MC fan-out payload damaged"
            mt_prefix, mt_tail = _split_prefix(mt_text)
            assert mt_prefix, f"MT fan-out {mt_text!r} lacks LXMF sender attribution"
            assert mt_tail == rx_nonce, "MT fan-out payload damaged"

            # Ledger: every lxmf-originated event relays to exactly one
            # sent receipt on each radio target.
            events = await _await_events_with_nonce(app, rx_nonce)
            assert events, f"LXMF message never admitted; {_lx_ingest_evidence(app)}"
            for ev in events:
                sent = await _await_sent_receipt_targets(
                    app, ev.event_id, ("mc_radio", "mt_radio")
                )
                targets = sorted(r.target_adapter for r in sent)
                assert targets == [
                    "mc_radio",
                    "mt_radio",
                ], f"{rx_nonce}: receipt targets {targets}"
        finally:
            await bounded(app.stop(), 30.0, "matrix runtime stop")
