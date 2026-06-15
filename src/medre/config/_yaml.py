"""Strict YAML parsing for MEDRE configuration.

This module exposes :func:`parse_yaml_config`, which parses YAML config
text into a plain ``dict`` containing only standard Python types
(``dict``, ``list``, ``str``, ``int``, ``float``, ``bool``, ``None``).
The result is safe to feed directly into
:func:`medre.config.loader._parse_runtime_config`.

The parser enforces a deliberately *boring* subset of YAML by rejecting:

- **Duplicate mapping keys** — the last definition silently winning is a
  common misconfiguration source.
- **Anchors** (``&name``) and **aliases** (``*name``) — these create
  implicit shared references that make config review harder and can be
  used in YAML billion-laughs style expansion attacks.
- **Merge keys** (``<<``) — implicit inheritance via ``<<`` hides where
  a value originates.
- **Custom or exotic tags** — anything that would construct a Python
  object other than a plain mapping, sequence, or scalar is rejected.
  ``yaml.SafeLoader`` already blocks ``!!python/*`` and unknown tags;
  the post-parse type walk additionally rejects standard but exotic
  types produced by ``!!binary``, ``!!set``, ``!!omap``, etc.
- **Non-mapping top-level documents** — the root of a MEDRE config must
  be a mapping.
- **Multi-document streams** — only a single YAML document is accepted.

Error messages include the source path, line, and column when available
but **never** include raw file content, so secret values in a malformed
config are not echoed back to the operator.
"""

from __future__ import annotations

from typing import Any

import yaml

from medre.config.errors import ConfigFileError

__all__ = ["StrictYAMLError", "parse_yaml_config"]

# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class StrictYAMLError(ConfigFileError):
    """Raised when YAML config parsing rejects an unsafe or malformed document.

    This inherits from :class:`ConfigFileError` so existing error-handling
    code that catches the config-file error base continues to work.
    """


# ---------------------------------------------------------------------------
# Allowed value types
# ---------------------------------------------------------------------------

# Only plain dict/list/scalar data may appear in a parsed config tree.
# Exotic standard-YAML types (bytes from ``!!binary``, set from ``!!set``,
# list-of-pairs from ``!!omap``, datetime from ``!!timestamp``) are
# rejected by the post-parse type walk below.
_ALLOWED_LEAF_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))

# YAML 1.1 merge-key tag, used to detect ``<<`` keys on mapping nodes.
_MERGE_TAG = "tag:yaml.org,2002:merge"


