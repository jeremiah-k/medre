"""Capability truth audit tests: durable proof that capability declarations are
authoritative runtime truth and do not overclaim.

Turns the Wave 1 capability audit (``docs/dev/capability-truth-audit.md``)
into machine-checked conformance proof.  The audit found 84 declarations
(4 adapters × 21 fields) that all pass, with 0 overclaims and 0 actionable
underclaims.  These tests lock that finding in place.

Evidence tiers: ``fake_pipeline`` (tier 1) and ``fake_adapter_callback`` (tier 2).
No network, no hardware, no optional SDK dependencies for fake-adapter checks.
Real-adapter imports use ``pytest.skip`` when SDKs are unavailable so the suite
runs cleanly in reduced-dependency environments.

Covers:
- Triplicate conformance: JSON profile == real adapter code == fake adapter
  for every capability field on every transport.
- PC focus fields: delivery_receipts, reactions, edits, threading,
  room/channel discovery, membership visibility, attachment support,
  history retrieval, read receipts, typing indicators, presence.
- Semantic honesty: LXMF delivery_receipts=False despite SDK delivery state,
  Matrix delivery_receipts=True as server-ACK only, MeshCore E2EE vs
  identity_encryption, Meshtastic store_and_forward vs firmware support.
- Audit summary invariant: 4 × 21 = 84, zero overclaims.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import pytest

from medre.core.contracts.adapter import AdapterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSPORTS = ("matrix", "meshtastic", "meshcore", "lxmf")

FIELD_COUNT = len(dataclass_fields(AdapterCapabilities))
# 21 fields as of the audit date.
assert FIELD_COUNT == 21, (
    f"AdapterCapabilities has {FIELD_COUNT} fields, expected 21. "
    "Update this test and the audit document together."
)

PROFILES_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "spec" / "transport-profiles"
)

_SENTINEL = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json_caps(transport: str) -> dict[str, Any]:
    """Load capabilities JSON for *transport*."""
    path = PROFILES_DIR / f"{transport}-capabilities.json"
    assert path.exists(), f"Missing capabilities file: {path}"
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["transport"] == transport
    return data["capabilities"]


def _get_real_caps(transport: str) -> AdapterCapabilities | None:
    """Return real adapter capabilities, or None when the SDK is unavailable."""
    try:
        if transport == "matrix":
            from medre.adapters.matrix.adapter import _MATRIX_CAPABILITIES

            return _MATRIX_CAPABILITIES
        if transport == "lxmf":
            from medre.adapters.lxmf.adapter import _LXMF_CAPABILITIES

            return _LXMF_CAPABILITIES
        if transport == "meshtastic":
            from medre.adapters.meshtastic.adapter import MeshtasticAdapter
            from medre.config.adapters.meshtastic import MeshtasticConfig

            config = MeshtasticConfig(adapter_id="audit_test")
            return MeshtasticAdapter(config)._capabilities
        if transport == "meshcore":
            from medre.adapters.meshcore.adapter import MeshCoreAdapter
            from medre.config.adapters.meshcore import MeshCoreConfig

            config = MeshCoreConfig(adapter_id="audit_test")
            return MeshCoreAdapter(config)._capabilities
    except ImportError:
        return None
    raise ValueError(f"Unknown transport: {transport}")


def _get_fake_caps(transport: str) -> AdapterCapabilities:
    """Return fake adapter capabilities.  No SDK dependency required."""
    if transport == "matrix":
        from medre.adapters.fakes.matrix import _FAKE_MATRIX_CAPABILITIES

        return _FAKE_MATRIX_CAPABILITIES
    if transport == "meshtastic":
        from medre.adapters.fakes.meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        return _FAKE_MESHTASTIC_CAPABILITIES
    if transport == "meshcore":
        from medre.adapters.fakes.meshcore import _FAKE_MESHCORE_CAPABILITIES

        return _FAKE_MESHCORE_CAPABILITIES
    if transport == "lxmf":
        from medre.adapters.fakes.lxmf import _FAKE_LXMF_CAPABILITIES

        return _FAKE_LXMF_CAPABILITIES
    raise ValueError(f"Unknown transport: {transport}")


def _all_field_names() -> set[str]:
    return {f.name for f in dataclass_fields(AdapterCapabilities)}


# ---------------------------------------------------------------------------
# 1. Triplicate conformance: JSON == code == fake for every field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_json_matches_real_adapter_for_every_field(transport: str) -> None:
    """Every JSON value matches the real adapter source code."""
    real_caps = _get_real_caps(transport)
    if real_caps is None:
        pytest.skip(f"{transport} SDK not installed")
    json_caps = _load_json_caps(transport)

    mismatches: list[str] = []
    for key, json_val in json_caps.items():
        actual = getattr(real_caps, key, _SENTINEL)
        if actual is _SENTINEL:
            mismatches.append(f"  {key}: in JSON but absent from AdapterCapabilities")
        elif actual != json_val:
            mismatches.append(f"  {key}: json={json_val!r}, code={actual!r}")

    assert not mismatches, f"JSON vs code mismatches for {transport}:\n" + "\n".join(
        mismatches
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_json_matches_fake_adapter_for_every_field(transport: str) -> None:
    """Every JSON value matches the fake adapter capabilities."""
    fake_caps = _get_fake_caps(transport)
    json_caps = _load_json_caps(transport)

    mismatches: list[str] = []
    for key, json_val in json_caps.items():
        actual = getattr(fake_caps, key, _SENTINEL)
        if actual is _SENTINEL:
            mismatches.append(
                f"  {key}: in JSON but absent from fake AdapterCapabilities"
            )
        elif actual != json_val:
            mismatches.append(f"  {key}: json={json_val!r}, fake={actual!r}")

    assert not mismatches, f"JSON vs fake mismatches for {transport}:\n" + "\n".join(
        mismatches
    )


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_real_adapter_matches_fake_for_every_field(transport: str) -> None:
    """Real adapter capabilities match fake adapter capabilities."""
    real_caps = _get_real_caps(transport)
    if real_caps is None:
        pytest.skip(f"{transport} SDK not installed")
    fake_caps = _get_fake_caps(transport)

    mismatches: list[str] = []
    for field in dataclass_fields(AdapterCapabilities):
        real_val = getattr(real_caps, field.name)
        fake_val = getattr(fake_caps, field.name)
        if real_val != fake_val:
            mismatches.append(f"  {field.name}: real={real_val!r}, fake={fake_val!r}")

    assert not mismatches, f"Real vs fake mismatches for {transport}:\n" + "\n".join(
        mismatches
    )


# ---------------------------------------------------------------------------
# 2. Audit summary invariant
# ---------------------------------------------------------------------------


def test_audit_total_declaration_count() -> None:
    """4 transports × 21 fields = 84 capability declarations."""
    assert len(TRANSPORTS) * FIELD_COUNT == 84


def test_every_transport_profile_json_exists() -> None:
    """All four transport capability JSON files exist."""
    for transport in TRANSPORTS:
        path = PROFILES_DIR / f"{transport}-capabilities.json"
        assert path.exists(), f"Missing: {path}"


def test_json_has_exactly_all_adapter_capability_fields() -> None:
    """Each JSON has exactly the AdapterCapabilities field set, no more no less."""
    valid = _all_field_names()
    for transport in TRANSPORTS:
        json_caps = _load_json_caps(transport)
        json_keys = set(json_caps.keys())
        missing = valid - json_keys
        extra = json_keys - valid
        assert not missing, f"{transport} JSON missing fields: {missing}"
        assert not extra, f"{transport} JSON has unknown fields: {extra}"


# ---------------------------------------------------------------------------
# 3. PC focus fields — explicit per-transport assertions
# ---------------------------------------------------------------------------


class TestDeliveryReceipts:
    """delivery_receipts: whether the adapter can confirm delivery back to the framework."""

    def test_matrix_delivery_receipts_true(self) -> None:
        """Matrix declares True: room_send returns event_id (server ACK)."""
        fake_caps = _get_fake_caps("matrix")
        assert fake_caps.delivery_receipts is True

    def test_meshtastic_delivery_receipts_false(self) -> None:
        """Meshtastic: queue-based deliver returns 'enqueued', no ACK confirmation."""
        fake_caps = _get_fake_caps("meshtastic")
        assert fake_caps.delivery_receipts is False

    def test_meshcore_delivery_receipts_false(self) -> None:
        """MeshCore: ACK is for contact DMs only, not tracked by MEDRE."""
        fake_caps = _get_fake_caps("meshcore")
        assert fake_caps.delivery_receipts is False

    def test_lxmf_delivery_receipts_false(self) -> None:
        """LXMF: 9-state model exists but MEDRE does not wire it as receipts."""
        fake_caps = _get_fake_caps("lxmf")
        assert fake_caps.delivery_receipts is False


class TestReactions:
    """reactions: emoji / reaction support level."""

    def test_matrix_reactions_native(self) -> None:
        fake_caps = _get_fake_caps("matrix")
        assert fake_caps.reactions == "native"

    def test_meshtastic_reactions_native(self) -> None:
        fake_caps = _get_fake_caps("meshtastic")
        assert fake_caps.reactions == "native"

    def test_meshcore_reactions_unsupported(self) -> None:
        fake_caps = _get_fake_caps("meshcore")
        assert fake_caps.reactions == "unsupported"

    def test_lxmf_reactions_unsupported(self) -> None:
        fake_caps = _get_fake_caps("lxmf")
        assert fake_caps.reactions == "unsupported"


class TestEdits:
    """edits: message edit support level."""

    def test_all_transports_edits_unsupported(self) -> None:
        """No adapter implements edits.  Honest omission."""
        for transport in TRANSPORTS:
            assert (
                _get_fake_caps(transport).edits == "unsupported"
            ), f"{transport} unexpectedly declares edits support"


class TestThreadingAndReplies:
    """threading: replies and topic_rooms for conversation structure."""

    def test_matrix_replies_native(self) -> None:
        """Matrix m.in_reply_to is first-class threading."""
        assert _get_fake_caps("matrix").replies == "native"

    def test_matrix_topic_rooms_true(self) -> None:
        """Matrix rooms are named destinations."""
        assert _get_fake_caps("matrix").topic_rooms is True

    def test_meshtastic_replies_native(self) -> None:
        """Meshtastic Data.reply_id is first-class reply."""
        assert _get_fake_caps("meshtastic").replies == "native"

    def test_meshtastic_topic_rooms_false(self) -> None:
        """Meshtastic channels are numeric, not named topics."""
        assert _get_fake_caps("meshtastic").topic_rooms is False

    def test_meshcore_replies_unsupported(self) -> None:
        assert _get_fake_caps("meshcore").replies == "unsupported"

    def test_meshcore_topic_rooms_false(self) -> None:
        assert _get_fake_caps("meshcore").topic_rooms is False

    def test_lxmf_replies_unsupported(self) -> None:
        assert _get_fake_caps("lxmf").replies == "unsupported"

    def test_lxmf_topic_rooms_false(self) -> None:
        assert _get_fake_caps("lxmf").topic_rooms is False


class TestRoomChannelDiscovery:
    """channels, direct_messages, topic_rooms: destination model."""

    def test_matrix_channels_true(self) -> None:
        assert _get_fake_caps("matrix").channels is True

    def test_matrix_direct_messages_true(self) -> None:
        assert _get_fake_caps("matrix").direct_messages is True

    def test_matrix_topic_rooms_true(self) -> None:
        assert _get_fake_caps("matrix").topic_rooms is True

    def test_meshtastic_channels_true(self) -> None:
        assert _get_fake_caps("meshtastic").channels is True

    def test_meshtastic_direct_messages_false(self) -> None:
        """DM packets classified and ignored.  Honest."""
        assert _get_fake_caps("meshtastic").direct_messages is False

    def test_meshcore_channels_true(self) -> None:
        assert _get_fake_caps("meshcore").channels is True

    def test_meshcore_direct_messages_false(self) -> None:
        """MEDRE relays inbound PRIV but does not initiate outbound DMs."""
        assert _get_fake_caps("meshcore").direct_messages is False

    def test_lxmf_channels_false(self) -> None:
        assert _get_fake_caps("lxmf").channels is False

    def test_lxmf_direct_messages_true(self) -> None:
        """LXMF is inherently a DM protocol (source_hash → destination_hash)."""
        assert _get_fake_caps("lxmf").direct_messages is True


class TestMembershipVisibility:
    """presence: whether the adapter exposes presence/online state."""

    def test_no_adapter_claims_presence(self) -> None:
        """No adapter tracks presence.  Correct: none have implementation."""
        for transport in TRANSPORTS:
            assert (
                _get_fake_caps(transport).presence is False
            ), f"{transport} unexpectedly declares presence support"


class TestAttachmentSupport:
    """attachments: whether the adapter can carry file attachments."""

    def test_no_adapter_claims_attachments(self) -> None:
        """No adapter implements file transfer.  Honest omission."""
        for transport in TRANSPORTS:
            assert (
                _get_fake_caps(transport).attachments is False
            ), f"{transport} unexpectedly declares attachment support"


class TestHistoryRetrieval:
    """store_and_forward: whether the adapter supports message persistence."""

    def test_matrix_store_and_forward_false(self) -> None:
        assert _get_fake_caps("matrix").store_and_forward is False

    def test_meshtastic_store_and_forward_false(self) -> None:
        """Firmware supports it but MEDRE does not exercise it."""
        assert _get_fake_caps("meshtastic").store_and_forward is False

    def test_meshcore_store_and_forward_false(self) -> None:
        assert _get_fake_caps("meshcore").store_and_forward is False

    def test_lxmf_store_and_forward_true(self) -> None:
        """LXMRouter has store-and-forward by design."""
        assert _get_fake_caps("lxmf").store_and_forward is True


class TestReadReceipts:
    """Read receipts map to delivery_receipts in MEDRE's capability model."""

    def test_matrix_delivery_receipts_is_server_ack_not_e2e_read(self) -> None:
        """Matrix delivery_receipts=True means room_send returned event_id,
        not that the recipient read the message.  Matrix m.receipt is not
        tracked.  No overclaim."""
        caps = _get_fake_caps("matrix")
        assert caps.delivery_receipts is True
        # The audit doc (§5.1) explicitly clarifies this is server ACK.

    def test_lxmf_no_delivery_receipts_despite_sdk_state(self) -> None:
        """LXMF has a 9-state delivery model but the adapter does not wire
        delivery confirmations back through the MEDRE capability system.
        delivery_state appears in metadata['lxmf'] only.
        delivery_receipts=False is the honest declaration."""
        caps = _get_fake_caps("lxmf")
        assert caps.delivery_receipts is False


