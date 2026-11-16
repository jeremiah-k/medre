"""Source switching delegates real Git, editable and index installs to uv."""

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

from medre.package_sources import plan_source_switch
from medre.tool_management import SourceCatalog, ToolInstallation, ToolManagementError
from tests.helpers.uv_packages import repository, run

pytestmark = pytest.mark.timeout(120)

EDITABLE_BACKEND = """
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    import json
    wheel = build_wheel(wheel_directory)
    target = pathlib.Path(wheel_directory) / wheel
    project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]
    name = project["name"].replace("-", "_")
    with zipfile.ZipFile(target) as archive:
        files = {path: archive.read(path) for path in archive.namelist() if path != name + ".py"}
    files[name + ".pth"] = str(pathlib.Path.cwd()) + "\\n"
    with zipfile.ZipFile(target, "w") as archive:
        for path, content in files.items(): archive.writestr(path, content)
    return wheel
"""


@pytest.mark.parametrize("target", ["core", "radio"])
def test_real_uv_roundtrips_git_editable_git_and_index(tmp_path, target):
    uv = shutil.which("uv")
    if uv is None or shutil.which("git") is None:
        pytest.skip("The source-switch contract requires uv and Git")
    packages = tmp_path / "packages with spaces"
    packages.mkdir()
    sources = {}
    for name in ["medre", "example-provider"]:
        source = repository(packages, name, "1.0")
        path = packages / name
        (path / "backend.py").write_text(
            (path / "backend.py").read_text() + EDITABLE_BACKEND
        )
        (path / (name.replace("-", "_") + ".py")).write_text("def main(): pass\n")
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
                "editable backend",
            ],
            cwd=path,
        )
        commit = run(["git", "rev-parse", "HEAD"], cwd=path).strip()
        sources[name] = source.rsplit("@", 1)[0] + "@" + commit
    env = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    env.pop("PYTHONPATH", None)
    env.update(
        UV_TOOL_DIR=str(tmp_path / "tools"),
        UV_TOOL_BIN_DIR=str(tmp_path / "bin"),
        UV_CACHE_DIR=str(tmp_path / "cache"),
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_CONFIG="1",
    )
    run(
        [
            uv,
            "tool",
            "install",
            "--python",
            sys.executable,
            "--with",
            sources["example-provider"],
            sources["medre"],
        ],
        env=env,
    )
    prefix = tmp_path / "tools/medre"
    python = prefix / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    catalog = SourceCatalog(None, {"radio": "example-provider"})
    selected = "medre" if target == "core" else "example-provider"
    unselected = "example-provider" if target == "core" else "medre"

    def installation():
        receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text())["tool"]
        return ToolInstallation(uv, prefix, receipt)

    def request(name):
        return next(
            raw for raw in installation().receipt["requirements"] if raw["name"] == name
        )

    unchanged = request(unselected)
    plan = plan_source_switch(
        installation(),
        catalog,
        (target,),
        source="local",
        path=str(packages / selected),
        editable=True,
    )
    run(list(plan.command), env=env)
    assert request(selected)["editable"] == str(packages / selected)
    observed = json.loads(
        run(
            [
                str(python),
                "-c",
                f"import {selected.replace('-', '_')} as p,json;print(json.dumps(p.__file__))",
            ],
            env=env,
        )
    )
    assert Path(observed).resolve().parent == packages / selected
    assert request(unselected) == unchanged
    # Replaying the complete request retains editable mode without bespoke flags.
    run(installation().install_request(installation().requirements()), env=env)
    assert "editable" in request(selected)
    plan = plan_source_switch(
        installation(),
        catalog,
        (target,),
        source="git",
        git_url=(packages / selected).as_uri(),
        ref="main",
    )
    run(list(plan.command), env=env)
    assert "editable" not in request(selected)
    assert "git" in request(selected)
    assert request(unselected) == unchanged

    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    run(
        [
            sys.executable,
            "-c",
            f"import backend;backend.build_wheel({str(wheelhouse)!r})",
        ],
        cwd=packages / selected,
    )
    plan = plan_source_switch(
        installation(), catalog, (target,), source="index", version="1.0"
    )
    run(
        list(plan.command),
        env={**env, "UV_FIND_LINKS": str(wheelhouse), "UV_NO_INDEX": "1"},
    )
    assert request(selected) == {"name": selected, "specifier": "==1.0"}
    assert request(unselected) == unchanged
    # uv persists custom wheelhouse settings; do not discard them on later replay.
    with pytest.raises(ToolManagementError, match="custom uv settings"):
        installation().requirements()
