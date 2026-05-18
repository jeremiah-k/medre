"""Identity resolution service for mapping native identities to canonical actors.

The :class:`IdentityResolver` maintains an in-memory registry of
:class:`~medre.core.identity.actor.CanonicalActor` instances and
their linked :class:`~medre.core.identity.actor.NativeIdentity`
mappings.  It provides lookup, creation, and linking operations used by
adapters and the event pipeline.

Phase 1 uses a pure in-memory store.  A storage backend can be injected
later via the ``_actors`` and ``_native_index`` constructor parameters or
by subclassing.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from medre.core.identity.actor import (
    CanonicalActor,
    NativeIdentity,
    VerificationStatus,
)

# ---------------------------------------------------------------------------
# Storage protocol (future extensibility)
# ---------------------------------------------------------------------------


class ActorStore(Protocol):
    """Minimal protocol that a persistence backend must satisfy.

    Not used in Phase 1, but defined here so that downstream code can
    depend on the interface from the start.
    """

    async def get(self, actor_id: str) -> CanonicalActor | None: ...

    async def put(self, actor: CanonicalActor) -> None: ...

    async def find_by_native(
        self, platform: str, adapter_id: str, native_id: str
    ) -> CanonicalActor | None: ...


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class IdentityResolver:
    """Resolve native platform identities to stable canonical actors.

    The resolver maintains two indices:

    * ``_actors`` – ``{actor_id: CanonicalActor}``
    * ``_native_index`` – ``{(platform, adapter_id, native_id): actor_id}``

    Calling :meth:`resolve` with the same :class:`NativeIdentity` always
    returns the same :class:`CanonicalActor`.

    Parameters
    ----------
    store:
        Optional storage backend implementing the :class:`ActorStore`
        protocol.  When ``None`` (the default), a pure in-memory store is
        used.
    """

    def __init__(self, store: ActorStore | None = None) -> None:
        self._store = store
        # In-memory indices (used when store is None)
        self._actors: dict[str, CanonicalActor] = {}
        self._native_index: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(self, native_identity: NativeIdentity) -> CanonicalActor:
        """Return the canonical actor for *native_identity*, creating one if needed.

        If an actor is already linked to the given native identity, it is
        returned directly.  Otherwise a new :class:`CanonicalActor` is
        created, the native identity is linked to it, and the new actor is
        returned.

        Parameters
        ----------
        native_identity:
            The platform-scoped identity to resolve.

        Returns
        -------
        CanonicalActor
            The existing or newly created canonical actor.
        """
        key = native_identity.identity_key()

        # Check storage backend first if present.
        if self._store is not None:
            existing = await self._store.find_by_native(*key)
            if existing is not None:
                return existing

        # In-memory fast path.
        actor_id = self._native_index.get(key)
        if actor_id is not None:
            actor = self._actors.get(actor_id)
            if actor is not None:
                return actor

        # Create a new actor.
        actor = CanonicalActor(
            actor_id=uuid.uuid4().hex,
            display_name=native_identity.display_name,
            verification_status=VerificationStatus.UNVERIFIED,
            linked_identities=[native_identity],
        )

        self._actors[actor.actor_id] = actor
        self._native_index[key] = actor.actor_id

        if self._store is not None:
            await self._store.put(actor)

        return actor

    async def link_identity(
        self, actor_id: str, native_identity: NativeIdentity
    ) -> None:
        """Link an additional native identity to an existing canonical actor.

        Parameters
        ----------
        actor_id:
            The stable canonical ID of the target actor.
        native_identity:
            The native identity to attach.

        Raises
        ------
        KeyError
            If *actor_id* does not reference a known actor.
        """
        actor = await self._get_actor_or_raise(actor_id)
        key = native_identity.identity_key()

        # Idempotent: if already linked, do nothing.
        if key in self._native_index and self._native_index[key] == actor_id:
            return

        # Unlink from any previous actor.
        previous_actor_id = self._native_index.get(key)
        if previous_actor_id is not None and previous_actor_id != actor_id:
            previous_actor = self._actors.get(previous_actor_id)
            if previous_actor is not None:
                previous_actor.linked_identities = [
                    ni
                    for ni in previous_actor.linked_identities
                    if ni.identity_key() != key
                ]
                previous_actor.touch()

        actor.linked_identities.append(native_identity)
        actor.touch()
        self._native_index[key] = actor_id

        if self._store is not None:
            await self._store.put(actor)

    async def get_actor(self, actor_id: str) -> CanonicalActor | None:
        """Look up a canonical actor by its stable ID.

        Parameters
        ----------
        actor_id:
            The canonical actor ID.

        Returns
        -------
        CanonicalActor or None
            The actor if found, otherwise ``None``.
        """
        if self._store is not None:
            return await self._store.get(actor_id)
        return self._actors.get(actor_id)

    async def find_by_native(
        self, platform: str, adapter_id: str, native_id: str
    ) -> CanonicalActor | None:
        """Find a canonical actor by its native identity triple.

        Parameters
        ----------
        platform:
            Platform name (e.g. ``"matrix"``).
        adapter_id:
            Adapter instance identifier.
        native_id:
            Opaque native identifier string.

        Returns
        -------
        CanonicalActor or None
            The linked actor if found, otherwise ``None``.
        """
        if self._store is not None:
            return await self._store.find_by_native(platform, adapter_id, native_id)

        actor_id = self._native_index.get((platform, adapter_id, native_id))
        if actor_id is None:
            return None
        return self._actors.get(actor_id)

    async def set_verification(self, actor_id: str, status: VerificationStatus) -> None:
        """Update the verification status of a canonical actor.

        Parameters
        ----------
        actor_id:
            The canonical actor ID.
        status:
            The new verification status.

        Raises
        ------
        KeyError
            If *actor_id* does not reference a known actor.
        """
        actor = await self._get_actor_or_raise(actor_id)
        actor.verification_status = status
        actor.touch()

        if self._store is not None:
            await self._store.put(actor)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_actor_or_raise(self, actor_id: str) -> CanonicalActor:
        """Retrieve an actor or raise :class:`KeyError` if missing."""
        if self._store is not None:
            actor = await self._store.get(actor_id)
        else:
            actor = self._actors.get(actor_id)

        if actor is None:
            raise KeyError(f"No canonical actor with id={actor_id!r}")
        return actor
