"""Explicit test topology, independent of runtime route matching."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from medre.config._yaml import parse_yaml_config
from medre.config.identifiers import adapter_id_problem
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve

TRANSPORTS = frozenset({"matrix", "meshtastic", "meshcore", "lxmf", "xmpp", "chatmail"})
RADIOS = frozenset({"meshtastic", "meshcore", "lxmf"})


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
        raise ValueError(f"{name} must be finite and between {minimum} and 300 seconds")
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
        raise ValueError("Campaign requires version 1 and recognized fields")
    entries = data.get("endpoints")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Campaign endpoints must be a nonempty mapping")
    selected = {}
    for name, endpoint in entries.items():
        if not isinstance(name, str) or adapter_id_problem(name):
            raise ValueError("Campaign adapter IDs must be valid MEDRE identifiers")
        if not isinstance(endpoint, dict) or set(endpoint) != {
            "transport",
            "channel",
            "expected_sender",
            "peer",
        }:
            raise ValueError(
                f"{name}: require transport, channel, expected_sender, peer"
            )
        if endpoint["transport"] not in TRANSPORTS:
            raise ValueError(f"{name}: unknown campaign transport")
        if not all(
            isinstance(endpoint[key], str) and endpoint[key]
            for key in ("channel", "expected_sender")
        ) or not isinstance(endpoint["peer"], dict):
            raise ValueError(f"{name}: channels and senders must be nonempty strings")
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
        raise ValueError(
            "Require exactly one endpoint per selected transport; default requires all six"
        )
    radio = bool(expected & RADIOS)
    runtime_file = data.get("runtime_config")
    if not isinstance(runtime_file, str) or not runtime_file:
        raise ValueError("runtime_config must name a dedicated lab YAML file")
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
            raise ValueError(
                f"{name}: require an enabled {'fake' if fake else 'real'} lab adapter"
            )
        peer = endpoint["peer"]
        if transport == "chatmail" and not fake:
            if (
                Path(peer["accounts_dir"]).resolve()
                == Path(config["accounts_dir"]).resolve()
            ):
                raise ValueError(
                    "Chatmail bot and peer require separate profile directories"
                )
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
