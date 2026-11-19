"""Adapter startup-info contract.

Core-side seam that lets an adapter surface platform facts at startup
(for example a Meshtastic board name and firmware version, or a Matrix
homeserver and user id).  The seam is deliberately narrow:

* **Duck-typed discovery.**  Core looks for an optional async
  ``startup_info()`` callable on the adapter object with ``getattr``.
  Absence means the adapter has nothing to report; adapters at any pin
  keep working.  Core never imports adapter symbols.
* **Bounded, non-blocking emission.**  When an adapter transitions to
  ready during runtime assembly, core awaits ``startup_info()`` under a
  bounded timeout.  The call may never block startup: a timeout or
  failure yields no info and never fails the adapter.
* **Pure reads.**  ``startup_info()`` formats already-gathered state the
  provider holds (diagnostics, handshake data).  It **MUST NOT** perform
  new device I/O; a provider that would have to poll simply does not
  implement it.
* **Display-only durability.**  Emitted info persists as the adapter's
  last-known startup info in the adapter state directory.  The cache is
  display-only: it is never an authority for routing or delivery
  decisions.  Values read back from the cache render with a ``cached``
  marker until a fresh emission replaces them.
* **Shared presentation.** Providers supply ordered facts. Core renders
  compact rows, labels cached facts, and filters credentials before
  persisting or displaying any field.

The record shape is explicitly **not schema-frozen**: nothing under
``docs/schemas`` describes it, and the durable payload may change
without a coordinated release.

See ``docs/spec/adapter-startup-info.md`` for the normative contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from _thread import LockType
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Mapping, TypeGuard, TypeVar
from weakref import WeakValueDictionary

from medre.core.observability.sanitization import (
    REDACTED_TOKEN,
    SECRET_FIELD_TOKENS,
    SECRET_KEY_PATTERNS,
    contains_credential_shape,
    sanitize_error,
)

__all__ = [
    "AdapterStartupInfo",
    "CACHE_FILENAME",
    "DEFAULT_EMISSION_TIMEOUT_SECONDS",
    "StartupInfoField",
    "collect_startup_info",
    "read_cache_file",
    "render_startup_info_block",
    "run_cache_io",
    "startup_info_callable",
    "write_cache_file",
]

_logger = logging.getLogger(__name__)

DEFAULT_EMISSION_TIMEOUT_SECONDS: float = 2.0
"""Upper bound for one ``startup_info()`` await at the ready transition.

Emission runs alongside peer adapter starts, so the bound applies per
adapter. The supervisor stops waiting after this interval; it cannot
preempt synchronous code in an async hook or forcibly stop a thread.
"""

CACHE_FILENAME: str = "startup-info.json"
"""Durable last-known startup-info file inside the adapter state directory.

