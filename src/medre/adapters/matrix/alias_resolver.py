"""Matrix room alias resolution.

Provides a self-contained helper for resolving Matrix room aliases
(``#room:server``) to canonical room IDs (``!roomid:server``) using
a lightweight nio client.
"""

from __future__ import annotations


async def resolve_room_alias(
    homeserver: str,
    access_token: str,
    alias: str,
) -> str | None:
    """Resolve a single Matrix room alias to its canonical room ID.

    Creates a temporary nio ``AsyncClient``, sets the access token,
    calls ``room_resolve_alias``, and closes the client.

    Returns the canonical room ID (``!roomid:server``) on success,
    or ``None`` if resolution fails.
    """
    try:
        import nio

        client = nio.AsyncClient(homeserver)
        client.access_token = access_token
        try:
            resp = await client.room_resolve_alias(alias)
            room_id = getattr(resp, "room_id", None)
            return str(room_id) if room_id else None
        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception:
        return None
