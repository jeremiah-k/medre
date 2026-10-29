# Canonical body admission identity

Canonical event append and durable ingress now persist a version-1 body
availability fact in the same SQLite transaction as the event. An explicitly
present empty string is retained content; a missing or malformed body is
unavailable content, **not** an empty message. Duplicate native admissions
preserve the original fact. Metadata-only inspection never returns content.

The required prerelease SQLite shape includes `event_body_identity`. Databases
from an earlier shape are refused: use fresh prerelease state. No migration,
legacy reader, or compatibility fallback is supplied.

This is an admission-identity foundation only. It does **not** authorize reads,
implement authenticated consumer holds, suppress delivery, erase text or
attachments, or enforce an expiry policy. Existing runtime payload and
provider-journal behavior is unchanged pending a unified admission/read/dispatch
and collection contract. Do not enable retention settings based on this fact.
