"""Live physical-pair tests for the LXMF adapter over RNode RF (two T-LoRas).

Opt-in ONLY: ``LXMF_PAIR=1`` with explicit owned lab endpoints. Default
suite never touches radios (the whole module skips without the env keys).

Topology:
- MEDRE runtime (in-process, real adapters, real sqlite) owns LX-A through
  an isolated Reticulum config dir whose ONLY interface is one RNodeInterface
  (``share_instance = No``, no UDP/TCP/AutoInterface backdoor).
- An independent native peer process (own Reticulum instance, own identity,
  own storage, own RNode) owns LX-B.

Evidence layers stay distinct in every verdict:
A. MEDRE durable admission (canonical events, receipts, outbox).
B. SDK/native acceptance (LXMF message hashes, delivery-state counts —
   receipts top out at ``sent`` = local acceptance, documented).
C. The independent peer's RF observation of correlated payloads.

Isolation and freshness: both RNS storage dirs are wiped before the suite
so no cached path/announce from a previous run can satisfy a case; every
message carries a unique nonce; the peer process is started fresh per case
and signals readiness via a file handshake after its router is armed.

The pinned RNS 1.5.4 calls the deprecated ``threading.setDaemon`` inside
its runtime; with the project's ``filterwarnings = ["error"]`` that would
raise inside ``Reticulum()``. Ignore exactly that warning here — no other
suppression.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from tests.helpers.live_harness import bounded
from tests.helpers.meshtastic import make_meshtastic_text_packet

_PAIR_ENABLED = os.environ.get("LXMF_PAIR", "") == "1"
_MEDRE_RNS = os.environ.get("LXMF_MEDRE_RNS_CONFIG", "")
_PEER_RNS = os.environ.get("LXMF_PEER_RNS_CONFIG", "")
_MEDRE_IDENTITY = os.environ.get("LXMF_MEDRE_IDENTITY", "")
_PEER_IDENTITY = os.environ.get("LXMF_PEER_IDENTITY", "")
# Per-port hub control for the RF-off negative control (uhubctl location
# and port of the PEER board).  Physical power loss, not a graceful
# interface disconnect — labelled as such in the assertions.
_PEER_HUB = os.environ.get("LXMF_PEER_HUB", "")
_PEER_HUB_PORT = os.environ.get("LXMF_PEER_HUB_PORT", "")

_QUICK = os.environ.get("MEDRE_LIVE_QUICK", "") == "1"

_REQUIRE_PAIR = pytest.mark.skipif(
    not (
        _PAIR_ENABLED
        and _MEDRE_RNS
        and _PEER_RNS
        and _MEDRE_IDENTITY
        and _PEER_IDENTITY
    ),
    reason=(
        "opt-in physical pair: set LXMF_PAIR=1, LXMF_MEDRE_RNS_CONFIG, "
        "LXMF_PEER_RNS_CONFIG, LXMF_MEDRE_IDENTITY, LXMF_PEER_IDENTITY"
    ),
)

_HAS_HUB_CONTROL = bool(_PEER_HUB and _PEER_HUB_PORT)

# Bounded waits.  LXMF DIRECT delivery over RNode RF takes a path request /
# announce, link setup, and the message resource — single-digit to low
# tens of seconds at SF8/BW125 bench range.
_PACING_SECONDS = 2.5  # peer send pacing between RF messages
_RECEIPT_TIMEOUT = 45.0
_DELIVERY_TIMEOUT = 90.0
_PEER_READY_TIMEOUT = 40.0
_ANNOUNCE_INTERVAL = 8.0  # MEDRE-side announce loop for discovery

_SCRATCH_JSONL = Path("/tmp/medre_lxmf_pair_peer.json")
_READY_PATH = Path("/tmp/medre_lxmf_pair_peer.ready")

pytestmark = [
    pytest.mark.filterwarnings(
        # Regex: literal parens must be escaped or the pattern silently
        # never matches the actual message text.
        r"ignore:setDaemon\(\) is deprecated:DeprecationWarning"
    ),
]


# ---------------------------------------------------------------------------
# Destination addressing (public RNS API; no Reticulum instance needed)
# ---------------------------------------------------------------------------
def _delivery_dest_hash(identity_path: str) -> str:
    """LXMF delivery destination hash for an identity file (32 hex)."""
    import RNS

    identity = RNS.Identity.from_file(identity_path)
    assert identity is not None, f"cannot load identity {identity_path!r}"
    return RNS.Destination.hash_from_name_and_identity("lxmf.delivery", identity).hex()


_MEDRE_DEST = lambda: _delivery_dest_hash(_MEDRE_IDENTITY)  # noqa: E731
_PEER_DEST = lambda: _delivery_dest_hash(_PEER_IDENTITY)  # noqa: E731


# ---------------------------------------------------------------------------
# Independent native peer (own RNS instance, identity, storage)
# ---------------------------------------------------------------------------
_PEER_SCRIPT = r'''
import json, sys, time

MODE = sys.argv[1]
RNS_DIR = sys.argv[2]
IDENTITY = sys.argv[3]
STORAGE = sys.argv[4]
READY = sys.argv[5]
JSONL = sys.argv[6]

import RNS, LXMF

reticulum = RNS.Reticulum(configdir=RNS_DIR, loglevel=0)
identity = RNS.Identity.from_file(IDENTITY)
router = LXMF.LXMRouter(identity=identity, storagepath=STORAGE, autopeer=False)
delivery_dest = router.register_delivery_identity(identity, display_name="lx-b-peer")

import threading

def announce_loop():
    while True:
        try:
            router.announce(delivery_dest.hash)
        except Exception:
            pass
        time.sleep(8.0)

def wait_terminal(lxm, timeout=90.0):
    """Wait for the router to reach a terminal LXMF state before this
    process exits — exiting early would kill the delivery jobs."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = lxm.state
        if last in (LXMF.LXMessage.DELIVERED, LXMF.LXMessage.FAILED):
            return last
        time.sleep(1.0)
    return last

