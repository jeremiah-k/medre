"""A private Compose project; no published ports, host networking, or privilege."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import yaml

from . import LabError
from .sources import write_json


def configure(directory: Path, images: dict[str, str], project: str, mas: dict) -> None:
    config = directory / "config"
    # Synapse keeps its state under the private lab directory instead of a
    # named volume: this host identity creates and owns the bind target before
    # any container starts, so the homeserver runs unprivileged and no
    # root-mode ownership step inside Docker is ever needed.
    synapse_data = directory / "data" / "synapse"
    synapse_data.mkdir(parents=True, mode=0o700)
    password = secrets.token_urlsafe(32)
    shared = secrets.token_hex(32)
    write_json(
        config / "accounts.json",
        {
            "password": password,
            "users": ["medre", "mmrelay", "peer"],
            "homeserver": "https://matrix.lab.test",
            "issuer": "https://auth.lab.test/",
        },
    )
    mas["http"].update(
        public_base="https://auth.lab.test/", issuer="https://auth.lab.test/"
    )
    mas["http"]["listeners"] = [
        {
            "name": "web",
            "binds": [{"address": "0.0.0.0:8080"}],
            "resources": [
                {"name": name}
                for name in (
                    "discovery",
                    "human",
                    "oauth",
                    "compat",
                    "graphql",
                    "assets",
                )
            ],
        }
    ]
    mas["database"]["uri"] = f"postgresql://mas:{password}@postgres/mas"
    mas["matrix"] = {
        "kind": "synapse",
        "homeserver": "matrix.lab.test",
        "secret": shared,
        "endpoint": "http://synapse:8008/",
    }
    mas["experimental"] = {"access_token_ttl": 60}
    (config / "mas.yaml").write_text(yaml.safe_dump(mas))
    synapse = {
        "server_name": "matrix.lab.test",
        "public_baseurl": "https://matrix.lab.test/",
        "pid_file": "/data/homeserver.pid",
        "report_stats": False,
        "listeners": [
            {
                "port": 8008,
                "tls": False,
                "type": "http",
                "bind_addresses": [
                    "0.0.0.0",  # nosec B104: no published container ports.
                ],
                "resources": [{"names": ["client"], "compress": False}],
            }
        ],
        "database": {"name": "sqlite3", "args": {"database": "/data/homeserver.db"}},
        "signing_key_path": "/data/signing.key",
        "media_store_path": "/data/media",
        "macaroon_secret_key": secrets.token_hex(32),
        "form_secret": secrets.token_hex(32),
        "matrix_authentication_service": {
            "enabled": True,
            "endpoint": "http://mas:8080/",
            "secret": shared,
        },
        "trusted_key_servers": [],
        "suppress_key_server_warning": True,
        "rc_message": {"per_second": 100, "burst_count": 1000},
        "rc_login": {
            "address": {"per_second": 100, "burst_count": 1000},
            "account": {"per_second": 100, "burst_count": 1000},
        },
    }
    (config / "synapse.yaml").write_text(yaml.safe_dump(synapse))
    (config / "Caddyfile").write_text("""{
  auto_https off
}
https://matrix.lab.test {
  tls /config/server.crt /config/server.key
  @wellknown path /.well-known/matrix/client
  header @wellknown Content-Type application/json
  respond @wellknown `{"m.homeserver":{"base_url":"https://matrix.lab.test"}}`
  @compat path_regexp compat ^/_matrix/client/[^/]+/(login|logout|refresh)$
  reverse_proxy @compat mas:8080
  reverse_proxy synapse:8008
}
https://auth.lab.test {
  tls /config/server.crt /config/server.key
  reverse_proxy mas:8080
}
""")
    (config / "prosody.cfg.lua").write_text("""admins = { "medre@xmpp.lab.test" }
plugin_paths = { "/usr/lib/prosody/modules" }
modules_enabled = { "roster"; "saslauth"; "tls"; "disco"; "ping"; "version"; "uptime"; "carbons"; "smacks"; "mam"; }
authentication = "internal_hashed"
c2s_require_encryption = true
allow_registration = false
c2s_ports = {5222}
s2s_ports = {}
pidfile = "/var/run/prosody/prosody.pid"
log = { {levels = {min = "info"}, to = "console"}; }
VirtualHost "xmpp.lab.test"
  ssl = { key = "/certs/server.key"; certificate = "/certs/server.crt"; }
Component "rooms.xmpp.lab.test" "muc"
  restrict_room_creation = false
  modules_enabled = { "muc_mam"; }
