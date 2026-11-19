# Adapter Startup Info

This page specifies the adapter startup-info contract: a narrow,
duck-typed seam that lets an adapter surface platform facts at startup
(for example a Meshtastic board name and firmware version, or a Matrix
homeserver and user id) and lets core render those facts on operator
surfaces. It extends the adapter protocol defined in
[adapter-runtime.md](adapter-runtime.md) without changing provisioning,
send authority, routing, or delivery anywhere in the runtime.

## Authority

The seam is **display-only**. Startup info is operator-facing platform
description. It **MUST NOT** become authority for routing, delivery,
admission, health classification, or any runtime decision, and its
absence **MUST NOT** change adapter behavior on any surface.

## Discovery

Core **MUST** discover the seam by looking up an optional
`startup_info` attribute on the adapter object with `getattr` at the
point of use. Core **MUST NOT** import adapter symbols to find it. An
adapter without the attribute — or with a non-callable one — simply has
nothing to report: discovery yields nothing, no error is surfaced, and
adapters at any pin keep working unchanged.

## Emission

When an adapter transitions to ready during runtime assembly, core
**MUST** call the discovered callable with no arguments and **MUST**
await the result when it is awaitable. The await **MUST** be bounded by
a timeout (2 seconds by default) that is independent of the adapter
startup budget. The call **MUST NOT** block startup beyond its own
bound, **MUST NOT** be retried, and a timeout or failure **MUST**
yield no startup info without failing the adapter start. Caller
cancellation **MUST** still propagate: only a failure of the provider
call itself is absorbed.

## Purity

`startup_info()` **MUST** be a pure read: it formats state the provider
already gathered (diagnostics, handshake data) and **MUST NOT** perform
new device I/O. A provider that would have to poll, connect, or refresh
native state to answer **MUST** either not implement the seam or return
nothing. Emission runs inside the adapter-start task group, so a
violating implementation is bounded by the emission timeout, not
trusted.

## Durable last-known cache

Core **MUST** persist emitted startup info as the adapter's last-known
startup info under the adapter state directory (one
`startup-info.json` file per adapter), written atomically and on a
best-effort basis: a cache write failure **MUST** be logged without
affecting startup or delivery.

On restart, core **MUST** load the cached record before fresh emission
and render its values with a `cached` marker until a fresh emission
replaces them. The cache is display-only: it **MUST NOT** be an
authority for routing, delivery, or any runtime decision. A missing,
malformed, or unreadable cache **MUST** be treated as "nothing to
render" and is normal on first boot.

The cache payload shape is **not schema-frozen**: no document under
`docs/schemas` describes it, it **MUST NOT** be added to the frozen
schema manifest or its guards, and it may change without a coordinated
release. Readers **MUST** tolerate unknown keys and missing fields.

## Redaction at the core render boundary

Redaction is enforced by core wherever startup facts enter a rendered
or durable surface, regardless of what the adapter returned:

- Fields whose names match the canonical secret-key patterns — and any
  name whose tokens mark credentials, identity keys, or pairing
  material — **MUST NOT** be rendered at all. Public-key names
  (`public_key`, `pubkey`) are the sanctioned exception: they are
  retained and fall under the prefix rule below.
- Device identifiers (`device_id`) are operator-visible platform facts
  on the same class as board names and firmware versions; they **MUST**
  render in full. The shared error-sanitization anchor that treats
  `device_id` as key material does not apply to startup facts.
- Credential-bearing URLs **MUST** be omitted outright; values carrying
  terminal control characters **MUST** be dropped.
- Public keys, key material, and address-like values (node addresses,
  actor addresses, MAC, IP forms, long hex or base64 runs) **MUST**
  render as prefixes only, capped at 12 characters with a truncation
  marker, matching the diagnostics public-key-prefix grammar.
- Remaining values **MUST** pass the canonical token scrub shared by
  the log and error surfaces and are capped at a bounded display length.

The transformation **MUST** be idempotent so core can re-apply it at
every render boundary — emission, cache read-back, CLI rendering —
without altering already-redacted values. A tampered or stale cache
file **MUST NOT** be able to introduce renderable key material.

## Rendering

When last-known startup info exists for a configured adapter (fresh or
cached), the `medre adapters` listing **MAY** render one per-adapter
startup-info line in its human output; a cached record **MUST** carry
the `(cached)` provenance marker, following the CLI's
parenthesized-provenance grammar. The `--json` surface **MUST** carry
the structured record with a per-field `cached` flag. Values read from
the durable cache are always cached from the CLI's point of view
because the listing never starts adapters.

Providers without startup info are unaffected: discovery, readiness
reporting, and every other surface behave exactly as before.
