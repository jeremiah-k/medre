"""Offline, redacted operator support bundle writer.

Produces a single ZIP artifact (``medre-support-bundle.zip`` by default)
containing JSON/YAML members that an operator can attach to a support
issue. The bundle is **observational only**:

- No adapter SDK is imported (no nio / meshtastic / meshcore / lxmf).
- No adapter is started, no network or hardware I/O is performed.
- All secret-named fields are redacted to ``***REDACTED***``.
- Error messages are routed through
  :func:`~medre.core.observability.sanitization.sanitize_error` so token
  substrings never leak.

The bundle is built from configuration + route plan only; it does not
reuse :func:`medre.runtime.evidence.collect_evidence_bundle`, which
builds a runtime and would violate the offline guarantee.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import platform
import sys
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import msgspec
import yaml

from medre.config._yaml import parse_yaml_config
from medre.config.errors import ConfigError, ConfigValidationError
from medre.config.loader import ConfigSource, find_config, load_config
from medre.core.observability.sanitization import sanitize_error
from medre.runtime.route_plan import build_route_plan

__all__ = ["create_support_bundle", "BUNDLE_SCHEMA_VERSION"]

BUNDLE_SCHEMA_VERSION: int = 1
"""Manifest ``bundle_schema_version``. Frozen at 1 during pre-release."""

_DEFAULT_OUTPUT_NAME = "medre-support-bundle.zip"
_REDACTED = "***REDACTED***"


# ---------------------------------------------------------------------------
# Typed bundle members (msgspec.Struct)
# ---------------------------------------------------------------------------
#
# Structured bundle members use ``msgspec.Struct`` per project convention
# (see :mod:`medre.core.evidence.bundle`). Frozen members are immutable
# snapshots; :class:`ConfigCheckMember` is mutable because its fields are
# set incrementally during bundle creation.
#
# JSON output identity is preserved: ``msgspec.to_builtins`` on a Struct
# produces the same dict as the old manual construction, and
# ``sort_keys=True`` in :func:`_json_bytes` canonicalises ordering.


class ManifestMember(msgspec.Struct, frozen=True):
    """``manifest.json`` — bundle identity and build context."""

    bundle_schema_version: int
    created_at: str
    command: str
    medre_version: str
    platform: dict[str, str]
    redaction_policy: str


class EnvironmentMember(msgspec.Struct, frozen=True):
    """``environment.json`` — Python/platform/runtime version snapshot."""

    python_version: str
    platform: str
    machine: str
    medre_version: str


class ConfigSourceMember(msgspec.Struct, frozen=True):
    """``config_source.json`` — where the config was discovered."""

    source: str | None
    path: str | None
    env_overrides_applied: bool


class ConfigCheckMember(msgspec.Struct):
    """``config_check.json`` — config load/validation outcome.

    Not frozen: fields are set incrementally as config discovery, load,
    and validation proceed within :func:`create_support_bundle`.
    """

    success: bool = False
    error: str | None = None
    error_section_path: str | None = None


class SchemaEntry(msgspec.Struct, frozen=True):
    """One schema-file metadata entry within :class:`SchemasMember`.

    ``$id`` / ``$schema`` are JSON-Schema keywords (not valid Python
    identifiers), so the Python field names use ``id`` / ``schema`` with
    ``msgspec.field(name=...)`` aliases for serialisation.
    """

    present: bool
    path: str | None = None
    id: str | None = msgspec.field(name="$id", default=None)
    schema: str | None = msgspec.field(name="$schema", default=None)


class SchemasMember(msgspec.Struct, frozen=True):
    """``schemas.json`` — schema-file presence and metadata."""

    runtime_config_schema: SchemaEntry
    adapter_config_schema: SchemaEntry
    routing_config_schema: SchemaEntry
    evidence_bundle_schema: SchemaEntry
    validate_example_configs_script_present: bool


# Substring tokens (case-insensitive) for secret-key detection. Over-broad
# on purpose: redacting a benign field is safe, leaking a secret is not.
# Covers the union of the audit's five redaction surfaces plus the
# task-specified token list.
_SECRET_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "password",
        "credential",
        "auth",
        "key",
        "private",
        "identity",
        "pin",
        "access_token",
        "refresh_token",
        "client_secret",
        "bearer",
    }
)


def _is_secret_key(key: Any) -> bool:
    """Return True if *key* looks like a secret-named field."""
    if not isinstance(key, str):
        return False
    lc = key.lower()
    return any(tok in lc for tok in _SECRET_KEY_TOKENS)


def _redact(obj: Any) -> Any:
    """Recursively replace secret-named values with ``***REDACTED***``.

    Preserves structure (keys are kept) so the redacted config remains
    useful for debugging. Non-dict/list values pass through unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _is_secret_key(k) else _redact(v)) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Config env-override detection
