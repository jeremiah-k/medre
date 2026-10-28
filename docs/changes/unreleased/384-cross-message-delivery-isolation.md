# Keep healthy destinations moving during a stalled handoff

Durable ingress stages complete route and plan decisions in the delivery outbox
before native I/O. The outbox dispatcher runs one handoff per adapter within the
global delivery limit, so a stalled destination does not block later messages to
healthy destinations. Waiting work stays in SQLite, including when automatic
retries are disabled. Initial reservations retain live provenance and use the
existing lifecycle, receipt, native-grant and paced adapter paths.

The outbox uses attempt zero for work never dispatched. Receipt and radio attempt
numbers remain positive. This changes the required prerelease SQLite shape;
earlier shapes are rejected rather than migrated. No existing database is deleted
or rewritten automatically.

Expired unreserved initial claims remain recoverable with automatic retries off.
Channel comparison follows storage's empty/null normalization, and full native
address scope is checked independently of the stored snapshot. Routed attribution
survives delivery reloads and retries. Lease renewal covers initial event
preparation. Due-time ordering prevents fresh pending work from starving retries;
operational deferrals pause an adapter lane without consuming a native attempt.
