"""Minimal metadata declarations for provider-independent registry tests."""

from medre.adapter_spec import AdapterSpec, SymbolRef


def make_adapter_spec(transport: str) -> AdapterSpec:
    symbol = SymbolRef("builtins", "object")
    return AdapterSpec(
        transport=transport,
        config=symbol,
        runtime_config=None,
        adapter=symbol,
        fake_adapter=symbol,
        renderer_factory=None,
        dependency_probe=None,
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
        native_namespace_reader=symbol,
        versioned_namespace_reader=symbol,
        attribution_projector=symbol,
    )
