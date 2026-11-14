# Unify shared rendering policy across adapters

Canonical relation descriptions, exact reaction keys and bounded target previews now have one core owner. Renderers preserve authored content once, honor destination-scoped binding facts, share attribution precedence and validate relay-prefix templates before delivery. Native protocol encoding, capability decisions and final wire budgets remain transport-owned. The shared UTF-8 clipping primitive uses a bounded byte slice without repeatedly re-encoding shrinking strings. No canonical schema changes are required.

Meshtastic descriptive reactions preserve the same sender-label spacing as
messages. Matrix media captions retain unrepresented relation context and obey
final text budgets. Native Matrix edits reject oversized replacement/fallback
bodies and ambiguous mutation targets instead of silently clipping or selecting
one operation.

Authored messages carrying reaction context retain their text; reaction-only
events use described or native reaction delivery. Reaction and delete payloads
remain suppressed even when native fields represent every relation. Candidate
snapshots reject duplicate transport keys before writing CI output. Adapter CI
follows core main by default, resolving it once per run; exact candidate core
SHAs remain explicit qualification inputs.

Native Matrix mutations additionally require a matching event kind and relation;
missing or mismatched mutation intent fails closed before a native operation is
constructed, even for owned targets.
