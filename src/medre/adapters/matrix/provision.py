"""Operator provisioning for private encrypted Matrix test resources.

Bridges the gap between ``medre adapter matrix auth login`` (account/session
bootstrap) and a usable encrypted room: private space + encrypted room
creation, space parent/child linkage, pre-assigned admin power levels,
invitations, and state read-back verification.

This module is deliberately *not* part of the runtime delivery path.  It
exists for operators and live smoke harnesses that need repeatable,
privately-scoped test resources — the runtime only ever *consumes* rooms
through the configured allowlist.

The ``m.room.encryption`` state event is written in the room's
``initial_state`` at creation time so the room is encrypted from its first
event; there is no unencrypted window to race against.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

__all__ = [
    "MEGOLM_ROOM_ALGORITHM",
    "ProvisionReport",
    "ProvisionedResource",
    "provision_private_space_and_room",
]

# The only room-encryption algorithm MEDRE provisions or accepts for test
# resources.  Single source of truth for this module's create/verify paths.
MEGOLM_ROOM_ALGORITHM = "m.megolm.v1.aes-sha2"

# Admin power level required by the lab smoke policy (bot + invited admins).
_ADMIN_POWER = 100


class _WireRoomVisibility(Enum):
    """Minimal ``.value`` contract consumed by the pinned nio room-create API."""

    PRIVATE = "private"


class _WireRoomPreset(Enum):
    """Minimal ``.value`` contract consumed by the pinned nio room-create API."""

    PRIVATE_CHAT = "private_chat"


class _ProvisionClient(Protocol):
    """Structural subset of :class:`nio.AsyncClient` this module consumes.

    Keeping the surface structural lets tests stub the client while the
    dedicated SDK-contract tier verifies the pinned release's signatures.
    """

    user_id: str

    async def room_create(self, **kwargs: Any) -> Any: ...

    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> Any: ...

    async def room_get_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> Any: ...

    async def room_invite(self, room_id: str, user_id: str) -> Any: ...


@dataclass(frozen=True)
class ProvisionedResource:
    """Outcome snapshot for one provisioned room/space.

    ``invited`` lists users that were successfully *invited* — membership is
    reported separately from joins; an invite is not a join.
    """

    room_id: str
    invited: tuple[str, ...]
    granted_power_levels: dict[str, int]


@dataclass(frozen=True)
class ProvisionReport:
    """Verified provisioning outcome for the space/room pair."""

    space: ProvisionedResource
    room: ProvisionedResource
    encryption_algorithm: str
    linkage_verified: bool
    bot_user_id: str

    def permalinks(self) -> dict[str, str]:
        """Return ``matrix.to`` permalinks (IDs are not credentials)."""
        return {
            "space": f"https://matrix.to/#/{self.space.room_id}",
            "room": f"https://matrix.to/#/{self.room.room_id}",
        }


def encryption_initial_state() -> dict[str, Any]:
    """Return the ``m.room.encryption`` initial_state entry for creation."""
    return {
        "type": "m.room.encryption",
        "state_key": "",
        "content": {"algorithm": MEGOLM_ROOM_ALGORITHM},
    }


def space_child_content(via: Sequence[str]) -> dict[str, Any]:
    """Return the space→room (``m.space.child``) state content."""
    return {"via": list(via)}


def space_parent_content(via: Sequence[str]) -> dict[str, Any]:
    """Return the room→space (``m.space.parent``) state content."""
    return {"via": list(via), "canonical": True}


def merge_power_level_users(
    existing: Mapping[str, Any], grants: Mapping[str, int]
) -> dict[str, Any]:
    """Return a new power-levels content with ``users`` grants merged in.

    Every pre-existing field (``ban``, ``events``, ``invite``, ``kick``,
    ``redact``, ``state_default``, ``notifications``, …) is preserved as-is;
    only the ``users`` map gains (or overrides) the granted entries.
    """
    merged = dict(existing)
    users = dict(existing.get("users") or {})
    users.update(grants)
    merged["users"] = users
    return merged


def _server_name_from_user_id(user_id: str) -> str:
    _, _, domain = user_id.partition(":")
    if not domain:
        raise ValueError(f"user_id {user_id!r} is not a fully-qualified MXID")
    return domain


def _validate_inputs(
    invite_user_ids: Sequence[str], admin_power_user_ids: Sequence[str]
) -> None:
    for label, values in (
        ("invite_user_ids", invite_user_ids),
        ("admin_power_user_ids", admin_power_user_ids),
    ):
        seen: set[str] = set()
        duplicates: set[str] = set()
        for user_id in values:
            localpart, separator, server_name = user_id.removeprefix("@").partition(":")
            if (
                not user_id.startswith("@")
                or not separator
                or not localpart
                or not server_name
            ):
                raise ValueError(
                    f"user ID {user_id!r} is not a fully-qualified MXID (@user:server)"
                )
            if user_id in seen:
                duplicates.add(user_id)
            seen.add(user_id)
        if duplicates:
            raise ValueError(
                f"{label} contains duplicate user IDs: {sorted(duplicates)}"
            )

    non_invited = set(admin_power_user_ids) - set(invite_user_ids)
    if non_invited:
        raise ValueError(
            "admin power can only be pre-assigned to invited users; "
            f"not invited: {sorted(non_invited)}"
        )


async def _raise_if_error(response: Any, action: str) -> None:
    from medre.adapters.matrix.errors import MatrixProvisionError

    if type(response).__name__.endswith("Error"):
        raise MatrixProvisionError(
            f"{action} failed: {getattr(response, 'status_code', '?')} "
            f"{getattr(response, 'message', response)}"
        )


async def _read_power_levels(client: _ProvisionClient, room_id: str) -> dict[str, Any]:
    """Read the room's power-levels content, failing closed on bad state."""
    from medre.adapters.matrix.errors import MatrixProvisionError

    response = await client.room_get_state_event(room_id, "m.room.power_levels")
    await _raise_if_error(response, f"power_levels read on {room_id}")
    content = getattr(response, "content", None)
    if not isinstance(content, Mapping):
        raise MatrixProvisionError(
            f"power_levels read on {room_id} returned no mapping content"
        )
    return dict(content)


