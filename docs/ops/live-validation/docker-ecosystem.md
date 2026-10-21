# Docker ecosystem campaigns

Use `python -m scripts.adapter_campaign.lab` from the core checkout to prepare
an isolated MEDRE and MMRelay test bench. It reuses the
[native campaign runner](adapter-campaign.md) and its offline evidence analyzer.
Candidate checkouts can live anywhere; committed compatibility pins stay on
qualified, merged revisions.

The bench runs Synapse, Matrix Authentication Service (MAS), a TLS proxy,
PostgreSQL, Prosody, SMTP/IMAP, a SimpleX SMP relay, and test processes in Docker.
MEDRE and MMRelay use separate environments so their Matrix SDK versions do not
replace one another. Source snapshots contain tracked files, commit identities,
dirty-state indicators, and content digests. Untracked credentials, local state,
and virtual environments are excluded.

## Prepare the bench

Use a local Linux Docker engine with Compose, Python, and the core development
environment. The native image is qualified on Linux x86-64. Hardware testing
requires permission to open the selected serial devices and, for MeshCore BLE,
access to the host's existing BlueZ service. Stop competing bridges first; only
one runtime can own a gateway radio at a time.

Copy [the development profile](fixtures/docker-dev.example.yaml) outside the
repository and replace its paths. This is the ordinary MEDRE development profile
format, with all seven adapters selected as explicit editable candidates.
Select the MMRelay candidate separately:

```console
python -m scripts.adapter_campaign.lab prepare \
  --directory /private/bench/campaign-001 \
  --profile /private/bench/dev.yaml \
  --mmrelay /checkouts/mmrelay-candidate
```

Keep campaign directories on persistent storage, such as `/private/bench`;
`/tmp` can be cleared during a reboot. Candidate worktrees can live anywhere.

The directory must be fresh; its parent must already exist. Source checkouts must
be clean. For deliberate worktree candidates, stage new files and pass
`--allow-dirty`; the snapshot records the tracked worktree bytes rather than
claiming they equal the commit. Each lab gets its own random Compose project,
accounts, passwords, TLS authority, volumes, and native profiles.

For hardware coverage, copy [the hardware fixture](fixtures/docker-hardware.example.json)
outside the repository, replace its identifiers, and add
`--hardware /private/bench/hardware.json` to `prepare`. Omit a transport's entry
when that hardware is intentionally unavailable. Devices are inspected before
use; the harness does not provision radio settings, flash firmware, pair BLE
devices, or modify host services and certificate trust.

If a previously used MeshCore board reports a BLE operation in progress, stop
competing clients and explicitly disconnect only that board before retrying:

```console
bluetoothctl disconnect AA:BB:CC:DD:EE:01
```

Use the configured board address. This does not remove its pairing or reset the
host Bluetooth service. Native probes and peers scan for a live BLE device and
close their owned client even when connection startup fails.

## Run and clean up

```console
lab_dir=/private/bench/campaign-001
python -m scripts.adapter_campaign.lab build --directory "$lab_dir"
python -m scripts.adapter_campaign.lab up --directory "$lab_dir"
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage all
python -m scripts.adapter_campaign.lab down --directory "$lab_dir"
```

Fresh lab certificates have a 90-day validity. Previously generated certificates
are not rotated implicitly; prepare a new lab after expiry.

The `contracts` stage runs provider suites sequentially and its host/child
timeouts scale with the number of recorded sources. Grouped stages allow enough
time for these child budgets and still require an explicit `down` on failure.

Always run `down`, including after a failed stage. It captures service logs and
removes only the matching lab project's containers and named volumes. Private
profiles, snapshots, logs, and results remain for analysis. A destroyed lab cannot
be restarted with stale accounts against empty service volumes; prepare a fresh
directory instead.

Individual stages support investigation without repeating every check:

| Stage       | Evidence                                                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `contracts` | Focused core and MMRelay regressions; all adapter-owned offline and installed-SDK tests. Hardware, external service, and soak tiers stay gated.              |
| `oauth`     | Real browser login, native device signing, cryptographic verification of the published chain, routine device/master-key reuse, and cross-process refresh.    |
| `provision` | Independent native accounts and conversations, secure Chatmail group joining, local SimpleX group joining, LXMF identities, and read-only hardware outcomes. |
| `campaign`  | Native fanout and presentation, durable core evidence, source correlation, late duplicates, and bounded cleanup.                                             |
| `mmrelay`   | The actual MMRelay process, independent Matrix and serial radio peers, default prefixes, HTML/plain-text parity, and UTF-8 truncation.                       |
| `recovery`  | Native logout, rejected revoked refresh grants, retained encryption stores/signing keys, and a new device signing without an account reset.                  |
| `logs`      | Timestamps on managed runtime records and evidence of unclosed sessions, connectors, or pending tasks; warning/error counts remain available for inspection. |

