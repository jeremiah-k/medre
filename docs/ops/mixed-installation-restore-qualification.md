# Mixed-installation restore qualification

The stopped-state snapshot is an **application-state** operation. It is not a
backup of radio firmware, remote Matrix accounts, or a shared network service.
Capture is meaningful only after the MEDRE runtime and local native owners
have stopped. Keep the original installation offline until the restored one
has been inspected and explicitly activated; a matching device identity on
two hosts is not fenced by local file locks.

## Ownership inventory

| Owner      | Source of truth                                                | Restored evidence                                                                                                                               |
| ---------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| MEDRE core | `medre.sqlite`                                                 | canonical events, native reply refs, outbox reservations, receipts, Matrix app-owned checkpoints, radio attempt progress                        |
| Matrix     | pinned nio account database under its `.ingest.lock` exclusion | cryptographic account/device identity, durable SDK stream and native trust material; credentials only if the configured XDG sidecar is included |
| Meshtastic | firmware on the configured radio                               | **not exported** in this file snapshot; identify and verify the same device separately                                                          |
| MeshCore   | firmware on the configured radio                               | **not exported**; verify node identity and available radio before restart                                                                       |

`capture-config` requires a single common root containing **both** the core
database and selected Matrix native files, including the credentials sidecar
where applicable. Default XDG config and state locations may be siblings
under the user's home directory. Choose a root that includes them without
inadvertently distributing unrelated files. The snapshot contains secrets:
protect its directory and do not attach it to public bug reports.

## Local campaigns

The SDK-free mixed-installation test composes all three actual adapter
stopped-state declarations with real core SQLite, captures the selection,
restores it and verifies the native reference, application checkpoints,
reserved radio outbox and `submitting` radio part remain unchanged:

```shell
python -m pytest -o addopts= -m local_integration \
  tests/test_mixed_stopped_installation.py -v
```

The Matrix adapter's `matrix_sdk` campaign additionally constructs and reopens
a **real pinned nio** ordinary or durable account while retaining core
correlation, checkpoint and radio-uncertainty evidence:

```shell
python -m pytest -o addopts= -m matrix_sdk \
  tests/test_stopped_capture_sdk.py -v
```

These tests use disposable private identities. They do not establish that
messages were delivered over a live remote Matrix server or over radio. Radio
part `submitting` is _uncertain_ after a crash: restored evidence must never
be mistaken for authorization to resend the packet. Matrix native IDs remain
usable for correlation but do not prove recipient delivery.

## Physical restore sign-off (not claimed by these tests)

Stop MEDRE, quiesce actual Matrix and radio owners, and capture. Verify the
snapshot, restore to a **new** root and check Matrix device identity and
crypto trust, the durable sync cursor, canonical/native reply refs, each
outbox status, and radio attempt histories before starting transport I/O.
Check the physical radios' identity and firmware state separately. Test reply
correlation through the same rooms/channels and demonstrate that uncertain
native handoffs remain held. Never simultaneously activate the original and
restored Matrix device. Run this with sanitized observations, not secrets.
