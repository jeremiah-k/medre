"""Static guard: relation_enricher.py has no transport-native identity chains.

Locks the layering contract by inspecting the source text of
``src/medre/core/planning/relation_enricher.py`` to ensure it contains
no references to transport-native identity keys (``displayname``,
``meshtastic.longname``, bare ``longname``, or bare ``sender`` as a
native-data key).

This guard complements the behavioural tests in
``test_relation_enricher.py`` and
``test_relation_enricher_projected_sender.py`` by failing fast at
collection time if a future change reintroduces native identity
interpretation into core planning.

The guard is grep-based rather than AST-based because the relevant
property is the *absence* of certain string literals and attribute
accesses; an AST walk would still miss string-form dict lookups.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_RELATED_ENRICHER_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "medre"
    / "core"
    / "planning"
    / "relation_enricher.py"
)

# Lines in relation_enricher.py that legitimately mention the native
# keys are confined to docstrings and comments (which describe what the
# module does NOT do).  The guard matches whole source lines so a
# violation is any line that *uses* one of these keys as a dict lookup
# or attribute access in executable code.
#
# Each entry is a substring that must NOT appear in the file's source.
# The substrings are narrow enough to avoid false positives from
# docstring references (which use the keys in quoted form, e.g.
# ``"meshtastic.longname"`` inside an rST list).
_FORBIDDEN_EXECUTABLE_PATTERNS: tuple[str, ...] = (
    # Direct dict lookups against target native data.
    'target_data.get("displayname")',
    'target_data.get("meshtastic.longname")',
    'target_data.get("longname")',
    'target_data.get("sender")',
    # Alternative bracket-form lookups.
    'target_data["displayname"]',
    'target_data["meshtastic.longname"]',
    'target_data["longname"]',
    'target_data["sender"]',
)


@pytest.fixture(scope="module")
def enricher_source() -> str:
    """Return the full source text of relation_enricher.py."""
    assert (
        _RELATED_ENRICHER_PATH.exists()
    ), f"relation_enricher.py not found at {_RELATED_ENRICHER_PATH}"
    return _RELATED_ENRICHER_PATH.read_text(encoding="utf-8")


class TestNoNativeIdentityFallbackChains:
    """relation_enricher.py must not interpret native identity keys."""

    @pytest.mark.parametrize("pattern", _FORBIDDEN_EXECUTABLE_PATTERNS)
    def test_no_native_identity_dict_lookup(
        self, enricher_source: str, pattern: str
    ) -> None:
        """No executable line may look up a native identity key directly.

        Violations indicate that core planning is interpreting
        transport-native identity (``displayname``,
        ``meshtastic.longname``, bare ``longname``, or bare ``sender``)
        rather than consuming generic projected fields.
        """
        assert pattern not in enricher_source, (
            f"Forbidden native identity lookup '{pattern}' found in "
            f"relation_enricher.py — core planning must consume generic "
            f"projected sender fields via SenderProjectionFn instead."
        )

    def test_no_target_data_native_lookup_block(self, enricher_source: str) -> None:
        """No surviving ``target_data = ...`` block reading native metadata.

        The previous implementation built a ``target_data`` dict from
        ``event.metadata.native.data`` and read native identity keys
        from it.  The new implementation reads only generic projected
        fields via :data:`SenderProjectionFn` and the generic
        ``source_transport_id`` attribute.  Any re-introduction of a
        ``target_data`` variable bound to native metadata is a layering
        regression.
        """
        # ``target_data`` was the local variable that held the native
        # metadata dict.  It must not appear as an assignment target.
        forbidden_assignment = "target_data ="
        assert forbidden_assignment not in enricher_source, (
            "Found 'target_data =' assignment in relation_enricher.py — "
            "core planning must not extract native metadata for identity "
            "interpretation.  Use SenderProjectionFn instead."
        )

    def test_no_native_data_extraction_in_enrichment(
        self, enricher_source: str
    ) -> None:
        """No extraction of ``event.metadata.native.data`` in the enricher.

        The enricher must not reach into the target event's native
        metadata dict directly.  Identity projection is the runtime's
        responsibility (via ``project_source_fields``); core consumes
        only the generic projected dict and the ``source_transport_id``
        attribute.
        """
        # The previous code did:
        #   _tn = getattr(target_event, "metadata", None)
        #   if _tn is not None:
        #       _tn = getattr(_tn, "native", None)
        #   ...
        #       target_data = _td
        #
        # The new code does not chase the ``.native`` attribute chain
        # at all.  Guard against its return.
        native_chain = 'getattr(_tn, "native", None)'
        assert native_chain not in enricher_source, (
            "Found native metadata chain access "
            '(`getattr(_tn, "native", None)`) in relation_enricher.py — '
            "core planning must not traverse target event native metadata "
            "directly.  Wire a SenderProjectionFn instead."
        )

    def test_sender_projection_fn_parameter_present(self, enricher_source: str) -> None:
        """The enrich_for_target signature must accept a projection callback.

        This is the positive half of the layering contract: native
        identity fallback is replaced by an injection point.
        """
        assert (
            "project_sender_fn: SenderProjectionFn | None = None" in enricher_source
        ), (
            "SenderProjectionFn parameter missing from enrich_for_target — "
            "the generic projection injection point is required."
        )

    def test_generic_source_transport_id_fallback_preserved(
        self, enricher_source: str
    ) -> None:
        """The generic ``source_transport_id`` terminal fallback is preserved.

        The fallback must read from the target event attribute
        (generic, adapter-neutral) and not from any native identity key.
        """
        assert "source_transport_id" in enricher_source, (
            "Generic source_transport_id terminal fallback missing from "
            "relation_enricher.py — required for the no-projection path."
        )