# ---------------------------------------------------------------------------
#
# The bundle surfaces ``env_overrides_applied`` as a boolean so operators can
# tell whether the loaded config differs from the on-disk YAML without
# exposing env-var *values*. Discovery vars (``MEDRE_CONFIG``, ``MEDRE_HOME``)
# are excluded: they pick a file, they do not override field values. Unknown
# ``MEDRE_*`` vars are excluded too — only the documented override surfaces
# count. See the operator-support-bundle-audit (F-006) for rationale.
_CONFIG_OVERRIDE_PREFIXES: tuple[str, ...] = (
    "MEDRE_ADAPTER__",
    "MEDRE_ROUTE__",
    "MEDRE_RETRY__",
)
_CONFIG_OVERRIDE_EXACT: frozenset[str] = frozenset(
    {
        "MEDRE_DB_PATH",
        "MEDRE_LOG_LEVEL",
        "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES",
        "MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS",
        "MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
        "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS",
    }
)


def _has_config_env_overrides(environ: Mapping[str, str] = os.environ) -> bool:
    """Return True if *environ* contains any config-field override var.

    Prefix-based overrides (``MEDRE_ADAPTER__``, ``MEDRE_ROUTE__``,
    ``MEDRE_RETRY__``) and the exact-name override vars are detected.
    Discovery-only vars (``MEDRE_CONFIG``, ``MEDRE_HOME``) and unknown
    ``MEDRE_*`` vars are not overrides and return False.
    """
    for key in environ:
        if any(key.startswith(prefix) for prefix in _CONFIG_OVERRIDE_PREFIXES):
            return True
        if key in _CONFIG_OVERRIDE_EXACT:
            return True
    return False


# ---------------------------------------------------------------------------
# Schema metadata (schemas.json member)
# ---------------------------------------------------------------------------
# Resolved from the package root so the bundle reports schema-file presence
# without shipping the schema contents (keeps the bundle small and avoids
# any chance of leaking example data embedded in schemas).
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCHEMAS_DIR = _PACKAGE_ROOT / "docs" / "schemas"
_VALIDATE_EXAMPLE_CONFIGS_SCRIPT = (
    _PACKAGE_ROOT / "scripts" / "ci" / "validate-example-configs.sh"
)


def _read_schema_meta(filename: str) -> SchemaEntry:
    """Return presence + ``$id``/``$schema`` for *filename* under docs/schemas.

    Defensive by design: a missing or malformed schema never raises. Returns
    a :class:`SchemaEntry` with ``present=False`` on any failure so the
    bundle stays JSON-valid.
    """
    path = _SCHEMAS_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — schema metadata is best-effort
        # Failure path emits present=False. When schemas are present (the
        # normal case) all four keys appear; on failure the extra null
        # keys are informative rather than harmful.
        return SchemaEntry(present=False)
    if not isinstance(data, dict):
        return SchemaEntry(present=False)
    return SchemaEntry(
        present=True,
        path=str(path),
        id=data.get("$id"),
        schema=data.get("$schema"),
    )


def _build_schemas_member() -> SchemasMember:
    """Build the ``schemas.json`` bundle member: schema-file metadata.

    Surfaces presence and ``$id``/``$schema`` for the runtime, adapter,
    routing, and evidence-bundle schemas, plus whether the example-config
    validator script exists. Useful for diagnosing config/schema drift
    between a deployment and the schemas the bundle was built against.
    Defensive — never raises.
    """
    return SchemasMember(
        runtime_config_schema=_read_schema_meta("runtime-config.schema.json"),
        adapter_config_schema=_read_schema_meta("adapter-config.schema.json"),
        routing_config_schema=_read_schema_meta("routing-config.schema.json"),
        evidence_bundle_schema=_read_schema_meta("evidence-bundle.schema.json"),
        validate_example_configs_script_present=(
            _VALIDATE_EXAMPLE_CONFIGS_SCRIPT.is_file()
        ),
    )


