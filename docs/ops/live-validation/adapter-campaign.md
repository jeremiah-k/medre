# Cross-adapter message campaign

Use the native campaign to pass messages from every adapter through MEDRE to
independent peers on every other adapter. The default topology includes Matrix,
Meshtastic, MeshCore, LXMF, XMPP, Chatmail, and SimpleX Chat: seven source sends exercise all 42
directed pairs. Matrix rooms, XMPP MUCs, Chatmail groups, and SimpleX groups are the messaging
fixtures. Radio peers use the configured private channels or LXMF destinations.

The runner preserves the native bench pattern: keep the runtime and peers alive
for the conversation, send commands to peer processes over stdin, collect JSONL
observations, and retain the core SQLite database. It creates channel-scoped
routes from the declared topology; expected destinations never come from the
live router's results.

## Prepare the lab

Install the selected providers and their native SDKs in the core development
environment. Use [dev sync](../../dev/environment.md#work-on-adapters-at-any-location)
for local editable checkouts at any location. Candidate sources belong in a local
profile/catalog; do not change committed compatibility pins to run the bench.

For each transport, provide one MEDRE endpoint and one independent native peer:

| Transport    | MEDRE endpoint                                             | Independent peer                                     | Expected sender at the peer                                 |
| ------------ | ---------------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------- |
| Matrix       | Bot joined to a plaintext test room                        | Another joined account                               | Bot Matrix user ID.                                         |
| Meshtastic   | Private-channel radio                                      | Second radio on the same channel/preset              | Bot node number as a decimal string.                        |
| MeshCore     | Companion radio                                            | Second companion on the same private channel         | Bot's wire-text sender prefix, without the colon and space. |
| LXMF         | Delivery identity with an isolated Reticulum configuration | Second identity and separate Reticulum configuration | Bot delivery destination hash.                              |
| XMPP         | Bot joined to an existing unlocked MUC                     | Another account with its own nickname                | Full `room@service/bot-nickname` occupant JID.              |
| Chatmail     | Configured account in an accepted group                    | Another profile in the same accepted group           | Bot contact ID in the peer profile, as a decimal string.    |
| SimpleX Chat | Initialized profile in an accepted group                   | Separate official-SDK profile in the same group      | Bot groupMemberId in the peer profile, as a decimal string. |

Use dedicated test rooms, profiles, and channels. Each radio belongs exclusively
to either MEDRE or its peer for the run. Stop other bridges using those devices.
Peer helpers preserve persistent radio settings. Reticulum applies the explicitly
supplied RNode configuration and identity; prepare matching parameters beforehand.
The runner does not provision servers or change host services, networking,
Bluetooth pairing, certificates, or firewall rules.

Chatmail's bot and peer require separate accounts directories. Their local group
IDs can differ: the endpoint channel is the bot's ID, while peer.channel is the
peer's ID. Discover IDs with `medre-chatmail chats --groups --json` in each profile.
The native mail service can be a preconfigured Chatmail relay or a loopback
SMTP/IMAP fixture. Record which service was used; the latter proves native core
interop without qualifying the official Chatmail Docker deployment.

SimpleX requires two explicitly initialized native profiles in the same accepted
group. Provision them using the official client and exchange a private group
link; keep the link and profile keys outside Git. The peer helper uses the
official Python SDK directly, independently of MEDRE's adapter and worker.
Set peer.database_prefix, peer.user_id, and peer.channel (for example group:3).
Group IDs and member IDs are local to each profile: expected_sender is the
bot's groupMemberId **as seen by the peer**, discoverable through the official
SDK's api_list_members. The bot and peer must not open the same native database.
Native receipt is separate evidence from the adapter's local_queue handoff.

## Configure and run

Copy [campaign.example.yaml](fixtures/campaign.example.yaml) as `campaign.yaml`
and [runtime.example.yaml](fixtures/runtime.example.yaml) as `runtime.yaml` into
a private directory, then replace the endpoint and peer-display-name placeholders. Keep the files outside Git.
The campaign runtime config accepts ordinary MEDRE adapter settings. Its routes,
core storage, retry policy, logging, and MEDRE paths are replaced for the run;
explicit provider profile, identity, and storage paths retain their meaning.

Preview the topology and validate runtime settings without connecting or sending:

```bash
uv run --no-sync python -m scripts.adapter_campaign \
  --config /private/bench/campaign.yaml
```

Permit bounded native sends and retain evidence in a fresh directory:

```bash
uv run --no-sync python -m scripts.adapter_campaign \
  --config /private/bench/campaign.yaml \
  --output /private/bench/run-001 --run
```

The runner refuses an existing output directory. For a lab with only some
endpoints ready, select an explicit subset, for example `--only matrix,xmpp,chatmail`.
The report labels that run partial and lists exactly the six directed pairs it
covered. Omitting --only requires exactly one endpoint for each of the seven
transports. Missing endpoints or unavailable peers fail the run.

The equivalent opt-in pytest entry point is:

```bash
MEDRE_ADAPTER_CAMPAIGN=/private/bench/campaign.yaml \
MEDRE_ADAPTER_CAMPAIGN_OUTPUT=/private/bench/run-002 \
uv run --no-sync pytest tests/test_native_adapter_campaign_live.py \
  -m 'live and hardware' -s -v
```

Set MEDRE_ADAPTER_CAMPAIGN_ONLY for an explicit pytest subset. Ordinary pytest
runs exclude this tier. For a private lab CA, pass SSL_CERT_FILE pointing to its
certificate bundle; do not disable certificate verification.

## Interpret the evidence

A source turn passes only after all expected peers receive its nonce and MEDRE
has one canonical event, one terminal outbox item per target, and matching live
sent receipts with attempt provenance. Queued receipts are retained as lifecycle
history, including deferred radio and SMTP handoffs. Sender and conversation
checks prevent an echo or an unrelated participant from satisfying native receipt.
Every selected directed pair requires a fixture-owned `expected_prefixes` entry
on its target. Empty strings explicitly qualify prefix-free output. The oracle
compares the complete visible body, including long Unicode truncation against
the runtime radio capability budget. MeshCore native sender headers are checked
separately from rendered body text and retained in raw peer observations.

After the final turn, an observation window checks late duplicates and canonical
echoes. Every peer and MEDRE must stop cleanly before the report becomes passed.
Radio runs enforce at least 2.5 seconds between source turns. This is a small
connectivity campaign, not a load, restart, attachment, or relation conformance run.
Run provider-owned recovery tiers separately; a successful text campaign cannot
qualify restart recovery or native feature support.

The output directory has mode 0700 and contains:

- results.json with full/partial coverage, expected/completed pairs, fixture-derived
  native sender/conversation and source-channel expectations, package versions,
  harness/config digests, native observations, and durable core evidence.
- medre.sqlite3 and isolated runtime paths for storage inspection.
- One JSONL command/observation stream and private SDK stdout/stderr files per peer.
- readiness.json with peer identities, interface state, and adapter diagnostics.
- failure.txt with private exception details when the campaign fails.

Only observations containing this run's nonce prefix enter peer JSONL files.
SQLite state and SDK diagnostics can contain private data; keep the entire
directory private. Credentials travel to child processes over stdin and are not
recorded in command arguments or the JSON report.

The default suite also runs test_all_adapter_campaign.py with SDK-free providers:
42 individually named directed-pair cases and a fanout case for each source,
including SimpleX Chat. The native campaign uses independent peers for all seven
providers; a missing SimpleX fixture requires an explicit six-transport --only
selection and reports partial coverage.
Unavailable provider cases are explicitly skipped. This tier checks routing,
renderers, storage, addressing, and delivery provenance; native receipt requires
the opt-in campaign above.

Plan validation prints only harness-authored messages. YAML/parser and native
exceptions remain private because their text can include credentials.

Peer cleanup bounds stdout draining and kills owned descendants that retain
pipes after their leader exits; forced cleanup prevents qualification. Cancelled
cleanup also reaps the owned process before closing evidence streams.

Every run creates an initial results report before collecting package versions
and config hashes; a metadata failure produces a failed report and private
failure trace.

## Verify visible presentation

Native receipt of a nonce alone does not qualify presentation. Each target in a
Matrix–Meshtastic pair must declare `expected_prefixes`, keyed by the source
adapter instance ID. The expectations come from the fixture's known peer names
and configured rendering policy, independently of renderer output. For example:

```yaml
# Within endpoints.matrix (the Matrix target):
expected_prefixes:
  radio: "[Alice Radio/Forx]: "
# Within endpoints.radio (the Meshtastic target):
expected_prefixes:
  matrix: "Alice[M]: "
```

Use the actual instance IDs and peer display names in the dedicated lab. An
explicit empty expectation verifies intentionally unprefixed output; omitting
an expectation for either reference-bridge direction fails plan validation.
Every other selected platform pair must also declare expected prefixes on its targets.

The campaign verifies the complete expected visible text, rejects duplicate or
incorrect prefixes, and compares Matrix formatted text with its plain body when
HTML is supplied. Matrix ingress includes a long Unicode body, so radio targets
must preserve the expected prefix while truncating the final composed text to
the configured byte budget without splitting UTF-8. Expected prefixes are saved
in the report, alongside the original sent text and independent native receipts.
This proves presentation as well as delivery; it does not require changing the
runtime's prefixes to satisfy the test.

## Repeat and analyze a campaign

Keep a private lab directory containing runtime.yaml and campaign.yaml. Prefer
relative runtime_config paths so copying the fixture does not silently select
another lab's configuration. Profile and radio identity paths can remain
absolute. Before each run, verify device ownership, server readiness, current
published ports, certificate expiry, group membership, and independently known
peer names. A running container alone does not establish server readiness.
Do not change host services, pairing, networking, or trust stores to run a lab.

Record the exact core/provider Git revisions and working-tree state before
starting. Use the checkout-local dev profile to select sources explicitly.
Finish synchronization and formatting first; keep the source and configuration
unchanged while the campaign runs. Preview the topology before permitting sends,
and use a fresh output directory for every attempt. Retain failed attempts so
fixture failures and implementation defects remain distinguishable.

Analyze a retained report without opening devices or native profiles:

```bash
uv run --no-sync python -m scripts.analyze_adapter_campaign /private/bench/run-001
```

The JSON summary checks every selected directed pair, nonempty and unambiguous
source nonces, fixture-derived native receipt identities and source/destination
channels, canonical source/outbox/receipt provenance, declared transport coverage,
and clean owner shutdown.
An incomplete, failed, malformed, or inconsistent report exits nonzero. Output
contains counts and harness-authored findings, without message bodies or native
credentials. This is an evidence consistency check, not a new native run or an
independent revalidation of the original presentation expectations. Keep the
original report and its source revisions together.

Run provider-owned SDK and recovery tests separately from connectivity. Chatmail's
live group tier exercises a real RPC process crash, reconnect, stopped-adapter
recovery, quotes, and an independently observed reply. SimpleX's SDK tier checks
local queue persistence, duplicate suppression, and worker failure; the shared
native campaign supplies independent group receipts. Text qualification does
not establish attachments, typing, read receipts, encryption in every transport,
or recovery under sustained load.

In environments containing SDK pytest plugins, select the required plugins
explicitly to keep unrelated collection hooks out of a qualification run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --no-sync pytest \
  -p pytest_asyncio.plugin -p pytest_timeout tests/test_native_adapter_campaign_live.py \
  -m 'live and hardware' -s -v
```

For another project, preserve the same boundaries: declarative topology and
expected presentation, independent native peer drivers, durable evidence
verification, bounded process ownership, and offline analysis. Replace the
provider drivers and storage verifier rather than deriving expected results
from the implementation under test. Commit sanitized templates and executable
checks; keep profiles, tokens, message captures, SQLite databases, and run-specific
reports outside Git.
