# Separate sender names from presentation annotations

Relay prefixes accept sender_raw, sender_short_raw and sender_verification.
Providers can supply unannotated names separately from their default decorated
labels. Missing raw names fall back to the existing names; missing verification
is unknown. Choosing a different prefix does not change identity assurance.

The sender_compact alias uses the first five Unicode code points of the raw
name for concise radio prefixes; full names remain in structured metadata.