async def _provision_resource(
    client: _ProvisionClient,
    *,
    name: str,
    admin_grants: Mapping[str, int],
    space: bool,
    initial_state: Sequence[dict[str, Any]] = (),
    topic: str | None = None,
    on_created: Callable[[str], None] | None = None,
) -> ProvisionedResource:
    """Create one room/space and persist verified power-level grants."""
    from medre.adapters.matrix.errors import MatrixProvisionError

    create_kwargs: dict[str, Any] = {
        "visibility": _WireRoomVisibility.PRIVATE,
        "name": name,
        "preset": _WireRoomPreset.PRIVATE_CHAT,
        # Room visibility (directory listing) is orthogonal to federation:
        # private rooms still federate with matrix.org by default.
        "federate": True,
        "space": space,
        # Invitations are deliberately NOT part of room_create().  The
        # operator contract pre-assigns power and links the space/room pair
        # before any invited user can join.
        "initial_state": list(initial_state),
    }
    if topic is not None:
        create_kwargs["topic"] = topic

    response = await client.room_create(**create_kwargs)
    await _raise_if_error(response, f"room_create({name!r})")
    room_id = getattr(response, "room_id", None)
    if not room_id:
        raise MatrixProvisionError(f"room_create({name!r}) returned no room_id")
    room_id = str(room_id)
    if on_created is not None:
        on_created(room_id)

    # Pre-assign admin power before any explicit invitation is sent so a
    # later join is immediately an admin (no watcher loop).  Preserve every
    # unrelated server-managed field rather than synthesizing replacement
    # power state when the read fails.
    current = await _read_power_levels(client, room_id)
    current_users = current.get("users")
    if current_users is not None and not isinstance(current_users, Mapping):
        raise MatrixProvisionError(
            f"power_levels read on {room_id} has non-mapping users field"
        )
    merged = merge_power_level_users(current, dict(admin_grants))
    put = await client.room_put_state(room_id, "m.room.power_levels", merged)
    await _raise_if_error(put, f"power_levels put on {room_id}")

    # Read back power state so the report reflects server truth, not intent.
    power_content = await _read_power_levels(client, room_id)
    raw_server_users = power_content.get("users")
    if raw_server_users is not None and not isinstance(raw_server_users, Mapping):
        raise MatrixProvisionError(
            f"power read-back on {room_id} has non-mapping users field"
        )
    server_users = raw_server_users or {}
    users_readback = {user: int(server_users.get(user, 0)) for user in admin_grants}
    for target, power in users_readback.items():
        if power != _ADMIN_POWER:
            raise MatrixProvisionError(
                f"power read-back for {target} in {room_id} is {power}, "
                f"expected {_ADMIN_POWER}"
            )

    return ProvisionedResource(
        room_id=room_id,
        invited=(),
        granted_power_levels=users_readback,
    )


