"""Run with --run to permit sends; the default prints the expected topology."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .plan import load_plan, runtime_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, required=True, help="Private campaign YAML"
    )
    parser.add_argument(
        "--only", help="Explicit partial run: comma-separated transport names"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Fresh private evidence directory; required with --run",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Connect to configured lab endpoints and send nonces",
    )
    args = parser.parse_args()
    try:
        plan = load_plan(args.config, set(args.only.split(",")) if args.only else None)
    except ValueError as exc:
        # Plan validation messages name manifest fields; they carry no credentials.
        print(f"Invalid campaign plan: {exc}")
        return 1
    try:
        if not args.run:
            # Validate the runtime too, but never build it or open any state.
            runtime_config(plan, Path("/tmp/medre-campaign-plan"))
            print(
                json.dumps(
                    {
                        "coverage": "full" if plan.full else "partial",
                        "pairs": plan.pairs,
                        "sends": len(plan.endpoints),
                    },
                    indent=2,
                )
            )
            return 0
        if args.output is None:
            parser.error("--run requires --output pointing to a fresh directory")
        from .run import run

        asyncio.run(run(plan, args.output.resolve()))
    except (Exception, KeyboardInterrupt) as exc:
        # Runtime and SDK errors can contain credentials. Keep public output bounded.
        print(
            f"Campaign failed ({type(exc).__name__}); inspect the private evidence directory."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
