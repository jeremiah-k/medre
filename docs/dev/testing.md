# Testing Guide

This guide covers testing patterns, rules, and conventions for the MEDRE
project. It is the authoritative reference for how tests are written, what
each test tier proves, and how to run the suite.

The test suite has 3,200+ tests with zero network or hardware dependencies.
Every transport has a fake adapter that exercises the full pipeline. The
standard `pytest -q` run requires a generous timeout (the full suite can
exceed 180 s on slower machines). See the [README](../../README.md) for project context and the
[Operator Workflows](../ops/operator-workflows.md) for bridge-specific test
commands.

## File Size Limits

> **Agent responsibility**: Before adding tests to any file, check its current
> line count (`wc -l <file>`). If the file is anywhere near 1,500 lines, create
> a new file instead. Anything over 1,500 lines causes CI to fail
> (`test_no_file_exceeds_1500_lines`). When in doubt, start a new file.

**Target: < 1,200 lines per test file.** Hard ceiling: 1,500 lines unless
explicitly justified in the file and in this guide.

Split files by behavioral domain, not by "coverage" or "misc". When a domain
file approaches the target, split it by subdomain following the procedure in
the [Splitting procedure](#splitting-procedure) section below.

### Size enforcement

There is **no oversized-test allowlist**. Every `test_*.py` file stays at
or below **1,500 lines** (`MAX_LINES`). The target remains below 1,200 lines.
If a file approaches the hard cap, split it by behavioral domain following
the procedure in the [Splitting procedure](#splitting-procedure) section.
Completed splits are listed in the [Completed Splits](#completed-splits)
table as historical record, not as active allowlist entries.

#### Next-PR candidates near the cap

These files are approaching the 1,500-line hard cap and should be split
opportunistically by behavioral domain:

- `test_runtime_snapshot.py` (1,301 lines)
- `test_trace.py` (1,425 lines)
- `test_replay_recover.py` (1,454 lines)
- Any other 1,000+ line file should be split when convenient

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

New tests use pytest function style (module-level `async def` or `def`),
not `unittest.TestCase`. Existing `TestCase` classes are acceptable as-is
but should not be extended with new test methods.

```python
# Preferred: pytest async function
async def test_adapter_delivers_event(fake_adapter, canonical_event):
    result = await fake_adapter.deliver(canonical_event)
    assert result.status == "success"


# Existing pattern: TestCase classes (do not extend with new methods)
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

Never use fixed sleeps directly in tests; use `wait_until()` or deterministic
hooks (`asyncio.Event`, mock callbacks).

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

| Production call                     | Mock type                         | Example                                   |
| ----------------------------------- | --------------------------------- | ----------------------------------------- |
| `await client.close()`              | `AsyncMock`                       | `client.close = AsyncMock()`              |
| `client.add_event_callback(fn)`     | `MagicMock` (never awaited)       | `client.add_event_callback = MagicMock()` |
| `await session.start()`             | `AsyncMock`                       | `session.start = AsyncMock()`             |
| `session.config` (attribute access) | Plain attribute or `PropertyMock` | `session.config = test_config`            |

If production code `await`s the callable, use `AsyncMock`. For everything
else, use `Mock` or `MagicMock`.

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

Async fakes that simulate cancellation raise `asyncio.CancelledError`,
not return a value or raise a different exception. Tests that exercise
cancellation paths catch `CancelledError` explicitly:

```python
async def _cancel_immediately(*args, **kwargs):
    raise asyncio.CancelledError()
```

## Adapter/Bridge Test Tiers

Tests are classified into five tiers based on what they honestly prove. Never
overclaim the evidence level of a test. If a test uses fake adapters, call it
"fake pipeline", not "docker" or "live".

| Tier | Label                   | What it proves                                                                 | How to test                                                                           |
| ---- | ----------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1    | `fake_pipeline`         | `PipelineRunner.handle_ingress()` works with direct `CanonicalEvent` injection | Import `CanonicalEvent`, construct it, call `runner.handle_ingress()` directly        |
| 2    | `fake_adapter_callback` | `adapter.simulate_inbound()` produces the same results as direct injection     | Use `FakeMatrixAdapter.simulate_inbound()`, compare output with direct injection      |
| 3    | `wrapper_callback`      | Real adapter SDK callback (e.g., `_on_room_message`) bridges to fake target    | Mock the SDK, test the wrapper callback through the pipeline to a fake target adapter |
| 4    | `docker_sdk_boundary`   | Real SDK code paths work against containerized services (Synapse, meshtasticd) | Docker Compose tests, gated by `@pytest.mark.docker`                                  |
| 5    | `live_network`          | Real adapter against real endpoint or hardware                                 | `@pytest.mark.live`, requires environment variables                                   |

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

Tests verify that SQLite and in-memory storage backends behave
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
print it, and close. They never modify the database. Tests for inspect
subcommands assert that the database content is unchanged after the
command runs.

### Schema version

Schema version stays at 1 pre-release. No schema bumps until beta. Tests
assert `schema_version == 1` and that opening a v1 database does not
trigger migration.

## Operator/CLI Tests

### Normalized JSON shape assertions

Smoke, drill, evidence, trace, and recover commands produce structured JSON
output. Tests assert the normalized JSON shape using shared assertion
helpers from `tests/helpers/assertions.py`.

```python
from tests.helpers.assertions import assert_report_shape

def test_smoke_report_json_shape(smoke_report):
    assert_report_shape(smoke_report)
    assert smoke_report["status"] == "passed"
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

### Patching adapter modules

Adapter package root `__init__.py` files are lightweight package markers and
should not be used as patch targets. Patch the concrete definition/use site
instead:

```python
# Correct: patch the actual module where the symbol is defined/used
from unittest.mock import patch
with patch("medre.adapters.matrix.compat.HAS_NIO", False):
    ...  # tests that need HAS_NIO=False run here

# Wrong: adapter package roots are docstring-only with no re-exports
# from medre.adapters.matrix import HAS_NIO  # This will not work
```

## Compatibility

### No compatibility shims in tests

Tests must not contain compatibility shims, version detection branches, or
environment-specific workarounds (e.g., `if os.getenv("MEDRE_TESTING")`).
Test and production code paths are identical.

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

All Docker tests use the `@pytest.mark.docker` decorator:

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
# Expected: 3,200+ collected, live and Docker tests skipped by default.
# The full suite can take several minutes; use timeout 300 or run subsets
# during development (see targeted runs below).
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
PYTHONPATH=src pytest tests/test_pipeline_delivery.py -v

# Files matching a prefix
PYTHONPATH=src pytest tests/test_matrix_session*.py -v

# Keyword match
PYTHONPATH=src pytest -k "test_delivery" -v

# Operator smoke command (Docker-free bridge validation)
PYTHONPATH=src medre smoke
PYTHONPATH=src medre smoke --json
```

### Identifying hanging tests

When the full suite hangs, use this per-file timeout loop to isolate which test
file is blocking:

```bash
# Run from the repository root
set -o pipefail
find tests -type f -name 'test_*.py' | sort | while read -r f; do
  echo -n "$(basename "$f"): "
  out="$(PYTHONPATH=src timeout 90 python -m pytest -q "$f" 2>&1)"
  status=$?
  printf '%s\n' "$out" | head -3
  if [ "$status" -eq 124 ]; then
    echo "TIMEOUT (124)"
  fi
done
```

Each file gets a 90-second timeout. Files that hang will report timeout exit code
`124`. Note that ordering/pollution across files can mask the real hang, so
always re-run suspect files in isolation to confirm.

### Test-execution discipline for agents

> **Agent responsibility**: These rules prevent timeout-tuning loops, output
> truncation, and retry storms. Violating them wastes time and hides real
> failures. Follow them exactly.

1. **No `timeout` wrappers for routine pytest runs.** Run `pytest` directly.
   The shell `timeout` command is reserved for the diagnostic loop above, not
   for routine validation or failure collection.

2. **No `tail`, `head`, grep-piping, or output truncation.** Capture full
   pytest output. Piping through `tail`, `head`, or `grep` hides failure
   context (tracebacks, fixture teardown errors, import failures). If the
   output is large, save it to a file or scroll it — never truncate it.

3. **No broad suite after scoped validation passes.** Once a targeted file or
   keyword run passes, do not escalate to the full suite "just to be sure"
   unless explicitly requested. The full suite is for pre-merge verification,
   not iterative debugging.

4. **If a test hangs or times out once, stop.** Do not rerun it with a longer
   or different timeout. A hang is the test telling you something is wrong
   (deadlock, missing cleanup, infinite loop). Longer timeouts do not fix the
   underlying issue — they just waste time before the same hang.

5. **Capture full pytest output once for a failure.** When a test fails, the
   first run's output is the evidence. Read it completely before taking any
   other action.

6. **Static-read the failing test and its source before any rerun.** Before
   rerunning a failing test, use the Read tool to examine both the test file
   and the source module it exercises. Identify the likely cause from the
   code, not from repeated execution.

7. **Rerun at most once after a concrete suspected fix.** Only rerun after
   making an edit that addresses a specific, identified cause. If the rerun
   still fails, stop and investigate further — do not loop on runs.

8. **Report blockers instead of looping.** If the test still hangs or fails
   after one fix-attempt rerun, stop and report the issue with: the full
   pytest output, the test file path, the source path, and what was tried.
   Do not continue running the test with variations.

9. **Do not repeatedly run hanging tests with varying timeouts.** This is
   worth stating twice: a test that hangs at 30 s will also hang at 60 s,
   90 s, and 300 s. Varying the timeout does not diagnose or fix the hang.

10. **Do not use shell pipes to filter pytest output during debugging.**
    Pipes hide information. If you need specific lines, use the Grep or Read
    tool on saved output, not shell-level truncation.

### Failure interpretation

| Symptom                                             | Likely cause                          | Action                                                                        |
| --------------------------------------------------- | ------------------------------------- | ----------------------------------------------------------------------------- |
| Docker tests skip with "Docker not available"       | Docker daemon not running             | `docker info`                                                                 |
| Docker tests skip with "mtjk not installed"         | Meshtastic SDK not installed          | `pip install -e ".[meshtastic]"`                                              |
| Docker tests skip with "mindroom-nio not installed" | Matrix SDK not installed              | `pip install -e ".[matrix]"`                                                  |
| Live tests skip                                     | Missing environment variables         | Set required `MATRIX_*` or `MESHTASTIC_*` env vars                            |
| Compile check produces output                       | Syntax error or import issue          | Fix the reported file                                                         |
| `ResourceWarning` in test output                    | Unclosed resource or leaked coroutine | Fix the mock or add cleanup (see [Async Mocking Rules](#async-mocking-rules)) |

## Completed Splits

These files have been split by behavioral domain following the procedure above.

| Original file                           | Result  | Domain files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/test_adapter_callback_bridge.py` | Split   | 6 domain files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `tests/test_longrun_callback_bridge.py` | Split   | 4 domain files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `tests/test_operator_workflows.py`      | Split   | 7 domain files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `tests/test_pipeline.py`                | Split   | 5 domain files (delivery, failure taxonomy, fanout, native refs, capacity)                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `tests/test_replay.py`                  | Split   | 5 domain files (engine, policy, accounting, capacity, traceability)                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `tests/test_cli.py`                     | Split   | 9 domain files: `test_cli_command_help_hints`, `test_cli_config_workflows`, `test_cli_diagnostics_workflows`, `test_cli_install_metadata`, `test_cli_replay_surface`, `test_cli_route_workflows`, `test_cli_run_workflows`, `test_cli_scenario_crosscheck`, `test_cli_smoke_run_session`. Helper: `helpers/cli.py`.                                                                                                                                                                                                     |
| `tests/test_alpha_walkthrough_cli.py`   | Split   | 4 domain files: `test_alpha_cli_config_and_smoke`, `test_alpha_cli_inspect_flow`, `test_alpha_cli_replay_flow`, `test_alpha_cli_error_paths`. Helper: `helpers/alpha_cli.py`.                                                                                                                                                                                                                                                                                                                                           |
| `tests/test_docker_bridge_artifacts.py` | Split   | 4 domain files: `test_docker_artifact_core`, `test_docker_artifact_plan`, `test_docker_artifact_metadata`, `test_docker_artifact_honesty`. Helper: `helpers/docker_artifacts.py`.                                                                                                                                                                                                                                                                                                                                       |
| `tests/test_matrix_session.py`          | Split   | 3 domain files: `test_matrix_session_config` (encryption config), `test_matrix_session_e2ee` (Megolm, encrypted rooms, E2EE diagnostics), `test_matrix_session_recovery` (sync failure, reconnect, crypto store continuity, sync state resilience). Original retained at 460 lines (lifecycle, diagnostics, start behavior).                                                                                                                                                                                            |
| `tests/test_storage.py`                 | Split   | 7 domain files: `test_storage_durability`, `test_storage_integrity`, `test_storage_invariants`, `test_storage_native_refs`, `test_storage_path_cli`, `test_storage_path_validation`, `test_storage_receipts`. Original retained at 231 lines.                                                                                                                                                                                                                                                                           |
| `tests/test_replay_routing.py`          | Split   | 3 domain files: `test_replay_routing_controls`, `test_replay_routing_durability`, `test_replay_routing_isolation`. Original retained at 422 lines.                                                                                                                                                                                                                                                                                                                                                                      |
| `tests/test_runtime_builder.py`         | Split   | 3 domain files: `test_runtime_builder_ordering` (build ordering, adapter ID propagation), `test_runtime_builder_paths` (Matrix store path derivation, ensure-dirs), `test_runtime_builder_routes` (degraded route validation). Original retained at 520 lines (construction, config, fakes).                                                                                                                                                                                                                            |
| `tests/test_meshtastic_adapter.py`      | Split   | 1 domain file: `test_meshtastic_adapter_delivery` (send semantics, session boundary, session unit). Original retained at 755 lines (connection modes, queue ownership, lifecycle).                                                                                                                                                                                                                                                                                                                                      |
| `tests/test_meshtastic_fake_bridge.py`  | Split   | 2 domain files: `test_meshtastic_fake_bridge_errors`, `test_meshtastic_fake_bridge_session`. Original retained at 938 lines.                                                                                                                                                                                                                                                                                                                                                                                            |
| `tests/test_storage_outbox.py`          | Deleted | 5 domain files: `test_storage_outbox_crud` (create, get, idempotent create, list, count, persistence), `test_storage_outbox_claim` (claim due, release claim, claim clears next_attempt_at), `test_storage_outbox_status` (status transitions, transition guards, queued lease semantics), `test_storage_outbox_atomic_create` (atomic create, no-steal guarantees), `test_storage_outbox_concurrency` (write lock serialisation, transaction rollback, stale queued reclaim, is_claimable property). Original deleted. |
| `tests/test_fake_runtime_smoke.py`      | Split   | 2 domain files: `test_fake_runtime_soak` (diagnostics snapshots, replay delivery, happy path), `test_fake_runtime_startup_snapshot` (startup/shutdown integration, snapshot integration). Original retained at 931 lines.                                                                                                                                                                                                                                                                                               |

## CLI split -- completed

`test_cli.py` has been split into domain files (all under 1,500 lines). The
monolith has been deleted. `test_cli` is listed in `DELETED_MONOLITHS` in
`test_test_suite_structure.py`.

## See also

- [Adapter authoring guide](adapter-authoring.md) -- writing a new transport adapter and its fake
- [Source audits](source-audits.md) -- audit evidence for transport SDK assumptions
- [Operator workflows](../ops/operator-workflows.md) -- operator commands for bridge testing
