"""Event schema registry and versioning support.

This module provides:

* :data:`CURRENT_SCHEMA_VERSION` – the current schema contract version.
* :data:`VALID_RELATION_TYPES` – the set of valid ``relation_type`` values.
* :class:`SchemaVersion` – a ``(event_kind, version)`` pair.
* :class:`SchemaRegistry` – a registry that maps event kinds to schema
  versions and validator callables.
* :class:`_MigrationRegistry` – minimal registry-only hook for future schema
  migrations.

The registry is deliberately lightweight – it stores validator callables
rather than performing structural schema validation itself.  Downstream
packages can register JSON-Schema validators, pydantic models, or any
``Callable[[dict], list[str]]`` that returns a list of error strings.

Schema Migration Policy
-----------------------
Pre-release (current):
  Schemas may change directly — fields renamed, types changed,
  structures reorganised — without migration paths.  There are no
  external consumers, so breaking changes are applied by updating
  tests and documentation in the same commit.

Post-release (future stability guarantee):
  Once a stable release ships, the schema becomes additive-only:
  new fields append with defaults; existing fields are never removed.
  ``MIGRATION_REGISTRY`` provides a registry-only hook for migration
  functions (``Callable[[dict], dict]``).  A migration window may be
  offered for non-trivial schema transitions.
"""

from __future__ import annotations

from typing import Callable

import msgspec

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Current schema contract version.  ``v1`` is the baseline; all
#: events with ``schema_version == 1`` conform to this contract.
CURRENT_SCHEMA_VERSION: int = 1

#: Immutable set of valid ``relation_type`` values accepted by
#: :class:`~medre.core.events.canonical.EventRelation`.
VALID_RELATION_TYPES: frozenset[str] = frozenset(
    {"reply", "reaction", "edit", "delete", "thread"}
)


# ---------------------------------------------------------------------------
# SchemaVersion
# ---------------------------------------------------------------------------


class SchemaVersion(msgspec.Struct, frozen=True):
    """An immutable ``(event_kind, version)`` pair.

    Attributes
    ----------
    event_kind:
        The event kind string this version applies to.
    version:
        Monotonically increasing version number for the kind's payload
        schema.
    """

    event_kind: str
    version: int


def schema_version_from_event(
    event_kind: str, payload: dict[str, object]
) -> SchemaVersion:
    """Extract a :class:`SchemaVersion` from a raw event payload.

    The payload is expected to contain a ``"schema_version"`` key with an
    ``int`` value.  If the key is missing or not an ``int``, version ``1``
    is assumed.

    Parameters
    ----------
    event_kind:
        The event kind string.
    payload:
        The raw event payload dictionary.

    Returns
    -------
    SchemaVersion
        The extracted version pair.
    """
    raw = payload.get("schema_version", 1)
    version: int = raw if isinstance(raw, int) else 1  # type: ignore[assignment]
    return SchemaVersion(event_kind=event_kind, version=version)


# Type alias for validator callables.
#
# A validator receives the event payload dict and returns a list of
# human-readable error strings.  An empty list means the payload is valid.
Validator = Callable[[dict[str, object]], list[str]]


# ---------------------------------------------------------------------------
# SchemaRegistry
# ---------------------------------------------------------------------------