def recall_or_fail(dest_hex, timeout=45.0):
    end = time.time() + timeout
    dest_identity = None
    while time.time() < end and dest_identity is None:
        dest_identity = RNS.Identity.recall(bytes.fromhex(dest_hex))
        if dest_identity is None:
            time.sleep(1.0)
    if dest_identity is None:
        print(json.dumps({"error": "no identity recall for " + dest_hex}))
        sys.exit(4)
    return dest_identity

if MODE == "listen":
    got = []
    def on_delivery(message):
        mh = getattr(message, "hash", None)
        sh = getattr(message, "source_hash", None)
        content = getattr(message, "content", None)
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", "replace")
        fields = getattr(message, "fields", None) or {}
        raw = fields.get(0xFD) if isinstance(fields, dict) else None
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = json.loads(raw.decode("utf-8"))
            except Exception:
                raw = None
        envelope = raw if isinstance(raw, dict) else None
        rec = {
            "hash": mh.hex() if mh is not None else None,
            "source": sh.hex() if sh is not None else None,
            "content": content,
            "envelope": envelope,
        }
        # default=str: never let a non-serialisable payload attribute kill
        # the delivery callback — the evidence line must land.
        with open(JSONL, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    router.register_delivery_callback(on_delivery)
    threading.Thread(target=announce_loop, daemon=True).start()
    with open(READY, "w") as fh:
        fh.write("1")
    time.sleep(float(sys.argv[7]))
    print(json.dumps({"received_count": len(got)}))
elif MODE == "send":
    dest_hex = sys.argv[7]
    texts = json.loads(sys.argv[8])
    dest_identity = recall_or_fail(dest_hex)
    threading.Thread(target=announce_loop, daemon=True).start()
    out = []
    for text in texts:
        dest = RNS.Destination(
            dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(
            dest, delivery_dest, text,
            desired_method=LXMF.LXMessage.DIRECT,
        )
        router.handle_outbound(lxm)
        state = wait_terminal(lxm)
        out.append({"text": text, "hash": lxm.hash.hex(), "state": int(state)})
        time.sleep(2.5)
    print(json.dumps({"sent": out}))
elif MODE == "sendenv":
    dest_hex = sys.argv[7]
    text = sys.argv[8]
    envelope = json.loads(sys.argv[9])
    dest_identity = recall_or_fail(dest_hex)
    threading.Thread(target=announce_loop, daemon=True).start()
    dest = RNS.Destination(
        dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(
        dest, delivery_dest, text,
        fields={0xFD: envelope},
        desired_method=LXMF.LXMessage.DIRECT,
    )
    router.handle_outbound(lxm)
    state = wait_terminal(lxm)
    print(json.dumps({"sent": [{"text": text, "hash": lxm.hash.hex(),
                                "state": int(state)}]}))
else:
    print(json.dumps({"error": "unknown mode"}))
    sys.exit(2)
'''


def _peer(args: list[str], timeout: float) -> dict:
    """Run the native peer script once and return its JSON payload.

    Each invocation gets a FRESH LXMF router storage dir so a pending
    outbound from a previous phase cannot leak into this one.
    """
    tmp = Path("/tmp/medre_lxmf_peer_tmp")
    tmp.mkdir(exist_ok=True)
    storage = tempfile.mkdtemp(prefix="medre_lxmf_peer_storage_")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                args[0],
                _PEER_RNS,
                _PEER_IDENTITY,
                storage,
                "unused",
                "unused",
                *args[1:],
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(storage, ignore_errors=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"native peer failed ({proc.returncode}): {proc.stderr[-800:]}"
        )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"native peer produced no JSON: {proc.stderr[-400:]}")
    return json.loads(lines[-1])


class _PeerListener:
    """Background native-peer LXMF listener with incremental collection.

    Readiness is a file handshake armed after the router and delivery
    callback are registered — before any MEDRE-side traffic.  Received
    messages append to a JSONL scratch file as they arrive so positive
    cases finish as soon as correlated evidence lands.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "_PeerListener":
        _SCRATCH_JSONL.unlink(missing_ok=True)
        _READY_PATH.unlink(missing_ok=True)
        self._storage = tempfile.mkdtemp(prefix="medre_lxmf_peer_storage_")
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PEER_SCRIPT,
                "listen",
                _PEER_RNS,
                _PEER_IDENTITY,
                self._storage,
                str(_READY_PATH),
                str(_SCRATCH_JSONL),
                str(self._seconds),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + _PEER_READY_TIMEOUT
        while time.monotonic() < deadline:
            if _READY_PATH.exists():
                return self
            if self._proc.poll() is not None:
                _, err = self._proc.communicate(timeout=10)
                raise AssertionError(f"native peer listener died: {err[-400:]}")
            time.sleep(0.2)
        raise AssertionError("native peer listener never signalled ready")

    def _read_packets(self) -> list[dict]:
        if not _SCRATCH_JSONL.exists():
            return []
        packets: list[dict] = []
        for line in _SCRATCH_JSONL.read_text().splitlines():
            line = line.strip()
            if line:
                packets.append(json.loads(line))
        return packets

    def packets_until(self, predicate, timeout: float) -> list[dict]:
        """Poll collected packets until ``predicate`` holds or timeout."""
        deadline = time.monotonic() + timeout
        packets: list[dict] = []
        while time.monotonic() < deadline:
            packets = self._read_packets()
            if predicate(packets):
                return packets
            time.sleep(0.5)
        return self._read_packets()

    def __exit__(self, *exc: object) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=10)
        # Close the child pipe handles — under filterwarnings=error an
        # unclosed-pipe ResourceWarning during interpreter GC (e.g. inside
        # RNS's gc.collect()) surfaces as an unraisable-exception error.
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                stream.close()
        shutil.rmtree(getattr(self, "_storage", ""), ignore_errors=True)


# ---------------------------------------------------------------------------
# In-process real runtime (built exactly like `medre run`)
# ---------------------------------------------------------------------------
def _build_runtime(db_path: Path, *, with_route: bool):
    from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter  # noqa: F401
    from medre.config.adapters.lxmf import LxmfConfig
    from medre.config.adapters.meshtastic import MeshtasticConfig
    from medre.config.model import (
        AdapterConfigSet,
        LoggingConfig,
        LxmfRuntimeConfig,
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
            announce_interval_seconds=_ANNOUNCE_INTERVAL,
            message_delay_seconds=_PACING_SECONDS,
            stamp_cost=0,
            default_delivery_method="direct",
        ).validate(),
    )
    routes = RouteConfigSet()
    if with_route:
        routes = RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="lab_egress",
                    source_adapters=("lab_src",),
                    dest_adapters=("lx_radio",),
                    source_channel="0",
                    dest_channel=_PEER_DEST(),
                ),
            )
        )
        routes.validate()
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="lxmf-pair-live"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(meshtastic={"lab_src": src}, lxmf={"lx_radio": lx}),
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
    """Build and start a runtime; LXMF health is local-session truth."""
    if os.environ.get("LXMF_RNS_DEBUG"):
        # The pinned RNS 1.5.4 ignores a ``loglevel`` line in the config
        # file; the module global (read by RNS.log) is the supported knob.
        import RNS

        RNS.loglevel = int(os.environ["LXMF_RNS_DEBUG"])
    app = _build_runtime(db_path, with_route=with_route)
    try:
        await bounded(app.start(), 120.0, "pair runtime app.start()")
    except Exception:
        try:
            await _stop_app(app)
        except Exception:
            pass
        raise
    deadline = time.monotonic() + 20.0
    last = None
    while time.monotonic() < deadline:
        info = await bounded(
            app.adapters["lx_radio"].health_check(), 15.0, "lx_radio health_check"
        )
        last = info.health
        if info.health == "healthy":
            return app
        await asyncio.sleep(1.0)
    await _stop_app(app)
    raise RuntimeError(f"pair runtime never reached healthy ({last})")


@pytest.fixture(scope="module", autouse=True)
def _fresh_rns_state():
    """Wipe both lab RNS storage dirs once before any instance starts.

    Storage holds persisted path tables and announce caches; wiping
    guarantees no pre-existing entry can satisfy a case.  Identities live
    in separate files and are untouched.  Nothing is running yet: Reticulum
    is only instantiated inside the tests.  Without the env keys the whole
    module is skipped — never touch any path then.
    """
    if not (_PAIR_ENABLED and _MEDRE_RNS and _PEER_RNS and _MEDRE_IDENTITY):
        yield
        return
    for cfg_dir in (_MEDRE_RNS, _PEER_RNS):
        storage = Path(cfg_dir) / "storage"
        if cfg_dir and storage.exists():
            shutil.rmtree(storage)
    yield


def _nonce(prefix: str) -> str:
    return f"MEDRE {prefix}-{uuid.uuid4().hex[:10]}"


async def _events_with_body(app, needle: str) -> list:  # noqa: ANN001
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
        if hits:
            return hits
        await asyncio.sleep(1.0)
    return hits


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


async def _event_count(app) -> int:  # noqa: ANN001
    ids = await app.storage.list_event_ids_page(after_event_id=None, limit=1000)
    return len(list(ids))


async def _await_peer_recall(app, dest_hex: str, timeout: float = 60.0) -> bool:
    """Wait until the runtime process can recall the peer's LXMF identity.

    This is the true egress precondition: LXMRouter DIRECT delivery
    encrypts to the recalled identity, so admission must only be simulated
    after recall is possible.  (The session's ``known_path_count``
    diagnostic reads ``router.path_table``, which the pinned LXMRouter
    does not expose, so it cannot arbitrate readiness.)
    """
    import RNS

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if RNS.Identity.recall(bytes.fromhex(dest_hex)) is not None:
            return True
        await asyncio.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestLxmfPairIngress:
    """Independent native peer -> RNode RF -> MEDRE durable admission."""

    async def test_native_ingress_core(self, tmp_path: Path) -> None:
        """N1 readiness/quiet + N2 ingress (plain, unicode, newline)."""
        app = await _launch(tmp_path / "lab.db", with_route=False)
        try:
            radio = app.adapters["lx_radio"]

            # -- N1: healthy local lifecycle (honest scope, no remote claims).
            info = await bounded(radio.health_check(), 15.0, "lx_radio health_check")
            assert info.health == "healthy", f"health {info.health!r}"
            diag = radio.diagnostics()
            assert diag["health_scope"] == "local_session_and_router"
            assert diag["peer_reachability"] == "unknown"
            before = await _event_count(app)

            # Quiet window with an armed peer: no RF traffic may arrive.
            if not _QUICK:
                with _PeerListener(12.0):
                    await asyncio.sleep(12.0)
                assert await _event_count(app) == before, "stale admission"

            # -- N2: unique nonced messages over RF, durably admitted.
            base = _nonce("N2")
            texts = [base + " plain"]
            if not _QUICK:
                texts = [
                    base + " plain",
                    base + " uni \u2713 \u4f60\u597d",
                    base + " nl a\nb",
                ]
            sent = await asyncio.to_thread(
                # recall (<=45s) + 3 deliveries (<=92.5s each worst case)
                _peer,
                ["send", _MEDRE_DEST(), json.dumps(texts)],
                380,
            )
            sent_by_text = {item["text"]: item["hash"] for item in sent["sent"]}
            assert len(sent_by_text) == len(texts), "peer send not accepted"
            # Peer-side layer B: each DIRECT message reached DELIVERED (0x08).
            assert all(
                item["state"] == 8 for item in sent["sent"]
            ), f"peer-side delivery not confirmed: {sent['sent']!r}"
            peer_ident = _delivery_dest_hash(_PEER_IDENTITY)
            for text in texts:
                hits = await _events_with_body(app, text)
                assert hits, f"message not durably admitted over RF: {text[:24]!r}"
                ev = hits[-1]
                assert ev.payload["body"] == text, "canonical body mismatch"
                assert ev.source_adapter == "lx_radio"
                lxmf_native = (
                    (ev.metadata.native.data or {}).get("lxmf", {})
                    if (ev.metadata.native is not None)
                    else {}
                )
                # Layer B correlation: native message hash == peer-side hash.
                assert lxmf_native.get("message_id") == sent_by_text[text], (
                    f"native hash mismatch for {text[:24]!r}: "
                    f"{lxmf_native.get('message_id')!r} != {sent_by_text[text]!r}"
                )
                # Native source identity is the peer's LXMF identity.
                assert (
                    lxmf_native.get("source_hash") == peer_ident
                ), "native source identity mismatch"
        finally:
            await _stop_app(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestLxmfPairEgress:
    """MEDRE -> RNode RF -> independent native peer."""

    async def test_native_egress_and_boundaries(self, tmp_path: Path) -> None:
        """N3 routed egress with peer receipt + N4 documented boundaries."""
        app = await _launch(tmp_path / "lab.db", with_route=True)
        try:
            fake = app.adapters["lab_src"]

            # -- N3: routed event egresses over RF; peer receives the nonce.
            # The peer listener announces at arming; wait until the runtime
            # can recall the peer's identity (the egress precondition)
            # before admission so the delivery attempt cannot fail on an
            # absent announce.
            nonce = _nonce("N3")
            packet_id = uuid.uuid4().hex
            with _PeerListener(_DELIVERY_TIMEOUT) as peer:
                assert await _await_peer_recall(
                    app, _PEER_DEST()
                ), "MEDRE runtime cannot recall the peer identity (no RF announce received)"
                await fake.simulate_inbound(
                    make_meshtastic_text_packet(
                        text=nonce,
                        sender="!peer0001",
                        channel=0,
                        packet_id=int(packet_id[:8], 16),
                    )
                )
                event = fake.inbound_events[-1]
                receipts = []
                deadline = time.monotonic() + _RECEIPT_TIMEOUT
                while time.monotonic() < deadline:
                    receipts = await app.storage.list_receipts_for_event(event.event_id)
                    if any(
                        r.status in ("sent", "failed", "dead_lettered")
                        for r in receipts
                    ):
                        break
                    await asyncio.sleep(0.5)
                assert receipts, "no durable delivery receipt appeared"
                rx = peer.packets_until(
                    lambda ps: any(nonce in (p.get("content") or "") for p in ps),
                    _DELIVERY_TIMEOUT,
                )
            hits = [p for p in rx if nonce in (p.get("content") or "")]
            assert hits, (
                f"peer did not observe the egress over RF; raw={rx!r} "
                f"receipts={[(r.status, r.adapter_message_id) for r in receipts]!r}"
            )
            latest = max(receipts, key=lambda r: r.sequence)
            # Layer B: LXMF receipt is local acceptance; RF receipt is layer C.
            assert latest.status == "sent", f"receipt {latest.status!r}"
            assert latest.target_adapter == "lx_radio"
            assert latest.route_id == "lab_egress"
            # Layer B/C hash correlation: peer saw the exact LXMF message.
            assert (
                hits[-1]["hash"] == latest.adapter_message_id
            ), "peer-observed hash differs from the receipt's native id"

            if _QUICK:
                return

            # -- N4: unicode, newline, and a multi-frame LXMF resource body.
            # The documented renderer budget (max_text_chars 16384) would
            # demand ~16 kB of RF airtime; the meaningful bounded boundary
            # here is a ~1.2 kB body, which LXMF carries as a link resource
            # (documented link-based fragmentation) instead of a single
            # packet.  No invented fragmentation semantics are asserted.
            unicode_msg = _nonce("N4-uni") + " w\u00f6rld \u2713"
            newline_msg = _nonce("N4-nl") + " line1\nline2"
            big_msg = _nonce("N4-big") + " " + "x" * 1200
            normal_msg = _nonce("N4-ok")
            cases = [unicode_msg, newline_msg, big_msg, normal_msg]
            window = _DELIVERY_TIMEOUT + len(cases) * (_PACING_SECONDS + 8.0)
            with _PeerListener(window) as peer:
                assert await _await_peer_recall(
                    app, _PEER_DEST()
                ), "MEDRE runtime cannot recall the peer identity (no RF announce received)"
                for text in cases:
                    await fake.simulate_inbound(
                        make_meshtastic_text_packet(
                            text=text,
                            sender="!peer0001",
                            channel=0,
                            packet_id=900_000 + cases.index(text),
                        )
                    )
                    await asyncio.sleep(_PACING_SECONDS + 1.0)
                received = peer.packets_until(
                    lambda ps: all(
                        key in "".join(p.get("content") or "" for p in ps)
                        for key in ("N4-uni", "N4-nl", "N4-big", "N4-ok")
                    ),
                    window,
                )
            by_nonce: dict[str, str] = {}
            for p in received:
                content = p.get("content") or ""
                for key in ("N4-uni", "N4-nl", "N4-big", "N4-ok"):
                    if key in content and key not in by_nonce:
                        by_nonce[key] = content
            assert "\u00f6" in by_nonce.get("N4-uni", ""), "unicode degraded"
            assert "\n" in by_nonce.get("N4-nl", ""), "newline degraded"
            assert (
                by_nonce.get("N4-big") == big_msg
            ), "multi-frame resource body damaged in transit"
            assert "N4-ok" in by_nonce, "adapter unusable after boundary cases"
        finally:
            await _stop_app(app)


@pytest.mark.live
@pytest.mark.hardware
@_REQUIRE_PAIR
class TestLxmfPairRelationsAndIsolation:
    """N5 identity distinction + N7 relation envelope + RF-off negative."""

    async def test_relation_envelope_and_rf_off_control(self, tmp_path: Path) -> None:
        """N5 (identical text, distinct native hashes), N7 relation envelope
        decoded by the real inbound path, then the RF-off negative with a
        physically quiesced peer radio (hub per-port VBUS cut) and a
        restored positive."""
        if _QUICK:
            pytest.skip("quick mode: relations/absence are full-mode proof")
        app = await _launch(tmp_path / "lab.db", with_route=True)
        try:
            fake = app.adapters["lab_src"]
            # -- N5: identical text twice = two RF messages, two durable
            # events with genuinely different native message hashes (LXMF
            # assigns a fresh content hash per message).  A same-hash replay
            # is not producible through the supported SDK surface and is
            # covered by the deterministic dedup unit suite instead.
            ident_text = _nonce("N5-ident")
            sent = await asyncio.to_thread(
                _peer,
                ["send", _MEDRE_DEST(), json.dumps([ident_text, ident_text])],
                280,
            )
            hashes = [item["hash"] for item in sent["sent"]]
            assert len(set(hashes)) == 2, "LXMF reused one message hash twice"
            assert all(
                item["state"] == 8 for item in sent["sent"]
            ), f"peer-side delivery not confirmed: {sent['sent']!r}"
            deadline = time.monotonic() + _RECEIPT_TIMEOUT
            hits: list = []
            while time.monotonic() < deadline:
                ids = await app.storage.list_event_ids_page(
                    after_event_id=None, limit=200
                )
                hits = []
                for eid in ids:
                    ev = await app.storage.get(eid)
                    if ev and ident_text in (ev.payload or {}).get("body", ""):
                        hits.append(ev)
                if len(hits) >= 2:
                    break
                await asyncio.sleep(1.0)
            assert len(hits) == 2, f"expected 2 durable events, got {len(hits)}"
            native_ids = {
                (ev.metadata.native.data or {}).get("lxmf", {}).get("message_id")
                for ev in hits
                if ev.metadata.native is not None
            }
            assert native_ids == set(hashes), "native hash correlation broken"

            # -- N7: MEDRE envelope (documented schema) with a reply relation
            # crosses the RNode RF hop and is decoded through the real
            # inbound codec/adapter into durable relations.
            target_event = f"evt-{uuid.uuid4().hex[:12]}"
            fallback = f"original text {uuid.uuid4().hex[:8]}"
            relation_text = _nonce("N7-rel")
            envelope = {
                "schema_version": 1,
                "event_id": f"evt-{uuid.uuid4().hex[:12]}",
                "source_adapter": "peer-native",
                "source_transport_id": None,
                "source_channel_id": None,
                "lineage": [],
                "relations": [
                    {
                        "relation_type": "reply",
                        "target_event_id": target_event,
                        "target_native_ref": {
                            "adapter": "lx_radio",
                            "native_channel_id": None,
                            "native_message_id": hashes[0],
                        },
                        "key": None,
                        "fallback_text": fallback,
                    }
                ],
                "metadata_keys": [],
            }
            sent_env = await asyncio.to_thread(
                _peer,
                # Wire format per LxmfFieldsHelper: the envelope rides
                # under the "medre" namespace key inside field 0xFD.
                [
                    "sendenv",
                    _MEDRE_DEST(),
                    relation_text,
                    json.dumps({"medre": envelope}),
                ],
                190,
            )
            assert sent_env["sent"], "relation envelope send not accepted"
            assert (
                sent_env["sent"][0]["state"] == 8
            ), f"peer-side envelope delivery not confirmed: {sent_env!r}"
            rel_hits = await _events_with_body(app, relation_text)
            assert rel_hits, "relation envelope not durably admitted"
            ev = rel_hits[-1]
            assert ev.payload["body"] == relation_text, "envelope body mismatch"
            assert ev.relations, "relation not reconstructed by the real codec"
            rel = ev.relations[0]
            assert rel.relation_type == "reply"
            assert rel.target_event_id == target_event
            assert rel.target_native_ref is not None
            assert (
                rel.target_native_ref.native_message_id == hashes[0]
            ), "referenced native identity not preserved across RF"
            assert rel.fallback_text == fallback

            # -- N6/N7 RF-off negative: cut VBUS to the PEER radio via the
            # mapped hub port (physical power loss — labelled accurately).
            # The listener is verifiably dead (its radio is unpowered, so no
            # layer-C observation is possible), the hub status is the power
            # authority, and MEDRE may never claim delivery while the RF
            # path is physically quiesced.
            if not _HAS_HUB_CONTROL:
                pytest.skip("RF-off control needs LXMF_PEER_HUB and LXMF_PEER_HUB_PORT")
            probe = _nonce("N6-ghost")

            subprocess.run(
                [
                    "/usr/sbin/uhubctl",
                    "-l",
                    _PEER_HUB,
                    "-p",
                    _PEER_HUB_PORT,
                    "-a",
                    "off",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                out = subprocess.run(
                    ["/usr/sbin/uhubctl", "-l", _PEER_HUB, "-p", _PEER_HUB_PORT],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                assert "0000" in out, f"hub port not off: {out!r}"
                await fake.simulate_inbound(
                    make_meshtastic_text_packet(
                        text=probe,
                        sender="!peer0001",
                        channel=0,
                        packet_id=950_001,
                    )
                )
                # Bounded absence: the correlated message cannot arrive via a
                # hidden host-local path while the RF path is physically dead.
                # MEDRE-side admission is expected (layer A); the honest
                # acceptance semantics must not fabricate a peer delivery.
                absence_deadline = time.monotonic() + 35.0
                delivered_claim = False
                while time.monotonic() < absence_deadline:
                    counts = app.adapters["lx_radio"].session.delivery_state_counts()
                    if counts.get("delivered"):
                        delivered_claim = True
                        break
                    await asyncio.sleep(2.0)
                assert not delivered_claim, "delivery claimed with the RF path dead"
                assert await _events_with_body(
                    app, probe
                ), "probe event missing MEDRE-side (layer A regression)"
            finally:
                subprocess.run(
                    [
                        "/usr/sbin/uhubctl",
                        "-l",
                        _PEER_HUB,
                        "-p",
                        _PEER_HUB_PORT,
                        "-a",
                        "on",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                time.sleep(4.0)

            # -- Restored positive: after VBUS restore, LXMF first completes
            # the still-pending ghost delivery (retry ladder) — its arrival
            # at the freshly armed listener IS the restored-RF proof for the
            # pre-off nonce.  Then a fresh nonce proves the adapter stays
            # usable after the outage.
            restored = _nonce("N6-positive")
            with _PeerListener(420.0) as peer:
                assert await _await_peer_recall(
                    app, _PEER_DEST()
                ), "MEDRE runtime cannot recall the peer identity after restore"
                await fake.simulate_inbound(
                    make_meshtastic_text_packet(
                        text=restored,
                        sender="!peer0001",
                        channel=0,
                        packet_id=950_002,
                    )
                )
                rx = peer.packets_until(
                    lambda ps: any(
                        probe in (p.get("content") or "")
                        or restored in (p.get("content") or "")
                        for p in ps
                    ),
                    300.0,
                )
                got_restored = [p for p in rx if restored in (p.get("content") or "")]
                got_ghost = [p for p in rx if probe in (p.get("content") or "")]
                assert got_ghost or got_restored, (
                    "neither the pending ghost nor the fresh nonce arrived "
                    f"after restore; raw={rx!r}"
                )
                if not got_ghost:
                    # Ghost correlation proves the restored path; still give
                    # the fresh nonce its own bounded chance to land.
                    rx2 = peer.packets_until(
                        lambda ps: any(
                            restored in (p.get("content") or "") for p in ps
                        ),
                        120.0,
                    )
                    got_restored = [
                        p for p in rx2 if restored in (p.get("content") or "")
                    ]
                    assert got_restored, (
                        "fresh nonce not delivered after restore; " f"raw2={rx2!r}"
                    )
        finally:
            await _stop_app(app)
