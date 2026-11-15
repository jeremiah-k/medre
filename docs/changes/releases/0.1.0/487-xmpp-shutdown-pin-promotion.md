# Promote explicit XMPP shutdown ownership

Promote the merged XMPP adapter with explicit session phases, shared shutdown
deadlines, and retained native ownership after failed or interrupted cleanup.
Reconnect, session reuse, and new ingress remain fenced until cleanup succeeds;
unexpected runner cancellation also fences fresh native callbacks.
Startup cancellation propagates and invalid budgets preserve health observation.
The native two-account Docker Prosody test covers local MUC ingress, direct
egress, and cleanup with strict TLS verification.
