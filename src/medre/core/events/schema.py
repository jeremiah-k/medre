"""Event schema registry and versioning support.

This module provides:

* :data:`CURRENT_SCHEMA_VERSION` – the current schema contract version.
* :data:`VALID_RELATION_TYPES` – the set of valid ``relation_type`` values.
* :class:`SchemaVersion` – a ``(event_kind, version)`` pair.
* :class:`SchemaRegistry` – a registry that maps event kinds to schema
  versions and validator callables.

The registry is deliberately lightweight – it stores validator callables
rather than performing structural schema validation itself.  Downstream
packages can register JSON-Schema validators, pydantic models, or any
``Callable[[dict], list[str]]`` that returns a list of error strings.

Schema Evolution Policy
-----------------------
The canonical envelope is a closed, versioned structure. During development,
contract changes are applied directly to the current schema and all producers,
consumers, schemas, examples, and tests move together. Unknown top-level
structure fields may be ignored by decoders; extensible payload and metadata
mappings are the preservation boundary for producer-defined data.

Long-term cross-version guarantees are intentionally undefined until MEDRE
publishes a stable release contract.
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
        Positive version number for the kind's payload schema.
    """

    event_kind: str
    version: int

    def __post_init__(self) -> None:
        if not _is_valid_schema_version(self.version):
            raise ValueError("version must be a positive integer")


def _is_valid_schema_version(value: object) -> bool:
    """Return whether *value* is a supported schema-version identifier."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def schema_version_from_event(
    event_kind: str, payload: dict[str, object]
) -> SchemaVersion:
    """Extract a :class:`SchemaVersion` from a raw event payload.

    When ``"schema_version"`` is absent, the current version is used. When
    present, the value MUST be a positive integer; invalid explicit values are
    rejected rather than reinterpreted as the current version.

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
    if "schema_version" not in payload:
        return SchemaVersion(
            event_kind=event_kind,
            version=CURRENT_SCHEMA_VERSION,
        )
    raw = payload["schema_version"]
    if not _is_valid_schema_version(raw):
        raise ValueError("schema_version must be a positive integer")
    return SchemaVersion(event_kind=event_kind, version=raw)


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
        if not _is_valid_schema_version(schema_version):
            raise ValueError("schema_version must be a positive integer")
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
        if not _is_valid_schema_version(schema_version):
            raise ValueError("schema_version must be a positive integer")
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
        if not _is_valid_schema_version(schema_version):
            return None
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
            try:
                sv = schema_version_from_event(event_kind, payload)
            except ValueError as exc:
                if errors is not None:
                    errors.append(str(exc))
                return False
            version = sv.version
        else:
            if not _is_valid_schema_version(schema_version):
                if errors is not None:
                    errors.append("schema_version must be a positive integer")
                return False
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
