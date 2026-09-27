"""Closed outbound operation envelope for the Matrix adapter.

Matrix renders no longer describe *what* to send by mutating a wire
content dict with magic keys.  Instead every native render produces a
single closed :class:`MatrixOutboundOperation` — either a ``send_event``
(room message, reaction, edit, thread, reply — anything with wire
content) or a ``redact_event`` (a redaction of a previously sent event).

The operation travels inside the rendering result payload under the
single closed key ``_matrix_operation``:

    {"_matrix_operation": {"kind": "send_event", ...}}

``MatrixAdapter.deliver`` strictly requires a valid envelope, pops it
before transport (nothing under the key ever reaches the homeserver),
and dispatches on ``kind``.  The envelope deliberately carries **no
room identity** — routing stays in ``RenderingResult.target_channel``.

Missing envelopes are permanent errors; malformed ones raise
:class:`MatrixOutboundEnvelopeError` so callers fail closed instead of
guessing intent.
"""

from __future__ import annotations

from typing import Literal, Mapping

import msgspec

from medre.core.events.attachments import CONTENT_REF_PATTERN

#: The single closed payload key under which the operation travels.
MATRIX_OPERATION_KEY = "_matrix_operation"

MatrixOperationKind = Literal["send_event", "redact_event", "send_media"]

#: Closed field set for strict envelope decoding; anything else is rejected.
_KNOWN_OPERATION_FIELDS = frozenset(
    {"kind", "event_type", "content", "redacts_event_id", "reason", "content_ref"}
)

_NEUTRAL_REDACTION_REASON = "Deleted by original author via MEDRE relay"


class MatrixOutboundEnvelopeError(ValueError):
    """Raised when a payload carries a malformed ``_matrix_operation``."""


