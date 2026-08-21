"""Focused Matrix invite callback registration tests."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import make_matrix_config


def test_invite_registration_uses_nio_events_direct_import() -> None:
    session = MatrixSession(make_matrix_config())
    session._client = MagicMock(name="mock_client")

    invite_cls = MagicMock(name="InviteMemberEvent")
    fake_nio = ModuleType("nio")
    fake_events = ModuleType("nio.events")
    fake_events.InviteMemberEvent = invite_cls
    fake_nio.events = fake_events

    with patch.dict(sys.modules, {"nio": fake_nio, "nio.events": fake_events}):
        session._register_invite_callback()

    session._client.add_event_callback.assert_called_once()
    call_args = session._client.add_event_callback.call_args
    registered_handler = call_args.args[0]
    assert registered_handler.__func__ is session._on_invite.__func__
    assert invite_cls in call_args.args[1]


def test_invite_registration_uses_top_level_fallback() -> None:
    session = MatrixSession(make_matrix_config())
    session._client = MagicMock(name="mock_client")

    invite_cls = MagicMock(name="InviteMemberEvent")
    fake_nio = ModuleType("nio")
    fake_nio.InviteMemberEvent = invite_cls
    fake_events = ModuleType("nio.events")

    with patch.dict(sys.modules, {"nio": fake_nio, "nio.events": fake_events}):
        session._register_invite_callback()

    session._client.add_event_callback.assert_called_once()
    call_args = session._client.add_event_callback.call_args
    registered_handler = call_args.args[0]
    assert registered_handler.__func__ is session._on_invite.__func__
    assert invite_cls in call_args.args[1]


def test_invite_registration_failure_is_nonfatal() -> None:
    session = MatrixSession(make_matrix_config())
    session._client = MagicMock(name="mock_client")
    session._client.add_event_callback.side_effect = RuntimeError("registration failed")

    invite_cls = MagicMock(name="InviteMemberEvent")
    fake_nio = ModuleType("nio")
    fake_events = ModuleType("nio.events")
    fake_events.InviteMemberEvent = invite_cls
    fake_nio.events = fake_events

    with patch.dict(sys.modules, {"nio": fake_nio, "nio.events": fake_events}):
        session._register_invite_callback()

    session._client.add_event_callback.assert_called_once()
