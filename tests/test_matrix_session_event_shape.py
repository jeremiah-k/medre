"""Matrix session normalization and event-class coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from medre.adapters.matrix.session import (
    MatrixSession,
    _event_classes_by_name,
    _media_message_classes,
    _redaction_event_classes,
)
from medre.config.adapters.matrix import MatrixConfig


def _config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="matrix-1",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )


def _session() -> MatrixSession:
    return MatrixSession(_config())


def test_session_normalization_captures_safe_crypto_provenance_only() -> None:
    session = _session()
    room = SimpleNamespace(
        room_id="!room:example.com",
        encrypted=True,
        user_name=lambda _sender: "Alice",
    )
    event = SimpleNamespace(
        sender="@alice:example.com",
        body="hello",
        event_id="$event",
        server_timestamp=1_700_000_000_123,
        decrypted=True,
        verified=True,
        sender_key="sensitive-sender-key",
        session_id="sensitive-session-id",
        transaction_id="txn-9",
        source={
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    normalized = session._normalize_event(room, event)

    assert normalized["event_type"] == "m.room.message"
    assert normalized["sender_display_name"] == "Alice"
    assert normalized["transaction_id"] == "txn-9"
    assert normalized["room_encrypted"] is True
    assert normalized["event_encrypted"] is True
    assert normalized["decrypted"] is True
    assert normalized["verified"] is True
    assert "sender_key" not in normalized
    assert "session_id" not in normalized


def test_session_normalization_uses_unsigned_transaction_id_fallback() -> None:
    session = _session()
    room = SimpleNamespace(
        room_id="!room:example.com",
        encrypted=False,
        user_name=lambda sender: sender,
    )
    event = SimpleNamespace(
        sender="@alice:example.com",
        body="hello",
        event_id="$event",
        decrypted=False,
        source={
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
            "unsigned": {"transaction_id": "txn-unsigned"},
        },
    )

    normalized = session._normalize_event(room, event)

    assert normalized["transaction_id"] == "txn-unsigned"
    assert normalized["event_encrypted"] is False
    assert normalized["verified"] is None


def test_session_normalization_preserves_unix_epoch_timestamp() -> None:
    session = _session()
    room = SimpleNamespace(
        room_id="!room:example.com",
        encrypted=False,
        user_name=lambda sender: sender,
    )
    event = SimpleNamespace(
        sender="@alice:example.com",
        body="epoch",
        event_id="$event",
        server_timestamp=0,
        origin_server_ts=1_700_000_000_000,
        decrypted=False,
        source={
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "epoch"},
        },
    )

    normalized = session._normalize_event(room, event)

    assert normalized["server_timestamp"] == 0


def test_media_message_class_discovery_is_version_tolerant() -> None:
    image = type("RoomMessageImage", (), {})
    audio = type("RoomMessageAudio", (), {})
    video = type("RoomMessageVideo", (), {})
    file_event = type("RoomMessageFile", (), {})
    encrypted_image = type("RoomEncryptedImage", (), {})
    encrypted_audio = type("RoomEncryptedAudio", (), {})
    encrypted_video = type("RoomEncryptedVideo", (), {})
    encrypted_file = type("RoomEncryptedFile", (), {})
    nio = SimpleNamespace(
        RoomMessageImage=image,
        RoomEncryptedImage=encrypted_image,
        events=SimpleNamespace(
            RoomMessageAudio=audio,
            RoomEncryptedAudio=encrypted_audio,
            room_events=SimpleNamespace(
                RoomMessageVideo=video,
                RoomMessageFile=file_event,
                RoomEncryptedVideo=encrypted_video,
                RoomEncryptedFile=encrypted_file,
            ),
        ),
    )

    discovered = _media_message_classes(nio)
    # The helper also falls back to the real nio module, so assert presence
    # of the expected mock classes (version-tolerance contract) rather than
    # exact equality.
    for expected in (
        image,
        encrypted_image,
        audio,
        encrypted_audio,
        video,
        file_event,
        encrypted_video,
        encrypted_file,
    ):
        assert expected in discovered


def test_redaction_event_class_discovery_is_version_tolerant() -> None:
    redaction = type("RedactionEvent", (), {})
    nio = SimpleNamespace(
        events=SimpleNamespace(
            room_events=SimpleNamespace(RedactionEvent=redaction),
        )
    )

    assert redaction in _redaction_event_classes(nio)


async def test_session_registers_media_and_redaction_callbacks(
    mock_nio: Any,
) -> None:
    image = type("RoomMessageImage", (), {})
    audio = type("RoomMessageAudio", (), {})
    video = type("RoomMessageVideo", (), {})
    file_event = type("RoomMessageFile", (), {})
    encrypted_image = type("RoomEncryptedImage", (), {})
    encrypted_file = type("RoomEncryptedFile", (), {})
    reaction = type("ReactionEvent", (), {})
    redaction = type("RedactionEvent", (), {})
    mock_nio.RoomMessageImage = image
    mock_nio.RoomMessageAudio = audio
    mock_nio.RoomMessageVideo = video
    mock_nio.RoomMessageFile = file_event
    mock_nio.RoomEncryptedImage = encrypted_image
    mock_nio.RoomEncryptedFile = encrypted_file
    mock_nio.ReactionEvent = reaction
    mock_nio.RedactionEvent = redaction

    async def _callback(_event: dict[str, Any]) -> None:
        return None

    session = _session()
    session._message_callback = _callback
    try:
        await session.start()
        registrations = [
            tuple(call.args[1])
            for call in (
                mock_nio.AsyncClient.return_value.add_event_callback.call_args_list
            )
            if len(call.args) >= 2
        ]
        assert any(image in classes for classes in registrations)
        assert any(audio in classes for classes in registrations)
        assert any(video in classes for classes in registrations)
        assert any(file_event in classes for classes in registrations)
        assert any(encrypted_image in classes for classes in registrations)
        assert any(encrypted_file in classes for classes in registrations)
        assert any(reaction in classes for classes in registrations)
        assert any(redaction in classes for classes in registrations)
    finally:
        await session.stop()


async def test_session_registers_durable_admission_for_all_supported_events(
    mock_nio: Any,
) -> None:
    media_classes = {
        "RoomMessageImage": type("RoomMessageImage", (), {}),
        "RoomMessageAudio": type("RoomMessageAudio", (), {}),
        "RoomMessageVideo": type("RoomMessageVideo", (), {}),
        "RoomMessageFile": type("RoomMessageFile", (), {}),
        "RoomEncryptedImage": type("RoomEncryptedImage", (), {}),
        "RoomEncryptedAudio": type("RoomEncryptedAudio", (), {}),
        "RoomEncryptedVideo": type("RoomEncryptedVideo", (), {}),
        "RoomEncryptedFile": type("RoomEncryptedFile", (), {}),
    }
    for name, cls in media_classes.items():
        setattr(mock_nio, name, cls)
    reaction = type("ReactionEvent", (), {})
    redaction = type("RedactionEvent", (), {})
    mock_nio.ReactionEvent = reaction
    mock_nio.RedactionEvent = redaction

    async def admit(_event: dict[str, Any], _provenance: object) -> None:
        return None

    async def load(_stream: str) -> None:
        return None

    async def commit(_stream: str, _cursor: str, _metadata: str) -> None:
        return None

    session = MatrixSession(
        _config(),
        admission_callback=admit,
        checkpoint_loader=load,
        checkpoint_committer=commit,
    )
    try:
        await session.start()
        client = mock_nio.AsyncClient.return_value
        calls = client.add_event_admission_callback.call_args_list
        assert len(calls) == 1
        registered = tuple(calls[0].args[1])
        expected = (
            mock_nio.RoomMessageText,
            mock_nio.RoomMessageNotice,
            mock_nio.RoomMessageEmote,
            *media_classes.values(),
            reaction,
            redaction,
        )
        for event_class in expected:
            assert event_class in registered
    finally:
        await session.stop()


def test_event_class_discovery_propagates_unexpected_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(name: str) -> object:
        raise RuntimeError(f"failed to initialize {name}")

    monkeypatch.setattr(
        "medre.adapters.matrix.session.importlib.import_module", fail_import
    )

    with pytest.raises(RuntimeError, match="failed to initialize nio.events"):
        _event_classes_by_name(SimpleNamespace(), "ReactionEvent")


def test_event_class_discovery_tolerates_missing_optional_nio_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction = type("ReactionEvent", (), {})

    def missing_import(name: str) -> object:
        raise ModuleNotFoundError(
            f"No module named {name!r}", name=name.split(".", 1)[0]
        )

    monkeypatch.setattr(
        "medre.adapters.matrix.session.importlib.import_module", missing_import
    )

    assert _event_classes_by_name(
        SimpleNamespace(ReactionEvent=reaction), "ReactionEvent"
    ) == (reaction,)


def test_event_class_discovery_propagates_nested_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_import(_name: str) -> object:
        raise ModuleNotFoundError(
            "No module named 'optional_crypto_dependency'",
            name="optional_crypto_dependency",
        )

    monkeypatch.setattr(
        "medre.adapters.matrix.session.importlib.import_module", fail_import
    )

    with pytest.raises(ModuleNotFoundError, match="optional_crypto_dependency"):
        _event_classes_by_name(SimpleNamespace(), "ReactionEvent")