class MatrixOutboundOperation(msgspec.Struct, frozen=True, kw_only=True):
    """One closed outbound Matrix operation.

    Attributes
    ----------
    kind:
        ``"send_event"`` or ``"redact_event"``.
    event_type:
        Matrix event type for ``send_event`` (``"m.room.message"``,
        ``"m.reaction"``, ...).  Must be ``None`` for ``redact_event``.
    content:
        Wire content dict for ``send_event``.  Must be ``None`` for
        ``redact_event``.  Contains no routing keys.
    redacts_event_id:
        Native event ID to redact; required by ``redact_event`` and
        forbidden for ``send_event``.
    reason:
        Optional redaction reason; only valid for ``redact_event``.
    """

    kind: MatrixOperationKind
    event_type: str | None = None
    content: dict[str, object] | None = None
    redacts_event_id: str | None = None
    reason: str | None = None
    content_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("send_event", "redact_event", "send_media"):
            raise MatrixOutboundEnvelopeError(f"unknown operation kind: {self.kind!r}")
        if self.kind == "send_media":
            self._validate_send_media()
        elif self.kind == "send_event":
            if not isinstance(self.event_type, str) or not self.event_type.strip():
                raise MatrixOutboundEnvelopeError(
                    "send_event requires a non-empty event_type"
                )
            if not isinstance(self.content, dict) or not self.content:
                raise MatrixOutboundEnvelopeError(
                    "send_event requires non-empty content"
                )
            if self.redacts_event_id is not None:
                raise MatrixOutboundEnvelopeError(
                    "send_event must not carry redacts_event_id"
                )
            if self.reason is not None:
                raise MatrixOutboundEnvelopeError(
                    "send_event must not carry a redaction reason"
                )
            if self.content_ref is not None:
                raise MatrixOutboundEnvelopeError(
                    "send_event must not carry content_ref"
                )
        else:
            if self.event_type is not None:
                raise MatrixOutboundEnvelopeError(
                    "redact_event must not carry event_type"
                )
            if self.content is not None:
                raise MatrixOutboundEnvelopeError("redact_event must not carry content")
            if (
                not isinstance(self.redacts_event_id, str)
                or not self.redacts_event_id.strip()
            ):
                raise MatrixOutboundEnvelopeError(
                    "redact_event requires a non-empty redacts_event_id"
                )
            if self.content_ref is not None:
                raise MatrixOutboundEnvelopeError(
                    "redact_event must not carry content_ref"
                )

    def _validate_send_media(self) -> None:
        """Validate the closed ``send_media`` field contract.

        ``content`` is the pure wire template (``msgtype``/``body``/
        ``filename``/``info``/relations/envelope — everything except the
        ``url``/``file`` key the adapter inserts after upload).  It must
        never carry bytes, key material, or a source MXC locator: forwarding
        a source URI as a delivered file is prohibited.
        """
        if self.event_type is not None:
            raise MatrixOutboundEnvelopeError("send_media must not carry event_type")
        if self.redacts_event_id is not None:
            raise MatrixOutboundEnvelopeError(
                "send_media must not carry redacts_event_id"
            )
        if self.reason is not None:
            raise MatrixOutboundEnvelopeError("send_media must not carry a reason")
        if (
            not isinstance(self.content_ref, str)
            or CONTENT_REF_PATTERN.match(self.content_ref) is None
        ):
            raise MatrixOutboundEnvelopeError(
                "send_media requires a local content reference of the form "
                "sha256:<64 lowercase hex>"
            )
        if not isinstance(self.content, dict) or not self.content:
            raise MatrixOutboundEnvelopeError(
                "send_media requires a non-empty wire template"
            )
        msgtype = self.content.get("msgtype")
        if not isinstance(msgtype, str) or msgtype not in (
            "m.image",
            "m.audio",
            "m.video",
            "m.file",
        ):
            raise MatrixOutboundEnvelopeError(
                "send_media template requires a media msgtype "
                "(m.image/m.audio/m.video/m.file)"
            )
        if "url" in self.content or "file" in self.content:
            raise MatrixOutboundEnvelopeError(
                "send_media template must not carry url/file; the adapter "
                "inserts the fresh upload reference after transfer"
            )

    # -- Constructors -----------------------------------------------------

    @classmethod
    def send_event(
        cls,
        event_type: str,
        content: dict[str, object],
    ) -> MatrixOutboundOperation:
        """Build a validated ``send_event`` operation."""
        return cls(kind="send_event", event_type=event_type, content=content)

    @classmethod
    def send_media(
        cls,
        content_ref: str,
        template: dict[str, object],
    ) -> MatrixOutboundOperation:
        """Build a validated ``send_media`` operation.

        *content_ref* names the event's retained local content; *template*
        is the pure wire template (no bytes, no keys, no url/file — the
        adapter resolves the retained bytes through the runtime content
        seam and inserts the fresh destination upload reference).
        """
        return cls(kind="send_media", content_ref=content_ref, content=template)

    @classmethod
    def redact(
        cls,
        redacts_event_id: str,
        reason: str | None = None,
    ) -> MatrixOutboundOperation:
        """Build a validated ``redact_event`` operation.

        The reason defaults to the neutral MEDRE relay wording; callers
        pass ``reason=""`` explicitly to omit the field entirely.
        """
        resolved = _NEUTRAL_REDACTION_REASON if reason is None else reason
        return cls(
            kind="redact_event",
            redacts_event_id=redacts_event_id,
            reason=resolved or None,
        )

    # -- Payload mapping --------------------------------------------------

    def to_payload(self) -> dict[str, object]:
        """Return ``{"_matrix_operation": <json-safe dict>}``."""
        return {MATRIX_OPERATION_KEY: msgspec.to_builtins(self)}

    @staticmethod
    def from_payload(
        payload: Mapping[str, object],
    ) -> MatrixOutboundOperation | None:
        """Strictly decode the envelope from *payload*.

        Returns ``None`` when the key is absent (caller decides whether
        that is acceptable — :meth:`MatrixAdapter.deliver` does not).
        Raises :class:`MatrixOutboundEnvelopeError` when the key is
        present but malformed: unknown fields, wrong types, unknown
        kinds, or per-kind field violations.  Malformed envelopes are
        contract violations and never repaired by guessing.
        """
        if not isinstance(payload, Mapping):
            raise MatrixOutboundEnvelopeError(
                "payload must be a mapping to carry _matrix_operation"
            )
        if MATRIX_OPERATION_KEY not in payload:
            return None
        raw = payload[MATRIX_OPERATION_KEY]
        if isinstance(raw, MatrixOutboundOperation):
            return raw
        if not isinstance(raw, Mapping):
            raise MatrixOutboundEnvelopeError("_matrix_operation must be a mapping")
        unknown = set(raw) - _KNOWN_OPERATION_FIELDS
        if unknown:
            raise MatrixOutboundEnvelopeError(
                f"unknown _matrix_operation fields: {sorted(unknown)}"
            )
        try:
            return msgspec.convert(
                dict(raw),
                MatrixOutboundOperation,
                strict=True,
            )
        except msgspec.ValidationError as exc:
            raise MatrixOutboundEnvelopeError(
                f"invalid _matrix_operation envelope: {exc}"
            ) from exc
