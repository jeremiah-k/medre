"""Derive packaged compatibility pins and frozen schemas from checkout sources."""

import hashlib
import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithCompatibility(build_py):
    """Copy runtime resources without maintaining another source snapshot."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).parent / ".github" / "adapter-compatibility.json"
        target = Path(self.build_lib) / "medre" / "_data" / source.name
        self.mkpath(str(target.parent))
        self.copy_file(str(source), str(target))

        schema_root = Path(__file__).parent / "docs" / "schemas"
        freeze_manifest = schema_root / "v0.1.0.freeze.json"
        frozen = json.loads(freeze_manifest.read_text(encoding="utf-8"))[
            "schema_files_sha256"
        ]
        if not isinstance(frozen, dict) or not frozen:
            raise ValueError("Frozen schema manifest must list schema digests")
        schema_target = Path(self.build_lib) / "medre" / "_schemas"
        # A previous build cannot leave extra packaged schema files behind.
        shutil.rmtree(schema_target, ignore_errors=True)
        self.mkpath(str(schema_target))
        for name, expected in sorted(frozen.items()):
            if (
                Path(name).name != name
                or not name.endswith((".schema.json", ".schema.yaml", ".schema.sql"))
                or not isinstance(expected, str)
            ):
                raise ValueError(f"Invalid frozen schema entry: {name!r}")
            path = schema_root / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Frozen schema digest mismatch: {name}")
            self.copy_file(str(path), str(schema_target / name))
        self.copy_file(str(freeze_manifest), str(schema_target / freeze_manifest.name))


setup(cmdclass={"build_py": BuildPyWithCompatibility})
