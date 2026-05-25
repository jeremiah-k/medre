# Live Test Harness Guide

> Last updated: 2026-05-25 (Tranche 6 truth-surface update)
> Scope: Writing and maintaining opt-in live tests for MEDRE transport adapters
> Status: **Alpha. Patterns are evolving.** This guide describes current conventions, not final API contracts.
> Tranche 6 note: No live tests were executed this session. Added not_executed_result, get_live_artifact_dir, and matrix_second_user_env_set helpers; extended boundary/test coverage with hardware marker discipline. Baseline: HEAD 41a07c7, Python 3.12.3, medre 0.1.0.

This guide covers how live tests work in MEDRE: how they are gated, how to write one for a new transport, and what rules they must follow. It is written for test developers contributing to the MEDRE test suite.

Live tests exercise real adapters against real endpoints (Matrix homeservers, Meshtastic radios, etc.). They are opt-in, excluded from default runs, and require environment variables to execute. They never run in CI without explicit credentials.

For operator workflows (running smoke tests, collecting evidence, diagnosing failures), see `docs/runbooks/operator-workflows.md`.

## 1. Live Tests Are Opt-In

Live tests never run by default. They are excluded at two levels.

### 1.1 pytest marker: `@pytest.mark.live`

Every live test must carry the `@pytest.mark.live` decorator:

```python
import pytest

@pytest.mark.live
async def test_matrix_adapter_delivers_to_real_room():
    ...
```

The `pyproject.toml` excludes live tests from default runs:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not live and not docker and not hardware'"
```

Running `pytest` without flags will skip all live tests.

### 1.2 `@pytest.mark.hardware` — physical hardware subset

Tests requiring physical hardware (serial/BLE Meshtastic radios, etc.) must carry `@pytest.mark.hardware` **in addition to** `@pytest.mark.live`. Hardware tests are a strict subset of live tests — they connect to real devices that may not be present on every machine.

```python
import pytest

@pytest.mark.live
@pytest.mark.hardware
async def test_meshtastic_serial_radio_send():
    ...
```

Discipline rule: every file using `@pytest.mark.hardware` must also use `@pytest.mark.live`. The boundary test suite (`test_deployment_boundaries.py`) enforces this invariant.

When hardware is unavailable, tests should produce a `not_executed` artifact via the `not_executed_result()` helper rather than skipping or fabricating a pass:

```python
from tests.helpers.live_harness import not_executed_result, live_result_to_json

if not radio_available:
    result = not_executed_result(
        transport="meshtastic",
        adapter_id="radio-serial",
        reason="serial radio not connected",
    )
    # Write artifact for audit trail
    ...
```

### 1.3 Environment variable skipif

In addition to the marker, each live test module or class should include a `pytest.importorskip` or manual skip guard that checks for required environment variables. If the variables are not set, the test skips with a clear reason:

```python
import pytest
from tests.helpers.live_harness import LiveRequirement, live_env_status

# Skip entire module if Matrix vars are not set
_MATRIX_REQUIREMENTS = [
    LiveRequirement(env_name="MATRIX_HOMESERVER", secret=False, description="Homeserver URL"),
    LiveRequirement(env_name="MATRIX_USER_ID", secret=False, description="Bot user ID"),
    LiveRequirement(env_name="MATRIX_ACCESS_TOKEN", secret=True, description="Bot access token"),
    LiveRequirement(env_name="MATRIX_ROOM_ID", secret=False, description="Target room for tests"),
]

_env = live_env_status(_MATRIX_REQUIREMENTS)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _env.enabled,
        reason=f"Missing: {', '.join(_env.missing)}",
    ),
]
```

This two-level gating means live tests will not accidentally run on a machine that has the `live` marker enabled but does not have credentials configured.

### 1.4 Running live tests

```bash
# All live tests
PYTHONPATH=src pytest -m live -v --tb=short

# Single transport
PYTHONPATH=src pytest tests/test_matrix_live.py -m live -v

