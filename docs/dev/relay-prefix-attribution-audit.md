# Relay Prefix and Provenance Audit

Factual audit of current MEDRE relay prefix and sender-provenance
behavior by transport. No aspirational language; describes running code
as inspected on the `main` branch.

---

## Matrix

### Inbound Identity Fields

The Matrix adapter (`MatrixAdapter._on_room_message`, lines 741–869 in
`src/medre/adapters/matrix/adapter.py`) receives normalized plain dicts
from the session boundary with keys: `room_id`, `sender`, `body`,
`event_id`, `source`, `msgtype`, `server_timestamp`,
`sender_display_name`.

The codec (`MatrixCodec.decode` in
`src/medre/adapters/matrix/codec.py`, lines 75–345) populates native
metadata with:

| Key        | Source                            |
| ---------- | --------------------------------- |
| `room_id`  | event dict                        |
| `event_id` | event dict                        |
| `sender`   | event dict (MXID, e.g. `@user:s`) |

The codec also captures MMRelay wire-format fields from Matrix event
content into native data via `_capture_mmrelay_fields` (codec.py lines
427–451):

| Wire key                  | Constant           | Captured as native key |
| ------------------------- | ------------------ | ---------------------- |
| `meshtastic_id`           | `KEY_ID`           | Same name              |
| `meshtastic_replyId`      | `KEY_REPLY_ID`     | Same name              |
| `meshtastic_text`         | `KEY_TEXT`         | Same name              |
| `meshtastic_emoji`        | `KEY_EMOJI`        | Same name              |
| `meshtastic_meshnet`      | `KEY_MESHNET`      | Same name              |
| `meshtastic_portnum`      | `KEY_PORTNUM`      | Same name              |
| `meshtastic_longname`     | `KEY_LONGNAME`     | Same name              |
| `meshtastic_shortname`    | `KEY_SHORTNAME`    | Same name              |
| `meshtastic_reaction_key` | `KEY_REACTION_KEY` | Same name              |

After codec decode, the adapter enriches native metadata with
display-name attribution (adapter.py lines 820–860). When the event
has no existing `longname` or `shortname` (from MMRelay fields), the
adapter derives them from the Matrix sender:

| Enriched key  | Derivation                                                                                     |
| ------------- | ---------------------------------------------------------------------------------------------- |
| `displayname` | `sender_display_name` or `sender`                                                              |
| `longname`    | Same as `displayname`                                                                          |
| `shortname`   | First 5 chars of `displayname`, or first 5 chars of MXID localpart if display name equals MXID |

This enrichment happens **after** codec decode, using
`msgspec.structs.replace` because `CanonicalEvent` is frozen.

### Outbound Prefix Behavior

The Matrix renderer (`MatrixRenderer` in
`src/medre/adapters/matrix/renderer.py`) applies a relay prefix to the
message body via `_apply_matrix_relay_prefix`.

**Prefix configuration source (two paths):**

1. **Target-local (preferred):** `MatrixConfig.relay_prefix` (string, default
   `""`). When non-empty, this template is used for all Matrix outbound
   renders. The prefix lives on the adapter that owns the rendering.

2. **Backward-compat fallback:** When `MatrixConfig.relay_prefix` is empty,
   the renderer falls back to the source adapter config resolved via the
   `source_configs` mapping. This preserves legacy behavior where
   the source adapter's config controlled the Matrix-bound prefix.

The `{origin_label}` template variable is resolved from the source adapter's
`origin_label` config via the runtime source-attribution registry.

**Available template variables** (all coalesced to empty string on
`None`):

| Variable          | Source                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| `{sender}`        | Source sender display name (from attribution extractor)                |
| `{sender_short}`  | Source sender short label (from attribution extractor)                 |
| `{sender_id}`     | Source sender native ID (MXID, node ID, etc.)                          |
| `{sender_handle}` | Source sender handle / address                                         |
| `{platform}`      | Source platform name (`matrix`, `meshtastic`, etc.)                    |
| `{route_id}`      | Matched route identifier                                               |
| `{channel}`       | Source room or channel ID                                              |
| `{origin_label}`  | Source adapter config `origin_label` (via source-attribution registry) |

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
and `{meshnet_name}` are **unknown placeholders** in the current formatter.
They are left unchanged in the rendered output and reported in
`unknown_variables`.

