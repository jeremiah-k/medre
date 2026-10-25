"""Bounded Docker orchestration and private, append-only action evidence."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import yaml

from . import LabError
from .budgets import stage_timeout
from .sources import selected_sources, snapshot, write_json
from .stack import configure


def command(
    directory: Path,
    name: str,
    argv: list[str],
    timeout: float = 300,
    *,
    cwd: Path | None = None,
) -> None:
    log = directory / "logs" / f"{name}-{time.time_ns()}.log"
    started = time.monotonic()
    status = "failed"
    try:
        with log.open("wb") as stream:
            result = subprocess.run(
                argv,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                cwd=cwd,
            )
        if result.returncode:
            raise RuntimeError(f"{name} failed; inspect private log")
        status = "passed"
    finally:
        with (directory / "actions.jsonl").open("a") as evidence:
            evidence.write(
                json.dumps(
                    {
                        "action": name,
                        "status": status,
                        "seconds": round(time.monotonic() - started, 3),
                        "log": log.relative_to(directory).as_posix(),
                    }
                )
                + "\n"
            )
    print(f"{name}: passed", flush=True)


def compose(directory: Path, *args: str) -> list[str]:
    return ["docker", "compose", "--file", str(directory / "compose.json"), *args]


def prepare(args: argparse.Namespace) -> None:
    if args.profile is None or args.mmrelay is None:
        raise LabError("prepare requires --profile and --mmrelay")
    sources = selected_sources(args.project, args.profile, args.mmrelay)
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700)
    for name in ("logs", "config", "profiles", "results", "build/sources", "home"):
        (directory / name).mkdir(parents=True, mode=0o700)
    images = json.loads((Path(__file__).parent / "images.json").read_text())
    write_json(directory / "images.json", images)
    write_json(
        directory / "hardware.json",
        json.loads(args.hardware.read_text()) if args.hardware else {},
    )
    write_json(
        directory / "state.json",
        {
            "version": 1,
            "project": "medre-lab-" + uuid4().hex[:12],
            "status": "preparing",
        },
    )
    recorded = {}
    for name, path in sources.items():
        recorded[name] = snapshot(
            path, directory / "build/sources" / name, allow_dirty=args.allow_dirty
        )
    write_json(directory / "sources.json", recorded)
    shutil.copy2(Path(__file__).parent / "Dockerfile", directory / "build/Dockerfile")
    generated = directory / "config/mas-generated.yaml"
    with generated.open("wb") as output:
        subprocess.run(
            ["docker", "run", "--rm", images["mas"], "config", "generate"],
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )
    state = json.loads((directory / "state.json").read_text())
    configure(
        directory, images, state["project"], yaml.safe_load(generated.read_text())
    )
    state["status"] = "prepared"
    write_json(directory / "state.json", state)
    print("Lab prepared; sources and configuration are private and recorded.")


def operate(args: argparse.Namespace) -> None:
    if args.action == "prepare":
        prepare(args)
        return
    directory = args.directory.resolve(strict=True)
    state = json.loads((directory / "state.json").read_text())
    manifest = json.loads((directory / "compose.json").read_text())
    if (
        state.get("version") != 1
        or not re.fullmatch(r"medre-lab-[a-f0-9]{12}", state.get("project", ""))
        or manifest.get("name") != state["project"]
    ):
        raise LabError(
            "The directory does not identify a matching disposable lab project"
        )
    if directory.stat().st_mode & 0o077:
        raise LabError(
            "The lab directory must be accessible only to its owner (mode 0700)"
        )
    if state.get("status") == "destroyed" and args.action != "status":
        raise LabError("This lab was destroyed; prepare a fresh directory")
    if args.action == "build":
        command(directory, "build", compose(directory, "build", "runner"), timeout=1800)
    elif args.action == "up":
        if not (directory / "config/ca.crt").exists():
            command(
                directory,
                "certificates",
                compose(
                    directory, "run", "--rm", "--no-deps", "runner", "certificates"
                ),
            )
        command(
            directory,
            "synapse-volume",
            compose(
                directory,
                "run",
                "--rm",
                "--no-deps",
                "--user",
                "0:0",
                "--entrypoint",
                "chown",
                "synapse",
                "-R",
                "991:991",
                "/data",
            ),
        )
        command(
            directory,
            "services",
            compose(
                directory,
                "up",
                "--detach",
                "postgres",
                "mas",
                "synapse",
                "proxy",
                "xmpp",
                "mail",
                "smp",
            ),
        )
        command(
            directory,
            "readiness",
            compose(directory, "run", "--rm", "runner", "readiness"),
        )
        accounts = json.loads((directory / "config/accounts.json").read_text())
        completed = {
            row["action"]
            for row in map(
                json.loads, (directory / "actions.jsonl").read_text().splitlines()
            )
            if row["status"] == "passed"
        }
        for user in accounts["users"]:
            if f"account-{user}" in completed:
                continue
            command(
                directory,
                f"account-{user}",
                compose(
                    directory,
                    "exec",
                    "--no-TTY",
                    "mas",
                    "mas-cli",
                    "manage",
                    "register-user",
                    "--config",
                    "/config/mas.yaml",
                    "--yes",
                    "--ignore-password-complexity",
                    "--password=" + accounts["password"],
                    user,
                ),
            )
        for user in ("medre", "peer"):
            if f"xmpp-account-{user}" in completed:
                continue
            command(
                directory,
                f"xmpp-account-{user}",
                compose(
                    directory,
                    "exec",
                    "--no-TTY",
                    "xmpp",
                    "prosodyctl",
                    "register",
                    user,
                    "xmpp.lab.test",
                    accounts["password"],
                ),
            )
        command(
            directory,
            "smp-identity",
            compose(
                directory,
                "exec",
                "--no-TTY",
                "smp",
                "cat",
                "/etc/opt/simplex/fingerprint",
            ),
        )
        fingerprint = subprocess.check_output(
            compose(
                directory,
                "exec",
                "--no-TTY",
                "smp",
                "cat",
                "/etc/opt/simplex/fingerprint",
            ),
            timeout=15,
        )
        (directory / "config/smp-fingerprint").write_bytes(fingerprint)
    elif args.action == "test":
        stage_command = compose(directory, "run", "--rm", "runner", args.stage)
        if getattr(args, "meshcore_repaired", False):
            stage_command.append("--meshcore-repaired")
        command(
            directory,
            args.stage,
            stage_command,
            timeout=stage_timeout(
                args.stage, len(json.loads((directory / "sources.json").read_text()))
            ),
        )
        report = directory / "results/qualification.json"
        if args.stage in {"all", "native"} and report.exists():
            print(
                "Qualification: "
                + json.loads(report.read_text())["status"]
                + "; details: results/qualification.json"
            )
    elif args.action == "status":
        subprocess.run(compose(directory, "ps"), check=True, timeout=30)
    elif args.action == "down":
        try:
            command(directory, "service-logs", compose(directory, "logs", "--no-color"))
        finally:
            command(
                directory,
                "cleanup",
                compose(directory, "down", "--volumes", "--remove-orphans"),
            )
            state["status"] = "destroyed"
            write_json(directory / "state.json", state)
