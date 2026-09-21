"""Tests for the Matrix provisioning seam (space + encrypted room).

Covers: power-level merge semantics, state-content builders, input
validation, the provisioning sequence against a stub client (order,
preservation, verification read-backs), failure boundaries, the CLI
parser surface, and the real pinned-SDK contract (matrix_sdk marker).
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.matrix.provision import (
    MEGOLM_ROOM_ALGORITHM,
    ProvisionReport,
    encryption_initial_state,
    merge_power_level_users,
    provision_private_space_and_room,
    space_child_content,
    space_parent_content,
)
from medre.cli.main import _build_parser

BOT = "@forxrelay:synod.im"
USER = "@tadchilly:matrix.org"
SERVER = "synod.im"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_preserves_unrelated_fields() -> None:
    existing: dict[str, Any] = {
        "ban": 50,
        "events": {"m.room.name": 100},
        "invite": 0,
        "kick": 50,
        "redact": 50,
        "state_default": 50,
        "users": {BOT: 100},
        "users_default": 0,
        "notifications": {"room": 50},
    }
    merged = merge_power_level_users(existing, {USER: 100})
    for key in (
        "ban",
        "events",
        "invite",
        "kick",
        "redact",
        "state_default",
        "users_default",
        "notifications",
    ):
        assert merged[key] == existing[key]
    assert merged["users"] == {BOT: 100, USER: 100}
    # Original untouched.
    assert existing["users"] == {BOT: 100}


def test_overrides_existing_grant() -> None:
    merged = merge_power_level_users({"users": {USER: 0}}, {USER: 100})
    assert merged["users"] == {USER: 100}


def test_creates_users_map_when_absent() -> None:
    merged = merge_power_level_users({}, {USER: 100})
    assert merged["users"] == {USER: 100}


def test_encryption_initial_state_shape() -> None:
    state = encryption_initial_state()
    assert state == {
        "type": "m.room.encryption",
        "state_key": "",
        "content": {"algorithm": MEGOLM_ROOM_ALGORITHM},
    }
    assert MEGOLM_ROOM_ALGORITHM == "m.megolm.v1.aes-sha2"


def test_child_and_parent_content() -> None:
    assert space_child_content([SERVER]) == {"via": [SERVER]}
    assert space_parent_content([SERVER]) == {
        "via": [SERVER],
        "canonical": True,
    }


# ---------------------------------------------------------------------------
# Stub client exercising the provisioning sequence
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: dict[str, Any] | None = None, **attrs: Any) -> None:
        if content is not None:
            self.content = content
        for key, value in attrs.items():
            setattr(self, key, value)


class _FakeError:
    def __init__(self, message: str = "denied", status_code: str = "M_FORBIDDEN"):
        self.message = message
        self.status_code = status_code


class _StubClient:
    """Records nio-style calls; replays scripted responses in order."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_responses: list[Any] = []
        self.scripted: dict[str, list[Any]] = {}

    def _pop(self, key: str, default: Any) -> Any:
        if key in self.scripted:
            return self.scripted[key].pop(0)
        return default

    async def room_create(self, **kwargs: Any) -> Any:
        self.calls.append(("room_create", kwargs))
        return self._pop(
            "room_create",
            _FakeResponse(
                room_id=f"!{'space' if kwargs.get('space') else 'room'}:{SERVER}"
            ),
        )

    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> Any:
        self.calls.append(
            (
                "room_put_state",
                {
                    "room_id": room_id,
                    "event_type": event_type,
                    "content": content,
                    "state_key": state_key,
                },
            )
        )
        return self._pop("room_put_state", _FakeResponse(event_id="$x"))

    async def room_get_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> Any:
        self.calls.append(
            (
                "room_get_state_event",
                {"room_id": room_id, "event_type": event_type, "state_key": state_key},
            )
        )
        return self._pop("room_get_state_event", _FakeError("not found", "M_NOT_FOUND"))

    async def room_invite(self, room_id: str, user_id: str) -> Any:
        self.calls.append(("room_invite", {"room_id": room_id, "user_id": user_id}))
        return self._pop("room_invite", _FakeResponse())

    async def joined_members(self, room_id: str) -> Any:
        self.calls.append(("joined_members", {"room_id": room_id}))
        return self._pop("joined_members", _FakeResponse(members=[]))