**Default prefix** (from `MatrixConfig.relay_prefix`):
`""` (no prefix by default). Operators configure the prefix template on the
Matrix adapter config (target-local).

**Application points:**

1. Direct mode body (renderer.py line 210)
2. Fallback-text mode body (renderer.py line 312)
3. Reaction emote body via `_format_reaction_prefix` (renderer.py lines
   469–496)

The prefix is applied **before** any truncation.

**When no source config is found** (e.g. event from an adapter not in
the `source_configs` mapping), the prefix resolves to empty string and
no prefix is prepended.

**Source-origin label enrichment:** The Matrix renderer looks up the source
adapter's `origin_label` from the source-attribution registry and populates
`source_origin_label` on the `RelayAttribution` used for prefix formatting.
When the source adapter has no `origin_label`, the value is empty string.

### Matrix Metadata Envelope

`MatrixMetadataEnvelope` (in
`src/medre/adapters/matrix/metadata.py`) embeds provenance under the
`medre.envelope` key in the Matrix content dict with fields:

- `schema_version` (default 1)
- `canonical_event_id`
- `source_adapter`
- `source_channel`
- `metadata_mode` (`"safe"`)

When `mmrelay_compatibility=True` on the source MeshtasticConfig, the
renderer injects additional mesh metadata via `_inject_mmrelay_metadata`
(renderer.py lines 669–702):

| Injected key           | Value source                                                        |
| ---------------------- | ------------------------------------------------------------------- |
| `meshtastic_id`        | `native_data["packet_id"]`                                          |
| `meshtastic_longname`  | `native_data["longname"]`                                           |
| `meshtastic_shortname` | `native_data["shortname"]`                                          |
| `meshtastic_meshnet`   | `config.origin_label` (mmrelay compat: populated from origin_label) |
| `meshtastic_portnum`   | Hardcoded `"TEXT_MESSAGE_APP"`                                      |
| `meshtastic_text`      | Event payload `text` or `body`                                      |

MMRelay injection is **skipped for reactions** (reaction rendering
handles its own MMRelay keys).

### MatrixConfig Prefix-Relevant Fields

`src/medre/config/adapters/matrix.py`: `MatrixConfig` now has a
**target-local prefix template field**:

| Field          | Default | Purpose                                          |
| -------------- | ------- | ------------------------------------------------ |
| `relay_prefix` | `""`    | Target-local prefix template for Matrix outbound |
| `origin_label` | `""`    | Source label for use when Matrix is source       |

The old model (prefix from `MeshtasticConfig.matrix_relay_prefix`) is removed.
Matrix prefix is now target-local via `MatrixConfig.relay_prefix` only.

---

## Meshtastic

### Meshtastic Inbound Identity Fields

The Meshtastic codec (`MeshtasticCodec.decode` in
`src/medre/adapters/meshtastic/codec.py`, lines 59–265) produces native
metadata with these identity-relevant keys:

| Key                 | Source                                           |
| ------------------- | ------------------------------------------------ |
| `packet_id`         | Classifier from packet `id` field                |
| `from_id`           | Classifier `from_id` (numeric node ID string)    |
| `channel`           | Packet `channel` or config `default_channel`     |
| `portnum`           | Classifier portnum                               |
| `to_id`             | Packet `toId`                                    |
| `is_direct_message` | Classifier flag                                  |
| `longname`          | From `node_info["longname"]` (passed at decode)  |
| `shortname`         | From `node_info["shortname"]` (passed at decode) |
| `reply_id`          | Classifier from `decoded.replyId`                |
| `emoji`             | Raw `decoded.emoji` value                        |
| `emoji_flag`        | Boolean from classifier                          |

The `node_info` dict is provided by the adapter at decode time from the
session's node database. When node info is unavailable, `longname` and
`shortname` are empty strings.

### Meshtastic Outbound Prefix Behavior

`src/medre/adapters/meshtastic/renderer.py`) prepends
`radio_relay_prefix` via `_format_prefix_for` (lines 158–228).

The renderer looks up the source adapter's `origin_label` from the
source-attribution registry to populate `source_origin_label` on the
`RelayAttribution`. When the source adapter has no `origin_label`, the
value is empty string.

**Available template variables:**

