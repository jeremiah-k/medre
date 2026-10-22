"""Transient actor aggregation never invents persistence or native delivery."""

from dataclasses import FrozenInstanceError

import pytest

from medre.core.contracts.typing import TypingActivity, TypingTarget, TypingUpdate
from medre.core.supervision.typing import TypingLeasePool


class Clock:
    """A deterministic monotonic time source requiring no scheduled sleeps."""

    def __init__(self):
        """Start at a nonzero instant, independent of wall-clock time."""
        self.now = 100.0

    def __call__(self):
        """Return the test-controlled instant."""
        return self.now


TARGET = TypingTarget("matrix", "!room:example.org")
OTHER = TypingTarget("xmpp", "room@conference.example.org")


def notice(sender="alice", typing=True, *, channel="source", seconds=30):
    """Construct one native actor's bounded activity claim."""
    return TypingActivity(channel, sender, typing, seconds)


@pytest.mark.parametrize("seconds", [0, -1, 31, float("nan"), float("inf"), True, "1"])
def test_typing_activity_refuses_unbounded_or_ambiguous_lifetimes(seconds):
    """Invalid native lifetimes cannot allocate a persistent composing lease."""
    with pytest.raises(ValueError):
        notice(seconds=seconds)


@pytest.mark.parametrize("value", ["", "  ", "bad\nroom", "a" * 4097])
def test_typing_identifiers_are_bounded_opaque_values(value):
    """Native identifier syntax remains adapter-owned, but storage is bounded."""
    with pytest.raises(ValueError):
        TypingTarget("matrix", value)
    with pytest.raises(ValueError):
        notice(sender=value)


def test_typing_contracts_are_immutable_and_updates_do_not_puppet_source_actors():
    """Destination updates carry relay activity without a forged sender identity."""
    update = TypingUpdate(TARGET, 5)
    assert update.typing and not TypingUpdate(TARGET, 0).typing
    assert not hasattr(update, "sender_id") and not hasattr(update, "event_id")
    with pytest.raises(FrozenInstanceError):
        update.timeout_seconds = 0
    with pytest.raises(ValueError):
        TypingActivity("room", "sender", 1)


def test_two_actors_share_one_indicator_until_the_last_actor_stops():
    """A sender stop releases its own lease without clearing another sender."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    assert pool.observe("radio", notice("alice"), (TARGET,))
    assert pool.poll() == (TypingUpdate(TARGET, 30),)
    assert pool.observe("radio", notice("bob"), (TARGET,))
    assert pool.poll() == ()
    assert pool.observe("radio", notice("alice", False), ())
    assert pool.poll() == ()
    assert pool.observe("radio", notice("bob", False), ())
    assert pool.poll() == (TypingUpdate(TARGET, 0),)
    assert pool.poll() == () and pool.next_deadline is None


def test_actor_identity_is_scoped_to_source_adapter_and_conversation():
    """Equal native sender strings in other source scopes cannot cancel a lease."""
    pool = TypingLeasePool()
    pool.observe("source_a", notice(), (TARGET,))
    pool.poll()
    pool.observe("source_b", notice(typing=False), ())
    pool.observe("source_a", notice(typing=False, channel="elsewhere"), ())
    assert pool.reset() == (TypingUpdate(TARGET, 0),)


def test_refresh_coalesces_repeated_notifications_and_expires_without_new_input():
    """The lease clock, not incoming traffic, owns indicator refresh and expiry."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice(), (TARGET,))
    pool.poll()
    assert pool.next_deadline == 105
    clock.now = 101
    pool.observe("radio", notice(), (TARGET,))
    assert pool.poll() == ()
    clock.now = 105
    assert pool.poll() == (TypingUpdate(TARGET, 26),)
    clock.now = 131
    assert pool.poll() == (TypingUpdate(TARGET, 0),)
    assert pool.next_deadline is None


