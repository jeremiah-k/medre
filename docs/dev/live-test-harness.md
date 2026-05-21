# Live Test Harness Guide

> Last updated: 2026-05-21
> Scope: Writing and maintaining opt-in live tests for MEDRE transport adapters
> Status: **Alpha. Patterns are evolving.** This guide describes current conventions, not final API contracts.

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
addopts = "-m 'not live and not docker'"
```

Running `pytest` without flags will skip all live tests.

### 1.2 Environment variable skipif

In addition to the marker, each live test module or class should include a `pytest.importorskip` or manual skip guard that checks for required environment variables. If the variables are not set, the test skips with a clear reason:

```python
import os
import pytest

pytestmark = pytest.mark.live

# Skip entire module if Matrix vars are not set
_REQUIRED_VARS = ["MATRIX_HOMESERVER", "MATRIX_USER_ID", "MATRIX_ACCESS_TOKEN", "MATRIX_ROOM_ID"]


def _has_matrix_env():
    return all(os.environ.get(v) for v in _REQUIRED_VARS)


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _has_matrix_env(), reason=(
        "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
        "and MATRIX_ROOM_ID env vars to run live Matrix tests"
    )),
]
```

This two-level gating means live tests will not accidentally run on a machine that has the `live` marker enabled but does not have credentials configured.

### 1.3 Running live tests

```bash
# All live tests
PYTHONPATH=src pytest -m live -v --tb=short

# Single transport
PYTHONPATH=src pytest tests/test_matrix_live.py -m live -v

# Specific test
PYTHONPATH=src pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_outbound_delivery -m live -v
```

## 2. Transport-Prefixed Environment Variables

Each transport uses its own prefix for environment variables. This prevents collisions and makes it clear which transport a test is exercising.

| Transport | Prefix | Example variables |
|---|---|---|
| Matrix | `MATRIX_` | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`, `MATRIX_ROOM_ALLOWLIST` |
| Meshtastic | `MESHTASTIC_` | `MESHTASTIC_PORT`, `MESHTASTIC_HOST`, `MESHTASTIC_CHANNEL` |
| MeshCore | `MESHCORE_` | (to be defined) |
| LXMF | `LXMF_` | (to be defined) |

The convention is consistent: the prefix matches the transport name in uppercase, followed by an underscore, followed by the variable name.

When adding a new transport, define its prefix and document the required variables in the test module's docstring and in the skipif reason string.

## 3. Live Test Helpers

The `tests/helpers/` directory contains shared utilities for live tests. The live harness helpers provide:

- **Environment status checking.** Helpers that verify required environment variables are set and non-empty, returning a status indicating which variables are present or missing.
- **Live requirement validation.** Helpers that construct adapter configuration from environment variables, validate it, and raise clear errors if something is wrong.

These helpers centralize the env var parsing and validation logic so individual test files do not duplicate it. If you are writing a new transport's live tests, check `tests/helpers/` for existing utilities before writing your own env var parsing.

## 4. Template: Adding a New Transport Live Test

Follow this template when adding live tests for a new transport. The pattern is proven: Matrix live tests use it, and Meshtastic live tests should follow it.

### 4.1 File structure

```
tests/
  test_<transport>_live.py          # Live tests
  helpers/
    <transport>_live.py             # (optional) shared live test helpers
```

### 4.2 Module skeleton

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

### 4.3 Required sections

Every live test module must have:

1. **Gating.** The `pytestmark` list with both `pytest.mark.live` and `pytest.mark.skipif`.
2. **Timeout constants.** Named constants at module level, used in all `asyncio.wait_for` calls.
3. **try/finally cleanup.** Every test that creates an adapter must stop it in a `finally` block.

## 5. Bounded Async Operations

Every async operation in a live test must be bounded by an explicit timeout. No unbounded awaits.

### 5.1 Use `asyncio.wait_for` with named constants

```python
import asyncio

# CORRECT
async def test_adapter_starts():
    adapter = make_live_adapter()
    try:
        await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
    finally:
        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)


# WRONG: unbounded await
async def test_adapter_starts_bad():
    adapter = make_live_adapter()
    await adapter.start(ctx)  # could hang forever
    await adapter.stop()
```

