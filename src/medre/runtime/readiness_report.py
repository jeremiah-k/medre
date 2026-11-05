"""Offline readiness report composing manifest, installation, and config preflight.

One read-only report composes the existing offline checks so an operator can
see, per compatibility-snapshot provider and per configured adapter, what is
installed, what is configured, and what offline preflight can honestly
establish. The report is observational only:

- No adapter SDK is imported (no nio / meshtastic / meshcore / lxmf).
- No adapter, account, device, or network is contacted.
- No storage, file, or configuration write is performed; the report is
  returned in memory and printed by the caller.
- Distribution presence is read through :mod:`importlib.metadata` against
  manifest-declared distribution names; provider modules are never probed
  or imported by name.
- Failures are reported as typed categories. Exception text, SDK error
  text, credentials, store paths, and native identifiers never enter the
  report.
- The report carries no timestamps and no filesystem paths, so identical
  input and environment produce identical output.

Every runtime and recipient claim is reported as ``not_observed``, the
existing evidence vocabulary's explicit absence-of-observation token
(docs/spec/native-evidence.md): offline checks say nothing about live
runtime state and never verify recipients. A passed preflight is not
native acceptance, runtime readiness, or recipient verification.
"""

from __future__ import annotations

import importlib.metadata
import json
from importlib.resources import files
from importlib.resources.abc import Traversable
import re
from pathlib import Path
from typing import Any

