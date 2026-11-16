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

from source_policy import validate_source_url


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
    validate_source_url(record["url"])
    if (record.get("dir_info") or {}).get("editable"):
        raise ValueError("Runtime images must not contain editable installations")
    return record


def installation_record(names: list[str]) -> dict:
    """Inspect actual installed metadata, including transitive direct sources."""
    distributions = list(metadata.distributions())
    return {
        "core": metadata.version("medre"),
        "providers": names,
        "packages": sorted(
            [dist.metadata["Name"], dist.version] for dist in distributions
        ),
        "direct_sources": {
            dist.metadata["Name"]: source_record(raw)
            for dist in distributions
            if (raw := dist.read_text("direct_url.json")) is not None
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    names = providers()
    if args.prepare:
        record = installation_record(names)
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
        Path("/opt/medre-build.json").write_text(json.dumps(record, indent=2) + "\n")
    else:
        record = json.loads(Path("/opt/medre-build.json").read_text())
        if installation_record(names) != record:
            raise RuntimeError("Installed packages differ from the built image record")
        if "simplexchat" in names:
            asyncio.run(simplex())
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