Use `--stage native` to run the native stages together after the contract tier
has passed. Without configured Meshtastic hardware, the grouped run records
MMRelay's RF tier as `not_configured`; its OAuth and recovery tiers still run.
`results/qualification.json` distinguishes partial coverage from complete
qualification.

OAuth setup is required before provisioning the campaign's Matrix room. Provision
before `campaign` or `mmrelay`. Run radio campaigns sequentially. The `all` stage
does this ordering and retains failures while continuing independent checks.
Initial MMRelay signing uses its explicit reset authorization only once in a fresh
lab profile. The reset attempt is recorded before execution, so even an interrupted
first signing or a later logout cannot silently authorize another account-key
replacement. If initial signing fails, investigate or prepare a fresh lab rather
than deleting the reset marker. Routine login and recovery checks omit reset. No
operator account is used. Refresh tests make the saved local renewal deadline due
and exercise a real server refresh grant; they do not claim to simulate natural
token expiry.

## Isolation and coverage

Services publish no host ports. The generated network has no external egress;
SimpleX queues use the local SMP relay. Browser and Python clients verify TLS
against the lab authority inside their containers. The authority is never added
to the host trust store. The official SimpleX native library is resolved while
building the image rather than downloaded during qualification.

Hardware access is explicit. Only selected serial character devices and their
group IDs enter the runner. Enabling MeshCore adds the BlueZ D-Bus socket and a
container-only `apparmor:unconfined` setting because Docker's default profile
blocks D-Bus subscriptions. Capabilities remain dropped, and no host profile is
loaded or changed. Omit MeshCore if that container permission is unsuitable.

Chatmail uses its real RPC client/server against the Docker SMTP/IMAP fixture.
This qualifies native messaging and groups, rather than deployment of the
official Chatmail mail-server image. That deployment has separate host-level
requirements. LXMF uses native Reticulum over a container-local TCP link; this
does not qualify an RNode RF path. MeshCore requires both BLE peers; one reachable
board is reported as partial coverage, never a successful two-peer RF test.

Expected senders and channels come from independently provisioned identities and
group membership, not from the outbox under examination. In Chatmail, the secure
group's key-contact ID can differ from an email address contact's ID. Rendering
expectations are fixture-authored, including the compact LXMF sender label on
Meshtastic and the Matrix display-name abbreviation. Peer synchronization
publishes LXMF identity announcements after the bridge becomes reachable, before
measured sends; periodic announces do not establish this presentation precondition.

## Read the evidence

The lab root is mode `0700`. Treat everything beneath it as private: configuration
includes disposable credentials, native profiles include private keys, and raw
logs and browser failure snapshots can contain temporary authorization codes.
Commit the harness and sanitized templates, not generated evidence.

- `sources.json` and `images.json` identify candidates and image digests.
- `actions.jsonl` records bounded command outcomes and private log paths without
  recording command arguments or passwords.
- `results/*-contracts.xml` holds per-project JUnit results.
- `results/*-oauth.json` and `*-recovery.json` hold native authentication checks.
- `results/hardware.json` records independent gateway/peer availability.
- `results/coverage.json` records selected and excluded transports.
- `results/native-*/` contains the existing campaign report, native peer streams,
  canonical storage, and any private failure traceback.
- `results/mmrelay-native-*/` contains the MMRelay runtime log and peer evidence.

The existing analyzer can be run offline against a retained native campaign:

```console
python -m scripts.analyze_adapter_campaign /private/bench/campaign-001/results/native-RUN
```

An incomplete campaign remains incomplete even when some messages arrived.
Consult the active source and named peer timeout before changing fixtures. Do not
derive a replacement expectation from the observed message just to make it pass.

## Further fidelity campaigns

Text fanout does not establish parity for encrypted room traffic, attachments,
edits, replies, reactions, typing, or recovery under every transport fault.
Those need independent native witnesses and explicit expected outcomes too.
For Matrix and XMPP, extend the fixtures to examine canonical/native references,
sender attribution, formatting, relations, and transport metadata across both
directions. Use each protocol's standard extensions where applicable and keep
platform-specific details in the adapter-owned metadata namespace. Published
Matrix metadata is useful evidence; its presence alone does not prove that the
equivalent XMPP semantics survived the bridge.