class TestTypingIndicators:
    """Typing indicators: no adapter implements them.  Verified via presence."""

    def test_no_adapter_has_typing_support(self) -> None:
        """MEDRE has no typing indicator capability field.  No adapter
        implements typing indicators.  The closest field is presence, which
        all adapters correctly set to False."""
        for transport in TRANSPORTS:
            assert (
                _get_fake_caps(transport).presence is False
            ), f"{transport}: presence must be False (no typing support)"


class TestIdentityEncryption:
    """identity_encryption: identity-level encryption semantics."""

    def test_lxmf_identity_encryption_true(self) -> None:
        """Reticulum identity-based encryption.  Confirmed in audit."""
        assert _get_fake_caps("lxmf").identity_encryption is True

    def test_meshcore_identity_encryption_false(self) -> None:
        """MeshCore has always-on AES-128+HMAC but not identity-based in
        the AdapterCapabilities sense.  Honest."""
        assert _get_fake_caps("meshcore").identity_encryption is False

    def test_matrix_identity_encryption_false(self) -> None:
        """Matrix E2EE is Megolm (session-based), not identity-based."""
        assert _get_fake_caps("matrix").identity_encryption is False

    def test_meshtastic_identity_encryption_false(self) -> None:
        """Meshtastic encryption is channel-key-based, not identity-based."""
        assert _get_fake_caps("meshtastic").identity_encryption is False


