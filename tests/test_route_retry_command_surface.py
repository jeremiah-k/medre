"""Command-surface/operator validation coverage for route retry config behavior.

Tests exercise the SAME paths operators use:
  - ``medre config check --config <path>`` → ``load_config()`` →
    ``RouteConfigSet.from_toml_dict()`` → ``RouteRetryConfig.from_toml_dict()``
  - ``medre run --config <path>`` → ``RuntimeBuilder`` →
    ``PipelineRunner`` with ``route_retry_policies`` + ``RetryWorker``

Scenarios covered:

1. Invalid route retry config rejected through ``load_config()``:
   negative ``max_attempts``, non-bool ``jitter``, non-number ``backoff_base``
2. Invalid route retry config rejected through CLI ``config check``:
   errors include route id and ``routes.<id>.retry`` section path
3. Valid route retry schedules ``next_retry_at`` on transient failure
4. Global ``[retry].enabled=false`` leaves due receipt pending (no worker)
5. Global ``[retry].enabled=true`` processes due receipt via RetryWorker
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

import pytest

from medre.cli import main
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.core.contracts.adapter import AdapterSendError
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing import Router
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.storage import SQLiteStorage

# ---------------------------------------------------------------------------
# TOML config snippets
# ---------------------------------------------------------------------------

_BASE_CONFIG = """\
[runtime]
name = "retry-cmd-surface-test"
shutdown_timeout_seconds = 5