async def _invite_users(
    client: _ProvisionClient,
    room_id: str,
    invite_user_ids: Sequence[str],
    *,
    on_invited: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    """Invite each validated user exactly once and return successful targets."""
    invited: list[str] = []
    for target in invite_user_ids:
        invite = await client.room_invite(room_id, target)
        await _raise_if_error(invite, f"invite {target} to {room_id}")
        invited.append(target)
        if on_invited is not None:
            on_invited(target)
    return tuple(invited)


async def _verify_encryption(client: _ProvisionClient, room_id: str) -> str:
    from medre.adapters.matrix.errors import MatrixProvisionError

    response = await client.room_get_state_event(room_id, "m.room.encryption")
    content = getattr(response, "content", None)
    if type(response).__name__.endswith("Error") or not isinstance(content, Mapping):
        raise MatrixProvisionError(
            f"room {room_id} has no readable m.room.encryption state; "
            "refusing to treat it as encrypted"
        )
    algorithm = content.get("algorithm")
    if algorithm != MEGOLM_ROOM_ALGORITHM:
        raise MatrixProvisionError(
            f"room {room_id} encryption algorithm is {algorithm!r}, "
            f"expected {MEGOLM_ROOM_ALGORITHM!r}"
        )
    return str(algorithm)


async def _verify_linkage(
    client: _ProvisionClient, space_id: str, room_id: str, server_name: str
) -> bool:
    from medre.adapters.matrix.errors import MatrixProvisionError

    child = await client.room_get_state_event(
        space_id, "m.space.child", state_key=room_id
    )
    parent = await client.room_get_state_event(
        room_id, "m.space.parent", state_key=space_id
    )
    if type(child).__name__.endswith("Error") or type(parent).__name__.endswith(
        "Error"
    ):
        raise MatrixProvisionError(
            f"space linkage incomplete: child={type(child).__name__} "
            f"parent={type(parent).__name__}"
        )
    child_content = getattr(child, "content", None)
    parent_content = getattr(parent, "content", None)
    if not isinstance(child_content, Mapping) or not isinstance(
        parent_content, Mapping
    ):
        raise MatrixProvisionError("space linkage state returned non-mapping content")
    child_via = child_content.get("via", [])
    parent_via = parent_content.get("via", [])
    if not isinstance(child_via, Sequence) or isinstance(child_via, (str, bytes)):
        raise MatrixProvisionError("space child linkage has invalid via field")
    if not isinstance(parent_via, Sequence) or isinstance(parent_via, (str, bytes)):
        raise MatrixProvisionError("space parent linkage has invalid via field")
    if server_name not in child_via or server_name not in parent_via:
        raise MatrixProvisionError(
            f"space linkage via-entries missing {server_name!r}: "
            f"child_via={child_via!r} parent_via={parent_via!r}"
        )
    if parent_content.get("canonical") is not True:
        raise MatrixProvisionError(
            "space parent linkage is not canonical as provisioned"
        )
    return True


async def provision_private_space_and_room(
    client: Any,
    *,
    space_name: str,
    room_name: str,
    invite_user_ids: Sequence[str],
    admin_power_user_ids: Sequence[str] = (),
    room_topic: str | None = None,
) -> ProvisionReport:
    """Provision a private space + encrypted room pair and verify its state.

    Sequence: create space → create room (encryption in initial_state) →
    preassign and verify power levels on both → write parent/child linkage →
    verify encryption/linkage from actual server state → invite on both.

    Raises :class:`~medre.adapters.matrix.errors.MatrixProvisionError` on any
    response error or verification mismatch.  When failure occurs after a
    resource is created, the error preserves the created IDs and completed
    steps so the partial operation can be reconciled rather than blindly
    retried. Raises :class:`ValueError` on malformed user IDs or admins
    outside the invite set.
    """
    from medre.adapters.matrix.errors import MatrixProvisionError

    _validate_inputs(invite_user_ids, admin_power_user_ids)

    bot_user_id = getattr(client, "user_id", "")
    if not bot_user_id:
        raise ValueError("client has no user_id — restore_login first")
    server_name = _server_name_from_user_id(bot_user_id)

    grants: dict[str, int] = {bot_user_id: _ADMIN_POWER}
    for admin in admin_power_user_ids:
        grants[admin] = _ADMIN_POWER

    space_id: str | None = None
    room_id: str | None = None
    completed_steps: list[str] = []
    space_invited: list[str] = []
    room_invited: list[str] = []

    def _record_space(created_id: str) -> None:
        nonlocal space_id
        space_id = created_id
        completed_steps.append("space_created")

    def _record_room(created_id: str) -> None:
        nonlocal room_id
        room_id = created_id
        completed_steps.append("room_created")

    def _partial_error(exc: BaseException) -> MatrixProvisionError:
        return MatrixProvisionError(
            str(exc),
            space_id=space_id,
            room_id=room_id,
            completed_steps=tuple(completed_steps),
            invited_space_user_ids=tuple(space_invited),
            invited_room_user_ids=tuple(room_invited),
        )

    try:
        space_resource = await _provision_resource(
            client,
            name=space_name,
            admin_grants=grants,
            space=True,
            on_created=_record_space,
        )
        completed_steps.append("space_power_verified")
        room_resource = await _provision_resource(
            client,
            name=room_name,
            admin_grants=grants,
            space=False,
            initial_state=[encryption_initial_state()],
            topic=room_topic,
            on_created=_record_room,
        )
        completed_steps.append("room_power_verified")

        child_put = await client.room_put_state(
            space_resource.room_id,
            "m.space.child",
            space_child_content([server_name]),
            state_key=room_resource.room_id,
        )
        await _raise_if_error(child_put, "space child state put")
        completed_steps.append("space_child_link_written")
        parent_put = await client.room_put_state(
            room_resource.room_id,
            "m.space.parent",
            space_parent_content([server_name]),
            state_key=space_resource.room_id,
        )
        await _raise_if_error(parent_put, "space parent state put")
        completed_steps.append("room_parent_link_written")

        algorithm = await _verify_encryption(client, room_resource.room_id)
        completed_steps.append("encryption_verified")
        linkage = await _verify_linkage(
            client, space_resource.room_id, room_resource.room_id, server_name
        )
        completed_steps.append("linkage_verified")

        space_resource = replace(
            space_resource,
            invited=await _invite_users(
                client,
                space_resource.room_id,
                invite_user_ids,
                on_invited=space_invited.append,
            ),
        )
        completed_steps.append("space_invites_completed")
        room_resource = replace(
            room_resource,
            invited=await _invite_users(
                client,
                room_resource.room_id,
                invite_user_ids,
                on_invited=room_invited.append,
            ),
        )
        completed_steps.append("room_invites_completed")
    except asyncio.CancelledError:
        raise
    except MatrixProvisionError as exc:
        if space_id is None and room_id is None:
            raise
        raise _partial_error(exc) from exc
    except Exception as exc:
        if space_id is None and room_id is None:
            raise
        raise _partial_error(exc) from exc

    return ProvisionReport(
        space=space_resource,
        room=room_resource,
        encryption_algorithm=algorithm,
        linkage_verified=linkage,
        bot_user_id=bot_user_id,
    )