The payload shape is not schema-frozen and may change between releases;
readers must tolerate unknown keys and missing fields.
"""

# Keep each adapter block compact even with oversized provider values.
_MAX_ROWS = 2
_MAX_VALUE_CHARS: int = 64
_MAX_RAW_CHARS = 4096
_MAX_FACTS_PER_ROW = 8
_MAX_CANDIDATES_PER_ROW = 128
_MAX_CACHE_BYTES = 64 * 1024

_TRUNCATION_SUFFIX = "…"

# Field-name tokens that mark pairing material in addition to the
# canonical sanitization token set.
_PAIRING_TOKENS: frozenset[str] = frozenset(
    {"pairing", "psk", "pin", "private", "bearer", "passphrase", "passwd"}
)

# Explicit public-key names keep published identifiers legible.
_PUBLIC_KEY_NAME_TOKENS: frozenset[str] = frozenset({"public", "pub"})

# Device identifiers are operator-visible platform facts on the same
# class as board names and firmware versions: they identify hardware in
# logs and packet headers rather than authorizing anything, so they
# render in full.  The shared error-sanitization anchor that treats
# ``device_id`` as key material stays untouched for error surfaces.
_DEVICE_ID_NAMES: frozenset[str] = frozenset({"device_id", "deviceid"})

_NAME_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FACT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")

# Value shapes that are never rendered in full.
# Credential-bearing URLs are omitted outright.
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_URL_USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/]*@[^\s]+")


# Terminal-display boundary: reject control characters in rendered
# values (same rationale as the native setup-instructions seam).
def _unsafe_text(value: str) -> bool:
    return any(
        unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in value
    )


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartupInfoField:
    """One redacted startup fact and its display-only freshness flag.

    ``cached`` marks values that came from the durable last-known cache
    rather than a fresh emission.  It is a provenance label for
    operators only; no runtime decision may read it.
    """

    name: str
    value: str
    cached: bool = False
    row: int = 0
    label: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterStartupInfo:
    """Redacted, display-only startup facts for one configured adapter."""

    adapter_id: str
    transport: str
    fields: tuple[StartupInfoField, ...]
    emitted_at: str

    def with_cached_flags(self, cached: bool) -> AdapterStartupInfo:
        """Return a copy with every field's ``cached`` flag set."""
        if all(field.cached == cached for field in self.fields):
            return self
        return replace(
            self, fields=tuple(replace(field, cached=cached) for field in self.fields)
        )

    def to_dict(self, *, include_cached: bool = False) -> dict[str, object]:
        """Serialize ordered facts for the cache or the JSON listing."""
        fields = []
        for field in _validated_fields(self.fields):
            entry: dict[str, object] = {
                "name": field.name,
                "value": field.value,
                "row": field.row,
            }
            if field.label is not None:
                entry["label"] = field.label
            if include_cached:
                entry["cached"] = field.cached
            fields.append(entry)
        return {
            "adapter_id": self.adapter_id,
            "transport": self.transport,
            "emitted_at": self.emitted_at,
            "fields": fields,
        }

    @classmethod
    def from_cache_dict(cls, data: Mapping[str, Any]) -> AdapterStartupInfo | None:
        """Parse a durable payload; return ``None`` when it is unusable.

        Cached facts pass the same validation as live facts, including
        the explicit public-key exception.
        """
        adapter_id = data.get("adapter_id")
        transport = data.get("transport")
        emitted_at = data.get("emitted_at")
        raw_fields = data.get("fields")
        if not (
            isinstance(adapter_id, str)
            and isinstance(transport, str)
            and isinstance(emitted_at, str)
            and isinstance(raw_fields, list)
        ):
            return None
        # Metadata is not provider-owned display text. A corrupt timestamp
        # cannot smuggle arbitrary content into the JSON listing.
        try:
            timestamp = datetime.fromisoformat(emitted_at)
            if timestamp.utcoffset() is None:
                return None
        except (ValueError, OverflowError):
            return None
        fields: list[StartupInfoField] = []
        counts = [0] * _MAX_ROWS
        for raw_field in islice(raw_fields, _MAX_CANDIDATES_PER_ROW * _MAX_ROWS):
            if not isinstance(raw_field, Mapping):
                continue
            row = raw_field.get("row", 0)
            if type(row) is not int or not 0 <= row < _MAX_ROWS:
                continue
            field = _startup_fact(
                raw_field.get("name"),
                raw_field.get("value"),
                raw_field.get("label"),
                row=row,
                cached=True,
            )
            if field is not None and counts[row] < _MAX_FACTS_PER_ROW:
                fields.append(field)
                counts[row] += 1
        if not fields:
            return None
        return cls(
            adapter_id=adapter_id,
            transport=transport,
            fields=tuple(fields),
            emitted_at=timestamp.isoformat(),
        )


# ---------------------------------------------------------------------------
# Duck-typed discovery
# ---------------------------------------------------------------------------


def startup_info_callable(provider: object) -> Callable[[], object] | None:
    """Return the provider's ``startup_info`` callable, or ``None``.

    Discovery is duck-typed with ``getattr``: a provider without the
    symbol — or with a non-callable one — simply has nothing to report.
    Core never imports adapter symbols to find this seam.
    """
    candidate = getattr(provider, "startup_info", None)
    if candidate is None or not callable(candidate):
        return None
    provider_callable: Callable[[], object] = candidate
    return provider_callable