async def test_provision_sequence_creates_invites_and_verifies() -> None:
    client = _StubClient(BOT)
    # Default scripted flow: power-level reads return a content with an
    # unrelated field on the room (first read) so preservation is observable.
    room_pl = {"ban": 50, "users": {BOT: 100}}
    reads: list[Any] = [
        # space PL read (pre-put)
        _FakeResponse(content={"users": {}}),
        # space PL read-back (post-put)
        _FakeResponse(content={"users": {BOT: 100, USER: 100}}),
        # room PL read (pre-put)
        _FakeResponse(content=dict(room_pl)),
        # room PL read-back (post-put)
        _FakeResponse(content={"ban": 50, "users": {BOT: 100, USER: 100}}),
        # encryption read-back
        _FakeResponse(content={"algorithm": MEGOLM_ROOM_ALGORITHM}),
        # linkage reads: space child, space parent
        _FakeResponse(content={"via": [SERVER]}),
        _FakeResponse(content={"via": [SERVER], "canonical": True}),
    ]
    client.scripted["room_get_state_event"] = reads

    report = await provision_private_space_and_room(
        client,
        space_name="MEDRE Lab",
        room_name="MEDRE Lab Encrypted",
        invite_user_ids=[USER],
        admin_power_user_ids=[USER],
        room_topic="smoke",
    )

    assert isinstance(report, ProvisionReport)
    assert report.space.room_id == f"!space:{SERVER}"
    assert report.room.room_id == f"!room:{SERVER}"
    assert report.encryption_algorithm == MEGOLM_ROOM_ALGORITHM
    assert report.linkage_verified is True
    assert report.space.invited == (USER,)
    assert report.room.invited == (USER,)
    assert report.space.granted_power_levels == {BOT: 100, USER: 100}
    assert report.permalinks()["room"].startswith("https://matrix.to/#/!room:")

    creates = [c for c in client.calls if c[0] == "room_create"]
    assert len(creates) == 2
    space_kwargs = creates[0][1]
    room_kwargs = creates[1][1]
    assert space_kwargs["space"] is True
    assert space_kwargs["visibility"].value == "private"
    assert space_kwargs["preset"].value == "private_chat"
    assert space_kwargs["federate"] is True
    assert (
        "invite" not in space_kwargs
    ), "room_create must not expose invitations before power/link state is ready"
    assert "invite" not in room_kwargs
    # Encryption arrives with the room's first state, not after creation.
    assert room_kwargs["initial_state"] == [encryption_initial_state()]
    assert room_kwargs["topic"] == "smoke"

    # Power preassignment happens BEFORE any invite on each resource.
    for prefix_room_id in (f"!space:{SERVER}", f"!room:{SERVER}"):
        put_idx = next(
            i
            for i, (name, kw) in enumerate(client.calls)
            if name == "room_put_state"
            and kw["event_type"] == "m.room.power_levels"
            and kw["room_id"] == prefix_room_id
        )
        invite_idx = next(
            i
            for i, (name, kw) in enumerate(client.calls)
            if name == "room_invite" and kw["room_id"] == prefix_room_id
        )
        assert put_idx < invite_idx, "power preassignment must precede invites"

    first_invite_idx = next(
        i for i, (name, _kw) in enumerate(client.calls) if name == "room_invite"
    )
    linkage_indices = [
        i
        for i, (name, kw) in enumerate(client.calls)
        if name == "room_put_state"
        and kw["event_type"] in {"m.space.child", "m.space.parent"}
    ]
    assert (
        linkage_indices and max(linkage_indices) < first_invite_idx
    ), "space/room linkage must be written before invitations become visible"

    # The room's power put preserved the unrelated ban field.
    room_pl_put = next(
        kw
        for name, kw in client.calls
        if name == "room_put_state"
        and kw["event_type"] == "m.room.power_levels"
        and kw["room_id"] == f"!room:{SERVER}"
        and "ban" in kw["content"]
    )
    assert room_pl_put["content"]["ban"] == 50

    # Linkage written from both directions with correct state keys.
    child_put = next(
        kw
        for name, kw in client.calls
        if name == "room_put_state" and kw["event_type"] == "m.space.child"
    )
    assert child_put["room_id"] == f"!space:{SERVER}"
    assert child_put["state_key"] == f"!room:{SERVER}"
    parent_put = next(
        kw
        for name, kw in client.calls
        if name == "room_put_state" and kw["event_type"] == "m.space.parent"
    )
    assert parent_put["room_id"] == f"!room:{SERVER}"
    assert parent_put["state_key"] == f"!space:{SERVER}"
    assert parent_put["content"] == {"via": [SERVER], "canonical": True}


