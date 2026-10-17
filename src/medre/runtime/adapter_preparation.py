"""Pure provider-owned configuration preflight shared by authoring and startup."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from medre.adapter_spec import AdapterSpec
from medre.config.paths import MedrePaths
from medre.runtime.errors import RuntimeConfigError


def prepare_adapter_config(
    spec: AdapterSpec,
    adapter_id: str,
    config: Any,
    *,
    paths: MedrePaths,
    routes: Iterable[Any],
) -> Any:
    """Validate and derive native config without constructing an adapter or doing I/O."""
    if spec.runtime_config_preparer is None:
        return config
    try:
        prepare = spec.runtime_config_preparer.load()
        return prepare(config, adapter_id=adapter_id, paths=paths, routes=routes)
    except Exception as exc:
        raise RuntimeConfigError(
            f"Failed to prepare adapter {adapter_id!r} ({spec.transport}): {exc}"
        ) from exc
