# medre-test-tier: compatibility
"""Native health transitions update current state while retaining startup evidence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from medre.config.paths import resolve
from medre.core.lifecycle.states import AdapterState
from medre.runtime.app import RuntimeState
from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import _build_app, _config_with_one_fake_adapter


async def recovering_app(tmp_path, monkeypatch):
    """Build a real runtime whose native provider connects after startup settles."""
    app = _build_app(
        _config_with_one_fake_adapter(), resolve(environ={"MEDRE_HOME": str(tmp_path)})
    )
    adapter = next(iter(app.adapters.values()))
    health = adapter.health_check
    start = adapter.start
    state = {"health": "degraded"}

    async def sample():
        """Use the provider's real identity and capabilities with controlled health."""
        return replace(await health(), health=state["health"])

    async def start_with_context(ctx):
        """Retain the injected runtime context for native report assertions."""
        state["context"] = ctx
        await start(ctx)

    monkeypatch.setattr(adapter, "start", start_with_context)
    monkeypatch.setattr(adapter, "health_check", sample)
    await app.start()
    return app, adapter, state, sample


async def test_native_recovery_updates_health_without_manual_refresh(
    tmp_path, monkeypatch
):
    """A connected provider clears degraded current health without rewriting boot facts."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    startup = dict(app._health_state)
    try:
        state["health"] = "healthy"
        assert state["context"].report_health is not None
        state["context"].report_health(await sample())
        assert await wait_until(lambda: app._live_health_state is not None)
        assert app._live_health_state.runtime_health == "healthy"
        assert app.adapter_states[adapter.adapter_id] is AdapterState.READY
        assert app._health_state == startup
        assert app._live_health_state.poll_count == 0
        assert app._live_health_state.source == "adapter_notification"
        state["health"] = "degraded"
        state["context"].report_health(await sample())
        assert await wait_until(
            lambda: app._live_health_state.runtime_health == "degraded"
        )
    finally:
        await app.stop()


async def test_retired_context_cannot_revive_stopped_adapter(tmp_path, monkeypatch):
    """Late SDK reports cannot turn a stopped lifetime healthy."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    report = state["context"].report_health
    info = replace(await sample(), health="healthy")
    await app.stop()
    report(info)
    await asyncio.sleep(0)
    assert app.state is RuntimeState.STOPPED
    assert app.adapter_states[adapter.adapter_id] is AdapterState.STOPPED
    assert app._live_health_state is None


async def test_health_reports_are_bound_to_their_adapter(tmp_path, monkeypatch):
    """A provider cannot publish another adapter's health through its context."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="bound adapter"):
            state["context"].report_health(replace(await sample(), adapter_id="other"))
        with pytest.raises(ValueError, match="settled health"):
            state["context"].report_health(replace(await sample(), health="starting"))
    finally:
        await app.stop()


async def test_health_samples_cannot_borrow_another_adapters_identity(
    tmp_path, monkeypatch
):
    """A misbound provider sample cannot publish another adapter's readiness."""
    app = _build_app(
        _config_with_one_fake_adapter(), resolve(environ={"MEDRE_HOME": str(tmp_path)})
    )
    adapter = next(iter(app.adapters.values()))
    info = replace(await adapter.health_check(), adapter_id="other", health="healthy")

    async def misbound_sample():
        """Model a provider accidentally returning another configured owner's info."""
        return info

    monkeypatch.setattr(adapter, "health_check", misbound_sample)
    try:
        await app.start()
        assert app.boot_summary.runtime_health == "degraded"
        entry = app._observed_adapter_health[adapter.adapter_id]
        assert entry.adapter_id == adapter.adapter_id
        assert entry.health == "degraded"
        snapshot = await app.refresh_live_health()
        assert snapshot.runtime_health == "failed"
        assert snapshot.adapters[adapter.adapter_id].adapter_id == adapter.adapter_id
        assert "bound adapter" in snapshot.adapters[adapter.adapter_id].error
    finally:
        await app.stop()