# ---------------------------------------------------------------------------
# 4. Semantic honesty: detailed overclaim disproval
# ---------------------------------------------------------------------------


def test_lxmf_delivery_receipts_false_despite_sdk_delivery_state() -> None:
    """LXMF SDK has a 9-state delivery model (outbound → sent → delivered, etc.)
    but the adapter does not wire these states back through the MEDRE delivery
    receipt system.  delivery_state appears in metadata["lxmf"] only, not as
    MEDRE-level delivery receipts.  This test documents that the flag is
    deliberately False.  See capability-truth-audit.md §5.3."""
    fake_caps = _get_fake_caps("lxmf")
    assert fake_caps.delivery_receipts is False
    json_caps = _load_json_caps("lxmf")
    assert json_caps["delivery_receipts"] is False


def test_matrix_delivery_receipts_true_means_server_ack_not_e2e() -> None:
    """Matrix delivery_receipts=True means room_send returns event_id on
    success (server-acknowledged delivery fact).  This is NOT end-to-end
    read receipt tracking.  Matrix m.receipt is not tracked by MEDRE.
    The flag is honest: the adapter does confirm delivery (to the homeserver)
    back to the framework.  See capability-truth-audit.md §5.1."""
    fake_caps = _get_fake_caps("matrix")
    assert fake_caps.delivery_receipts is True
    json_caps = _load_json_caps("matrix")
    assert json_caps["delivery_receipts"] is True


