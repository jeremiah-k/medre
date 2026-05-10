# medre — Modular Event-driven Routing Engine

**Status: Pre-beta. Not production-ready. Not suitable for real workloads.**

medre is a Python library for routing text messages across heterogeneous mesh
and messaging transports. It provides a canonical event model, per-transport
adapter boundaries, and a pipeline that moves events through codec → renderer →
session → adapter with structured diagnostics at every stage.

medre is pre-beta and ships as importable Python components. The project is
building toward an operational runtime that owns its own process lifecycle, but
today you wire adapters into your own code. No server, API endpoint, or
deployment model is provided yet.


## What medre actually does

- Defines a frozen, schema-versioned `CanonicalEvent` (msgspec Struct) as the
  single unit of data flow through the pipeline.
- Provides four transport adapters — Matrix, Meshtastic, MeshCore, LXMF — each
  with its own codec, renderer, session, config, error types, and compat guard.
- Each adapter owns its own transport lifecycle: start, stop, deliver,
  health\_check, diagnostics. No external component touches the transport
  connection.
- Fake adapters for all four transports allow full pipeline testing without
  network access, radio hardware, or SDK installation.
- Structured diagnostics at every adapter boundary. Read-only snapshots. No
  secrets leak. No SDK objects cross the boundary.
- Delivery result contract: every `deliver()` returns an
  `AdapterDeliveryResult` recording what actually happened (success/failure,
  native message ID, retry count, timestamp).
- Session boundary contract: each session owns task lifecycle, retry budgets,
  reconnect policy, and resource cleanup. No cross-session coordination.
- Event bus, rendering pipeline, routing/planning layer, storage, and identity
  modules in `medre.core.*`.


## Supported transports

| Transport | Adapter path | Live-validated | Maturity |
|-----------|-------------|----------------|----------|
| **Matrix** | Synapse homeservers via `mindroom-nio` | Yes | Beta-candidate |
| **Meshtastic** | LoRa mesh nodes via `mtjk` (serial/TCP) | Yes | Beta-candidate |
| **MeshCore** | MeshCore radio nodes via `meshcore_py` | No | Alpha-operational |
| **LXMF** | Reticulum / LXMF mesh via `lxmf` + `rns` | No | Alpha-operational |

Per-transport maturity definitions and live test evidence:
[docs/contracts/37-transport-maturity-classification.md](docs/contracts/37-transport-maturity-classification.md).

**Live-validated means smoke-tested once against a real endpoint.** It does not
mean sustained, reliable, or production-tested.


## Fake vs. live transports

Every transport has a fake adapter (`FakeMatrixAdapter`, `FakeMeshtasticAdapter`,
`FakeMeshCoreAdapter`, `FakeLxmfAdapter`). These:

- Accept the same config as the real adapter but ignore network/hardware.
- Exercise the full pipeline: codec → renderer → session → adapter → delivery
  result → diagnostics.
- Are used by the unit test suite (2,000+ tests, all passing).
- Require zero optional dependencies.

The real adapters are gated behind optional dependency groups (see Installation).
If a dependency is missing, the adapter's compat module reports
`HAS_<SDK> = False` and the adapter cannot be instantiated.


## Matrix encrypted rooms

The Matrix adapter supports sending and receiving text messages in encrypted
Matrix rooms. Encryption is handled entirely by the Matrix client layer via
`mindroom-nio`, `vodozemac` (Rust Olm/Megolm implementation), and the Olm
double-ratchet protocol. medre does not implement its own encryption or
decryption.

To enable encrypted-room support:

```bash
pip install ".[matrix-e2e]"   # pulls in vodozemac
```

Binary wheels exist for common platforms (Linux x86\_64, macOS, Windows).
Alpine and ARM may require a Rust toolchain.

The Matrix adapter uses `ignore_unverified_devices=True` with no cross-signed
device verification. This trade-off is documented in
[docs/contracts/25-matrix-e2ee-readiness.md](docs/contracts/25-matrix-e2ee-readiness.md).
The crypto store persists across restarts via `restore_login`.

medre does not provide a cross-transport E2EE abstraction. Each transport's
encryption story is its own: radio transports (Meshtastic, MeshCore, LXMF) use
link-layer or protocol-level encryption managed by their respective stacks, not
by medre.


## Known limitations

These are not bugs. They are honest boundaries of what medre covers today.

- **Fire-and-forget radio delivery.** Meshtastic, MeshCore, and LXMF adapters
  report `success=True` when the local radio or router accepts the message.
  Remote receipt is not confirmed. This is inherent to the protocols, not a
  medre defect. See
  [docs/contracts/36-radio-limitations.md](docs/contracts/36-radio-limitations.md).
- **Duplicate sends.** Meshtastic and MeshCore sessions retry transient failures
  (up to 3 attempts). Duplicates are possible. Consumers must deduplicate.
- **No sustained throughput testing.** Live tests are smoke tests (send a
  message, verify it arrives). No load testing exists.
- **No reconnect resilience testing.** No live test exercises adapter behavior
  during real network failures.
- **Text only.** No reactions, edits, deletes, attachments, media, or rich
  message types.
- **No admin APIs, webhooks, HTTP endpoints, or deployment tooling.** None of
  these exist yet.
- **Two transports lack live evidence.** MeshCore and LXMF are unit-tested only.
  They may work perfectly or may have fundamental issues with real hardware.
- **Third-party Matrix inbound unconfirmed.** No live test has verified inbound
  message reception from a second Matrix account.
