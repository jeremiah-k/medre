"""Shared fixtures and helpers for docker_bridge_artifacts split test files."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

_FIXED_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    """Provide a temporary base directory for artifact runs."""
    return tmp_path / "bridge-runs"
