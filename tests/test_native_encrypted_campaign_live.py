# medre-test-tier: compatibility
"""Opt-in requalification of private, retained native encrypted evidence."""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


async def test_native_encrypted_campaign():
    """Require a completed encrypted Docker run, without contacting a homeserver."""
    directory = os.environ.get("MEDRE_ENCRYPTED_CAMPAIGN")
    if not directory:
        pytest.skip("Set MEDRE_ENCRYPTED_CAMPAIGN to a private completed campaign")
    from scripts.analyze_encrypted_campaign import analyze

    result = await analyze(Path(directory))
    assert result["status"] == "passed" and result["cases"] == 6
