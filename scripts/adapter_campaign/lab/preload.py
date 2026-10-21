"""Resolve the official SDK's native artifact at build time, never during tests."""

import asyncio
import tempfile


async def preload() -> None:
    from simplex_chat import ChatApi, MigrationConfirmation, SqliteDb

    with tempfile.TemporaryDirectory() as root:
        api = await ChatApi.init(
            SqliteDb(root + "/profile", ""), confirm=MigrationConfirmation.YES_UP
        )
        await api.close()


if __name__ == "__main__":
    asyncio.run(preload())
