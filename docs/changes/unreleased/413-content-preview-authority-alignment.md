# Keep content-policy previews aligned with persisted grants

Do not let an optional consumer hold authorize the trusted internal
`requester=None` path; only mandatory dependency holds may satisfy that path.
Reject optional holds extending beyond the immutable event ceiling in the
preview, consistent with persisted grant issuance. No runtime content retention,
read mediation, or deletion semantics change in this patch.
