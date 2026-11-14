# medre-test-tier: compatibility
"""Native exact-target qualification: the LXMF propagation leg.

Evidence tier: real pinned LXMF/RNS SDKs on this machine, two independent
Reticulum instances wired only by a loopback ``UDPInterface`` pair
(127.0.0.1, per-run free ports, per-process ``HOME`` and router storage).
The sender and receiver share an owned POSIX group that is terminated even
when the outer scenario times out or its test task is cancelled.
Only disposable test identities are used; no external network or hardware. This is
local SDK/integration evidence with an independently observed recipient
instance; it is not external-network or hardware evidence.

Topology:

- Instance B runs in its own process on the raw pinned SDKs: an
  ``LXMRouter`` with propagation enabled (a real propagation node) and one
  registered ``lxmf.delivery`` destination. It is the independent
  recipient: every verdict about arrival comes from B's own delivery
  callback, never from MEDRE reading back its own receipts.
- Instance A is the MEDRE side: the real ``LxmfSession`` from the
  installed ``medre-adapter-lxmf`` distribution, configured with the exact
  16-byte destination hashes observed from B's readiness file, sending
  with ``delivery_method="propagated"``.

Exact-target properties qualified at this tier:

* **Propagation handoff is not recipient receipt.** A's honest terminal
  state tops out at ``sent`` — the propagation node accepted the message —
  while the recipient truth is recorded separately, in B's process, from
  B's own callback. The two verdicts are asserted as distinct evidence.
* **Exact destination, independently observed.** B observes the message
  addressed to exactly B's announced delivery destination hash, sourced
  from A's announced delivery destination, with the propagation delivery
  method — and exactly one delivery to B. A's own destination remains silent;
  unobserved destinations are outside this two-instance observation.
* **Content-addressed identity survives the native path.** The native
  message id A handed off equals the message hash B observed.
* **No handoff without an observed exact destination.** A propagated send
  to a destination hash A has never observed natively fails the pinned
  discovery gate. An SDK-constructor probe records zero message constructions,
  the sender has no outbound delivery state, and B receives no callback.
  These observations do not independently count propagation-node intake.

Both Reticulum instances live in child processes: Reticulum is a
process-wide singleton, so the pytest process itself never constructs one
and every run starts from a fresh per-process environment. Coordination is
deadline-bounded polling of observable artifacts (readiness files,
``RNS.Identity.recall``, delivery-state counters) — no fixed sleeps.

The scenario scripts are materialised into the test's temporary directory
and executed with the running interpreter, so they run against exactly the
installed adapter distribution and the SDK versions it pins.

CI collection note: the required ``core-owned-lxmf-integration`` workflow
job installs the pinned adapter distribution and its exact-pinned
RNS/LXMF SDKs, executes this file with ``-m "local_integration and
lxmf_sdk"``, and fails when the scenario is not collected, is skipped, or
falls short of its expected execution count, so a pass on that job is
executed-at-pin native evidence for this loopback topology. The test also
runs on any POSIX host where the pinned SDK extra is installed
(``pip install 'medre-adapter-lxmf'``) via
``pytest tests/integration/test_lxmf_exact_target_propagation.py -m
"local_integration and not soak" -v``; external-network and hardware
rungs remain outstanding and are tracked in the exact-target recovery
specification's qualification section.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.lxmf_scenario import run_owned_scenario
from scripts.ci.native_qualification import provider_provenance

try:
    from medre.adapters.lxmf.compat import HAS_LXMF
except ImportError:
    # The lxmf adapter distribution is absent in SDK-less environments
    # (the docker-integration job installs only its own transports); the
    # skipif marker below then deselects both tests at collection.
    HAS_LXMF = False

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership"),
    pytest.mark.local_integration,
    pytest.mark.lxmf_sdk,
    pytest.mark.skipif(
        not HAS_LXMF,
        reason="requires the pinned lxmf/rns SDKs (pip install 'medre-adapter-lxmf')",
    ),
]

_RESULT_PREFIX = "MEDRE_EXACT_TARGET_PROPAGATION_RESULT="
_SCENARIO_TIMEOUT = 300.0


@pytest.fixture(autouse=True)
def selected_provider_provenance() -> None:
    """CI qualifies the selected editable provider, not just matching SDK peers."""
    checkout = os.environ.get("MEDRE_LXMF_PROVIDER_ROOT")
    candidate_refs = os.environ.get("MEDRE_CI_CANDIDATE_REFS")
    if checkout or candidate_refs:
        root = Path(__file__).resolve().parents[2]
        provider_provenance(
            root,
            Path(checkout) if checkout else root / ".adapter",
            "lxmf",
            candidate_refs=candidate_refs,
        )


# The receiver (instance B) runs on the raw pinned SDKs. It is the
# independent recipient and a real propagation node; nothing in it imports
# MEDRE, so none of its observations can be MEDRE reading its own state.
_RECEIVER_SOURCE = r"""
import json
import sys
import threading
import time
from pathlib import Path

