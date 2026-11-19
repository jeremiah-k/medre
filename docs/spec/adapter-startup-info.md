# Adapter Startup Info

Startup info lets an adapter describe the device or account it is running on.
It is display-only. Its presence or contents MUST NOT change readiness, routing,
admission, or delivery decisions.

## Provider contract

Core discovers an optional `startup_info()` callable on the adapter object
and calls it with no arguments. Providers can return an ordered mapping with a
`rows` entry, each row containing a sequence of facts. Each fact has a machine
`name`, a display `value`, and an optional short display `label`. Names start
with an ASCII letter and then use letters, digits, underscores, hyphens, or dots.
Values may be strings or scalar booleans and numbers. Omit unavailable facts.

The adapter chooses the facts, their order, and their labels. Put the primary
account or node identity in the first row and device or endpoint details in the
second, when useful. Core displays up to two rows. For example, a radio adapter
may provide:

```python
{
    "rows": [
        [
            {"name": "node_name", "value": "Ridge Relay (RIDG)"},
            {"name": "node_id", "value": "!a4b3c2d1"},
        ],
        [
            {"name": "board", "value": "RAK4631"},
            {"name": "firmware", "label": "firmware", "value": "2.7.15"},
        ],
    ],
}
```

A flat mapping of fact names to scalar values produces one labeled row.
Providers MUST supply facts separately for validation and MUST NOT return
preformatted output lines. Firmware means the device's firmware version.
A public key belongs in startup info when it is a primary operator identifier.

The callable MUST use already-gathered state and MUST NOT perform new device
I/O. Core awaits an awaitable result under a separate timeout, defaulting to
two seconds. A provider failure or expired timeout yields no fresh facts.
Caller cancellation propagates. An adapter with no callable skips this step.

## Presentation and cache

Core renders one heading per adapter with indented facts separated by a middle
dot. At runtime startup, the fresh block includes the current lifecycle state.
Cached blocks render before adapter start and carry a `(cached)` marker with
no claim about current health. `medre adapters` uses the same block under
the configured adapter. Its JSON output includes ordered facts, row indices,
optional labels, and per-field `cached` flags.

Core atomically writes the last-known facts beneath the adapter state directory
in `startup-info.json`. A cache failure cannot affect startup or delivery.
Readers treat missing, malformed, or identity-mismatched cache entries as
absent. Readers accept older flat entries without row or label metadata.
This payload is outside the frozen schemas.

## Redaction

Core validates and filters each fact before writing or displaying it. Secret-named
fields, pairing material, pre-shared keys, credential-bearing URLs, and unsafe
names or values do not appear. Explicitly public identifiers and device IDs
can be shown in full. Credential-shaped values under other names use the
canonical redaction marker. Core caps field length for layout and validates
display labels separately from the values they describe.
