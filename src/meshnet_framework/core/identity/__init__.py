"""Identity subsystem for the meshnet framework.

This package provides identity resolution between platform-specific
(native) identifiers and stable canonical actors.

Re-exported symbols
-------------------
* From :mod:`~meshnet_framework.core.identity.actor`:
  ``CanonicalActor``, ``NativeIdentity``, ``VerificationStatus``.
* From :mod:`~meshnet_framework.core.identity.resolver`:
  ``IdentityResolver``, ``ActorStore``.
"""

from meshnet_framework.core.identity.actor import (
    CanonicalActor,
    NativeIdentity,
    VerificationStatus,
)
from meshnet_framework.core.identity.resolver import (
    ActorStore,
    IdentityResolver,
)

__all__ = [
    "ActorStore",
    "CanonicalActor",
    "IdentityResolver",
    "NativeIdentity",
    "VerificationStatus",
]
