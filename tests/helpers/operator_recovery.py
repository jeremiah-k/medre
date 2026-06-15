"""Shared fixtures and helpers for operator-recovery domain test modules.

Holds the common YAML config snippets, pytest fixtures, CLI runners, and
runtime construction helpers used by multiple operator-recovery test files:

- ``tests/test_config_repair.py`` — malformed config, storage path, repair workflows
- ``tests/test_startup_recovery.py`` — startup failure, degraded runtime, adapter lifecycle
- ``tests/test_deterministic_messaging.py`` — no-traceback assertions, deterministic shapes
- ``tests/test_operator_recovery.py`` (slimmed) — route validation recovery, replay after restart

Items used by exactly one domain file stay in their consumer module; only
fixtures/constants/helpers used by two or more files live here. Import via::

    from tests.helpers.operator_recovery import _clean_env, tmp_paths, ...
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# YAML config snippets shared across modules
# ---------------------------------------------------------------------------

CONFIG_BAD_YAML = """\
runtime:
  name: [unclosed sequence
"""

CONFIG_MISSING_ADAPTER_REF = """\
runtime:
  name: bad-route-refs
storage:
  backend: memory
adapters:
  matrix:
    real_adapter:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
routes:
  broken_route:
    source_adapters:
      - real_adapter
    dest_adapters:
      - ghost_adapter
    directionality: source_to_dest
    enabled: true
"""

# ---------------------------------------------------------------------------
# Fixtures (reference definitions — pytest does not discover fixtures
# imported from non-conftest helpers, so each consumer module re-declares
# ``_clean_env`` and ``tmp_paths`` locally. Kept here as the canonical
# reference, matching the tests/helpers/startup_cleanup.py pattern.)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at temp directories."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _write_config(path: Path, content: str) -> Path:
    """Write YAML content to *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run_cli(*args: str) -> str:
    """Run CLI, capture stdout, return output. Propagate non-zero SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


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


# ---------------------------------------------------------------------------
# Runtime construction helpers
# ---------------------------------------------------------------------------


class _FailingAdapter(AdapterContract):
    """Adapter that raises on start() for failure-recovery testing."""

    adapter_id: str = "failing_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_adapter") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated adapter failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


def _fake_matrix_runtime_config(
    adapter_id: str = "fake_matrix",
    enabled: bool = True,
) -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _fake_meshtastic_runtime_config(
    adapter_id: str = "fake_mesh",
    enabled: bool = True,
) -> MeshtasticRuntimeConfig:
    return MeshtasticRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _config_with_fake_adapters(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with two fake adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
            meshtastic={"radio": _fake_meshtastic_runtime_config()},
        ),
    )


def _config_with_one_fake_adapter(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with one fake adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery-single"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()
