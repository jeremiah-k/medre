"""Fixed native presentation oracles, independent of provider conversions."""

from html import escape
from html.parser import HTMLParser

CASES = (
    ("matrix_strong", "matrix", None),
    ("matrix_unsafe", "matrix", None),
    ("matrix_link", "matrix", None),
    ("xmpp_strong", "xmpp", None),
    ("xmpp_unstyled", "xmpp", None),
    ("xmpp_code", "xmpp", None),
)


def example(name: str, marker: str) -> dict | None:
    """Derive expectations only from the named fixture and its unique nonce."""
    if name not in {row[0] for row in CASES}:
        return None
    suffix = {
        "matrix_strong": "café 🦉",
        "matrix_unsafe": "link",
        "matrix_link": "link",
        "xmpp_strong": "*café 🦉*",
        "xmpp_unstyled": "*literal*",
        "xmpp_code": "`*literal*`",
    }[name]
    text = marker + suffix
    start, end = len(marker), len(text)
    spans = []
    command = {}
    target_text = text
    target_style = None
    if name == "matrix_strong":
        command["html"] = escape(marker) + "<strong>café 🦉</strong>"
        spans = [{"start": start, "end": end, "style": "strong"}]
        target_text = marker + "*café 🦉*"
    elif name == "matrix_unsafe":
        command["html"] = escape(marker) + '<a href="javascript:alert(1)">link</a>'
    elif name == "matrix_link":
        command["html"] = escape(marker) + '<a href="https://例え.test/文">link</a>'
        spans = [
            {
                "start": start,
                "end": end,
                "style": "link",
                "href": "https://例え.test/文",
            }
        ]
    elif name == "xmpp_unstyled":
        command["unstyled"] = True
    else:
        target_style = "strong" if name == "xmpp_strong" else "code"
        spans = [{"start": start, "end": end, "style": target_style}]
    return {
        "text": text,
        "command": command,
        "spans": spans,
        "target_text": target_text,
        "target_style": target_style,
        "target_unstyled": name in {"matrix_unsafe", "matrix_link"},
    }


class _HTMLWitness(HTMLParser):
    """Record semantic inline ranges from the native witness, never the outbox."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.stack = []
        self.spans = []

    def handle_data(self, data):
        self.text += data

    def handle_starttag(self, tag, attrs):
        assert tag in {
            "p",
            "br",
            "strong",
            "b",
            "code",
            "em",
            "i",
            "del",
            "s",
            "a",
        }, "Unexpected native HTML element"
        assert not attrs, "Unexpected native HTML attributes"
        if tag == "br":
            self.text += "\n"
        if tag in {"strong", "b", "code", "em", "i", "del", "s", "a"}:
            self.stack.append((tag, len(self.text)))

    def handle_endtag(self, tag):
        if tag in {"strong", "b", "code", "em", "i", "del", "s", "a"}:
            assert self.stack and self.stack[-1][0] == tag, "Unbalanced native HTML"
            _, start = self.stack.pop()
            self.spans.append((start, len(self.text), "strong" if tag == "b" else tag))


def verify(case: dict, payload: dict, sent: dict, delivered: dict, prefix: str) -> None:
    """Reject canonical or native presentation inconsistent with fixed fixtures."""
    expected = example(case["name"], case["marker"])
    if expected is None:
        return
    assert case["text"] == expected["text"], "Presentation fixture text differs"
    formatting = payload.get("formatting")
    assert formatting == (
        {"version": 1, "spans": expected["spans"]} if expected["spans"] else None
    ), "Canonical presentation differs"
    if case["name"].startswith("matrix_"):
        assert (
            sent.get("formatted_body") == expected["command"]["html"]
        ), "Source HTML fixture differs"
        assert (
            delivered["native_details"].get("unstyled") is expected["target_unstyled"]
        ), "Native unstyled control differs"
    else:
        assert sent["native_details"].get("unstyled") is expected["command"].get(
            "unstyled", False
        ), "Source unstyled control differs"
        witness = _HTMLWitness()
        witness.feed(delivered["formatted_body"])
        witness.close()
        assert (
            not witness.stack and witness.text == delivered["text"]
        ), "Native HTML text differs"
        spans = []
        if expected["target_style"]:
            spans = [
                (
                    len(prefix) + len(case["marker"]),
                    len(prefix) + len(case["text"]),
                    expected["target_style"],
                )
            ]
        assert witness.spans == spans, "Native HTML style or attribution scope differs"
