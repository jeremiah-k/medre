# medre-test-tier: compatibility
"""Production assembly binds activity sinks and drains before native teardown."""

from dataclasses import replace

import pytest

from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre.core.contracts.typing import TypingActivity
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from tests.helpers.async_utils import wait_until


class TypingFake(FakeTransportAdapter):
    """Use the existing fake message adapter with an independently recorded port."""

    def __init__(self, adapter_id, *, declared=True):
        """Record activity separately from rendered message delivery."""
        super().__init__(adapter_id)
        self.typing_updates = []
        self.declared = declared

    async def send_typing(self, update):
        """Assert that every desired-state send precedes adapter shutdown."""
        assert self._started
        self.typing_updates.append(update)

    async def health_check(self):
        """Report the capability through the same contract as native adapters."""
        info = await super().health_check()
        return replace(
            info, capabilities=replace(info.capabilities, typing=self.declared)
        )


def assemble(tmp_path, monkeypatch, *, forwarding=True, declared=True, supported=True):
    """Build a production app from parsed YAML-shaped configuration and fake ports."""
    paths = resolve(environ={"MEDRE_HOME": str(tmp_path)})
    config = parse_runtime_config(
        {
            "adapters": {
                "matrix": {
                    name: {
                        "adapter_kind": "fake",
                        "encryption_mode": "plaintext",
                        "homeserver": "https://matrix.example.invalid",
                        "user_id": "@bot:example.invalid",
                        "access_token": "test-token",
                    }
                    for name in ("source", "target")
                }
            },
            "routes": {
                "activity": {
                    "source_adapters": ["source"],
                    "dest_adapters": ["target"],
                    "source_channel": "!a:example.invalid",
                    "dest_channel": "!b:example.invalid",
                    "forward_typing": forwarding,
                }
            },
            "storage": {"backend": "sqlite", "path": str(tmp_path / "events.sqlite")},
        },
        paths,
    )
    ports = {
        name: (
            TypingFake(name, declared=declared)
            if supported
            else FakeTransportAdapter(name)
        )
        for name in ("source", "target")
    }

    def build(_, adapters):
        """Replace only native construction; leave production route/lifecycle wiring live."""
        adapters.update(ports)
        return []

    monkeypatch.setattr(RuntimeBuilder, "_build_adapters", build)
    return RuntimeBuilder(config, paths).build(), ports


async def test_builder_binds_source_identity_without_creating_durable_events(
    tmp_path, monkeypatch
):
    """The actual app forwards activity and clears it before closing adapters."""
    app, ports = assemble(tmp_path, monkeypatch)
    try:
        await app.start()
        sink = ports["source"].ctx.publish_typing
        assert sink is not None
        assert await sink(TypingActivity("!a:example.invalid", "alice", True))
        assert await wait_until(lambda: bool(ports["target"].typing_updates))
        assert ports["target"].typing_updates[-1].typing
        assert (
            await app.storage.list_event_ids_page(after_event_id=None, limit=10) == []
        )
        assert not ports["target"].delivered_payloads
    finally:
        await app.stop()
    assert not ports["target"].typing_updates[-1].typing
    assert not await sink(TypingActivity("!a:example.invalid", "alice", True))


async def test_default_route_has_no_activity_sink_or_background_worker(
    tmp_path, monkeypatch
):
    """Existing message routes do not disclose typing or start an activity worker."""
    app, ports = assemble(tmp_path, monkeypatch, forwarding=False, supported=False)
    try:
        await app.start()
        assert app._typing_relay is None
        assert ports["source"].ctx.publish_typing is None
    finally:
        await app.stop()


async def test_false_capability_declaration_rolls_back_started_adapters(
    tmp_path, monkeypatch
):
    """Having a callable port does not override a provider's declared capability."""
    app, ports = assemble(tmp_path, monkeypatch, declared=False)
    with pytest.raises(RuntimeError, match="without declaring"):
        await app.start()
    assert not any(port._started for port in ports.values())
    assert not app.started_adapter_ids


def test_unsupported_opt_in_fails_before_runtime_start(tmp_path, monkeypatch):
    """An explicit feature request cannot silently pass with a legacy adapter."""
    with pytest.raises(RuntimeConfigError, match="typing sender ports"):
        assemble(tmp_path, monkeypatch, supported=False)
