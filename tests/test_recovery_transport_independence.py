"""Recovery advice depends on durable evidence, not transport-name guesses."""

import msgspec
import pytest

from medre.cli.recover_commands import _build_event_recovery_runbook


@pytest.mark.parametrize(
    "adapter",
    ["private-chat-bridge", "meshtastic-looking-name", "uninstalled-provider"],
)
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_duplicate_warning_uses_outbound_evidence(
    temp_storage, sample_event, sample_native_message_ref, adapter: str, direction: str
) -> None:
    await temp_storage.append(sample_event)
    reference = msgspec.structs.replace(
        sample_native_message_ref, adapter=adapter, direction=direction
    )
    await temp_storage.store_native_ref(reference)
    report = await _build_event_recovery_runbook(
        temp_storage, sample_event.event_id, storage_path="test.sqlite"
    )
    assert report is not None
    warnings = report["warnings"]
    assert any("duplicate sends" in warning for warning in warnings) is (
        direction == "outbound"
    )
    assert all("radio" not in warning.lower() for warning in warnings)