### 5.2 Timeout constant naming

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

### 5.3 Timeout values

The values above are starting points. Adjust based on the transport:

- Matrix: 30 seconds for start is generous but accounts for initial sync.
- Meshtastic: may need longer depending on radio conditions.
- Localhost services: 10 seconds for start is usually enough.

If a test times out consistently, investigate the cause before increasing the timeout. A timeout often indicates a bug, not a slow network.

## 6. try/finally Cleanup

Every live test that creates an adapter, client, or any resource with a `stop()` or `close()` method must use `try/finally` to ensure cleanup.

### 6.1 Single adapter pattern

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

### 6.2 Multiple resources pattern

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

### 6.3 Why not fixtures?

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

## 7. No Secret Printing

Live tests handle real credentials. They must never print them.

### 7.1 What counts as a secret

- Access tokens (`MATRIX_ACCESS_TOKEN`)
- Passwords
- API keys
- Any value that could be used to impersonate the bot or access the service

### 7.2 Redaction helpers

When test output might contain secrets (e.g., printing an adapter's config for debugging), use redaction helpers. These replace secret values with `***REDACTED***` before printing.

The project provides redaction utilities in `tests/helpers/`. Use them. If they do not cover your case, add a new helper rather than printing raw values.

### 7.3 Assert messages

Test assertion messages should not contain secrets:

```python
# WRONG
assert result.status == "success", f"Delivery failed with token={os.environ['MATRIX_ACCESS_TOKEN']}"

# CORRECT
assert result.status == "success", f"Delivery failed: {result.error}"
```

### 7.4 Logging

Live tests should log at INFO level for major milestones (adapter started, message sent, message received) and at DEBUG level for details. Never log credentials. The adapter's own `__repr__` redacts tokens, but test code that handles raw environment variables must not pass them to the logger.

## 8. Safe-to-Paste Reports

Live test output, evidence bundles, and diagnostic reports must be safe to paste into GitHub issues without manual redaction.

### 8.1 What is safe

- Event IDs, adapter IDs, room IDs
- Timestamps, health states, delivery statuses
- Error messages from the adapter or SDK (as long as they do not contain tokens)
- Configuration with secrets redacted

### 8.2 What is not safe

- Access tokens, passwords, API keys
- Full URLs that embed tokens (e.g., `https://user:token@host/...`)
- Raw environment variable dumps

### 8.3 Evidence bundle safety

The `medre evidence` command produces bundles that redact secrets. Live tests that generate evidence should use the same bundling logic. If you are constructing a report manually, follow the same redaction patterns.

## 9. Rules Summary

1. **Always gate with `@pytest.mark.live` and env var skipif.** Two levels of protection.
2. **Always use `asyncio.wait_for` with named timeout constants.** No unbounded awaits.
3. **Always use `try/finally` for cleanup.** Every adapter must be stopped, every client closed.
4. **Never print secrets.** Use redaction helpers. Review output before sharing.
5. **Never run live tests in CI without explicit credentials.** The skipif guard prevents this, but do not override it.
6. **Use transport-prefixed env vars.** `MATRIX_*` for Matrix, `MESHTASTIC_*` for Meshtastic.
7. **Keep tests independent.** Each test should work in isolation. Do not depend on state from a previous test.
8. **Be honest about what the test proves.** A live test proves the adapter works against a real endpoint. It does not prove the system is production-ready.

## 10. Related Documentation

| Document | What it covers |
|---|---|
| `docs/dev/TESTING_GUIDE.md` | General testing guide (tiers, style, async mocking, fixtures) |
| `docs/runbooks/operator-workflows.md` | Operator guide (smoke tests, evidence, tracing, diagnosis) |
| `docs/runbooks/matrix-alpha-operation.md` | Full Matrix alpha setup and operation |
| `docs/runbooks/matrix-live-smoke.md` | Matrix live smoke test instructions |
