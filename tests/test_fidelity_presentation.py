"""Presentation qualification rejects self-consistent but incorrect evidence."""

import io
import json
from copy import deepcopy
from html import escape
from unittest.mock import AsyncMock

import pytest

from scripts.adapter_campaign import worker
from scripts.adapter_campaign.presentation import CASES, example, verify


def witness(name):
    marker = "MF-0123456789ab-" + name + " "
    fixture = example(name, marker)
    case = {"name": name, "marker": marker, "text": fixture["text"]}
    payload = {"body": fixture["text"]}
    if fixture["spans"]:
        payload["formatting"] = {"version": 1, "spans": deepcopy(fixture["spans"])}
    sent = {"native_details": {"unstyled": fixture["command"].get("unstyled", False)}}
    if "html" in fixture["command"]:
        sent["formatted_body"] = fixture["command"]["html"]
    prefix = "[Peer]: "
    target = {
        "text": prefix + fixture["target_text"],
        "native_details": {"unstyled": fixture["target_unstyled"]},
    }
    style = fixture["target_style"]
    target["formatted_body"] = (
        escape(prefix + marker)
        + (f"<{style}>" if style else "")
        + escape(case["text"][len(marker) :])
        + (f"</{style}>" if style else "")
    )
    return case, payload, sent, target, prefix


@pytest.mark.parametrize("name", [row[0] for row in CASES])
def test_fixed_native_presentation_fixtures_qualify(name):
    verify(*witness(name))


@pytest.mark.parametrize("name", [row[0] for row in CASES])
def test_canonical_metadata_cannot_define_its_own_expectations(name):
    args = list(witness(name))
    if args[1].get("formatting"):
        args[1]["formatting"]["spans"][0]["start"] = 0
    else:
        args[1]["formatting"] = {"version": 1, "spans": []}
    with pytest.raises(AssertionError, match="Canonical presentation"):
        verify(*args)


@pytest.mark.parametrize("name", ["matrix_strong", "matrix_link", "matrix_unsafe"])
def test_missing_or_inverted_native_unstyled_control_cannot_pass(name):
    args = list(witness(name))
    args[3]["native_details"]["unstyled"] = not args[3]["native_details"]["unstyled"]
    with pytest.raises(AssertionError, match="unstyled"):
        verify(*args)


@pytest.mark.parametrize(
    "change",
    ["prefix", "missing_style", "wrong_style", "text", "source_control", "active_html"],
)
def test_native_html_cannot_hide_source_style_or_attribution_errors(change):
    args = list(witness("xmpp_strong"))
    if change == "prefix":
        args[3]["formatted_body"] = "<strong>" + escape(args[3]["text"]) + "</strong>"
    elif change == "missing_style":
        args[3]["formatted_body"] = escape(args[3]["text"])
    elif change == "wrong_style":
        args[3]["formatted_body"] = args[3]["formatted_body"].replace("strong", "em")
    elif change == "text":
        args[3]["formatted_body"] += "extra"
    elif change == "active_html":
        args[3]["formatted_body"] += '<img src="https://evil.example">'
    else:
        args[2]["native_details"]["unstyled"] = True
    with pytest.raises(AssertionError):
        verify(*args)


@pytest.mark.parametrize(
    "markup",
    [
        "<!-- unobserved extra content -->",
        "<?ignored processing?>",
        "</unexpected>",
        "</p>",
        "</br>",
        "<![CDATA[hidden]]>",
        "<strong/>",
        "<!DOCTYPE foo>",
    ],
)
def test_native_witness_rejects_unrecognized_html_without_visible_text(markup):
    args = list(witness("xmpp_strong"))
    args[3]["formatted_body"] += markup
    with pytest.raises(AssertionError, match="Unexpected native HTML"):
        verify(*args)


async def test_native_peer_dispatches_presentation_without_importing_providers(
    monkeypatch,
):
    styled = AsyncMock(return_value="$event")
    commands = [
        {
            "op": "styled",
            "id": "command",
            "text": "body",
            "presentation": {"html": "<b>body</b>"},
        },
        {"op": "stop"},
    ]
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        io.StringIO("\n".join(json.dumps(c) for c in commands) + "\n"),
    )
    evidence = []
    monkeypatch.setattr(
        worker, "emit", lambda kind, **data: evidence.append((kind, data))
    )
    await worker.commands(AsyncMock(), styled=styled)
    styled.assert_awaited_once_with("body", {"html": "<b>body</b>"})
    assert evidence == [("sent", {"command": "command", "native_id": "$event"})]


def test_native_witness_tracks_balanced_paragraphs_and_void_line_breaks():
    from scripts.adapter_campaign.presentation import _HTMLWitness

    parser = _HTMLWitness()
    parser.feed("<p>one<br/>two</p>")
    parser.close()
    assert parser.text == "one\ntwo"
    assert not parser.stack and not parser.spans
