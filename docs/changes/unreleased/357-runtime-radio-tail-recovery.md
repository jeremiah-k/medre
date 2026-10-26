# Continue proven-unsent radio packets through configured retries

Integrate retained radio progress with the existing retry/lifecycle owner.
Transient outages before the next native grant can retain retry eligibility
for a proven plain-channel unsent tail. A fresh reserved attempt inherits
original accepted packet facts and sends only the persisted remainder through
the existing paced owner. Consumed reservations without radio work do not hide
an earlier prefix.

Changed rendering, explicit native-channel conflicts, submitting/unknown work,
permanent errors and consumed grants remain held. Retry opt-in, budgets,
backoff, deadlines, capacity, leases and terminal-row ownership are retained.
A retry cannot downgrade a retained multipart plan to an ordinary single send.
No operator command, package version, SQLite table or schema marker is added;
local handoff acceptance remains distinct from recipient delivery.
