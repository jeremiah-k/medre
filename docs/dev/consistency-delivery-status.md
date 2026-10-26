# Consistency, recovery and delivery status

This maps the October 25 consistency/recovery/delivery audit to implementation
and qualification. Tests prove only their stated tier; the frozen 42-directed-pair,
168-receipt baseline remains regression evidence rather than a new campaign target.

| Audit work package | Implemented or landed | Remaining qualification or implementation |
| --- | --- | --- |
| Source and evidence authority | Core #342 adopts the exact merged six-provider canonical-text pins; Matrix retains its qualified pin. Core #341 repairs explicit reset recovery. Both passed their full CI matrices before merge. | One authoritative machine-readable native-evidence index and reconciliation of older SimpleX qualification prose against original private receipts. |
| Canonical text and attribution | Core #340 and the six coordinated provider PRs share styled/legacy text selection and strict native boundaries. Their pinned compatibility jobs passed. Sender/origin display and structured claims remain distinct. | Broader independent native evidence must retain transport, substrate, source SHA and confirmation limits; existing field support does not imply universal native identity. |
| Radio multipart attempts | Core #342 retains exact wire plans and guarded progress. The current core/MeshCore changes implement a complete opt-in synchronous vertical through the paced native owner, exact packet handoffs, weakest-level aggregation and read-only inspection. | Installed-SDK and physical MeshCore recovery qualification; Meshtastic's deferred queue ownership and packet-feedback aggregation; independently verified recipient receipts. No exactly-once RF claim. |
| Mixed-device recovery | Existing adapter reconnect pacing, diagnostics and lifecycle ownership remain. | Controlled radio interruption/restart with independent native observations, reconciliation and retained raw evidence. No new physical campaign was run here. |
| SimpleX text-transport parity | Shared text selection and conservative accepted-handoff reuse are landed. The local-queue ceiling remains explicit. | Separate contact/group receipts, native worker/profile shutdown fault injection, measured large-history envelope and evidence-document reconciliation. |
| Operator evidence and rollback | Exact radio-attempt inspection now reports current parent authority and historical packet progress without mutation or native I/O. Existing storage backup/reset remains explicit. | Consistent native-profile/signing/ledger restore procedures and correlated recovery evidence beyond radio local handoffs. |
| Plugin integration | Core #299 remains isolated. | Reconcile current contracts, storage, replay and authority only after delivery/recovery gates. |

The next implementation after the synchronous MeshCore vertical is Meshtastic's
deferred queue ownership boundary. A queued outbox has released its worker;
it cannot borrow an earlier in-progress claim or reuse the synchronous radio
opt-in. Native acknowledgement observations must retain packet scope and cannot
complete a parent after only one accepted fragment. Physical qualifications
remain bounded targeted verticals, not repeat all-pairs campaigns.
