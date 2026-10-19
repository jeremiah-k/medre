"""Exercise the real uv resolver with disposable local Git distributions only."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import distributions

import pytest

from medre import self_upgrade as upgrade
from medre.tool_management import ToolInstallation, ToolManagementError

pytestmark = pytest.mark.timeout(120)

BACKEND = """import pathlib, tomllib, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]
    name, version = project["name"].replace("-", "_"), project["version"]
    info = name + "-" + version + ".dist-info"
    files = {
        name + ".py": "def main(): pass\\n",
        info + "/METADATA": "Metadata-Version: 2.3\\nName: " + project["name"] + "\\nVersion: " + version + "\\n",
        info + "/WHEEL": "Wheel-Version: 1.0\\nGenerator: medre-uv-contract\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
        info + "/entry_points.txt": "[console_scripts]\\nmedre = medre:main\\n" if name == "medre" else "[medre.adapters]\\n" + name + " = " + name + ":main\\n",
    }
    files[info + "/RECORD"] = "".join(path + ",,\\n" for path in files) + info + "/RECORD,,\\n"
    wheel = name + "-" + version + "-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / wheel, "w") as archive:
        for path, content in files.items(): archive.writestr(path, content)
    return wheel
"""


def run(command, *, env=None, cwd=None):
    result = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def repository(root, name, version):
    path = root / name
    path.mkdir(exist_ok=True)
    if not (path / ".git").exists():
        run(["git", "init", "-b", "main"], cwd=path)
        run(["git", "config", "user.name", "MEDRE fixture"], cwd=path)
        run(["git", "config", "user.email", "fixture@example.invalid"], cwd=path)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
    )
    (path / "backend.py").write_text(BACKEND)
    run(["git", "add", "."], cwd=path)
    run(
        [
            "git",
            "-c",
            "core.hooksPath=",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            version,
        ],
        cwd=path,
    )
    return f"{name} @ git+{path.as_uri()}@main"


@pytest.mark.parametrize("scope", ["all", "core", "adapters", "floating"])
def test_real_uv_updates_the_selected_git_packages_without_persisting_pins(
    tmp_path, monkeypatch, scope
):
    uv = shutil.which("uv")
    if uv is None or shutil.which("git") is None:
        pytest.skip("The isolated uv contract requires uv and git")
    root = tmp_path / "packages"
    root.mkdir()
    core = repository(root, "medre", "1.0")
    provider = repository(root, "example-provider", "1.0")
    if scope == "core":
        commit = run(
            ["git", "rev-parse", "HEAD"], cwd=root / "example-provider"
        ).strip()
        provider = provider.rsplit("@", 1)[0] + "@" + commit
    elif scope == "adapters":
        commit = run(["git", "rev-parse", "HEAD"], cwd=root / "medre").strip()
        core = core.rsplit("@", 1)[0] + "@" + commit
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_CACHE_DIR": str(tmp_path / "cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
    }
    for key in [
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_CONSTRAINT",
        "UV_OVERRIDE",
        "UV_FIND_LINKS",
        "UV_INDEX",
        "UV_DEFAULT_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
    ]:
        env.pop(key, None)
    run(
        [uv, "tool", "install", "--python", sys.executable, "--with", provider, core],
        env=env,
    )
    prefix = tmp_path / "tools/medre"
    python = prefix / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    locations = json.loads(
        run(
            [
                str(python),
                "-c",
                "import json,sysconfig; print(json.dumps([sysconfig.get_path('purelib'),sysconfig.get_path('platlib')]))",
            ],
            env=env,
        )
    )
    entries = [
        entry
        for distribution in distributions(path=locations)
        for entry in distribution.entry_points
        if entry.group == "medre.adapters"
    ]
    monkeypatch.setattr(
        upgrade.importlib.metadata, "entry_points", lambda **kw: entries
    )
    receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
    installation = ToolInstallation(uv, prefix, receipt)
    repository(root, "medre", "2.0")
    repository(root, "example-provider", "2.0")
    if scope == "floating":
        with pytest.raises(
            ToolManagementError, match="cannot preserve the mutable source"
        ):
            upgrade.plan_upgrade(installation, core_only=True)
        assert (
            tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"] == receipt
        )
        installed = json.loads(
            run(
                [
                    str(python),
                    "-c",
                    "import json; from importlib.metadata import version; print(json.dumps([version('medre'),version('example-provider')]))",
                ],
                env=env,
            )
        )
        assert installed == ["1.0", "1.0"]
        return
    plan = upgrade.plan_upgrade(
        installation, core_only=scope == "core", adapters_only=scope == "adapters"
    )
    run(list(plan.command), env=env)
    versions = json.loads(
        run(
            [
                str(python),
                "-c",
                "import json; from importlib.metadata import version; print(json.dumps([version('medre'),version('example-provider')]))",
            ],
            env=env,
        )
    )
    assert (
        versions
        == {"all": ["2.0", "2.0"], "core": ["2.0", "1.0"], "adapters": ["1.0", "2.0"]}[
            scope
        ]
    )
    saved = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
    assert "constraints" not in saved and "overrides" not in saved
    assert [
        requirement["git"].split("#", 1)[0] for requirement in saved["requirements"]
    ] == [
        requirement["git"].split("#", 1)[0] for requirement in receipt["requirements"]
    ]
