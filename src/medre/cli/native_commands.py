"""Private, offline connection authoring driven entirely by adapter metadata."""

from __future__ import annotations

import getpass
import os
import sys
import warnings
from dataclasses import fields, replace
from pathlib import Path

from medre.adapter_registry import get_adapter_spec
from medre.adapter_spec import AdapterSpec, NativeSetupField
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeAuthoring

from .bridge_commands import _ask, _command_example, _error_detail, _write_new_config
from .exit_codes import EXIT_CONFIG, EXIT_FAILED


def _validate_native_fields(spec: AdapterSpec) -> None:
    """Refuse unsafe or stale provider declarations before accepting input."""
    available = {field.name for field in fields(spec.config.load())}
    prior: dict[str, NativeSetupField] = {}
    for field in spec.native_setup_fields:
        if not isinstance(field, NativeSetupField):
            raise ConfigValidationError("Malformed native setup field declaration")
        if (
            not isinstance(field.name, str)
            or field.name not in available
            or field.name in {"adapter_id", "adapter_kind", "enabled"}
        ):
            raise ConfigValidationError("Invalid native setup field name")
        if field.name in prior:
            raise ConfigValidationError(f"Repeated native setup field {field.name!r}")
        if not isinstance(field.label, str) or not field.label.strip():
            raise ConfigValidationError("Native setup labels must be non-empty strings")
        if any(
            type(value) is not bool
            for value in (field.required, field.secret, field.numeric)
        ):
            raise ConfigValidationError("Native setup flags must be booleans")
        if (
            not isinstance(field.choices, tuple)
            or any(
                not isinstance(choice, str) or not choice.strip()
                for choice in field.choices
            )
            or len(set(field.choices)) != len(field.choices)
            or (field.choices and (field.secret or field.numeric))
        ):
            raise ConfigValidationError(
                "Native setup choices must be unique plain text values"
            )
        if field.secret != (field.name in spec.support_secret_fields):
            raise ConfigValidationError(
                f"Native setup secret declaration for {field.name!r} must match provider redaction metadata"
            )
        condition = field.active_when
        if condition is not None:
            if (
                not isinstance(condition, tuple)
                or len(condition) != 2
                or any(not isinstance(value, str) for value in condition)
                or condition[0] not in prior
                or condition[1] not in prior[condition[0]].choices
            ):
                raise ConfigValidationError(
                    f"Conditional field {field.name!r} must reference a prior choice"
                )
        prior[field.name] = field


def _config_native(args) -> None:
    """Collect provider-declared values, validate, and create a new private YAML."""
    document: dict = {}
    saved = False
    saving = False
    output = Path(args.output).expanduser().absolute()
    try:
        if not sys.stdin.isatty():
            raise ConfigValidationError("Native setup requires an interactive terminal")
        if output.suffix.lower() not in (".yaml", ".yml"):
            raise ConfigValidationError("Output must use .yaml or .yml")
        if os.path.lexists(output):
            raise FileExistsError(f"Output already exists: {output}")
        spec = get_adapter_spec(args.transport)
        if spec is None or not spec.native_setup_fields:
            raise ConfigValidationError(
                f"{args.transport!r} has no provider-declared native setup prompts; "
                "use its native configuration guide"
            )
        _validate_native_fields(spec)
        if args.config:
            source = Path(args.config).expanduser().absolute()
            document = parse_yaml_config(
                source.read_text(encoding="utf-8"), str(source)
            )
        else:
            # No sample instances, default routing, or implicit native permission.
            document = {"adapters": {}, "routes": {}}
        adapters = document.setdefault("adapters", {})
        if not isinstance(adapters, dict):
            raise ConfigValidationError("adapters must be a mapping")
        if any(
            args.adapter_id in group
            for group in adapters.values()
            if isinstance(group, dict)
        ):
            raise ConfigValidationError(
                f"Adapter instance {args.adapter_id!r} already exists"
            )
        print(
            f"New {args.transport} connection {args.adapter_id}. "
            "No SDK, account, or device will be contacted. Ctrl+C cancels.",
            file=sys.stderr,
        )
        values: dict[str, object] = {"adapter_kind": "real", "enabled": True}
        # Retain entered secrets for redaction even if a later prompt fails.
        group = adapters.setdefault(args.transport, {})
        if not isinstance(group, dict):
            raise ConfigValidationError("Adapter transport entries must be mappings")
        group[args.adapter_id] = values
        for field in spec.native_setup_fields:
            if (
                field.active_when
                and values.get(field.active_when[0]) != field.active_when[1]
            ):
                continue
            if field.secret:
                print(
                    f"{field.label} is sensitive: when entered, it is stored "
                    "in the private output file. Never commit this YAML.",
                    file=sys.stderr,
                )
            while True:
                if field.secret:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        try:
                            entered = getpass.getpass(f"{field.label}: ")
                        except getpass.GetPassWarning:
                            raise ConfigValidationError(
                                "Hidden input is unavailable; use a terminal with echo control"
                            ) from None
                else:
                    entered = _ask(
                        f"{field.label}"
                        + (f" ({', '.join(field.choices)})" if field.choices else "")
                    )
                if not entered:
                    if field.required:
                        print(f"{field.label} is required; try again.", file=sys.stderr)
                        continue
                    break
                if field.choices and entered not in field.choices:
                    print("Choose one of: " + ", ".join(field.choices), file=sys.stderr)
                    continue
                if field.numeric:
                    try:
                        value: object = int(entered)
                    except ValueError:
                        print(f"{field.label} must be an integer.", file=sys.stderr)
                        continue
                else:
                    value = entered
                values[field.name] = value
                break
        yaml_text = dump_yaml_config(document)
        # Authoritative typed loader, runtime route plan, and provider-owned
        # pure preflight; never instantiate an adapter or import its SDK.
        BridgeAuthoring(yaml_text, paths=replace(resolve(), config_file=output))
        from .bridge_commands import _confirm_save

        if not _confirm_save(output):
            print("Cancelled; no configuration written.", file=sys.stderr)
            return
        saving = True
        _write_new_config(output, yaml_text)
        saved = True
        print(f"Saved {output}; no native runtime started.", file=sys.stderr)
        print("Next steps:", file=sys.stderr)
        print(
            "  "
            + _command_example("medre", "config", "check", "--config", str(output)),
            file=sys.stderr,
        )
        count = sum(len(group) for group in adapters.values())
        if count < 2:
            print(
                "  Add a second instance: medre config native --transport PROVIDER "
                "--adapter-id NEW_ID --config INPUT --output NEXT.yaml",
                file=sys.stderr,
            )
        else:
            print(
                "  "
                + _command_example(
                    "medre",
                    "config",
                    "bridge",
                    "--config",
                    str(output),
                    "--output",
                    str(output.with_name("bridge.yaml")),
                ),
                file=sys.stderr,
            )
    except (KeyboardInterrupt, EOFError):
        print(
            (
                f"Interrupted while saving; inspect {output}."
                if saving and not saved
                else "Cancelled; no configuration written."
            ),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_FAILED) from None
    except Exception as exc:
        print(
            f"Native configuration error: {_error_detail(exc, document)}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG) from None