base = Path(sys.argv[1]).resolve()
config_dir = base / "home-b" / ".reticulum"
stop_path = base / "stop"
received_path = base / "b-received.json"
ready_path = base / "b-ready.json"

import LXMF
import RNS

_lock = threading.Lock()
_records = []


def _write_json(payload, path=received_path):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _method_name(raw):
    constants = {
        LXMF.LXMessage.OPPORTUNISTIC: "opportunistic",
        LXMF.LXMessage.DIRECT: "direct",
        LXMF.LXMessage.PROPAGATED: "propagated",
    }
    return constants.get(raw)


def _on_delivery(message):
    try:
        record = {
            "delivery_method": _method_name(getattr(message, "method", None)),
            "destination_hash": message.destination_hash.hex(),
            "source_hash": message.source_hash.hex(),
            "message_hash": message.hash.hex(),
            "title": bytes(message.title).decode("utf-8"),
            "body": message.content_as_string(),
        }
        with _lock:
            _records.append(record)
            _write_json(
                {
                    "received": True,
                    "delivery_count": len(_records),
                    "deliveries_so_far": len(_records),
                    "lxmf_version": str(LXMF.__version__),
                    "rns_version": str(RNS.__version__),
                    **record,
                }
            )
    except Exception as exc:  # a broken callback must not kill the harness
        _write_json({"received": False, "callback_error": repr(exc)})


reticulum = RNS.Reticulum(configdir=str(config_dir))
del reticulum
identity = RNS.Identity()
router = LXMF.LXMRouter(identity=identity, storagepath=str(base / "b" / "router"))
delivery_destination = router.register_delivery_identity(
    identity, display_name="medre-exact-target-b", stamp_cost=1
)
if delivery_destination is None:
    _write_json(
        {"received": False, "error": "delivery identity registration failed"},
        ready_path,
    )
    raise SystemExit(1)
router.register_delivery_callback(_on_delivery)
router.enable_propagation()

_write_json(
    {
        "delivery_hash": delivery_destination.hash.hex(),
        "propagation_hash": router.propagation_destination.hash.hex(),
        "lxmf_version": str(LXMF.__version__),
        "rns_version": str(RNS.__version__),
    },
    ready_path,
)

deadline = time.monotonic() + 150.0
next_announce = 0.0
announce_failures = 0
while time.monotonic() < deadline:
    if stop_path.is_file():
        break
    now = time.monotonic()
    if now >= next_announce:
        try:
            router.announce(delivery_destination.hash)
        except Exception:
            announce_failures += 1
        next_announce = now + 1.0
    time.sleep(0.05)

with _lock:
    if _records:
        _write_json(
            {
                "received": True,
                "delivery_count": len(_records),
                "announce_failures": announce_failures,
                "lxmf_version": str(LXMF.__version__),
                "rns_version": str(RNS.__version__),
                **_records[0],
            }
        )
    else:
        _write_json(
            {
                "received": False,
                "delivery_count": 0,
                "announce_failures": announce_failures,
                "reason": "no inbound delivery before the receiver deadline",
            }
        )
