"""Process lifecycle transitions are enforced before their observable events."""

import pytest

from medre.runtime.app import MedreApp, RuntimeState, runtime_transition_allowed


class _Events:
    def __init__(self):
        self.records = []

    def emit(self, kind, detail):
        self.records.append((kind, detail))


def _app(state):
    app = object.__new__(MedreApp)
    app._state = state
    app._event_buffer = _Events()
    return app


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeState.INITIALIZED, RuntimeState.STARTING),
        (RuntimeState.STARTING, RuntimeState.RUNNING),
        (RuntimeState.STARTING, RuntimeState.FAILED),
        (RuntimeState.RUNNING, RuntimeState.STOPPING),
        (RuntimeState.STOPPING, RuntimeState.STOPPED),
        (RuntimeState.STOPPING, RuntimeState.FAILED),
        (RuntimeState.FAILED, RuntimeState.STOPPING),
    ],
)
def test_legal_runtime_transition_emits_exactly_one_event(source, target):
    app = _app(source)
    app._set_state(target)
    assert app.state is target
    assert len(app._event_buffer.records) == 1
    assert app._event_buffer.records[0][1] == {"from": source.value, "to": target.value}


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeState.INITIALIZED, RuntimeState.RUNNING),
        (RuntimeState.STARTING, RuntimeState.STOPPED),
        (RuntimeState.STARTING, RuntimeState.STOPPING),
        (RuntimeState.RUNNING, RuntimeState.STARTING),
        (RuntimeState.FAILED, RuntimeState.RUNNING),
        (RuntimeState.STOPPED, RuntimeState.STARTING),
        (RuntimeState.STOPPED, RuntimeState.FAILED),
    ],
)
def test_illegal_runtime_transition_leaves_state_and_events_unchanged(source, target):
    app = _app(source)
    assert not runtime_transition_allowed(source, target)
    with pytest.raises(RuntimeError, match="Invalid runtime lifecycle transition"):
        app._set_state(target)
    assert app.state is source
    assert not app._event_buffer.records


def test_idempotent_and_malformed_transitions_do_not_emit_events():
    for state in RuntimeState:
        app = _app(state)
        app._set_state(state)
        assert not app._event_buffer.records
    assert not runtime_transition_allowed("stopped", RuntimeState.RUNNING)
    assert not runtime_transition_allowed(RuntimeState.RUNNING, "stopped")
