"""Recovery source enumeration — which subsystem reclaimed ownership.

Distinguishes startup recovery, retry-worker polling, and replay
execution so operators can attribute recovery actions correctly.
"""

from __future__ import annotations

import enum

__all__ = ["RecoverySource"]


class RecoverySource(enum.StrEnum):
    """Which subsystem performed the recovery action.

    Each ``RecoveryOwnershipAction`` carries one of these values in its
    ``recovery_source`` field.
    """

    STARTUP_RECOVERY = "startup_recovery"
    """Outbox item reclaimed during runtime startup (via
    :meth:`~medre.core.storage.backend.claim_due_outbox_items` called
    by the :class:`~medre.runtime.retry.RetryWorker` at boot)."""

    RETRY_WORKER_RECOVERY = "retry_worker_recovery"
    """Outbox item reclaimed during steady-state retry polling
    (the :class:`~medre.runtime.retry.RetryWorker` poll loop)."""

    REPLAY_EXECUTION = "replay_execution"
    """Outbox item reclaimed during replay execution
    (:class:`~medre.core.engine.replay.ReplayEngine`)."""