def test_short_leases_refresh_before_their_native_timeout():
    """A short receiver lifetime cannot wait for the longer configured cadence."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice(seconds=2), (TARGET,))
    assert pool.poll() == (TypingUpdate(TARGET, 2),)
    assert pool.next_deadline == 101
    clock.now = 101
    pool.observe("radio", notice(seconds=2), (TARGET,))
    assert pool.poll() == (TypingUpdate(TARGET, 2),)
    assert pool.next_deadline == 102


def test_longer_actor_stop_immediately_shortens_the_aggregate_lifetime():
    """A shorter surviving lease cannot leave the receiver with a stale timeout."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice("alice", seconds=30), (TARGET,))
    pool.observe("radio", notice("bob", seconds=3), (TARGET,))
    pool.poll()
    clock.now = 101
    pool.observe("radio", notice("alice", False), ())
    assert pool.poll() == (TypingUpdate(TARGET, 2),)
    clock.now = 103
    assert pool.poll() == (TypingUpdate(TARGET, 0),)


def test_capacity_refusal_preserves_existing_lease_and_does_not_block_stops():
    """Overload drops incoming activity without evicting or extending other users."""
    clock = Clock()
    pool = TypingLeasePool(max_sources=1, max_targets=1, clock=clock)
    pool.observe("radio", notice(), (TARGET,))
    pool.poll()
    clock.now = 101
    assert not pool.observe("radio", notice("bob"), (TARGET,))
    assert not pool.observe("radio", notice(), (TARGET, OTHER))
    assert pool.next_deadline == 105
    assert pool.observe("radio", notice(typing=False), ())
    assert pool.poll() == (TypingUpdate(TARGET, 0),)
    assert pool.observe("radio", notice("bob"), (OTHER,))
    assert pool.poll() == (TypingUpdate(OTHER, 30),)


def test_expired_source_capacity_is_reusable_without_an_external_poll():
    """Silent expired actors cannot starve fresh traffic at the capacity bound."""
    clock = Clock()
    pool = TypingLeasePool(max_sources=1, max_targets=1, clock=clock)
    pool.observe("radio", notice(seconds=1), (TARGET,))
    pool.poll()
    clock.now = 101
    assert pool.observe("radio", notice("bob"), (OTHER,))
    assert pool.poll() == (TypingUpdate(TARGET, 0), TypingUpdate(OTHER, 30))


def test_target_changes_replace_grants_and_clear_old_destinations():
    """Remapping does not retain the sender's activity at a withdrawn destination."""
    pool = TypingLeasePool(max_targets=1, clock=Clock())
    pool.observe("radio", notice(), (TARGET,))
    pool.poll()
    assert pool.observe("radio", notice(), (OTHER,))
    assert pool.poll() == (TypingUpdate(TARGET, 0), TypingUpdate(OTHER, 30))
    pool.observe("radio", notice(), ())
    assert pool.poll() == (TypingUpdate(OTHER, 0),)


def test_shared_target_capacity_refusal_is_atomic():
    """A rejected remap cannot clear another actor's occupied destination."""
    pool = TypingLeasePool(max_targets=1, clock=Clock())
    pool.observe("radio", notice(), (TARGET,))
    pool.observe("radio", notice("bob"), (TARGET,))
    pool.poll()
    assert not pool.observe("radio", notice(), (OTHER,))
    assert pool.poll() == ()
    assert pool.reset() == (TypingUpdate(TARGET, 0),)


def test_self_echo_is_ignored_and_duplicates_coalesce():
    """Loop prevention uses the concrete adapter/channel without parsing native IDs."""
    pool = TypingLeasePool(max_targets=1, clock=Clock())
    pool.observe("matrix", notice(channel=TARGET.channel_id), (TARGET, TARGET))
    assert pool.poll() == () and pool.next_deadline is None
    pool.observe("radio", notice(), (TARGET, TARGET))
    assert pool.poll() == (TypingUpdate(TARGET, 30),)