async def test_provision_rejects_admin_outside_invite_set() -> None:
    client = _StubClient(BOT)
    with pytest.raises(ValueError, match="not invited"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
            admin_power_user_ids=["@other:matrix.org"],
        )
    assert client.calls == []


@pytest.mark.parametrize("bad_mxid", ["tadchilly", "@:matrix.org", "@user:"])
async def test_provision_rejects_malformed_user_id_before_creation(
    bad_mxid: str,
) -> None:
    client = _StubClient(BOT)
    with pytest.raises(ValueError, match="fully-qualified MXID"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[bad_mxid],
        )
    assert client.calls == []


async def test_provision_rejects_duplicate_invites_before_creating_rooms() -> None:
    client = _StubClient(BOT)
    with pytest.raises(ValueError, match="duplicate user IDs"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER, USER],
        )
    assert client.calls == []


async def test_provision_fails_closed_when_power_state_read_fails() -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    client = _StubClient(BOT)
    client.scripted["room_get_state_event"] = [
        _FakeError("temporary server failure", "M_UNKNOWN")
    ]
    with pytest.raises(MatrixProvisionError, match="power_levels read"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
        )
    assert not any(name == "room_invite" for name, _ in client.calls)
    assert not any(
        name == "room_put_state" and kw["event_type"] == "m.room.power_levels"
        for name, kw in client.calls
    )


async def test_provision_fails_on_create_error() -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    client = _StubClient(BOT)
    client.scripted["room_create"] = [_FakeError("forbidden")]
    with pytest.raises(MatrixProvisionError, match="room_create"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
        )


async def test_provision_fails_when_power_readback_missing() -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    client = _StubClient(BOT)
    client.scripted["room_get_state_event"] = [
        _FakeResponse(content={}),  # space PL pre
        _FakeResponse(content={"users": {BOT: 100}}),  # space PL readback: grant lost
    ]
    with pytest.raises(MatrixProvisionError, match="power read-back"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
            admin_power_user_ids=[USER],
        )


async def test_provision_fails_when_room_not_encrypted() -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    client = _StubClient(BOT)
    ok_users = {"users": {BOT: 100, USER: 100}}
    client.scripted["room_get_state_event"] = [
        _FakeResponse(content={"users": {BOT: 100}}),  # space PL pre
        _FakeResponse(content=dict(ok_users)),  # space PL read-back
        _FakeResponse(content={"users": {BOT: 100}}),  # room PL pre
        _FakeResponse(content=dict(ok_users)),  # room PL read-back
        _FakeError("not found", "M_NOT_FOUND"),  # encryption read fails
    ]
    with pytest.raises(MatrixProvisionError, match="m.room.encryption"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
        )
    assert not any(
        name == "room_invite" for name, _ in client.calls
    ), "users must not be invited when encryption verification fails"


