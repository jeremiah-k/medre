"""Shared SDK-related constants for boundary tests.

Single source of truth for SDK instantiation patterns used across
test_deployment_boundaries.py and test_operational_boundaries.py.
"""

# SDK class instantiation patterns — used to scan source for forbidden
# direct SDK construction (e.g. ``nio.AsyncClient(``).
_SDK_INSTANTIATION_PATTERNS: tuple[str, ...] = (
    "nio.AsyncClient(",
    "MeshtasticClient(",
    "MeshCore(",
    "RNS.Reticulum(",
    "LXMF.LXMF(",
    "lxmf.LXMF(",
)