| Variable          | Source                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| `{sender}`        | Source sender display name (from attribution extractor)                |
| `{sender_short}`  | Source sender short label (from attribution extractor)                 |
| `{sender_id}`     | Source sender native ID                                                |
| `{sender_handle}` | Source sender handle / address                                         |
| `{platform}`      | Source platform name (`matrix`, `meshtastic`, etc.)                    |
| `{route_id}`      | Matched route identifier                                               |
| `{channel}`       | Source room or channel ID                                              |
| `{origin_label}`  | Source adapter config `origin_label` (via source-attribution registry) |

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
and `{meshnet_name}` are **unknown placeholders** in the current formatter.

**Default prefix:** `"{sender_short}: "` — matches mmrelay's
`DEFAULT_MESHTASTIC_PREFIX = "{display5}[M]: "` minus the hardcoded
platform tag.

**compact mode:** Used for cross-platform reactions. Strips spaces
from sender labels before template substitution.

**Prefix is NOT applied for:**

- Structured native reactions (`emoji=1`, same-adapter source)
- Descriptive cross-platform reactions (which embed their own compact
  prefix in the text body)

Prefix is applied **before** UTF-8 byte-budget truncation.

### MeshtasticConfig Prefix-Relevant Fields

`src/medre/config/adapters/meshtastic.py`:

| Field                   | Default              | Purpose                                          |
| ----------------------- | -------------------- | ------------------------------------------------ |
| `radio_relay_prefix`    | `"{sender_short}: "` | Default for Matrix→mesh body prefix              |
| `mmrelay_compatibility` | `False`              | Inject MMRelay mesh metadata into Matrix content |
| `origin_label`          | `""`                 | Available as `{origin_label}` in both prefixes   |
| `max_text_bytes`        | `227`                | UTF-8 byte budget after rendering                |

---

## MeshCore

### MeshCore Inbound Identity Fields

The MeshCore codec (`MeshCoreCodec.decode` in
`src/medre/adapters/meshcore/codec.py`, lines 52–154) produces native
metadata with **namespace-prefixed** keys:

| Key                          | Source                               |
| ---------------------------- | ------------------------------------ |
| `meshcore.packet_id`         | `sender_timestamp` from SDK event    |
| `meshcore.sender_id`         | `pubkey_prefix` (6-byte hex)         |
| `meshcore.channel`           | `channel_idx` from event             |
| `meshcore.pubkey_prefix`     | Same as `sender_id`                  |
| `meshcore.txt_type`          | `txt_type` from event                |
| `meshcore.is_direct_message` | Classifier flag                      |
| `meshcore.classification`    | Sub-dict with action/category/reason |

Notable: **no `longname`, `shortname`, or `from_id` keys**. Sender
identity is a hex pubkey prefix, not a human-readable name.

### MeshCore Outbound Prefix Behavior

The MeshCore renderer (`MeshCoreRenderer` in
`src/medre/adapters/meshcore/renderer.py`, lines 208–221) prepends a relay
prefix when `meshcore_relay_prefix` is non-empty on the target adapter's
`MeshCoreConfig`.

The renderer looks up the source adapter's `origin_label` from the
source-attribution registry to populate `source_origin_label` on the
`RelayAttribution`. When the source adapter has no `origin_label`, the
value is empty string.

**Prefix configuration source:** `MeshCoreConfig.meshcore_relay_prefix`
(string, default `""`).

**Available template variables** — same shared set as all transports (see
attribution.py `_ALL_KNOWN_NAMES`):

| Variable          | Source                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| `{sender}`        | Attribution extractor (empty for MeshCore sources)                     |
| `{sender_short}`  | Attribution extractor (empty for MeshCore sources)                     |
| `{sender_id}`     | Attribution extractor (`pubkey_prefix` for MeshCore)                   |
| `{sender_handle}` | Attribution extractor (empty for MeshCore sources)                     |
| `{platform}`      | Source platform name                                                   |
| `{route_id}`      | Matched route identifier                                               |
| `{channel}`       | Source channel ID                                                      |
| `{origin_label}`  | Source adapter config `origin_label` (via source-attribution registry) |

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
and `{meshnet_name}` are **unknown placeholders** — they are left unchanged
in the rendered output.

**Default prefix:** `""` (no prefix).

**Application:** Prefix is prepended before UTF-8 byte-budget truncation.
The rendered prefix counts toward `max_text_bytes`.

**Metadata keys** (conditional, only when `meshcore_relay_prefix` is
non-empty):