# ---------------------------------------------------------------------------
# Strict loader subclass
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` subclass enforcing MEDRE's boring YAML subset.

    Anchors and aliases are rejected at the scanner level so that the
    billion-laughs expansion and shared-reference patterns are impossible.
    Merge keys and duplicate keys are rejected at the constructor level.
    """

    # -- Scanner overrides: reject anchors and aliases entirely ------------

    def fetch_anchor(self) -> None:  # type: ignore[override]
        """Reject ``&anchor`` definitions at scan time."""
        raise StrictYAMLError(
            _format_mark(self.get_mark(), "YAML anchors (&) are not supported")
        )

    def fetch_alias(self) -> None:  # type: ignore[override]
        """Reject ``*alias`` references at scan time."""
        raise StrictYAMLError(
            _format_mark(self.get_mark(), "YAML aliases (*) are not supported")
        )

    # -- Constructor override: reject merge keys and duplicate keys --------

    def construct_mapping(  # type: ignore[override]
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        """Build a mapping, rejecting merge keys and duplicate keys.

        This intentionally does **not** call
        :meth:`yaml.SafeConstructor.flatten_mapping`, so ``<<`` merge
        keys are never silently expanded.
        """
        if not isinstance(node, yaml.MappingNode):
            raise StrictYAMLError(
                _format_mark(
                    node.start_mark,
                    f"expected a mapping but found {node.tag}",
                )
            )

        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            # Reject merge keys (``<<``).
            if key_node.tag == _MERGE_TAG:
                raise StrictYAMLError(
                    _format_mark(
                        key_node.start_mark,
                        "YAML merge keys (<<) are not supported",
                    )
                )

            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, _ALLOWED_LEAF_TYPES):
                # Only plain scalar types (str, int, float, bool, None)
                # are permitted as mapping keys for compatibility. Exotic
                # hashable types (tuple, frozenset, etc.) produced by tags
                # like ``!!omap`` or ``!!set`` are rejected to keep config
                # boring and reviewable.
                raise StrictYAMLError(
                    _format_mark(
                        key_node.start_mark,
                        f"unsupported mapping key type {type(key).__name__}; "
                        f"only plain scalar keys are allowed",
                    )
                )
            if not _is_hashable(key):
                raise StrictYAMLError(
                    _format_mark(
                        key_node.start_mark,
                        "mapping key is not hashable",
                    )
                )

            if key in mapping:
                raise StrictYAMLError(
                    _format_mark(
                        key_node.start_mark,
                        f"duplicate mapping key {_redact_key(key)!r}",
                    )
                )

            value = self.construct_object(value_node, deep=deep)
            mapping[key] = value

        return mapping


def _is_hashable(obj: Any) -> bool:
    """Return ``True`` if *obj* is hashable."""
    try:
        hash(obj)
    except TypeError:
        return False
    return True


# ---------------------------------------------------------------------------
# Secret-redaction helpers
# ---------------------------------------------------------------------------

# Keys whose values are treated as secrets in error messages.  Only the
# *key name* is redacted from messages; the key itself is shown to help
# the operator identify which key is duplicated.
_SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "access_token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "token",
        "private_key",
        "client_secret",
    }
)


def _redact_key(key: Any) -> Any:
    """Return a key for inclusion in an error message.

    Key *names* are shown so the operator knows which key is duplicated
    or malformed.  Key names are not secrets themselves — the secret is
    the *value*, which is never present in our strict-parser error
    messages because we raise before values are formatted.
    """
    if isinstance(key, str) and key.lower() in _SECRET_KEY_NAMES:
        return f"<redacted key {key!r}>"
    return key


# ---------------------------------------------------------------------------
# Mark formatting
# ---------------------------------------------------------------------------


def _format_mark(mark: yaml.Mark | None, message: str) -> str:
    """Format a YAML mark into a ``path:line:column: message`` string.

    The raw file buffer / snippet is never included, so secret values
    near the error position are not leaked.
    """
    if mark is None:
        return f"<config>: {message}"
    name = mark.name or "<config>"
    line = mark.line + 1  # YAML marks are 0-based
    column = mark.column + 1
    return f"{name}:{line}:{column}: {message}"


def _sanitize_yaml_error(exc: yaml.YAMLError, source: str) -> str:
    """Extract a clean, secret-safe message from a ``yaml.YAMLError``.

    PyYAML error strings include a context snippet from the file buffer,
    which could contain secret values.  This function extracts the line
    and column from the error's mark and the problem description without
    the buffer snippet.
    """
    parts: list[str] = []

    # ``problem`` / ``problem_mark`` — the primary error location.
    problem = getattr(exc, "problem", None)
    problem_mark = getattr(exc, "problem_mark", None)
    context = getattr(exc, "context", None)
    context_mark = getattr(exc, "context_mark", None)

    if context:
        parts.append(str(context))
    if problem:
        parts.append(str(problem))

    # Location from problem_mark (preferred) or context_mark.
    mark = problem_mark or context_mark
    location = _format_mark(mark, "") if mark else f"{source}: "

    detail = "; ".join(parts) if parts else "YAML parse error"
    # The location string already ends with a trailing space from
    # ``_format_mark``; concatenate without an extra separator.
    return f"{location}{detail}"


# ---------------------------------------------------------------------------
# Post-parse type validation
# ---------------------------------------------------------------------------


def _validate_plain_types(data: Any, path: str) -> None:
    """Recursively assert *data* contains only plain dict/list/scalar types.

    Raises :class:`StrictYAMLError` if any value has an unsupported type
    (e.g. ``bytes`` from ``!!binary``, ``set`` from ``!!set``, ``tuple``
    from ``!!omap``).
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if not isinstance(key, _ALLOWED_LEAF_TYPES):
                raise StrictYAMLError(
                    f"{path or '<root>'}: unsupported mapping key type "
                    f"{type(key).__name__}; only plain scalar keys are allowed"
                )
            child_path = f"{path}.{key}" if path else str(key)
            _validate_plain_types(value, child_path)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            _validate_plain_types(item, f"{path}[{index}]")
    elif isinstance(data, bool):
        # ``bool`` is a subtype of ``int``; explicitly allowed as a leaf.
        return
    elif isinstance(data, _ALLOWED_LEAF_TYPES):
        return
    else:
        raise StrictYAMLError(
            f"{path or '<root>'}: unsupported YAML value type "
            f"{type(data).__name__}; only mappings, sequences, and plain "
            f"scalars are allowed"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_yaml_config(text: str, source: str = "<config>") -> dict[str, Any]:
    """Parse YAML config *text* strictly, returning a plain ``dict``.

    Parameters
    ----------
    text:
        Raw YAML text (already decoded as ``str``).
    source:
        File path or label used in error messages.

    Returns
    -------
    dict[str, Any]
        Parsed config containing only ``dict``, ``list``, ``str``,
        ``int``, ``float``, ``bool``, and ``None`` values.

    Raises
    ------
    StrictYAMLError
        For any rejected YAML construct, parse error, or type violation.
    """
    loader = _StrictSafeLoader(text)
    # Set the Reader's ``name`` so mark-based error messages reference the
    # config file path rather than ``<unicode string>``.
    loader.name = source
    try:
        data = loader.get_single_data()
    except StrictYAMLError:
        raise
    except yaml.YAMLError as exc:
        raise StrictYAMLError(_sanitize_yaml_error(exc, source)) from exc
    finally:
        loader.dispose()

    if data is None:
        raise StrictYAMLError(f"{source}: YAML config document is empty")

    if not isinstance(data, dict):
        raise StrictYAMLError(
            f"{source}: YAML config top level must be a mapping, "
            f"got {type(data).__name__}"
        )

    _validate_plain_types(data, "")

    return data
