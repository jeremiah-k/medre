"""Exercise installed providers and preload SimpleX's native SQLite library."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as metadata
import json
import os
import shutil
import tempfile
from pathlib import Path

from medre.tool_management import redact_source


def providers() -> list[str]:
    entries = metadata.entry_points(group="medre.adapters")
    names = []
    for entry in entries:
        entry.load()
        names.append(entry.name)
    return sorted(names)


async def simplex() -> None:
    from simplex_chat import ChatApi, MigrationConfirmation, SqliteDb

    with tempfile.TemporaryDirectory() as root:
        api = await ChatApi.init(
            SqliteDb(root + "/profile", ""), confirm=MigrationConfirmation.YES_UP
        )
        await api.close()


def source_record(raw: str) -> dict:
    record = json.loads(raw)
    record["url"] = redact_source(record["url"])
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    names = providers()
    if args.prepare:
        target = Path("/opt/simplex-libs")
        target.mkdir()
        if "simplexchat" in names:
            with tempfile.TemporaryDirectory() as cache:
                os.environ["XDG_CACHE_HOME"] = cache
                asyncio.run(simplex())
                matches = list(Path(cache).glob("simplex-chat/*/sqlite/libsimplex.so"))
                if len(matches) != 1:
                    raise RuntimeError(
                        "Expected one resolved SimpleX SQLite native artifact"
                    )
                shutil.copytree(matches[0].parent, target, dirs_exist_ok=True)
        record = {
            "core": metadata.version("medre"),
            "providers": names,
            "packages": sorted(
                (dist.metadata["Name"], dist.version)
                for dist in metadata.distributions()
            ),
            "direct_sources": {
                dist.metadata["Name"]: source_record(raw)
                for dist in metadata.distributions()
                if (raw := dist.read_text("direct_url.json"))
            },
        }
        Path("/opt/medre-build.json").write_text(json.dumps(record, indent=2) + "\n")
    else:
        record = json.loads(Path("/opt/medre-build.json").read_text())
        if names != record["providers"] or metadata.version("medre") != record["core"]:
            raise RuntimeError("Installed providers differ from the built image record")
        if "simplexchat" in names:
            asyncio.run(simplex())
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
