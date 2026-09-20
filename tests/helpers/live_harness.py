"""Transport-neutral live-test harness helpers.

Provides environment gating, secret redaction, bounded async execution,
smoke-test result capture, and artifact directory management for MEDRE
live integration tests.  All helpers are pure functions or simple
dataclasses with no external dependencies beyond the Python standard
library.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypeVar

# ---------------------------------------------------------------------------
# Heuristic tokens used by ``redact_env_value`` to detect secret env vars.
# A variable whose *upper-cased* name contains any of these substrings
# is treated as sensitive and its value is replaced with "<redacted>".
# ---------------------------------------------------------------------------
_SECRET_NAME_PARTS: frozenset[str] = frozenset(
    {"TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH", "CREDENTIAL"}
)

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveRequirement:
    """A single environment-variable requirement for a live test.

    Attributes:
        env_name: The environment variable name (e.g. ``"MATRIX_HOMESERVER"``).
        secret: If ``True``, the value is redacted in reports.
        description: Human-readable purpose for the variable.
    """

    env_name: str
    secret: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class LiveEnvStatus:
    """Aggregate result of checking live-test environment variables.

    Attributes:
        enabled: ``True`` when every required variable is present and non-empty.
        missing: Tuple of variable names that are absent or empty.
        redacted_values: Mapping of variable names to their redacted string
            representations (present values as ``"<redacted>"`` for secrets,
            or the literal value for non-secrets).
    """

    enabled: bool
    missing: tuple[str, ...]
    redacted_values: dict[str, str]


@dataclass(frozen=True, slots=True)
class LiveSmokeResult:
    """Structured result from a single live smoke-test run.

    Attributes:
        transport: Transport name (e.g. ``"matrix"``, ``"meshtastic"``).
        adapter_id: The adapter identifier used for the test.
        status: Outcome string (e.g. ``"pass"``, ``"fail"``, ``"skip"``).
        native_message_id: Platform-native message ID returned on delivery.
        native_channel_id: Platform-native channel / room ID.
        storage_path: Path to any persisted test artefact.
        evidence_path: Path to captured evidence (logs, screenshots, etc.).
        notes: Arbitrary free-form notes attached to the result.
    """

    transport: str
    adapter_id: str
    status: str
    native_message_id: str | None = None
    native_channel_id: str | None = None
    storage_path: str | None = None
    evidence_path: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Environment gating
# ---------------------------------------------------------------------------


def live_env_status(requirements: Iterable[LiveRequirement]) -> LiveEnvStatus:
    """Check which live-test environment variables are present.

    Iterates over *requirements*, reads each ``env_name`` from
    ``os.environ``, and builds a :class:`LiveEnvStatus`.  Empty strings
    are treated as missing.

    Returns:
        A :class:`LiveEnvStatus` indicating whether all requirements are
        satisfied, which are missing, and a redacted snapshot of present
        values.
    """
    missing: list[str] = []
    redacted_values: dict[str, str] = {}

    for req in requirements:
        raw = os.environ.get(req.env_name)
        if not raw:
            missing.append(req.env_name)
        else:
            if req.secret:
                redacted_values[req.env_name] = "<redacted>"
            else:
                redacted_values[req.env_name] = redact_env_value(req.env_name, raw)

    return LiveEnvStatus(
        enabled=len(missing) == 0,
        missing=tuple(missing),
        redacted_values=redacted_values,
    )


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def redact_env_value(name: str, value: str | None) -> str:
    """Return a safe, possibly-redacted representation of an env value.

    - ``None`` values always yield ``"<redacted>"``.
    - Values whose *upper-cased* variable name contains a secret heuristic
      token (see :data:`_SECRET_NAME_PARTS`) are redacted.
    - All other values are returned as-is.

    Args:
        name: The environment variable name.
        value: The raw environment variable value (may be ``None``).

    Returns:
        Either the original value or ``"<redacted>"``.
    """
    if value is None:
        return "<redacted>"
    upper_name = name.upper()
    if any(token in upper_name for token in _SECRET_NAME_PARTS):
        return "<redacted>"
    return value


def assert_no_secret_leak(obj: object, secret_values: Iterable[str]) -> None:
    """Assert that no secret value appears in the serialized form of *obj*.

    Serialises *obj* via ``json.dumps(..., default=str)`` (which handles
    dicts, lists, dataclasses via ``default=str``, and primitive types) and
    then checks that none of the strings in *secret_values* occur as a
    substring of the serialised output.

    Args:
        obj: The object to serialise and check.
        secret_values: Iterable of raw secret strings that must not appear
            in the serialised output.

    Raises:
        AssertionError: If any secret value is found in the serialised form.
    """
    serialized = json.dumps(obj, default=str)
    for secret in secret_values:
        if not secret:
            continue
        assert secret not in serialized, (
            f"Secret value leaked in serialized output: "
            f"found substring of a protected value "
            f"(length {len(secret)})"
        )
        # Also check JSON-escaped form to catch secrets containing
        # characters that json.dumps would escape (e.g. " or \).
        escaped = json.dumps(secret)[1:-1]
        assert escaped not in serialized, (
            f"Secret value leaked (escaped form) in serialized output: "
            f"found substring of a protected value "
            f"(length {len(secret)})"
        )


# ---------------------------------------------------------------------------
# Bounded async execution
# ---------------------------------------------------------------------------


async def bounded(coro: Awaitable[_T], timeout: float, label: str) -> _T:
    """Await *coro* with a timeout, raising a descriptive error on expiry.

    Wraps :func:`asyncio.wait_for` so that timeout failures include the
    human-readable *label* for easier debugging in live-test logs.

    Args:
        coro: The awaitable / coroutine to execute.
        timeout: Maximum seconds to wait.
        label: Descriptive label included in the error message on timeout.

    Returns:
        The result of the awaited coroutine.

    Raises:
        RuntimeError: When the coroutine does not complete within *timeout*
            seconds.  The message includes *label* and *timeout*.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Live test timed out after {timeout}s: {label}") from None