| Key                              | Value                                  |
| -------------------------------- | -------------------------------------- |
| `relay_prefix_template`          | Original template string               |
| `relay_prefix_rendered`          | Rendered prefix string                 |
| `relay_prefix_variables_used`    | Tuple of template variables resolved   |
| `relay_prefix_missing_variables` | Tuple of variables that resolved empty |
| `relay_prefix_unknown_variables` | Tuple of unknown placeholder names     |
| `relay_prefix_formatting_error`  | Error string or `None`                 |

### MeshCoreConfig Prefix-Relevant Fields

`src/medre/config/adapters/meshcore.py`:

| Field                   | Default | Note                                             |
| ----------------------- | ------- | ------------------------------------------------ |
| `origin_label`          | `""`    | Available as `{origin_label}` in prefix template |
| `max_text_bytes`        | `512`   | UTF-8 byte budget (prefix counts toward it)      |
| `meshcore_relay_prefix` | `""`    | Prefix template; empty = no prefix prepended     |

---

## LXMF

### LXMF Inbound Identity Fields

The LXMF codec (`LxmfCodec.decode` in
`src/medre/adapters/lxmf/codec.py`, lines 132–242) produces native
metadata:

| Key                | Source                                   |
| ------------------ | ---------------------------------------- |
| `source_hash`      | Sender identity hash (hex, 32 chars)     |
| `destination_hash` | Recipient identity hash (hex, 32 chars)  |
| `message_id`       | `LXMessage.hash` (hex)                   |
| `timestamp`        | `LXMessage.timestamp`                    |
| `title`            | `LXMessage.title`                        |
| `delivery_method`  | `LXMessage.method` from event            |
| `has_fields`       | Whether MEDRE fields envelope is present |

Notable: **no `longname`, `shortname`, or `from_id` keys**. Sender
identity is a 32-character hex Reticulum identity hash.

The codec can reconstruct `EventRelation` objects from the MEDRE
envelope in `fields[0xFD]` via `_reconstruct_relations` (codec.py lines
62–130).

### LXMF Outbound Prefix Behavior

The LXMF renderer (`LxmfRenderer` in
`src/medre/adapters/lxmf/renderer.py`, lines 168–183) prepends a relay
prefix when a relay prefix is configured on the target `LxmfConfig`.

The LXMF renderer is **target-aware**: the prefix template comes from the
target LXMF adapter's `lxmf_relay_prefix` config, resolved per delivery
target. The renderer looks up the source adapter's `origin_label` from the
source-attribution registry to populate `source_origin_label` on the
`RelayAttribution`.

**Prefix configuration source:** `LxmfConfig.lxmf_relay_prefix` (string,
default `""`).

**Available template variables** — same shared set as all transports (see
attribution.py `_ALL_KNOWN_NAMES`):

| Variable          | Source                                                                 |
| ----------------- | ---------------------------------------------------------------------- |
| `{sender}`        | Attribution extractor (empty for LXMF sources)                         |
| `{sender_short}`  | Attribution extractor (empty for LXMF sources)                         |
| `{sender_id}`     | Attribution extractor (`source_hash` for LXMF)                         |
| `{sender_handle}` | Attribution extractor (empty for LXMF sources)                         |
| `{platform}`      | Source platform name                                                   |
| `{route_id}`      | Matched route identifier                                               |
| `{channel}`       | Source channel ID (always empty for LXMF)                              |
| `{origin_label}`  | Source adapter config `origin_label` (via source-attribution registry) |

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
and `{meshnet_name}` are **unknown placeholders** in the current formatter.

**Default prefix:** `""` (no prefix).

**Application:** Prefix is prepended to the content body before
character-budget truncation (`max_text_chars`) and before envelope
handling. The rendered prefix counts toward the character budget.

**Metadata keys** (conditional, only when `lxmf_relay_prefix` is
non-empty):

| Key                              | Value                                  |
| -------------------------------- | -------------------------------------- |
| `relay_prefix_template`          | Original template string               |
| `relay_prefix_rendered`          | Rendered prefix string                 |
| `relay_prefix_variables_used`    | Tuple of template variables resolved   |
| `relay_prefix_missing_variables` | Tuple of variables that resolved empty |
| `relay_prefix_unknown_variables` | Tuple of unknown placeholder names     |
| `relay_prefix_formatting_error`  | Error string or `None`                 |

### LxmfConfig Prefix-Relevant Fields

`src/medre/config/adapters/lxmf.py`:

