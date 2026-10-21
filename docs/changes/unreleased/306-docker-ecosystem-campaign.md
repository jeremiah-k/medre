# Repeat native adapter and OAuth campaigns in an isolated Docker bench

The campaign lab provisions digest-pinned Matrix/MAS, XMPP, SMTP/IMAP, SimpleX
relay, and Reticulum TCP fixtures around the existing native peer runner and
evidence analyzer. Explicit candidate checkouts are snapshotted with commit
identities and content digests without changing compatibility pins.

Separate MEDRE and MMRelay environments exercise browser authorization, device
signing, concurrent refresh, logout recovery, message presentation, and optional
serial/BLE hardware. Fixtures declare independent sender and channel
expectations; reports distinguish incomplete coverage from successful delivery.
Provisioning retries preserve successful fixtures while retrying failed
transports. Portable report paths and timestamp/resource-leak checks retain
private evidence for diagnosis.

Sanitized profiles and a runbook describe repeatable setup, isolation, evidence
retention, cleanup, and the native tiers that require further qualification.
