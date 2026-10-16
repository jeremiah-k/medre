"""Context selection is delegated to fake factories, never forged afterward."""

from types import SimpleNamespace

import pytest

from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.runtime.smoke_ingress import make_smoke_event
from tests.helpers.cli import _run_cli_raw


def test_opaque_context_reaches_the_factory_and_canonical_event():
    event = make_smoke_event(
        FakeTransportAdapter("source"), "hello", source_context="room=α"
    )
    assert event.source_adapter == "source"
    assert event.source_channel_id == "room=α"
    assert event.payload["body"] == event.payload["text"] == "hello"


def test_factory_that_ignores_context_is_rejected():
    source = FakeTransportAdapter("source")
    broken = SimpleNamespace(
        adapter_id="source", make_event=lambda **kwargs: source.make_event()
    )
    with pytest.raises(ValueError, match="did not preserve"):
        make_smoke_event(broken, "hello", source_context="requested")


def test_legacy_factories_work_without_opt_in_but_cannot_claim_context_coverage():
    source = FakeTransportAdapter("source")
    legacy = SimpleNamespace(
        adapter_id="source",
        make_event=lambda text, event_kind: source.make_event(text, event_kind),
    )
    assert make_smoke_event(legacy, "hello").payload["text"] == "hello"
    with pytest.raises(TypeError):
        make_smoke_event(legacy, "hello", source_context="requested")


def test_factory_cannot_attribute_an_event_to_another_adapter():
    source = FakeTransportAdapter("other")
    broken = SimpleNamespace(adapter_id="source", make_event=source.make_event)
    with pytest.raises(ValueError, match="adapter identity"):
        make_smoke_event(broken, "hello", source_context="room")


@pytest.mark.parametrize("context", ["", " "])
def test_empty_context_is_rejected(context):
    with pytest.raises(ValueError, match="non-empty"):
        make_smoke_event(
            FakeTransportAdapter("source"), "hello", source_context=context
        )


@pytest.mark.parametrize("mode", ["--run-session", "--drill"])
def test_other_smoke_modes_cannot_silently_ignore_context_selection(mode):
    args = ["smoke", "--source-context", "room"]
    args.extend([mode, "renderer_failure"] if mode == "--drill" else [mode])
    _, stderr, code = _run_cli_raw(*args)
    assert code == 2 and "--source-context is supported only for normal smoke" in stderr
