"""Tests for the strict YAML parser in medre.config._yaml.

These tests verify that the parser accepts a boring YAML subset and
rejects every unsafe or surprising construct:

- duplicate mapping keys
- custom tags (``!!python/*``, ``!!binary``, ``!!set``, ``!!omap``)
- anchors (``&name``) and aliases (``*name``)
- merge keys (``<<``)
- non-mapping top-level documents (lists, scalars, null)
- multi-document streams
- unsafe parse errors without leaking secret values

Error messages must include line/column/path where practical but must
never echo back secret values from the config file.
"""

from __future__ import annotations

import re

import pytest
import yaml

from medre.config._yaml import StrictYAMLError, parse_yaml_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(text: str, source: str = "test.yaml") -> dict:
    """Convenience wrapper around parse_yaml_config."""
    return parse_yaml_config(text, source)


# ---------------------------------------------------------------------------
# Valid YAML accepted
# ---------------------------------------------------------------------------

# --- TestValidYAMLAccepted: The boring YAML subset parses into plain Python data. ---


def test_simple_mapping() -> None:
    data = _parse("a: 1\nb: hello\n")
    assert data == {"a": 1, "b": "hello"}


def test_nested_mappings() -> None:
    data = _parse("outer:\n  inner: value\n")
    assert data == {"outer": {"inner": "value"}}


def test_list_of_scalars() -> None:
    data = _parse("items:\n  - one\n  - two\n  - three\n")
    assert data == {"items": ["one", "two", "three"]}


def test_list_of_mappings() -> None:
    data = _parse("routes:\n  - source: a\n    dest: b\n  - source: c\n    dest: d\n")
    assert data == {
        "routes": [
            {"source": "a", "dest": "b"},
            {"source": "c", "dest": "d"},
        ]
    }


def test_int_float_bool_null_types() -> None:
    data = _parse(
        "int_val: 42\n"
        "float_val: 3.14\n"
        "bool_val: true\n"
        "null_val: null\n"
        "str_val: hello\n"
    )
    assert data["int_val"] == 42 and isinstance(data["int_val"], int)
    assert data["float_val"] == 3.14 and isinstance(data["float_val"], float)
    assert data["bool_val"] is True
    assert data["null_val"] is None
    assert data["str_val"] == "hello"


def test_flow_mapping() -> None:
    data = _parse("config: {a: 1, b: 2}\n")
    assert data == {"config": {"a": 1, "b": 2}}


def test_flow_sequence() -> None:
    data = _parse("items: [1, 2, 3]\n")
    assert data == {"items": [1, 2, 3]}


def test_quoted_string_with_special_chars() -> None:
    data = _parse('room: "!room:test"\n')
    assert data == {"room": "!room:test"}


def test_empty_mapping() -> None:
    """An empty but explicitly-mapping document is still a dict."""
    data = _parse("{}\n")
    assert data == {}


def test_returns_dict_type() -> None:
    data = _parse("a: 1\n")
    assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Duplicate keys rejected
# ---------------------------------------------------------------------------

# --- TestDuplicateKeysRejected: Duplicate mapping keys are rejected at parse time. ---


def test_top_level_duplicate_string_key() -> None:
    with pytest.raises(StrictYAMLError, match="duplicate mapping key"):
        _parse("a: 1\na: 2\n")


def test_nested_duplicate_key() -> None:
    with pytest.raises(StrictYAMLError, match="duplicate mapping key"):
        _parse("outer:\n  x: 1\n  x: 2\n")


def test_duplicate_key_error_includes_line() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("a: 1\na: 2\n", "myconfig.yaml")
    msg = str(exc_info.value)
    # Line 2, column 1 for the second 'a'
    assert "myconfig.yaml" in msg
    assert ":2:" in msg


def test_duplicate_int_key() -> None:
    with pytest.raises(StrictYAMLError, match="duplicate mapping key"):
        _parse("1: a\n1: b\n")


def test_three_duplicates_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="duplicate mapping key"):
        _parse("x: 1\nx: 2\nx: 3\n")


# ---------------------------------------------------------------------------
# Custom tags rejected
# ---------------------------------------------------------------------------

# --- TestCustomTagsRejected: Custom and exotic tags that produce non-plain types are rejected. ---


def test_python_object_apply_tag_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse('!!python/object/apply:os.system ["echo hi"]\n')


def test_python_object_tag_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("!!python/object:__main__.MyClass\n  x: 1\n")


def test_binary_tag_rejected() -> None:
    """!!binary produces bytes, which is not a plain scalar."""
    with pytest.raises(StrictYAMLError, match="unsupported YAML value type bytes"):
        _parse("x: !!binary aGVsbG8=\n")


def test_set_tag_rejected() -> None:
    """!!set produces a set, which is not a plain mapping."""
    with pytest.raises(StrictYAMLError):
        _parse("!!set {a, b, c}\n")


