"""Track 9 — Final scope/boundary audit for beta candidate closure.

These tests enforce the **beta scope contract**: a closed, auditable boundary
on what the beta candidate may and may not contain.  They use **source-level
text inspection** (not runtime importing of optional SDKs) and cover:

1. No transport SDK imports in runtime/core modules.
2. No live tests enabled by default (addopts + marker discipline).
3. No admin server or admin handler subsystem.
4. No webhook handler subsystem.
5. No plugin loader/runtime/manager — only protocol scaffolding allowed.
6. No distributed dependencies (redis, celery, kafka, rabbitmq, zmq, …).
7. No persistent queue infrastructure.
8. No replay deduplication engine (only run_id tracking allowed).
9. No dynamic route/config reload mechanism.
10. No package split — single ``medre`` package only.
11. No canonical event redesign — single stable ``CanonicalEvent`` in
   ``medre.core.events.canonical``.

Pattern
-------
All tests use source-level text inspection.  This avoids triggering SDK
imports at test collection time and works in environments where some or
all SDKs are not installed.

The helper functions and scanning patterns are reused from
``test_operational_boundaries.py`` and
``test_runtime_durability_boundaries.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from medre.runtime.architecture_report import _SDK_PACKAGES
from tests.helpers.source_reader import source_of as _source_of

# ---------------------------------------------------------------------------
# Shared helpers (reused from test_operational_boundaries.py)
# ---------------------------------------------------------------------------

_ADAPTER_PREFIXES = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)
"""Concrete adapter package prefixes (excludes medre.core.contracts.adapter and fake_*)."""

_ADAPTER_COMPAT_MODULES = (
    "medre.adapters.matrix.compat",
    "medre.adapters.meshtastic.compat",
    "medre.adapters.meshcore.compat",
    "medre.adapters.lxmf.compat",
)
"""Adapter compat modules that are ALLOWED to import SDKs internally."""

_DISTRIBUTED_PACKAGES = (
    "redis",
    "celery",
    "kafka",
    "kafka-python",
    "confluent_kafka",
    "rabbitmq",
    "pika",
    "aio_pika",
    "zmq",
    "pyzmq",
    "kombu",
    "dramatiq",
    "huey",
    "rq",
    "aerospike",
    "cassandra",
    "cassandra-driver",
    "motor",
    "pymongo",
    "aiomcache",
    "memcache",
    "pylibmc",
)
"""Third-party distributed-infrastructure package names."""

_TESTS_DIR = Path(__file__).parent
"""Root tests directory."""

_SRC_ROOT = _TESTS_DIR.parent / "src" / "medre"
"""Root source directory for medre package."""


def _import_lines(source: str) -> list[str]:
    """Extract all import/from-import lines from source text.

    See also: architecture_ast.runtime_scope_imports() for AST-based
    import extraction (returns ImportRecord objects with resolved names).
    """
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def _banned_imports(lines: list[str], banned: tuple[str, ...]) -> list[str]:
    """Return import lines referencing any banned package.

    See also: architecture_ast.import_matches() for module-prefix matching
    on resolved module names (AST-level, not text-level).
    """
    found: list[str] = []
    for line in lines:
        for b in banned:
            if re.search(rf"\b{re.escape(b)}\b", line):
                found.append(line)
                break
    return found


def _file_source(path: Path) -> str:
    """Read source from a file path."""
    return path.read_text()


def _scan_file_for_banned_imports(
    path: Path,
    banned: tuple[str, ...],
) -> list[str]:
    """Scan a file for banned import-line prefixes.

    Returns list of ``"{filename}:{lineno}: {line}"`` strings for violations.
    Skips comment lines.
    """
    source = _file_source(path)
    violations: list[str] = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in banned:
            if pattern in stripped:
                violations.append(f"{path.name}:{i}: {stripped}")
                break
    return violations


def _scan_source_for_pattern(
    source: str,
    filename: str,
    patterns: tuple[str, ...],
    *,
    skip_docstrings: bool = True,
) -> list[str]:
    """Scan source text for banned patterns.

    Returns list of ``"{filename}:{lineno}: {line}"`` strings for violations.
    Skips comment lines.  Optionally skips lines that look like docstring
    content (triple-quoted blocks).
    """
    violations: list[str] = []
    in_docstring = False
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if skip_docstrings:
            # Track triple-quote boundaries (simple heuristic).
            count = stripped.count('"""')
            if count == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
        for pattern in patterns:
            if pattern in stripped:
                violations.append(f"{filename}:{i}: {stripped}")
                break
    return violations


