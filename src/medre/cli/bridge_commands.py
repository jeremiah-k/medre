"""Guided, offline configuration authoring; runtime startup is a separate action."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from medre.adapter_registry import get_adapter_spec, iter_adapter_specs
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.env import _is_secret_field, apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre.config.routes import RouteDirectionality
from medre.core.observability.sanitization import (
    BROAD_SECRET_TOKENS,
    REDACTED_TOKEN,
    sanitize_error,
)
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge

from .exit_codes import EXIT_CONFIG, EXIT_FAILED
from .json import to_json
from .route_commands import _render_route_plan


def _ask(prompt: str, *, default: str | None = None) -> str:
    """Keep prompts off stdout so JSON output remains machine-readable."""
    suffix = f" [{default}]" if default is not None else ""
    print(f"{prompt}{suffix}: ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if not answer:
        raise EOFError
    return answer.rstrip("\r\n") or (default if default is not None else "")


def _choose(value: str | None, choices: list[str], name: str, interactive: bool) -> str:
    if value is not None:
        if value not in choices:
            raise ConfigValidationError(f"Invalid {name}; choose from {choices}")
        return value
    if not interactive:
        raise ConfigValidationError(
            f"--{name} is required without an interactive terminal"
        )
    while True:
        answer = _ask(name.capitalize())
        if answer in choices:
            return answer
        print(f"Choose from {choices}.", file=sys.stderr)


def _write_new_config(path: Path, text: str) -> None:
    """Create a private file exclusively; never follow or overwrite an existing path.

    Exclusive creation works without hard-link support. On failure, remove only
    our own incomplete file, retaining a concurrent replacement if one exists.
    POSIX permissions are owner-only; Windows inherits the directory's ACL.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass
        raise


def _error_detail(exc: Exception, document: dict) -> str:
    """Redact known values even when a provider quotes an unlabelled secret."""
    declared = {
        field.lower()
        for spec in iter_adapter_specs()
        for field in spec.support_secret_fields
    }

    def sensitive(name: str) -> bool:
        return (
            name.lower() in declared
            or _is_secret_field(name)
            or bool(set(re.split(r"[^a-z0-9]+", name.lower())) & BROAD_SECRET_TOKENS)
        )

    secrets: set[str] = set()

    def collect(value: object, secret: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, secret or sensitive(str(key)))
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, secret)
        elif (
            secret
            and isinstance(value, (str, int, float))
            and not isinstance(value, bool)
        ):
            if str(value):
                secrets.add(str(value))
                if isinstance(value, str):
                    secrets.update(
                        (
                            repr(value),
                            json.dumps(value),
                            json.dumps(value, ensure_ascii=False),
                        )
                    )

    collect(document)
    for name, value in os.environ.items():
        if name.startswith("MEDRE_") and sensitive(name.rsplit("__", 1)[-1]):
            collect(value, True)
    detail = str(exc)
    for value in sorted(secrets, key=len, reverse=True):
        # Short values (including PINs) can be ordinary word fragments.
        # Redact complete tokens and quoted forms without shredding prose.
        if len(value) < 8:
            detail = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", REDACTED_TOKEN, detail)
        else:
            detail = detail.replace(value, REDACTED_TOKEN)
    return sanitize_error(detail)


