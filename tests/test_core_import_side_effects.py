"""Core import probes remain executable without any installed adapters."""

import pytest

from tests.helpers.import_side_effects import check_in_subprocess


@pytest.mark.parametrize(
    "module",
    [
        "medre.interop.mmrelay",
        "medre.core.observability.sanitization",
        "medre.core.events.text",
        "medre",
        "medre.adapters",
        "medre.adapters.metadata",
        "medre.config",
        "medre.config.adapters",
        "medre.runtime",
        "medre.runtime.evidence",
        "medre.runtime.run_session",
    ],
)
def test_core_imports_have_no_runtime_or_logging_side_effects(module):
    result = check_in_subprocess(module)
    assert result["forbidden_loaded"] == []
    assert result["root_level_changed"] is False
    assert result["new_handlers"] == 0