def _all_source_py_files(root: Path) -> list[Path]:
    """Return all .py files under root, excluding __pycache__."""
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ===================================================================
# 1. No transport SDK imports in runtime/core
# ===================================================================


class TestNoTransportSdkInRuntimeCore:
    """Runtime and core modules must not import any transport SDK.

    The runtime layer (``medre.runtime.*``) and core layer
    (``medre.core.*``) must remain transport-agnostic.  Only adapter
    compat modules (``medre.adapters.*.compat``) may import SDKs.

    Adapter config dataclasses (``medre.config.adapters.*``) are pure
    data — they import no SDKs and are excluded from this check.
    """

    _RUNTIME_MODULES = [
        "medre.runtime",
        "medre.runtime.app",
        "medre.runtime.builder",
        "medre.core.runtime.capacity",
        "medre.runtime.errors",
        "medre.runtime.observability",
        "medre.runtime.route_engine",
        "medre.config.routes",
        "medre.runtime.snapshot",
        "medre.runtime.boot_summary",
    ]

    _CORE_MODULES = [
        "medre.core",
        "medre.core.diagnostics",
        "medre.core.diagnostics.replay_metrics",
        "medre.core.diagnostics.snapshot",
        "medre.core.engine",
        "medre.core.engine.pipeline",
        "medre.core.events",
        "medre.core.events.bus",
        "medre.core.events.canonical",
        "medre.core.events.kinds",
        "medre.core.events.metadata",
        "medre.core.events.schema",
        "medre.core.identity",
        "medre.core.identity.actor",
        "medre.core.identity.resolver",
        "medre.core.lifecycle",
        "medre.core.lifecycle.manager",
        "medre.core.lifecycle.states",
        "medre.core.observability",
        "medre.core.observability.logging",
        "medre.core.observability.metrics",
        "medre.core.planning",
        "medre.core.planning.delivery_plan",
        "medre.core.planning.relation_resolution",
        "medre.core.planning.fallback_resolution",
        "medre.core.policies",
        "medre.core.rendering",
        "medre.core.rendering.renderer",
        "medre.core.rendering.text",
        "medre.core.routing",
        "medre.core.routing.models",
        "medre.core.routing.router",
        "medre.core.routing.stats",
        "medre.core.runtime",
        "medre.core.runtime.accounting",
        "medre.core.runtime.capabilities",
        "medre.core.runtime.diagnostic_contract",
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.health",
        "medre.core.runtime.supervision",
        "medre.core.storage",
        "medre.core.storage.backend",
        "medre.core.storage.replay",
        "medre.core.storage.sqlite",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _RUNTIME_MODULES + _CORE_MODULES,
        ids=_RUNTIME_MODULES + _CORE_MODULES,
    )
    def test_no_sdk_imports(self, module_name: str) -> None:
        """Module must not import any transport SDK package."""
        try:
            source = _source_of(module_name)
        except (FileNotFoundError, ModuleNotFoundError):
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"{module_name} imports transport SDKs: {banned}"

    @pytest.mark.parametrize(
        "module_name",
        _RUNTIME_MODULES + _CORE_MODULES,
        ids=_RUNTIME_MODULES + _CORE_MODULES,
    )
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        """Module must not import concrete adapter packages.

        Imports from ``medre.core.contracts.adapter`` (protocol types) and
        ``medre.adapters.fake_*`` are permitted.
        """
        try:
            source = _source_of(module_name)
        except (FileNotFoundError, ModuleNotFoundError):
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"{module_name} imports concrete adapter packages: {banned}"


