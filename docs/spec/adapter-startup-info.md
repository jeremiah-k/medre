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
Names are at most 64 characters. Values may be strings or scalar booleans and
numbers. Omit unavailable facts. Core drops `None`, containers and empty text.

The adapter chooses the facts, their order, and their labels. Put the primary
account or node identity in the first row and device or endpoint details in the
following rows. Core displays up to ten rows of eight accepted facts each. The
row bound exists to keep a worst-case multi-adapter banner readable, not to
ration honest detail: ten packed rows carry up to eighty facts, roughly five
times what today's richest provider reports, and per-row fact packing already
bounds density. A provider that supplies more rows than displayed is truncated
to the first ten, and the rendered block ends with one `… +N more` marker line
counting the suppressed rows, so truncation is never invisible. For example, a
radio adapter may provide:

```python
{
    "rows": [
        [
            {"name": "node_name", "value": "Ridge Relay (RIDG)"},
            {"name": "node_id", "value": "!a4b3c2d1"},
        ],
        [
            {"name": "board", "value": "RAK4631"},
            {"name": "firmware", "value": "2.7.15"},
            {"name": "radio", "label": "radio (MHz)", "value": "910.525"},
        ],
    ],
}
```

Make every fact self-describing. When a fact carries no explicit label, core
derives one from the fact name, so a field named `node_id` renders as
`node !a4b3c2d1` and a field named `radio_mhz` renders as `radio mhz 910.525`.
To state units, prefer a label that carries them, such as `"label": "radio
(MHz)"`. Units are provider-owned guidance: core owns presentation and never
attaches transport-specific units or meanings itself. An explicit label always
wins over the derived one.

A flat mapping of fact names to scalar values produces one row whose facts
render with labels derived from their names. A non-string sequence under `rows`
selects grouped output; a scalar `rows` fact is an ordinary flat fact. Core
examines at most 128 candidates per row.
Providers MUST supply facts separately for validation and MUST NOT return
preformatted output lines. Firmware means the device's firmware version.
A public key belongs in startup info when it is a primary operator identifier.

The callable MUST use already-gathered state and MUST NOT perform new device
I/O, mutate runtime state, or acquire native resources. Synchronous discovery,
invocation and normalization run in worker threads; providers MUST expose
thread-safe snapshots for these operations. Async hooks execute on the owner
event loop and MUST yield cooperatively. A synchronous hook can return a mapping
or an awaitable that can be awaited on the owner loop. The provider MUST NOT
use the hook to own work that needs to survive startup or retirement.

Core supervises collection with a separate wait budget, defaulting to two
seconds. Provider failure, provider-owned cancellation or expiry yields no
fresh facts. Caller cancellation propagates. An adapter with no callable
skips emission. Core cancels outstanding async emission on expiry or caller
cancellation and discards late results, retaining unfinished work until it
settles. This bounds the coordinator's wait, not arbitrary provider execution:
Python cannot preempt synchronous code in an async hook or terminate a blocked
worker thread. A violating provider can still interfere with event-loop or
process shutdown; the hook contract prohibits such work.

## Presentation and cache

Core renders one heading per adapter with indented facts separated by a middle
dot. Every fact renders in the same `label value` grammar. When the provider
omits a label, core derives one from the fact name: word boundaries split at
underscores, hyphens, dots, and camel-case transitions, then lowercase and join
with spaces, so `node_id` renders `node !a4b3c2d1`. An explicit provider label
always wins. A provider that supplied more rows than displayed ends the block
with an indented `… +N more` line counting the suppressed rows; the JSON output
carries the same count. At runtime startup, the fresh block includes the
current lifecycle state.
Cached blocks render before adapter start and carry a `(cached)` marker with
no claim about current health. `medre adapters` uses the same block under
the configured adapter. Its JSON output includes ordered facts, row indices,
optional labels, per-field `cached` flags, and the suppressed-row count when
rows were truncated. Both text and JSON honor the
selected `--config` and use the same configured inventory and resolved paths.
`--json` applies only to the listing, not adapter preflight or package actions.

Display bounds exist to keep the banner compact and to bound malformed input,
never to truncate an honest record silently. Core displays at most ten rows
(flood prevention: rows are the visual unit, per-row fact packing already
bounds density, and ten packed rows keep a worst-case seven-adapter banner
readable) and packs at most eight accepted facts per row (line packing:
providers spread more facts across rows instead of overloading one line).
Omitted source rows beyond the first ten are counted by the `… +N more`
marker, including rows whose facts were not examined. The count saturates at
one million (displayed as a lower bound) consistently in live and cached
records. Per-row fact limits do not count as omitted rows. The
malformed-input defenses are unchanged: at most 128 candidate facts are
examined per row, displayed values are capped at 64 characters including the
truncation marker, raw inputs longer than 4096 characters are dropped, and
cache reads inspect at most 64 KiB per file; writers use compact UTF-8 JSON
and reject records exceeding that same limit (bounding a tampered cache to at
most the 1280 candidate fields parsed from one record). These caps bound
garbage, not honest records.

Core atomically writes the last-known facts beneath the adapter state directory
in `startup-info.json`, using the configured transport key and adapter ID.
Cached facts identify that state directory's last successful emission, not a
verified current account or connected device. The timestamp records an
emission observation using an offset-aware ISO timestamp.

Runtime cache reads and writes run off-loop with independent bounded waits;
reads run concurrently across adapters. A failed or expired read acts as a
miss; a failed or expired write does not invalidate fresh in-memory facts or
fail startup. Each write stages a unique private temporary file (owner-only
mode on POSIX), closes it before replacement, and cleans it up on failure or
expiry. No expired staging operation may begin replacement; a replacement
already in progress cannot be interrupted. Same-path writes are serialized
within one process, not across independent runtime owners. This is an atomic
last-known cache, not a cross-process transaction or crash-durable journal.

Readers inspect only regular files and read at most 64 KiB. Missing, oversized,
malformed, or identity-mismatched cache entries are absent. Readers accept older
flat entries without row or label metadata. This payload is outside the frozen
schemas.

## Redaction

Core validates and filters each fact before writing or displaying it. Secret-named
fields, pairing material, pre-shared keys, credential-bearing URLs, and unsafe
names or values do not appear. Core omits URLs with userinfo, a query, or a
fragment rather than guessing which parameters carry credentials. Explicit
public-key names (including `pubkey` and `publicKey`) exempt only the generic
long-key heuristic; secret-name tokens and recognized credential formats still
apply. Device IDs may be displayed, but remain subject to value checks.
Credential-shaped values use the canonical redaction marker, not a partial
secret. Values and display labels are capped at 64 characters including the
truncation marker; inputs longer than 4096 characters are dropped. Control,
format, surrogate and line-separator characters are rejected. A cached record
is revalidated on read, serialization and rendering so stale or tampered facts
must pass the same policy.

These checks are defense in depth, not a proof that arbitrary opaque strings
are public. Providers MUST NOT supply credentials, private keys, pairing
material or other non-public secrets as facts or labels. Startup logs and
last-known facts contain operator-visible account/device identifiers; they are
not intended as anonymized telemetry.
