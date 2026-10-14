"""SDK qualification follows manifest identity and extras, never package naming."""

import importlib.util
import json
from pathlib import Path

import pytest


def _script(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts/ci"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "checkout_adapter", scripts / "install_checkout_adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checkout_identity_and_sdk_extra_are_manifest_driven(tmp_path, monkeypatch):
    checkout = tmp_path / "unconventional-repository"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "Private.Chat"\n[project.entry-points."medre.adapters"]\nprivate = "chat:spec"\n[project.optional-dependencies]\ntest = []\nsecure = []\n'
    )
    manifest = tmp_path / "compatibility.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "transport": "private",
                    "distribution": "private-chat",
                    "repository": "author/unconventional-repository",
                    "ref": "a" * 40,
                    "extras": "secure",
                    "local_integration": False,
                }
            ]
        )
    )
    module = _script(monkeypatch)
    assert (
        module.checkout_requirement(checkout, manifest, test=True)
        == str(checkout.resolve()) + "[secure,test]"
    )
    assert module.checkout_requirement(checkout, manifest, test=False).endswith(
        "[secure]"
    )
    (checkout / "pyproject.toml").write_text(
        (checkout / "pyproject.toml").read_text().replace("secure = []", "other = []")
    )
    with pytest.raises(ValueError, match="not declared"):
        module.checkout_requirement(checkout, manifest, test=False)
