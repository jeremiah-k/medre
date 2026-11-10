"""Derive packaged compatibility pins and frozen schemas from checkout sources."""

import hashlib
import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


def frozen_schema_bytes(path: Path, expected: str) -> bytes:
    """Recover frozen LF bytes only when the manifest proves exact identity.

    A Git clone of main followed by checkout of a candidate revision can retain
    unchanged CRLF files even when that revision adds a -text attribute.
    """
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() == expected:
        return data
    # The setuptools PEP 517 runner rewrites escaped CRLF literals in setup.py.
    restored = data.replace(bytes((13, 10)), bytes((10,)))
    if hashlib.sha256(restored).hexdigest() != expected:
        raise ValueError(
            f"Frozen schema digest mismatch: {path.name}; "
            f"expected={expected}; actual={hashlib.sha256(data).hexdigest()}; "
            f"lf={hashlib.sha256(restored).hexdigest()}; "
            f"size={len(data)}; restored_size={len(restored)}; "
            f"crlf_count={data.count(bytes((13, 10)))}; prefix={data[:16].hex()}"
        )
    return restored


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
            (schema_target / name).write_bytes(frozen_schema_bytes(path, expected))
        self.copy_file(str(freeze_manifest), str(schema_target / freeze_manifest.name))


setup(cmdclass={"build_py": BuildPyWithCompatibility})
