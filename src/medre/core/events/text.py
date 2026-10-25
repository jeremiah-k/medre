"""Bounded, protocol-neutral presentation spans over authoritative plain text.

Spans decorate Unicode code-point ranges; they never carry HTML, callbacks,
identity, or delivery authority. Protocol codecs and renderers own conversion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

TextStyle = Literal["strong", "emphasis", "strike", "code", "pre", "quote", "link"]
TEXT_STYLES: tuple[TextStyle, ...] = (
    "quote",
    "strong",
    "emphasis",
    "strike",
    "link",
    "pre",
    "code",
)
MAX_TEXT_SPANS = 256
MAX_TEXT_STYLE_DEPTH = 16
MAX_STYLED_TEXT_LENGTH = 65536
MAX_LINK_LENGTH = 2048


def _safe_link(value: str) -> bool:
    if (
        not value
        or len(value) > MAX_LINK_LENGTH
        or any(
            c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 or c in '\\"<>'
            for c in value
        )
    ):
        return False
    try:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"}:
            return (
                bool(parts.hostname)
                and parts.username is None
                and parts.password is None
                and (parts.port is None or parts.port > 0)
            )
        return parts.scheme == "mailto" and bool(parts.path) and not parts.netloc
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class TextSpan:
    """Decorate [start, end) code points; only links may carry an href.

    Invalid types, bounds, styles, or link targets raise ValueError. Body-relative
    bounds and nesting are validated by StyledText, which owns the actual body.
    """

    start: int
    end: int
    style: TextStyle
    href: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or not 0 <= self.start < self.end
        ):
            raise ValueError("Text span requires a nonempty integer range")
        if not isinstance(self.style, str) or self.style not in TEXT_STYLES:
            raise ValueError("Unknown text style")
        if self.style == "link":
            if not isinstance(self.href, str) or not _safe_link(self.href):
                raise ValueError("Text link requires an allowed absolute URI")
        elif self.href is not None:
            raise ValueError("Only text links may carry an href")


def select_message_body(payload: Mapping[str, object], *, strict: bool = True) -> str:
    """Select one canonical text body for every transport renderer.

    Styled messages bind offsets to ``body`` and must not use a stale ``text``
    alias. Unstyled legacy messages retain ``text`` precedence. Strict native
    text adapters reject non-string bodies instead of inventing wire content.
    The permissive rendering helper retains historical string coercion.
    """
    body = payload.get("body")
    # A malformed explicit body cannot borrow validity from a compatibility
    # alias. It may otherwise acquire spans belonging to different text.
    if strict and "body" in payload and not isinstance(body, str):
        raise ValueError("Message body must be text")
    selected: object
    if "formatting" in payload and isinstance(body, str):
        selected = body
    else:
        selected = payload.get("text", payload.get("body", ""))
    if not isinstance(selected, str):
        if strict:
            raise ValueError("Message body must be text")
        return str(selected)
    return selected


@dataclass(frozen=True, slots=True)
class StyledText:
    """Immutable plain body and canonical ordered presentation spans.

    Construction validates all spans and raises ValueError on invalid metadata.
    from_payload drops invalid optional formatting as a whole and retains the
    original body; it raises only when the plain body is not text. No parser,
    renderer, network fetch, or transport SDK executes during construction.
    """

    body: str
    spans: tuple[TextSpan, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.body, str):
            raise ValueError("Styled text requires a plain string body")
        if (
            not isinstance(self.spans, tuple)
            or len(self.spans) > MAX_TEXT_SPANS
            or any(not isinstance(span, TextSpan) for span in self.spans)
        ):
            raise ValueError("Text spans require a bounded immutable tuple")
        if self.spans and len(self.body) > MAX_STYLED_TEXT_LENGTH:
            raise ValueError("Styled body exceeds the presentation limit")
        ordered = tuple(
            sorted(
                self.spans,
                key=lambda span: (
                    span.start,
                    -span.end,
                    TEXT_STYLES.index(span.style),
                    span.href or "",
                ),
            )
        )
        if len(set(ordered)) != len(ordered):
            raise ValueError("Duplicate text spans")
        stack: list[TextSpan] = []
        for span in ordered:
            if span.end > len(self.body):
                raise ValueError("Text span exceeds the plain body")
            if span.style in {"pre", "quote"} and (
                (span.start != 0 and self.body[span.start - 1] != "\n")
                or (
                    span.end != len(self.body)
                    and self.body[span.end - 1] != "\n"
                    and self.body[span.end] != "\n"
                )
            ):
                raise ValueError("Block text style requires complete lines")
            while stack and stack[-1].end <= span.start:
                stack.pop()
            if stack and span.end > stack[-1].end:
                raise ValueError("Text spans must be nested or disjoint")
            if any(parent.style in {"code", "pre"} for parent in stack):
                raise ValueError("Code text cannot contain child styles")
            if span.style == "link" and any(parent.style == "link" for parent in stack):
                raise ValueError("Text links cannot be nested")
            stack.append(span)
            if len(stack) > MAX_TEXT_STYLE_DEPTH:
                raise ValueError("Text style nesting exceeds the limit")
        object.__setattr__(self, "spans", ordered)

    def to_payload(self) -> dict[str, object]:
        """Return a fresh canonical body/formatting mapping for event admission."""
        result: dict[str, object] = {"body": self.body}
        if self.spans:
            rows = []
            for span in self.spans:
                row: dict[str, object] = {
                    "start": span.start,
                    "end": span.end,
                    "style": span.style,
                }
                if span.href is not None:
                    row["href"] = span.href
                rows.append(row)
            result["formatting"] = {"version": 1, "spans": rows}
        return result

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> StyledText:
        """Read optional v1 formatting, retaining plain text on invalid metadata.

        Unstyled payloads retain legacy text-first authority; styled payloads
        bind offsets to the body. Missing values resolve to an empty string.
        Unknown versions/fields, malformed ranges, and excessive nesting discard
        the formatting as a whole. Never mutate the supplied payload.
        """
        body = select_message_body(payload)
        value = payload.get("formatting")
        # Span offsets are meaningful only for an explicitly authored body.
        # Do not reinterpret spans against a compatibility-only text alias.
        if "body" not in payload:
            return cls(body)
        if (
            not isinstance(value, Mapping)
            or len(value) != 2
            or set(value) != {"version", "spans"}
            or type(value.get("version")) is not int
            or value["version"] != 1
        ):
            return cls(body)
        rows = value["spans"]
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_TEXT_SPANS:
            return cls(body)
        spans = []
        try:
            for row in rows:
                if (
                    not isinstance(row, Mapping)
                    or len(row) not in {3, 4}
                    or not {"start", "end", "style"}
                    <= set(row)
                    <= {"start", "end", "style", "href"}
                    or ("href" in row and row["style"] != "link")
                ):
                    return cls(body)
                spans.append(
                    TextSpan(
                        row["start"],
                        row["end"],
                        cast(TextStyle, row["style"]),
                        row.get("href"),
                    )
                )
            return cls(body, tuple(spans))
        except ValueError:
            return cls(body)
