# Consistency, recovery and delivery status

This maps the October 25 consistency/recovery/delivery audit to implementation
and qualification. Tests prove only their stated tier; the frozen 42-directed-pair,
168-receipt baseline remains regression evidence rather than a new campaign target.

| Audit work package | Implemented or landed | Remaining qualification or implementation |
| --- | --- | --- |
| Source and evidence authority | Core #342 adopts the exact merged six-provider canonical-text pins; Matrix retains its qualified pin. Core #341 repairs explicit reset recovery. Both passed their full CI matrices before merge. | One authoritative machine-readable native-evidence index and reconciliation of older SimpleX qualification prose against original private receipts. |
| Canonical text and attribution | Core #340 and the six coordinated provider PRs share styled/legacy text selection and strict native boundaries. Their pinned compatibility jobs passed. Sender/origin display and structured claims remain distinct. | Broader independent native evidence must retain transport, substrate, source SHA and confirmation limits; existing field support does not imply universal native identity. |
| Radio multipart attempts | Core #342 retains exact wire plans and guarded progress. Merged core #343 and MeshCore #13 provide the opt-in synchronous handoff path, full-plan local aggregation and read-only inspection. MeshCore fake/SDK CI passed against merged core main; its exact merged revision is adopted here. Merged Meshtastic #15 keeps that same claim while packets wait on the paced queue; this adoption pins its exact merged SHA and configuration schema. | Physical interruption/recovery and independent recipient receipts; native ACK aggregation for the complete message; safe resumption of proven-unsent tails with original part evidence. Partial and unknown work remains held. |
| Mixed-device recovery | Existing adapter reconnect pacing, diagnostics and lifecycle ownership remain. | Controlled radio interruption/restart with independent native observations, reconciliation and retained raw evidence. No new physical campaign was run here. |
| SimpleX text-transport parity | Shared text selection and conservative accepted-handoff reuse are landed. The local-queue ceiling remains explicit. | Separate contact/group receipts, native worker/profile shutdown fault injection, measured large-history envelope and evidence-document reconciliation. |
| Operator evidence and rollback | Exact radio-attempt inspection now reports current parent authority and historical packet progress without mutation or native I/O. Existing storage backup/reset remains explicit. | Consistent native-profile/signing/ledger restore procedures and correlated recovery evidence beyond radio local handoffs. |
| Plugin integration | Core #299 remains isolated. | Reconcile current contracts, storage, replay and authority only after delivery/recovery gates. |

Merged Meshtastic #15 uses the same synchronous guarded contract for planned
packets while its existing paced queue remains the native send owner. Core holds
its current in-progress claim until those packet waiters finish; ordinary sends
retain deferred feedback. It does not borrow a released queued claim or introduce
another storage authority. Cancellation closes the waiter and fences the SDK
worker; per-packet facts never become deferred parent completion. Native ACKs
still need separate full-plan aggregation before recipient confirmation can be
promoted. Partially accepted messages remain held after restart. Physical
qualifications remain bounded targeted verticals, not repeat all-pairs campaigns.

The remaining restart boundary is substantive: a proven accepted prefix with an
unsent tail still cannot resume automatically under a newly acquired attempt.
That needs an explicit ownership-transfer and original-part-evidence contract;
unknown or submitting parts must remain blocked. It is the next radio recovery
implementation gate rather than a completed feature. Physical and independent
recipient qualification cannot be substituted by another broad fake campaign.
