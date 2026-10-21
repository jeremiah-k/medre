"""One process exercising an application's durable token manager and whoami."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path


async def main(owner: str, path: Path) -> None:
    if owner == "medre":
        from medre.adapters.matrix.oauth import OAuthSession
        from medre.adapters.matrix.oauth_session import OAuthSessionManager

        session = OAuthSession.parse(json.loads(path.read_text()))
        manager = OAuthSessionManager(session, path)
    else:
        from mmrelay.matrix.oauth import OAuthSession
        from mmrelay.matrix.oauth_session import OAuthSessionManager
        from mmrelay.matrix.oauth_store import OAuthStore

        store = OAuthStore(path)
        session = OAuthSession.parse(await store.load())
        manager = OAuthSessionManager(session, store)
    print("READY", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    token = await manager.access_token()
    import aiohttp

    async with aiohttp.ClientSession(
        headers={"Authorization": "Bearer " + token}
    ) as http:
        async with http.get(
            session.homeserver + "/_matrix/client/v3/account/whoami"
        ) as response:
            response.raise_for_status()
            identity = await response.json()
    assert identity["user_id"] == session.user_id
    assert identity["device_id"] == session.device_id
    print(
        json.dumps(
            {
                "token_digest": hashlib.sha256(token.encode()).hexdigest(),
                "device_id": identity["device_id"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
