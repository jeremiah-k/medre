"""Evidence bundle collector for operator support diagnostics.

Provides :func:`collect_evidence_bundle` — a single async function that
collects a comprehensive, read-only diagnostic bundle from a MEDRE
configuration.  The bundle is a JSON-safe ``dict`` suitable for attachment
to bug reports, support tickets, or operational dashboards.

Read-only by default
--------------------
The evidence command does **not** start the runtime or mutate storage
unless ``include_refresh_health=True`` is passed.  When live health is
requested, the runtime is started once, health is refreshed, and the
runtime is stopped cleanly.  The report unambiguously flags this via
``runtime_started: true``.

Report shape
------------
The top-level dict contains:

* ``schema_version`` — ``1`` (frozen during pre-release).
* ``status`` — ``"passed"`` | ``"partial"`` | ``"error"``.
* ``collected_at`` — ISO-8601 UTC timestamp.
* ``medre_version`` — installed package version.
* ``config_source`` — how the config file was found.
* ``command`` — ``"evidence"`` (the CLI command that produced the report).
* ``generated_at`` — ISO-8601 UTC timestamp (same as ``collected_at``).
* ``runtime_started`` — ``true`` only when ``--include-refresh-health``
  was used and the runtime actually started.
* ``sections`` — grouped evidence (each with its own ``status``).
* ``errors`` — flat list of bounded error strings across all sections.
* ``limitations`` — honest list of what the evidence does **not** prove.

Section status values: ``"passed"``, ``"partial"``, ``"error"``, ``"skipped"``.

Public symbols
--------------
* :func:`collect_evidence_bundle` — main entry point.
"""

# Public API — re-exported from the bundle module.
from ._bundle import collect_evidence_bundle

# Internal helpers re-exported for test compatibility.
# Tests import these from ``medre.runtime.evidence`` directly.
from ._helpers import (  # noqa: F401
    SCHEMA_VERSION,
    _compute_overall_status,
    _section_error,
    _section_ok,
    _section_partial,
    _section_skipped,
)

__all__ = ["collect_evidence_bundle"]
