"""Run the established evidence campaign with production timestamped logging."""

from medre.core.observability.logging import setup_logging

from ..__main__ import main

if __name__ == "__main__":
    setup_logging(level="INFO")
    raise SystemExit(main())
