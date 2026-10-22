"""Container-only lab entry point; each stage retains a bounded result record."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from .sources import write_json
from .budgets import PROVIDER_CONTRACT_SECONDS

ROOT = Path("/lab")


def certificates() -> None:
    config = ROOT / "config"
    if (config / "ca.crt").exists():
        raise ValueError("Existing lab identity must not be rotated implicitly")
    commands = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
            "-days",
            "90",
            "-subj",
            "/CN=MEDRE disposable lab CA",
        ],
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
            "-subj",
            "/CN=matrix.lab.test",
        ],
        [
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "server.crt",
            "-days",
            "90",
            "-extfile",
            "server.ext",
        ],
    ]
    (config / "server.ext").write_text(
        "subjectAltName=DNS:matrix.lab.test,DNS:auth.lab.test,DNS:xmpp.lab.test,DNS:rooms.xmpp.lab.test\nextendedKeyUsage=serverAuth\n"
    )
    for argv in commands:
        subprocess.run(["openssl", *argv], cwd=config, check=True, timeout=30)
    # Service images use different UIDs. Only disposable server identity is
    # readable there; the containing host lab directory remains mode 0700.
    for name in (
        "server.key",
        "server.crt",
        "ca.crt",
        "prosody.cfg.lua",
        "mas.yaml",
        "synapse.yaml",
        "Caddyfile",
    ):
        (config / name).chmod(0o644)
    config.chmod(0o755)


async def readiness() -> None:
    import aiohttp

    deadline = time.monotonic() + 120
    urls = [
        "https://matrix.lab.test/_matrix/client/versions",
        "https://auth.lab.test/.well-known/openid-configuration",
    ]
    async with aiohttp.ClientSession() as session:
        pending = set(urls)
        while pending:
            for url in list(pending):
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as response:
                        if response.status == 200:
                            await response.json()
                            pending.remove(url)
                except (aiohttp.ClientError, TimeoutError):
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError("TLS-verified Matrix/MAS readiness timed out")
            if pending:
                await asyncio.sleep(0.5)
    for host, port in (("xmpp.lab.test", 5222), ("mail", 3025), ("mail", 3143)):
        while True:
            try:
                with socket.create_connection((host, port), timeout=2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Service readiness timed out: {host}") from None
                await asyncio.sleep(0.5)


def contracts() -> None:
    """Use provider-owned tests; keep incompatible SDK versions in separate venvs."""
    from .control import command

    sources = json.loads((ROOT / "sources.json").read_text())
    failures = []
    for name in sources:
        python = "/opt/mmrelay/bin/python" if name == "mmrelay" else sys.executable
        junit = ROOT / "results" / f"{name}-contracts.xml"
        plugins = [
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "pytest_timeout",
            "-p",
            "pytest_cov.plugin",
        ]
        if name == "mmrelay":
            plugins += ["-p", "pytest_env.plugin"]
        argv = [
            python,
            "-m",
            "pytest",
            *plugins,
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--timeout=45",
            "--junitxml",
            str(junit),
            "-q",
        ]
        directory = Path("/sources") / name
        if name == "medre":
            patterns = (
                "test_*adapter_campaign*.py",
                "test_campaign_presentation.py",
                "test_encrypted_campaign.py",
                "test_cross_transport*.py",
                "test_logging*.py",
                "test_runtime_observability.py",
                "test_docker_campaign_lab.py",
            )
        elif name == "mmrelay":
            patterns = (
                "test_matrix_oauth*.py",
                "test_matrix_cross_signing.py",
                "test_cli_oauth.py",
                "test_cli_utils.py",
                "test_prefix_customization.py",
                "test_log_utils.py",
                "test_matrix_utils_relay.py",
                "test_network_reliability.py",
                "test_main_startup_rollback.py",
            )
        else:
            patterns = ("test_*.py",)
        selected = sorted(
            {
                str(path)
                for pattern in patterns
                for path in (directory / "tests").glob(pattern)
            }
        )
        if not selected:
            raise ValueError(f"No contract tests found for {name}")
        argv += selected
        # The adapter/core default marker expressions are part of their contract;
        # clearing addopts must not accidentally admit hardware/service tiers.
        if name != "mmrelay":
            argv += [
                "-m",
                "not live and not docker and not hardware and not local_integration and not soak",
            ]
        try:
            command(
                ROOT,
                f"contracts-{name}",
                argv,
                timeout=PROVIDER_CONTRACT_SECONDS,
                cwd=Path("/sources") / name,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            failures.append(name)
    if failures:
        raise RuntimeError("Provider contract suites failed: " + ", ".join(failures))


def main() -> int:
    if not __debug__:
        print("Qualification requires enabled assertions")
        return 1
    os.umask(0o077)
    stage = sys.argv[1]
    result = {"stage": stage, "status": "failed", "started_at": time.time()}
    path = ROOT / "results" / f"{stage}-{time.time_ns()}.json"
    try:
        if stage == "certificates":
            certificates()
        elif stage == "readiness":
            asyncio.run(readiness())
        elif stage == "contracts":
            contracts()
        else:
            from .scenarios import run

            asyncio.run(run(stage))
        result["status"] = "passed"
        if stage in {"all", "native"}:
            result["status"] = json.loads(
                (ROOT / "results/qualification.json").read_text()
            )["status"]
        return 0
    except BaseException as exc:
        # Tracebacks stay in private action logs, never the public summary.
        import traceback

        traceback.print_exc()
        result["error_type"] = type(exc).__name__
        return 1
    finally:
        result["finished_at"] = time.time()
        write_json(path, result)


if __name__ == "__main__":
    raise SystemExit(main())
