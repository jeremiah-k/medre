"""Transport-neutral canonical attachment descriptor.

A ``message.file`` canonical event carries exactly one primary attachment
descriptor in its payload under :data:`ATTACHMENT_PAYLOAD_KEY`.  The
descriptor is the single typed, validated seam for attachment metadata:
filename, media kind, MIME type, measured byte length, safe optional
dimensions/duration, and — when durable bytes were retained — an opaque
immutable local content reference.

Transport provenance (Matrix MXC locators, wire ``file`` objects,
encrypted-media key material, local paths, raw bytes) never enters this
descriptor.  Adapters keep native provenance in their own versioned
``metadata.native.data`` namespace.

Descriptor states are explicit:

* **retained** — ``content_ref`` is set, ``unavailable_reason`` is ``None``.
  ``size_bytes`` is the measured length of the stored bytes.
* **unavailable** — ``unavailable_reason`` is one of the stable secret-free
  codes in :data:`ATTACHMENT_UNAVAILABLE_REASONS` and ``content_ref`` is
  ``None``.  ``size_bytes``, when present, is the source-declared value and
  was never verified against retained bytes.
* **declared (in-flight only)** — the wire-declared form produced by a
  decoding adapter before retention is decided.  It never persists: core
  admission rewrites it to a retained descriptor when bytes are admitted
  atomically, and the adapter rewrites it to an unavailable descriptor
  before any descriptor-only admission.

Content is never fabricated: an unavailable descriptor describes the
attachment honestly and carries no usable content reference.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: Payload key holding the descriptor dict on a ``message.file`` event.
ATTACHMENT_PAYLOAD_KEY = "attachment"

#: Format of the opaque local content reference assigned by core
#: admission: content-addressed SHA-256, lowercase hex.
CONTENT_REF_PATTERN: re.Pattern[str] = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Supported media kinds (transport-neutral).
ATTACHMENT_KINDS: frozenset[str] = frozenset({"image", "audio", "video", "file"})

#: Stable, secret-free unavailable reason codes.
ATTACHMENT_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "policy_disabled",
        "history_suppressed",
        "oversized",
        "malformed_source",
        "integrity_failed",
        "unsupported_source",
        "quota_exceeded",
        "not_retained",
        "content_missing",
    }
)

#: Fields allowed in a complete (persisted-form) descriptor payload.
_DESCRIPTOR_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "filename",
        "mime_type",
        "size_bytes",
        "width",
        "height",
        "duration_ms",
        "content_ref",
        "unavailable_reason",
    }
)

#: Fields allowed in the wire-declared (retention-pending) form.
_DECLARED_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "filename",
        "mime_type",
        "size_bytes",
        "width",
        "height",
        "duration_ms",
    }
)

_NUMERIC_FIELDS: tuple[str, ...] = ("size_bytes", "width", "height", "duration_ms")
_STRING_FIELDS: tuple[str, ...] = (
    "filename",
    "mime_type",
    "content_ref",
    "unavailable_reason",
)


def _optional_int(value: object) -> int | None:
    """Return a nonnegative integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value or None


class AttachmentDescriptorError(ValueError):
    """Raised when a payload attachment descriptor is malformed."""


