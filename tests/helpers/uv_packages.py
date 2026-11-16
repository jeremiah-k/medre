"""Disposable dependency-free Git packages for actual uv tool contracts."""

import subprocess

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
