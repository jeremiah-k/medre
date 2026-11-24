# Explicit radio qualification

Provision Docker services before touching hardware. The lab's `provision` stage
creates native service accounts and conversations without opening a serial port
or Bluetooth connection. Run `hardware` separately when the configured board
pair is ready:

```bash
python -m scripts.adapter_campaign.lab test --directory PRIVATE_LAB --stage provision
python -m scripts.adapter_campaign.lab test --directory PRIVATE_LAB --stage hardware
```

MeshCore discovery probes each configured board once. The first communication
failure pauses the pair, leaves later boards unattempted and writes
`results/meshcore-paused.json`. Subsequent probes do not open either board while
that marker exists; campaigns omit the paused transport and report partial
coverage. Docker and other independently ready transports remain usable.

Stop MeshCore campaign attempts after a native communication/readiness failure
or witness timeout. Retire owned connections and ask the operator to re-pair the
boards. After the operator confirms that repair, acknowledge it explicitly:

```bash
python -m scripts.adapter_campaign.lab test --directory PRIVATE_LAB \
  --stage hardware --meshcore-repaired
```

The flag is accepted only for the hardware stage. It permits one new probe; a
further communication failure pauses the pair again. Do not keep scanning,
re-bonding or guessing controller addresses. This is a qualification workflow;
production adapters retain their independent self-healing policy.

Use one SDK owner per physical board. Two boards per radio transport provide one
MEDRE endpoint and one independent native witness. Multi-instance tests require
a separate topology or additional witnesses; do not open one board through
serial and BLE simultaneously to fabricate independent receipt evidence.
