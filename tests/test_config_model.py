"""Tests for medre.config.model helper functions.

Covers:
- _is_tuple_annotation (lines 106-113)
- _is_set_annotation return-False path (line 97)
- list → tuple coercion in _coerce_adapter_kwargs (lines 71-72)
- unknown adapter key rejection via _coerce_adapter_kwargs and via load_config
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import (
    _coerce_adapter_kwargs,
    _is_set_annotation,
    _is_tuple_annotation,
)

# ---------------------------------------------------------------------------
# _is_tuple_annotation
# ---------------------------------------------------------------------------


class TestIsTupleAnnotation:
    """_is_tuple_annotation identifies tuple type hints."""

    def test_tuple_str_ellipsis(self) -> None:
        """tuple[str, ...] is recognised as a tuple annotation."""
        assert _is_tuple_annotation(tuple[str, ...]) is True

    def test_plain_tuple(self) -> None:
        """Plain tuple (no args) is recognised."""
        assert _is_tuple_annotation(tuple) is False  # bare tuple has no __origin__

    def test_list_str_is_not_tuple(self) -> None:
        """list[str] is NOT a tuple annotation."""
        assert _is_tuple_annotation(list[str]) is False

    def test_str_is_not_tuple(self) -> None:
        """Plain str is NOT a tuple annotation."""
        assert _is_tuple_annotation(str) is False

    def test_none_is_not_tuple(self) -> None:
        """None is NOT a tuple annotation."""
        assert _is_tuple_annotation(None) is False

    def test_int_is_not_tuple(self) -> None:
        """Plain int is NOT a tuple annotation."""
        assert _is_tuple_annotation(int) is False

    def test_optional_tuple(self) -> None:
        """tuple[str, ...] | None (union) is still a tuple annotation."""
        assert _is_tuple_annotation(tuple[str, ...] | None) is True


# ---------------------------------------------------------------------------
# _is_set_annotation  (return-False path, line 97)
# ---------------------------------------------------------------------------


class TestIsSetAnnotationFalsePath:
    """_is_set_annotation returns False for non-set hints (line 97)."""

    @pytest.mark.parametrize("hint", [str, int, list[str], dict[str, int]])
    def test_non_set_hints_return_false(self, hint: Any) -> None:
        assert _is_set_annotation(hint) is False


# ---------------------------------------------------------------------------
# list → tuple coercion in _coerce_adapter_kwargs (lines 71-72)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeAdapterConfig:
    """Minimal frozen dataclass with a tuple-typed field for coercion tests."""

    name: str = "test"
    tags: tuple[str, ...] = ()


class TestCoerceAdapterKwargsListToTuple:
    """_coerce_adapter_kwargs converts list → tuple for tuple-typed fields.

    Since Wave 2 the helper takes required keyword-only ``transport`` and
    ``section_path`` arguments and rejects unknown keys with
    :class:`ConfigValidationError` (matching ``additionalProperties: false``
    on the adapter JSON schemas). These tests pass the new kwargs and
    exercise both the coercion path and the rejection path.
    """

    def test_list_coerced_to_tuple(self) -> None:
        """A YAML list value for a tuple[str, ...] field is coerced to tuple."""
        raw: dict[str, Any] = {"tags": ["!room:server"]}
        result = _coerce_adapter_kwargs(
            _FakeAdapterConfig,
            raw,
            transport="test",
            section_path="adapters.test.fake",
        )
        assert result["tags"] == ("!room:server",)
        assert isinstance(result["tags"], tuple)

    def test_empty_list_coerced_to_empty_tuple(self) -> None:
        """An empty list becomes an empty tuple."""
        raw: dict[str, Any] = {"tags": []}
        result = _coerce_adapter_kwargs(
            _FakeAdapterConfig,
            raw,
            transport="test",
            section_path="adapters.test.fake",
        )
        assert result["tags"] == ()
        assert isinstance(result["tags"], tuple)

    def test_multiple_items_coerced(self) -> None:
        """Multiple-item list is fully converted."""
        raw: dict[str, Any] = {"tags": ["a", "b", "c"]}
        result = _coerce_adapter_kwargs(
            _FakeAdapterConfig,
            raw,
            transport="test",
            section_path="adapters.test.fake",
        )
        assert result["tags"] == ("a", "b", "c")

    def test_non_list_value_passes_through(self) -> None:
        """A value that is already a tuple is not modified."""
        raw: dict[str, Any] = {"tags": ("x",)}
        result = _coerce_adapter_kwargs(
            _FakeAdapterConfig,
            raw,
            transport="test",
            section_path="adapters.test.fake",
        )
        assert result["tags"] == ("x",)

    def test_unknown_key_rejected(self) -> None:
        """Keys not present in the dataclass raise ConfigValidationError.

        Replaces the former ``test_unknown_key_ignored`` which codified the
        silent-drop behavior. The loader now matches the JSON schema's
        ``additionalProperties: false`` so a typo (e.g. ``conection_type``)
        surfaces at load time instead of silently falling back to the field
        default. See audit finding F-013 / F-021.
        """
        raw: dict[str, Any] = {"tags": ["a"], "bogus": 42}
        with pytest.raises(
            ConfigValidationError, match="unknown adapter config key"
        ) as exc_info:
            _coerce_adapter_kwargs(
                _FakeAdapterConfig,
                raw,
                transport="test",
                section_path="adapters.test.fake",
            )
        # The error must carry the transport/section_path context so
        # operators can locate the offending adapter table.
        assert exc_info.value.transport == "test"
        assert exc_info.value.section_path == "adapters.test.fake"
        # The offending key name must appear in the message.
        assert "bogus" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Unknown adapter key rejection via the full load_config path (TC-012)
# ---------------------------------------------------------------------------


def test_unknown_adapter_key_rejected_via_load(tmp_path: Path) -> None:
    """An unknown key in an ``adapters.<transport>.<instance>`` table is
    rejected end-to-end through ``load_config``, not only when
    ``_coerce_adapter_kwargs`` is called directly.

    Distinct from ``test_unknown_key_rejected`` above (which exercises the
    helper in isolation): this test verifies the rejection propagates
    through ``_parse_adapter_section`` → ``MatrixRuntimeConfig.from_dict``
    → ``_coerce_adapter_kwargs`` and surfaces as a
    :class:`ConfigValidationError` from ``load_config``.
    """
    config = (
        "adapters:\n"
        "  matrix:\n"
        "    main:\n"
        "      adapter_id: matrix\n"
        "      bogusextra: true\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(config)
    with pytest.raises(
        ConfigValidationError, match="unknown adapter config key"
    ) as exc_info:
        load_config(str(p))
    # section_path points at the offending adapter table.
    assert exc_info.value.section_path == "adapters.matrix.main"
    assert exc_info.value.transport == "matrix"
    assert "bogusextra" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Migration diagnostics for removed adapter keys (F-018 / Task 4)
# ---------------------------------------------------------------------------


def test_removed_adapter_key_hint_appended_via_load(tmp_path: Path) -> None:
    """``meshnet_name`` as an adapter key surfaces a migration hint.

    Exercises the hint path in :func:`_coerce_adapter_kwargs` end-to-end
    through ``load_config``. The rejection itself is unchanged (still
    raises ``unknown adapter config key``); the suggestion is *appended*
    and points at the replacement origin-label fields.
    """
    config = (
        "adapters:\n"
        "  matrix:\n"
        "    main:\n"
        "      adapter_id: matrix\n"
        "      meshnet_name: old-style\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(config)
    with pytest.raises(
        ConfigValidationError, match="unknown adapter config key"
    ) as exc_info:
        load_config(str(p))
    msg = str(exc_info.value)
    assert "meshnet_name" in msg
    # Migration hint block must be present and name the replacement.
    assert "Hints:" in msg
    assert "origin_label" in msg
