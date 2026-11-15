# Guard ordinary MeshCore native handoffs

The pinned MeshCore provider now renders short, clipped and fallback native text
as a one-packet plan under the existing durable core grant. One granted attempt
makes at most one SDK command. Timeout, disconnect and cancellation retain
uncertainty rather than resubmitting; acceptance and uncertain progress remain
bound after restart. Wire text and clipping stay unchanged, while empty final
native text fails before dispatch. Direct unbound session calls retain legacy
retry behavior outside this core guarantee.

The required companion-protocol replay qualification expands from 12 to 15
executed cases, with ordinary single-packet uncertainty alongside multipart
coverage. Positive single-packet replay now requires a complete durable
checkpoint as well as independent command frames and an exact ledger receipt.
The shared plan, progress, handoff and qualification schema shapes are unchanged.
Physical RF delivery, recipient acknowledgment and uncertain-handoff recovery
remain separate qualification or authority gaps.

The provider also fences prebuilt core-bound packets to the receiving adapter
and the renderer's selected native channel before grant use or synthetic
handoff. Signed numeric forms and configured-default targets cannot bypass the
check or redirect a channel send to a DM. Durable target identity stays exact.