"""


# The sender (instance A) is the MEDRE side: the real LxmfSession from the
# installed adapter distribution, pointed at the exact destination hashes B
# published in its readiness file.  For the unobserved-destination leg the
# session's own native discovery gate is exercised; no announce-wait runs
# because an unobserved destination never announces.
_SENDER_SOURCE = r"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

base = Path(sys.argv[1]).resolve()
receiver_path = Path(sys.argv[2]).resolve()
missing_destination = sys.argv[3] if len(sys.argv) > 3 else None
stop_path = base / "stop"
ready_path = base / "b-ready.json"
log_path = base / "receiver.log"
result_prefix = "MEDRE_EXACT_TARGET_PROPAGATION_RESULT="

home_a = base / "home-a"
home_b = base / "home-b"


def _fail(message):
    tail = ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    except OSError:
        tail = "<receiver log unavailable>"
    raise SystemExit("exact-target propagation scenario failed: " + message + "\n" + tail)


def _await_json(path, timeout, child=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if child is not None and child.poll() is not None:
            _fail(f"receiver exited with {child.returncode} before writing {path.name}")
        time.sleep(0.05)
    _fail(f"timed out after {timeout}s waiting for {path.name}")


def _port_pair():
    import socket

    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second,
    ):
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return int(first.getsockname()[1]), int(second.getsockname()[1])


def _write_config(home, name, listen_port, forward_port):
    config_dir = home / ".reticulum"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config").write_text(
        "[reticulum]\n"
        "enable_transport = No\n"
        "share_instance = No\n"
        "\n"
        "[logging]\n"
        "loglevel = 4\n"
        "\n"
        "[interfaces]\n"
        f"  [[{name}]]\n"
        "    type = UDPInterface\n"
        "    enabled = Yes\n"
        "    listen_ip = 127.0.0.1\n"
        f"    listen_port = {listen_port}\n"
        "    forward_ip = 127.0.0.1\n"
        f"    forward_port = {forward_port}\n",
        encoding="utf-8",
    )


def _sdk_version(distribution):
    import importlib.metadata

    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _main():
    from medre.adapters.lxmf.compat import _require_lxmf
    from medre.adapters.lxmf.errors import LxmfSendError
    from medre.adapters.lxmf.session import LxmfSession
    from medre.config.adapters.lxmf import LxmfConfig

    port_a, port_b = _port_pair()
    _write_config(home_a, "medre-exact-target-a", port_a, port_b)
    _write_config(home_b, "medre-exact-target-b", port_b, port_a)

    child_log = open(log_path, "wb")
    try:
        child = subprocess.Popen(
            [sys.executable, str(receiver_path), str(base)],
            env={**os.environ, "HOME": str(home_b)},
            stdout=child_log,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        child_log.close()
        raise

    try:
        ready = _await_json(ready_path, 60.0, child)
        if "error" in ready:
            _fail(f"receiver readiness failed: {ready['error']}")
        b_delivery = str(ready["delivery_hash"])
        b_propagation = str(ready["propagation_hash"])

        os.environ["HOME"] = str(home_a)

        async def run():
            config = LxmfConfig(
                adapter_id="medre-exact-target-a",
                connection_type="reticulum",
                storage_path=str(base / "a" / "router"),
                announce_interval_seconds=0,
                message_delay_seconds=0,
                outbound_propagation_node=b_propagation,
            )
            session = LxmfSession(
                config=config,
                adapter_id=config.adapter_id,
                reticulum_config_dir=str(home_a / ".reticulum"),
            )
            inbound = []

            async def on_message(payload):
                inbound.append(payload)

            await session.start(on_message)
            a_delivery = session._delivery_destination_hash
            if a_delivery is None:
                await session.stop()
                _fail("MEDRE session registered no delivery destination")
            nonce = os.urandom(8).hex()
            content = f"medre exact-target propagation body {nonce}"
            title = "medre exact-target propagation"
            verdict = {
                "b_ready": True,
                "a_started": True,
                "a_handoff_accepted": False,
                "a_native_id": None,
                "a_initial_state": None,
                "a_handoff_error": None,
                "a_delivery_hash": a_delivery.hex(),
                "a_inbound_count": len(inbound),
                "a_native_message_constructions": 0,
            }
            try:
                if missing_destination is None:
                    target = b_delivery
                    # Deterministic preconditions for the observed leg: the
                    # exact destination announce has been recalled natively
                    # and the router has learned the announced stamp cost.
                    rns, _lxmf = _require_lxmf()
                    router = session._router
                    deadline = time.monotonic() + 30.0
                    while rns.Identity.recall(bytes.fromhex(target)) is None:
                        if time.monotonic() > deadline:
                            await session.stop()
                            _fail(
                                "sender never observed the exact destination "
                                f"announce for {target}"
                            )
                        await asyncio.sleep(0.05)
                    deadline = time.monotonic() + 30.0
                    while router.get_outbound_stamp_cost(bytes.fromhex(target)) is None:
                        if time.monotonic() > deadline:
                            await session.stop()
                            _fail("sender router never learned the stamp cost")
                        await asyncio.sleep(0.05)
                else:
                    # Unobserved destination: send directly so the session's
                    # own native discovery gate decides the handoff.
                    target = missing_destination

                # Observe the actual SDK construction boundary for the
                # missing-destination scenario, not just the adapter result.
                # The real adapter uses this module object to construct LXMessage.
                if missing_destination is not None:
                    _rns, sdk_lxmf = _require_lxmf()
                    original_constructor = sdk_lxmf.LXMessage

                    class ConstructionObserved(original_constructor):
                        def __init__(self, *args, **kwargs):
                            verdict["a_native_message_constructions"] += 1
                            super().__init__(*args, **kwargs)

                    sdk_lxmf.LXMessage = ConstructionObserved
                try:
                    try:
                        native_id, initial_state = await session.send_text(
                            target,
                            content,
                            title=title,
                            delivery_method="propagated",
                        )
                    except LxmfSendError as exc:
                        if missing_destination is None:
                            raise
                        assert str(exc) == "Native destination discovery is pending"
                        assert getattr(exc, "transient", False) is True
                        assert session.delivery_state_counts() == {}
                        assert verdict["a_native_message_constructions"] == 0
                        verdict["handoff_refused_unobserved"] = True
                        verdict["a_inbound_count"] = len(inbound)
                        await session.stop()
                        return verdict, content
                finally:
                    if missing_destination is not None:
                        sdk_lxmf.LXMessage = original_constructor

                verdict["a_native_id"] = native_id
                verdict["a_initial_state"] = str(
                    getattr(initial_state, "value", initial_state)
                )

                deadline = time.monotonic() + 120.0
                while time.monotonic() < deadline:
                    counts = session.delivery_state_counts()
                    if counts.get("sent", 0) >= 1:
                        verdict["a_handoff_accepted"] = True
                        break
                    if any(
                        counts.get(state, 0) > 0
                        for state in ("failed", "rejected", "cancelled")
                    ):
                        verdict["a_handoff_error"] = (
                            f"terminal failure states {counts}"
                        )
                        break
                    await asyncio.sleep(0.05)
                if (
                    not verdict["a_handoff_accepted"]
                    and verdict["a_handoff_error"] is None
                ):
                    verdict["a_handoff_error"] = (
                        "handoff state not reached within deadline; "
                        f"delivery_state_counts={session.delivery_state_counts()}"
                    )

                if verdict["a_handoff_accepted"]:
                    # A native handoff is not B's delivery callback. Keep the
                    # session alive until B independently records the receipt.
                    receipt_deadline = time.monotonic() + 60.0
                    while time.monotonic() < receipt_deadline:
                        try:
                            received = json.loads(
                                (base / "b-received.json").read_text(encoding="utf-8")
                            )
                            if received.get("received") is True:
                                break
                        except (OSError, ValueError):
                            pass
                        await asyncio.sleep(0.05)
                verdict["a_inbound_count"] = len(inbound)
                await session.stop()
                return verdict, content

            except BaseException:
                try:
                    await session.stop()
                except BaseException:
                    pass
                raise

        verdict, content = asyncio.run(run())
    finally:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text("done", encoding="utf-8")
        try:
            if child.poll() is None:
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
        finally:
            child_log.close()

    final = _await_json(base / "b-received.json", 30.0)

    if missing_destination is not None:
        result = {
            "b_ready": True,
            "a_started": True,
            "handoff_refused_unobserved": bool(
                verdict.get("handoff_refused_unobserved")
            ),
            "b_delivery_count": int(final.get("delivery_count", 0)),
            "a_inbound_count": int(verdict.get("a_inbound_count", 0)),
            "a_native_message_constructions": int(
                verdict["a_native_message_constructions"]
            ),
        }
    else:
        result = {
            "b_ready": True,
            "a_started": True,
            "a_handoff_accepted": bool(verdict["a_handoff_accepted"]),
            "a_native_id": verdict["a_native_id"],
            "a_initial_state": verdict["a_initial_state"],
            "a_inbound_count": int(verdict.get("a_inbound_count", 0)),
            "b_received": final.get("received") is True,
            "b_delivery_method": final.get("delivery_method"),
            "b_body": final.get("body"),
            "b_title": final.get("title"),
            "b_message_hash": final.get("message_hash"),
            "b_destination_hash": final.get("destination_hash"),
            "b_source_hash": final.get("source_hash"),
            "b_delivery_count": int(final.get("delivery_count", 0)),
            "b_delivery_hash": b_delivery,
            "a_delivery_hash": verdict["a_delivery_hash"],
            "sent_content": content,
            "a_lxmf_version": _sdk_version("lxmf"),
            "a_rns_version": _sdk_version("rns"),
            "b_lxmf_version": final.get("lxmf_version"),
            "b_rns_version": final.get("rns_version"),
        }
    print(result_prefix + json.dumps(result, sort_keys=True))

    if missing_destination is None and not result["a_handoff_accepted"]:
        _fail(verdict.get("a_handoff_error") or "propagation handoff not accepted")
    if missing_destination is None and not final.get("received"):
        _fail(f"receiver reported no delivery: {json.dumps(final, sort_keys=True)}")


if __name__ == "__main__":
    _main()
"""


