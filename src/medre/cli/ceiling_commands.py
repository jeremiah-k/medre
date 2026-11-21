"""Storage ceiling CLI commands: read-only audit and receipt verification.

``medre storage ceiling audit``   — read-only per-store residual audit for
                                    content whose ceiling has elapsed.
``medre storage ceiling verify``  — read-only one-shot post-sweep proof:
                                    receipts re-checked against the store.

Both modes open storage strictly read-only, emit metadata-only JSON (event
identifiers and census store labels are content-free identity; no message
text, excerpt, or blob bytes are ever read), and never purge, tombstone,
record, or rewrite anything: remediation belongs to the guarded sweep.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .exit_codes import EXIT_BUILD, EXIT_CONFIG, EXIT_FAILED


def _ceiling_limit_error() -> None:
    print("Error: --limit needs an integer between 1 and 100", file=sys.stderr)
    raise SystemExit(EXIT_CONFIG)


def _ceiling_event_error() -> None:
    print("Error: --event needs a nonempty event ID", file=sys.stderr)
    raise SystemExit(EXIT_CONFIG)


def _validate_ceiling_flags(limit: int, event_id: str | None) -> None:
    """Reject malformed flags before touching storage."""
    if not 1 <= limit <= 100:
        _ceiling_limit_error()
    if event_id is not None and not event_id:
        _ceiling_event_error()


def _require_ceiling_database(storage_path: str) -> None:
    if not Path(storage_path).is_file():
        print("Error: existing SQLite database required", file=sys.stderr)
        raise SystemExit(EXIT_BUILD)


async def _storage_ceiling_audit(
    storage_path: str,
    *,
    limit: int = 25,
    event_id: str | None = None,
) -> None:
    """Report per-store residuals for elapsed ceilings; read-only.

    Walks the residual battery — the same battery the guarded enforcement
    verifies with — over content whose immutable ceiling has elapsed and
    reports what would be purged, per census store. Elapsed ceilings
    without an enforcement receipt are the sweep's next candidates;
    enforced events carrying residual content-potential values are the
    documented late-write case and are flagged until the next sweep
    re-purges them. Nothing is purged, tombstoned, or recorded.
    """
    from medre.core.storage.backend import StorageError
    from medre.core.storage.content import (
        UNMEASURED_CONTENT_SCOPES as CEILING_UNMEASURED_SCOPES,
    )

    from .json import to_json
    from .storage_helpers import _open_readonly_storage

    _validate_ceiling_flags(limit, event_id)
    _require_ceiling_database(storage_path)
    storage = await _open_readonly_storage(storage_path)
    try:
        report: dict[str, Any] = await storage.audit_content_ceilings(
            limit=limit, event_id=event_id
        )
    except (StorageError, ValueError):
        print(
            "Ceiling audit refused: storage state is not safe to read",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_BUILD) from None
    finally:
        await storage.close()

    result: dict[str, Any] = {
        "format": "medre-content-ceiling-audit",
        "version": 1,
        "scope": "core_sqlite",
        "mode": "audit",
        "purged": False,
        "physical_erasure_claimed": False,
        "unmeasured": list(CEILING_UNMEASURED_SCOPES),
        **report,
    }
    print(to_json(result))


async def _storage_ceiling_verify(
    storage_path: str,
    *,
    limit: int = 25,
    event_id: str | None = None,
) -> None:
    """Verify enforcement receipts against the live store; read-only.

    Re-runs the residual battery over every enforcement receipt in a
    bounded window and reports pass/fail per receipt with the receipt's
    recorded content-free counters (rows, logical bytes, blobs, and blob
    bytes removed) — the operator's one-shot proof that expired content is
    gone. A receipt fails when its event holds residual content-potential
    values now; an explicitly requested event without a receipt fails
    closed. Exits 1 when the verdict is failed so the proof is scriptable.
    """
    from medre.core.storage.backend import StorageError
    from medre.core.storage.content import (
        UNMEASURED_CONTENT_SCOPES as CEILING_UNMEASURED_SCOPES,
    )

    from .json import to_json
    from .storage_helpers import _open_readonly_storage

    _validate_ceiling_flags(limit, event_id)
    _require_ceiling_database(storage_path)
    storage = await _open_readonly_storage(storage_path)
    try:
        report: dict[str, Any] = await storage.verify_content_ceiling_receipts(
            limit=limit, event_id=event_id
        )
    except (StorageError, ValueError):
        print(
            "Ceiling receipt verification refused: storage state is not safe "
            "to read",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_BUILD) from None
    finally:
        await storage.close()

    result: dict[str, Any] = {
        "format": "medre-content-ceiling-verify",
        "version": 1,
        "scope": "core_sqlite",
        "mode": "verify",
        "purged": False,
        "physical_erasure_claimed": False,
        "unmeasured": list(CEILING_UNMEASURED_SCOPES),
        **report,
    }
    print(to_json(result))
    if result["verdict"] == "failed":
        raise SystemExit(EXIT_FAILED)