# ===================================================================
# 2. No live tests enabled by default
# ===================================================================


class TestNoLiveTestsByDefault:
    """Default pytest invocation must not run live tests.

    The ``pyproject.toml`` must exclude the ``live`` marker in
    ``addopts``, and the ``live`` marker must be registered.
    """

    def test_addopts_excludes_live(self) -> None:
        """``pyproject.toml`` must have ``addopts = "-m 'not live'"``."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml not found"
        content = _file_source(pyproject)
        assert "not live" in content, (
            "pyproject.toml addopts must exclude live marker "
            "(expected: addopts = \"-m 'not live'\")"
        )

    def test_live_marker_registered(self) -> None:
        """``pyproject.toml`` must register the ``live`` marker."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        content = _file_source(pyproject)
        assert (
            "live:" in content
        ), "pyproject.toml must register 'live' marker in markers list"


# ===================================================================
# 3. No admin server/handler subsystem
# ===================================================================


class TestNoAdminSubsystem:
    """Beta must not contain an admin server, admin handler, or admin API.

    Admin interfaces (REST, HTTP, websocket) are post-beta scope.
    This checks that no source file defines admin-server classes or
    imports admin-related frameworks.

    Note: string literals like ``"admin"`` in Meshtastic packet
    classification (portnum mapping) are NOT admin subsystem code —
    they are adapter data-plane constants and are explicitly excluded
    from violation matching.
    """

    _ADMIN_CLASS_PATTERNS = (
        "class AdminServer",
        "class AdminHandler",
        "class AdminAPI",
        "class AdminPanel",
        "class AdminRouter",
    )

    _ADMIN_IMPORT_PATTERNS = (
        "from fastapi_admin",
        "import fastapi_admin",
        "from flask_admin",
        "import flask_admin",
        "from django.contrib.admin",
        "import django.contrib.admin",
    )

    def test_no_admin_classes(self) -> None:
        """No source file defines an admin-server class."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in self._ADMIN_CLASS_PATTERNS:
                    if pattern in stripped:
                        rel = path.relative_to(_SRC_ROOT)
                        violations.append(f"{rel}:{i}: {stripped}")
                        break
        assert (
            violations == []
        ), "Found admin subsystem classes in source:\n" + "\n".join(violations)

    def test_no_admin_framework_imports(self) -> None:
        """No source file imports admin framework packages."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            lines = _import_lines(source)
            found = _banned_imports(
                lines,
                tuple(
                    p.split()[-1].split(".")[-1].rstrip("'\"")
                    for p in self._ADMIN_IMPORT_PATTERNS
                    if "import" in p
                ),
            )
            for line in found:
                rel = path.relative_to(_SRC_ROOT)
                violations.append(f"{rel}: {line}")
        assert (
            violations == []
        ), "Found admin framework imports in source:\n" + "\n".join(violations)

    def test_no_admin_source_files(self) -> None:
        """No dedicated admin source files exist."""
        admin_files = [
            p
            for p in _all_source_py_files(_SRC_ROOT)
            if "admin" in p.name.lower()
            and "admin" in _file_source(p).lower()
            and p.name != "__init__.py"
            # Exclude adapter data-plane files that use "admin" as a
            # packet type name (e.g. Meshtastic ADMIN_APP portnum).
            and "packet_classifier" not in p.name and "compat" not in str(p)
        ]
        assert admin_files == [], "Found dedicated admin source files:\n" + "\n".join(
            str(f.relative_to(_SRC_ROOT)) for f in admin_files
        )


# ===================================================================
# 4. No webhook handler subsystem
# ===================================================================


