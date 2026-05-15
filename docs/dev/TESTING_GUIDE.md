# Testing Guide

This guide covers testing patterns, rules, and conventions for the MEDRE
project. It is the authoritative reference for how tests are written, what
each test tier proves, and how to run the suite.

The test suite has 3,200+ tests, all passing with zero network or hardware
dependencies. Every transport has a fake adapter that exercises the full
pipeline. See the [README](../../README.md) for project context and the
[Fake Bridge Smoke Runbook](../runbooks/fake-bridge-smoke-runbook.md) for
bridge-specific test commands.


## File Size Limits

**Target: < 1,200 lines per test file.** Hard ceiling: 1,500 lines unless
explicitly justified in the file and in this guide.

Split files by behavioral domain, not by "coverage" or "misc". When a domain
file approaches the target, split it by subdomain following the procedure in
the [Next Modernization Wave](#next-modernization-wave) section below.

### Allowlisted files (legacy, above 1,500 lines)

These files predate the size policy and have not yet been split. Each must
carry a TODO comment in the structure test explaining why it has not been
split.

| File | Lines | Status |
|------|-------|--------|
| `tests/test_pipeline.py` | 2,937 | Allowlisted; pending split |
| `tests/test_matrix_session.py` | 2,241 | Allowlisted; pending split |
| `tests/test_cli.py` | 2,172 | Allowlisted; pending split |
| `tests/test_replay.py` | 1,850 | Allowlisted; pending split |
| `tests/test_storage.py` | 1,939 | Allowlisted; pending split |
| `tests/test_canonical_events.py` | 1,992 | Allowlisted; pending split |
| `tests/test_meshtastic_fake_bridge.py` | 1,540 | Allowlisted; pending split |
| `tests/test_fake_runtime_smoke.py` | 1,506 | Allowlisted; pending split |
| `tests/test_runtime_builder.py` | 1,440 | Borderline; near enough to track here |

No new test files may exceed 1,500 lines. Files in the allowlist above should
be split according to the schedule in
[Next Modernization Wave](#next-modernization-wave).

### Splitting procedure

1. Identify distinct subdomains in the file (e.g., delivery vs. failure
   taxonomy vs. fanout for pipeline tests).
2. Create new `test_<module>_<subdomain>.py` files.
3. Move the relevant test functions, fixtures, and imports. Keep shared
   helpers in `tests/helpers/` if needed by multiple files.
4. Run `PYTHONPATH=src pytest tests/test_<module>_<subdomain>.py -q` to
   confirm the split tests pass.
5. Delete the moved code from the original file.
6. Run the full suite to confirm nothing is broken:
   `PYTHONPATH=src pytest -q`.


## Test Style

### Use pytest function style for new tests

New tests must use pytest function style (module-level `async def` or `def`),
not `unittest.TestCase`. Existing `TestCase` classes are acceptable as legacy
but should not be extended with new test methods.

```python
# Preferred: pytest async function
async def test_adapter_delivers_event(fake_adapter, canonical_event):
    result = await fake_adapter.deliver(canonical_event)
    assert result.status == "success"


# Acceptable legacy: existing TestCase classes (do not extend)
class TestLegacyStorage(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStorage()
```

### Use pytest fixtures over setUp/tearDown

Prefer pytest fixtures for test setup and teardown. Fixtures are composable,
support async natively, and have explicit scoping.

```python
@pytest.fixture
async def storage(tmp_path):
    store = SqliteStorage(db_path=tmp_path / "test.db")
    await store.start()
    yield store
    await store.stop()


async def test_event_round_trip(storage):
    event = make_test_event()
    await storage.put(event)
    retrieved = await storage.get(event.event_id)
    assert retrieved is not None
```

### asyncio_mode = "auto"

The project uses `asyncio_mode = "auto"` in `pyproject.toml`. Most async test
functions do not need an explicit `@pytest.mark.asyncio` decorator. Add the
decorator only when the auto-detection fails (rare, usually with parametrized
generators).


## Avoiding Fixed Sleeps

Never use `asyncio.sleep()` in tests. Fixed sleeps are nondeterministic, slow
the suite, and mask race conditions.

### Use `wait_until()` for polling conditions

`wait_until()` (from `tests/helpers/async_utils.py`) polls a condition with a
short interval and a bounded timeout. It fails loudly if the condition is not
met within the timeout, rather than silently passing.

```python
from tests.helpers.async_utils import wait_until

# CORRECT: deterministic polling
async def test_delivery_propagates(adapter, event):
    await adapter.simulate_inbound(event)
    await wait_until(lambda: len(adapter.delivered_payloads) >= 1)
    assert adapter.delivered_payloads[0].text == "expected"

# WRONG: fixed sleep
async def test_delivery_propagates_bad(adapter, event):
    await adapter.simulate_inbound(event)
    await asyncio.sleep(0.3)  # nondeterministic, slow, fragile
    assert len(adapter.delivered_payloads) >= 1
```

### Use deterministic hooks where possible

For conditions that can be triggered by side effects, prefer mocking the side
effect or using `asyncio.Event` over polling:

```python
async def test_pipeline_processes_event(pipeline, event):
    processed = asyncio.Event()

    original_handle = pipeline.handle_ingress

    async def tracking_handle(*args, **kwargs):
        result = await original_handle(*args, **kwargs)
        processed.set()
        return result

    pipeline.handle_ingress = tracking_handle
    await pipeline.submit(event)
    await asyncio.wait_for(processed.wait(), timeout=2.0)
```


## Async Mocking Rules

Using the wrong mock type is the most common source of `RuntimeWarning:
coroutine was never awaited` and `ResourceWarning` noise in the test suite.

### Rule: match the mock type to the production call shape

| Production call | Mock type | Example |
|-----------------|-----------|---------|
| `await client.close()` | `AsyncMock` | `client.close = AsyncMock()` |
| `client.add_event_callback(fn)` | `MagicMock` (never awaited) | `client.add_event_callback = MagicMock()` |
| `await session.start()` | `AsyncMock` | `session.start = AsyncMock()` |
| `session.config` (attribute access) | Plain attribute or `PropertyMock` | `session.config = test_config` |

The rule is simple: if production code `await`s the callable, use `AsyncMock`.
For everything else, use `Mock` or `MagicMock`.

### Coroutine leak prevention in scheduler fakes

When faking scheduler submission helpers (`_submit_coro`,
`run_coroutine_threadsafe`, etc.), close passed coroutines before returning.
This prevents "coroutine was never awaited" warnings:

```python
from concurrent.futures import Future
import asyncio

def _submit_done(coro, loop=None):
    """Fake scheduler submit that closes the coroutine immediately."""
    if asyncio.iscoroutine(coro):
        coro.close()
    fut = Future()
    fut.set_result(None)
    return fut
```

### CancelledError handling in async fakes

Async fakes that simulate cancellation must raise `asyncio.CancelledError`,
not return a value or raise a different exception. Tests that exercise
cancellation paths should catch `CancelledError` explicitly:

```python
async def _cancel_immediately(*args, **kwargs):
    raise asyncio.CancelledError()
```


## Adapter/Bridge Test Tiers

Tests are classified into five tiers based on what they honestly prove. Never
overclaim the evidence level of a test. If a test uses fake adapters, call it
"fake pipeline", not "docker" or "live".

| Tier | Label | What it proves | How to test |
|------|-------|---------------|-------------|
| 1 | `fake_pipeline` | `PipelineRunner.handle_ingress()` works with direct `CanonicalEvent` injection | Import `CanonicalEvent`, construct it, call `runner.handle_ingress()` directly |
| 2 | `fake_adapter_callback` | `adapter.simulate_inbound()` produces the same results as direct injection | Use `FakeMatrixAdapter.simulate_inbound()`, compare output with direct injection |
| 3 | `wrapper_callback` | Real adapter SDK callback (e.g., `_on_room_message`) bridges to fake target | Mock the SDK, test the wrapper callback through the pipeline to a fake target adapter |
| 4 | `docker_sdk_boundary` | Real SDK code paths work against containerized services (Synapse, meshtasticd) | Docker Compose tests, gated by `@pytest.mark.docker` |
| 5 | `live_network` | Real adapter against real endpoint or hardware | `@pytest.mark.live`, requires environment variables |

### Honest evidence reporting

- If a test uses Docker but not real hardware, label it **docker_sdk_boundary**
  (tier 4), not "live".
- If a test uses fake adapters but a real pipeline, label it **fake_pipeline**
  (tier 1) or **fake_adapter_callback** (tier 2), not "docker".
- If a test uses a real SDK but routes to a fake outbound target, label it
  **docker_sdk_boundary bridge smoke** (tier 4), not "live bridge".

The `medre smoke --json` report includes an `evidence_level` field set to
`fake_bridge`. This is intentional. It does not overclaim.


## Storage Tests

### Persistent SQLite vs. in-memory behavior

Tests must verify that SQLite and in-memory storage backends behave
consistently for core operations (put, get, list, delete) and that SQLite
persists across restarts while in-memory does not.

```python
async def test_sqlite_persists_across_restarts(tmp_path):
    db_path = tmp_path / "test.db"
    store = SqliteStorage(db_path=db_path)
    await store.start()
    await store.put(event)
    await store.stop()

    store2 = SqliteStorage(db_path=db_path)
    await store2.start()
    retrieved = await store2.get(event.event_id)
    assert retrieved is not None
    await store2.stop()
```

### Read-only inspect behavior

`medre inspect` commands are read-only. They open the database, query data,
print it, and close. They must never modify the database. Tests for inspect
subcommands should assert that the database content is unchanged after the
command runs.

### Schema version

Schema version stays at 1 pre-release. No schema bumps until beta. Tests
should assert `schema_version == 1` and that opening a v1 database does not
trigger migration.


## Operator/CLI Tests

### Normalized JSON shape assertions

Smoke, drill, evidence, trace, and recover commands produce structured JSON
output. Tests should assert the normalized JSON shape using shared assertion
helpers from `tests/helpers/assertions.py`.

```python
from tests.helpers.assertions import assert_report_shape

def test_smoke_report_json_shape(smoke_report):
    assert_report_shape(smoke_report)
    assert smoke_report["status"] == "pass"
    assert smoke_report["evidence_level"] == "fake_bridge"
```

### CLI test style

CLI tests use module-level functions, not `unittest.TestCase` classes. This
allows proper pytest fixture injection and parametrization.

```python
async def test_config_check_valid_config(tmp_path):
    config_path = write_test_config(tmp_path)
    result = await runner.invoke(["config", "check", "--config", str(config_path)])
    assert result.exit_code == 0
```


## Patch Target Policy

Patch the canonical module where the object is **looked up**, not where it is
**defined**. This ensures the patch intercepts the actual import path used by
the code under test.

```python
# CORRECT: patch at the lookup site
@patch("medre.adapters.matrix.adapter.HAS_NIO")
async def test_matrix_adapter_without_nio(mock_has_nio):
    mock_has_nio.return_value = False
    ...

# WRONG: patch at the definition site (may not intercept the import)
@patch("medre.adapters.matrix.HAS_NIO")
async def test_matrix_adapter_without_nio_bad(mock_has_nio):
    ...
```

Avoid patching package-root re-exports. If `medre.adapters.matrix.__init__`
re-exports `HAS_NIO`, patch `medre.adapters.matrix.adapter.HAS_NIO` instead
of `medre.adapters.matrix.HAS_NIO`.


## Compatibility

### No compatibility shims in tests

Tests must not contain compatibility shims, version detection branches, or
environment-specific workarounds (e.g., `if os.getenv("MEDRE_TESTING")`).
Test and production code paths must be identical.

### Warnings are bugs

Treat warnings as bugs where practical. `ResourceWarning` and
`RuntimeWarning` about unawaited coroutines indicate real issues (leaked
coroutines, unclosed resources) that will cause problems in production.

For CI hardening, use:

```bash
PYTHONPATH=src pytest -W error::ResourceWarning -q
```

This is not enforced by default (some third-party libraries produce noisy
warnings), but failures from this flag should be fixed, not suppressed.


## Docker Tests

Docker tests exercise real SDK code paths against containerized services
(Synapse, meshtasticd). They are **opt-in** and excluded from default runs.

### Gating with the `docker` marker

All Docker tests must use the `@pytest.mark.docker` decorator:

```python
import pytest

@pytest.mark.docker
async def test_synapse_connectivity():
    """Test real Matrix SDK against containerized Synapse."""
    ...
```

### Default exclusion

`pyproject.toml` excludes Docker and live tests from default runs:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not live and not docker'"
```

### Running Docker tests

```bash
# Prerequisites: Docker daemon running, SDK extras installed
pip install -e ".[matrix,meshtastic,dev]"

# All Docker integration tests
PYTHONPATH=src pytest tests/integration/ -m docker -v

# Matrix (Synapse) only
PYTHONPATH=src pytest tests/integration/test_synapse_connectivity.py -m docker -v

# Meshtastic (meshtasticd) only
PYTHONPATH=src pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v
```


## Type Safety

### Fix type issues with better fakes

When a type checker reports a false positive in a test, fix it by improving
the mock or fake to have the correct type, not by suppressing the warning.

### No broad type ignores

Broad `# type: ignore` or `# pyright: ignore` comments at module level are not
acceptable in tests. Specific line-level ignores are acceptable only with a
comment explaining why:

```python
adapter._client.send_response = MagicMock()  # type: ignore[assignment]  # fake does not implement full protocol
```

When editing a test file that already has type-ignores, remove any that the
edit makes unnecessary.


## Running Tests

### Standard suite (no network, no hardware)

```bash
PYTHONPATH=src pytest -q
# Expected: 3,200+ passed, live and Docker tests skipped by default
```

This is the primary development command. It runs all unit and fake-pipeline
tests. No network, no hardware, no optional SDK dependencies required.

### Docker integration tests

```bash
PYTHONPATH=src pytest -m docker -v
# Requires: Docker daemon running, SDK extras installed
```

### Live network tests

```bash
PYTHONPATH=src pytest -m live -v --tb=short
# Requires: hardware, credentials, environment variables
```

### Compile check

```bash
python -m compileall -q src tests
# Expected: no output (all files compile cleanly)
```

### Full verification

Run all three test tiers plus the compile check before merging:

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src pytest -m docker -v
PYTHONPATH=src pytest -m live -v --tb=short
python -m compileall -q src tests
```

### Targeted runs during development

```bash
# Single test file
PYTHONPATH=src pytest tests/test_pipeline.py -v

# Files matching a prefix
PYTHONPATH=src pytest tests/test_matrix_session*.py -v

# Keyword match
PYTHONPATH=src pytest -k "test_delivery" -v

# Operator smoke command (Docker-free bridge validation)
PYTHONPATH=src medre smoke
PYTHONPATH=src medre smoke --json
```

### Failure interpretation

| Symptom | Likely cause | Action |
|---------|-------------|--------|
| Docker tests skip with "Docker not available" | Docker daemon not running | `docker info` |
| Docker tests skip with "mtjk not installed" | Meshtastic SDK not installed | `pip install -e ".[meshtastic]"` |
| Docker tests skip with "mindroom-nio not installed" | Matrix SDK not installed | `pip install -e ".[matrix]"` |
| Live tests skip | Missing environment variables | Set required `MATRIX_*` or `MESHTASTIC_*` env vars |
| Compile check produces output | Syntax error or import issue | Fix the reported file |
| `ResourceWarning` in test output | Unclosed resource or leaked coroutine | Fix the mock or add cleanup (see [Async Mocking Rules](#async-mocking-rules)) |


## Next Modernization Wave

The following allowlisted files should be split by subdomain. The suggested
split targets are starting points; actual domains may shift during analysis.

### Priority splits (largest files first)

**`tests/test_pipeline.py` (2,937 lines)**

| Target file | Domain |
|-------------|--------|
| `tests/test_pipeline_delivery.py` | Core delivery path: ingress, validation, routing, delivery |
| `tests/test_pipeline_failure_taxonomy.py` | Failure classification, error paths, retry budget |
| `tests/test_pipeline_fanout.py` | Multi-target delivery, fanout, error isolation |
| `tests/test_pipeline_native_refs.py` | NativeMessageRef resolution, cross-adapter mapping |
| `tests/test_pipeline_capacity.py` | Capacity limits, backpressure, overload behavior |

**`tests/test_matrix_session.py` (2,241 lines)**

| Target file | Domain |
|-------------|--------|
| `tests/test_matrix_session_lifecycle.py` | Session start, stop, health check |
| `tests/test_matrix_session_sync_recovery.py` | Initial sync, reconnection, error recovery |
| `tests/test_matrix_session_encryption.py` | E2EE setup, encrypted room handling |
| `tests/test_matrix_session_delivery_retry.py` | Delivery attempts, retry budgets, failure handling |

**`tests/test_cli.py` (2,172 lines)**

| Target file | Domain |
|-------------|--------|
| `tests/test_cli_parser.py` | Argument parsing, subcommand routing |
| `tests/test_cli_routes.py` | Route validation, route listing commands |
| `tests/test_cli_diagnostics.py` | Diagnostics, health check, snapshot commands |
| `tests/test_cli_inspect.py` | Inspect subcommands (event, receipts, native-ref) |
| `tests/test_cli_run.py` | `medre run`, `medre smoke`, `medre evidence` commands |

**`tests/test_replay.py` (1,850 lines)**

| Target file | Domain |
|-------------|--------|
| `tests/test_replay_engine.py` | Replay engine core: event selection, re-injection |
| `tests/test_replay_accounting.py` | Replay run tracking, receipt accounting |
| `tests/test_replay_stress.py` | High-volume replay, concurrency, resource limits |
| `tests/test_replay_policy.py` | Replay policy, filtering, deduplication behavior |

**`tests/test_storage.py` (1,939 lines)**

| Target file | Domain |
|-------------|--------|
| `tests/test_storage_sqlite.py` | SQLite-specific: persistence, concurrent access, WAL |
| `tests/test_storage_in_memory.py` | In-memory backend: consistency, lifecycle |
| `tests/test_storage_inspect.py` | Inspect queries: event, receipts, native refs |
| `tests/test_storage_durability.py` | Crash recovery, partial writes, schema validation |

### Analysis-pending splits

These files need domain analysis before split targets can be defined:

- `tests/test_canonical_events.py` (1,992 lines) -- domains TBD
- `tests/test_meshtastic_fake_bridge.py` (1,540 lines) -- domains TBD
- `tests/test_fake_runtime_smoke.py` (1,506 lines) -- domains TBD
- `tests/test_runtime_builder.py` (1,440 lines) -- domains TBD
