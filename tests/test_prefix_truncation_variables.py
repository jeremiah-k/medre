"""Truncation variables in the shared relay-prefix formatter.

``format_relay_prefix`` accepts a ``truncatable`` declaration: each
declared name additionally resolves ``{nameN}`` placeholders to the
first N Unicode code points of its value. Adapters declare truncation
only where constrained displays make it useful; undeclared stems treat
numeric suffixes as unknown placeholders.
"""

from medre.core.rendering.attribution import (
    RelayAttribution,
    format_relay_prefix,
)


def _attr(**kwargs: object) -> RelayAttribution:
    return RelayAttribution(**kwargs)  # type: ignore[arg-type]


class TestTruncationRendering:
    def test_truncation_renders_first_n_code_points(self) -> None:
        attr = _attr(source_sender_label="BobSmith")
        result = format_relay_prefix("{sender5}[M]: ", attr, truncatable=("sender",))
        assert result.rendered_prefix == "BobSm[M]: "
        assert result.formatting_error is None
        assert result.variables_used == ("sender5",)

    def test_truncation_counts_code_points_not_bytes(self) -> None:
        attr = _attr(source_sender_label="\U0001f47eIngrid")
        result = format_relay_prefix("{sender2}", attr, truncatable=("sender",))
        assert result.rendered_prefix == "\U0001f47eI"

    def test_value_shorter_than_suffix_renders_whole_value(self) -> None:
        attr = _attr(source_sender_label="AN")
        result = format_relay_prefix("{sender5}", attr, truncatable=("sender",))
        assert result.rendered_prefix == "AN"
        assert result.missing_variables == ()

    def test_canonical_stem_name_is_declared(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix(
            "{source_sender_label4}", attr, truncatable=("source_sender_label",)
        )
        assert result.rendered_prefix == "Marg"

    def test_plain_variable_unaffected_by_declaration(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix("{sender}", attr, truncatable=("sender",))
        assert result.rendered_prefix == "Margaret"


class TestTruncationGating:
    def test_suffix_zero_is_unknown(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix("{sender0}", attr, truncatable=("sender",))
        assert result.rendered_prefix == "{sender0}"
        assert result.unknown_variables == ("sender0",)
        assert result.formatting_error is not None

    def test_undeclared_stem_is_unknown(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix("{sender5}", attr, truncatable=("origin_label",))
        assert result.rendered_prefix == "{sender5}"
        assert result.unknown_variables == ("sender5",)

    def test_no_declaration_rejects_every_suffix(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix("{sender5}", attr)
        assert result.unknown_variables == ("sender5",)

    def test_declared_stem_with_empty_value_is_missing(self) -> None:
        attr = _attr(source_sender_label=None)
        result = format_relay_prefix("{sender5}:", attr, truncatable=("sender",))
        assert result.rendered_prefix == ":"
        assert result.missing_variables == ("sender5",)
        assert result.formatting_error is None


class TestTruncatableValidation:
    def test_invalid_truncatable_name_sets_error_without_raising(self) -> None:
        attr = _attr(source_sender_label="Margaret")
        result = format_relay_prefix("{sender}", attr, truncatable=("bogus", "sender"))
        assert result.rendered_prefix == "Margaret"
        assert "invalid truncatable name(s): bogus" in (result.formatting_error or "")

    def test_invalid_truncatable_names_are_sorted_in_error(self) -> None:
        attr = _attr()
        result = format_relay_prefix("x", attr, truncatable=("zeta", "alpha"))
        assert "invalid truncatable name(s): alpha, zeta" in (
            result.formatting_error or ""
        )