@pytest.mark.parametrize("probe_fails", [False, True])
async def test_notification_supersedes_a_manual_poll_in_flight(
    tmp_path, monkeypatch, probe_fails
):
    """A slow diagnostic sample cannot overwrite a later native recovery report."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    stale = await sample()

    async def delayed_sample():
        """Retain a degraded sample while the native provider recovers."""
        entered.set()
        await release.wait()
        if probe_fails:
            raise RuntimeError("sample unavailable")
        return stale

    monkeypatch.setattr(adapter, "health_check", delayed_sample)
    polling = asyncio.create_task(app.refresh_live_health())
    try:
        await entered.wait()
        state["context"].report_health(replace(stale, health="healthy"))
        assert await wait_until(lambda: app._live_health_state is not None)
        release.set()
        snapshot = await polling
        assert snapshot.runtime_health == "healthy"
        assert snapshot.adapters[adapter.adapter_id].health == "healthy"
        assert snapshot.poll_count == 1
        assert "failed_adapters" not in list(app.event_buffer)[-1].detail
    finally:
        release.set()
        await polling
        await app.stop()


@pytest.mark.parametrize("probe_number", [1, 2])
@pytest.mark.parametrize("probe_result", ["stale", "error", "timeout"])
async def test_native_recovery_supersedes_startup_probe(
    tmp_path, monkeypatch, probe_number, probe_result
):
    """Both startup probes retain native recovery observed while a sample waits."""
    app = _build_app(
        _config_with_one_fake_adapter(), resolve(environ={"MEDRE_HOME": str(tmp_path)})
    )
    adapter = next(iter(app.adapters.values()))
    health, start = adapter.health_check, adapter.start
    info = replace(await health(), health="healthy")
    state = {"calls": 0, "health": "degraded"}
    results = []
    probe = app._probe_startup_health

    async def retain_context(ctx):
        """Use the actual injected reporter during the owned startup probes."""
        state["context"] = ctx
        await start(ctx)

    async def sample():
        """Report recovery after taking a stale sample, before completing the probe."""
        state["calls"] += 1
        sampled = replace(info, health=state["health"])
        if state["calls"] == probe_number:
            state["health"] = "healthy"
            state["context"].report_health(info)
            await asyncio.sleep(0)
            if probe_result == "error":
                raise RuntimeError("sample unavailable")
            if probe_result == "timeout":
                await asyncio.Event().wait()
        return sampled

    async def record_probe(adapter_id):
        """Retain each classification so a later probe cannot hide an earlier loss."""
        result = await probe(adapter_id)
        results.append(result)
        return result

    monkeypatch.setattr(adapter, "start", retain_context)
    monkeypatch.setattr(adapter, "health_check", sample)
    monkeypatch.setattr(app, "_probe_startup_health", record_probe)
    if probe_result == "timeout":
        monkeypatch.setattr("medre.runtime.app._STARTUP_HEALTH_TIMEOUT_SECONDS", 0.01)
    try:
        await app.start()
        assert results[probe_number - 1] == "healthy"
        assert app.boot_summary.runtime_health == "healthy"
        assert app.adapter_states[adapter.adapter_id] is AdapterState.READY
    finally:
        await app.stop()


async def test_startup_barrier_uses_transition_after_completed_peer_probe(
    tmp_path, monkeypatch
):
    """A fast peer's completed probe cannot override a later loss while a slow peer waits."""
    from tests.helpers.startup_cleanup import _config_with_two_fake_adapters

    app = _build_app(
        _config_with_two_fake_adapters(), resolve(environ={"MEDRE_HOME": str(tmp_path)})
    )
    fast, slow = app.adapters["alpha"], app.adapters["beta"]
    start, sample = fast.start, slow.health_check
    info = await fast.health_check()
    retained = {}
    sampled = asyncio.Event()
    probe = app._probe_startup_health

    async def retain_context(ctx):
        """Keep the fast provider's actual runtime report sink."""
        retained["report"] = ctx.report_health
        await start(ctx)

    async def observe_probe(adapter_id):
        """Signal when the fast provider's barrier sample has completed."""
        result = await probe(adapter_id)
        if adapter_id == "alpha" and adapter_id in app.started_adapter_ids:
            sampled.set()
        return result

    async def slow_sample():
        """Lose the fast connection after its probe, while the barrier is waiting."""
        if "beta" in app.started_adapter_ids:
            await sampled.wait()
            retained["report"](replace(info, health="degraded"))
            await asyncio.sleep(0)
        return await sample()

    monkeypatch.setattr(fast, "start", retain_context)
    monkeypatch.setattr(slow, "health_check", slow_sample)
    monkeypatch.setattr(app, "_probe_startup_health", observe_probe)
    try:
        await app.start()
        assert app.adapter_states["alpha"] is AdapterState.DEGRADED
        assert app.boot_summary.runtime_health == "degraded"
        assert app.live_health.adapters["alpha"].health == "degraded"
    finally:
        await app.stop()


