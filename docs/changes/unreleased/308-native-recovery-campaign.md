# Qualify native crash and transport recovery

- Add a reusable native Matrix-to-XMPP recovery campaign covering process death
  before and after handoff, restart after completed delivery, destination outage,
  and bounded retry exhaustion with a post-recovery canary.
- Add the Docker lab's resilience stage and offline analysis of independent
  native witnesses, immutable event/receipt history, committed outbox authority,
  crash windows, duplicate risk, and cleanup outcomes.
- Keep fault injection in owned subprocesses and an opaque loopback TCP gate;
  leave production recovery policies, leases, and persisted state unchanged.
- Review both stdout and stderr for every recovered runtime generation,
  reject missing streams and unhandled resource/task warnings, and use a
  longer bounded retry backoff for native reconnection qualification.
