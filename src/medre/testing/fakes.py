"""Conformance checks for the minimal fake construction and smoke seams."""

from inspect import signature

from medre.adapter_spec import AdapterSpec
from medre.core.contracts.adapter import AdapterContract
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind


def assert_fake_contract(spec: AdapterSpec) -> None:
    """Construct a fake using core's keyword call and check optional ingress.

    Providers may accept additional constructor arguments or event defaults.
    Core relies only on the explicit keyword calls checked here. The fake must
    be constructible without native SDKs, credentials, network, or hardware.
    """
    assert spec.fake_adapter is not None, "provider does not declare a fake"
    factory = spec.fake_adapter.load()
    signature(factory).bind(adapter_id="contract_probe")
    adapter = factory(adapter_id="contract_probe")
    assert isinstance(adapter, AdapterContract)
    assert adapter.adapter_id == "contract_probe"
    make_event = getattr(adapter, "make_event", None)
    if make_event is None:
        return
    assert callable(make_event)
    kwargs = {"text": "contract probe", "event_kind": EventKind.MESSAGE_TEXT}
    signature(make_event).bind(**kwargs)
    event = make_event(**kwargs)
    assert isinstance(event, CanonicalEvent)
    assert event.source_adapter == adapter.adapter_id
    assert event.event_kind == EventKind.MESSAGE_TEXT