async def test_report_from_sdk_thread_reaches_runtime_owner(tmp_path, monkeypatch):
    """Native SDK threads can report recovery without mutating runtime state themselves."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    try:
        await asyncio.to_thread(
            state["context"].report_health, replace(await sample(), health="healthy")
        )
        assert await wait_until(lambda: app._live_health_state is not None)
        assert app._live_health_state.runtime_health == "healthy"
    finally:
        await app.stop()


async def test_native_failed_health_does_not_make_started_owner_terminal(
    tmp_path, monkeypatch
):
    """A transport's failed health can recover without reviving a failed startup."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    try:
        info = await sample()
        state["context"].report_health(replace(info, health="failed"))
        assert await wait_until(lambda: app._live_health_state is not None)
        assert app._live_health_state.runtime_health == "failed"
        state["context"].report_health(replace(info, health="healthy"))
        assert await wait_until(
            lambda: app._live_health_state.runtime_health == "healthy"
        )
        assert app.adapter_states[adapter.adapter_id] is AdapterState.READY
    finally:
        await app.stop()


def test_started_adapter_owners_are_ready_only_with_healthy_current_state():
    """Startup output distinguishes a running recovery owner from a usable adapter."""
    from medre.runtime.summaries import startup_summary

    owners = [("mc", "meshcore", True, 0.1, None)]
    assert "Runtime running (1 adapter(s) running)" in startup_summary(
        owners, ready=False
    )
    assert "Runtime ready (1 adapter(s) running)" in startup_summary(owners, ready=True)


async def test_public_current_health_cannot_mutate_runtime_observations(
    tmp_path, monkeypatch
):
    """Published diagnostic mappings cannot rewrite runtime-owned current health."""
    import json
    from pathlib import Path

    import jsonschema

    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    try:
        state["context"].report_health(replace(await sample(), health="healthy"))
        assert await wait_until(lambda: app.live_health is not None)
        snapshot = app.live_health
        schema = json.loads(
            (
                Path(__file__).parents[1] / "docs/schemas/live-health.schema.json"
            ).read_text()
        )
        jsonschema.Draft202012Validator(schema).validate(snapshot.to_dict())
        snapshot.adapters.clear()
        snapshot.adapter_summary["healthy"] = 0
        assert app.live_health.adapter_summary["healthy"] == 1
        assert adapter.adapter_id in app.live_health.adapters
    finally:
        await app.stop()


