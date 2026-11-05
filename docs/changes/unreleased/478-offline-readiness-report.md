# Offline readiness report

Add the read-only `medre readiness` command, which composes the existing
offline checks into one authoritative report: per compatibility-snapshot
provider (all seven, installed or not) the pinned repository revision,
declared distribution, installed version, registration state, and the
provider-owned setup form; per configured adapter the provider and SDK
distribution states, config validity, and the pure offline preflight
outcome with typed blocked categories; plus the offline route-plan
expansion summary, ordered actionable guidance, and an explicit
truthfulness boundary.

The command performs no SDK import, no login, no network or hardware I/O,
and no write of any kind; distribution presence is read through
distribution metadata only, and failures reduce to typed categories with
no error text, credentials, paths, or native identifiers in the output.
The report carries no timestamps and no filesystem paths, so identical
input and environment produce identical output. Zero installed providers
is a first-class report outcome, not an error, and the command exits 0
whenever it produces a report. Every runtime and recipient claim is
reported as `not_observed`, reusing the native-evidence vocabulary:
offline results never establish runtime readiness, native acceptance, or
recipient verification.