# ---------------------------------------------------------------------------
# Smoke-test result serialisation
# ---------------------------------------------------------------------------


def live_result_to_json(result: LiveSmokeResult) -> str:
    """Serialise a :class:`LiveSmokeResult` to a pretty-printed JSON string.

    Args:
        result: The smoke-test result to serialise.

    Returns:
        An indented JSON string representation of the result.
    """
    return json.dumps(asdict(result), default=str, indent=2)


# ---------------------------------------------------------------------------
# NOT EXECUTED result factory
# ---------------------------------------------------------------------------


def not_executed_result(
    *,
    transport: str,
    adapter_id: str,
    reason: str = "",
) -> LiveSmokeResult:
    """Create a :class:`LiveSmokeResult` with status ``"not_executed"``.

    Used when a hardware-dependent or live test cannot run because the
    required hardware or service is unavailable.  This produces an
    honest artifact record rather than fabricating a pass/fail result.

    Args:
        transport: Transport name (e.g. ``"meshtastic"``).
        adapter_id: Adapter identifier.
        reason: Human-readable explanation for why the test was not
            executed (e.g. ``"serial radio not connected"``).

    Returns:
        A :class:`LiveSmokeResult` with ``status="not_executed"`` and
        the reason in ``notes``.
    """
    notes: tuple[str, ...] = ()
    if reason:
        notes = (reason,)
    return LiveSmokeResult(
        transport=transport,
        adapter_id=adapter_id,
        status="not_executed",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Live artifact directory convention
# ---------------------------------------------------------------------------

_default_artifact_dir: Path | None = None


def get_live_artifact_dir() -> Path:
    """Return the live-test artifact directory, creating it if needed.

    Reads ``MEDRE_LIVE_ARTIFACT_DIR`` from the environment.  When unset,
    defaults to ``.ci-artifacts/live-evidence/<timestamp>`` relative to
    the repository root (the directory containing ``pyproject.toml``).

    The default timestamped path is cached in the module on first call
    so that every call within a single test run returns the same
    directory.  Use ``MEDRE_LIVE_ARTIFACT_DIR`` to override.

    The directory is created with ``mkdir(parents=True, exist_ok=True)``
    before returning.

    Returns:
        A :class:`Path` to the artifact directory (guaranteed to exist).
    """
    global _default_artifact_dir
    env_val = os.environ.get("MEDRE_LIVE_ARTIFACT_DIR", "").strip()
    if env_val:
        p = Path(env_val)
    else:
        if _default_artifact_dir is None:
            repo_root = Path(__file__).resolve().parent.parent.parent
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            _default_artifact_dir = (
                repo_root / ".ci-artifacts" / "live-evidence" / timestamp
            )
        p = _default_artifact_dir
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Scoped unraisable-warning filters for pinned SDK boundaries
# ---------------------------------------------------------------------------
# pytest splits a ``filterwarnings`` spec on EVERY colon into
# ``action:message:category:module:lineno`` -- the message field may not
# contain a literal ``:``.  The unraisable texts targeted here all begin
# ``Exception ignored in: <...>``, so the colon is matched by ``.*`` in
# the message regex instead of appearing literally.  A malformed spec is
# a run-fatal pytest INTERNALERROR (a hardware-gated suite then cannot
# even report a skip), so the strings live here once and
# tests/test_live_harness.py runs each through pytest's own filter
# parser.  Each filter is deliberately narrow: any unraisable outside the
# documented boundary still errors.
#
# - ratchets: RNS 1.5.4 ``Destination._reload_ratchets`` (Destination.py:444)
#   opens ``<storage>/lxmf/ratchets/*.ratchets`` and never closes the
#   handle; the GC-time ResourceWarning surfaces as an unraisable in a
#   LATER test.  External pinned-SDK defect reached via
#   ``LXMRouter.register_delivery_identity -> enable_ratchets``; minimal
#   reproducer: medre-lab/rns_ratchets_leak_repro.py.
# - 443 / _SelectorTransport: pinned aiohttp performs a graceful TLS
#   shutdown on one idle keep-alive socket at ClientSession close
#   (``ssl_shutdown_timeout=30 s``, not reachable through nio 0.40.0's
#   public config); the socket is still mid-shutdown when the test loop
#   exits, surfacing either as the socket finaliser or the transport
#   finaliser.  MEDRE's own drain of client-bound request tasks is pinned
#   deterministically by test_stop_drains_client_bound_tasks_before_close,
#   so these filters cover only the shutdown-window socket.
# - system_bus_socket: bleak/BlueZ peer-helper teardown leaves the
#   system-bus socket for the same GC round; helper-owned, not MEDRE state.

RNS_RATCHETS_UNRAISABLE_FILTER = (
    "ignore:Exception ignored in.*ratchets:pytest.PytestUnraisableExceptionWarning"
)
AIOHTTP_TLS_SHUTDOWN_UNRAISABLE_FILTER = (
    "ignore:Exception ignored in.*<socket\\.socket.*443"
    ":pytest.PytestUnraisableExceptionWarning"
)
SELECTOR_TRANSPORT_UNRAISABLE_FILTER = (
    "ignore:Exception ignored in.*_SelectorTransport\\.__del__"
    ":pytest.PytestUnraisableExceptionWarning"
)
BLEAK_SYSTEM_BUS_UNRAISABLE_FILTER = (
    "ignore:Exception ignored in.*system_bus_socket"
    ":pytest.PytestUnraisableExceptionWarning"
)

PINNED_SDK_UNRAISABLE_FILTERS: tuple[str, ...] = (
    RNS_RATCHETS_UNRAISABLE_FILTER,
    AIOHTTP_TLS_SHUTDOWN_UNRAISABLE_FILTER,
    SELECTOR_TRANSPORT_UNRAISABLE_FILTER,
    BLEAK_SYSTEM_BUS_UNRAISABLE_FILTER,
)
