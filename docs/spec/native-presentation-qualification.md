# Native basic presentation qualification

The baseline-v1 suite MUST enumerate each selected source with all four fixed
text fixtures: Unicode, multiline whitespace, literal HTML characters and byte
budget pressure. Radio-origin budget fixtures MUST use bounded source text;
oversized inputs MUST enter through IP services. Native owners SHOULD remain
alive across source turns rather than reconnect for each fixture.

Direction coverage and fixture coverage are distinct. Reports MUST retain both
the selected directed pairs and each source/fixture observation. Missing or
duplicate source fixtures MUST fail retained-evidence analysis even when every
direction has one successful message. Canonical identities MUST be distinct.

Expected prefixes and source claims MUST be authored independently of renderers
and outbox content. Each native receipt MUST match the expected sender,
conversation and complete rendered text, including declared truncation and
firmware headers. Matrix's visible HTML MUST agree with its plain body. Byte
budgets MUST include relay prefixes and exclude only independently established
firmware overhead. Full native protocol coverage MUST NOT imply RF qualification
for a transport exercised on a virtual substrate.

For Matrix destinations the baseline MUST observe the embedded bridge context
through the independent native receiver. Source instance, platform, canonical
event correlation, conversation, sender ID, raw sender name, origin label,
verification and scoped native reference MUST match the declared fixture and
ingress evidence. A plain source fixture MUST NOT acquire an invented relation.
Hiding a visible verification annotation MUST NOT upgrade the structured claim.

The uniform fixture profile uses full sender/origin prefixes on chat transports
and compact sender/source-instance prefixes on Meshtastic and MeshCore. Radio
host identity remains a separate native header. This profile does not change
production defaults; smoke qualification retains the default rendering profile.


A targeted XMPP endpoint MAY set `expect_bridge_context: true`. The runner MUST
retain that expectation separately from delivery rows, capture context from the
independent Slixmpp peer's actual XML stanza, and compare its source claims and
scoped references with authored expectations and canonical ingress evidence.
Retained-evidence analysis MUST reject missing or substituted context when it
was explicitly required. Existing baseline-v1 fixtures remain regression evidence;
this opt-in property MUST NOT require re-running the seven-platform baseline.

The report's `bridge_context_expectations` map MUST contain only selected adapter
IDs and boolean values, as described in
[native-bridge-context-expectations.schema.json](../schemas/native-bridge-context-expectations.schema.json).
Malformed maps MUST be rejected. Matrix requirements remain mandatory even when
a supplied map contains false or omits the Matrix destination.