def _config_bridge(args: argparse.Namespace) -> None:
    """Select endpoints and contexts, preview, then save a validated draft."""
    interactive = sys.stdin.isatty()
    document: dict = {}
    saved = False
    try:
        output = Path(args.output).expanduser().absolute()
        if output.suffix.lower() not in (".yaml", ".yml"):
            raise ConfigValidationError("Output must use .yaml or .yml")
        if not args.preview and os.path.lexists(output):
            raise FileExistsError(f"Output already exists: {output}; choose a new path")
        paths = replace(resolve(), config_file=output)
        if args.config is None:
            from medre.config.sample import generate_sample_config

            document = parse_yaml_config(generate_sample_config())
            document["routes"] = {}
            text = dump_yaml_config(document)
            print(
                "Starting with installed providers' offline fake samples.",
                file=sys.stderr,
            )
        else:
            from medre.adapter_installation import inspect_adapter_installation

            report = inspect_adapter_installation(args.config)
            broken = {item.transport for item in report.broken}
            unavailable = sorted(set(report.missing) | (set(report.required) & broken))
            if unavailable:
                raise ConfigValidationError(
                    f"Unavailable providers: {unavailable}. Run medre adapters check "
                    "--config with the input file; install or repair their distributions."
                )
            text = report.config_path.read_text(encoding="utf-8")

        document = parse_yaml_config(text)
        config = apply_env_overrides(parse_runtime_config(document, paths), paths)
        adapters = {
            aid: (transport, rtc)
            for transport, aid, rtc in config.adapters.all_configs()
        }
        choices = sorted(aid for aid, (_, rtc) in adapters.items() if rtc.enabled)
        if len(choices) < 2:
            raise ConfigValidationError(
                "Two enabled adapter instances are required. Install a provider with "
                "fake sample support, or supply --config with your provider-owned "
                "connection settings. See docs/ops/adapter-installation.md."
            )
        print("Adapter instances:", file=sys.stderr)
        for aid in choices:
            transport, rtc = adapters[aid]
            print(f"  {aid}: {transport} ({rtc.adapter_kind})", file=sys.stderr)
        source = _choose(args.source, choices, "source", interactive)
        dest = _choose(
            args.dest, [aid for aid in choices if aid != source], "dest", interactive
        )
        if args.config is None:
            # A first bridge needs only its selected sample connections.
            # Existing user configurations retain their complete inventory.
            selected = {source, dest}
            groups = {
                transport: {
                    aid: values for aid, values in group.items() if aid in selected
                }
                for transport, group in document["adapters"].items()
            }
            document["adapters"] = {
                transport: group for transport, group in groups.items() if group
            }
            text = dump_yaml_config(document)
        direction = args.direction
        if direction is None:
            if not interactive:
                raise ConfigValidationError(
                    "--direction is required without an interactive terminal"
                )
            while direction not in {item.value for item in RouteDirectionality}:
                direction = _ask(
                    "Direction (source_to_dest, dest_to_source, bidirectional)",
                    default="bidirectional",
                )
        contexts = args.contexts
        if contexts is None:
            if not interactive:
                raise ConfigValidationError(
                    "At least one --map SOURCE_CONTEXT DEST_CONTEXT is required"
                )
            print(
                "A context is a platform's conversation address (room, channel, or topic). "
                "Use exact provider identifiers. Each mapping bounds both directions; "
                "leave the source blank when finished.",
                file=sys.stderr,
            )
            contexts = []
            while context := _ask(f"{source} context"):
                contexts.append((context, _ask(f"{dest} context")))
        source_label = args.source_label
        dest_label = args.dest_label
        if interactive:
            if source_label is None:
                source_label = (
                    _ask(
                        f"Origin label for messages from {source} (blank keeps adapter default)"
                    )
                    or None
                )
            if dest_label is None:
                dest_label = (
                    _ask(
                        f"Origin label for messages from {dest} (blank keeps adapter default)"
                    )
                    or None
                )
        draft = draft_bridge(
            text,
            BridgeRequest(
                source,
                dest,
                tuple(tuple(pair) for pair in contexts),
                RouteDirectionality(direction),
                args.route_id,
                source_label,
                dest_label,
            ),
            paths=paths,
        )
        checked_transports: set[str] = set()
        for aid, (transport, rtc) in adapters.items():
            if args.config is None and aid not in (source, dest):
                continue
            spec = get_adapter_spec(transport)
            if (
                rtc.enabled
                and rtc.adapter_kind == "real"
                and spec
                and spec.distribution
            ):
                if transport in checked_transports:
                    continue
                checked_transports.add(transport)
                try:
                    version(spec.distribution)
                except PackageNotFoundError:
                    print(
                        f"Prerequisite missing: SDK distribution {spec.distribution!r} "
                        f"for {transport}. Install the provider's runtime dependencies.",
                        file=sys.stderr,
                    )
        print(
            "Offline preview includes current environment overrides; their values are "
            "not copied into YAML. No accounts or devices have been contacted. "
            "Only message.created/message.text events are enabled by the new route.",
            file=sys.stderr,
        )
        if args.json:
            print(to_json(draft.plan))
        else:
            _render_route_plan(draft.plan)
        if args.preview:
            print("Preview only; no configuration written.", file=sys.stderr)
            return
        if (
            interactive
            and _ask(
                f"Save new configuration to {output}? (yes/no)", default="no"
            ).lower()
            != "yes"
        ):
            print("Cancelled; no configuration written.", file=sys.stderr)
            return
        _write_new_config(output, draft.yaml)
        saved = True
        print(f"Saved {output}. No runtime started.", file=sys.stderr)
        print(
            "Next: medre config check --config PATH; medre routes plan --config PATH. "
            "For fake smoke, select --source-adapter and --source-context from the plan. "
            "Native prerequisites and delivery need separate qualification.",
            file=sys.stderr,
        )
    except (EOFError, KeyboardInterrupt):
        print(
            (
                "Interrupted after saving configuration; no runtime started."
                if saved
                else "Cancelled; no configuration written."
            ),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_FAILED) from None
    except Exception as exc:
        print(
            f"Bridge configuration error: {_error_detail(exc, document)}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG) from None