| Field                | Default | Note                                             |
| -------------------- | ------- | ------------------------------------------------ |
| `origin_label`       | `""`    | Available as `{origin_label}` in prefix template |
| `metadata_embedding` | `True`  | Controls MEDRE envelope in fields                |
| `display_name`       | `""`    | For LXMF announces, not prefixing                |
| `lxmf_relay_prefix`  | `""`    | Prefix template; empty = no prefix prepended     |

---

## MMRelay Compatibility

### Wire-Format Constants

`src/medre/interop/mmrelay.py` defines cross-adapter wire keys:

| Constant           | Value                       | Used by                           |
| ------------------ | --------------------------- | --------------------------------- |
| `KEY_ID`           | `"meshtastic_id"`           | Matrix renderer injection         |
| `KEY_LONGNAME`     | `"meshtastic_longname"`     | Matrix renderer + codec capture   |
| `KEY_SHORTNAME`    | `"meshtastic_shortname"`    | Matrix renderer + codec capture   |
| `KEY_MESHNET`      | `"meshtastic_meshnet"`      | Matrix renderer injection         |
| `KEY_PORTNUM`      | `"meshtastic_portnum"`      | Matrix renderer injection         |
| `KEY_TEXT`         | `"meshtastic_text"`         | Matrix renderer injection         |
| `KEY_REPLY_ID`     | `"meshtastic_replyId"`      | Matrix renderer reply + reaction  |
| `KEY_EMOJI`        | `"meshtastic_emoji"`        | Matrix renderer reaction fallback |
| `KEY_REACTION_KEY` | `"meshtastic_reaction_key`" | MEDRE extension, not standard     |
| `PORTNUM_TEXT`     | `"TEXT_MESSAGE_APP"`        | Hardcoded injection value         |
| `EMOJI_FLAG_VALUE` | `1`                         | Reaction flag                     |

### KEY_MESHNET Isolation

`KEY_MESHNET` (`"meshtastic_meshnet"`) is an **external mmrelay wire-format
field** — not a MEDRE attribution variable, config field, or template
variable. It is read and written **only** when `mmrelay_compatibility=True`
on the source Meshtastic adapter config.

- **Populated from:** `derive_meshnet_value()` in `src/medre/interop/mmrelay.py`
  resolves from `source_origin_label` (route/context) →
  `adapter_origin_label` (source-attribution registry) → empty string.
- **Written to:** Matrix event content payload by the Matrix renderer during
  mmrelay-compatible metadata injection.
- **Read from:** Matrix event content by the Matrix codec's
  `_capture_mmrelay_fields` for inbound mmrelay-origin messages.
- **Not used in:** prefix templates, routing, delivery, or any other
  rendering path. `{meshnet_name}` is an unknown placeholder in the prefix
  formatter.

This field is **temporary and isolated** in the mmrelay interop code.
It is easy to remove once mmrelay is updated to use MEDRE's native
attribution model. Operators should not rely on `meshtastic_meshnet` as a
config or template variable.

### MMRelay-Compatible Fields in Matrix Inbound

The Matrix codec captures all MMRelay fields from Matrix event content
into native metadata via `_capture_mmrelay_fields`. This means
messages relayed by mmrelay into Matrix rooms have their Meshtastic
provenance preserved in native metadata under the `meshtastic_*` keys.

### MMRelay-Compatible Fields in Matrix Outbound

When `MeshtasticConfig.mmrelay_compatibility=True`, the Matrix renderer
injects all standard MMRelay mesh metadata keys into the Matrix event
content. This is controlled by the Meshtastic source adapter config,
not the Matrix target adapter config.

For reactions with mmrelay_compat or missing Matrix-native target:
renders as `m.emote` with `KEY_EMOJI=1`, `KEY_REPLY_ID`,
`KEY_REACTION_KEY`, `KEY_TEXT`, plus full mesh provenance (`KEY_ID`,
`KEY_LONGNAME`, `KEY_SHORTNAME`, `KEY_MESHNET`, `KEY_PORTNUM`).

---

## origin_label

### Concept

`origin_label` is a platform-neutral, operator-defined source label stored on
each adapter config (`MatrixConfig.origin_label`, `MeshtasticConfig.origin_label`,
`MeshCoreConfig.origin_label`, `LxmfConfig.origin_label`). All four configs
declare it as a string with default `""`.

