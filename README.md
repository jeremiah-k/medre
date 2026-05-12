# medre — Modular Event-driven Routing Engine

**Status: Pre-beta. Not production-ready. Not suitable for real workloads.**

medre is a modular event routing toolkit with an optional runtime. It ships as
importable Python components you wire into your own code, with a canonical event
model, per-transport adapter boundaries, and a pipeline that moves events through
codec → renderer → session → adapter. The same architecture serves as a
standalone runtime that reads a TOML config file and runs declared routes — but
that runtime is early and not yet hardened for unsupervised use.

Today: import the pieces you need, or use the config-file-first runtime to start
adapters and routes from a TOML file. The event contracts, adapters, codecs,
renderers, and session types all work as library components in any async Python
application. The runtime (`medre run`) assembles adapters from config, starts
them in deterministic order, and manages a supervised lifecycle. No API
endpoints, webhooks, or deployment tooling are provided.


### At a glance

- **Importable toolkit** — adapters, codecs, sessions, and the event model are
  library components with no mandatory runtime or server.
- **Optional runtime** — `medre run` reads a TOML config file, assembles
  adapters, and starts a supervised process. Config-file-first, no flags or
  environment-only paths.
- **Explicit routes** — routes are declared in the config file as named
  entries. No auto-discovery, no implicit bridging. What you declare is what
  runs.
- **Fake adapters included** — every transport has a fake adapter that exercises
  the full pipeline with zero dependencies and no network. Used in CI and for
  local development.
- **Live transports are optional** — real adapters are gated behind optional
  dependency groups (`pip install medre[matrix]`, etc.). Install only what you
  need.
- **Honest maturity** — Matrix and Meshtastic are live-validated against real
  endpoints. MeshCore and LXMF are unit-tested only. No transport is claimed
  production-ready.


## Architecture layers

**Contracts and core** (`medre.core.*`). A frozen, schema-versioned
`CanonicalEvent` (msgspec Struct) is the single unit of data flow. Event bus,
rendering pipeline, routing/planning layer, storage, and identity modules live
here. These are importable library components with no transport dependencies.

**Transport adapters.** Four adapters (Matrix, Meshtastic, MeshCore, LXMF),
each with its own codec, renderer, session, config, error types, and compat
guard. Each adapter owns its transport lifecycle entirely: start, stop, deliver,
health check, diagnostics. No external component touches the transport
connection. Every `deliver()` returns an `AdapterDeliveryResult` recording what
happened. Each session owns its own task lifecycle, retry budgets, reconnect
policy, and cleanup. No cross-session coordination.

**Runtime orchestration.** `RuntimeBuilder` assembles adapters from parsed TOML
config, starts them in deterministic order (alphabetical by transport, then
adapter ID), and provides supervised lifecycle (`start` / `stop`). Routes are
explicit entries declared in the config — no auto-discovery. The runtime exists
and passes its test suite, but has not been exercised under sustained load or
real operational conditions. See
[contract 47](docs/contracts/47-runtime-assembly-contract.md).

**External composition.** Consumers import adapters, wire them into their own
async applications, and manage process lifecycle themselves. This is the
operational mode today and remains fully supported alongside the runtime.


## Supported transports

| Transport | Adapter path | Live-validated | Maturity |
|-----------|-------------|----------------|----------|
| **Matrix** | Synapse homeservers via `mindroom-nio` | Yes | Beta-candidate |
| **Meshtastic** | LoRa mesh nodes via `mtjk` (serial/TCP) | Yes | Beta-candidate |
| **MeshCore** | MeshCore radio nodes via `meshcore_py` | No | Alpha-operational |
| **LXMF** | Reticulum / LXMF mesh via `lxmf` + `rns` | No | Alpha-operational |

Beta-candidate means the adapter has passed a live smoke test against a real
endpoint. Alpha-operational means the adapter passes its full unit and fake
pipeline test suite but has not been exercised against real hardware or a live
network through medre. These are meaningfully different maturity levels, not
just labels. Per-transport maturity definitions and live test evidence:
[docs/contracts/37-transport-maturity-classification.md](docs/contracts/37-transport-maturity-classification.md).

**Live-validated means smoke-tested once against a real endpoint.** It does not
mean sustained, reliable, or production-tested.


## Fake vs. live transports

Every transport has a fake adapter (`FakeMatrixAdapter`, `FakeMeshtasticAdapter`,
`FakeMeshCoreAdapter`, `FakeLxmfAdapter`). These:

- Accept the same config as the real adapter but ignore network/hardware.
- Exercise the full pipeline: codec → renderer → session → adapter → delivery
  result → diagnostics.
- Are used by the unit test suite (3,000+ tests, all passing).
- Require zero optional dependencies.

The real adapters are gated behind optional dependency groups (see Installation).
If a dependency is missing, the adapter's compat module reports
`HAS_<SDK> = False` and the adapter cannot be instantiated.


## Matrix encrypted-room support

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

**Dual-role architecture.** medre is both an importable toolkit and a runtime.
The contracts, adapters, codecs, and sessions all work as library components
today. The same architecture serves as a standalone event routing runtime via
`medre run`. Neither role is provisional. The canonical event model, pipeline
stages, and adapter boundaries exist so that transport-specific code is isolated
and the core is transport-agnostic, whether you import two adapters into your
own process or hand process lifecycle to medre itself.

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
# Expected: 3200+ passed, live tests skipped by default

