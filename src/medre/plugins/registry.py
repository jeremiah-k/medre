"""Registry for separately distributed MEDRE plugin types.

The registry contains *declarative registration metadata only*. It
deliberately stores implementation references as strings so configuration and
discovery import only SDK-free declarations. It does not import plugin
implementations.

Plugin packages register zero-argument factories through Python entry points
in the ``medre.plugins`` group. Discovery runs once at import time; installing
new packages requires a process restart. The registry never downloads
packages and never executes plugins; pipeline execution arrives with the
plugin execution runtime defined in ``docs/spec/plugins.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from medre.plugins.capabilities import (
    HOST_GRANTABLE_CAPABILITIES,
    PluginCapability,
)
from medre.plugins.decisions import PLUGIN_OUTPUT_KINDS
from medre.plugins.spec import PluginSpec
from medre.plugins.stages import PluginStage
from medre.registration import SymbolRef, valid_registration_name

__all__ = [
    "PLUGIN_REGISTRY",
    "PluginDiscoveryFailure",
    "PluginTypeRegistry",
    "get_plugin_spec",
    "iter_plugin_specs",
    "plugin_discovery_failures",
    "registered_plugins",
]


@dataclass(frozen=True, slots=True)
class PluginDiscoveryFailure:
    """A quarantined plugin provider that diagnostics can report safely."""

    plugin_id: str
    entry_point: str
    reason: str
    provider_distribution: str | None = None


@dataclass(frozen=True, slots=True, init=False)
class PluginTypeRegistry:
    """Immutable ordered registry of discovered plugin type specifications."""

    _specs: tuple[PluginSpec, ...]
    _by_plugin: Mapping[str, PluginSpec]
    failures: tuple[PluginDiscoveryFailure, ...]

    def __init__(
        self,
        specs: Iterable[PluginSpec],
        *,
        failures: Iterable[PluginDiscoveryFailure] = (),
    ) -> None:
        """Build and validate an immutable registry from *specs*.

        Raises :class:`ValueError` for invalid or duplicate plugin
        identifiers, reserved or missing capabilities, invalid stage or
        transform-kind declarations, or invalid dependency logger
        declarations.
        """
        from medre.core.events.kinds import is_registered
        from medre.core.observability.log_levels import VALID_LEVEL_NAMES

        object.__setattr__(self, "failures", tuple(failures))
        ordered = tuple(specs)
        by_plugin: dict[str, PluginSpec] = {}
        for spec in ordered:
            if not isinstance(spec, PluginSpec):
                raise TypeError("registry entries must be PluginSpec declarations")
            plugin_id = spec.plugin_id
            if not valid_registration_name(plugin_id):
                raise ValueError(f"invalid plugin id: {plugin_id!r}")
            if plugin_id in by_plugin:
                raise ValueError(f"duplicate plugin id: {plugin_id!r}")
            if not isinstance(spec.version, str) or not spec.version.strip():
                raise ValueError(f"plugin {plugin_id!r} must declare a version")
            for field, reference in (("plugin", spec.plugin), ("config", spec.config)):
                if field == "config" and reference is None:
                    continue
                if (
                    not isinstance(reference, SymbolRef)
                    or not isinstance(reference.module, str)
                    or not reference.module
                    or not all(
                        part.isidentifier() for part in reference.module.split(".")
                    )
                    or not isinstance(reference.name, str)
                    or not reference.name.isidentifier()
                ):
                    raise ValueError(
                        f"plugin {plugin_id!r} has an invalid {field} SymbolRef"
                    )
            capabilities = spec.capabilities
            if not capabilities:
                raise ValueError(
                    f"plugin {plugin_id!r} must declare at least one capability"
                )
            for capability in capabilities:
                if not isinstance(capability, PluginCapability):
                    raise ValueError(
                        f"invalid capability declaration for {plugin_id!r}: "
                        f"{capability!r}"
                    )
                if capability not in HOST_GRANTABLE_CAPABILITIES:
                    raise ValueError(
                        f"plugin {plugin_id!r} declares reserved capability "
                        f"{capability.value!r}; routing authority remains with "
                        "the compiled operator configuration"
                    )
            if PluginCapability.READ_EVENTS not in capabilities:
                raise ValueError(
                    f"plugin {plugin_id!r} must declare read_events; every "
                    "stage subscription processes canonical events"
                )
            stages = spec.stages
            if not stages:
                raise ValueError(
                    f"plugin {plugin_id!r} must subscribe to at least one stage"
                )
            for stage in stages:
                if not isinstance(stage, PluginStage):
                    raise ValueError(
                        f"invalid stage subscription for {plugin_id!r}: {stage!r}"
                    )
            if (
                PluginStage.EVENT_POLICY in stages
                and PluginCapability.SUPPRESS_EVENTS not in capabilities
            ):
                raise ValueError(
                    f"plugin {plugin_id!r} must declare suppress_events for event_policy"
                )
            declares_transform_kinds = bool(
                spec.transform_inputs and spec.transform_outputs
            )
            if PluginStage.TRANSFORM in stages and not declares_transform_kinds:
                raise ValueError(
                    f"plugin {plugin_id!r} must declare transform_inputs and "
                    "transform_outputs when subscribing to the transform stage"
                )
            if PluginStage.TRANSFORM not in stages and (
                spec.transform_inputs or spec.transform_outputs
            ):
                raise ValueError(
                    f"plugin {plugin_id!r} declares transform kinds without "
                    "subscribing to the transform stage"
                )
            for kind in (*spec.transform_inputs, *spec.transform_outputs):
                if not isinstance(kind, str) or not is_registered(kind):
                    raise ValueError(
                        f"plugin {plugin_id!r} declares unregistered event "
                        f"kind: {kind!r}"
                    )
            if any(kind not in PLUGIN_OUTPUT_KINDS for kind in spec.transform_outputs):
                raise ValueError(
                    "transform outputs must not contain core-owned evidence kinds"
                )
            logger_names = set()
            for logger_name, level in spec.logger_levels:
                if (
                    not isinstance(logger_name, str)
                    or not logger_name
                    or logger_name in {"root", "medre"}
                    or logger_name.startswith("medre.")
                    or logger_name in logger_names
                    or level not in VALID_LEVEL_NAMES
                ):
                    raise ValueError(
                        f"invalid dependency logger declaration for {plugin_id!r}"
                    )
                logger_names.add(logger_name)
            by_plugin[plugin_id] = spec
        object.__setattr__(self, "_specs", ordered)
        object.__setattr__(self, "_by_plugin", MappingProxyType(by_plugin))

    def __iter__(self) -> Iterator[PluginSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def get(self, plugin_id: str) -> PluginSpec | None:
        """Return the spec for *plugin_id*, or ``None`` when unregistered."""
        return self._by_plugin.get(plugin_id)

    def require(self, plugin_id: str) -> PluginSpec:
        """Return the spec for *plugin_id*.

        Raises :class:`KeyError` naming the known plugin types when
        *plugin_id* is not registered.
        """
        try:
            return self._by_plugin[plugin_id]
        except KeyError as exc:
            raise KeyError(
                f"unknown plugin type {plugin_id!r}; "
                f"known plugin types: {', '.join(self.plugin_ids())}"
            ) from exc

    def plugin_ids(self) -> tuple[str, ...]:
        """Return plugin type names in registration order."""
        return tuple(spec.plugin_id for spec in self._specs)


def _discover_installed_plugins() -> PluginTypeRegistry:
    """Quarantine broken and conflicting providers while retaining diagnostics.

    No winner is selected for duplicate plugin-type ownership. Healthy,
    unrelated providers remain usable, including by diagnostics.
    """
    from collections import Counter
    from importlib.metadata import entry_points

    from medre.core.observability.sanitization import sanitize_error

    candidates = []
    failures = []
    providers = sorted(
        entry_points(group="medre.plugins"),
        key=lambda ep: (
            ep.name,
            ep.value,
            getattr(getattr(ep, "dist", None), "name", "") or "",
        ),
    )
    for ep in providers:
        try:
            spec = ep.load()()
            if not isinstance(spec, PluginSpec):
                raise TypeError(f"expected PluginSpec, received {type(spec).__name__}")
            if spec.plugin_id != ep.name:
                raise ValueError(
                    f"entry point {ep.name!r} registered plugin {spec.plugin_id!r}"
                )
            PluginTypeRegistry((spec,))
        except Exception as exc:
            failures.append(
                PluginDiscoveryFailure(
                    ep.name,
                    ep.value,
                    sanitize_error(f"{type(exc).__name__}: {exc}"),
                    getattr(getattr(ep, "dist", None), "name", None),
                )
            )
        else:
            candidates.append((ep, spec))
    plugin_ids = Counter(ep.name for ep in providers)
    accepted = []
    for ep, spec in candidates:
        if plugin_ids[spec.plugin_id] > 1:
            failures.append(
                PluginDiscoveryFailure(
                    ep.name,
                    ep.value,
                    "Conflicting provider ownership: plugin type declared by "
                    "multiple entry points",
                    getattr(getattr(ep, "dist", None), "name", None),
                )
            )
        else:
            accepted.append(spec)
    return PluginTypeRegistry(accepted, failures=failures)


def plugin_discovery_failures() -> tuple[PluginDiscoveryFailure, ...]:
    """Return installed providers that failed registration."""
    return PLUGIN_REGISTRY.failures


def iter_plugin_specs() -> tuple[PluginSpec, ...]:
    """Return installed plugin specs in deterministic registration order."""
    return tuple(PLUGIN_REGISTRY)


def get_plugin_spec(plugin_id: str) -> PluginSpec | None:
    """Return the installed spec for *plugin_id*, or ``None``."""
    return PLUGIN_REGISTRY.get(plugin_id)


def registered_plugins() -> tuple[str, ...]:
    """Return the installed plugin type vocabulary."""
    return PLUGIN_REGISTRY.plugin_ids()


# Define the public lookup functions before loading factories: a declarative
# provider may import SDK-free public helpers while discovery is in progress.
# Factories must not query the incomplete registry or import implementations.
PLUGIN_REGISTRY = _discover_installed_plugins()