The canonical field on `RelayAttribution` is `source_origin_label`. The
template alias is `{origin_label}`. It is resolved through the runtime
source-attribution registry, which maps adapter instance names to their
`origin_label` config value.

### Source-Attribution Registry

The runtime builder constructs a source-attribution registry from all adapter
configs at assembly time. Renderers consult this registry to look up the
source adapter's `origin_label` when populating `RelayAttribution` for prefix
formatting. When the source adapter has no `origin_label` configured (empty
string), the variable resolves to an empty string.

### Distinction from Other Labels

| Concept               | Template variable | Source                       | Scope                        |
| --------------------- | ----------------- | ---------------------------- | ---------------------------- |
| `origin_label`        | `{origin_label}`  | Source adapter config        | MEDRE-generic operator label |
| `source_sender_id`    | `{sender_id}`     | Source event native metadata | Per-transport native ID      |
| `source_display_name` | —                 | Source event native metadata | Per-transport display name   |

---

## Cross-Transport Gaps

### 1. Prefix defaults differ across transports

MeshCore and LXMF have relay prefix support via `meshcore_relay_prefix`
and `lxmf_relay_prefix`, but both default to `""` (no prefix). Meshtastic
defaults to `"{sender_short}: "` for radio. Matrix uses `MatrixConfig.relay_prefix`
(target-local, default `""`). Operators must explicitly
configure MeshCore, LXMF, and Matrix prefixes to get attribution on those
transports.

### 2. No `{sender}`/`{sender_short}` on MeshCore or LXMF sources

MeshCore uses `meshcore.sender_id` (hex pubkey prefix). LXMF uses
`source_hash` (hex identity hash). Neither populates
`source_sender_label` or `source_sender_short_label` in native metadata.

Consequence: prefix templates referencing `{sender}` or `{sender_short}`
resolve to empty string when the source is MeshCore or LXMF. Operators
SHOULD prefer `{origin_label}` or `{sender_id}` for cross-platform templates.

### 3. Prefix config ownership is target-local for Matrix, target-owned for others

`MatrixConfig.relay_prefix` is the target-local prefix template for Matrix
outbound. `MeshtasticConfig.matrix_relay_prefix` has been removed — Matrix
prefix is target-local only. `MeshCoreConfig.meshcore_relay_prefix` and
`LxmfConfig.lxmf_relay_prefix` are on their respective target configs,
resolved by their own renderers. All renderers resolve `{origin_label}` from
the source adapter config via the source-attribution registry — prefix
variables describe the source, not the target.

### 4. Matrix display-name enrichment is post-codec

The Matrix adapter enriches `longname`/`shortname` native metadata keys
from Matrix display names **after** codec decode, in `_on_room_message`.
These native metadata keys are consumed by the attribution extractor to
populate `source_sender_label` and `source_sender_short_label`. Code paths
that use codec output directly (without passing through the adapter) will
not have display-name attribution.

### 5. Metadata key namespace inconsistency

Meshtastic native metadata uses flat keys: `longname`, `shortname`,
`from_id`, `packet_id`, `channel`. MeshCore uses namespaced keys:
`meshcore.packet_id`, `meshcore.sender_id`, `meshcore.channel`. LXMF
uses yet another set: `source_hash`, `destination_hash`, `message_id`.
Matrix uses flat keys matching MMRelay: `sender`, `room_id`, `event_id`,
plus captured `meshtastic_*` keys.

The attribution extractor normalizes these into canonical
`source_sender_label`, `source_sender_short_label`, `source_sender_id`,
etc. Prefix template variables (`{sender}`, `{sender_short}`,
`{sender_id}`) work for all source transports via this normalization
layer. For MeshCore and LXMF sources, sender labels are empty but
`sender_id` carries the native identifier.

`{origin_label}` is the MEDRE-generic source label available on all
adapter configs. It resolves to the operator-defined label for any
transport source. Operators SHOULD prefer `{origin_label}` in
cross-platform templates.

### 6. `mmrelay_compatibility` is Meshtastic-only

Only `MeshtasticConfig` has the `mmrelay_compatibility` flag. The
Matrix renderer checks this flag on the source adapter's config to
decide MMRelay metadata injection. There is no equivalent mechanism
for MeshCore or LXMF sources, and no MMRelay-compatible metadata is
generated for those transports.

### 7. `shortname5` derivation removed

The old `{shortname5}` variable was a derived value that behaved
differently per context. It has been removed from the known variable
schema. Use `{sender_short}` instead — the attribution extractor
provides the short label directly.