# Specific test
PYTHONPATH=src pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_outbound_delivery -m live -v
```

### 1.5 Second Matrix test user

Live tests that simulate inbound messages from a non-bot user can use a second Matrix identity. Set these environment variables:

- `MATRIX_SECOND_USER_ID` — fully-qualified user ID (e.g. `@test-user:localhost`)
- `MATRIX_SECOND_ACCESS_TOKEN` — access token for the second user

Check availability with `matrix_second_user_env_set()` from `tests.helpers.live_config`. The helper returns a boolean and never reads or prints the token value.

## 2. Live Artifact Directory

Live tests persist structured artifacts (results, logs, evidence) to a configurable directory via `get_live_artifact_dir()` from `tests.helpers.live_harness`.

| Variable | Default | Description |
|---|---|---|
| `MEDRE_LIVE_ARTIFACT_DIR` | `.ci-artifacts/live-evidence/<timestamp>` | Override to a custom path |

The default path includes an ISO-8601 timestamp to separate runs. The directory is created automatically.

## 3. Instance-Scoped Environment Variables

Live test adapters are configured using MEDRE's instance-scoped env var format. Every adapter override follows `MEDRE_ADAPTER__<TOKEN>__<FIELD>`, where `<TOKEN>` is the uppercased, normalised adapter ID.

> **Runtime config vs. test convenience vars.** This section describes environment variables used by the live test harness. MEDRE's runtime config system uses `MEDRE_ADAPTER__<TOKEN>__<FIELD>` as its only adapter override surface (see `docs/runbooks/configuration.md`). Some test modules may also read convenience variables like `MATRIX_HOMESERVER` or `MESHTASTIC_CONNECTION_TYPE` (without the `MEDRE_` prefix) for constructing test fixtures. These convenience vars are **test-only**. They are consumed by pytest test code, not by MEDRE's runtime config loader. If you need to override adapter config at runtime (in production or in a Docker container), always use `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

| Transport  | Token example    | Example variables                                                                                                                             |
| ---------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | `MAIN`           | `MEDRE_ADAPTER__MAIN__HOMESERVER`, `MEDRE_ADAPTER__MAIN__USER_ID`, `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN`, `MEDRE_ADAPTER__MAIN__ROOM_ALLOWLIST` |
| Meshtastic | `RADIO`          | `MEDRE_ADAPTER__RADIO__HOST`, `MEDRE_ADAPTER__RADIO__PORT`, `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE`                                           |
| MeshCore   | `MESHCORE_RADIO` | `MEDRE_ADAPTER__MESHCORE_RADIO__HOST`, `MEDRE_ADAPTER__MESHCORE_RADIO__CONNECTION_TYPE`                                                       |
| LXMF       | `LOCAL`          | `MEDRE_ADAPTER__LOCAL__CONNECTION_TYPE`, `MEDRE_ADAPTER__LOCAL__IDENTITY_PATH`                                                                |

The token is derived from the adapter's `adapter_id` by stripping non-alphanumeric characters (replaced with `_`), collapsing consecutive underscores, and uppercasing. See `docs/runbooks/configuration.md` for the full normalisation table.

When adding a new transport, define its adapter ID and document the required `MEDRE_ADAPTER__<TOKEN>__<FIELD>` variables in the test module's docstring and in the skipif reason string.

## 4. Live Test Helpers

The `tests/helpers/` directory contains shared utilities for live tests. The live harness helpers provide:

- **Environment status checking.** Helpers that verify required environment variables are set and non-empty, returning a status indicating which variables are present or missing.
- **Live requirement validation.** Helpers that construct adapter configuration from environment variables, validate it, and raise clear errors if something is wrong.

These helpers centralize the env var parsing and validation logic so individual test files do not duplicate it. If you are writing a new transport's live tests, check `tests/helpers/` for existing utilities before writing your own env var parsing.

## 5. Template: Adding a New Transport Live Test

Follow this template when adding live tests for a new transport. The pattern is proven: Matrix live tests use it, and Meshtastic live tests should follow it.

### 5.1 File structure

```text
tests/
  test_<transport>_live.py          # Live tests
  helpers/
    <transport>_live.py             # (optional) shared live test helpers
```

### 5.2 Module skeleton

```python
"""Live tests for the <Transport> adapter.

These tests require a real <transport> endpoint and credentials.
Set the required environment variables (see _REQUIRED_VARS) to run them.

Gating:
  - pytest.mark.live: excluded from default runs
  - skipif: skipped unless all required env vars are set
"""

import os
import pytest

# --- Gating --------------------------------------------------------

_REQUIRED_VARS = [
    "<TRANSPORT>_HOST",
    "<TRANSPORT>_PORT",
]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not all(os.environ.get(v) for v in _REQUIRED_VARS),
        reason=(
            "Set " + ", ".join(_REQUIRED_VARS)
            + " env vars to run live <Transport> tests"
        ),
    ),
]

# --- Constants -----------------------------------------------------

_ADAPTER_START_TIMEOUT = 30  # seconds
_ADAPTER_STOP_TIMEOUT = 10   # seconds
_DELIVER_TIMEOUT = 15        # seconds

# --- Tests ---------------------------------------------------------

async def test_adapter_starts_and_connects():
    """Adapter can connect to a real endpoint."""
    ...


async def test_outbound_delivery():
    """Adapter delivers a message to a real endpoint."""
    ...


async def test_inbound_reception():
    """Adapter receives a message from a real endpoint."""
    ...
```