class AttachmentDescriptor:
    """One validated primary attachment descriptor.

    Use :meth:`from_payload` / :meth:`to_payload` for the canonical dict
    form stored in the event payload.  Construction via ``__init__`` is
    unrestricted for internal builders, but :meth:`validate` enforces the
    exclusive retained/unavailable invariant and field domains.
    """

    __slots__ = (
        "kind",
        "filename",
        "mime_type",
        "size_bytes",
        "width",
        "height",
        "duration_ms",
        "content_ref",
        "unavailable_reason",
    )

    def __init__(
        self,
        *,
        kind: str,
        filename: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        content_ref: str | None = None,
        unavailable_reason: str | None = None,
    ) -> None:
        self.kind = kind
        self.filename = filename
        self.mime_type = mime_type
        self.size_bytes = size_bytes
        self.width = width
        self.height = height
        self.duration_ms = duration_ms
        self.content_ref = content_ref
        self.unavailable_reason = unavailable_reason

    # -- Validation -------------------------------------------------------

    def _validate_kind(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in ATTACHMENT_KINDS:
            raise AttachmentDescriptorError(
                f"attachment kind must be one of {sorted(ATTACHMENT_KINDS)}, "
                f"got {self.kind!r}"
            )

    def _validate_numbers(self) -> None:
        for name in _NUMERIC_FIELDS:
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AttachmentDescriptorError(
                    f"attachment {name} must be a nonnegative integer, "
                    f"got {value!r}"
                )

    def _validate_declared(self) -> None:
        self._validate_kind()
        self._validate_numbers()

    def validate(self) -> None:
        """Enforce descriptor invariants or raise ``AttachmentDescriptorError``."""
        self._validate_kind()
        if self.content_ref is not None and self.unavailable_reason is not None:
            raise AttachmentDescriptorError(
                "attachment descriptor cannot be both retained "
                "(content_ref) and unavailable (reason)"
            )
        if self.content_ref is None and self.unavailable_reason is None:
            raise AttachmentDescriptorError(
                "attachment descriptor must be either retained (content_ref) "
                "or unavailable (unavailable_reason)"
            )
        if self.content_ref is not None and (
            not isinstance(self.content_ref, str)
            or CONTENT_REF_PATTERN.match(self.content_ref) is None
        ):
            raise AttachmentDescriptorError(
                "attachment content_ref must be a local content-addressed "
                "reference of the form sha256:<64 lowercase hex>, "
                f"got {self.content_ref!r}"
            )
        if self.unavailable_reason is not None and (
            not isinstance(self.unavailable_reason, str)
            or self.unavailable_reason not in ATTACHMENT_UNAVAILABLE_REASONS
        ):
            raise AttachmentDescriptorError(
                f"attachment unavailable_reason must be one of "
                f"{sorted(ATTACHMENT_UNAVAILABLE_REASONS)}, "
                f"got {self.unavailable_reason!r}"
            )
        self._validate_numbers()

    # -- Derived states ---------------------------------------------------

    @property
    def retained(self) -> bool:
        """Return whether durable local content is referenced."""
        return self.content_ref is not None

    def with_retained_content(
        self, content_ref: str, measured_size: int
    ) -> "AttachmentDescriptor":
        """Return a copy referencing verified durable content."""
        return AttachmentDescriptor(
            kind=self.kind,
            filename=self.filename,
            mime_type=self.mime_type,
            size_bytes=measured_size,
            width=self.width,
            height=self.height,
            duration_ms=self.duration_ms,
            content_ref=content_ref,
            unavailable_reason=None,
        )

    def with_unavailable(self, reason: str) -> "AttachmentDescriptor":
        """Return a copy that is explicitly unavailable for *reason*."""
        if reason not in ATTACHMENT_UNAVAILABLE_REASONS:
            raise AttachmentDescriptorError(
                f"unknown attachment unavailable reason {reason!r}"
            )
        return AttachmentDescriptor(
            kind=self.kind,
            filename=self.filename,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            width=self.width,
            height=self.height,
            duration_ms=self.duration_ms,
            content_ref=None,
            unavailable_reason=reason,
        )

    # -- Payload mapping --------------------------------------------------

    def to_payload(self) -> dict[str, object]:
        """Serialize to the canonical payload dict form."""
        self.validate()
        result: dict[str, object] = {"kind": self.kind}
        optional: tuple[tuple[str, object | None], ...] = (
            ("filename", self.filename),
            ("mime_type", self.mime_type),
            ("size_bytes", self.size_bytes),
            ("width", self.width),
            ("height", self.height),
            ("duration_ms", self.duration_ms),
            ("content_ref", self.content_ref),
            ("unavailable_reason", self.unavailable_reason),
        )
        result.update({key: value for key, value in optional if value is not None})
        return result

    def to_declared_payload(self) -> dict[str, object]:
        """Serialize the wire-declared (retention-pending) dict form.

        Only declared metadata fields are emitted — no ``content_ref`` and
        no ``unavailable_reason``.  This form exists solely on the in-flight
        decoded event between an adapter and core admission; admission
        always rewrites it to a retained descriptor before the event
        persists, so no persisted event ever carries it.
        """
        self._validate_declared()
        result: dict[str, object] = {"kind": self.kind}
        optional: tuple[tuple[str, object | None], ...] = (
            ("filename", self.filename),
            ("mime_type", self.mime_type),
            ("size_bytes", self.size_bytes),
            ("width", self.width),
            ("height", self.height),
            ("duration_ms", self.duration_ms),
        )
        result.update({key: value for key, value in optional if value is not None})
        return result

    @staticmethod
    def _strict_fields(
        value: Mapping[str, Any],
        *,
        allowed: frozenset[str],
    ) -> dict[str, object]:
        """Strictly coerce allowed fields; reject everything else."""
        unknown = set(value) - allowed
        if unknown:
            raise AttachmentDescriptorError(
                f"attachment descriptor has field(s) outside the allowed "
                f"set {sorted(allowed)}: {sorted(unknown)}"
            )
        strict: dict[str, object] = {}
        for key in sorted(allowed):
            if key not in value or value[key] is None:
                continue
            raw = value[key]
            if key == "kind":
                strict[key] = raw
            elif key in _STRING_FIELDS:
                coerced = _optional_str(raw)
                if coerced is None:
                    raise AttachmentDescriptorError(
                        f"attachment {key} must be a non-empty string, got {raw!r}"
                    )
                strict[key] = coerced
            else:
                coerced = _optional_int(raw)
                if coerced is None:
                    raise AttachmentDescriptorError(
                        f"attachment {key} must be a nonnegative integer, "
                        f"got {raw!r}"
                    )
                strict[key] = coerced
        return strict

    @classmethod
    def from_payload(cls, value: object) -> "AttachmentDescriptor":
        """Strictly parse and validate a descriptor from a payload mapping.

        Present-but-invalid values raise :class:`AttachmentDescriptorError`
        (booleans are not integers, empty strings are not values), and so do
        unknown fields: scattered dict interpretations cannot accrete around
        the seam.
        """
        if not isinstance(value, Mapping):
            raise AttachmentDescriptorError("attachment descriptor must be a mapping")
        strict = cls._strict_fields(value, allowed=_DESCRIPTOR_FIELDS)
        descriptor = cls(
            kind=str(strict.get("kind", "")),
            filename=strict.get("filename"),  # type: ignore[arg-type]
            mime_type=strict.get("mime_type"),  # type: ignore[arg-type]
            size_bytes=strict.get("size_bytes"),  # type: ignore[arg-type]
            width=strict.get("width"),  # type: ignore[arg-type]
            height=strict.get("height"),  # type: ignore[arg-type]
            duration_ms=strict.get("duration_ms"),  # type: ignore[arg-type]
            content_ref=strict.get("content_ref"),  # type: ignore[arg-type]
            unavailable_reason=strict.get("unavailable_reason"),  # type: ignore[arg-type]
        )
        descriptor.validate()
        return descriptor

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AttachmentDescriptor):
            return NotImplemented
        return self.to_payload() == other.to_payload()

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.to_payload().items(), key=lambda kv: kv[0])))

    def __repr__(self) -> str:
        return (
            f"AttachmentDescriptor(kind={self.kind!r}, "
            f"filename={self.filename!r}, size_bytes={self.size_bytes!r}, "
            f"content_ref={self.content_ref!r}, "
            f"unavailable_reason={self.unavailable_reason!r})"
        )


