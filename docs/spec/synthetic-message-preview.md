# Synthetic message preview

The `medre config preview-message` command is an offline presentation aid. It
MUST NOT create a canonical storage row, outbox item, delivery plan, receipt,
replay decision, native-send grant, SDK session, account, or network connection.

## Input and ownership

The command loads the effective configuration using the runtime YAML parser and
environment overrides. It uses the authoritative `build_route_plan` expansion
and the same registered, pure renderer factories as runtime assembly. It MUST
refuse a route set with expansion errors or conflicting exclusive routes. The
operator selects exactly one enabled configuration-level route; every expanded
leg of that route is shown (maximum 128 legs). No target is inferred from an
unspecified route or from a renderer-provided payload.

Every sample is a fixed **synthetic** `message.created` event (or `message.text`
when it is the only text kind admitted by the configured route), carrying an
explicit synthetic origin, a fixed text sample, and no native sender claim.
`--repeat` repeats that sample 1–32 times to exercise real truncation budgets.
`--relation reply` constructs a synthetic unresolved reply and explicitly
selects `fallback_text`; it does not claim that the live capability resolver
would select that strategy or that a native target relation exists.

Renderer output MUST pass the same event ID, adapter ID, and target channel
identity checks used at the delivery boundary. Only string text from known
rendered payload representations and wire-text parts from a generated radio
plan may enter the report. The command MUST NOT dump arbitrary adapter payload,
renderer metadata, exception text, or non-text native envelopes.

## Report and failure behavior

JSON output has `format=medre-synthetic-message-preview`, `version=1`,
`evidence_level=offline_renderer_only`, `source=synthetic`, `route_id`,
`relation_probe`, `sample_event_kind`, `status`, `legs`, and `limitations`.

Each leg records its expanded ID, source/target adapter IDs, target transport,
requested demonstration strategy, and either `status=rendered` with renderer,
text, rendered segments, lengths, `truncated`, and `fallback_applied`, or
`status=refused` with a closed `reason`: `target_not_enabled`,
`non_text_payload`, or `renderer_refusal`. Renderer exceptions are mapped to
`renderer_refusal` without exposing their strings. The top-level status is
`rendered` only when every requested leg renders; otherwise it is `incomplete`
and the CLI exits with the config-error code. Unknown, disabled, conflicted or
unresolvable routes cause a generic refusal without displaying config values.

## Evidence boundary

A preview is **not** proof that a route will match a real event, that planning
will select the demonstrated strategy, or that a provider will accept a native
payload. Sender attribution in real events depends on native sender metadata,
which a synthetic event cannot supply. This command MUST NOT be described as
fake smoke, adapter delivery, durable admission, native acceptance, or
independent recipient observation. No timeout, refusal, or preview outcome
confers resend or replay authority.