def test_reset_clears_only_emitted_indicators_and_discards_pending_sources():
    """Shutdown has no send queue to drain or replay and can be repeated safely."""
    pool = TypingLeasePool(clock=Clock())
    pool.observe("radio", notice(), (TARGET,))
    assert pool.reset() == ()
    pool.observe("radio", notice(), (OTHER, TARGET))
    assert pool.poll() == (TypingUpdate(TARGET, 30), TypingUpdate(OTHER, 30))
    assert pool.reset() == (TypingUpdate(TARGET, 0), TypingUpdate(OTHER, 0))
    assert pool.reset() == () and pool.next_deadline is None


def test_time_is_monotonic_and_a_rejected_clock_does_not_extend_leases():
    """Wall-clock shifts or an invalid test clock cannot keep stale activity alive."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice(), (TARGET,))
    pool.poll()
    for invalid in (99, float("nan"), float("inf"), True):
        clock.now = invalid
        with pytest.raises(ValueError, match="clock"):
            pool.poll()
    clock.now = 130
    assert pool.poll() == (TypingUpdate(TARGET, 0),)


@pytest.mark.parametrize(
    "settings",
    [
        {"max_sources": 0},
        {"max_targets": True},
        {"refresh_seconds": 0},
        {"refresh_seconds": float("nan")},
    ],
)
def test_invalid_capacity_and_refresh_configuration_is_rejected(settings):
    """Invalid local configuration cannot create an unbounded or busy-loop pool."""
    with pytest.raises(ValueError):
        TypingLeasePool(**settings)


def test_idle_refreshes_make_bounded_progress_to_expiry():
    """Half-life refreshes must not approach expiry through an infinite busy loop."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice(), (TARGET,))
    assert pool.poll()
    for _ in range(61):
        deadline = pool.next_deadline
        if deadline is None:
            break
        assert deadline > clock.now
        clock.now = deadline
        pool.poll()
    assert pool.next_deadline is None and clock.now == 130


def test_fractional_and_low_precision_clocks_do_not_spin_or_extend_lifetimes():
    """The next timer advances even when the clock cannot represent small deltas."""
    clock = Clock()
    pool = TypingLeasePool(clock=clock)
    pool.observe("radio", notice(seconds=0.125), (TARGET,))
    assert pool.poll() == (TypingUpdate(TARGET, 0.125),)
    assert pool.next_deadline == 100.125
    clock.now = 100.125
    assert pool.poll() == (TypingUpdate(TARGET, 0),)
    clock.now = 1e17
    pool.observe("radio", notice(), (TARGET,))
    assert pool.poll()[0].timeout_seconds <= 30
    assert clock.now < pool.next_deadline <= clock.now + 30
    clock.now = pool.next_deadline
    assert pool.poll() == (TypingUpdate(TARGET, 0),)


def test_sustained_target_churn_keeps_active_and_pending_output_bounded():
    """Rapid remapping retains one pending clear, without accumulating a backlog."""
    pool = TypingLeasePool(max_sources=1, max_targets=1, clock=Clock())
    pool.observe("radio", notice(), (TARGET,))
    pool.poll()
    for number in range(1000):
        assert pool.observe("radio", notice(), (TypingTarget("matrix", str(number)),))
    assert pool.poll() == (
        TypingUpdate(TARGET, 0),
        TypingUpdate(TypingTarget("matrix", "999"), 30),
    )


def test_native_identifiers_retain_unicode_joiners_and_exact_spelling():
    """Opaque native identity keys preserve emoji sequences without normalization."""
    native = "room@conference.example.org/🧑‍💻"
    activity = notice(sender=native)
    assert activity.sender_id == native
    target = TypingTarget("xmpp", native)
    assert target.channel_id == native
    with pytest.raises(ValueError):
        TypingTarget("xmpp", "bad\ud800identifier")
