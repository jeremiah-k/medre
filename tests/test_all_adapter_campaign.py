# medre-test-tier: compatibility
"""Cross-provider text routing through real storage with SDK-free adapters.

Every ordered pair is named explicitly, including Chatmail. Missing providers
skip their own cases; the native campaign requires all six unless --only is set.
"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from medre.adapter_registry import registered_transports
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.smoke_ingress import make_smoke_event
from scripts.adapter_campaign.evidence import verify, wait_evidence
from scripts.adapter_campaign.plan import TRANSPORTS, Plan, runtime_config

CHANNELS = {
    "matrix": "!campaign:example.org",
    "meshtastic": "0",
    "meshcore": "1",
    "lxmf": "ab" * 16,
    "xmpp": "campaign@conference.example.org",
    "chatmail": "10",
}
PAIRS = [
    (source, target)
    for source in sorted(TRANSPORTS)
    for target in sorted(TRANSPORTS)
    if source != target
]


def fake_plan(tmp_path: Path, transports: set[str]) -> Plan:
    runtime_file = tmp_path / "runtime.yaml"
    settings = {
        "matrix": {
            "homeserver": "https://example.org",
            "user_id": "@bot:example.org",
            "access_token": "fake",
            "room_allowlist": [CHANNELS["matrix"]],
            "encryption_mode": "plaintext",
        },
        "meshtastic": {"connection_type": "fake"},
        "meshcore": {"connection_type": "fake"},
        "lxmf": {"connection_type": "fake"},
        "xmpp": {"jid": "bot@example.org", "rooms": [CHANNELS["xmpp"]]},
        "chatmail": {"chats": [10]},
    }
    runtime_file.write_text(
        yaml.safe_dump(
            {
                "adapters": {
                    transport: {
                        transport: {
                            "enabled": True,
                            "adapter_kind": "fake",
                            **settings[transport],
                        }
                    }
                    for transport in transports
                }
            }
        )
    )
    endpoints = {
        transport: {
            "transport": transport,
            "channel": CHANNELS[transport],
            "expected_sender": "bot",
            "peer": {},
        }
        for transport in sorted(transports)
    }
    return Plan(runtime_file, endpoints, 5, 0, 0.1)


@pytest.mark.parametrize(
    ("source", "target"),
    PAIRS,
    ids=[f"{source}-to-{target}" for source, target in PAIRS],
)
async def test_every_directed_adapter_pair_stores_one_delivery(
    source, target, tmp_path
):
    missing = {source, target} - set(registered_transports())
    if missing:
        pytest.skip(f"Install adapter providers: {', '.join(sorted(missing))}")
    plan = fake_plan(tmp_path, {source, target})
    config, paths = runtime_config(plan, tmp_path, fake=True)
    app = RuntimeBuilder(config, paths).build()
    try:
        await app.start()
        assert not app.build_failures
        marker = f"MR-pair-{uuid4().hex} café ✓"
        event = make_smoke_event(
            app.adapters[source], marker, source_context=CHANNELS[source]
        )
        await app.pipeline_runner.handle_ingress(event)
        item = await wait_evidence(
            app.storage,
            marker,
            source=source,
            channel=CHANNELS[source],
            targets={target: CHANNELS[target]},
            timeout=5,
        )
        assert item["event"]["event_id"] == event.event_id
        delivered = app.adapters[target].delivered_payloads
        assert len(delivered) == 1
        assert marker in str(delivered[0].payload)
        assert delivered[0].target_channel == CHANNELS[target]
        assert app.adapters[source].delivered_payloads == []
        # The oracle must reject duplicates, incorrect addressing, and lost
        # attempt provenance rather than merely finding one successful row.
        for mutation in ("duplicate", "channel", "attempt"):
            corrupt = deepcopy(item)
            if mutation == "duplicate":
                corrupt["receipts"].append(corrupt["receipts"][0])
            elif mutation == "channel":
                corrupt["receipts"][0]["target_channel"] = "wrong"
            else:
                corrupt["outbox"][0]["attempt_number"] = 2
            with pytest.raises(AssertionError):
                verify(
                    corrupt,
                    source=source,
                    channel=CHANNELS[source],
                    targets={target: CHANNELS[target]},
                )
    finally:
        await app.stop()


@pytest.mark.parametrize("source", sorted(TRANSPORTS))
async def test_source_fans_out_to_every_installed_adapter_without_self_delivery(
    source, tmp_path
):
    installed = TRANSPORTS & set(registered_transports())
    if source not in installed or len(installed) < 2:
        pytest.skip("Install source and at least one target adapter")
    plan = fake_plan(tmp_path, installed)
    config, paths = runtime_config(plan, tmp_path, fake=True)
    app = RuntimeBuilder(config, paths).build()
    try:
        await app.start()
        assert set(app.started_adapter_ids) == installed
        marker = f"MR-fanout-{uuid4().hex} café ✓"
        event = make_smoke_event(
            app.adapters[source], marker, source_context=CHANNELS[source]
        )
        await app.pipeline_runner.handle_ingress(event)
        targets = {target: CHANNELS[target] for target in installed - {source}}
        await wait_evidence(
            app.storage,
            marker,
            source=source,
            channel=CHANNELS[source],
            targets=targets,
            timeout=5,
        )
        for target in targets:
            assert len(app.adapters[target].delivered_payloads) == 1
        assert app.adapters[source].delivered_payloads == []
    finally:
        await app.stop()
