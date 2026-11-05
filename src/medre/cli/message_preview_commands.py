"""Read-only operator-facing rendering preview for one configured route."""

from __future__ import annotations

import asyncio
import sys

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.runtime.message_preview import build_message_preview

from .exit_codes import EXIT_CONFIG, EXIT_OK
from .json import to_json


def _config_preview_message(
    config_path: str | None,
    *,
    route_id: str,
    repeat: int,
    relation: str,
    as_json: bool,
) -> None:
    """Print synthetic renderer output; never display exception or config values."""
    try:
        config, _, paths = load_config(config_path)
        effective = apply_env_overrides(config, paths)
        # Argparse constrains relation; this cast keeps the runtime API typed.
        from typing import Literal, cast

        report = asyncio.run(
            build_message_preview(
                effective,
                route_id=route_id,
                repeat=repeat,
                relation=cast(Literal["none", "reply"], relation),
            )
        )
    except Exception:
        print(
            "Message preview refused: configuration or pure renderer check failed; "
            "run 'medre config check' and 'medre routes plan'.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG) from None
    if as_json:
        print(to_json(report))
    else:
        print("MEDRE synthetic message preview (never sent)")
        print(f"Route: {report['route_id']} | status: {report['status']}")
        for leg in report["legs"]:
            print(
                f"  {leg['source_adapter']} -> {leg['target_adapter']} "
                f"[{leg['expanded_route_id']}] {leg['status']}"
            )
            if leg["status"] == "rendered":
                print(f"    {leg['text']}")
                if leg["truncated"]:
                    print("    (truncated by renderer)")
                if leg["fallback_applied"]:
                    print(f"    (fallback: {leg['fallback_applied']})")
            else:
                print(f"    (reason: {leg['reason']})")
        print("No runtime, native acceptance, or recipient observation performed.")
    raise SystemExit(EXIT_OK if report["status"] == "rendered" else EXIT_CONFIG)