def test_omap_tag_rejected() -> None:
    """!!omap produces a list of tuples, not a plain mapping."""
    with pytest.raises(StrictYAMLError):
        _parse("!!omap [a: 1, b: 2]\n")


def test_unknown_custom_tag_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("!mytag value\n")


def test_nested_binary_tag_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("data:\n  secret: !!binary aGVsbG8=\n")


# ---------------------------------------------------------------------------
# Anchors and aliases rejected
# ---------------------------------------------------------------------------

# --- TestAnchorsAliasesRejected: YAML anchors (&) and aliases (*) are rejected. ---


def test_anchor_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="anchors"):
        _parse("x: &a\n  k: v\n")


def test_alias_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="anchor"):
        _parse("x: &a\n  k: v\ny: *a\n")


def test_anchor_in_flow_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="anchor"):
        _parse("x: &a {k: v}\n")


def test_anchor_error_includes_line() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("a: 1\nx: &a\n  k: v\n", "cfg.yaml")
    msg = str(exc_info.value)
    assert "cfg.yaml" in msg
    assert ":2:" in msg


# ---------------------------------------------------------------------------
# Merge keys rejected
# ---------------------------------------------------------------------------

# --- TestMergeKeysRejected: YAML merge keys (<<) are rejected. ---


def test_block_merge_key_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="merge keys"):
        _parse("a: 1\n<<:\n  b: 2\n")


def test_flow_merge_key_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("{<<: {b: 2}, a: 1}\n")


def test_merge_does_not_inherit_values() -> None:
    """The merge key must not silently inject values."""
    with pytest.raises(StrictYAMLError):
        _parse("base: &ignored\n  x: 1\nchild:\n  <<: base\n")


# ---------------------------------------------------------------------------
# Non-mapping top-level rejected
# ---------------------------------------------------------------------------

# --- TestNonMappingTopLevelRejected: The top-level YAML document must be a mapping. ---


def test_top_level_list_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="top level must be a mapping"):
        _parse("- a\n- b\n")


def test_top_level_scalar_string_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="top level must be a mapping"):
        _parse("hello world\n")


def test_top_level_int_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="top level must be a mapping"):
        _parse("42\n")


def test_top_level_null_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="empty"):
        _parse("\n")


def test_top_level_explicit_null_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="empty|top level"):
        _parse("null\n")


def test_top_level_flow_sequence_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="top level must be a mapping"):
        _parse("[1, 2, 3]\n")


# ---------------------------------------------------------------------------
# Multi-document streams rejected
# ---------------------------------------------------------------------------

# --- TestMultiDocumentRejected: Only a single YAML document is accepted. ---


def test_two_documents_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("---\na: 1\n---\nb: 2\n")


def test_three_documents_rejected() -> None:
    with pytest.raises(StrictYAMLError):
        _parse("---\na: 1\n---\nb: 2\n---\nc: 3\n")


# ---------------------------------------------------------------------------
# Secret redaction in parse errors
# ---------------------------------------------------------------------------

# --- TestSecretRedaction: Parse-error messages must not echo secret values from the config. ---


def test_parse_error_does_not_leak_nearby_secret() -> None:
    """A syntax error on a later line must not leak the token value."""
    secret_value = "SUPER_SECRET_TOKEN_12345"
    text = f"access_token: {secret_value}\nbroken: [\n"
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse(text, "secret.yaml")
    msg = str(exc_info.value)
    assert secret_value not in msg, f"Secret value leaked in error message: {msg}"


def test_parse_error_does_not_leak_password() -> None:
    password = "my-secret-password"
    text = f"password: {password}\nbroken: [\n"
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse(text, "pw.yaml")
    msg = str(exc_info.value)
    assert password not in msg


def test_duplicate_key_error_shows_key_name_not_value() -> None:
    """When a secret key is duplicated, the key NAME is shown but the
    key NAME is redacted in the error message."""
    text = "access_token: secret1\naccess_token: secret2\n"
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse(text, "redact.yaml")
    msg = str(exc_info.value)
    # The error should reference "redacted" for the secret key name
    assert "redacted" in msg.lower()
    # The actual values must not appear
    assert "secret1" not in msg
    assert "secret2" not in msg


def test_non_secret_duplicate_key_shown_in_plain() -> None:
    """Non-secret keys are shown as-is in duplicate-key errors."""
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("timeout: 1\ntimeout: 2\n", "plain.yaml")
    msg = str(exc_info.value)
    assert "timeout" in msg


# ---------------------------------------------------------------------------
# Error location info
# ---------------------------------------------------------------------------

# --- TestErrorLocationInfo: Error messages include source path, line, and column where practical. ---


def test_duplicate_key_has_line_column() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("a: 1\nb: 2\na: 3\n", "loc.yaml")
    msg = str(exc_info.value)
    # Third line, first column for the duplicate 'a'
    assert "loc.yaml" in msg
    assert re.search(r":3:\d", msg)


