"""Guided, offline configuration authoring; runtime startup is a separate action."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from medre.adapter_registry import get_adapter_spec, iter_adapter_specs
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.env import _is_secret_field
from medre.config.errors import ConfigValidationError
from medre.config.paths import resolve
from medre.config.routes import RouteDirectionality
from medre.core.observability.sanitization import (
    BROAD_SECRET_TOKENS,
    REDACTED_TOKEN,
    sanitize_error,
)
from medre.runtime.bridge_setup import BridgeAuthoring, BridgeRequest, precheck_bridge
from medre.runtime.errors import RuntimeConfigError

from .exit_codes import EXIT_CONFIG, EXIT_FAILED
from .json import to_json
from .route_commands import _render_route_plan

# Display-only labels for adapter kinds; saved YAML keeps provider literals.
_KIND_LABELS = {"fake": "offline sample"}


def _ask(prompt: str, *, default: str | None = None) -> str:
    """Keep prompts off stdout so JSON output remains machine-readable."""
    suffix = f" [{default}]" if default is not None else ""
    print(f"{prompt}{suffix}: ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if not answer:
        raise EOFError
    return answer.rstrip("\r\n") or (default if default is not None else "")


def _choose(
    value: str | None,
    choices: list[str],
    name: str,
    interactive: bool,
    *,
    default: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Accept stable IDs for automation and numbered choices at a terminal."""
    if value is not None:
        if value not in choices:
            raise ConfigValidationError(f"Invalid {name}; choose from {choices}")
        return value
    if not interactive:
        raise ConfigValidationError(
            f"--{name} is required without an interactive terminal"
        )
    print(f"Choose {name}:", file=sys.stderr)
    # A literal numeric ID must remain selectable without shadowing menu rows.
    numbered_ids = any(choice.isascii() and choice.isdecimal() for choice in choices)
    for index, choice in enumerate(choices, start=1):
        explanation = f" — {labels[choice]}" if labels and choice in labels else ""
        number = f"#{index}" if numbered_ids else str(index)
        print(f"  {number}. {choice}{explanation}", file=sys.stderr)
    while True:
        answer = _ask(name.capitalize(), default=default)
        if answer in choices:
            return answer
        number = answer.removeprefix("#")
        if (
            number.isascii()
            and number.isdecimal()
            and len(number) <= len(str(len(choices)))
        ):
            index = int(number)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        print(
            f"Invalid {name} {answer[:80]!r}; enter a number from "
            f"{'#' if numbered_ids else ''}1 to {'#' if numbered_ids else ''}{len(choices)} "
            "or an exact value from the list.",
            file=sys.stderr,
        )


def _command_example(*args: str) -> str:
    """Render a copyable platform-native command even for paths with spaces."""
    if os.name == "nt":
        # PowerShell single quotes preserve &, $, backticks and embedded quotes.
        def literal(arg: str) -> str:
            if re.fullmatch(r"[A-Za-z0-9_./:\\-]+", arg):
                return arg
            return "'" + arg.replace("'", "''") + "'"

        command = " ".join(literal(arg) for arg in args)
        return "& " + command if args and literal(args[0]).startswith("'") else command
    return shlex.join(args)


def _confirm_save(output: Path) -> bool:
    while True:
        answer = (
            _ask(f"Save new configuration to {output}? (yes/no)", default="no")
            .strip()
            .lower()
        )
        if answer in ("yes", "y"):
            return True
        if answer in ("no", "n"):
            return False
        print("Please answer yes or no (y/n).", file=sys.stderr)


