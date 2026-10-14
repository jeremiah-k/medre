"""Exercise a third-party provider through actual installed entry-point metadata."""

import os
import subprocess
import sys
from pathlib import Path


def test_arbitrary_distribution_config_and_runtime(tmp_path: Path) -> None:
    """No medre-adapter naming, namespace layout, SDK, or core edit is needed."""
    dist = tmp_path / "independent_sensor-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: independent-sensor\nVersion: 1.0\n"
    )
    (dist / "entry_points.txt").write_text(
        "[medre.adapters]\ncommunity_sensor = sensor_plugin:spec\n"
    )
    (tmp_path / "sensor_plugin.py").write_text("""
from dataclasses import dataclass
from medre.adapter_registry import AdapterSpec, DestinationSpec, SymbolRef
from medre.adapters.fakes.transport import FakeTransportAdapter

@dataclass(frozen=True)
class SensorConfig:
    adapter_id: str
    label: str = "sensor"
    def validate(self):
        return self

def reader(*args, **kwargs):
    return None

def project(*args, **kwargs):
    return {}

def validate_address(destination):
    if not destination.metadata.get("device_id"):
        raise ValueError("device_id is required")

def spec():
    ref = SymbolRef("sensor_plugin", "reader")
    return AdapterSpec(
        destinations=(DestinationSpec("sensor_device", SymbolRef("sensor_plugin", "validate_address")),),
        transport="community_sensor", config=SymbolRef("sensor_plugin", "SensorConfig"),
        runtime_config=None, adapter=SymbolRef("sensor_plugin", "FakeTransportAdapter"),
        fake_adapter=SymbolRef("sensor_plugin", "FakeTransportAdapter"), renderer_factory=None,
        dependency_probe=None, distribution=None, import_names=(), sdk_import_roots=(),
        native_namespace_reader=ref, versioned_namespace_reader=ref, attribution_projector=SymbolRef("sensor_plugin", "project"),
    )
""")
    (tmp_path / "sensor.yaml").write_text(
        "storage:\n  backend: memory\nadapters:\n  community_sensor:\n    node:\n      adapter_kind: fake\n      label: lab sensor\n"
    )
    program = """
import asyncio
import importlib.metadata
from medre.adapter_registry import get_adapter_spec
from medre.config.loader import load_config
from medre.config.model import GenericAdapterRuntimeConfig
from medre.runtime.builder import RuntimeBuilder
async def check():
    assert importlib.metadata.distribution("independent-sensor").version == "1.0"
    from medre.adapters._attribution_dispatch import project_source_fields
    assert project_source_fields({}, platform_hint="community_sensor") == {"source_platform": "community_sensor"}
    assert get_adapter_spec("community_sensor").config.module == "sensor_plugin"
    from medre.config.routes import RouteDestinationConfig
    from medre.config.errors import ConfigValidationError
    destination = RouteDestinationConfig.from_dict("sensor", {"kind": "sensor_device", "metadata": {"device_id": "s1"}}, field_name="dest_destination", section_path="routes.sensor")
    assert destination.metadata == {"device_id": "s1"}
    try:
        RouteDestinationConfig.from_dict("sensor", {"kind": "sensor_device"}, field_name="dest_destination", section_path="routes.sensor")
    except ConfigValidationError as error:
        assert "device_id is required" in str(error)
    else:
        raise AssertionError("provider address validation was skipped")
    config, _, paths = load_config("sensor.yaml")
    wrapper = config.adapters.for_transport("community_sensor")["node"]
    assert type(wrapper) is GenericAdapterRuntimeConfig
    assert wrapper.config.label == "lab sensor"
    app = RuntimeBuilder(config, paths).build()
    assert "node" in app.adapters
    try:
        await app.start()
        assert "node" in app.started_adapter_ids
    finally:
        await app.stop()
asyncio.run(check())
"""
    source = Path(__file__).resolve().parents[1] / "src"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("MEDRE_")
    }
    env.update(
        PYTHONPATH=os.pathsep.join((str(source), str(tmp_path))),
        MEDRE_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_source_ids_do_not_shadow_longer_transport_names(monkeypatch):
    from medre import adapter_registry
    from medre.adapter_registry import AdapterTypeRegistry
    from medre.adapters import _attribution_dispatch as dispatch
    from tests.helpers.adapter_spec import make_adapter_spec

    specs = tuple(
        make_adapter_spec(name)
        for name in ("mesh", "meshcore", "meshtastic", "private_chat")
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(specs)
    )
    monkeypatch.setattr(
        dispatch,
        "versioned_native_namespace",
        lambda data, transport: data.get(transport),
    )
    for source, expected in (
        ("meshcore-node", "meshcore"),
        ("meshtastic-radio", "meshtastic"),
        ("fake_private_chat", "private_chat"),
        ("mesh", "mesh"),
        ("meshwork", None),
    ):
        assert dispatch.detect_source_platform(source, {}) == expected
    assert (
        dispatch.detect_source_platform(
            "meshcore-node", {"private_chat": {"version": 1}}
        )
        == "private_chat"
    )
    assert (
        dispatch.detect_source_platform(
            "meshcore-node", {}, platform_hint="private_chat"
        )
        == "private_chat"
    )


def test_broken_entry_point_does_not_brick_help_or_preflight(tmp_path: Path):
    dist = tmp_path / "broken_provider-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: broken-provider\nVersion: 1.0\n"
    )
    (dist / "entry_points.txt").write_text(
        "[medre.adapters]\nbroken_chat = unavailable_plugin:spec\n"
    )
    config = tmp_path / "broken.yaml"
    config.write_text("adapters:\n  broken_chat:\n    node: {}\n")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(tmp_path), str(Path(__file__).resolve().parents[1] / "src"))
        ),
    }
    for args, expected in (
        (["--help"], 0),
        (["adapters", "check", "--config", str(config)], 2),
        (["config", "check", "--config", str(config)], 2),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "medre", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == expected, result.stderr
        assert "Traceback" not in result.stderr
        if args[0] == "adapters":
            assert "broken_chat: broken" in result.stdout
            assert "broken-provider" in result.stdout
        elif args[0] == "config":
            assert "Broken adapter provider" in result.stderr
