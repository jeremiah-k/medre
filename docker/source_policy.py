"""Keep authentication material out of persisted container source metadata."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit


def validate_source_url(value: str) -> None:
    """Reject userinfo, queries and unsupported fragments, without echoing URLs."""
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("Invalid image source URL")
    try:
        url = urlsplit(value)
        # Accessing port also detects invalid port syntax.
        _ = url.port
        fragment = parse_qsl(url.fragment, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ValueError("Invalid image source URL") from None
    if (
        url.scheme.lower() not in {"https", "http", "git+https", "git+http", "file"}
        or (url.scheme.lower() != "file" and not url.hostname)
        or "@" in url.netloc
        or url.query
        or len({key for key, _ in fragment}) != len(fragment)
        or any(
            not (
                key == "subdirectory"
                and bool(part)
                or key == "sha256"
                and re.fullmatch(r"[0-9a-fA-F]{64}", part)
            )
            for key, part in fragment
        )
    ):
        raise ValueError(
            "Image source URLs must be public and contain no credentials, query, "
            "or unsupported fragment; use a trusted local checkout for private sources."
        )