def test_anchor_has_source_path() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("x: &a\n  k: v\n", "path/to/cfg.yaml")
    msg = str(exc_info.value)
    assert "path/to/cfg.yaml" in msg


def test_merge_key_has_line_column() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("a: 1\n<<:\n  b: 2\n", "merge.yaml")
    msg = str(exc_info.value)
    assert "merge.yaml" in msg
    assert re.search(r":2:\d", msg)


def test_syntax_error_has_location() -> None:
    with pytest.raises(StrictYAMLError) as exc_info:
        _parse("a: [unclosed\n", "syntax.yaml")
    msg = str(exc_info.value)
    assert "syntax.yaml" in msg


# ---------------------------------------------------------------------------
# StrictYAMLError inheritance
# ---------------------------------------------------------------------------

# --- TestErrorInheritance: StrictYAMLError inherits from ConfigFileError for backward compat. ---


def test_strict_error_is_config_file_error() -> None:
    from medre.config.errors import ConfigFileError

    with pytest.raises(ConfigFileError):
        _parse("- not a mapping\n")


def test_strict_error_is_config_error() -> None:
    from medre.config.errors import ConfigError

    with pytest.raises(ConfigError):
        _parse("- not a mapping\n")


# ---------------------------------------------------------------------------
# Alias without a preceding anchor (fetch_alias rejection path)
# ---------------------------------------------------------------------------

# --- TestAliasWithoutAnchor: A bare alias (``*name``) with no preceding anchor ---
# reaches the alias scanner rejection directly.
#
# The existing anchor/alias tests pair ``&a`` then ``*a``, so the anchor
# scanner (:meth:`fetch_anchor`) raises before the alias scanner
# (:meth:`fetch_alias`) ever runs.  A lone alias exercises the alias
# branch on its own.


def test_alias_value_without_anchor_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="aliases"):
        _parse("value: *missing\n")


def test_alias_in_flow_without_anchor_rejected() -> None:
    with pytest.raises(StrictYAMLError, match="aliases"):
        _parse("key: {b: *x}\n")


# ---------------------------------------------------------------------------
# construct_mapping defensive guards and key validation
# ---------------------------------------------------------------------------

# --- TestConstructMappingGuards: Cover the defensive guards and key-validation block ---
# in :meth:`_StrictSafeLoader.construct_mapping` that are hard to reach
# through normal parsing.


def test_non_mapping_node_rejected() -> None:
    """A non-MappingNode handed to construct_mapping is rejected.

    Also exercises ``_format_mark`` with a ``None`` mark, since a
    hand-built ``ScalarNode`` defaults ``start_mark`` to ``None``.
    """
    from medre.config._yaml import _StrictSafeLoader

    loader = _StrictSafeLoader("")
    node = yaml.ScalarNode("tag:yaml.org,2002:str", "x")
    with pytest.raises(StrictYAMLError, match="expected a mapping"):
        loader.construct_mapping(node)


def test_format_mark_none_returns_config_prefix() -> None:
    from medre.config._yaml import _format_mark

    assert _format_mark(None, "boom") == "<config>: boom"


def test_bool_key_passes_hashable_check() -> None:
    """A boolean key enters the leaf-type/bool branch (the no-op pass
    block) and then passes the hashable check, so the document loads."""
    data = _parse("true: value\n")
    assert data == {True: "value"}


def test_complex_sequence_key_rejected() -> None:
    """A YAML complex key that parses to a list is rejected by the
    leaf-type guard before the hashable-key defense-in-depth check is
    reached."""
    with pytest.raises(StrictYAMLError, match="unsupported mapping key type"):
        _parse("? [a, b]\n: v\n")


# ---------------------------------------------------------------------------
# _sanitize_yaml_error branch coverage
# ---------------------------------------------------------------------------

# --- TestSanitizeYamlError: Cover the problem/context branches of the YAML error sanitizer. ---


def test_problem_branch_included() -> None:
    from medre.config._yaml import _sanitize_yaml_error

    exc = yaml.YAMLError()
    exc.problem = "could not find expected ':'"
    msg = _sanitize_yaml_error(exc, "cfg.yaml")
    assert "could not find expected ':'" in msg


def test_context_branch_included() -> None:
    from medre.config._yaml import _sanitize_yaml_error

    exc = yaml.YAMLError()
    exc.context = "while parsing a block mapping"
    msg = _sanitize_yaml_error(exc, "cfg.yaml")
    assert "while parsing a block mapping" in msg


def test_no_problem_or_context_uses_default() -> None:
    from medre.config._yaml import _sanitize_yaml_error

    exc = yaml.YAMLError()
    msg = _sanitize_yaml_error(exc, "cfg.yaml")
    assert "YAML parse error" in msg