from medre.adapter_registry import (
    adapter_discovery_failures,
    get_adapter_spec,
    valid_transport_name,
)
from medre.config.env import apply_env_overrides
from medre.config.errors import (
    ConfigError,
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.loader import find_config, load_config
from medre.config.paths import resolve
from medre.runtime.adapter_preparation import prepare_adapter_config
from medre.runtime.route_engine import build_runtime_routes
from medre.runtime.route_plan import build_route_plan

__all__ = ["REPORT_VERSION", "build_readiness_report"]

REPORT_VERSION: int = 1
"""Report shape version. Bump only for a breaking output-shape change."""

_MANIFEST_RELPATH = Path(".github") / "adapter-compatibility.json"
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
_BOUNDARY_STATEMENT = (
    "Offline checks run without SDK sessions, logins, network, or writes. "
    "They never observe the live runtime or provider state, never establish "
    "native acceptance, and never verify recipients; a passed preflight "
    "alone qualifies nothing beyond what it states."
)


def _core_version() -> str:
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


# ---------------------------------------------------------------------------
# Compatibility snapshot (.github/adapter-compatibility.json)
# ---------------------------------------------------------------------------


def _locate_manifest() -> Path | Traversable | None:
    """Find the compatibility snapshot for this installation.

    The built resource is authoritative when present; editable installs
    otherwise use their own source checkout. An installation without either
    reports ``not_found``. Another checkout discovered from the working
    directory never supplies the running installation's pins.
    """
    packaged = files("medre").joinpath("_data", "adapter-compatibility.json")
    if packaged.is_file():
        return packaged
    # Only the source file's own src-layout checkout can supply a fallback.
    # Wheel locations must not borrow an incidental parent/cwd snapshot.
    source = _PACKAGE_ROOT / "src" / "medre" / "runtime" / "readiness_report.py"
    if source.resolve() != Path(__file__).resolve():
        return None
    candidate = _PACKAGE_ROOT / _MANIFEST_RELPATH
    return candidate if candidate.is_file() else None


_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_DISTRIBUTION_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_EXCEPTION_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _load_manifest() -> tuple[str, list[dict[str, Any]]]:
    """Return ``(status, provider_entries)`` for the compatibility snapshot.

    Status is ``available`` or a typed unavailability category
    (``not_found`` / ``malformed``). Entries are validated with the same
    rules the development-environment authority applies (at least one
    entry, transport token, owner/repository, full commit SHA,
    distribution name, unique alias); a snapshot failing any rule is
    ``malformed`` rather than partially reported. Providers keep manifest
    order so the same snapshot always yields the same report ordering. A
    missing provider stays visible: every snapshot entry is reported
    whether or not it is installed.
    """
    path = _locate_manifest()
    if path is None:
        return "not_found", []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "not_found", []
    except (OSError, UnicodeError, ValueError):
        return "malformed", []
    if not isinstance(entries, list) or not entries:
        return "malformed", []
    providers: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not _valid_manifest_entry(entry, providers):
            return "malformed", []
        providers.append(
            {
                "transport": entry["transport"],
                "repository": entry["repository"],
                "ref": entry["ref"],
                "extras": entry.get("extras") or None,
                "distribution": entry["distribution"],
                "python_requires": entry.get("python_requires"),
                "local_integration": bool(entry.get("local_integration", False)),
            }
        )
    return "available", providers


def _valid_manifest_entry(entry: dict[str, Any], seen: list[dict[str, Any]]) -> bool:
    """Apply the compatibility snapshot's structural validation rules.

    The rules mirror the development-environment authority: a valid
    transport token, an owner/repository pair, a full commit SHA, a
    distribution name, and a unique transport alias per snapshot.
    """
    transport = entry.get("transport")
    repository = entry.get("repository")
    ref = entry.get("ref")
    distribution = entry.get("distribution")
    if not isinstance(transport, str) or not valid_transport_name(transport):
        return False
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        return False
    if not isinstance(ref, str) or not _REVISION_RE.fullmatch(ref):
        return False
    if not isinstance(distribution, str) or not _DISTRIBUTION_RE.fullmatch(
        distribution
    ):
        return False
    if any(
        entry.get(name) is not None and not isinstance(entry[name], str)
        for name in ("extras", "python_requires")
    ) or not isinstance(entry.get("local_integration", False), bool):
        return False
    return all(provider["transport"] != transport for provider in seen)


def _distribution_state(name: str) -> tuple[str, str | None]:
    """Return ``(distribution_status, installed_version)`` for *name*.

    Distribution metadata is the only evidence used: no module is probed,
    imported, or executed to learn whether a distribution is present.
    """
    try:
        return "installed", importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing", None


def _registration_state(transport: str) -> str:
    """Return the entry-point registry state for *transport*.

    ``registered`` means a healthy provider spec was discovered;
    ``broken`` means the provider was quarantined with a diagnostic;
    ``unregistered`` means no entry point declared this transport.
    """
    if get_adapter_spec(transport) is not None:
        return "registered"
    if any(failure.transport == transport for failure in adapter_discovery_failures()):
        return "broken"
    return "unregistered"


def _registration_error_category(transport: str) -> str | None:
    """Reduce a quarantined provider's diagnostic to a typed category.

    Discovery diagnostics are exception-shaped (``TypeName: message``) and
    may quote provider paths; only the exception class name is safe to
    report. Full diagnostics remain available through
    ``medre adapters check``.
    """
    for failure in adapter_discovery_failures():
        if failure.transport == transport:
            if ":" not in failure.reason:
                return "provider_error"
            head = failure.reason.split(":", 1)[0].strip()
            return head if _EXCEPTION_NAME_RE.fullmatch(head) else "provider_error"
    return None


def _native_setup(spec: object | None) -> str | None:
    """Classify the provider-owned setup path from the spec declaration.

    ``provider_guided`` means the provider declares native setup fields, so
    the pure offline setup form (``medre config native``) exists.
    ``manual_provisioning`` means the operator must follow the provider's
    own provisioning guide. ``None`` means the provider is not available
    for inspection, so the setup path is unknown.
    """
    if spec is None:
        return None
    fields = getattr(spec, "native_setup_fields", ())
    return "provider_guided" if fields else "manual_provisioning"


def _manifest_distribution_of(
    providers: list[dict[str, Any]], transport: str
) -> str | None:
    """Return the snapshot's distribution name for *transport*, if declared."""
    for provider in providers:
        if provider["transport"] == transport:
            distribution = provider.get("distribution")
            return distribution if isinstance(distribution, str) else None
    return None


def _manifest_providers_section() -> dict[str, Any]:
    status, entries = _load_manifest()
    providers: list[dict[str, Any]] = []
    if status == "available":
        for entry in entries:
            transport = entry["transport"]
            registration = _registration_state(transport)
            distribution_status, installed_version = _distribution_state(
                entry["distribution"]
            )
            providers.append(
                {
                    **entry,
                    "distribution_status": distribution_status,
                    "installed_version": installed_version,
                    "registration": registration,
                    "registration_error_category": (
                        _registration_error_category(transport)
                        if registration == "broken"
                        else None
                    ),
                    "native_setup": _native_setup(get_adapter_spec(transport)),
                }
            )
    return {
        "status": status,
        "providers": providers,
        "provider_count": len(providers),
        "installed_count": sum(
            1 for p in providers if p["distribution_status"] == "installed"
        ),
    }


# ---------------------------------------------------------------------------
# Configuration section
# ---------------------------------------------------------------------------


def _config_error_category(exc: BaseException) -> str:
    """Map a configuration failure to a typed category without its text."""
    if isinstance(exc, ConfigNotFoundError):
        return "config_not_found"
    if isinstance(exc, ConfigFileError):
        return "config_file_error"
    if isinstance(exc, ConfigValidationError):
        return "validation_error"
    if isinstance(exc, ConfigError):
        return "config_error"
    return "unexpected_error"


def _safe_section_path(path: str | None) -> str | None:
    """Retain only structural YAML location, never operator-chosen keys.

    Validation exceptions can include arbitrary adapter instance names,
    route IDs, or logger names in their section path. These are data, not
    closed schema keys, so do not copy them to the readiness report.
    """
    if path is None:
        return None
    if path == "<root>":
        return path
    parts = path.split(".")
    if parts[0] == "adapters" and len(parts) >= 2:
        transport = parts[1]
        if not valid_transport_name(transport) or get_adapter_spec(transport) is None:
            return "adapters"
        return (
            f"adapters.{transport}.instance"
            if len(parts) >= 3
            else f"adapters.{transport}"
        )
    if parts[0] == "routes":
        return "routes.route" if len(parts) >= 2 else "routes"
    if parts[0] == "logging" and len(parts) >= 2 and parts[1] == "overrides":
        return "logging.overrides"  # Logger names can contain arbitrary text.
    structural = {
        "runtime",
        "logging",
        "storage",
        "retry",
        "attachments",
        "content_retention",
        "content_ceiling",
    }
    # Other nested paths can contain arbitrary user-provided YAML keys.
    # The owning top-level section is sufficient for a safe diagnostic.
    return parts[0] if parts[0] in structural else "section"


def _load_effective_config(
    config_path: str | Path | None,
) -> tuple[dict[str, Any], Any]:
    """Load and environment-override the configuration like ``medre run``.

    Returns ``(section, config_or_None)``. ``section`` always carries a
    ``status`` of ``valid`` or an actionable-error category plus, for
    validation errors, the structural ``section_path``. Error text never
    enters the report; ``medre config check`` remains the detailed
    diagnostic surface.
    """
    section: dict[str, Any] = {
        "source": None,
        "section_path": None,
        "error_category": None,
    }

    def failed(
        category: str, section_path: str | None = None
    ) -> tuple[dict[str, Any], None]:
        section["status"] = category
        section["error_category"] = category
        section["section_path"] = _safe_section_path(section_path)
        return section, None

    try:
        path, source = find_config(config_path)
    except ConfigValidationError as exc:
        return failed(_config_error_category(exc), exc.section_path)
    except ConfigError as exc:
        return failed(_config_error_category(exc))
    except Exception as exc:
        return failed(_config_error_category(exc))
    try:
        config, _resolved_source, paths = load_config(path)
        config = apply_env_overrides(config, paths)
    except ConfigValidationError as exc:
        return failed(_config_error_category(exc), exc.section_path)
    except ConfigError as exc:
        return failed(_config_error_category(exc))
    except Exception as exc:
        return failed(_config_error_category(exc))
    section["source"] = source.value
    section["status"] = "valid"
    return section, config


# ---------------------------------------------------------------------------
# Per-configured-adapter section
# ---------------------------------------------------------------------------


def _sdk_distribution_state(
    spec: object | None, adapter_kind: str
) -> tuple[str | None, str, str | None]:
    """Return ``(sdk_distribution, sdk_status, installed_version)``.

    A ``real`` adapter whose spec declares an SDK distribution requires it;
    ``fake`` adapters and providers without a declared SDK distribution are
    ``not_required`` (the same rule the guided authoring preflight applies).
    Without an installed spec the SDK requirement is ``unknown``.
    """
    if spec is None:
        return None, "unknown", None
    declared = getattr(spec, "distribution", None)
    if adapter_kind != "real" or not declared:
        return declared, "not_required", None
    status, version = _distribution_state(declared)
    return declared, status, version


def _adapter_rows(config: Any, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compose one readiness row per configured adapter instance.

    Rows follow config registration order, which is deterministic for
    identical YAML. Each row answers only what offline checks can know:
    installation, config validity, and pure provider-owned preflight.
    Runtime and recipient claims are ``not_observed``.
    """
    paths = resolve()
    try:
        routes = tuple(build_runtime_routes(config.routes))
    except Exception:
        # Route expansion failures are reported in the routes section;
        # per-adapter preflight still judges each adapter's own config.
        routes = ()
    rows: list[dict[str, Any]] = []
    for transport, adapter_id, rtc in config.adapters.all_configs():
        adapter_kind = getattr(rtc, "adapter_kind", "real")
        spec = get_adapter_spec(transport)
        adapter_config = getattr(rtc, "config", None)
        provider_distribution = _manifest_distribution_of(providers, transport)
        provider_status = (
            "unknown"
            if provider_distribution is None
            else _distribution_state(provider_distribution)[0]
        )
        sdk_distribution, sdk_status, sdk_version = _sdk_distribution_state(
            spec, adapter_kind
        )
        row: dict[str, Any] = {
            "adapter_id": adapter_id,
            "transport": transport,
            "enabled": bool(getattr(rtc, "enabled", False)),
            "adapter_kind": adapter_kind,
            "provider_distribution": provider_distribution,
            "provider_distribution_status": provider_status,
            "sdk_distribution": sdk_distribution,
            "sdk_distribution_status": sdk_status,
            "sdk_installed_version": sdk_version,
            "native_setup": _native_setup(spec),
            "runtime": "not_observed",
            "recipient": "not_observed",
        }

        blocked: str | None = None
        config_state = "valid"
        if spec is None:
            if any(f.transport == transport for f in adapter_discovery_failures()):
                blocked = "transport_broken"
            else:
                blocked = "transport_unregistered"
        else:
            if adapter_config is not None and hasattr(adapter_config, "validate"):
                try:
                    adapter_config.validate()
                except Exception:
                    config_state = "actionable_error"
                    blocked = "config_invalid"
            if blocked is None and sdk_status == "missing":
                blocked = "sdk_distribution_missing"
            if blocked is None and adapter_config is not None:
                # Provider-owned pure preflight, shared with authoring and
                # startup; it derives native config without I/O. Config-less
                # instances (adapters without config fields) need none.
                try:
                    prepare_adapter_config(
                        spec, adapter_id, adapter_config, paths=paths, routes=routes
                    )
                except Exception:
                    config_state = "actionable_error"
                    blocked = "preparation_failed"
        row["config"] = config_state
        row["preflight"] = "blocked" if blocked is not None else "passed"
        row["blocked_category"] = blocked
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Route-plan section
# ---------------------------------------------------------------------------


def _routes_section(config: Any) -> dict[str, Any]:
    """Summarize the offline route-plan expansion with typed categories."""
    section: dict[str, Any] = {"status": "passed", "error_category": None}
    try:
        plan = build_route_plan(config)
    except Exception:
        # A failed plan has no countable routes. Never fabricate zero totals.
        section.update(
            status="blocked",
            error_category="route_expansion_error",
            total=None,
            enabled=None,
            expansion_failures=None,
            conflicts=None,
        )
        return section
    failed = [entry for entry in plan.routes if entry.error is not None]
    section["total"] = len(plan.routes)
    section["enabled"] = sum(1 for entry in plan.routes if entry.enabled)
    section["expansion_failures"] = len(failed)
    section["conflicts"] = len(plan.conflicts)
    if failed or plan.conflicts:
        section["status"] = "blocked"
        section["error_category"] = (
            "route_expansion_error" if failed else "route_conflict"
        )
    return section


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _guidance(
    manifest: dict[str, Any],
    config_section: dict[str, Any],
    adapters: list[dict[str, Any]],
    routes: dict[str, Any] | None,
) -> list[str]:
    """Build ordered, path-free, secret-safe next-step guidance."""
    steps: list[str] = []
    if manifest["status"] != "available":
        steps.append(
            "Compatibility snapshot unavailable in this installation; "
            "use a MEDRE build that includes its own compatibility pins."
        )
    missing = [
        p["distribution"]
        for p in manifest["providers"]
        if p["distribution_status"] == "missing"
    ]
    if missing:
        steps.append(
            "Install missing provider distributions "
            f"({', '.join(missing)}); see docs/ops/adapter-installation.md."
        )
    if any(p["registration"] == "broken" for p in manifest["providers"]):
        steps.append(
            "A provider registration is quarantined; 'medre adapters check' "
            "shows discovery diagnostics."
        )
    if any(
        p["registration"] == "unregistered" and p["distribution_status"] == "installed"
        for p in manifest["providers"]
    ):
        steps.append(
            "An installed provider has no registered adapter entry point; "
            "'medre adapters check' shows registration diagnostics."
        )
    if config_section["status"] == "config_not_found":
        steps.append(
            "No configuration found; run 'medre config sample' or author one "
            "with 'medre config bridge'."
        )
    elif config_section["status"] != "valid":
        steps.append(
            "Fix the reported configuration section; 'medre config check' "
            "prints full validation details."
        )
    blocked = {
        row["blocked_category"] for row in adapters if row["preflight"] == "blocked"
    }
    if blocked & {"transport_unregistered", "transport_broken"}:
        steps.append(
            "Resolve provider discovery failures; 'medre adapters check' "
            "shows registration diagnostics."
        )
    if "config_invalid" in blocked:
        steps.append(
            "Resolve adapter configuration errors; 'medre config check' "
            "provides the structural validation details."
        )
    if "sdk_distribution_missing" in blocked:
        steps.append(
            "Install the missing provider-declared SDK distributions and "
            "rerun 'medre readiness' to inspect installed metadata."
        )
    if "preparation_failed" in blocked:
        steps.append(
            "Resolve the provider-owned configuration preparation failure "
            "using its native setup guide, then rerun 'medre readiness'; "
            "'medre config check' does not execute provider preparation."
        )
    if routes is not None and routes.get("status") == "blocked":
        steps.append(
            "Resolve route plan failures; 'medre routes plan' shows the "
            "per-route detail."
        )
    if not steps:
        steps.append(
            "Offline readiness looks complete; qualify native delivery "
            "separately before relying on live delivery."
        )
    steps.append(
        "Offline results never establish runtime readiness, native "
        "acceptance, or recipient verification."
    )
    return steps


def build_readiness_report(config_path: str | Path | None = None) -> dict[str, Any]:
    """Compose the offline readiness report as a JSON-safe dict.

    The report is authoritative for what offline checks can establish and
    deliberately silent about everything they cannot. See the module
    docstring for the observation boundary and the determinism rules.
    """
    manifest = _manifest_providers_section()
    config_section, config = _load_effective_config(config_path)
    adapters: list[dict[str, Any]] = []
    routes: dict[str, Any] | None = None
    if config is not None:
        adapters = _adapter_rows(config, manifest["providers"])
        routes = _routes_section(config)
    config_section["adapter_count"] = len(adapters)
    config_section["enabled_adapter_count"] = sum(
        1 for row in adapters if row["enabled"]
    )
    config_section["routes"] = routes
    return {
        "report_version": REPORT_VERSION,
        "medre_version": _core_version(),
        "manifest": manifest,
        "config": config_section,
        "adapters": adapters,
        "boundary": {
            "offline": True,
            "runtime": "not_observed",
            "recipients": "not_observed",
            "statement": _BOUNDARY_STATEMENT,
        },
        "guidance": _guidance(manifest, config_section, adapters, routes),
    }
