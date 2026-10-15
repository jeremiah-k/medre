"""Public adapter conformance tools work without first-party distributions."""

import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import SymbolRef
from medre.testing import sdk_contract
from medre.testing.fakes import assert_fake_contract
from tests.helpers.adapter_spec import make_adapter_spec


def test_registration_types_do_not_trigger_discovery():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
from importlib import metadata
from unittest.mock import patch
with patch.object(metadata, "entry_points", side_effect=AssertionError("eager discovery")):
    from medre.adapter_spec import AdapterSpec, SymbolRef, DestinationSpec, AdapterSample
    assert AdapterSpec and SymbolRef and DestinationSpec and AdapterSample
""",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("priority", [True, 1.5, "1", None, -1])
def test_detection_priority_is_a_nonnegative_integer(priority):
    with pytest.raises(ValueError, match="native_detection_priority"):
        AdapterTypeRegistry(
            (
                replace(
                    make_adapter_spec("private_chat"),
                    native_detection_priority=priority,
                ),
            )
        )


@pytest.mark.parametrize("kind", ["transport", "presentation"])
def test_core_fake_contract(kind):
    spec = replace(
        make_adapter_spec("private_chat"),
        fake_adapter=SymbolRef(
            "medre.adapters.fakes." + kind,
            (
                "FakeTransportAdapter"
                if kind == "transport"
                else "FakePresentationAdapter"
            ),
        ),
    )
    assert_fake_contract(spec)


def test_sdk_pins_use_arbitrary_distribution_and_selected_extras(monkeypatch):
    requested = []

    def distribution(name):
        requested.append(name)
        return SimpleNamespace(
            requires=[
                "medre>=0.1.0",
                "privacy-sdk==99.111.222",
                'encryption-sdk==99.222.333; extra == "secure"',
                'test-only==99.333.444; extra == "test"',
            ]
        )

    monkeypatch.setattr(sdk_contract.metadata, "distribution", distribution)
    pins = sdk_contract.declared_sdk_pins(
        "arbitrary-provider-package", extras=("secure",)
    )
    assert requested == ["arbitrary-provider-package"]
    assert set(pins) == {"privacy-sdk", "encryption-sdk"}


def test_sdk_check_reports_conflicting_provider_pins(monkeypatch):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            requires=["privacy-sdk==99.111.222", "privacy-sdk==99.222.333"]
        ),
    )
    with pytest.raises(AssertionError, match="conflicting SDK requirement"):
        sdk_contract.declared_sdk_pins("arbitrary-provider-package")


def test_equivalent_requirement_names_and_versions_do_not_conflict(monkeypatch):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            requires=[
                "Privacy_SDK==99.111.0",
                'privacy-sdk[secure]==99.111; extra == "secure"',
            ]
        ),
    )
    assert set(
        sdk_contract.declared_sdk_pins("arbitrary-provider-package", extras=("secure",))
    ) == {"privacy-sdk"}