class SchemaRegistry:
    """Mutable registry that maps ``(event_kind, version)`` to validators.

    Thread-safety is the caller's responsibility – the registry is
    intended to be populated once during application startup and then
    used read-only.

    Example
    -------
    >>> registry = SchemaRegistry()
    >>> registry.register("message.text", 1, lambda p: [])
    >>> registry.validate("message.text", {"body": "hello"})
    True
    """

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], Validator] = {}

    # -- Mutation ---------------------------------------------------------

    def register(
        self,
        event_kind: str,
        schema_version: int,
        validator: Validator,
    ) -> None:
        """Register a validator for an event kind and version.

        If a validator was already registered for the same
        ``(event_kind, schema_version)`` pair it is silently replaced.

        Parameters
        ----------
        event_kind:
            The event kind string (e.g. ``"message.text"``).
        schema_version:
            The schema version number.
        validator:
            A callable that accepts a payload dict and returns a list of
            error strings (empty if valid).
        """
        self._schemas[(event_kind, schema_version)] = validator

    def register_or_replace(
        self,
        event_kind: str,
        schema_version: int,
        validator: Validator,
    ) -> None:
        """Register a validator, explicitly overwriting any existing one.

        Unlike :meth:`register`, this method is named to make the
        overwrite semantics explicit at the call site.

        Parameters
        ----------
        event_kind:
            The event kind string (e.g. ``"message.text"``).
        schema_version:
            The schema version number.
        validator:
            A callable that accepts a payload dict and returns a list of
            error strings (empty if valid).
        """
        self._schemas[(event_kind, schema_version)] = validator

    # -- Query ------------------------------------------------------------

    def get(self, event_kind: str, schema_version: int = 1) -> Validator | None:
        """Retrieve the validator for a kind and version.

        Parameters
        ----------
        event_kind:
            The event kind string.
        schema_version:
            The schema version number (defaults to ``1``).

        Returns
        -------
        Validator | None
            The registered validator, or ``None`` if no schema has been
            registered for the given kind and version.
        """
        return self._schemas.get((event_kind, schema_version))

    # -- Validation -------------------------------------------------------

    def validate(
        self,
        event_kind: str,
        payload: dict[str, object],
        schema_version: int | None = None,
        *,
        errors: list[str] | None = None,
    ) -> bool:
        """Validate a payload against the registered schema.

        Parameters
        ----------
        event_kind:
            The event kind string.
        payload:
            The event payload to validate.
        schema_version:
            Explicit schema version.  When ``None`` the version is read
            from ``payload["schema_version"]``, defaulting to ``1``.
        errors:
            Optional mutable list that will be populated with validation
            error strings if the caller wants to inspect them.

        Returns
        -------
        bool
            ``True`` if the payload is valid, ``False`` otherwise.
        """
        if schema_version is None:
            sv = schema_version_from_event(event_kind, payload)
            version = sv.version
        else:
            version = schema_version

        validator = self._schemas.get((event_kind, version))
        if validator is None:
            if errors is not None:
                errors.append(
                    f"No schema registered for kind={event_kind!r} "
                    f"version={version}"
                )
            return False

        if not callable(validator):
            if errors is not None:
                errors.append(
                    f"Registered validator for kind={event_kind!r} "
                    f"version={version} is not callable"
                )
            return False

        found_errors = validator(payload)
        if errors is not None:
            errors.extend(found_errors)
        return len(found_errors) == 0


# ---------------------------------------------------------------------------
# Migration registry (Phase 1: registry-only hook)
# ---------------------------------------------------------------------------

#: Type alias for a migration function.  Receives a payload dict and
#: returns a new dict with any added default fields.
MigrationFn = Callable[[dict[str, object]], dict[str, object]]


class _MigrationRegistry:
    """Minimal registry for schema migration functions.

    Phase 1 provides the registration and lookup API only.  No migrations
    are executed automatically – the registry exists so that downstream
    packages can register migration functions that future versions of the
    framework will call during decode.

    Migration contract:

    * Migrations are keyed by ``(event_kind, from_version, to_version)``.
    * A migration receives the payload dict and returns a **new** dict
      with any appended default fields.
    * Within a stability guarantee cycle: fields are never removed — only appended or deprecated-in-place.
    * Deprecated fields remain populated for at least one version cycle once a public stability guarantee is in effect.
      (No public stability guarantee is currently declared; pre-release migration rules apply, see module docstring.)

    Thread-safety is the caller's responsibility, same as :class:`SchemaRegistry`.
    """

    def __init__(self) -> None:
        self._migrations: dict[tuple[str, int, int], MigrationFn] = {}

    def register(
        self,
        event_kind: str,
        from_version: int,
        to_version: int,
        migration: MigrationFn,
    ) -> None:
        """Register a migration function.

        Silently replaces any existing migration for the same key.

        Parameters
        ----------
        event_kind:
            The event kind string.
        from_version:
            Source schema version.
        to_version:
            Target schema version.
        migration:
            Callable that transforms a payload dict from *from_version*
            to *to_version* shape.
        """
        self._migrations[(event_kind, from_version, to_version)] = migration

    def get(
        self,
        event_kind: str,
        from_version: int,
        to_version: int,
    ) -> MigrationFn | None:
        """Retrieve a migration function, or ``None`` if not registered."""
        return self._migrations.get((event_kind, from_version, to_version))

    @property
    def registered_keys(self) -> frozenset[tuple[str, int, int]]:
        """Snapshot of all registered migration keys."""
        return frozenset(self._migrations.keys())


#: Module-level migration registry singleton.
MIGRATION_REGISTRY: _MigrationRegistry = _MigrationRegistry()