### 5.3 Required sections

Every live test module must have:

1. **Gating.** The `pytestmark` list with both `pytest.mark.live` and `pytest.mark.skipif`.
2. **Timeout constants.** Named constants at module level, used in all `asyncio.wait_for` calls.
3. **try/finally cleanup.** Every test that creates an adapter must stop it in a `finally` block.

## 6. Bounded Async Operations

Every async operation in a live test must be bounded by an explicit timeout. No unbounded awaits.

### 6.1 Use `bounded()` for async operations

```python
from tests.helpers.live_harness import bounded

# CORRECT
async def test_adapter_starts():
    adapter = make_live_adapter()
    try:
        await bounded(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT, label="adapter start")
    finally:
        await bounded(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT, label="adapter stop")


# WRONG: unbounded await
async def test_adapter_starts_bad():
    adapter = make_live_adapter()
    await adapter.start(ctx)  # could hang forever
    await adapter.stop()
```

### 6.2 Timeout constant naming

Use module-level constants with descriptive names:

```python
_ADAPTER_START_TIMEOUT = 30  # seconds
_ADAPTER_STOP_TIMEOUT = 10   # seconds
_DELIVER_TIMEOUT = 15        # seconds
_INBOUND_WAIT_TIMEOUT = 30   # seconds
```

Never use magic numbers in `wait_for` calls:

```python
# WRONG
await asyncio.wait_for(adapter.start(ctx), timeout=30)

# CORRECT
await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
```

### 6.3 Timeout values

The values above are starting points. Adjust based on the transport:

- Matrix: 30 seconds for start is generous but accounts for initial sync.
- Meshtastic: may need longer depending on radio conditions.
- Localhost services: 10 seconds for start is usually enough.

If a test times out consistently, investigate the cause before increasing the timeout. A timeout often indicates a bug, not a slow network.

## 7. try/finally Cleanup

Every live test that creates an adapter, client, or any resource with a `stop()` or `close()` method must use `try/finally` to ensure cleanup.

### 7.1 Single adapter pattern

```python
async def test_outbound_delivery():
    adapter = make_live_adapter()
    await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
    try:
        result = await asyncio.wait_for(
            adapter.deliver(event), timeout=_DELIVER_TIMEOUT
        )
        assert result.status == "success"
    finally:
        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
```

### 7.2 Multiple resources pattern

```python
async def test_bridge_delivery():
    inbound = make_inbound_adapter()
    outbound = make_outbound_adapter()
    await asyncio.wait_for(inbound.start(inbound_ctx), timeout=_ADAPTER_START_TIMEOUT)
    try:
        await asyncio.wait_for(outbound.start(outbound_ctx), timeout=_ADAPTER_START_TIMEOUT)
        try:
            # ... test logic ...
            pass
        finally:
            await asyncio.wait_for(outbound.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
    finally:
        await asyncio.wait_for(inbound.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
```

### 7.3 Why not fixtures?

Live adapters are expensive to start (network connections, authentication, sync loops). Using module-scoped fixtures is acceptable for a group of related tests. Using function-scoped fixtures that start and stop the adapter for every test is wasteful and slow. The `try/finally` pattern gives fine-grained control over when the adapter starts and stops within a test.

If you do use fixtures, make sure cleanup is robust:

```python
@pytest.fixture
async def live_adapter():
    adapter = make_live_adapter()
    await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
    yield adapter
    await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
```

## 8. No Secret Printing

Live tests handle real credentials. They must never print them.

### 8.1 What counts as a secret

- Access tokens (e.g. `MATRIX_ACCESS_TOKEN` in test convenience vars, or `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN` at runtime)
- Passwords
- API keys
- Any value that could be used to impersonate the bot or access the service

### 8.2 Redaction helpers

