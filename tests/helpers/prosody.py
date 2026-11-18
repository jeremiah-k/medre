"""Credentials and endpoint details for disposable Prosody fixtures."""

from __future__ import annotations

import subprocess
from pathlib import Path

PROSODY_DOMAIN = "xmpp.lab.test"
_MUC_DOMAIN = "rooms.xmpp.lab.test"


class ProsodyEnvironment:
    """Holds connection details and per-run private accounts for Prosody.

    Account passwords are generated per session with ``secrets`` and live
    only in this object; they are never written to config snapshots,
    artifacts, or logs.
    """

    def __init__(
        self,
        container_name: str,
        host: str,
        port: int,
        config_dir: Path,
        ca_path: Path,
        image: str,
    ) -> None:
        self.container_name = container_name
        self.host = host
        self.port = port
        self.config_dir = config_dir
        self.ca_path = ca_path
        self.image = image
        self.domain = PROSODY_DOMAIN
        self.muc_domain = _MUC_DOMAIN
        self._accounts: dict[str, str] = {}

    def register_account(self, localpart: str, password: str) -> None:
        """Record one account provisioned through prosodyctl."""
        self._accounts[localpart] = password

    def jid(self, localpart: str) -> str:
        return f"{localpart}@{self.domain}"

    def password(self, localpart: str) -> str:
        return self._accounts[localpart]

    def room_jid(self, room: str) -> str:
        return f"{room}@{self.muc_domain}"


def generate_prosody_certs(config_dir: Path) -> Path:
    """Create a throwaway CA and server certificate for the fixture."""
    san = (
        "subjectAltName=DNS:xmpp.lab.test,DNS:rooms.xmpp.lab.test,"
        "DNS:localhost,IP:127.0.0.1"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "3",
            "-nodes",
            "-keyout",
            str(config_dir / "ca.key"),
            "-out",
            str(config_dir / "ca.crt"),
            "-subj",
            "/CN=medre-prosody-fixture-ca",
            # OpenSSL verification refuses a CA without an explicit key usage.
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(config_dir / "server.key"),
            "-out",
            str(config_dir / "server.csr"),
            "-subj",
            "/CN=xmpp.lab.test",
            "-addext",
            san,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    ext_file = config_dir / "server.ext"
    ext_file.write_text(san + "\n")
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(config_dir / "server.csr"),
            "-CA",
            str(config_dir / "ca.crt"),
            "-CAkey",
            str(config_dir / "ca.key"),
            "-CAcreateserial",
            "-days",
            "3",
            "-sha256",
            "-out",
            str(config_dir / "server.crt"),
            "-extfile",
            str(ext_file),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    # The container runs Prosody as its own unprivileged user; the key must
    # be readable through the read-only bind mount. This is a per-run
    # throwaway key that is deleted with the config directory.
    config_dir.joinpath("server.key").chmod(0o644)
    return config_dir / "ca.crt"