def test_meshcore_identity_encryption_false_despite_always_on_e2ee() -> None:
    """MeshCore has always-on AES-128 + HMAC encryption, but this is not
    identity-based encryption in the AdapterCapabilities sense (which models
    the LXMF/Reticulum identity hash model).  The flag correctly reflects
    that MeshCore does not expose identity-level encryption semantics to
    MEDRE.  See capability-truth-audit.md §5.4."""
    fake_caps = _get_fake_caps("meshcore")
    assert fake_caps.identity_encryption is False
    json_caps = _load_json_caps("meshcore")
    assert json_caps["identity_encryption"] is False


def test_meshtastic_store_and_forward_false_despite_firmware_support() -> None:
    """Meshtastic firmware has store-and-forward but MEDRE does not exercise
    it.  The flag is correctly False.  This is an honest underclaim, not an
    overclaim.  See capability-truth-audit.md §9."""
    fake_caps = _get_fake_caps("meshtastic")
    assert fake_caps.store_and_forward is False
    json_caps = _load_json_caps("meshtastic")
    assert json_caps["store_and_forward"] is False


def test_meshcore_direct_messages_false_despite_inbound_relay() -> None:
    """MeshCore relays inbound PRIV packets but does not initiate outbound DMs.
    relay ≠ DM initiation.  The flag is correctly False.  See capability-truth-audit.md §4.3.
    """
    fake_caps = _get_fake_caps("meshcore")
    assert fake_caps.direct_messages is False
    json_caps = _load_json_caps("meshcore")
    assert json_caps["direct_messages"] is False