class TestNoWebhookSubsystem:
    """Beta must not contain webhook handlers or webhook infrastructure."""

    _WEBHOOK_PATTERNS = (
        "class WebhookHandler",
        "class WebhookServer",
        "class WebhookEndpoint",
        "class WebhookRouter",
        "class WebhookReceiver",
        "webhook_handler",
        "webhook_server",
        "webhook_endpoint",
    )

    def test_no_webhook_source_files(self) -> None:
        """No dedicated webhook source files exist."""
        webhook_files = [
            p
            for p in _all_source_py_files(_SRC_ROOT)
            if "webhook" in p.name.lower() and p.name != "__init__.py"
        ]
        assert webhook_files == [], "Found webhook source files:\n" + "\n".join(
            str(f.relative_to(_SRC_ROOT)) for f in webhook_files
        )

    def test_no_webhook_patterns_in_source(self) -> None:
        """No source file defines webhook handler classes/functions."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            found = _scan_source_for_pattern(
                source,
                str(path.relative_to(_SRC_ROOT)),
                self._WEBHOOK_PATTERNS,
            )
            violations.extend(found)
        assert (
            violations == []
        ), "Found webhook subsystem patterns in source:\n" + "\n".join(violations)


# ===================================================================
# 5. No plugin loader/runtime/manager
# ===================================================================


class TestNoPluginRuntime:
    """Beta must not contain a plugin loader, runtime, or manager.

    ``medre.plugins.__init__`` provides only the protocol boundary
    scaffolding (``Plugin`` protocol, ``PluginCapability`` enum,
    ``validate_plugin_payload``).  No loader, lifecycle manager, or
    registry may exist.
    """

    _PLUGIN_RUNTIME_PATTERNS = (
        "class PluginLoader",
        "class PluginManager",
        "class PluginRegistry",
        "class PluginRuntime",
        "class PluginHost",
        "def load_plugins",
        "def discover_plugins",
        "def register_plugin",
        "PluginLoader",
        "PluginManager",
        "PluginRegistry",
    )

    def test_no_plugin_loader_files(self) -> None:
        """No plugin loader/manager source files exist under plugins/."""
        plugins_dir = _SRC_ROOT / "plugins"
        plugin_files = [
            p
            for p in sorted(plugins_dir.glob("*.py"))
            if p.name != "__init__.py" and p.name != "__pycache__"
        ]
        assert plugin_files == [], "Found non-scaffolding plugin files:\n" + "\n".join(
            f.name for f in plugin_files
        )

    def test_plugins_init_is_scaffolding_only(self) -> None:
        """plugins/__init__.py must only contain protocol scaffolding."""
        source = _source_of("medre.plugins")

        # Must contain the expected scaffolding symbols.
        assert (
            "class Plugin" in source
        ), "plugins/__init__.py must define Plugin protocol"
        assert (
            "class PluginCapability" in source
        ), "plugins/__init__.py must define PluginCapability enum"
        assert (
            "validate_plugin_payload" in source
        ), "plugins/__init__.py must define validate_plugin_payload"

        # Must NOT contain runtime/loader patterns.
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            for pattern in self._PLUGIN_RUNTIME_PATTERNS:
                if pattern in stripped:
                    violations.append(f"plugins/__init__.py:{i}: {stripped}")
                    break

        assert (
            violations == []
        ), "plugins/__init__.py contains plugin runtime patterns:\n" + "\n".join(
            violations
        )

    def test_no_plugin_loader_imports(self) -> None:
        """No source module imports a plugin loader/manager."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            lines = _import_lines(source)
            for line in lines:
                if "plugin_loader" in line or "plugin_manager" in line:
                    rel = path.relative_to(_SRC_ROOT)
                    violations.append(f"{rel}: {line}")
        assert violations == [], "Found plugin loader/manager imports:\n" + "\n".join(
            violations
        )


# ===================================================================
# 6. No distributed dependencies
# ===================================================================