When test output might contain secrets (e.g., printing an adapter's config for debugging), use redaction helpers. These replace secret values with `***REDACTED***` before printing.

The project provides redaction utilities in `tests/helpers/`. Use them. If they do not cover your case, add a new helper rather than printing raw values.

### 8.3 Assert messages

Test assertion messages should not contain secrets:

```python
# WRONG
assert result.status == "success", f"Delivery failed with token={os.environ['MATRIX_ACCESS_TOKEN']}"

# CORRECT
assert result.status == "success", f"Delivery failed: {result.error}"
```

### 8.4 Logging

Live tests should log at INFO level for major milestones (adapter started, message sent, message received) and at DEBUG level for details. Never log credentials. The adapter's own `__repr__` redacts tokens, but test code that handles raw environment variables must not pass them to the logger.

## 9. Safe-to-Paste Reports

Live test output, evidence bundles, and diagnostic reports must be safe to paste into GitHub issues without manual redaction.

### 9.1 What is safe

- Event IDs, adapter IDs, room IDs
- Timestamps, health states, delivery statuses
- Error messages from the adapter or SDK (as long as they do not contain tokens)
- Configuration with secrets redacted

### 9.2 What is not safe

- Access tokens, passwords, API keys
- Full URLs that embed tokens (e.g., `https://user:token@host/...`)
- Raw environment variable dumps

### 9.3 Evidence bundle safety

The `medre evidence` command produces bundles that redact secrets. Live tests that generate evidence should use the same bundling logic. If you are constructing a report manually, follow the same redaction patterns.

## 10. Rules Summary

1. **Always gate with `@pytest.mark.live` and env var skipif.** Two levels of protection.
2. **Always use `asyncio.wait_for` with named timeout constants.** No unbounded awaits.
3. **Always use `try/finally` for cleanup.** Every adapter must be stopped, every client closed.
4. **Never print secrets.** Use redaction helpers. Review output before sharing.
5. **Never run live tests in CI without explicit credentials.** The skipif guard prevents this, but do not override it.
6. **Use instance-scoped env vars.** `MEDRE_ADAPTER__<TOKEN>__<FIELD>` for all adapter overrides.
7. **Keep tests independent.** Each test should work in isolation. Do not depend on state from a previous test.
8. **Be honest about what the test proves.** A live test proves the adapter works against a real endpoint. It does not prove the system is production-ready.

## Adopting the harness in adapter branches

When porting the live-test harness into a transport-specific adapter branch, follow these conventions to stay consistent with the shared helpers:

- **Use `LiveRequirement` for env var requirements.** Define your transport's requirements as a list of `LiveRequirement` instances and pass them to `live_env_status()`. Mark credentials with `secret=True` for explicit redaction.
- **Use `bounded()` for async start/stop/deliver operations.** Wrap every `await adapter.start()`, `adapter.stop()`, and `adapter.deliver()` call with `bounded()` and a named timeout constant. Never use unbounded awaits.
- **Use `assert_no_secret_leak()` against diagnostics/reports.** After capturing any serialisable output (smoke results, evidence bundles, error reports), run `assert_no_secret_leak()` with the raw secret values to confirm nothing leaked into the serialized form.
- **Radio sends require `TRANSPORT_LIVE_SEND=1` opt-in env var.** Transports that transmit over radio (Meshtastic, MeshCore, LXMF) must gate any outbound radio transmission behind `TRANSPORT_LIVE_SEND=1`. This prevents accidental transmissions during development.
- **Do not import optional SDKs in shared helpers.** The `tests/helpers/live_harness.py` module is SDK-free by design. Transport-specific SDK imports belong in the per-transport test module or a transport-specific helper, never in the shared harness.
- **Do not add package-root facades.** Live test helpers live under `tests/helpers/`. Do not create top-level packages or facade modules that re-export harness utilities. Import directly from `tests.helpers.live_harness`.

## 11. Related Documentation

| Document                                  | What it covers                                                |
| ----------------------------------------- | ------------------------------------------------------------- |
| `docs/dev/TESTING_GUIDE.md`               | General testing guide (tiers, style, async mocking, fixtures) |
| `docs/runbooks/operator-workflows.md`     | Operator guide (smoke tests, evidence, tracing, diagnosis)    |
| `docs/runbooks/matrix-alpha-operation.md` | Full Matrix alpha setup and operation                         |
| `docs/runbooks/matrix-live-smoke.md`      | Matrix live smoke test instructions                           |
