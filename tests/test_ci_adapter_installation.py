"""SDK qualification preserves the selected core checkout, including prerelease."""

import importlib
import json
from pathlib import Path


def test_sdk_install_never_resolves_core_from_an_index(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/ci"))
    installer = importlib.import_module("install_checkout_adapter")
    checkout = tmp_path / "provider"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("""
[project]
name = "private-bridge"
dependencies = ["medre>=99.0.0", "private-sdk==99.111.222"]
[project.entry-points."medre.adapters"]
private_chat = "private_bridge:spec"
[project.optional-dependencies]
secure = ["private-crypto==99.222.333"]
test = ["test-tool==99.333.444"]
""")
    manifest = tmp_path / "compatibility.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "transport": "private_chat",
                    "repository": "someone/private-bridge",
                    "distribution": "private-bridge",
                    "ref": "a" * 40,
                    "extras": "secure",
                    "local_integration": False,
                }
            ]
        )
    )
    calls = []
    monkeypatch.setattr(
        installer.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    monkeypatch.setattr(
        installer.sys,
        "argv",
        [
            "install",
            "--adapter",
            str(checkout),
            "--manifest",
            str(manifest),
            "--python",
            "selected-python",
            "--test",
        ],
    )
    installer.main()
    assert len(calls) == 2
    assert calls[0] == [
        "uv",
        "pip",
        "install",
        "--python",
        "selected-python",
        "private-sdk==99.111.222",
        "private-crypto==99.222.333",
        "test-tool==99.333.444",
    ]
    assert calls[1] == [
        "uv",
        "pip",
        "install",
        "--python",
        "selected-python",
        "--no-deps",
        "-e",
        str(checkout.resolve()) + "[secure,test]",
    ]