async def test_recovery_during_activation_is_published_after_running(
    tmp_path, monkeypatch
):
    """Recovery between boot classification and activation cannot lose its notification."""
    from datetime import datetime, timezone
    from time import monotonic

    app = _build_app(
        _config_with_one_fake_adapter(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    adapter = next(iter(app.adapters.values()))
    info = await adapter.health_check()

    async def degraded():
        """Keep the historical startup classification degraded."""
        return replace(info, health="degraded")

    transition = app._set_state

    def recover_at_activation(target):
        """Place a completed native report at the final startup handoff."""
        if target is RuntimeState.RUNNING:
            app._apply_health_report(
                adapter.adapter_id,
                replace(info, health="healthy"),
                app._native_health_epoch,
                monotonic(),
                datetime.now(timezone.utc).isoformat(),
            )
        transition(target)

    monkeypatch.setattr(adapter, "health_check", degraded)
    monkeypatch.setattr(app, "_set_state", recover_at_activation)
    try:
        await app.start()
        assert app._health_state["runtime_health"] == "degraded"
        assert app.live_health.runtime_health == "healthy"
        assert not app._pending_health_reports
    finally:
        await app.stop()


async def test_failed_start_context_cannot_report_a_healthy_running_owner(
    tmp_path, monkeypatch
):
    """A native health report cannot bypass failed-start cleanup and ownership."""
    from tests.helpers.startup_cleanup import _config_with_two_fake_adapters

    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    failed = app.adapters["alpha"]
    info = await failed.health_check()
    retained = {}

    async def failed_start(ctx):
        """Retain the report sink of a startup that never acquired readiness."""
        retained["report"] = ctx.report_health
        ctx.report_health(replace(info, health="healthy"))
        await asyncio.sleep(0)
        raise RuntimeError("native setup failed")

    monkeypatch.setattr(failed, "start", failed_start)
    try:
        await app.start()
        retained["report"](replace(info, health="healthy"))
        await asyncio.sleep(0)
        assert app.adapter_states["alpha"] is AdapterState.FAILED
        assert app.live_health is None
        assert "alpha" not in app._observed_adapter_health
    finally:
        await app.stop()


async def _two_provider_app(tmp_path, monkeypatch):
    """Start a running two-provider runtime and retain both report sinks."""
    from tests.helpers.startup_cleanup import _config_with_two_fake_adapters

    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    alpha, beta = app.adapters["alpha"], app.adapters["beta"]
    infos = {
        "alpha": await alpha.health_check(),
        "beta": await beta.health_check(),
    }
    sinks: dict[str, Any] = {}

    def capture(adapter_id: str, original: Any):
        async def start_with_context(ctx):
            """Retain this provider's runtime report sink."""
            sinks[adapter_id] = ctx.report_health
            await original(ctx)

        return start_with_context

    monkeypatch.setattr(alpha, "start", capture("alpha", alpha.start))
    monkeypatch.setattr(beta, "start", capture("beta", beta.start))
    await app.start()
    return app, sinks, infos


def _health_events(app):
    from medre.runtime.events import RuntimeEventType

    return [
        event
        for event in list(app.event_buffer)
        if event.event_type is RuntimeEventType.HEALTH_REFRESHED
    ]


async def test_concurrent_provider_transitions_keep_their_own_observations(
    tmp_path, monkeypatch
):
    """Simultaneous opposite transitions never clobber each other's evidence."""
    app, sinks, infos = await _two_provider_app(tmp_path, monkeypatch)
    try:
        # Both providers change concurrently: one fails, one degrades.
        sinks["alpha"](replace(infos["alpha"], health="failed"))
        sinks["beta"](replace(infos["beta"], health="degraded"))
        assert await wait_until(lambda: app.live_health is not None)
        await asyncio.sleep(0)
        snapshot = app.live_health
        assert snapshot.adapters["alpha"].health == "failed"
        assert snapshot.adapters["beta"].health == "degraded"
        # No operational provider remains; partial states classify degraded.
        assert snapshot.runtime_health == "degraded"
        assert snapshot.poll_count == 0
        changed = {
            adapter_id
            for event in _health_events(app)
            for adapter_id in event.detail["changed_adapters"]
        }
        assert changed == {"alpha", "beta"}

        # A second provider failing reclassifies the aggregate without
        # discarding the first provider's retained failure observation.
        sinks["beta"](replace(infos["beta"], health="failed"))
        assert await wait_until(lambda: app.live_health.runtime_health == "failed")
        beta_failure = app.live_health.adapters["beta"]

        # A later recovery refreshes only its own entry: the peer provider
        # keeps the observation timestamps of its own completed transition.
        sinks["alpha"](replace(infos["alpha"], health="healthy"))
        assert await wait_until(lambda: app.live_health.runtime_health == "degraded")
        refreshed = app.live_health.adapters["beta"]
        assert refreshed.health == "failed"
        assert (
            refreshed.poll_timestamp_monotonic == beta_failure.poll_timestamp_monotonic
        )
        assert refreshed.poll_timestamp_wall == beta_failure.poll_timestamp_wall
        assert app.adapter_states["alpha"] is AdapterState.READY
    finally:
        await app.stop()


async def test_rapid_flapping_retains_the_latest_report_without_probing(
    tmp_path, monkeypatch
):
    """Back-to-back transitions publish in order and never start a probe cycle."""
    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    probe_calls = {"count": 0}
    inner = adapter.health_check

    async def counting_sample():
        """Count every completed probe so notifications can be compared against it."""
        probe_calls["count"] += 1
        return await inner()

    monkeypatch.setattr(adapter, "health_check", counting_sample)
    try:
        base = await counting_sample()
        probe_calls["count"] = 0
        # The first report repeats the retained startup observation and must
        # stay silent; only genuine transitions publish.
        sequence = ["degraded", "healthy", "degraded", "failed", "healthy"]
        report = state["context"].report_health
        for health in sequence:
            report(replace(base, health=health))
        assert await wait_until(
            lambda: app.live_health is not None
            and app.live_health.adapters[adapter.adapter_id].health == "healthy"
        )
        await asyncio.sleep(0)
        snapshot = app.live_health
        assert snapshot.adapters[adapter.adapter_id].health == "healthy"
        assert snapshot.runtime_health == "healthy"
        assert snapshot.poll_count == 0
        assert snapshot.source == "adapter_notification"
        assert [event.detail["runtime_health"] for event in _health_events(app)] == [
            "healthy",
            "degraded",
            "failed",
            "healthy",
        ]
        assert app.adapter_states[adapter.adapter_id] is AdapterState.READY
        # Reports carry observations; they never issue probes of their own.
        assert probe_calls["count"] == 0
    finally:
        await app.stop()


async def test_native_transition_keeps_every_surface_consistent(tmp_path, monkeypatch):
    """Current state, diagnostic snapshot, live health, and boot history agree."""
    from medre.runtime.snapshot import build_runtime_snapshot

    app, adapter, state, sample = await recovering_app(tmp_path, monkeypatch)
    accepted_sends = {"called": False}
    claimed = {"called": False}

    def record_acceptance():
        """Record any shutdown of delivery acceptance during health traffic."""
        accepted_sends["called"] = True

    async def record_claim(*args, **kwargs):
        """Record any claim cycle triggered by health traffic."""
        claimed["called"] = True
        raise AssertionError("health reports must not trigger claim cycles")

    capacity = app._capacity_controller
    monkeypatch.setattr(capacity, "stop_accepting", record_acceptance)
    monkeypatch.setattr(app.storage, "claim_due_outbox_items", record_claim)
    # The initial-dispatch poller claims on its own sub-second cadence and is
    # unrelated to health traffic; neutralize its dispatch cycle so the
    # storage-level tripwire above only catches a health-driven claim.
    if app._retry_worker is not None:

        async def idle_dispatch_cycle(*args, **kwargs):
            return None

        monkeypatch.setattr(app._retry_worker, "_process_due", idle_dispatch_cycle)
    tasks_before = sorted(task.get_name() for task in asyncio.all_tasks())
    try:
        report = state["context"].report_health
        info = await sample()
        baseline = build_runtime_snapshot(app)
        assert baseline["health"]["live_health"] is None
        # The first notification repeats the boot observation (silent), the
        # recovery transitions current health while everything else holds.
        report(info)
        await asyncio.sleep(0)
        assert app.live_health is None
        report(replace(info, health="healthy"))
        assert await wait_until(lambda: app.live_health is not None)
        await asyncio.sleep(0)
        assert app.state is RuntimeState.RUNNING
        assert app.live_health.runtime_health == "healthy"
        assert app.live_health.poll_count == 0
        snapshot = build_runtime_snapshot(app)
        assert snapshot["lifecycle"]["runtime_state"] == "running"
        assert snapshot["health"]["scope"] == "live"
        assert snapshot["health"]["live_refresh"] is True
        assert snapshot["health"]["live_health"]["runtime_health"] == "healthy"
        assert snapshot["health"]["live_health"]["source"] == "adapter_notification"
        # The frozen startup record is byte-identical across the transition.
        assert snapshot["startup"] == baseline["startup"]
        # Native traffic changed no delivery authority and spawned no task.
        assert not accepted_sends["called"]
        assert not claimed["called"]
        assert sorted(task.get_name() for task in asyncio.all_tasks()) == tasks_before
    finally:
        await app.stop()
