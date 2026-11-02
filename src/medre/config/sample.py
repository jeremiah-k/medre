"""Generate installation-aware examples from SDK-free adapter contributions."""

from __future__ import annotations

from typing import Any

from medre.adapter_registry import iter_adapter_specs
from medre.adapter_spec import AdapterSample
from medre.config._yaml import dump_yaml_config

_HEADER = """\
# MEDRE Configuration - YAML format
# Copy to ~/.config/medre/config.yaml or $MEDRE_HOME/config.yaml.
# Installed providers supply their own native sample values. These are fake
# adapters: no SDKs, credentials, hardware, or network are required.
# To use real adapters, consult each provider's documentation and configure
# adapter_kind: real with its connection settings and secret environment overrides.
# See examples/configs/ in the source repository for deployment examples.
runtime:
  name: medre
  shutdown_timeout_seconds: 10
  limits:
    max_inflight_deliveries: 100
    max_inflight_replay_events: 100
    shutdown_drain_timeout_seconds: 10
    delivery_acquire_timeout_seconds: 1.0
logging:
  level: INFO
  format: text
storage:
  backend: sqlite
  path: '{state}/medre.sqlite'
# Durable attachments are disabled until retention and transfer limits are set.
attachments:
  enabled: false
# Content retention is disabled by default: retain mode never collects, and
# relay-only additionally requires enabled: true.
content_retention:
  enabled: false
  mode: retain
"""

_ROUTE_HELP = """\
# Routes connect adapter instances, regardless of their underlying platforms.
# Required fields: source_adapters, dest_adapters (instance IDs).
# directionality: source_to_dest, dest_to_source, or bidirectional.
# priority: lower numbers match first; ownership: shared or exclusive.
# source_channel and dest_channel are opaque conversation addresses. The
# source_room/dest_room aliases refer to the same generic route fields.
# context_map maps source contexts to dest_context or dest_destination;
# Per-context source_origin_label and dest_origin_label control attribution.
# Consult docs/spec/routing-delivery.md for structured targeting examples.
# For fanout, add routes from the same source to further destinations.
# Set enabled: false on a route to exclude it from startup.
"""


def generate_sample_config() -> str:
    """Return loadable YAML containing only installed, opt-in fake providers.

    With no contributions the config explains why no routing can be exercised.
    A single contributing provider gets two distinct instances. Core assigns
    IDs, so independently authored providers never coordinate instance names.
    IDs derive from the transport, so listings, plans, and saved files are
    self-describing; numbering counts per provider, leaving room for further
    instances of the same provider under ``adapters.<provider>``.
    """
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    samples: list[tuple[str, str, AdapterSample]] = []
    for spec in iter_adapter_specs():
        if spec.sample_config is None or spec.fake_adapter is None:
            continue
        sample = spec.sample_config.load()()
        if not isinstance(sample, AdapterSample):
            raise ValueError(
                f"{spec.transport} sample callback must return AdapterSample"
            )
        if sample.channel is not None and not isinstance(sample.channel, str):
            raise ValueError(
                f"{spec.transport} sample channel must be an opaque string"
            )
        values = dict(sample.config)
        forbidden = {"adapter_id", "adapter_kind", "enabled"} & values.keys()
        if forbidden:
            raise ValueError(
                f"{spec.transport} sample overrides lifecycle fields: {sorted(forbidden)}"
            )
        instances = groups.setdefault(spec.transport, {})
        instance_id = f"{spec.transport}_{len(instances) + 1}"
        instances[instance_id] = {"enabled": True, "adapter_kind": "fake", **values}
        samples.append((instance_id, spec.transport, sample))
    if len(samples) == 1:
        source_id, transport, sample = samples[0]
        duplicate_id = f"{transport}_{len(groups[transport]) + 1}"
        groups[transport][duplicate_id] = dict(groups[transport][source_id])
        samples.append((duplicate_id, transport, sample))
    routes = {}
    if samples:
        source_id = samples[0][0]
        for index, (target_id, _, target) in enumerate(samples[1:]):
            route = {
                "source_adapters": [source_id],
                "dest_adapters": [target_id],
                "directionality": "source_to_dest",
                "enabled": True,
                "policy": {"allowed_event_types": ["message.created", "message.text"]},
            }
            if target.channel is not None:
                route["dest_channel"] = target.channel
            routes["sample_bridge" if index == 0 else f"sample_bridge_{index + 1}"] = (
                route
            )
    notice = ""
    if not samples:
        notice = "# No installed provider declares an SDK-free sample. Install an adapter\n# with sample support, or configure one using its documentation.\n# See docs/ops/adapter-installation.md.\n"
    return (
        _HEADER
        + notice
        + dump_yaml_config({"adapters": groups})
        + "\n"
        + _ROUTE_HELP
        + dump_yaml_config({"routes": routes})
    )