# Compile check
python -m compileall -q src tests
# Expected: no output
```

For full environment setup, see
[docs/runbooks/developer-environment.md](docs/runbooks/developer-environment.md).


### Quick start with fake adapters

No hardware, no network, no SDK dependencies:

```bash
pip install -e ".[dev]"

# Run the full unit suite (all fake adapters, zero network)
PYTHONPATH=src pytest -q
# Expected: 3200+ passed, live tests skipped

# Start the runtime with a config that uses fake adapters
medre config sample > /tmp/medre-test.toml
# Edit adapters to use fake transports, then:
PYTHONPATH=src medre run --config /tmp/medre-test.toml
```

Every transport has a fake adapter (`FakeMatrixAdapter`,
`FakeMeshtasticAdapter`, `FakeMeshCoreAdapter`, `FakeLxmfAdapter`). They accept
the same config as the real adapter, exercise the full pipeline (codec →
renderer → session → adapter → delivery result → diagnostics), and require
zero optional dependencies. See [Fake vs. live transports](#fake-vs-live-transports)
below.


## Configuration

MEDRE is config-file-first. A TOML config declares adapters, routes, and
runtime settings. The runtime reads the config, assembles adapters, and starts
them — no CLI flags or environment-only paths for adapter setup.

Routes are explicit: named entries in the config that bind a source to one or
more target adapters. No auto-discovery, no implicit bridging. What you declare
is what runs.

```bash
# Generate a sample config (includes adapter and route declarations)
medre config sample > ~/.config/medre/config.toml

# Edit the config file, then run
medre run

# Validate config without starting
medre config check
```

See the [Configuration Runbook](docs/runbooks/configuration.md) for the full
TOML schema, environment variable overrides, XDG path defaults, and route
declaration syntax.


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
  medre. Their unit and fake-pipeline tests pass, but no live smoke test has
  been recorded.
- Expect radio transports to lose messages without reporting failure (fire-and-forget).
  `success=True` means the local radio accepted the packet, not that the remote
  party received it.
- Expect duplicate messages under retry conditions (up to 3 retries per session).
- Expect API changes. The public interface is not yet stable.
- Do not expect production reliability, deployment tooling, or operational
  support.
- Do not expect exactly-once delivery semantics. No transport provides this, and
  medre does not synthesize it.
- Do not expect reactions, media, attachments, bridging, or multi-device
  coordination.
- Do not expect equal transport maturity. Matrix and Meshtastic are
  live-validated. MeshCore and LXMF are unit-tested only. See
  [Supported transports](#supported-transports) for per-transport status.

The beta readiness checklist tracks what must be true before beta:
[docs/contracts/32-beta-readiness-checklist.md](docs/contracts/32-beta-readiness-checklist.md).


## Documentation

| Path | Content |
|------|---------|
| [`docs/runbooks/developer-environment.md`](docs/runbooks/developer-environment.md) | Setup guide, tested versions, transport-specific install |
| [`docs/runbooks/configuration.md`](docs/runbooks/configuration.md) | Configuration reference: TOML schema, routes, env vars, paths |
| [`docs/runbooks/operational-evidence.md`](docs/runbooks/operational-evidence.md) | Live test results per transport |
| [`docs/runbooks/secure-credentials.md`](docs/runbooks/secure-credentials.md) | Credential handling recommendations |
| [`docs/contracts/`](docs/contracts/) | Design contracts, audit reports, maturity assessments |
| [`docs/contracts/47-runtime-assembly-contract.md`](docs/contracts/47-runtime-assembly-contract.md) | Runtime builder, adapter lifecycle, startup ordering |
| [`docs/contracts/49-routing-and-bridge-contract.md`](docs/contracts/49-routing-and-bridge-contract.md) | Route declaration, bridge policy, delivery planning |
| [`docs/contracts/37-transport-maturity-classification.md`](docs/contracts/37-transport-maturity-classification.md) | Per-transport maturity tier and evidence |
| [`docs/contracts/32-beta-readiness-checklist.md`](docs/contracts/32-beta-readiness-checklist.md) | What must be true before beta release |
| [`docs/contracts/36-radio-limitations.md`](docs/contracts/36-radio-limitations.md) | Fire-and-forget delivery model for radio transports |


## License

MIT is declared in `pyproject.toml`, but license governance is being formalized.
GPL-3.0-or-later and LGPL-3.0-or-later are under evaluation as alternatives that
may better match the Meshtastic ecosystem, where upstream SDKs and firmware are
GPL or LGPL. MIT no longer clearly fits the dependency reality. No decision has
been made. See `docs/contracts/42-contributor-governance.md` §5 for relicensing
constraints.

The LXMF adapter depends on Reticulum, which uses a non-OSI-approved license
(the Reticulum License). This is an unresolved ambiguity in the dependency
chain: medre's own license choice does not resolve the upstream license question
for downstream consumers who use the LXMF transport.

No top-level `LICENSE` file exists yet. This is a known gap tracked in contract
45 §3 (SPDX + Metadata Hygiene Audit).
