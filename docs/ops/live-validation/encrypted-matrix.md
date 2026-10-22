# Encrypted Matrix qualification

The optional Docker lab `encrypted` stage qualifies actual encrypted messages
and files through two production Matrix adapters. An independent native SDK
client joins both rooms, sends source events, decrypts the relayed events, and
fetches the original server events to prove their encrypted wire representation.
Successful outbox delivery alone cannot pass this tier.

After [preparing, building, and starting the lab](docker-ecosystem.md), run:

```console
python -m scripts.adapter_campaign.lab test \
  --directory /private/bench/campaign-001 --stage encrypted
python -m scripts.adapter_campaign.lab test \
  --directory /private/bench/campaign-001 --stage logs
python -m scripts.adapter_campaign.lab down \
  --directory /private/bench/campaign-001
```

Always run `down`, including when qualification fails. Keep the directory private
and persistent. It contains disposable credentials, crypto stores, retained
plaintext attachments, native captures, and server logs. These are local evidence,
not files to commit or publish.

## What a passing run establishes

Six cases cover text and binary files in both directions, then further text and
file delivery after a graceful runtime restart. The two relay devices and the
independent witness each have their own identity and exclusive crypto store.
Each configured room requires encryption. No runtime or witness shares a live
SDK client or store with another process.

Each case requires:

- An acknowledged native source event and exactly one decrypted event in each
  declared room, with the expected sender, device, caption, and relay prefix.
- An independently fetched `m.room.encrypted` wire event using Megolm, with no
  plaintext message body or media locator in its content.
- For files, an encrypted upload that the witness downloads and decrypts into
  the original deterministic bytes, filename, MIME type, and measured size.
- A single canonical ingress event, correlated native references, one terminal
  live outbox row and receipt, and independently configured channel identities.
- Association-scoped retained file bytes matching the original input, including
  after restart, without transient media keys or IVs in canonical payloads.
- Stable public device keys, immutable captured durable evidence, complete
  stdout/stderr captures, and clean process/resource shutdown.

Key exchange or decryption timeouts fail the stage; they never become `xfail`.
Waits and evidence streams are bounded. The fixed prefix isolates encrypted
transport correctness from the separate default-attribution presentation tier.

## Trust and qualification boundaries

The witness and adapter allow unverified peer devices, matching the adapter's
peer-trust policy. Successful decryption does not establish verified-user trust.
The stage uses disposable password sessions; OAuth renewal and own-device
cross-signing are qualified by the separate `oauth` and `recovery` stages. It
neither resets cross-signing nor changes production retry or trust settings.

This tier tests a graceful restart, not a crash during upload, key backup recovery,
or arbitrary homeserver outages. An interrupted upload/send can leave unused
remote media. Bounded observation does not prove exactly-once delivery.

## Recheck retained evidence offline

Find the completed `results/encrypted-<run>` directory, then run from the core
checkout with its development environment:

```console
python -m scripts.analyze_encrypted_campaign \
  /private/bench/campaign-001/results/encrypted-<run>
```

The analyzer opens SQLite read-only and rechecks native captures, independently
derived fixture bytes, channel identities, source/destination correlation,
restart chronology, and logs. It needs no credentials, native SDK, or server.
The opt-in pytest gate accepts the same directory through
`MEDRE_ENCRYPTED_CAMPAIGN`:

```console
pytest -m live tests/test_native_encrypted_campaign_live.py
```