def _get_version() -> str:
    """Return the MEDRE version string (``"unknown"`` if not installed)."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _json_default(obj: Any) -> Any:
    """``json.dumps`` default hook for dataclasses and Structs.

    ``_to_builtins`` already converts every production input, so this hook
    normally never fires for those types. Both branches are defense in
    depth and route through :func:`_to_builtins` so the normalisation
    contract is identical to the main path: a Struct nested inside a
    dataclass nested inside a Struct (or any future member shape) cannot
    leak through as a raw instance and trip :func:`json.dumps`.
    """
    # Defense-in-depth fallback. _to_builtins handles every current input;
    # this only fires if a future payload shape slips past it. The
    # dataclass branch routes through _to_builtins for the same reason
    # _to_builtins itself does: asdict leaves Struct-valued fields as
    # raw Struct instances, which json.dumps cannot serialise.
    if isinstance(obj, msgspec.Struct):
        return _to_builtins(msgspec.to_builtins(obj))
    if is_dataclass(obj) and not isinstance(obj, type):
        return _to_builtins(asdict(obj))
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _to_builtins(obj: Any) -> Any:
    """Convert msgspec.Struct / dataclass instances to JSON-safe builtins.

    Each container kind is handled so a Struct (or dataclass) nested at
    any depth is converted before reaching :func:`json.dumps`:

    - ``msgspec.Struct`` → :func:`msgspec.to_builtins`, then the result
      is RECURSED back through this function. ``msgspec.to_builtins``
      converts nested Structs and honours ``msgspec.field(name=...)``
      aliases such as ``$id`` / ``$schema`` on :class:`SchemaEntry`,
      but it does not convert every container shape the way this
      function does (e.g. tuples survive as tuples). The recursion
      guarantees a single normalisation contract for every nesting
      depth regardless of which layer produced the intermediate dict.
    - dataclass instance → :func:`dataclasses.asdict`, then the result
      is RECURSED back through this function. ``asdict`` does not know
      about Structs, so a Struct-valued field survives ``asdict`` as a
       raw Struct and would fall back to ``_json_default`` which now also
       handles Structs. The recursion keeps that case on the main
       normalisation path and also normalises tuples produced by ``asdict``
       into lists.
    - ``dict`` / ``list`` / ``tuple`` → recurse element-wise; tuples
      become lists (matching :func:`json.dumps` tuple semantics).

    Anything else passes through unchanged.
    """
    if isinstance(obj, msgspec.Struct):
        # msgspec.to_builtins handles Structs natively, but its output
        # is not guaranteed to match this function's container contract
        # (tuples stay tuples, future field shapes may slip through).
        # Recurse so every nesting depth is normalised by one function.
        return _to_builtins(msgspec.to_builtins(obj))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # asdict does not see Structs; recurse the result so a Struct nested
        # inside a dataclass field cannot leak through.
        return _to_builtins(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_builtins(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_builtins(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_builtins(v) for v in obj]
    # Closes the set/frozenset gap: previously fell through to the
    # fallthrough below and tripped json.dumps. Elements are normalised
    # through _to_builtins first, then sorted by their JSON
    # representation to guarantee a total ordering even for
    # heterogeneous sets with equal str representations (e.g. {1, "1"}).
    # bytes/datetime/Enum intentionally NOT handled here —
    # they need a conversion-policy decision (encoding, format, value
    # vs name). Add explicit branches when a member actually needs them.
    if isinstance(obj, (set, frozenset)):
        normalised = [_to_builtins(v) for v in obj]
        return sorted(
            normalised,
            key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")),
        )
    return obj


def _platform_info() -> dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "platform": platform.system().lower() or "unknown",
        "machine": platform.machine() or "unknown",
    }


def _safe_error(exc: BaseException) -> str:
    """Sanitise an exception's string form for inclusion in the bundle."""
    return sanitize_error(str(exc))


