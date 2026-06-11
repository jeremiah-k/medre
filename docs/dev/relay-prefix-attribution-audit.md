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
message body via `_apply_matrix_relay_prefix` (lines 613–663).

**Prefix configuration source:** The prefix template is NOT on
`MatrixConfig`. It comes from the **source adapter's** config —
specifically `MeshtasticConfig.matrix_relay_prefix`. The renderer
resolves it by looking up `event.source_adapter` in the
`source_configs` mapping supplied at renderer construction (lines 74–84
and 97–106).

**Available template variables** (all coalesced to empty string on
`None`):

| Variable         | Source                                     |
| ---------------- | ------------------------------------------ |
| `{longname}`     | `event.metadata.native.data["longname"]`   |
| `{shortname}`    | `event.metadata.native.data["shortname"]`  |
| `{shortname5}`   | First 5 chars of `shortname`, or `from_id` |
| `{meshnet_name}` | Source adapter config `meshnet_name`       |
| `{from_id}`      | `event.metadata.native.data["from_id"]`    |

**Default prefix** (from `MeshtasticConfig`):
`"[{longname}/{meshnet_name}]: "` — documented as matching mmrelay's
`DEFAULT_MATRIX_PREFIX = "[{long}/{mesh}]: "`.

**Application points:**

1. Direct mode body (renderer.py line 210)
2. Fallback-text mode body (renderer.py line 312)
3. Reaction emote body via `_format_reaction_prefix` (renderer.py lines
   469–496)

The prefix is applied **before** any truncation.

**When no source config is found** (e.g. event from an adapter not in
the `source_configs` mapping), the prefix resolves to empty string and
no prefix is prepended.

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

| Injected key           | Value source                   |
| ---------------------- | ------------------------------ |
| `meshtastic_id`        | `native_data["packet_id"]`     |
| `meshtastic_longname`  | `native_data["longname"]`      |
| `meshtastic_shortname` | `native_data["shortname"]`     |
| `meshtastic_meshnet`   | `config.meshnet_name`          |
| `meshtastic_portnum`   | Hardcoded `"TEXT_MESSAGE_APP"` |
| `meshtastic_text`      | Event payload `text` or `body` |

MMRelay injection is **skipped for reactions** (reaction rendering
handles its own MMRelay keys).

### MatrixConfig Prefix-Relevant Fields

`src/medre/config/adapters/matrix.py`: `MatrixConfig` has **no prefix
template fields**. No `matrix_relay_prefix`, no `radio_relay_prefix`,
no `mmrelay_compatibility`.

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

**Available template variables:**

| Variable         | Source                                            |
| ---------------- | ------------------------------------------------- |
| `{longname}`     | `native_data["longname"]`                         |
| `{shortname}`    | `native_data["shortname"]`                        |
| `{shortname5}`   | First 5 chars of `shortname`, or `from_id`        |
| `{meshnet_name}` | Target adapter config `meshnet_name`              |
| `{from_id}`      | `native_data["from_id"]` or `source_transport_id` |

**Default prefix:** `"{shortname5}[M]: "` — documented as matching
mmrelay's `DEFAULT_MESHTASTIC_PREFIX = "{display5}[M]: "`.

**compact mode:** Used for cross-platform reactions. Strips spaces
from `longname` and `shortname` before template substitution.

**Prefix is NOT applied for:**

- Structured native reactions (`emoji=1`, same-adapter source)
- Descriptive cross-platform reactions (which embed their own compact
  prefix in the text body)

Prefix is applied **before** UTF-8 byte-budget truncation.

### MeshtasticConfig Prefix-Relevant Fields

`src/medre/config/adapters/meshtastic.py`:

| Field                   | Default                           | Purpose                                          |
| ----------------------- | --------------------------------- | ------------------------------------------------ |
| `matrix_relay_prefix`   | `"[{longname}/{meshnet_name}]: "` | Template for mesh→Matrix body prefix             |
| `radio_relay_prefix`    | `"{shortname5}[M]: "`             | Template for Matrix→mesh body prefix             |
| `mmrelay_compatibility` | `False`                           | Inject MMRelay mesh metadata into Matrix content |
| `meshnet_name`          | `""`                              | Available as `{meshnet_name}` in both prefixes   |
| `max_text_bytes`        | `227`                             | UTF-8 byte budget after rendering                |

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
`src/medre/adapters/meshcore/renderer.py`, lines 59–296) has **no
relay prefix support**. There is no `_format_prefix_for` method, no
prefix template configuration, and no prefix prepend logic.

Rendering flow: extract text → apply fallback-text degradation if
needed → UTF-8 byte-budget truncation → output payload.