# ---------------------------------------------------------------------------
# Core render-boundary redaction
# ---------------------------------------------------------------------------


def _name_tokens(name: str) -> frozenset[str]:
    return frozenset(_NAME_TOKEN_SPLIT.split(_CAMEL_BOUNDARY.sub("_", name).lower()))


def _field_name_is_secret(name: str) -> bool:
    """Only exempt explicit public key names from generic key tokens."""
    lowered = name.lower()
    if lowered in _DEVICE_ID_NAMES:
        return False
    if any(pattern.search(lowered) for pattern in SECRET_KEY_PATTERNS):
        return True
    tokens = _name_tokens(name)
    secret_tokens = SECRET_FIELD_TOKENS | _PAIRING_TOKENS
    if _is_public_key_name(name):
        secret_tokens = secret_tokens - {"key", "keys"}
    return bool(tokens & secret_tokens)


def _is_public_key_name(name: str) -> bool:
    """Recognize explicit public-key names, not arbitrary generic keys."""
    tokens = _name_tokens(name)
    return bool(
        tokens & {"pubkey", "publickey"}
        or (tokens & _PUBLIC_KEY_NAME_TOKENS and "key" in tokens)
    )


def _redact_value(name: str, value: object) -> str | None:
    """Redact one raw value for rendering; ``None`` drops the field.

    Defense-in-depth boundary applied wherever startup facts enter a
    rendered or durable surface, regardless of what the adapter
    returned:

    * secret-named and pairing-named fields are omitted entirely;
      a value that cannot be shown safely does not show at all;
    * ordinary identifiers, addresses, and explicit public keys are
      eligible for display subject to credential and length checks;
    * non-string scalars are stringified; containers and ``None`` are
      dropped;
    * values carrying control characters are dropped;
    * credential-bearing URLs are omitted outright;
    * token-like values render as the canonical redaction marker;
    * remaining values are capped at a pure display-length limit.

    The transformation is idempotent so core can re-apply it at every
    render boundary without changing already-rendered records.
    """
    if (
        not _FACT_NAME_RE.fullmatch(name)
        or _field_name_is_secret(name)
        or contains_credential_shape(name)
    ):
        return None
    if isinstance(value, (bool, int, float)):
        try:
            rendered = str(value)
        except ValueError:
            return None
    elif isinstance(value, str):
        rendered = value
    else:
        return None
    if len(rendered) > _MAX_RAW_CHARS or not rendered.strip() or _unsafe_text(rendered):
        return None
    # Startup facts need no query strings or fragments. They may carry
    # opaque credentials whose parameter names are unknown to MEDRE.
    if _URL_USERINFO_RE.search(rendered) or any(
        "?" in url or "#" in url for url in _URL_RE.findall(rendered)
    ):
        return None
    public_key = _is_public_key_name(name)
    if contains_credential_shape(rendered, allow_public_key=public_key):
        return REDACTED_TOKEN
    if not public_key:
        rendered = sanitize_error(rendered)
        if REDACTED_TOKEN in rendered:
            return REDACTED_TOKEN
    if len(rendered) > _MAX_VALUE_CHARS:
        return (
            rendered[: _MAX_VALUE_CHARS - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
        )
    return rendered


def _startup_fact(
    name: object, value: object, label: object, *, row: int, cached: bool = False
) -> StartupInfoField | None:
    if not isinstance(name, str):
        return None
    rendered = _redact_value(name, value)
    if rendered is None:
        return None
    return StartupInfoField(
        name=name,
        value=rendered,
        cached=cached,
        row=row,
        label=_redact_value("label", label) if isinstance(label, str) else None,
    )


def _validated_fields(
    fields: Sequence[StartupInfoField],
) -> tuple[StartupInfoField, ...]:
    """Apply the same facts policy on every output surface."""
    result: list[StartupInfoField] = []
    counts = [0] * _MAX_ROWS
    for field in islice(fields, _MAX_CANDIDATES_PER_ROW * _MAX_ROWS):
        if type(field.row) is not int or not 0 <= field.row < _MAX_ROWS:
            continue
        if counts[field.row] >= _MAX_FACTS_PER_ROW:
            continue
        fact = _startup_fact(
            field.name, field.value, field.label, row=field.row, cached=field.cached
        )
        if fact is not None:
            result.append(fact)
            counts[field.row] += 1
    return tuple(result)


def _row_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _redacted_fields(raw: Mapping[str, object]) -> tuple[StartupInfoField, ...]:
    """Read adapter-owned ordered rows, or accept a flat mapping of facts."""
    fields: list[StartupInfoField] = []
    rows = raw.get("rows")
    if not _row_sequence(rows):
        for name, value in islice(raw.items(), _MAX_CANDIDATES_PER_ROW):
            label = name.replace("_", " ") if isinstance(name, str) else None
            field = _startup_fact(name, value, label, row=0)
            if field is not None:
                fields.append(field)
                if len(fields) == _MAX_FACTS_PER_ROW:
                    break
        return tuple(fields)

    for index, row in enumerate(islice(rows, _MAX_ROWS)):
        if not _row_sequence(row):
            continue
        count = 0
        for fact in islice(row, _MAX_CANDIDATES_PER_ROW):
            if not isinstance(fact, Mapping):
                continue
            field = _startup_fact(
                fact.get("name"), fact.get("value"), fact.get("label"), row=index
            )
            if field is not None:
                fields.append(field)
                count += 1
                if count == _MAX_FACTS_PER_ROW:
                    break
    return tuple(fields)


# ---------------------------------------------------------------------------
# Bounded emission and optional cache I/O
# ---------------------------------------------------------------------------

_T = TypeVar("_T")
# Retain unfinished work until it really retires, including exceptions. A
# timed-out operation cannot publish startup facts through its supervisor.
_pending_optional_tasks: set[asyncio.Task[Any]] = set()


def _retire_optional(task: asyncio.Task[Any]) -> None:
    _pending_optional_tasks.discard(task)
    if not task.cancelled():
        task.exception()


async def _wait_optional(
    work: Coroutine[Any, Any, _T],
    timeout_seconds: float,
    *,
    retire: Callable[[], None] | None = None,
) -> _T:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    task = asyncio.create_task(work, name="adapter-startup-info")
    _pending_optional_tasks.add(task)
    task.add_done_callback(_retire_optional)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        if retire is not None:
            retire()
        raise
    if task not in done or loop.time() >= deadline:
        if retire is not None:
            retire()
        raise TimeoutError("startup-info wait expired")
    if task.cancelled():
        raise RuntimeError("startup-info operation cancelled itself")
    return task.result()


async def run_cache_io(
    call: Callable[[], _T],
    *,
    timeout_seconds: float,
    retire: Callable[[], None] | None = None,
) -> _T:
    """Bound an off-loop cache operation without cancelling its worker thread."""
    return await _wait_optional(asyncio.to_thread(call), timeout_seconds, retire=retire)


async def collect_startup_info(
    adapter_id: str,
    transport: str,
    provider: object,
    *,
    timeout_seconds: float = DEFAULT_EMISSION_TIMEOUT_SECONDS,
    emitted_at: str,
) -> AdapterStartupInfo | None:
    """Collect display facts without trusting a provider to finish cancellation.

    Sync discovery/invocation and normalization run off-loop. Async hooks
    must cooperate with the loop; neither a thread nor blocking async code
    can be forcibly interrupted. Late results are discarded.
    """
    retired = threading.Event()
    hook_task: asyncio.Future[object] | None = None

    def retire() -> None:
        retired.set()
        if hook_task is not None and not hook_task.done():
            hook_task.cancel()

    def invoke() -> object:
        if retired.is_set():
            return None
        provider_callable = startup_info_callable(provider)
        if provider_callable is None or retired.is_set():
            return None
        raw = provider_callable()
        if retired.is_set() and inspect.iscoroutine(raw):
            raw.close()
            return None
        return raw

    async def normalize() -> tuple[StartupInfoField, ...]:
        nonlocal hook_task
        raw = await asyncio.to_thread(invoke)
        if retired.is_set():
            # A sync factory may return an unstarted coroutine. Do not
            # execute it after its supervisor has retired the emission.
            if inspect.iscoroutine(raw):
                raw.close()
            return ()
        if inspect.isawaitable(raw):
            hook_task = asyncio.ensure_future(raw)
            raw = await hook_task
        if retired.is_set() or not isinstance(raw, Mapping):
            return ()
        return await asyncio.to_thread(_redacted_fields, raw)

    try:
        fields = await _wait_optional(normalize(), timeout_seconds, retire=retire)
    except TimeoutError:
        return None
    except Exception as exc:
        _logger.debug(
            "Adapter %s.%s startup_info unavailable (%s)",
            transport,
            adapter_id,
            type(exc).__name__,
        )
        return None
    if not fields:
        return None
    return AdapterStartupInfo(
        adapter_id=adapter_id, transport=transport, fields=fields, emitted_at=emitted_at
    )


# ---------------------------------------------------------------------------
# Durable last-known cache
# ---------------------------------------------------------------------------


# Serialize same-path publication within this process, even if an earlier
# supervisor has stopped waiting. Unrelated paths never share a disk lock.
_cache_locks: WeakValueDictionary[Path, LockType] = WeakValueDictionary()
_cache_locks_guard = threading.Lock()


def write_cache_file(
    path: Path,
    info: AdapterStartupInfo,
    *,
    expires_at: float | None = None,
    retired: threading.Event | None = None,
) -> None:
    """Atomically publish a private cache file; expiry prevents late staging.

    Once replacement starts, the OS operation cannot be cancelled. A later
    same-path write cannot overtake it within this process.
    """

    def expired() -> bool:
        return (retired is not None and retired.is_set()) or (
            expires_at is not None and time.monotonic() >= expires_at
        )

    path = path.absolute()
    with _cache_locks_guard:
        lock = _cache_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _cache_locks[path] = lock
    if expired():
        raise TimeoutError("startup-info cache write expired")
    if expires_at is None:
        lock.acquire()
    elif not lock.acquire(timeout=max(0, expires_at - time.monotonic())):
        raise TimeoutError("startup-info cache write expired waiting for prior writer")
    try:
        if expired():
            raise TimeoutError("startup-info cache write expired")
        encoded = json.dumps(info.to_dict(), sort_keys=True, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if expired():
            raise TimeoutError("startup-info cache write expired")
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(encoded)
            if expired():
                raise TimeoutError("startup-info cache write expired")
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    finally:
        lock.release()


def read_cache_file(
    path: Path, *, adapter_id: str | None = None, transport: str | None = None
) -> AdapterStartupInfo | None:
    """Read a bounded regular-file snapshot; malformed caches are absent."""
    try:
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            encoded = handle.read(_MAX_CACHE_BYTES + 1)
        if len(encoded) > _MAX_CACHE_BYTES:
            return None
        raw = json.loads(encoded.decode("utf-8"))
        if not isinstance(raw, Mapping):
            return None
        info = AdapterStartupInfo.from_cache_dict(raw)
    except (OSError, ValueError, RecursionError, OverflowError):
        return None
    if info is None or (
        (adapter_id is not None and info.adapter_id != adapter_id)
        or (transport is not None and info.transport != transport)
    ):
        return None
    return info


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_startup_info_block(heading: str, info: AdapterStartupInfo | None) -> str:
    """Render the same compact block for live startup and adapter listings."""
    if info is None:
        return heading
    rows: list[list[str]] = [[] for _ in range(_MAX_ROWS)]
    cached = True
    for fact in _validated_fields(info.fields):
        rows[fact.row].append(
            f"{fact.label} {fact.value}" if fact.label else fact.value
        )
        cached = cached and fact.cached
    lines = ["  " + " · ".join(row) for row in rows if row]
    if not lines:
        return heading
    if cached:
        heading += " (cached)"
    return "\n".join([heading, *lines])
