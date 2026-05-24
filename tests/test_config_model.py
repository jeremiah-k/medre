"""Tests for medre.config.model helper functions.

Covers:
- _is_tuple_annotation (lines 106-113)
- _is_set_annotation return-False path (line 97)
- list → tuple coercion in _coerce_adapter_kwargs (lines 71-72)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

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
    """_coerce_adapter_kwargs converts list → tuple for tuple-typed fields."""

    def test_list_coerced_to_tuple(self) -> None:
        """A TOML list value for a tuple[str, ...] field is coerced to tuple."""
        raw: dict[str, Any] = {"tags": ["!room:server"]}
        result = _coerce_adapter_kwargs(_FakeAdapterConfig, raw)
        assert result["tags"] == ("!room:server",)
        assert isinstance(result["tags"], tuple)

    def test_empty_list_coerced_to_empty_tuple(self) -> None:
        """An empty list becomes an empty tuple."""
        raw: dict[str, Any] = {"tags": []}
        result = _coerce_adapter_kwargs(_FakeAdapterConfig, raw)
        assert result["tags"] == ()
        assert isinstance(result["tags"], tuple)

    def test_multiple_items_coerced(self) -> None:
        """Multiple-item list is fully converted."""
        raw: dict[str, Any] = {"tags": ["a", "b", "c"]}
        result = _coerce_adapter_kwargs(_FakeAdapterConfig, raw)
        assert result["tags"] == ("a", "b", "c")

    def test_non_list_value_passes_through(self) -> None:
        """A value that is already a tuple is not modified."""
        raw: dict[str, Any] = {"tags": ("x",)}
        result = _coerce_adapter_kwargs(_FakeAdapterConfig, raw)
        assert result["tags"] == ("x",)

    def test_unknown_key_ignored(self) -> None:
        """Keys not present in the dataclass are silently dropped."""
        raw: dict[str, Any] = {"tags": ["a"], "bogus": 42}
        result = _coerce_adapter_kwargs(_FakeAdapterConfig, raw)
        assert "bogus" not in result
        assert result["tags"] == ("a",)