class TestNoDistributedDependencies:
    """Beta must not depend on distributed-infrastructure packages.

    No redis, celery, kafka, rabbitmq, zmq, memcached, cassandra,
    or similar distributed-system packages may appear in imports.
    """

    _BANNED_IMPORT_PREFIXES: tuple[str, ...] = tuple(
        prefix + pkg for prefix in ("import ", "from ") for pkg in _DISTRIBUTED_PACKAGES
    )

    def test_no_distributed_imports_in_source(self) -> None:
        """No source file imports distributed-infrastructure packages."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            lines = _import_lines(source)
            for line in lines:
                for pkg in _DISTRIBUTED_PACKAGES:
                    if re.search(rf"\b{re.escape(pkg)}\b", line):
                        rel = path.relative_to(_SRC_ROOT)
                        violations.append(f"{rel}: {line}")
                        break
        assert violations == [], "Found distributed dependency imports:\n" + "\n".join(
            violations
        )

    def test_no_distributed_deps_in_pyproject(self) -> None:
        """pyproject.toml dependencies must not include distributed packages."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        content = _file_source(pyproject)
        violations: list[str] = []
        for pkg in _DISTRIBUTED_PACKAGES:
            # Match package name as a dependency (not in comments).
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(rf"\b{re.escape(pkg)}\b", stripped):
                    violations.append(f"pyproject.toml:{i}: {stripped}")
                    break
        assert (
            violations == []
        ), "pyproject.toml references distributed packages:\n" + "\n".join(violations)


# ===================================================================
# 7. No persistent queue infrastructure
# ===================================================================


class TestNoPersistentQueues:
    """Beta must not contain persistent queue infrastructure.

    No PersistentQueue, DurableQueue, RedisQueue, KafkaQueue, or
    similar persistent/durable queue abstractions may exist in the
    source code.
    """

    _QUEUE_PATTERNS = (
        "class PersistentQueue",
        "class DurableQueue",
        "class RedisQueue",
        "class KafkaQueue",
        "class RabbitMQQueue",
        "class CeleryQueue",
        "PersistentQueue(",
        "DurableQueue(",
        "persistent_queue",
        "durable_queue",
    )

    def test_no_persistent_queue_classes(self) -> None:
        """No source file defines persistent queue classes."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            found = _scan_source_for_pattern(
                source,
                str(path.relative_to(_SRC_ROOT)),
                self._QUEUE_PATTERNS,
            )
            violations.extend(found)
        assert (
            violations == []
        ), "Found persistent queue patterns in source:\n" + "\n".join(violations)

    def test_no_persistent_queue_imports(self) -> None:
        """No source file imports persistent queue packages."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            lines = _import_lines(source)
            for line in lines:
                for pattern in (
                    "redis_queue",
                    "kafka_queue",
                    "celery_queue",
                    "persistent_queue",
                    "durable_queue",
                    "PersistentQueue",
                    "DurableQueue",
                ):
                    if pattern in line:
                        rel = path.relative_to(_SRC_ROOT)
                        violations.append(f"{rel}: {line}")
                        break
        assert violations == [], "Found persistent queue imports:\n" + "\n".join(
            violations
        )


# ===================================================================
# 8. No replay deduplication engine
# ===================================================================