def _write_new_config(path: Path, text: str) -> None:
    """Create a private file exclusively; never follow or overwrite an existing path.

    Exclusive creation works without hard-link support. On failure, remove only
    our own incomplete file, retaining a concurrent replacement if one exists.
    POSIX permissions are owner-only; Windows inherits the directory's ACL.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = None
    stream = None
    try:
        identity = os.fstat(fd)
        stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        with stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # fstat/fdopen can fail before a stream takes ownership of the fd.
        # Obtain identity while it is still open, then close before unlinking
        # (required on Windows). Never delete a path without an identity check.
        if stream is None:
            try:
                if identity is None:
                    identity = os.stat(fd)
            finally:
                os.close(fd)
        try:
            current = path.stat(follow_symlinks=False)
            if identity and (current.st_dev, current.st_ino) == (
                identity.st_dev,
                identity.st_ino,
            ):
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
    saving = False
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
                "No input configuration given: starting from installed providers' "
                "offline samples.",
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

        document = parse_yaml_config(
            text, str(report.config_path) if args.config is not None else "<sample>"
        )
        authoring = BridgeAuthoring(text, paths=paths)
        config = authoring.config
        if interactive:
            print(
                "Press Ctrl+C at any prompt to cancel without saving.", file=sys.stderr
            )
        adapters = {
            aid: (transport, rtc)
            for transport, aid, rtc in config.adapters.all_configs()
        }
        choices = sorted(aid for aid, (_, rtc) in adapters.items() if rtc.enabled)
        if len(choices) < 2:
            raise ConfigValidationError(
                "Two enabled adapter instances are required. Install a provider with "
                "offline sample support, or supply --config with your provider-owned "
                "connection settings. See docs/ops/adapter-installation.md."
            )
        print("Adapter instances:", file=sys.stderr)
        for aid in choices:
            transport, rtc = adapters[aid]
            kind = _KIND_LABELS.get(rtc.adapter_kind, rtc.adapter_kind)
            print(f"  {aid}: {transport} ({kind})", file=sys.stderr)
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
            authoring = BridgeAuthoring(text, paths=paths)
        direction = _choose(
            args.direction,
            ["bidirectional", "source_to_dest", "dest_to_source"],
            "direction",
            interactive,
            default="bidirectional",
            labels={
                "bidirectional": "messages flow both ways",
                "source_to_dest": f"{source} → {dest} only",
                "dest_to_source": f"{dest} → {source} only",
            },
        )
        contexts = args.contexts
        if contexts is None:
            if not interactive:
                raise ConfigValidationError(
                    "At least one --map SOURCE_CONTEXT DEST_CONTEXT is required"
                )
            # Structural failures surface before the operator types any
            # context. Only context-specific failures can reject a pair in
            # the loop below, so validation there stays retryable and never
            # aborts the session.
            precheck_bridge(
                authoring.config,
                BridgeRequest(
                    source,
                    dest,
                    (),
                    RouteDirectionality(direction),
                    args.route_id,
                    replace_route=args.replace_route,
                ),
                require_contexts=False,
            )
            print(
                "A context is a platform's conversation address (room, channel, or topic). "
                "Use exact provider identifiers in source/destination order; "
                "the selected direction controls message flow. "
                "Leave the source blank when finished.",
                file=sys.stderr,
            )

            def context_prompt(aid: str) -> str:
                spec = get_adapter_spec(adapters[aid][0])
                example = None
                if spec is not None and spec.sample_config is not None:
                    # A broken sample callback must not block authoring over a
                    # cosmetic hint; provider diagnostics already surfaced it.
                    try:
                        example = spec.sample_config.load()().channel
                    except Exception:
                        example = None
                return (
                    f"{aid} context (e.g. '{example}')" if example else f"{aid} context"
                )

            source_prompt = context_prompt(source)
            dest_prompt = context_prompt(dest)
            contexts: list[tuple[str, str]] = []
            while True:
                context = _ask(source_prompt)
                if not context:
                    if contexts:
                        break
                    print(
                        "Enter at least one context pair before finishing.",
                        file=sys.stderr,
                    )
                    continue
                target = _ask(dest_prompt)
                if any(
                    not value or value.strip() != value for value in (context, target)
                ):
                    print(
                        "Contexts cannot be empty or begin or end with whitespace.",
                        file=sys.stderr,
                    )
                    continue
                # Each accumulated pair is validated through the same draft
                # the command will save, so provider restrictions (allowlists,
                # duplicates, already-connected pairs) are rejected at entry.
                attempt = BridgeRequest(
                    source,
                    dest,
                    tuple(contexts) + ((context, target),),
                    RouteDirectionality(direction),
                    args.route_id,
                    replace_route=args.replace_route,
                )
                try:
                    authoring.draft(attempt)
                except (ConfigValidationError, RuntimeConfigError) as exc:
                    # Core validation and provider-owned restrictions report
                    # through different hierarchies; both are pair-specific.
                    print(
                        f"Context rejected: {_error_detail(exc, document)}",
                        file=sys.stderr,
                    )
                    continue
                contexts.append((context, target))
                print(
                    f"Added context pair {len(contexts)}; add another or press Enter to continue.",
                    file=sys.stderr,
                )
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
        draft = authoring.draft(
            BridgeRequest(
                source,
                dest,
                tuple(tuple(pair) for pair in contexts),
                RouteDirectionality(direction),
                args.route_id,
                source_label,
                dest_label,
                args.replace_route,
            ),
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
            if direction == "bidirectional" and interactive:
                print(
                    "Note: a two-way bridge forms a route cycle by design. "
                    "Topology warnings below are not proof of duplicate sends; "
                    "native echo prevention still requires testing.",
                    file=sys.stderr,
                )
            _render_route_plan(draft.plan)
        if args.preview:
            print("Preview only; no configuration written.", file=sys.stderr)
            return
        if interactive and not _confirm_save(output):
            print("Cancelled; no configuration written.", file=sys.stderr)
            return
        saving = True
        _write_new_config(output, draft.yaml)
        saved = True
        print(f"Saved {output}. No runtime started.", file=sys.stderr)
        print("Next steps:", file=sys.stderr)
        for command in (("config", "check"), ("routes", "plan")):
            print(
                "  " + _command_example("medre", *command, "--config", str(output)),
                file=sys.stderr,
            )
        if all(
            rtc.adapter_kind == "fake"
            for _, _, rtc in authoring.config.adapters.all_configs()
            if rtc.enabled
        ):
            authored = next(
                route for route in draft.plan.routes if route.route_id == args.route_id
            )
            seen_sources: set[str] = set()
            for leg in authored.legs:
                if leg.source_adapter_id in seen_sources:
                    continue
                seen_sources.add(leg.source_adapter_id)
                if leg.source_channel is not None:
                    context_args = (
                        (f"--source-context={leg.source_channel}",)
                        if leg.source_channel.startswith("-")
                        else ("--source-context", leg.source_channel)
                    )
                    print(
                        "  "
                        + _command_example(
                            "medre",
                            "smoke",
                            "--config",
                            str(output),
                            "--source-adapter",
                            leg.source_adapter_id,
                            *context_args,
                        ),
                        file=sys.stderr,
                    )
            print(
                "  Run these if the provider supports context-aware fake events. "
                "Smoke proves only the selected sample contexts; repeat for "
                "other mappings and qualify native delivery separately.",
                file=sys.stderr,
            )
        else:
            print(
                "  "
                + _command_example(
                    "medre", "adapters", "check", "--config", str(output)
                ),
                file=sys.stderr,
            )
            print(
                "  Fake smoke requires every enabled adapter to be an offline sample; "
                "a plan does not prove live delivery.",
                file=sys.stderr,
            )
    except (EOFError, KeyboardInterrupt):
        print(
            (
                "Interrupted after saving configuration; no runtime started."
                if saved
                else (
                    f"Interrupted while saving; check {output} for a completed "
                    "configuration. No runtime started."
                    if saving
                    else "Cancelled; no configuration written."
                )
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
