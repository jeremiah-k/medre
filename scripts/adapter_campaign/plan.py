"""Explicit test topology, independent of runtime route matching."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from medre.config._yaml import parse_yaml_config
from medre.config.identifiers import adapter_id_problem
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve

TRANSPORTS = frozenset(
    {"matrix", "meshtastic", "meshcore", "lxmf", "xmpp", "chatmail", "simplexchat"}
)
RADIOS = frozenset({"meshtastic", "meshcore", "lxmf"})


class PlanError(ValueError):
    """Harness-authored validation text that is safe for public output."""


@dataclass(frozen=True)
class Plan:
    runtime_file: Path
    endpoints: dict[str, dict]
    timeout: float
    pacing: float
    settle: float

    @property
    def full(self) -> bool:
        return {
            endpoint["transport"] for endpoint in self.endpoints.values()
        } == TRANSPORTS

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return [
            (source, target)
            for source in self.endpoints
            for target in self.endpoints
            if source != target
        ]


def _seconds(value, name: str, minimum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not minimum <= value <= 300
    ):
        raise PlanError(f"{name} must be finite and between {minimum} and 300 seconds")
    return float(value)


def load_plan(path: Path, only: set[str] | None = None) -> Plan:
    data = parse_yaml_config(path.read_text(), str(path))
    if (
        set(data)
        - {
            "version",
            "runtime_config",
            "endpoints",
            "timeout_seconds",
            "pacing_seconds",
            "settle_seconds",
        }
        or type(data.get("version")) is not int
        or data.get("version") != 1
    ):
        raise PlanError("Campaign requires version 1 and recognized fields")
    entries = data.get("endpoints")
    if not isinstance(entries, dict) or not entries:
        raise PlanError("Campaign endpoints must be a nonempty mapping")
    selected = {}
    for name, endpoint in entries.items():
        if not isinstance(name, str) or adapter_id_problem(name):
            raise PlanError("Campaign adapter IDs must be valid MEDRE identifiers")
        required = {"transport", "channel", "expected_sender", "peer"}
        if (
            not isinstance(endpoint, dict)
            or not required <= set(endpoint)
            or set(endpoint) - required - {"expected_prefixes"}
        ):
            raise PlanError(
                f"{name}: require transport, channel, expected_sender, peer"
            )
        if endpoint["transport"] not in TRANSPORTS:
            raise PlanError(f"{name}: unknown campaign transport")
        if not all(
            isinstance(endpoint[key], str) and endpoint[key]
            for key in ("channel", "expected_sender")
        ) or not isinstance(endpoint["peer"], dict):
            raise PlanError(f"{name}: channels and senders must be nonempty strings")
        prefixes = endpoint.get("expected_prefixes", {})
        if not isinstance(prefixes, dict) or any(
            not isinstance(source, str)
            or source not in entries
            or not isinstance(prefix, str)
            or (prefix and not prefix.isprintable())
            for source, prefix in prefixes.items()
        ):
            raise PlanError(
                f"{name}: expected_prefixes must map source IDs to printable prefixes"
            )
        if only is None or endpoint["transport"] in only:
            selected[name] = endpoint
    transports = [entry["transport"] for entry in selected.values()]
    expected = TRANSPORTS if only is None else only
    if (
        len(expected) < 2
        or not expected <= TRANSPORTS
        or set(transports) != expected
        or len(transports) != len(expected)
    ):
        raise PlanError(
            "Require exactly one endpoint per selected transport; default requires all seven"
        )
    # Delivery and presentation are independent qualification facts.
    for source in selected:
        for target, destination in selected.items():
            if source != target and source not in destination.get(
                "expected_prefixes", {}
            ):
                raise PlanError(
                    f"{source} -> {target}: require an expected_prefixes entry on the target"
                )
    radio = bool(expected & RADIOS)
    runtime_file = data.get("runtime_config")
    if not isinstance(runtime_file, str) or not runtime_file:
        raise PlanError("runtime_config must name a dedicated lab YAML file")
    return Plan(
        (path.parent / runtime_file).resolve(),
        selected,
        _seconds(data.get("timeout_seconds", 90), "timeout_seconds", 1),
        _seconds(
            data.get("pacing_seconds", 2.5), "pacing_seconds", 2.5 if radio else 0
        ),
        _seconds(
            data.get("settle_seconds", 5), "settle_seconds", 2.5 if radio else 0.1
        ),
    )


def runtime_config(plan: Plan, directory: Path, *, fake: bool = False):
    """Isolate core state and author one channel-scoped route per ordered pair."""
    data = parse_yaml_config(plan.runtime_file.read_text(), str(plan.runtime_file))
    groups = data.get("adapters", {})
    selected = {}
    for name, endpoint in plan.endpoints.items():
        transport = endpoint["transport"]
        config = groups.get(transport, {}).get(name)
        if (
            not isinstance(config, dict)
            or config.get("adapter_kind") != ("fake" if fake else "real")
            or config.get("enabled", True) is not True
        ):
            raise PlanError(
                f"{name}: require an enabled {'fake' if fake else 'real'} lab adapter"
            )
        peer = endpoint["peer"]
        if transport == "chatmail" and not fake:
            if (
                Path(peer["accounts_dir"]).resolve()
                == Path(config["accounts_dir"]).resolve()
            ):
                raise PlanError(
                    "Chatmail bot and peer require separate profile directories"
                )
        if transport == "simplexchat" and not fake:
            bot_prefix, peer_prefix = config.get("database_prefix"), peer.get(
                "database_prefix"
            )
            if (
                not isinstance(bot_prefix, str)
                or not bot_prefix
                or not isinstance(peer_prefix, str)
                or not peer_prefix
                or Path(bot_prefix + "_chat.db").resolve()
                == Path(peer_prefix + "_chat.db").resolve()
            ):
                raise PlanError("SimpleX bot and peer require separate native profiles")
        selected.setdefault(transport, {})[name] = config
    data["adapters"] = selected
    data["storage"] = {"backend": "sqlite", "path": str(directory / "medre.sqlite3")}
    data["retry"] = {"enabled": False}
    data["logging"] = {"level": "WARNING"}
    data["routes"] = {
        f"{source}_to_{target}": {
            "source_adapters": [source],
            "dest_adapters": [target],
            "source_channel": plan.endpoints[source]["channel"],
            "dest_channel": plan.endpoints[target]["channel"],
            "policy": {"allowed_event_types": ["message.created", "message.text"]},
        }
        for source, target in plan.pairs
    }
    paths = resolve(environ={"MEDRE_HOME": str(directory)})
    return parse_runtime_config(data, paths), paths