def attachment_descriptor_from_event_payload(
    payload: Mapping[str, Any],
) -> AttachmentDescriptor | None:
    """Return the validated primary attachment descriptor, if any.

    Strictly parses the descriptor under :data:`ATTACHMENT_PAYLOAD_KEY`;
    a present-but-invalid value raises rather than being coerced.
    """
    raw = payload.get(ATTACHMENT_PAYLOAD_KEY) if isinstance(payload, Mapping) else None
    if raw is None:
        return None
    return AttachmentDescriptor.from_payload(raw)


def declared_descriptor_from_event_payload(
    payload: Mapping[str, Any],
) -> AttachmentDescriptor | None:
    """Return the declared (retention-pending) descriptor, if any.

    Core admission uses this to normalize an in-flight decoded event's
    wire-declared descriptor into a retained one inside the atomic
    admission transaction.  Declared payloads must contain only declared
    fields; a payload that already carries retention state (``content_ref``
    or ``unavailable_reason``) is rejected — admission never trusts a
    wire-supplied content reference or reason.
    """
    raw = payload.get(ATTACHMENT_PAYLOAD_KEY) if isinstance(payload, Mapping) else None
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise AttachmentDescriptorError("attachment descriptor must be a mapping")
    strict = AttachmentDescriptor._strict_fields(raw, allowed=_DECLARED_FIELDS)
    descriptor = AttachmentDescriptor(
        kind=str(strict.get("kind", "")),
        filename=strict.get("filename"),  # type: ignore[arg-type]
        mime_type=strict.get("mime_type"),  # type: ignore[arg-type]
        size_bytes=strict.get("size_bytes"),  # type: ignore[arg-type]
        width=strict.get("width"),  # type: ignore[arg-type]
        height=strict.get("height"),  # type: ignore[arg-type]
        duration_ms=strict.get("duration_ms"),  # type: ignore[arg-type]
    )
    descriptor._validate_declared()
    return descriptor


__all__ = [
    "ATTACHMENT_KINDS",
    "ATTACHMENT_PAYLOAD_KEY",
    "ATTACHMENT_UNAVAILABLE_REASONS",
    "CONTENT_REF_PATTERN",
    "AttachmentDescriptor",
    "AttachmentDescriptorError",
    "attachment_descriptor_from_event_payload",
    "declared_descriptor_from_event_payload",
]
