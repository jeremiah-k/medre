# Sharing a Matrix Room with MMRelay

[MMRelay](https://github.com/jeremiah-k/meshtastic-matrix-relay) bridges a
Meshtastic mesh into Matrix rooms. MEDRE and MMRelay can serve the same room:
MEDRE ingests what the room carries and routes it to its other transports,
while MMRelay relays the room to its mesh. Each project documents its own
half — this page is what a MEDRE operator sets and observes; the MMRelay side
is documented in
[MMRelay's shared-rooms guide](https://github.com/jeremiah-k/meshtastic-matrix-relay/blob/main/docs/SHARED_ROOMS.md).

## What sharing a room means

Both bots join the room and both post to it. Every post carries its relay's
own attribution as visible text at the start of the body, so the room reads
like any other bridged room:

- MMRelay posts radio messages as `[{long}/{mesh}]: message`, using the
  sender's node name and meshnet label.
- MEDRE posts Matrix output as `[sender/origin]: message`, or
  `[sender]: message` when no origin label applies.

MEDRE recognizes its own posts and suppresses them as echoes. Posts from
MMRelay carry MEDRE's event envelope under the reserved `content.medre`
namespace, which lets a peer relay preserve sender names and match replies
and reactions across the room. The authored body always stays the visible
text: embedded metadata never replaces it, supplies identity, or authorizes
mutations.

Attribution chains across the room. Neither relay rewrites the text it
receives, so a message that crossed the room keeps the first relay's
attribution and gains the second's. A message from the MMRelay mesh that
MEDRE passes on to another transport reads `[sender/origin]: [node/mesh]:
message`; a Matrix message that MMRelay carries onto its mesh reads
`[node/mesh]: [sender/origin]: message`. The leading prefix always names the
relay that sent it to you, and the one inside names where it came from
before that.

## One relay per radio mesh

The recommended shared-room topology has **one bridge per radio mesh**.
Two relays attached to the same mesh and sharing a room can generate an
escalating echo cycle. Relay-side self-mesh checks may suppress particular
echoes, but they are not a guarantee for arbitrary mixed deployments. Keep
bridges on distinct meshes that meet only in the shared rooms. This reduces
loop risk, but does **not** prove exactly-once radio delivery or distributed
deduplication.

## What to set on MEDRE

Set the shared room in the Matrix adapter's `room_allowlist`. For an
encrypted room, use `encryption_mode: e2ee_required` and confirm that **both**
bridges have working device identity, room keys, and permissions; selecting
the setting does not prove either bridge can decrypt. For a deliberately
unencrypted room, use the separate plaintext procedure in the
[Matrix setup guide](transport-setup/matrix.md).
Connect the room to your other transports with
[guided configuration](guided-configuration.md) as for any other route.

MEDRE has no peer-relay trust list. Every message in an allowlisted room is
processed on its authored text, and embedded provenance — including a
`content.medre` envelope posted by any sender — is stored as an untrusted
claim that never authenticates the sender it names. This is why the loop
rule above is the real defense, and why trusting a bot account in MMRelay
does not vouch for the radio messages it forwards.

## What the MMRelay operator sets

On the MMRelay side, every instance sharing the room runs MMRelay 1.5.0 or
later and lists the exact Matrix user ID of every other relay bot in the
room — including this MEDRE bot — under `matrix.trusted_relays`. An empty
list is the default; entries match exactly, with no wildcards; malformed
entries fail `mmrelay config check`. The minimum version is a deployment
requirement, not a runtime version gate that excludes older instances.
MMRelay's guide shows the exact configuration and is the authority for
that side.

Give the MMRelay operator this bot's Matrix user ID. It is the account the
adapter logs in as — the `MEDRE_ADAPTER__<TOKEN>__USER_ID` override value,
or the `Account` line printed by:

```bash
medre adapter matrix auth status
```

## Verify the room in both directions

With `medre run` healthy and the room allowlisted:

- **Mesh to room.** Send a test message from a node on the MMRelay mesh. It
  appears in the room as an MMRelay post, prefixed with the node and meshnet
  names and sent by the MMRelay bot account. In `medre diagnostics`, the
  Matrix adapter's `inbound_published` counter rises.
- **Room to mesh.** Post in the room as an ordinary Matrix user — not from
  the MEDRE bot account, whose own messages MEDRE suppresses. MEDRE relays
  the post with its own attribution; MMRelay carries it onto its mesh
  behind MMRelay's prefix, as described above.

## What MEDRE ignores, and where it shows

MEDRE drops room events silently when they are not needed:

| Dropped event                                           | Where it shows                                                                         |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Room not in `room_allowlist`                            | `inbound_filtered_allowlist` diagnostic counter                                        |
| Encrypted-room event under `encryption_mode: plaintext` | `inbound_filtered_encryption_policy` counter and a debug log line                      |
| MEDRE's own posts (self-sender or own-envelope echoes)  | `inbound_suppressed_self` / `inbound_suppressed_envelope` counters and debug log lines |

A rising `inbound_filtered_allowlist` usually means a room ID in the
allowlist does not match the shared room, or the shared room is missing from
it.

## Drops and warnings on the MMRelay side

Two MMRelay warnings matter to a MEDRE operator, and both are fixed on the
MMRelay side:

- **"Dropping MEDRE relay claim from untrusted Matrix sender …"** — the
  MMRelay instance does not list this bot's Matrix user ID under
  `matrix.trusted_relays`. Verify the account, then have the MMRelay
  operator add it.
- **"Dropping invalid/incoherent MEDRE relay claim from trusted Matrix
  sender …"** — the claim arrived from a listed bot in a form the peer could
  not accept. Check that both sides run versions whose packages match; an
  MMRelay instance older than 1.5.0 in the room is the usual cause.

## Privacy notes

Relay events may carry stable Meshtastic node IDs in Matrix event metadata.
People and clients with access to the room can correlate those IDs across
Matrix rooms and radio meshes, even if the IDs are also broadcast over
radio. Trusting a relay's Matrix bot account does not authenticate its
claimed radio sender, verify that a displayed radio name is genuine, or
prove delivery to a radio node.

## Migrating a Matrix account from an MMRelay installation

MMRelay and the MEDRE Matrix adapter use the same Matrix SDK and store
layout, so an account that already ran in MMRelay can keep its signing
identity when it moves to MEDRE. Stop the MEDRE adapter first. Then locate
the account-signing JSON for the same Matrix account in the MMRelay store,
named after the account with a `_cross_signing.json` suffix — for example
`~/.mmrelay/matrix/store/@bot:example.org_cross_signing.json` — and import
it with MEDRE's credentials:

```bash
medre adapter matrix auth restore-signing --adapter-id bridge \
  --config ~/medre/bridge.yaml \
  --from ~/.mmrelay/matrix/store/@bot:example.org_cross_signing.json
```

The import verifies the saved token's account and device against the
homeserver, matches the backup's keys against the existing server identity,
and publishes only when the destination signing file does not exist yet, so
an existing file is never replaced. The source backup is left unchanged; the
filesystem must support hard links. Import restores account-signing keys,
not historical encrypted-room message keys. Do not copy tokens or device
databases between applications — move only the signing file.

## Limitations

- Embedded provenance is a claim, never evidence: MEDRE cannot authenticate
  the radio sender a relay names, and there is no peer-relay allowlist to
  configure.
- The shared-room qualification covered one room family across three meshes
  with two MMRelay instances and one MEDRE instance; a room family larger
  than that has the same one-relay-per-mesh requirement but has not been
  exercised by that campaign.
- MEDRE relays what its transports observe into mapped rooms and provides no
  peer-suppression surface for radio-origin echoes — the loop rule is
  deployment-level, not a MEDRE setting.

## Reference

- [Matrix setup guide](transport-setup/matrix.md) — the adapter this page
  configures
- [Meshtastic setup guide](transport-setup/meshtastic.md) — MEDRE's own radio
  side, for deployments where MEDRE owns a mesh
- [Relay attribution and sender claims](../spec/relay-attribution.md) and
  [Matrix event shape, section 6](../spec/matrix-event-shape.md) — the
  envelope and the untrusted-claim boundary
- [MMRelay compatibility boundary](../spec/compatibility-boundaries.md) —
  why MEDRE ignores MMRelay's historical flat `meshtastic_*` content fields
- [MMRelay deployment constraints](https://github.com/jeremiah-k/medre-adapter-matrix/blob/main/docs/spec/deployment-constraints.md) —
  the one-bridge-per-mesh qualification
- [MMRelay's shared-rooms guide](https://github.com/jeremiah-k/meshtastic-matrix-relay/blob/main/docs/SHARED_ROOMS.md) —
  the other half: `matrix.trusted_relays`, drop warnings, and loop guidance
  on the MMRelay side