### 8. No cross-transport name resolution

There is no mechanism to resolve a human-readable name from a MeshCore
pubkey prefix or LXMF identity hash into `source_sender_label` or
`source_sender_short_label` for downstream prefix templates. Node info
lookup exists only for Meshtastic (via the SDK node database).

---

## Inspected Files

### Source code

| File                                        | Lines inspected  |
| ------------------------------------------- | ---------------- |
| `src/medre/config/adapters/meshtastic.py`   | Full (268 lines) |
| `src/medre/config/adapters/matrix.py`       | Full (217 lines) |
| `src/medre/config/adapters/meshcore.py`     | Full (243 lines) |
| `src/medre/config/adapters/lxmf.py`         | Full (243 lines) |
| `src/medre/adapters/matrix/adapter.py`      | Full (984 lines) |
| `src/medre/adapters/matrix/codec.py`        | Full (452 lines) |
| `src/medre/adapters/matrix/renderer.py`     | Full (702 lines) |
| `src/medre/adapters/meshtastic/codec.py`    | Full (265 lines) |
| `src/medre/adapters/meshtastic/renderer.py` | Full (777 lines) |
| `src/medre/adapters/meshcore/codec.py`      | Full (154 lines) |
| `src/medre/adapters/meshcore/renderer.py`   | Full (296 lines) |
| `src/medre/adapters/lxmf/codec.py`          | Full (242 lines) |
| `src/medre/adapters/lxmf/renderer.py`       | Full (214 lines) |
| `src/medre/interop/mmrelay.py`              | Full (24 lines)  |

### Spec docs

| File                                         | Lines inspected  |
| -------------------------------------------- | ---------------- |
| `docs/spec/transport-profiles/matrix.md`     | Full (200 lines) |
| `docs/spec/transport-profiles/meshtastic.md` | Full (239 lines) |
| `docs/spec/transport-profiles/meshcore.md`   | Full (220 lines) |
| `docs/spec/transport-profiles/lxmf.md`       | Full (244 lines) |

### Test files (prefix-relevant coverage)

| File                                                | Focus                                |
| --------------------------------------------------- | ------------------------------------ |
| `tests/test_matrix_renderer.py`                     | Prefix, rendering, relations         |
| `tests/test_matrix_metadata.py`                     | Envelope round-trip, secrets         |
| `tests/test_meshtastic_renderer.py`                 | Prefix, rendering, config resolution |
| `tests/test_meshtastic_renderer_relations.py`       | Cross-platform reactions, prefix     |
| `tests/test_meshtastic_renderer_extra.py`           | Additional prefix edge cases         |
| `tests/test_meshtastic_renderer_channel_default.py` | Channel resolution                   |
| `tests/test_meshtastic_fallback_text.py`            | Fallback-text with prefix            |
| `tests/test_matrix_reaction_mmrelay.py`             | MMRelay reaction rendering           |
| `tests/test_matrix_reaction_mmrelay_mapped.py`      | Structured reaction key propagation  |
| `tests/test_reaction_roundtrip.py`                  | Cross-transport reaction fidelity    |
| `tests/test_reply_roundtrip.py`                     | Cross-transport reply fidelity       |
| `tests/test_bridge_rendering.py`                    | Bridge prefix + metadata rendering   |
| `tests/test_meshtastic_relation_mapping.py`         | Relation metadata propagation        |
| `tests/test_cross_adapter_relation_fallback.py`     | Cross-adapter relation degradation   |
| `tests/test_conversation_graph_cross_transport.py`  | Cross-transport identity tracking    |
| `tests/test_meshtastic_pipeline.py`                 | End-to-end prefix in pipeline        |
| `tests/test_meshtastic_fake_bridge.py`              | Fake bridge prefix verification      |
| `tests/test_meshtastic_fake_bridge_session.py`      | Session-level prefix behavior        |
| `tests/test_meshtastic_runtime_multi.py`            | Multi-radio prefix config            |
| `tests/test_meshtastic_target_selection_rules.py`   | Target selection with prefix         |
| `tests/test_meshtastic_boundaries.py`               | Boundary conditions including prefix |
| `tests/test_adapter_reuse_examples.py`              | Config reuse examples                |
| `tests/integration/test_meshtasticd_sdk_bridge.py`  | Docker SDK bridge prefix testing     |
