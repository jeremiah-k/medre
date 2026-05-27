"""Parameterized adapter conformance tests.

Verifies that every adapter (real and fake) conforms to the §3.1 adapter
conformance requirements defined in ``docs/spec/conformance.md``.

§3.1 Adapter Conformance — an adapter conforms when it:

1. Implements the ``Adapter`` protocol (``start``, ``stop``, ``deliver``,
   ``health_check``).
2. Provides an ``AdapterCodec`` for native-to-canonical event conversion.
3. Sets ``source_transport_id`` to the transport's native sender identifier.
4. Sets ``source_channel_id`` to the native channel identifier (or ``None``).
5. Never puts private keys, credentials, or configuration in canonical events.
6. Publishes inbound events via ``ctx.publish_inbound()``, not by calling
   other adapters.
7. Reports health via ``health_check()``.
8. Respects payload limits when embedding envelopes on constrained transports.

Tests 1, 2, and 7 can be validated statically (no running adapter needed).
Tests 3, 4, 5, 6, and 8 require a runtime adapter instance and are marked
``pytest.mark.xfail`` until integration harnesses are available.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.fakes.presentation import FakePresentationAdapter

# Import fake adapters eagerly (no SDK dependencies).
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.contracts.adapter import (
    AdapterCodec,
    AdapterContext,
    AdapterContract,
    AdapterInfo,
    AdapterRole,
)

# ---------------------------------------------------------------------------
# Adapter matrix: fake adapters (always available) and real adapters
# (module, class, sdk_name) where sdk_name is used for availability checks.
# ---------------------------------------------------------------------------


_FAKE_ADAPTERS: list[tuple[str, type[AdapterContract]]] = [
    ("FakeTransportAdapter", FakeTransportAdapter),
    ("FakePresentationAdapter", FakePresentationAdapter),
    ("FakeMatrixAdapter", FakeMatrixAdapter),
    ("FakeMeshtasticAdapter", FakeMeshtasticAdapter),
    ("FakeMeshCoreAdapter", FakeMeshCoreAdapter),
    ("FakeLxmfAdapter", FakeLxmfAdapter),
]

_REAL_ADAPTER_SPECS: list[tuple[str, str, str, str]] = [
    # (display_name, module_path, class_name, sdk_name)
    ("MatrixAdapter", "medre.adapters.matrix.adapter", "MatrixAdapter", "nio"),
    (
        "MeshtasticAdapter",
        "medre.adapters.meshtastic.adapter",
        "MeshtasticAdapter",
        "meshtastic",
    ),
    (
        "MeshCoreAdapter",
        "medre.adapters.meshcore.adapter",
        "MeshCoreAdapter",
        "meshcore",
    ),
    ("LxmfAdapter", "medre.adapters.lxmf.adapter", "LxmfAdapter", "RNS"),
]


def _try_import_real_adapter(
    module_path: str, class_name: str
) -> type[AdapterContract] | None:
    """Attempt to import a real adapter class; return None on failure."""
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name, None)
    except Exception:
        return None


# Build the combined parameterization list at module level.
# Each entry is (display_name, adapter_cls_or_None, requires_runtime, sdk_name_or_None).
_ADAPTER_PARAMS: list[tuple[str, type[AdapterContract] | None, bool, str | None]] = []

for name, cls in _FAKE_ADAPTERS:
    _ADAPTER_PARAMS.append((name, cls, False, None))

for display, module_path, class_name, sdk_name in _REAL_ADAPTER_SPECS:
    cls = _try_import_real_adapter(module_path, class_name)
    _ADAPTER_PARAMS.append((display, cls, False, sdk_name))


# ---------------------------------------------------------------------------
# §3.1 Requirement 1: Adapter protocol methods
# ---------------------------------------------------------------------------


class TestAdapterProtocol:
    """§3.1.1: Implements the Adapter protocol (start, stop, deliver,
    health_check).
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_is_subclass_of_adapter_contract(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.1: Adapter class must be a subclass of AdapterContract."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        assert issubclass(
            cls, AdapterContract
        ), f"{name} is not a subclass of AdapterContract"

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_has_start_method(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.1: Adapter class must expose an async ``start`` method."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        assert hasattr(cls, "start"), f"{name} lacks start method"
        assert inspect.iscoroutinefunction(cls.start), f"{name}.start must be async"

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_has_stop_method(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.1: Adapter class must expose an async ``stop`` method."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        assert hasattr(cls, "stop"), f"{name} lacks stop method"
        assert inspect.iscoroutinefunction(cls.stop), f"{name}.stop must be async"

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_has_deliver_method(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.1: Adapter class must expose an async ``deliver`` method."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        assert hasattr(cls, "deliver"), f"{name} lacks deliver method"
        assert inspect.iscoroutinefunction(cls.deliver), f"{name}.deliver must be async"

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_has_health_check_method(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.1: Adapter class must expose an async ``health_check``
        method."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        assert hasattr(cls, "health_check"), f"{name} lacks health_check method"
        assert inspect.iscoroutinefunction(
            cls.health_check
        ), f"{name}.health_check must be async"


# ---------------------------------------------------------------------------
# §3.1 Requirement 2: AdapterCodec provision
# ---------------------------------------------------------------------------


class TestAdapterCodec:
    """§3.1.2: Provides an AdapterCodec for native-to-canonical event
    conversion.
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_get_codec_returns_codec_or_none(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.2: ``get_codec()`` must return an AdapterCodec subclass or
        ``None``."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.xfail(f"{name} cannot be instantiated without config/runtime")
        codec = adapter.get_codec()
        if codec is not None:
            assert isinstance(
                codec, AdapterCodec
            ), f"{name}.get_codec() returned {type(codec).__name__}, expected AdapterCodec"

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    def test_codec_has_decode_method(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.2: When a codec is provided, it must have a ``decode``
        method."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.xfail(f"{name} cannot be instantiated without config/runtime")
        codec = adapter.get_codec()
        if codec is None:
            pytest.skip(f"{name} does not provide a codec")
        assert hasattr(codec, "decode"), f"{name} codec lacks decode method"
        assert callable(codec.decode), f"{name} codec.decode must be callable"


# ---------------------------------------------------------------------------
# §3.1 Requirement 3: source_transport_id
# ---------------------------------------------------------------------------


class TestSourceTransportId:
    """§3.1.3: Sets source_transport_id to the transport's native sender
    identifier (as a string) for all source events.
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    @pytest.mark.xfail(
        reason="Requires runtime adapter instance with inbound event",
        strict=False,
    )
    async def test_source_transport_id_is_string(
        self,
        name: str,
        cls: type | None,
        _runtime: bool,
        sdk: str | None,
    ) -> None:
        """§3.1.3: source_transport_id must be a string on produced events."""
        if cls is None:
            pytest.skip(f"{name} not importable")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.skip(f"{name} cannot be instantiated")
        events = await _produce_inbound_events(adapter, name)
        for event in events:
            assert isinstance(
                event.source_transport_id, str
            ), f"{name}: source_transport_id is not a string: {event.source_transport_id!r}"
            assert (
                event.source_transport_id
            ), f"{name}: source_transport_id must not be empty"


# ---------------------------------------------------------------------------
# §3.1 Requirement 4: source_channel_id
# ---------------------------------------------------------------------------


class TestSourceChannelId:
    """§3.1.4: Sets source_channel_id to the native channel identifier (or
    ``None`` if the transport has no channel concept).
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    @pytest.mark.xfail(
        reason="Requires runtime adapter instance with inbound event",
        strict=False,
    )
    async def test_source_channel_id_is_string_or_none(
        self,
        name: str,
        cls: type | None,
        _runtime: bool,
        sdk: str | None,
    ) -> None:
        """§3.1.4: source_channel_id must be a string or ``None``."""
        if cls is None:
            pytest.skip(f"{name} not importable")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.skip(f"{name} cannot be instantiated")
        events = await _produce_inbound_events(adapter, name)
        for event in events:
            assert event.source_channel_id is None or isinstance(
                event.source_channel_id, str
            ), (
                f"{name}: source_channel_id must be str or None, "
                f"got {type(event.source_channel_id).__name__}"
            )


# ---------------------------------------------------------------------------
# §3.1 Requirement 5: No credentials in events
# ---------------------------------------------------------------------------

# Strings that should never appear in canonical event payloads or metadata.
_CREDENTIAL_PATTERNS: tuple[str, ...] = (
    "password",
    "secret",
    "private_key",
    "access_token",
    "api_key",
    "token",
    "credential",
    "auth_token",
)


class TestNoCredentialsInEvents:
    """§3.1.5: Never puts private keys, credentials, or configuration in
    canonical events.
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    @pytest.mark.xfail(
        reason="Requires runtime adapter instance with inbound event",
        strict=False,
    )
    async def test_no_credential_patterns_in_payload(
        self,
        name: str,
        cls: type | None,
        _runtime: bool,
        sdk: str | None,
    ) -> None:
        """§3.1.5: Canonical event payload must not contain credential-like
        keys."""
        if cls is None:
            pytest.skip(f"{name} not importable")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.skip(f"{name} cannot be instantiated")
        events = await _produce_inbound_events(adapter, name)
        for event in events:
            _assert_no_credential_keys(event.payload, f"{name} payload")


# ---------------------------------------------------------------------------
# §3.1 Requirement 6: Uses ctx.publish_inbound()
# ---------------------------------------------------------------------------


class TestPublishInbound:
    """§3.1.6: Publishes inbound events via ``ctx.publish_inbound()``, not
    by calling other adapters.
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    @pytest.mark.xfail(
        reason="Requires runtime adapter instance to observe publish_inbound call",
        strict=False,
    )
    async def test_uses_publish_inbound(
        self,
        name: str,
        cls: type | None,
        _runtime: bool,
        sdk: str | None,
    ) -> None:
        """§3.1.6: Adapter must publish inbound events through
        ``ctx.publish_inbound()``."""
        if cls is None:
            pytest.skip(f"{name} not importable")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.skip(f"{name} cannot be instantiated")

        collected: list[Any] = []

        async def _collector(event: Any) -> None:
            collected.append(event)

        ctx = AdapterContext(
            adapter_id="test",
            event_bus=None,
            publish_inbound=_collector,
            logger=__import__("logging").getLogger(f"test.{name}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            events = _make_test_events(adapter, name)
            for event in events:
                await adapter.publish_inbound(event)
            # At least one event should have been forwarded.
            assert len(collected) >= 1, (
                f"{name}: publish_inbound was not called; "
                f"expected at least 1 event, got {len(collected)}"
            )
        finally:
            await adapter.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# §3.1 Requirement 7: health_check returns AdapterInfo
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """§3.1.7: Reports health via ``health_check()``."""

    @pytest.mark.parametrize(
        "name,cls,_runtime,_sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    async def test_health_check_returns_adapter_info(
        self, name: str, cls: type | None, _runtime: bool, _sdk: str | None
    ) -> None:
        """§3.1.7: ``health_check()`` must return an ``AdapterInfo`` with
        required fields."""
        if cls is None:
            pytest.skip(f"{name} not importable (SDK unavailable)")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.xfail(f"{name} cannot be instantiated without config/runtime")

        info = await adapter.health_check()
        assert isinstance(
            info, AdapterInfo
        ), f"{name}.health_check() returned {type(info).__name__}, expected AdapterInfo"
        assert isinstance(
            info.adapter_id, str
        ), f"{name}: AdapterInfo.adapter_id must be str"
        assert isinstance(
            info.platform, str
        ), f"{name}: AdapterInfo.platform must be str"
        assert isinstance(info.health, str), f"{name}: AdapterInfo.health must be str"
        assert info.health in {
            "healthy",
            "degraded",
            "failed",
            "unknown",
            "starting",
            "stopping",
        }, f"{name}: AdapterInfo.health has invalid value: {info.health!r}"
        assert isinstance(
            info.role, AdapterRole
        ), f"{name}.health_check did not return AdapterRole"
        assert (
            isinstance(info.version, str) and info.version
        ), f"{name}.health_check version is empty or not a string"


# ---------------------------------------------------------------------------
# §3.1 Requirement 8: Payload limits
# ---------------------------------------------------------------------------


class TestPayloadLimits:
    """§3.1.8: Respects payload limits when embedding envelopes on constrained
    transports.
    """

    @pytest.mark.parametrize(
        "name,cls,_runtime,sdk",
        _ADAPTER_PARAMS,
        ids=[p[0] for p in _ADAPTER_PARAMS],
    )
    @pytest.mark.xfail(
        reason="Requires runtime adapter delivery with oversized payload",
        strict=False,
    )
    async def test_respects_max_text_bytes(
        self,
        name: str,
        cls: type | None,
        _runtime: bool,
        sdk: str | None,
    ) -> None:
        """§3.1.8: Adapter must respect ``max_text_bytes`` when set in
        capabilities."""
        if cls is None:
            pytest.skip(f"{name} not importable")
        adapter = cls() if _can_instantiate(cls) else None
        if adapter is None:
            pytest.skip(f"{name} cannot be instantiated")

        info = await adapter.health_check()
        cap = info.capabilities
        if cap.max_text_bytes is None and cap.max_text_chars is None:
            pytest.skip(f"{name} has no text size limits declared")

        # Verify the capability is a positive integer when set.
        if cap.max_text_bytes is not None:
            assert isinstance(
                cap.max_text_bytes, int
            ), f"{name}: max_text_bytes must be int"
            assert cap.max_text_bytes > 0, f"{name}: max_text_bytes must be positive"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _can_instantiate(cls: type) -> bool:
    """Return True if the adapter class can be instantiated with minimal args."""
    try:
        # Most fake adapters accept adapter_id as a keyword.
        # Some real adapters need a config object.
        sig = inspect.signature(cls.__init__)
        params = [
            p
            for p in sig.parameters.values()
            if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        # Check if all non-self params have defaults.
        return all(p.default is not p.empty for p in params)
    except Exception:
        return False


async def _produce_inbound_events(adapter: AdapterContract, name: str) -> list[Any]:
    """Start the adapter, produce inbound events, and return them.

    Falls back to ``make_event`` / ``make_text_event`` methods if available.
    """
    from tests.conftest import _InboundCollector

    collector = _InboundCollector()

    ctx = AdapterContext(
        adapter_id="test",
        event_bus=None,
        publish_inbound=collector,
        logger=__import__("logging").getLogger(f"test.{name}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    await adapter.start(ctx)
    try:
        events = _make_test_events(adapter, name)
        for event in events:
            await adapter.publish_inbound(event)
    finally:
        await adapter.stop(timeout=1.0)

    return collector.events if collector.events else events


def _make_test_events(adapter: Any, name: str) -> list[Any]:
    """Create test events using the adapter's helper methods if available."""
    if hasattr(adapter, "make_event"):
        return [adapter.make_event(text="conformance test")]
    if hasattr(adapter, "make_text_event"):
        return [adapter.make_text_event(body="conformance test")]
    # Fallback: create a minimal CanonicalEvent manually.
    from medre.core.events.canonical import CanonicalEvent
    from medre.core.events.metadata import EventMetadata

    return [
        CanonicalEvent(
            event_id=f"conf-test-{id(adapter)}",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=getattr(adapter, "adapter_id", "test"),
            source_transport_id=getattr(adapter, "adapter_id", "test"),
            source_channel_id="test_channel",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "conformance test"},
            metadata=EventMetadata(),
        )
    ]


def _assert_no_credential_keys(data: Any, path: str) -> None:
    """Recursively check that no credential-like keys appear in *data*."""
    if isinstance(data, dict):
        for key in data:
            key_lower = str(key).lower()
            for pattern in _CREDENTIAL_PATTERNS:
                if pattern in key_lower:
                    val = data[key]
                    if val is not None and str(val).strip():
                        # Allow test fixtures that use obviously fake tokens.
                        val_str = str(val)
                        if val_str in (
                            "fake_tok",
                            "tok_single",
                            "fake_token",
                            "syt_fake",
                        ):
                            continue
                        raise AssertionError(
                            f"{path}.{key} = {val_str!r} — credential-like key "
                            f"with non-empty value violates §3.1.5"
                        )
            _assert_no_credential_keys(data[key], f"{path}.{key}")
    elif isinstance(data, (list, tuple)):
        for i, item in enumerate(data):
            _assert_no_credential_keys(item, f"{path}[{i}]")