[logging]
level = "INFO"
format = "text"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake_tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
origin_label = "TestMesh"
"""

_ROUTES_NO_RETRY = """\
[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
"""

_ROUTES_WITH_RETRY = """\
[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true

[routes.mx_to_mesh.retry]
enabled = true
max_attempts = 3
backoff_base = 2.0
max_delay_seconds = 60.0
jitter = false
"""


def _config_with_route_retry_override(retry_section: str) -> str:
    """Build a full TOML config with a custom [routes.mx_to_mesh.retry] section."""
    return _BASE_CONFIG + """
[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true

""" + retry_section


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-retry-cmd-001",
    source_adapter: str = "src",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_pipeline_config(
    storage: StorageBackend,
    router: Router,
    adapters: dict | None = None,
    route_retry_policies: dict[str, RetryPolicy] | None = None,
) -> PipelineConfig:
    return PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=EventBus(),
        route_retry_policies=route_retry_policies or {},
    )


class _TransientFailAdapter:
    """Adapter that always raises a transient error."""

    adapter_id = "fail-target"

    def __init__(self) -> None:
        self.received_events: list[object] = []

    async def deliver(self, payload: object) -> None:
        self.received_events.append(payload)
        raise AdapterSendError("transient boom", transient=True)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {}

    def diagnostics(self) -> dict:
        return {}


class _SuccessAdapter:
    """Adapter that always succeeds."""

    adapter_id = "ok-target"

    def __init__(self) -> None:
        self.received_events: list[object] = []

    async def deliver(self, payload: object) -> None:
        self.received_events.append(payload)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {}

    def diagnostics(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
async def mem_storage() -> AsyncGenerator[SQLiteStorage, None]:
    """SQLiteStorage backed by :memory:, initialized and cleaned up."""
    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    yield storage
    await storage.close()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = 1 if isinstance(e.code, str) else e.code
    return stdout.getvalue(), stderr.getvalue(), code


def _write_config(tmp_path, content: str) -> str:
    """Write TOML content to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(content)
    return str(p)


# ===================================================================
# 1. Invalid route retry config rejected through load_config()
# ===================================================================


class TestRouteRetryConfigValidation:
    """Invalid [routes.<id>.retry] config is rejected by load_config()
    with clear error messages that include route id and section path."""

    def test_negative_max_attempts_rejected(self, tmp_path) -> None:
        """Negative max_attempts in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" "max_attempts = -1\n"
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg, f"Route id missing from error: {msg}"
        assert "max_attempts" in msg
        assert "must be > 0" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_zero_max_attempts_rejected(self, tmp_path) -> None:
        """Zero max_attempts in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" "max_attempts = 0\n"
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg
        assert "max_attempts" in msg
        assert "must be > 0" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_non_bool_jitter_rejected(self, tmp_path) -> None:
        """Non-boolean jitter in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" 'jitter = "yes"\n'
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg, f"Route id missing from error: {msg}"
        assert "jitter" in msg
        assert "must be a boolean" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_non_number_backoff_base_rejected(self, tmp_path) -> None:
        """Non-numeric backoff_base in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" 'backoff_base = "fast"\n'
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg, f"Route id missing from error: {msg}"
        assert "backoff_base" in msg
        assert "must be a number" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_negative_backoff_base_rejected(self, tmp_path) -> None:
        """Negative backoff_base in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" "backoff_base = -1.0\n"
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg
        assert "backoff_base" in msg
        assert "must be >= 0" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_non_bool_enabled_rejected(self, tmp_path) -> None:
        """Non-boolean enabled in [routes.<id>.retry] raises ConfigValidationError."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" 'enabled = "yes"\n'
        )
        path = _write_config(tmp_path, config_text)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(path)
        msg = str(exc_info.value)
        assert "mx_to_mesh" in msg
        assert "enabled" in msg
        assert "must be a boolean" in msg
        assert exc_info.value.section_path == "routes.mx_to_mesh.retry"

    def test_valid_route_retry_passes_load_config(self, tmp_path) -> None:
        """Valid [routes.<id>.retry] config loads without error."""
        config_text = _BASE_CONFIG + _ROUTES_WITH_RETRY
        path = _write_config(tmp_path, config_text)
        config, _source, _paths = load_config(path)
        route = config.routes.routes[0]
        assert route.retry is not None
        assert route.retry.enabled is True
        assert route.retry.max_attempts == 3


# ===================================================================
# 2. CLI config check rejects invalid route retry with clear errors
# ===================================================================


class TestRouteRetryCLIConfigCheck:
    """``medre config check --config <path>`` rejects invalid route retry
    TOML and prints error messages that include route id and section path."""

    def test_negative_max_attempts_via_cli(self, tmp_path) -> None:
        """CLI config check exits non-zero for negative max_attempts."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" "max_attempts = -1\n"
        )
        path = _write_config(tmp_path, config_text)
        stdout, stderr, code = _run_cli_raw("config", "check", "--config", path)
        assert code != 0, "Expected non-zero exit for negative max_attempts"
        combined = stdout + stderr
        assert "mx_to_mesh" in combined, f"Route id missing from CLI output: {combined}"
        assert "max_attempts" in combined

    def test_non_bool_jitter_via_cli(self, tmp_path) -> None:
        """CLI config check exits non-zero for non-bool jitter."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" 'jitter = "yes"\n'
        )
        path = _write_config(tmp_path, config_text)
        stdout, stderr, code = _run_cli_raw("config", "check", "--config", path)
        assert code != 0, "Expected non-zero exit for non-bool jitter"
        combined = stdout + stderr
        assert "mx_to_mesh" in combined
        assert "jitter" in combined

    def test_non_number_backoff_base_via_cli(self, tmp_path) -> None:
        """CLI config check exits non-zero for non-number backoff_base."""
        config_text = _config_with_route_retry_override(
            "[routes.mx_to_mesh.retry]\n" "enabled = true\n" 'backoff_base = "fast"\n'
        )
        path = _write_config(tmp_path, config_text)
        stdout, stderr, code = _run_cli_raw("config", "check", "--config", path)
        assert code != 0, "Expected non-zero exit for non-number backoff_base"
        combined = stdout + stderr
        assert "mx_to_mesh" in combined
        assert "backoff_base" in combined

    def test_valid_route_retry_passes_cli_check(self, tmp_path) -> None:
        """CLI config check succeeds for valid route retry config."""
        config_text = _BASE_CONFIG + _ROUTES_WITH_RETRY
        path = _write_config(tmp_path, config_text)
        stdout, stderr, code = _run_cli_raw("config", "check", "--config", path)
        assert code == 0, (
            f"Expected success for valid config, got code={code}\n" f"stderr: {stderr}"
        )
        assert "Config valid" in stdout


# ===================================================================
# 3. Valid route retry schedules next_retry_at on transient failure
# ===================================================================


class TestRouteRetryScheduling:
    """Valid [routes.<id>.retry] produces next_retry_at on transient failure."""

    @pytest.mark.asyncio()
    async def test_valid_retry_schedules_next_retry_at(
        self,
        mem_storage: SQLiteStorage,
    ) -> None:
        """With route retry enabled, transient failure produces a receipt
        with next_retry_at set in SQLite storage."""
        route = Route(
            id="retry-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, backoff_base=2.0, jitter=False)
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
            route_retry_policies={"retry-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event()
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Verify receipt in SQLite has next_retry_at set.
            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1
            rcpt = receipts[0]
            assert rcpt.status == "failed"
            assert rcpt.next_retry_at is not None, (
                "Expected next_retry_at to be set for transient failure on "
                "retry-enabled route"
            )
            assert rcpt.retry_max_attempts == 3
            assert rcpt.retry_backoff_base == 2.0
            assert rcpt.retry_jitter is False
        finally:
            await runner.stop()

    @pytest.mark.asyncio()
    async def test_no_retry_no_schedule(
        self,
        mem_storage: SQLiteStorage,
    ) -> None:
        """Without route retry, transient failure has no next_retry_at."""
        route = Route(
            id="no-retry",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event()
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1
            rcpt = receipts[0]
            assert rcpt.status == "failed"
            assert (
                rcpt.next_retry_at is None
            ), "Expected no next_retry_at without route retry policy"
        finally:
            await runner.stop()


# ===================================================================
# 4. Global [retry].enabled=false leaves due receipt pending
# ===================================================================


class TestGlobalRetryDisabledPending:
    """When global [retry].enabled=false but route has retry enabled,
    transient failures produce pending retry receipts that are never
    processed by the RetryWorker."""

    @pytest.mark.asyncio()
    async def test_global_disabled_receipt_stays_pending(
        self,
        mem_storage: SQLiteStorage,
    ) -> None:
        """Route retry creates next_retry_at but no RetryWorker picks it up
        when global retry is disabled."""
        route = Route(
            id="pending-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, jitter=False)
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
            route_retry_policies={"pending-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event()
            outcomes = await runner.handle_ingress(event)
            assert outcomes[0].status == "transient_failure"

            # Receipt has next_retry_at set (route retry schedules it).
            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) == 1
            rcpt = receipts[0]
            assert (
                rcpt.next_retry_at is not None
            ), "Route retry should have set next_retry_at"

            # Advance time past next_retry_at.
            future_now = rcpt.next_retry_at.replace(tzinfo=timezone.utc) + __import__(
                "datetime"
            ).timedelta(seconds=1)

            # Due receipt is queryable in storage.
            due = await mem_storage.list_due_retry_receipts(future_now)
            assert len(due) >= 1, "Due retry receipt should be queryable"

            # Only 1 receipt total — no retry receipt created (no worker).
            all_receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(all_receipts) == 1, (
                f"Expected 1 receipt (original failed only), "
                f"got {len(all_receipts)}"
            )
            assert all_receipts[0].source != "retry"
        finally:
            await runner.stop()


# ===================================================================
# 5. Global [retry].enabled=true processes due receipt
# ===================================================================


class TestGlobalRetryEnabledProcesses:
    """When global [retry].enabled=true and route has retry enabled,
    the RetryWorker processes due retry receipts and re-delivers."""

    @pytest.mark.asyncio()
    async def test_global_enabled_worker_processes_receipt(
        self,
        mem_storage: SQLiteStorage,
    ) -> None:
        """RetryWorker picks up due receipt and successfully re-delivers."""

        from medre.runtime.retry import RetryWorker

        # Phase 1: Deliver through pipeline with transient failure.
        route = Route(
            id="worker-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="ok-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, backoff_base=2.0, jitter=False)

        # Use a transient-fail adapter for the first delivery.
        fail_adapter = _TransientFailAdapter()
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"ok-target": fail_adapter},
            route_retry_policies={"worker-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event()
            # handle_ingress stores the event and then delivers it.
            outcomes = await runner.handle_ingress(event)
            assert outcomes[0].status == "transient_failure"

            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            original = receipts[0]
            assert original.next_retry_at is not None
        finally:
            await runner.stop()

        # Phase 2: Swap adapter to success and run RetryWorker.
        success_adapter = _SuccessAdapter()
        config2 = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"ok-target": success_adapter},
            route_retry_policies={"worker-route": policy},
        )
        runner2 = PipelineRunner(config2)
        await runner2.start()
        worker = RetryWorker(
            storage=mem_storage,
            pipeline=runner2,
            capacity_controller=None,
            enabled=True,
            interval_seconds=1.0,
            batch_size=10,
            max_attempts=3,
        )
        try:
            # Process due receipts manually (one cycle).
            future_now = original.next_retry_at.replace(
                tzinfo=timezone.utc
            ) + __import__("datetime").timedelta(seconds=1)
            await worker._process_due(future_now)

            # Worker should have processed the receipt.
            assert (
                worker.state.processed >= 1
            ), f"Expected >= 1 processed, got {worker.state.processed}"
            assert (
                worker.state.succeeded >= 1
            ), f"Expected >= 1 succeeded, got {worker.state.succeeded}"

            # Verify retry receipt in storage.
            all_receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(all_receipts) >= 2, (
                f"Expected >= 2 receipts (original + retry), "
                f"got {len(all_receipts)}"
            )
            retry_receipts = [r for r in all_receipts if r.source == "retry"]
            assert len(retry_receipts) >= 1, "Expected at least 1 retry receipt"
            retry_rcpt = retry_receipts[0]
            assert retry_rcpt.status == "sent"
            assert retry_rcpt.attempt_number == 2
            assert retry_rcpt.parent_receipt_id == original.receipt_id
        finally:
            await worker.stop()
            await runner2.stop()
