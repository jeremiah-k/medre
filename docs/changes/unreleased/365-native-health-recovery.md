# Reflect native recovery in current runtime health

Accept adapter-scoped native health observations through the runtime context.
Confirmed recovery updates current health without a manual diagnostics command
or periodic polling. Preserve startup history, per-provider observation times
and manual poll counts; ignore retired callbacks and prevent a slow diagnostic
sample from overwriting a later native transition. Native health remains
separate from lifecycle ownership and delivery evidence.

Retain provisional observations during startup probes, including probes that
fail or time out. Classify the startup barrier from the latest observations
after all providers settle, and discard provisional evidence from failed starts.

Startup summaries use current health to distinguish running adapter owners from
ready adapters. Routine startup output omits the stop-and-probe diagnostic hint;
the explicit diagnostic commands remain available in troubleshooting workflows.