def _section_path_of(exc: BaseException) -> str | None:
    """Pull ``section_path`` off a ConfigValidationError if present."""
    if isinstance(exc, ConfigValidationError):
        return exc.section_path
    return None


def _build_manifest(now_fn: datetime) -> ManifestMember:
    return ManifestMember(
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        created_at=now_fn.isoformat(),
        command="medre support bundle",
        medre_version=_get_version(),
        platform=_platform_info(),
        redaction_policy="secret-key-name-match-v1",
    )


def _build_environment() -> EnvironmentMember:
    info = _platform_info()
    return EnvironmentMember(
        python_version=info["python_version"],
        platform=info["platform"],
        machine=info["machine"],
        medre_version=_get_version(),
    )


def _build_config_source(
    source: ConfigSource | None,
    path: Path | None,
) -> ConfigSourceMember:
    env_overrides_applied = _has_config_env_overrides()
    return ConfigSourceMember(
        source=source.value if source is not None else None,
        path=str(path) if path is not None else None,
        env_overrides_applied=env_overrides_applied,
    )


def _build_adapters(config: Any) -> dict[str, Any]:
    """Adapter inventory with safe wrapper fields and value-free introspection.

    Base fields (``adapter_id``, ``transport``, ``enabled``,
    ``origin_label``, ``adapter_kind``) are always emitted. When the typed
    adapter config can be introspected, three additional value-free
    summaries are added per adapter:

    - ``connection_type`` — the transport mode string (e.g. ``"fake"``,
      ``"tcp"``, ``"serial"``, ``"ble"``, ``"reticulum"``) if the config
      exposes one.
    - ``endpoint_fields_present`` — ``{field_name: true}`` for safe,
      non-secret endpoint-ish fields that are set. Helps triage without
      leaking values.
    - ``secret_fields_present`` — ``{field_name: true}`` for secret-like
      fields that are populated. **Values are never included** — only the
      boolean presence, so support can see which credentials an operator
      has configured without exposing them.

    Introspection uses only ``hasattr`` / ``getattr`` on the frozen
    dataclass — never ``validate()`` and never any method that could
    trigger an adapter SDK import. If introspection fails for one
    adapter, that adapter keeps its base fields and enrichment is
    skipped; the bundle stays JSON-valid.

    .. note::

       This member stays a plain ``dict`` rather than a ``msgspec.Struct``:
       ``connection_type`` is conditionally present (tests assert its
       *absence* for transports without one), and enrichment fields are
       omitted entirely on per-adapter introspection failure. A Struct
       always emits all fields, which would change the JSON shape.
    """
    adapters: list[dict[str, Any]] = []
    try:
        for transport, adapter_id, rtc in config.adapters.all_configs():
            entry: dict[str, Any] = {
                "adapter_id": adapter_id,
                "transport": transport,
                "enabled": bool(getattr(rtc, "enabled", False)),
                "origin_label": getattr(
                    getattr(rtc, "config", None), "origin_label", ""
                )
                or "",
                "adapter_kind": getattr(rtc, "adapter_kind", "real"),
            }
            cfg = getattr(rtc, "config", None)
            # Enrichment is best-effort per adapter — a single malformed
            # wrapper must not strip enrichment from the others, and must
            # never sink the bundle.
            try:
                ct = getattr(cfg, "connection_type", None)
                if ct is not None:
                    entry["connection_type"] = ct
                entry["endpoint_fields_present"] = _endpoint_fields_present(
                    transport, cfg
                )
                entry["secret_fields_present"] = _secret_fields_present(transport, cfg)
            except Exception:  # noqa: BLE001 — enrichment is best-effort
                pass
            adapters.append(entry)
    except Exception:
        # Defensive: keep partial inventory already collected before failure.
        pass
    return {"adapters": adapters}


