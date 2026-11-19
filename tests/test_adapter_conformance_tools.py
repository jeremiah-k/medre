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


@pytest.mark.parametrize(
    "requirements",
    [
        ["privacy-sdk>=99.111.0,<99.222.0", "privacy-sdk>=99.111.0,<99.333.0"],
        ["privacy-sdk==99.111.0", "privacy-sdk>=99.111.0,<99.222.0"],
    ],
)
def test_active_sdk_constraints_with_the_same_floor_can_conflict(
    monkeypatch, requirements
):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(requires=requirements),
    )
    with pytest.raises(AssertionError, match="conflicting SDK requirement"):
        sdk_contract.declared_sdk_pins("arbitrary-provider-package")


def test_equivalent_base_and_extra_sdk_ranges_are_accepted(monkeypatch):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            requires=[
                "Privacy_SDK>=99.111,<99.222.0",
                'privacy-sdk[secure]<99.222,>=99.111.0; extra == "secure"',
            ]
        ),
    )
    assert set(
        sdk_contract.declared_sdk_pins("arbitrary-provider-package", extras=("secure",))
    ) == {"privacy-sdk"}


def test_sdk_ranges_with_explicit_bounds_are_accepted(monkeypatch):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            requires=["privacy-sdk>=99.100.0,>=99.111.0,<99.222.0"],
        ),
    )
    pins = sdk_contract.declared_sdk_pins("arbitrary-provider-package")
    assert set(pins) == {"privacy-sdk"}
    assert pins["privacy-sdk"].version == "99.111.0"
    assert pins["privacy-sdk"].specifier == "<99.222.0,>=99.100.0,>=99.111.0"


@pytest.mark.parametrize(
    "requirement",
    [
        "privacy-sdk>=99.111.0",
        "privacy-sdk<99.222.0",
        "privacy-sdk==99.*",
        "privacy-sdk!=99.111.0",
        "privacy-sdk",
        "privacy-sdk @ https://example.invalid/privacy.whl",
    ],
)
def test_sdk_requirements_without_bounded_shape_stay_rejected(monkeypatch, requirement):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(requires=[requirement]),
    )
    with pytest.raises(
        AssertionError, match="neither an exact pin nor a bounded range"
    ):
        sdk_contract.declared_sdk_pins("arbitrary-provider-package")


@pytest.mark.parametrize(
    ("installed", "accepted"),
    [
        ("99.110.0", False),
        ("99.111.0", True),
        ("99.111.5", True),
        ("99.112.0rc1", False),
        ("99.222.0", False),
    ],
)
def test_installed_sdks_are_checked_against_declared_ranges(
    monkeypatch, installed, accepted
):
    monkeypatch.setattr(
        sdk_contract.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            requires=["privacy-sdk>=99.111.0,<99.222.0"],
        ),
    )
    monkeypatch.setattr(sdk_contract.metadata, "version", lambda name: installed)
    if accepted:
        sdk_contract.assert_installed_sdk_pins(
            "arbitrary-provider-package", ["privacy-sdk"]
        )
    else:
        with pytest.raises(AssertionError, match="outside .* range"):
            sdk_contract.assert_installed_sdk_pins(
                "arbitrary-provider-package", ["privacy-sdk"]
            )
