"""Derive packaged compatibility pins from the checkout's canonical snapshot."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithCompatibility(build_py):
    """Copy pins into build output without maintaining another source snapshot."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).parent / ".github" / "adapter-compatibility.json"
        target = Path(self.build_lib) / "medre" / "_data" / source.name
        self.mkpath(str(target.parent))
        self.copy_file(str(source), str(target))
        self.copy_tree(
            str(Path(__file__).parent / "docs" / "schemas"),
            str(Path(self.build_lib) / "medre" / "_schemas"),
        )


setup(cmdclass={"build_py": BuildPyWithCompatibility})
