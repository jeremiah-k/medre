# Count coalesced shutdown presses and start teardown at servicing time

The `medre run` signal receipt could undercount adjacent Ctrl+C presses. A
SIGINT delivered while the loop thread is un-serviced inside a C call (native
SDK work holding the GIL, for example) only trips CPython's process-wide
signal flag; every delivery that lands before the interpreter next services
signals coalesces into a single Python-level handler invocation. Two adjacent
presses then produced `Shutdown requested 1 time(s)` with the first-request
timestamp pointing at the second press, making the first press look swallowed.
The run path now installs a `signal.set_wakeup_fd` self-pipe — CPython's
C-level handler writes one byte per delivery there — and the run loop drains it outside the Python handler to avoid reentrant
counter updates, so the teardown receipt reports individual deliveries when
MEDRE owns the descriptor and the pipe has capacity. An existing process wakeup fd remains owned by its
host; MEDRE falls back to handler-count evidence rather than replacing it. The handler also wakes the run loop at servicing time through
`loop.call_soon_threadsafe`, so teardown starts immediately instead of
sleeping out the remainder of the one-second poll tick; that tick remains the
fallback cadence when the wake is unavailable. A separate real-process
regression test lengthens the fallback interval to establish the wakeup path
without relying on scheduler timing close to the production one-second poll. Signal escalation
semantics are unchanged: a repeated signal never interrupts owned cleanup, and
startup-cancellation ownership and the lifecycle transition contract are
untouched.

The run loop observes app-side shutdown-wait failures directly, including when a
signal completes at the same boundary. Ownership probing forwards any captured
wakeup bytes to an existing host before releasing the temporary pipe.
