"""Identity subsystem for the medre.

This package provides identity resolution between platform-specific
(native) identifiers and stable canonical actors.

Package-level imports
---------------------
* From :mod:`~medre.core.identity.actor`:
  ``CanonicalActor``, ``NativeIdentity``, ``VerificationStatus``.
* From :mod:`~medre.core.identity.resolver`:
  ``IdentityResolver``, ``ActorStore``.
"""

from medre.core.identity.actor import (
    CanonicalActor,
    NativeIdentity,
    VerificationStatus,
)
from medre.core.identity.resolver import (
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
