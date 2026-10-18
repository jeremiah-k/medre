"""Declared provider samples supply offline defaults for capability audits."""

import msgspec

from medre.adapter_registry import get_adapter_spec
from medre.core.contracts.adapter import AdapterCapabilities


def default_provider_capabilities(transport: str) -> AdapterCapabilities:
    """Construct an unstarted adapter with its typed, provider-owned sample."""
    spec = get_adapter_spec(transport)
    assert spec is not None and spec.sample_config is not None
    sample = spec.sample_config.load()()
    config = msgspec.convert(
        {"adapter_id": "capability-audit", **dict(sample.config)},
        type=spec.config.load(),
        strict=False,
    )
    return spec.adapter.load()(config)._capabilities
