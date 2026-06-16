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


def _read_schema_meta(filename: str) -> dict[str, Any]:
    """Return presence + ``$id``/``$schema`` for *filename* under docs/schemas.

    Defensive by design: a missing or malformed schema never raises. Returns
    ``{"present": False}`` on any failure so the bundle stays JSON-valid.
    """
    path = _SCHEMAS_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — schema metadata is best-effort
        return {"present": False}
    if not isinstance(data, dict):
        return {"present": False}
    return {
        "present": True,
        "path": str(path),
        "$id": data.get("$id"),
        "$schema": data.get("$schema"),
    }


def _build_schemas_member() -> dict[str, Any]:
    """Build the ``schemas.json`` bundle member: schema-file metadata.

    Surfaces presence and ``$id``/``$schema`` for the runtime, adapter, and
    routing config schemas, plus whether the example-config validator script
    exists. Useful for diagnosing config/schema drift between a deployment
    and the schemas the bundle was built against. Defensive — never raises.
    """
    return {
        "runtime_config_schema": _read_schema_meta("runtime-config.schema.json"),
        "adapter_config_schema": _read_schema_meta("adapter-config.schema.json"),
        "routing_config_schema": _read_schema_meta("routing-config.schema.json"),
        "validate_example_configs_script_present": (
            _VALIDATE_EXAMPLE_CONFIGS_SCRIPT.is_file()
        ),
    }


def _get_version() -> str:
    """Return the MEDRE version string (``"unknown"`` if not installed)."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _json_default(obj: Any) -> Any:
    """``json.dumps`` default hook for dataclasses and datetimes."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


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


def _build_manifest(now_fn: datetime) -> dict[str, Any]:
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": now_fn.isoformat(),
        "command": "medre support bundle",
        "medre_version": _get_version(),
        "platform": _platform_info(),
        "redaction_policy": "secret-key-name-match-v1",
    }


def _build_environment() -> dict[str, Any]:
    info = _platform_info()
    info["medre_version"] = _get_version()
    return info


def _build_config_source(
    source: ConfigSource | None,
    path: Path | None,
) -> dict[str, Any]:
    env_overrides_applied = _has_config_env_overrides()
    return {
        "source": source.value if source is not None else None,
        "path": str(path) if path is not None else None,
        "env_overrides_applied": env_overrides_applied,
    }


def _build_adapters(config: Any) -> dict[str, Any]:
    """Adapter inventory with only safe wrapper fields.

    Never introspects ``rtc.config`` internals — origin_label comes from
    the route-plan walk which already filters it to the safe fallback
    string.
    """
    adapters: list[dict[str, Any]] = []
    try:
        for transport, adapter_id, rtc in config.adapters.all_configs():
            adapters.append(
                {
                    "adapter_id": adapter_id,
                    "transport": transport,
                    "enabled": bool(getattr(rtc, "enabled", False)),
                    "origin_label": getattr(
                        getattr(rtc, "config", None), "origin_label", ""
                    )
                    or "",
                }
            )
    except Exception:
        # Defensive: a malformed adapter set should not sink the bundle.
        adapters = []
    return {"adapters": adapters}


def _build_route_plan_member(config: Any) -> dict[str, Any]:
    """Serialise ``build_route_plan(config)`` to a JSON-safe dict.

    On any failure, returns ``{"error": "<sanitised>"}`` so the member
    is always valid JSON.
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
    config_check: dict[str, Any] = {
        "success": False,
        "error": None,
        "error_section_path": None,
    }

    try:
        discovered_path, discovered_source = find_config(config_path)
    except Exception as exc:  # noqa: BLE001 — bundle keeps going on config miss
        config_check["error"] = _safe_error(exc)

    members["config_source.json"] = _json_bytes(
        _build_config_source(discovered_source, discovered_path)
    )

    raw_text: str | None = None
    config: Any = None
    if discovered_path is not None:
        try:
            raw_text = discovered_path.read_text(encoding="utf-8")
        except OSError as exc:
            config_check["error"] = _safe_error(exc)
            raw_text = None

    if discovered_path is not None:
        try:
            # ponytail: pass the discovered path, not the original argument —
            # when discovery expanded a directory to a file (or applied an
            # XDG default), load_config must see the same path that was read.
            config, _source, _paths = load_config(discovered_path)
            config_check["success"] = True
            config_check["error"] = None
            config_check["error_section_path"] = None
        except ConfigValidationError as exc:
            config_check["success"] = False
            config_check["error"] = _safe_error(exc)
            config_check["error_section_path"] = _section_path_of(exc)
        except ConfigError as exc:
            config_check["success"] = False
            config_check["error"] = _safe_error(exc)
        except Exception as exc:  # noqa: BLE001 — defensive: keep bundle alive
            config_check["success"] = False
            config_check["error"] = _safe_error(exc)

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
    """Serialise *data* to canonical, sorted, indented JSON UTF-8 bytes."""
    return (
        json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n"
    ).encode("utf-8")


if __name__ == "__main__":  # ponytail: tiny self-check, no test framework
    # Smoke: round-trip a redactor walk on a synthetic config dict.
    sample = {
        "adapters": {
            "matrix": {
                "main": {
                    "access_token": "syt_secret_value",
                    "homeserver": "https://example.org",
                    "origin_label": "bridge",
                }
            }
        },
        "storage": {"backend": "sqlite", "path": "/tmp/medre.db"},
    }
    redacted = _redact(sample)
    matrix_cfg = redacted["adapters"]["matrix"]["main"]
    assert matrix_cfg["access_token"] == _REDACTED, matrix_cfg
    assert matrix_cfg["homeserver"] == "https://example.org"
    assert matrix_cfg["origin_label"] == "bridge"
    assert redacted["storage"]["path"] == "/tmp/medre.db"
    print("support_bundle self-check OK", file=sys.stderr)