### MeshCoreConfig Prefix-Relevant Fields

`src/medre/config/adapters/meshcore.py`: **No prefix template fields.**

| Field            | Default | Note                                                         |
| ---------------- | ------- | ------------------------------------------------------------ |
| `meshnet_name`   | `""`    | Included in payload dict but not used as a template variable |
| `max_text_bytes` | `512`   | UTF-8 byte budget                                            |

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
`src/medre/adapters/lxmf/renderer.py`, lines 43–214) has **no relay
prefix support**. There is no prefix template configuration or prefix
prepend logic.

Rendering flow: extract text + title → embed MEDRE envelope in fields →
degrade relations inline if fallback_text → character truncation via
`max_text_chars` → output payload.

### LxmfConfig Prefix-Relevant Fields

`src/medre/config/adapters/lxmf.py`: **No prefix template fields.**

| Field                | Default | Note                                     |
| -------------------- | ------- | ---------------------------------------- |
| `meshnet_name`       | `""`    | Present on config but unused by renderer |
| `metadata_embedding` | `True`  | Controls MEDRE envelope in fields        |
| `display_name`       | `""`    | For LXMF announces, not prefixing        |

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

## Cross-Transport Gaps

### 1. No prefix support on MeshCore or LXMF

MeshCore and LXMF have no relay prefix template config, no prefix
rendering logic, and no `_format_prefix_for` equivalent. Messages
relayed from MeshCore or LXMF to any other transport arrive without a
sender attribution prefix.

### 2. No `longname`/`shortname` on MeshCore or LXMF native metadata

MeshCore uses `meshcore.sender_id` (hex pubkey prefix). LXMF uses
`source_hash` (hex identity hash). Neither populates `longname`,
`shortname`, or `from_id` in native metadata.

Consequence: prefix templates referencing `{longname}` or `{shortname}`
resolve to empty string when the source is MeshCore or LXMF.

### 3. Prefix config ownership is asymmetric

Both `matrix_relay_prefix` and `radio_relay_prefix` live exclusively on
`MeshtasticConfig`. The Matrix renderer resolves the prefix from the
source adapter's config. MatrixConfig, MeshCoreConfig, and LxmfConfig
have no prefix fields. If MeshCore or LXMF needed prefix behavior, a
new config field would be required.

### 4. Matrix display-name enrichment is post-codec

The Matrix adapter enriches `longname`/`shortname` from Matrix display
names **after** codec decode, in `_on_room_message`. This means any
code path that uses codec output directly (without passing through the
adapter) will not have display-name attribution.

### 5. Metadata key namespace inconsistency

Meshtastic native metadata uses flat keys: `longname`, `shortname`,
`from_id`, `packet_id`, `channel`. MeshCore uses namespaced keys:
`meshcore.packet_id`, `meshcore.sender_id`, `meshcore.channel`. LXMF
uses yet another set: `source_hash`, `destination_hash`, `message_id`.
Matrix uses flat keys matching MMRelay: `sender`, `room_id`, `event_id`,
plus captured `meshtastic_*` keys.

Prefix template variables (`{longname}`, `{shortname}`, `{from_id}`)
work for Meshtastic sources and (after enrichment) Matrix sources. They
resolve to empty strings for MeshCore and LXMF sources.

### 6. `mmrelay_compatibility` is Meshtastic-only

Only `MeshtasticConfig` has the `mmrelay_compatibility` flag. The
Matrix renderer checks this flag on the source adapter's config to
decide MMRelay metadata injection. There is no equivalent mechanism
for MeshCore or LXMF sources, and no MMRelay-compatible metadata is
generated for those transports.

### 7. `shortname5` derivation differs by context

In the Meshtastic renderer, `shortname5` falls back to `from_id` or
`source_transport_id`. In the Matrix renderer, it falls back to
`from_id` from native metadata. For Matrix-origin events, the adapter
enrichment derives `shortname` from display name (first 5 chars) or MXID
localpart (first 5 chars). For MeshCore-origin events, there is no
`shortname` at all, so `shortname5` would be empty or fall back to
`from_id` (which is the hex pubkey prefix).

### 8. No cross-transport name resolution

There is no mechanism to resolve a human-readable name from a MeshCore
pubkey prefix or LXMF identity hash into `longname`/`shortname` for
downstream prefix templates. Node info lookup exists only for
Meshtastic (via the SDK node database).

---

## Inspected Files

### Source code

| File                                        | Lines inspected  |
| ------------------------------------------- | ---------------- |
| `src/medre/config/adapters/meshtastic.py`   | Full (264 lines) |
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
