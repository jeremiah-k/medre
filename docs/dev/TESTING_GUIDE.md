# Resource-Warning Testing Guide

Rules and patterns for writing tests that do not leak resources. This guide
supplements the general [testing.md](testing.md) with concrete rules that
prevent `ResourceWarning`, `PytestUnraisableExceptionWarning`, and unawaited
coroutine noise in the test suite.

If a test introduces a new `ResourceWarning`, that test is broken. Fix the root
cause, never suppress the warning.

## Warnings-as-Errors Policy

Every test must pass under strict `ResourceWarning` promotion:

```bash
PYTHONPATH=src pytest -W error::ResourceWarning -q
```

This is the CI hardening baseline. Failures from this flag must be fixed, not
silenced with `filterwarnings` suppressions.

General test warnings policy is documented in
[testing.md: Warnings are bugs](testing.md#warnings-are-bugs). The rules below
are the specific practices that keep that policy green.

## Python 3.13 sqlite3 Behavior

Python 3.13 started emitting `ResourceWarning` for `sqlite3.Connection` objects
that are garbage-collected without an explicit `.close()`. Older Python
versions did not warn about this, so leaked connections could accumulate
silently for years.

The fix is not to downgrade Python. The fix is to close every connection.

### Symptom

```text
ResourceWarning: unclosed database in <sqlite3.Connection object at 0x...>
```

or under pytest:

```text
PytestUnraisableExceptionWarning: Exception ignored in: <sqlite3.Connection ...>
```

## Raw sqlite3.connect Rule

Any code that calls `sqlite3.connect()` directly must guarantee `.close()` is
called, regardless of how the function exits.

### Bad: unclosed connection

```python
def query_schema(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT * FROM schema").fetchall()
    return rows
    # conn never closed; ResourceWarning on Python 3.13+
```

### Good: context manager

```python
from contextlib import closing

def query_schema(path):
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT * FROM schema").fetchall()
        return rows
```

Or with an explicit `finally`:

```python
def query_schema(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT * FROM schema").fetchall()
    finally:
        conn.close()
```

`sqlite3.connect` is not a context manager for connection lifetime. The `with`
statement only manages transactions (commit/rollback), not the connection
itself. Wrap with `contextlib.closing()` or use `try/finally` to guarantee
`.close()` is called.

## SQLiteStorage Rule

`SQLiteStorage.initialize()` and `open_readonly()` open an internal
`sqlite3.Connection`. The caller that successfully calls either method owns the
obligation to call `close()`. `open_readonly()` is an async classmethod factory
called as `store = await SQLiteStorage.open_readonly(path)`, not an instance
method.

### Bad: unclosed storage

```python
async def test_storage_append():
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    await store.append(event)
    # store.close() never called; connection leaks
```

### GOOD: yield fixture

```python
@pytest.fixture
async def storage(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


async def test_storage_append(storage):
    await storage.append(event)
    # fixture teardown calls close()
```

### GOOD: explicit try/finally in the test

```python
async def test_storage_append(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    try:
        await store.append(event)
    finally:
        await store.close()
```

The yield-fixture pattern is preferred. It centralizes cleanup and prevents
forgetting `close()` when a test is later copy-pasted.

### initialize() failure path

If `initialize()` raises, the connection may have been partially opened. The
storage class handles this internally (idempotent close guards), but test code
should still use `try/finally` around the entire initialize-and-use sequence:

```python
async def test_initialize_failure(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    try:
        with pytest.raises(SomeError):
            await store.initialize()  # might fail partway
    finally:
        await store.close()  # safe even if initialize() never succeeded
```

`close()` is idempotent and guards against a `None` connection, so calling it
unconditionally in `finally` is always correct.

## CLI SystemExit Rule

CLI entry points sometimes call `sys.exit()` after printing a message. On
Python 3.13, if storage cleanup is pending when `SystemExit` propagates, the
interpreter can garbage-collect unclosed sqlite connections before their
`finally` blocks run, producing `ResourceWarning`.

### Bad: sys.exit before close

```python
async def handle_command(args):
    store = SQLiteStorage(db_path=args.db)
    await store.initialize()
    if not await verify_store_integrity(store):
        print("Database is corrupt")
        sys.exit(1)  # await store.close() never reached
    await store.close()
```

### Good: defer exit with try/finally

```python
async def handle_command(args):
    store = SQLiteStorage(db_path=args.db)
    await store.initialize()
    try:
        if not await verify_store_integrity(store):
            print("Database is corrupt")
            return 1  # defer exit
    finally:
        await store.close()
    return 0
```

Let the caller (usually `main()`) decide whether to call `sys.exit()` based on
the return code. This keeps cleanup in the `finally` block where it belongs.

### When SystemExit is unavoidable

If a library you don't control raises `SystemExit`, catch it in the test and
close storage manually:

```python
async def test_cli_exits_on_bad_config(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    try:
        with pytest.raises(SystemExit):
            cli_main(["--config", str(bad_config)])
    finally:
        await store.close()
```

## pytest-asyncio Loop Rule

Do not call `asyncio.run()` inside a test that runs under pytest-asyncio's
event loop management. `asyncio.run()` creates a new loop, destroys it on
return, and can leave dangling references that collide with the
pytest-asyncio-managed loop.

This project uses `asyncio_mode = "auto"` (see
[testing.md](testing.md#asyncio_mode--auto)). Write async tests as `async def`
and `await` directly.

### Bad: asyncio.run inside pytest-asyncio

```python
class TestStorage(unittest.TestCase):
    def test_append_and_get(self):
        asyncio.run(self._async_append_and_get())  # creates conflicting loop
```

### Good: native async def

```python
async def test_append_and_get(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    try:
        await store.append(event)
        result = await store.get(event.event_id)
        assert result is not None
    finally:
        await store.close()
```

### Sync test wrappers

If you must test a sync function that internally calls `asyncio.run()`, isolate
it in a dedicated test function with no other async resources. The general
async mocking rules in [testing.md](testing.md#async-mocking-rules) still
apply: match the mock type to the production call shape.

## App Lifecycle in Tests

Tests that construct a `MedreApp` or use `RuntimeBuilder` must clean up
according to what was built.

### Built but not started

A built app has a storage instance that is not yet initialized, plus
constructed adapters, and has not started any background tasks. Cleanup is
manual:

```python
async def test_builder_creates_storage(tmp_path):
    config = write_test_config(tmp_path)
    paths = write_test_paths(tmp_path)
    app = RuntimeBuilder(config, paths).build()
    try:
        assert app.storage is not None
    finally:
        # Stop any adapters that may have initialized during build
        for adapter in app.adapters.values():
            await adapter.stop(timeout=1)
        # PipelineRunner.stop() is safe even if start() was never called
        await app.pipeline_runner.stop()
        # Close storage if it was opened
        if app.storage is not None:
            await app.storage.close()
```

### Started app

A started app has background tasks running. Use `app.stop()`:

```python
async def test_app_starts_and_stops(tmp_path):
    config = write_test_config(tmp_path)
    paths = write_test_paths(tmp_path)
    app = RuntimeBuilder(config, paths).build()
    await app.start()
    try:
        # exercise the running app
        pass
    finally:
        await app.stop()
```

### Cleanup helper pattern

For tests that repeatedly build apps, use a fixture:

```python
from medre.runtime.app import RuntimeState

@pytest.fixture
async def built_app(tmp_path):
    config = write_test_config(tmp_path)
    paths = write_test_paths(tmp_path)
    app = RuntimeBuilder(config, paths).build()
    yield app
    # app.stop() is a no-op for INITIALIZED, so handle it manually
    if app.state not in (RuntimeState.INITIALIZED, RuntimeState.STOPPED):
        await app.stop()
    elif app.state == RuntimeState.INITIALIZED:
        # Built but never started: stop adapters, pipeline runner, and storage
        for adapter in app.adapters.values():
            await adapter.stop(timeout=1)
        await app.pipeline_runner.stop()
        if app.storage is not None:
            await app.storage.close()
    # STOPPED: already clean, nothing to do
```

The full shutdown sequence is documented in
[resource-lifecycle.md: Shutdown Sequence](resource-lifecycle.md#shutdown-sequence).

## Fixture Patterns

### Yield fixtures for resources

Any fixture that creates a resource with a lifecycle must use `yield` for
teardown, not a bare `close()` in the test body:

```python
# GOOD: yield fixture with cleanup
@pytest.fixture
async def storage(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


# BAD: bare resource created in test body
async def test_something(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    # ... 50 lines of test logic ...
    # forgot to close, or an early return skipped it
```

### Fixture ordering

When fixtures depend on each other, the teardown runs in reverse dependency
order. This means storage teardown happens after adapter teardown, which is the
correct order (adapters may need storage during their own stop):

```python
@pytest.fixture
async def storage(tmp_path):
    store = SQLiteStorage(db_path=tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def adapter(storage):
    adapter = FakeAdapter(storage=storage)
    await adapter.start()
    yield adapter
    await adapter.stop(timeout=1)
```

## AsyncMock vs Mock Decision Table

Using the wrong mock type is the most common source of unawaited coroutine
warnings. Match the mock type to what the production code actually does with the
callable.

| Production call shape             | Mock type       | Pattern                          |
| --------------------------------- | --------------- | -------------------------------- |
| `await store.close()`             | `AsyncMock`     | `store.close = AsyncMock()`      |
| `await store.initialize()`        | `AsyncMock`     | `store.initialize = AsyncMock()` |
| `adapter.start()` (never awaited) | `Mock`          | `adapter.start = Mock()`         |
| `await adapter.start()`           | `AsyncMock`     | `adapter.start = AsyncMock()`    |
| `adapter.config` (property)       | Plain attribute | `adapter.config = test_config`   |

**Rule of thumb**: if production code `await`s it, use `AsyncMock`. For
everything else, use `Mock`, `MagicMock`, or a plain attribute assignment.

### AsyncMock coroutine leak

When `AsyncMock` is called, it returns a coroutine object. If that coroutine is
never awaited (e.g., the test passes it to a mocked scheduler that discards it),
Python emits:

```text
RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
```

The fix is to use a regular `Mock` when the call site does not `await` the
result, or to close the coroutine in the scheduler fake.

## Coroutine Leak Prevention

Some failures only show up on certain Python versions (commonly 3.10, 3.11,
3.13+) as:

```text
PytestUnraisableExceptionWarning: coroutine ... was never awaited
```

### Rule 1: Match the production call shape exactly

If production does `fn(...)` (sync call), use `Mock` or a sync `def`, not
`AsyncMock`. If production does `await fn(...)`, use `AsyncMock` or an async
stub.

### Rule 2: Close coroutines in scheduler fakes

When faking scheduler submission helpers that accept coroutines, close the
passed coroutine before returning:

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

### Rule 3: Close awaitables in timeout fakes

When mocking `asyncio.wait_for` to raise `TimeoutError`, close the awaitable
first:

```python
def _timeout_wait_for(awaitable, timeout=None):
    if asyncio.iscoroutine(awaitable):
        awaitable.close()
    raise asyncio.TimeoutError()
```

### Rule 4: Use sync no-ops for sync-only paths

For cleanup hooks that are only called synchronously (e.g., a mocked
`disconnect()` in a sync branch), use:

```python
def _noop(*args, **kwargs):
    return None
```

Do not use `async def _noop` for sync paths. It creates a coroutine object
that gets discarded.

### Rule 5: Lightweight awaitable for close() stubs

When you need an awaitable `close()` stub without `AsyncMock` bookkeeping:

```python
class _ImmediateAwaitable:
    def __init__(self, value=None):
        self._value = value

    def __await__(self):
        if False:
            yield
        return self._value
```

### Rule 6: Be explicit when patching async functions

`patch("module.async_fn")` defaults to `AsyncMock`, which creates coroutine
objects when called. If the code under test passes that coroutine to a mocked
scheduler that raises early, the coroutine leaks. Use an explicit sync
`Mock(...)` when no await is needed, or make the scheduler fake close the
passed coroutine.

## Future Tooling: Sqlite Provenance Tracker

A provenance tracker can catch leaked sqlite connections at test teardown time
by wrapping `sqlite3.connect` to record the call site and stack trace of every
open connection. This is recommended future tooling, not currently implemented.

The tracker would work roughly as follows:

1. `conftest.py` patches `sqlite3.connect` with a wrapper that records
   `(connection_id, traceback, timestamp)` in a module-level set.
2. Each connection wrapper removes itself from the set when `.close()` is
   called.
3. A `pytest_sessionfinish` hook asserts the set is empty and prints the
   stack traces of any surviving connections.

This pattern has been validated in the meshtastic-matrix-relay project
at its own `tests/sqlite_provenance.py` (an external sibling repo, not a
MEDRE file). If leak noise resurfaces despite the rules above,
implementing this tracker here is the next step.

## Troubleshooting

### PytestUnraisableExceptionWarning from sqlite

```text
PytestUnraisableExceptionWarning: Exception ignored in: <sqlite3.Connection object at 0x...>
ResourceWarning: unclosed database in <sqlite3.Connection object at 0x...>
```

**Cause**: A `sqlite3.Connection` was opened but never `.close()`-d before
garbage collection.

**Fix**: Add a `try/finally` or use a yield fixture that calls `.close()` in
teardown. See [SQLiteStorage Rule](#sqlitestorage-rule) and
[Raw sqlite3.connect Rule](#raw-sqlite3connect-rule).

**Diagnosis**: Run the failing test file in isolation with:

```bash
PYTHONPATH=src pytest -W error::ResourceWarning tests/test_storage.py -v
```

The traceback usually points to the `initialize()` or `connect()` call site.

### PytestUnraisableExceptionWarning from asyncio loop

```text
PytestUnraisableExceptionWarning: Exception ignored in: <coroutine object ...>
RuntimeWarning: coroutine '...' was never awaited
```

**Cause**: A coroutine was created but never awaited or closed. Common
causes:

1. Using `AsyncMock` for a function that is called synchronously.
2. A scheduler fake that accepts a coroutine but raises without closing it.
3. Calling `asyncio.run()` inside a pytest-asyncio-managed test.

**Fix**: See [AsyncMock vs Mock Decision Table](#asyncmock-vs-mock-decision-table)
and [Coroutine Leak Prevention](#coroutine-leak-prevention).

### Unawaited coroutine warnings in CI only

Some coroutine leaks only manifest on specific Python versions. If CI fails
with unawaited coroutine warnings but local tests pass:

1. Check the CI Python version (likely 3.13+).
2. Run locally with the same Python version.
3. Add `-W error::RuntimeWarning` to expose the leak:

```bash
PYTHONPATH=src pytest -W error::RuntimeWarning -W error::ResourceWarning -q
```

### Test passes but leaves warnings in output

A test that passes but emits `ResourceWarning` is a broken test. The warning
means a resource was leaked. Even if the test assertion passes, the leaked
resource can cause cascading failures in later tests (file descriptor
exhaustion, sqlite lock contention, event loop pollution).

Fix the leak. Do not add `filterwarnings = ["ignore::ResourceWarning"]` to
suppress it.

### Storage fixture ordering causes "database is locked"

If two tests share the same database file and one fixture has not closed
storage before the next opens it, sqlite raises `database is locked`. The fix
is to use `tmp_path` (which gives each test a unique directory) and ensure
fixtures clean up in the correct order.

## What NOT to Copy from Other Projects

This guide is adapted from patterns proven in the meshtastic-matrix-relay
project. The following mmrelay-specific patterns do not apply to medre and
should not be ported:

- BLE serial connection fixtures and mocks
- Matrix SDK (nio) mock classes and facade testing patterns
- Meshtastic interface mock hierarchies and radio-specific assertions
- `reset_meshtastic_globals` / `reset_matrix_utils_globals` fixtures
- Plugin loader test decomposition (medre uses adapters, not plugins)
- nio `RoomSendError` / `InviteMemberEvent` mock class registration
- `MMRELAY_TESTING` environment variable branching (medre bans env-based test
  detection; see [testing.md](testing.md#no-compatibility-shims-in-tests))
- Trunk-based linting commands (medre uses its own tooling)