# Safe endpoint-ish fields reported per transport (presence only, never
# values). Chosen to aid support triage: enough to see connection shape
# without leaking endpoints that could be sensitive in some deployments.
_ENDPOINT_FIELDS: dict[str, tuple[str, ...]] = {
    "matrix": ("homeserver", "user_id", "room_allowlist"),
    "meshtastic": ("host", "port", "serial_port", "ble_address", "channel_mapping"),
    "meshcore": ("host", "port", "serial_port", "ble_address", "serial_baudrate"),
    "lxmf": ("storage_path", "display_name"),
}

# Secret-like fields reported per transport as boolean presence only.
# Values are NEVER included — this dict only names which fields count.
_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "matrix": ("access_token",),
    "meshcore": ("ble_pin",),
    "lxmf": ("identity_path",),
}


def _field_is_present(cfg: Any, name: str) -> bool:
    """Return True if *name* on *cfg* counts as a populated field.

    Uses only ``hasattr`` / ``getattr``. Semantics:

    - ``room_allowlist`` — present when non-``None`` (``None`` means
      "accept all rooms", a real configuration choice rather than a
      missing field, so it is reported distinctly from a populated set).
    - Strings — present when non-empty after strip.
    - Collections (list/tuple/set/dict) — present when non-empty.
    - Everything else (bool/int/float) — present when truthy.
    """
    if not hasattr(cfg, name):
        return False
    value = getattr(cfg, name)
    if name == "room_allowlist":
        return value is not None
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return bool(value)


def _endpoint_fields_present(transport: str, cfg: Any) -> dict[str, bool]:
    """Return ``{field: True}`` for populated endpoint fields on *cfg*.

    Never includes values — only boolean presence. Returns ``{}`` for
    unknown transports so an unrecognised transport never raises.
    """
    return {
        name: True
        for name in _ENDPOINT_FIELDS.get(transport, ())
        if _field_is_present(cfg, name)
    }


def _secret_fields_present(transport: str, cfg: Any) -> dict[str, bool]:
    """Return ``{field: True}`` for populated secret-like fields on *cfg*.

    **Never includes values** — only boolean presence. This lets support
    see which credentials an operator has configured without leaking
    them. Returns ``{}`` for unknown transports.
    """
    return {
        name: True
        for name in _SECRET_FIELDS.get(transport, ())
        if _field_is_present(cfg, name)
    }


def _build_route_plan_member(config: Any) -> dict[str, Any]:
    """Serialise ``build_route_plan(config)`` to a JSON-safe dict.

    On any failure, returns ``{"error": "<sanitised>"}`` so the member
    is always valid JSON.

    .. note::

       Stays a plain ``dict``: the success path is already typed via the
       ``RoutePlan`` dataclass (serialised with :func:`dataclasses.asdict`),
       and the failure fallback ``{"error": ...}`` is intentionally
       dynamic. No separate model is warranted.
    """
    try:
        plan = build_route_plan(config)
        return asdict(plan)
    except Exception as exc:  # noqa: BLE001 — bundle must stay JSON-valid
        return {"error": _safe_error(exc)}


def _build_redacted_config_text(raw_text: str, source_label: str) -> str | None:
    """Parse *raw_text* as YAML and return a redacted YAML serialisation.

    Returns ``None`` if the text cannot be parsed (the failure is
    surfaced elsewhere as a config_check error). Round-trips through
    :func:`parse_yaml_config` so the same strict parsing rules apply.

    *source_label* is the config path used in parser error messages.
    """
    try:
        data = parse_yaml_config(raw_text, source_label)
        redacted = _redact(data)
        return yaml.safe_dump(redacted, sort_keys=True, default_flow_style=False)
    except (
        Exception
    ):  # noqa: BLE001 — any failure drops the member, never crashes the bundle
        # Config-check already records the load failure; no config member.
        return None


