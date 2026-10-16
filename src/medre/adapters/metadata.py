"""Public SDK-free metadata helpers for independently distributed adapters.

Namespace validation owns only generic version structure. Native field
interpretation and attribution remain with the registered provider. Import
these helpers from codecs/renderers, leaving registration factories declarative
and their implementation references lazy.
"""

from medre.adapters._attribution_dispatch import (
    detect_source_platform,
    project_source_fields,
)
from medre.adapters._native_metadata import current_namespace, versioned_namespace
from medre.adapters._native_metadata_dispatch import (
    current_native_namespace,
    versioned_native_namespace,
)

__all__ = [
    "current_namespace",
    "versioned_namespace",
    "current_native_namespace",
    "versioned_native_namespace",
    "detect_source_platform",
    "project_source_fields",
]