class TestNoReplayDeduplication:
    """Beta must not contain a replay deduplication engine.

    Replay storage may track ``run_id`` for correlation, but must not
    implement event-level deduplication (hash-based, bloom-filter,
    content-based, or otherwise).

    The replay module's docstrings may mention "deduplicate" in the
    context of ``run_id`` correlation — this is informational only
    and does not constitute a deduplication engine.
    """

    _DEDUP_ENGINE_PATTERNS = (
        "class.*Deduplicat",
        "def.*deduplicat",
        "DeduplicationEngine",
        "DeduplicationService",
        "EventDeduplicator",
        "BloomFilter",
        "dedup_cache",
        "dedup_store",
        "_seen_hashes",
        "_dedup_set",
    )

    def test_no_dedup_engine_in_source(self) -> None:
        """No source file defines a deduplication engine."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            rel = str(path.relative_to(_SRC_ROOT))
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in self._DEDUP_ENGINE_PATTERNS:
                    if re.search(pattern, stripped):
                        violations.append(f"{rel}:{i}: {stripped}")
                        break
        assert (
            violations == []
        ), "Found deduplication engine patterns in source:\n" + "\n".join(violations)

    def test_replay_module_no_dedup_logic(self) -> None:
        """replay.py must not implement deduplication beyond run_id tracking."""
        try:
            source = _source_of("medre.core.storage.replay")
        except (FileNotFoundError, ModuleNotFoundError):
            pytest.skip("medre.core.storage.replay not importable")

        # "deduplicate" may appear in docstrings/comments only.
        violations: list[str] = []
        in_docstring = False
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()

            # Track triple-quote docstring boundaries.
            count = stripped.count('"""')
            if count == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue

            if stripped.startswith("#"):
                continue

            # In code lines, "deduplicate" or "dedup" as a function/class
            # name is a violation.  Just appearing in a string literal
            # inside code is also suspicious but we check for actual
            # function/class definitions or assignments.
            for bad in ("def _dedup", "def dedup", "class Dedup", "Deduplicat"):
                if bad in stripped:
                    violations.append(f"replay.py:{i}: {stripped}")
                    break

        assert violations == [], (
            "replay.py contains deduplication logic beyond run_id tracking:\n"
            + "\n".join(violations)
        )


# ===================================================================
# 9. No dynamic route/config reload
# ===================================================================


class TestNoDynamicReload:
    """Beta must not contain dynamic route or config reload mechanisms.

    Routes and configuration are loaded at startup and remain static
    for the lifetime of the process.  Hot-reload, file-watch, or
    signal-based config/route reload features are post-beta scope.
    """

    _RELOAD_PATTERNS = (
        "hot_reload",
        "hot-reload",
        "config_reload",
        "config-reload",
        "route_reload",
        "route-reload",
        "reload_config",
        "reload_routes",
        "dynamic_reload",
        "watch_config",
        "ConfigReloader",
        "RouteReloader",
        "class.*Reloader",
    )

    # Patterns that should NOT trigger false positives:
    # - "reload" in EventKind docstring (kinds.py) — just a lifecycle label
    # - "reload" in comments/docstrings
    _FALSE_POSITIVE_FILES = {
        "core/events/kinds.py",  # "reload" is a docstring example
    }

    def test_no_reload_classes_or_functions(self) -> None:
        """No source file defines reload classes or functions."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            rel = str(path.relative_to(_SRC_ROOT))
            if rel in self._FALSE_POSITIVE_FILES:
                continue

            source = _file_source(path)
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in self._RELOAD_PATTERNS:
                    if re.search(pattern, stripped):
                        # Skip docstring content.
                        if stripped.startswith('"""') or stripped.startswith("'''"):
                            continue
                        violations.append(f"{rel}:{i}: {stripped}")
                        break
        assert (
            violations == []
        ), "Found dynamic reload patterns in source:\n" + "\n".join(violations)

    def test_no_file_watch_imports(self) -> None:
        """No source file imports file-watch libraries for config reload."""
        watch_packages = ("watchdog", "inotify", "pyinotify", "watchfiles")
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            lines = _import_lines(source)
            for line in lines:
                for pkg in watch_packages:
                    if re.search(rf"\b{re.escape(pkg)}\b", line):
                        rel = path.relative_to(_SRC_ROOT)
                        violations.append(f"{rel}: {line}")
                        break
        assert (
            violations == []
        ), "Found file-watch imports for config reload:\n" + "\n".join(violations)


# ===================================================================
# 10. No package split
# ===================================================================