def create_support_bundle(
    config_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> Path:
    """Create a redacted support bundle ZIP file.

    Parameters
    ----------
    config_path:
        Optional explicit path to a YAML config file. When ``None`` the
        standard MEDRE discovery path is used.
    output_path:
        Optional path for the ZIP file. When ``None`` the bundle is
        written to ``medre-support-bundle.zip`` in the current working
        directory.

    Returns
    -------
    Path
        Absolute path to the written ZIP file.

    Notes
    -----
    The bundle is offline and observational: it loads config through
    the normal strict YAML path, runs config validation (recording
    success/failure), builds a route plan (if config loads), collects
    adapter summaries, redacts all secret-named fields, and writes a
    deterministic set of JSON / YAML members to a ZIP archive. It never
    starts adapters or performs network / hardware I/O.

    On any per-section failure the ZIP is still written with the
    sections that succeeded plus an ``error`` field describing the
    failure (sanitised). The function only raises if the ZIP itself
    cannot be written.
    """
    out = Path(output_path) if output_path is not None else Path(_DEFAULT_OUTPUT_NAME)
    out = out.expanduser().resolve()

    now = datetime.now(timezone.utc)

    members: dict[str, bytes] = {}

    # -- Always-present members --------------------------------------------
    members["manifest.json"] = _json_bytes(_build_manifest(now))
    members["environment.json"] = _json_bytes(_build_environment())
    # schemas.json depends only on the repo layout, never on config — it sits
    # with the always-present members so schema drift is reported even when
    # the config fails to load.
    members["schemas.json"] = _json_bytes(_build_schemas_member())

    # -- Config discovery, load, and validation ----------------------------
    discovered_path: Path | None = None
    discovered_source: ConfigSource | None = None
    config_check = ConfigCheckMember()

    try:
        discovered_path, discovered_source = find_config(config_path)
    except Exception as exc:  # noqa: BLE001 — bundle keeps going on config miss
        config_check.error = _safe_error(exc)

    members["config_source.json"] = _json_bytes(
        _build_config_source(discovered_source, discovered_path)
    )

    raw_text: str | None = None
    config: Any = None
    if discovered_path is not None:
        try:
            raw_text = discovered_path.read_text(encoding="utf-8")
        except OSError as exc:
            config_check.error = _safe_error(exc)
            raw_text = None

    if discovered_path is not None:
        try:
            # Pass the discovered path, not the original argument — when
            # discovery expanded a directory to a file (or applied an XDG
            # default), load_config must see the same path that was read.
            config, _source, _paths = load_config(discovered_path)
            config_check.success = True
            config_check.error = None
            config_check.error_section_path = None
        except ConfigValidationError as exc:
            config_check.success = False
            config_check.error = _safe_error(exc)
            config_check.error_section_path = _section_path_of(exc)
        except ConfigError as exc:
            config_check.success = False
            config_check.error = _safe_error(exc)
        except Exception as exc:  # noqa: BLE001 — defensive: keep bundle alive
            config_check.success = False
            config_check.error = _safe_error(exc)

    members["config_check.json"] = _json_bytes(config_check)

    # -- Route plan (only meaningful when config loaded) -------------------
    if config is not None:
        members["route_plan.json"] = _json_bytes(_build_route_plan_member(config))
        members["adapters.json"] = _json_bytes(_build_adapters(config))
    else:
        members["route_plan.json"] = _json_bytes({"error": "config load failed"})
        members["adapters.json"] = _json_bytes({"adapters": []})

    # -- Redacted config text ---------------------------------------------
    if raw_text is not None and discovered_path is not None:
        redacted_yaml = _build_redacted_config_text(raw_text, str(discovered_path))
        if redacted_yaml is not None:
            members["redacted_config.yaml"] = redacted_yaml.encode("utf-8")

    # -- Write the ZIP -----------------------------------------------------
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)

    return out


def _json_bytes(data: Any) -> bytes:
    """Serialise *data* to canonical, sorted, indented JSON UTF-8 bytes.

    msgspec.Struct instances are converted to plain builtins via
    :func:`_to_builtins` before reaching :func:`json.dumps`; the
    ``default`` hook remains as a fallback for any stray dataclasses.
    """
    return (
        json.dumps(
            _to_builtins(data),
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        + "\n"
    ).encode("utf-8")


if __name__ == "__main__":  # pragma: no cover — minimal import smoke
    # Behavioural coverage lives in tests/test_operator_support_bundle.py
    # and tests/test_support_bundle_no_io.py. This block only confirms
    # the module imports cleanly and exposes its public symbols.
    assert create_support_bundle is not None
    assert BUNDLE_SCHEMA_VERSION == 1
    print("support_bundle module loads OK", file=sys.stderr)