def test_matrix_edits_unsupported_despite_matrix_spec_support() -> None:
    """Matrix spec supports edits/redactions but MEDRE has no implementation.
    Correct to not declare until implemented.  See capability-truth-audit.md §9."""
    fake_caps = _get_fake_caps("matrix")
    assert fake_caps.edits == "unsupported"
    assert fake_caps.deletes == "unsupported"
    json_caps = _load_json_caps("matrix")
    assert json_caps["edits"] == "unsupported"
    assert json_caps["deletes"] == "unsupported"


# ---------------------------------------------------------------------------
# 5. Text limits per transport
# ---------------------------------------------------------------------------


class TestTextLimits:
    """max_text_bytes and max_text_chars: payload size constraints."""

    def test_matrix_no_limits(self) -> None:
        caps = _get_fake_caps("matrix")
        assert caps.max_text_bytes is None
        assert caps.max_text_chars is None

    def test_meshtastic_227_bytes_no_char_limit(self) -> None:
        """Default from MeshtasticConfig.max_text_bytes."""
        caps = _get_fake_caps("meshtastic")
        assert caps.max_text_bytes == 227
        assert caps.max_text_chars is None

    def test_meshcore_512_bytes_no_char_limit(self) -> None:
        """Default from MeshCoreConfig.max_text_bytes."""
        caps = _get_fake_caps("meshcore")
        assert caps.max_text_bytes == 512
        assert caps.max_text_chars is None

    def test_lxmf_no_byte_limit_16384_chars(self) -> None:
        """LXMF 16KB character limit."""
        caps = _get_fake_caps("lxmf")
        assert caps.max_text_bytes is None
        assert caps.max_text_chars == 16384


