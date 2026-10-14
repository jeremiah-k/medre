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
        native_namespace_reader=ref, versioned_namespace_reader=ref, attribution_projector=ref,
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