async def test_provision_fails_on_wrong_algorithm() -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    client = _StubClient(BOT)
    ok_users = {"users": {BOT: 100, USER: 100}}
    client.scripted["room_get_state_event"] = [
        _FakeResponse(content={"users": {BOT: 100}}),  # space PL pre
        _FakeResponse(content=dict(ok_users)),  # space PL read-back
        _FakeResponse(content={"users": {BOT: 100}}),  # room PL pre
        _FakeResponse(content=dict(ok_users)),  # room PL read-back
        _FakeResponse(content={"algorithm": "m.unrelated.alg"}),
    ]
    with pytest.raises(MatrixProvisionError, match="algorithm"):
        await provision_private_space_and_room(
            client,
            space_name="s",
            room_name="r",
            invite_user_ids=[USER],
        )


# ---------------------------------------------------------------------------
# CLI parser surface
# ---------------------------------------------------------------------------


def test_provision_parses_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "adapter",
            "matrix",
            "provision",
            "--space-name",
            "Lab Space",
            "--room-name",
            "Lab Room",
            "--room-topic",
            "smoke",
            "--invite",
            USER,
            "--admin",
            USER,
        ]
    )
    assert args.space_name == "Lab Space"
    assert args.room_name == "Lab Room"
    assert args.room_topic == "smoke"
    assert args.invite == [USER]
    assert args.admin == [USER]


def test_provision_requires_space_and_room_names() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["adapter", "matrix", "provision", "--invite", USER])


def test_provision_requires_invite() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "adapter",
                "matrix",
                "provision",
                "--space-name",
                "s",
                "--room-name",
                "r",
            ]
        )


def test_provision_help_mentions_invite_not_join(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["adapter", "matrix", "provision", "--help"])
    out = capsys.readouterr().out
    assert "effective on join" in out


# ---------------------------------------------------------------------------
# Real pinned SDK contract (opt-in, matrix_sdk marker)
# ---------------------------------------------------------------------------


@pytest.mark.matrix_sdk
def test_pinned_nio_exposes_provisioning_contract() -> None:
    from tests.helpers.sdk_contract import (
        assert_installed_extra_matches_declared_pins,
    )

    assert_installed_extra_matches_declared_pins("matrix", ("mindroom-nio",))

    import inspect

    import nio

    create_params = inspect.signature(nio.AsyncClient.room_create).parameters
    for name in (
        "visibility",
        "name",
        "topic",
        "preset",
        "federate",
        "initial_state",
        "space",
    ):
        assert name in create_params, f"room_create missing {name!r}"
    # Provisioning writes encryption state at creation time.
    assert "initial_state" in create_params

    put_params = inspect.signature(nio.AsyncClient.room_put_state).parameters
    for name in ("room_id", "event_type", "content", "state_key"):
        assert name in put_params, f"room_put_state missing {name!r}"

    get_params = inspect.signature(nio.AsyncClient.room_get_state_event).parameters
    assert "state_key" in get_params

    assert "user_id" in inspect.signature(nio.AsyncClient.room_invite).parameters

    assert nio.RoomVisibility.private.value == "private"
    assert nio.RoomPreset.private_chat.value == "private_chat"
    assert nio.RoomPutStateResponse is not None


def test_provision_admin_default_does_not_accumulate_across_parses() -> None:
    """Repeated parser use must not retain values from action='append'."""
    parser = _build_parser()
    base = [
        "adapter",
        "matrix",
        "provision",
        "--space-name",
        "Lab Space",
        "--room-name",
        "Lab Room",
        "--invite",
        USER,
    ]

    first = parser.parse_args([*base, "--admin", USER])
    second = parser.parse_args(base)

    assert first.admin == [USER]
    assert second.admin is None