class TestTextLimitsJsonConformance:
    """Text limits in JSON match fake adapter constants."""

    def test_matrix_json_limits(self) -> None:
        caps = _load_json_caps("matrix")
        assert caps["max_text_bytes"] is None
        assert caps["max_text_chars"] is None

    def test_meshtastic_json_limits(self) -> None:
        caps = _load_json_caps("meshtastic")
        assert caps["max_text_bytes"] == 227
        assert caps["max_text_chars"] is None

    def test_meshcore_json_limits(self) -> None:
        caps = _load_json_caps("meshcore")
        assert caps["max_text_bytes"] == 512
        assert caps["max_text_chars"] is None

    def test_lxmf_json_limits(self) -> None:
        caps = _load_json_caps("lxmf")
        assert caps["max_text_bytes"] is None
        assert caps["max_text_chars"] == 16384


# ---------------------------------------------------------------------------
# 6. Transport role consistency
# ---------------------------------------------------------------------------


def test_matrix_and_meshtastic_mesh_routing_opposite() -> None:
    """Matrix is not a mesh protocol; Meshtastic is.  Capabilities reflect this."""
    assert _get_fake_caps("matrix").mesh_routing is False
    assert _get_fake_caps("meshtastic").mesh_routing is True


def test_meshcore_and_lxmf_both_mesh_routing() -> None:
    """MeshCore and LXMF are both mesh protocols."""
    assert _get_fake_caps("meshcore").mesh_routing is True
    assert _get_fake_caps("lxmf").mesh_routing is True


# ---------------------------------------------------------------------------
# 7. Title support only on LXMF
# ---------------------------------------------------------------------------


def test_only_lxmf_claims_title() -> None:
    """LXMF is the only adapter with a native title/subject field."""
    for transport in TRANSPORTS:
        caps = _get_fake_caps(transport)
        if transport == "lxmf":
            assert caps.title is True
        else:
            assert (
                caps.title is False
            ), f"{transport} unexpectedly declares title support"


# ---------------------------------------------------------------------------
# 8. Metadata fields: who supports structured metadata
# ---------------------------------------------------------------------------


def test_meshtastic_and_lxmf_support_metadata_fields() -> None:
    assert _get_fake_caps("meshtastic").metadata_fields is True
    assert _get_fake_caps("lxmf").metadata_fields is True


def test_matrix_and_meshcore_no_metadata_fields() -> None:
    assert _get_fake_caps("matrix").metadata_fields is False
    assert _get_fake_caps("meshcore").metadata_fields is False


# ---------------------------------------------------------------------------
# 9. Async delivery: all adapters claim async
# ---------------------------------------------------------------------------


def test_all_adapters_claim_async_delivery() -> None:
    """All four adapters deliver asynchronously."""
    for transport in TRANSPORTS:
        assert (
            _get_fake_caps(transport).async_delivery is True
        ), f"{transport} should declare async_delivery=True"


# ---------------------------------------------------------------------------
# 10. Conservative defaults check: no adapter overclaims ack_tracking or
#     priority_delivery
# ---------------------------------------------------------------------------


def test_no_adapter_claims_ack_tracking() -> None:
    for transport in TRANSPORTS:
        assert (
            _get_fake_caps(transport).ack_tracking is False
        ), f"{transport} unexpectedly declares ack_tracking"


def test_no_adapter_claims_priority_delivery() -> None:
    for transport in TRANSPORTS:
        assert (
            _get_fake_caps(transport).priority_delivery is False
        ), f"{transport} unexpectedly declares priority_delivery"