- **Fork dependencies.** Matrix uses `mindroom-nio` (fork of `matrix-nio`).
  Meshtastic uses `mtjk` (fork of `meshtastic-python`). Fork maintenance is an
  ongoing responsibility.
- **Identity file security (LXMF).** Reticulum identity is a 64-byte raw
  private key file with no encryption. File permission management is the
  operator's responsibility.


## Philosophy

**Runtime-shaped.** medre is designed as an event routing runtime, not a
protocol library. The canonical event model, pipeline stages, and adapter
boundaries exist so that transport-specific code is isolated and the core is
transport-agnostic. The architecture anticipates a standalone runtime, even
though today it ships as importable components.

**Transport-owned adapters.** Each adapter owns its transport lifecycle from
start to stop. The runtime does not open connections, schedule retries across
transports, or orchestrate reconnection. Sessions manage their own tasks, retry
budgets, and resource cleanup. This is a deliberate architectural choice, not a
missing feature.

**Honest diagnostics.** Every adapter exposes a `diagnostics()` method that
returns a read-only snapshot of its current state. Diagnostics are not
authoritative state — they are observations at a point in time. No secrets
appear in diagnostics. No SDK objects cross the adapter boundary.

**Fake adapters are first-class.** The fake adapters are not stubs. They
exercise the real pipeline with the real codec and renderer. They are how medre
is tested in CI and in development.

**No hype.** If something is not validated, medre says so. If something is
inherent to a radio protocol, medre documents it rather than abstracting over
it. If a transport is alpha-operational, it is labeled alpha-operational.


## Installation

medre requires Python >= 3.11. The only core dependency is `msgspec`.

```bash
# Core only (no transport SDKs)
pip install -e .

# With dev dependencies (pytest, pytest-asyncio)
pip install -e ".[dev]"

# With specific transport SDKs
pip install -e ".[matrix]"          # Matrix plaintext
pip install -e ".[matrix-e2e]"      # Matrix with E2EE (requires Rust toolchain on some platforms)
pip install -e ".[meshtastic]"      # Meshtastic LoRa
pip install -e ".[meshcore]"        # MeshCore radio
pip install -e ".[lxmf]"            # LXMF / Reticulum

# Multiple transports
pip install -e ".[matrix,meshcore,dev]"
```

### Verify installation

```bash
# Run the full unit test suite (no network, no hardware)
PYTHONPATH=src pytest -q
# Expected: 2000+ passed, live tests skipped by default

# Compile check
python -m compileall -q src tests
# Expected: no output
```

For full environment setup, see
[docs/runbooks/developer-environment.md](docs/runbooks/developer-environment.md).


## Live testing

Live tests connect to real transport endpoints. They are **off by default** and
require hardware, credentials, and environment variables.

```bash
# Live tests are excluded from default runs via pytest markers
pytest -q                       # runs only unit tests
pytest -q -m live               # runs only live tests (requires env vars)

# Example: Matrix live smoke
MATRIX_HOMESERVER=https://matrix.org \
MATRIX_USER_ID=@user:matrix.org \
MATRIX_ACCESS_TOKEN=syt_... \
MATRIX_ROOM_ID='!roomid:matrix.org' \
pytest tests/test_matrix_live.py -m live --tb=short
```

Live tests use `@require_live` skip guards. If required environment variables
are missing, tests are skipped (not failed). This ensures the unit suite is
never broken by missing hardware or credentials.

Live test philosophy: smoke tests, not reliability tests. A passing live test
proves the adapter can start, send, and report diagnostics against a real
endpoint. It does not prove sustained throughput, reconnect resilience, or
multi-hop delivery.

Live test results and evidence per transport:
[docs/runbooks/operational-evidence.md](docs/runbooks/operational-evidence.md).


## Beta expectations

medre is pre-beta software. If you use it:

- Expect rough edges in config ergonomics and error messages.
- Expect that MeshCore and LXMF adapters have not touched real hardware through
  medre.
- Expect radio transports to lose messages without reporting failure (fire-and-forget).
- Expect duplicate messages under retry conditions.
- Expect API changes. The public interface is not yet stable.
- Do not expect production reliability, deployment tooling, or operational
  support.
- Do not expect reactions, media, attachments, bridging, or multi-device
  coordination.

The beta readiness checklist tracks what must be true before beta:
[docs/contracts/32-beta-readiness-checklist.md](docs/contracts/32-beta-readiness-checklist.md).


## Documentation

| Path | Content |
|------|---------|
| [`docs/runbooks/developer-environment.md`](docs/runbooks/developer-environment.md) | Setup guide, tested versions, transport-specific install |
| [`docs/runbooks/operational-evidence.md`](docs/runbooks/operational-evidence.md) | Live test results per transport |
| [`docs/runbooks/secure-credentials.md`](docs/runbooks/secure-credentials.md) | Credential handling recommendations |
| [`docs/contracts/`](docs/contracts/) | Design contracts, audit reports, maturity assessments |
| [`docs/contracts/37-transport-maturity-classification.md`](docs/contracts/37-transport-maturity-classification.md) | Per-transport maturity tier and evidence |
| [`docs/contracts/32-beta-readiness-checklist.md`](docs/contracts/32-beta-readiness-checklist.md) | What must be true before beta release |
| [`docs/contracts/36-radio-limitations.md`](docs/contracts/36-radio-limitations.md) | Fire-and-forget delivery model for radio transports |


## License

MIT. See `pyproject.toml`.
