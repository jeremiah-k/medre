"""Support ``python -m medre``.

Delegates to the canonical CLI entry point ``medre.cli:main``
without importing optional transport SDKs at module load time.
"""

from medre.cli import main

main()
