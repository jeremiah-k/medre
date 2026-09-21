"""Registry-driven source-attribution dispatch contracts.

The dispatch module must only project through *registered* adapters: a
platform that resolves by heuristic but has no registry entry yields its
``source_platform`` field and nothing more, never an import of an
unregistered projector.
"""

from __future__ import annotations

from typing import Any

import medre.adapters._attribution_dispatch as dispatch_mod
from medre.adapters._attribution_dispatch import project_source_fields


def test_unregistered_platform_short_circuits_without_projection(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(dispatch_mod, "get_adapter_spec", lambda transport: None)
    fields = project_source_fields({}, source_adapter="matrix-main")
    assert fields == {"source_platform": "matrix"}


def test_registered_platform_projects_through_registry() -> None:
    fields = project_source_fields({}, source_adapter="matrix-main")
    assert fields["source_platform"] == "matrix"


def test_unknown_platform_reports_none() -> None:
    fields = project_source_fields({}, source_adapter="mystery-box")
    assert fields == {"source_platform": None}