""")
    services: dict = {
        # Policy exception: the official entrypoint chowns the data directory
        # as root and then demotes to its own postgres user (UID 70 on this
        # alpine image); the data path is a named volume, so no root-owned
        # path ever reaches a host bind mount.
        "postgres": {
            "image": images["postgres"],
            "environment": {
                "POSTGRES_USER": "mas",
                "POSTGRES_DB": "mas",
                "POSTGRES_PASSWORD": password,
            },
            "volumes": ["postgres:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U mas"],
                "interval": "2s",
                "timeout": "2s",
                "retries": 30,
            },
        },
        # The MAS image pins the distroless nonroot identity (UID/GID 65532).
        "mas": {
            "image": images["mas"],
            "user": "65532:65532",
            "command": ["server", "--config", "/config/mas.yaml"],
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "volumes": [f"{config}:/config:ro"],
        },
        # The image's start.py only demotes with a pre-owned data directory:
        # the bind target above is created host-side, so the homeserver runs
        # as this host identity exactly like the runner service.
        "synapse": {
            "image": images["synapse"],
            "user": f"{os.getuid()}:{os.getgid()}",
            "environment": {"SYNAPSE_CONFIG_PATH": "/config/synapse.yaml"},
            "volumes": [f"{config}:/config:ro", f"{synapse_data}:/data"],
        },
        # The Caddy image ships no non-root user; UID/GID 1000 with the
        # ambient NET_BIND_SERVICE capability serves the TLS listener while
        # the config bind stays read-only and storage remains container-local.
        "proxy": {
            "image": images["caddy"],
            "user": "1000:1000",
            "cap_add": ["NET_BIND_SERVICE"],
            "volumes": [f"{config}:/config:ro"],
            "command": [
                "caddy",
                "run",
                "--config",
                "/config/Caddyfile",
                "--adapter",
                "caddyfile",
            ],
            "networks": {"default": {"aliases": ["matrix.lab.test", "auth.lab.test"]}},
        },
        # Policy exception: the entrypoint aligns the prosody user with the
        # data directory owner and demotes via runuser; the config and
        # certificate binds are read-only and the database is a named volume,
        # so no root-owned path reaches the host.
        "xmpp": {
            "image": images["xmpp"],
            "volumes": [
                f"{config}/prosody.cfg.lua:/etc/prosody/prosody.cfg.lua:ro",
                f"{config}:/certs:ro",
                "xmpp:/var/lib/prosody",
            ],
            "networks": {
                "default": {"aliases": ["xmpp.lab.test", "rooms.xmpp.lab.test"]}
            },
        },
        # The image pins the greenmail identity (UID/GID 999); no volumes.
        "mail": {
            "image": images["mail"],
            "user": "999:999",
            "environment": {
                "GREENMAIL_OPTS": f"-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=bot:{password}@chatmail.test,peer:{password}@chatmail.test"
            },
        },
        # Policy exception: this image's entrypoint performs no demotion and
        # initializes its state under root-owned named volumes; it bind-mounts
        # nothing, so no root-owned path reaches the host filesystem. There is
        # no unprivileged setup path that would not require a root-mode
        # provisioning container of our own.
        "smp": {
            "image": images["smp"],
            "environment": {"ADDR": "smp.lab.test", "WEB_MANUAL": "1"},
            "volumes": ["smp-config:/etc/opt/simplex", "smp-state:/var/opt/simplex"],
            "networks": {"default": {"aliases": ["smp.lab.test"]}},
        },
        "runner": {
            "image": project + ":runner",
            "build": {
                "context": str(directory / "build"),
                "args": {
                    "BASE_IMAGE": images["browser"],
                    "LAB_UID": str(os.getuid()),
                    "LAB_GID": str(os.getgid()),
                },
            },
            "user": f"{os.getuid()}:{os.getgid()}",
            "init": True,
            "volumes": [f"{directory}:/lab"],
            "environment": {
                "SSL_CERT_FILE": "/lab/config/ca.crt",
                "REQUESTS_CA_BUNDLE": "/lab/config/ca.crt",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            "shm_size": "512mb",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        },
    }
    hardware = json.loads((directory / "hardware.json").read_text())
    devices = []
    groups = set()
    for settings in hardware.values():
        for key, value in settings.items():
            if key.endswith("serial_port"):
                path = Path(value).resolve(strict=True)
                if not path.is_char_device():
                    raise LabError(
                        "Serial hardware paths must identify character devices"
                    )
                devices.append(f"{path}:{value}")
                groups.add(str(path.stat().st_gid))
    if devices:
        services["runner"]["devices"] = devices
        services["runner"]["group_add"] = sorted(groups)
    if "meshcore" in hardware or any(
        key.endswith("_ble_address") for key in hardware.get("meshtastic", {})
    ):
        # Docker's default AppArmor label rejects host D-Bus AddMatch calls.
        # This relaxes only the explicitly hardware-enabled test container;
        # no profile is loaded or changed on the host. Capabilities stay dropped.
        services["runner"]["security_opt"].append("apparmor:unconfined")
        services["runner"]["volumes"].append(
            "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro"
        )
    write_json(
        directory / "compose.json",
        {
            "name": project,
            "services": services,
            "networks": {"default": {"internal": True}},
            "volumes": {
                key: {} for key in ("postgres", "xmpp", "smp-config", "smp-state")
            },
        },
    )