def _materialise(workdir: Path) -> tuple[Path, Path]:
    """Write both scenario scripts and return (sender, receiver) paths."""
    sender = workdir / "propagation_sender.py"
    receiver = workdir / "propagation_receiver.py"
    sender.write_text(_SENDER_SOURCE, encoding="utf-8")
    receiver.write_text(_RECEIVER_SOURCE, encoding="utf-8")
    return sender, receiver


async def _run_scenario(
    workdir: Path, missing_destination: str | None = None
) -> dict[str, Any]:
    """Run the two-instance propagation scenario in isolated processes."""
    sender, receiver = _materialise(workdir)
    command = [sys.executable, str(sender), str(workdir), str(receiver)]
    if missing_destination is not None:
        command.append(missing_destination)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    try:
        completed = await run_owned_scenario(
            command, timeout=_SCENARIO_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"LXMF scenario timed out after {_SCENARIO_TIMEOUT}s")
    if completed.returncode != 0:
        pytest.fail(
            "LXMF exact-target propagation scenario failed with exit code "
            f"{completed.returncode}\nstdout:\n{completed.stdout}\n\nstderr:\n"
            f"{completed.stderr}"
        )
    result_line = next(
        (
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        pytest.fail(
            "LXMF exact-target propagation scenario emitted no result line\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
        )
    return json.loads(result_line[len(_RESULT_PREFIX) :])


async def test_propagated_delivery_reaches_independent_receiver(tmp_path: Path) -> None:
    """A propagated handoff reaches the independent second instance.

    Evidence layers stay distinct in the verdict: A's ``sent`` state is the
    propagation node accepting the handoff (native acceptance, not receipt);
    every recipient fact comes from B's own callback in B's own process.
    """
    result = await _run_scenario(tmp_path)

    assert result["b_ready"] is True
    assert result["a_started"] is True
    assert result["a_handoff_accepted"] is True
    assert isinstance(result["a_native_id"], str) and result["a_native_id"]
    # Honest handoff semantics: the reported state at handoff is a pre-send
    # state, never a recipient receipt.
    assert result["a_initial_state"] in {"generating", "outbound"}
    assert result["a_inbound_count"] == 0, "A's own destination must stay silent"

    assert result["b_received"] is True
    assert result["b_delivery_method"] == "propagated"
    assert result["b_body"] == result["sent_content"]
    assert result["b_title"] == "medre exact-target propagation"
    assert result["b_destination_hash"] == result["b_delivery_hash"]
    assert result["b_source_hash"] == result["a_delivery_hash"]
    assert result["b_message_hash"] == result["a_native_id"]
    assert result["b_delivery_count"] == 1, "exactly one delivery to B"

    assert result["a_lxmf_version"] == result["b_lxmf_version"]
    assert result["a_rns_version"] == result["b_rns_version"]
    assert result["a_lxmf_version"] and result["a_rns_version"]


async def test_unobserved_exact_destination_never_hands_off(tmp_path: Path) -> None:
    """A propagated send to an unobserved destination hands off nothing.

    The pinned native discovery gate refuses before SDK message construction;
    a constructor probe and an independent recipient callback establish the
    negative result without treating a transient error as sufficient proof.
    """
    missing = os.urandom(16).hex()
    result = await _run_scenario(tmp_path, missing_destination=missing)

    assert result["b_ready"] is True
    assert result["a_started"] is True
    assert result["handoff_refused_unobserved"] is True, (
        "an unobserved destination must produce a transient refusal, " "never a handoff"
    )
    assert result["b_delivery_count"] == 0, "the recipient callback must stay silent"
    assert result["a_inbound_count"] == 0
    assert result["a_native_message_constructions"] == 0
