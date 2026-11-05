"""Built artifacts retain their own compatibility pins without another checkout."""

import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def test_sdist_rebuilt_wheel_loads_its_own_compatibility_snapshot(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE"):
        shutil.copyfile(root / name, source / name)
    shutil.copytree(
        root / "src" / "medre",
        source / "src" / "medre",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    snapshot = root / ".github" / "adapter-compatibility.json"
    expected = snapshot.read_bytes()
    (source / ".github").mkdir()
    shutil.copyfile(snapshot, source / ".github" / snapshot.name)
    artifacts = tmp_path / "artifacts"
    built = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(artifacts)],
        cwd=source,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with tarfile.open(next(artifacts.glob("*.tar.gz"))) as archive:
        member = next(
            item
            for item in archive.getmembers()
            if item.name.endswith(".github/" + snapshot.name)
        )
        stream = archive.extractfile(member)
        assert stream is not None and stream.read() == expected
    wheel = next(artifacts.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("medre/_data/" + snapshot.name) == expected
    # There is no generated second authority in the source package.
    assert not (source / "src" / "medre" / "_data" / snapshot.name).exists()
    foreign = tmp_path / "foreign"
    (foreign / ".github").mkdir(parents=True)
    (foreign / ".github" / snapshot.name).write_text("[]", encoding="utf-8")
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import json, sys
sys.path.insert(0, sys.argv[1])
from medre.runtime import readiness_report
assert sys.argv[1] in readiness_report.__file__
status, entries = readiness_report._load_manifest()
print(json.dumps([status, [entry['ref'] for entry in entries]]))
""",
            str(wheel),
        ],
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert json.loads(probe.stdout) == [
        "available",
        [item["ref"] for item in json.loads(expected)],
    ]