class TestNoPackageSplit:
    """Beta must be a single ``medre`` package — no namespace packages
    or split distributions.

    There must be exactly one ``pyproject.toml`` at the project root,
    no nested ``pyproject.toml`` or ``setup.py`` under ``src/``,
    and no PEP 420 implicit namespace package declarations.
    """

    def test_single_root_pyproject(self) -> None:
        """Project must have exactly one pyproject.toml at root."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        assert pyproject.exists(), "Root pyproject.toml not found"

    def test_no_nested_build_files(self) -> None:
        """No nested pyproject.toml or setup.py under src/."""
        nested = []
        for pattern in ("**/pyproject.toml", "**/setup.py", "**/setup.cfg"):
            for path in (_TESTS_DIR.parent / "src").rglob(pattern):
                nested.append(path.relative_to(_TESTS_DIR.parent))
        assert nested == [], "Found nested build files (package split):\n" + "\n".join(
            str(f) for f in nested
        )

    def test_no_namespace_package_declaration(self) -> None:
        """No __init__.py declares namespace package status."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            if path.name != "__init__.py":
                continue
            source = _file_source(path)
            for bad in (
                "__path__",
                "declare_namespace",
                "extend_path",
                "namespace_packages",
            ):
                # Skip if it's just a comment/docstring reference.
                for i, line in enumerate(source.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if bad in stripped:
                        rel = path.relative_to(_SRC_ROOT)
                        violations.append(f"{rel}:{i}: {stripped}")
        # The observability/__init__.py has "namespace" in a docstring
        # — filter out that specific false positive.
        real_violations = [
            v
            for v in violations
            if not re.match(r".*\d+:.*#.*", v)
            and "obtain a child logger in the framework namespace" not in v
            and "logging namespace" not in v
        ]
        assert (
            real_violations == []
        ), "Found namespace package declarations:\n" + "\n".join(real_violations)

    def test_single_package_name(self) -> None:
        """pyproject.toml must define a single medre package."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        content = _file_source(pyproject)
        # Verify the package name is "medre".
        assert re.search(
            r'^name\s*=\s*["\']medre["\']', content, re.MULTILINE
        ), "pyproject.toml must declare package name as 'medre'"


# ===================================================================
# 11. No canonical event redesign
# ===================================================================


class TestNoCanonicalEventRedesign:
    """Beta must retain a single stable canonical event model.

    ``CanonicalEvent`` in ``medre.core.events.canonical`` is the sole
    event envelope.  No alternative, unified, generic, V2/V3, or
    competing event models may exist in the source.
    """

    def test_single_canonical_event_definition(self) -> None:
        """CanonicalEvent must be defined only in canonical.py."""
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            rel = str(path.relative_to(_SRC_ROOT))
            if rel == "core/events/canonical.py":
                continue  # This is the canonical definition site.
            source = _file_source(path)
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "class CanonicalEvent" in stripped:
                    violations.append(f"{rel}:{i}: {stripped}")
        assert (
            violations == []
        ), "CanonicalEvent defined outside canonical.py:\n" + "\n".join(violations)

    def test_no_competing_event_models(self) -> None:
        """No alternative event envelope classes (V2, Unified, etc.)."""
        competing_patterns = (
            "class UnifiedEvent",
            "class GenericEvent",
            "class EventV2",
            "class EventV3",
            "class NewEvent",
            "class AlternativeEvent",
            "class BaseEvent",
            "class CoreEvent",
            "class SimpleEvent",
        )
        violations: list[str] = []
        for path in _all_source_py_files(_SRC_ROOT):
            source = _file_source(path)
            rel = str(path.relative_to(_SRC_ROOT))
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in competing_patterns:
                    if pattern in stripped:
                        violations.append(f"{rel}:{i}: {stripped}")
                        break
        assert (
            violations == []
        ), "Found competing event envelope classes:\n" + "\n".join(violations)

    def test_canonical_event_file_exists_and_stable(self) -> None:
        """core/events/canonical.py must exist and define CanonicalEvent."""
        source = _source_of("medre.core.events.canonical")
        assert (
            "class CanonicalEvent" in source
        ), "medre.core.events.canonical must define CanonicalEvent"
        # Must use msgspec.Struct as base (stable design).
        assert (
            "msgspec.Struct" in source
        ), "CanonicalEvent must use msgspec.Struct as base"
        # Must be frozen (immutable).
        assert "frozen=True" in source, "CanonicalEvent must be frozen=True (immutable)"
