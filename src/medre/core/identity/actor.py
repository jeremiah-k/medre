"""Identity models for canonical actors and their native-platform identities.

This module defines the core data structures used by the identity subsystem:

* :class:`VerificationStatus` – trust level of an identity mapping.
* :class:`NativeIdentity` – an identity scoped to a single adapter/platform.
* :class:`CanonicalActor` – a stable cross-platform identity that aggregates
  one or more :class:`NativeIdentity` instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class VerificationStatus(Enum):
    """Trust level of a canonical-to-native identity mapping.

    Attributes
    ----------
    UNVERIFIED:
        No mapping has been established; the actor is treated as standalone.
    CRYPTOGRAPHIC:
        Verified by a cryptographic key or proof.
    OPERATOR_LINKED:
        Manually linked by a human operator.
    ADAPTER_ASSERTED:
        The source adapter asserts this identity mapping.
    """

    UNVERIFIED = "unverified"
    CRYPTOGRAPHIC = "cryptographic"
    OPERATOR_LINKED = "operator_linked"
    ADAPTER_ASSERTED = "adapter_asserted"


@dataclass(frozen=True)
class NativeIdentity:
    """An identity scoped to a single adapter / platform.

    Two :class:`NativeIdentity` instances are considered equal when their
    ``platform``, ``adapter_id``, and ``native_id`` all match.  The
    ``display_name`` and ``metadata`` fields are ignored for equality and
    hashing purposes (frozen dataclass default behaviour).

    Attributes
    ----------
    platform:
        Protocol or platform name (e.g. ``"matrix"``, ``"meshtastic"``,
        ``"meshcore"``, ``"lxmf"``, ``"discord"``).
    adapter_id:
        Identifier of the adapter instance that owns this native identity.
    native_id:
        Opaque, platform-specific identifier string.
    display_name:
        Human-readable display name at the time the identity was observed.
    metadata:
        Arbitrary adapter-specific metadata attached to this identity.
    """

    platform: str
    adapter_id: str
    native_id: str
    display_name: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def identity_key(self) -> tuple[str, str, str]:
        """Return the composite lookup key ``(platform, adapter_id, native_id)``."""
        return (self.platform, self.adapter_id, self.native_id)


@dataclass
class CanonicalActor:
    """A stable, cross-platform identity that aggregates native identities.

    Each :class:`CanonicalActor` is assigned a UUID at creation time and may
    be linked to zero or more :class:`NativeIdentity` instances from different
    adapters.  The actor is mutable so that identities can be linked and
    unlinked over its lifetime.

    Attributes
    ----------
    actor_id:
        Stable canonical identifier (UUID).
    display_name:
        Preferred human-readable name, if known.
    short_name:
        Abbreviated name for constrained displays.
    verification_status:
        Current trust level of the identity mapping.
    linked_identities:
        Ordered list of native identities linked to this actor.
    metadata:
        Arbitrary metadata attached to this actor.
    created_at:
        Timestamp when the actor was first created (UTC).
    updated_at:
        Timestamp of the last mutation to this actor (UTC).
    """

    actor_id: str
    display_name: str | None = None
    short_name: str | None = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    linked_identities: list[NativeIdentity] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self) -> None:
        """Update ``updated_at`` to the current UTC time."""
        self.updated_at = datetime.now(timezone.utc)
