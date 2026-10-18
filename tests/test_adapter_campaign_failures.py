"""Campaign startup failures retain inspectable private reports."""

import json
from importlib.metadata import PackageNotFoundError

import pytest

from scripts.adapter_campaign import run as campaign
from scripts.adapter_campaign.plan import Plan


async def test_package_metadata_failure_writes_a_failed_report(tmp_path, monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(campaign, "version", missing)
    plan = Plan(tmp_path / "runtime.yaml", {}, 1, 0, 0.1)
    destination = tmp_path / "evidence"
    with pytest.raises(PackageNotFoundError):
        await campaign.run(plan, destination)
    report = json.loads((destination / "results.json").read_text())
    assert report["status"] == "failed"
    assert report["error_type"] == "PackageNotFoundError"
    assert report["cleanup_errors"] == []
    assert (destination / "failure.txt").is_file()
