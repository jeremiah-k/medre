"""Static guard: relation_enricher.py has no transport-native identity chains.

Locks the layering contract by inspecting the source of
``src/medre/core/planning/relation_enricher.py`` to ensure it contains
no references to transport-native identity keys (``displayname``,
``meshtastic.longname``, bare ``longname``, or bare ``sender`` as a
native-data key).

This guard complements the behavioural tests in
``test_relation_enricher.py`` and
``test_relation_enricher_projected_sender.py`` by failing fast at
collection time if a future change reintroduces native identity
interpretation into core planning.

The structural checks (no ``.native`` attribute chain; the
``project_sender_fn`` injection point is present; the generic
``source_transport_id`` fallback is referenced) are AST-based so they
walk the actual ``enrich_for_target`` function body and cannot be
fooled by docstring/comment text.  The narrow dict-literal lookup
patterns are matched as source substrings because they target a
specific executable shape (``target_data.get("displayname")``) that
does not appear in prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_RELATION_ENRICHER_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "medre"
    / "core"
    / "planning"
    / "relation_enricher.py"
)

# Each entry is a substring that must NOT appear in the file's source.
# The substrings target the executable dict-lookup shape
# (``target_data.get("displayname")`` / ``target_data["displayname"]``)
# rather than the bare key names, so docstring references to the keys
# (which use the quoted form inside rST lists) do not trip the guard.
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
        _RELATION_ENRICHER_PATH.exists()
    ), f"relation_enricher.py not found at {_RELATION_ENRICHER_PATH}"
    return _RELATION_ENRICHER_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def enricher_tree(enricher_source: str) -> ast.Module:
    """Return the parsed AST of relation_enricher.py."""
    return ast.parse(enricher_source)


def _enrich_for_target_func(tree: ast.Module) -> ast.AsyncFunctionDef:
    """Return the ``enrich_for_target`` async function definition node.

    Raises ``AssertionError`` if the function is absent, so a future
    rename fails loudly rather than silently vacating the guard.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "enrich_for_target":
            return node
    raise AssertionError(
        "enrich_for_target not found in relation_enricher.py — "
        "the enrichment entry point must exist for the layering guard."
    )


def _references_name(func: ast.AsyncFunctionDef, name: str) -> bool:
    """Return True if *func* references *name* as an attribute or getattr arg.

    Covers both attribute-access forms (``event.<name>``) and
    ``getattr(event, "<name>", ...)`` calls, so the check is robust to
    which spelling the implementation chooses.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "hasattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == name
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Source-substring checks: no executable native-identity dict lookups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", _FORBIDDEN_EXECUTABLE_PATTERNS)
def test_no_native_identity_dict_lookup(enricher_source: str, pattern: str) -> None:
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


def test_no_target_data_native_lookup_block(enricher_source: str) -> None:
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


# ---------------------------------------------------------------------------
# AST-based structural checks on the enrich_for_target function body
# ---------------------------------------------------------------------------


def test_no_native_metadata_chain_in_enrich_for_target(
    enricher_tree: ast.Module,
) -> None:
    """The enricher must not traverse ``.native`` metadata chains.

    The previous code chased ``getattr(_tn, "native", None)`` (and the
    equivalent attribute access) to pull native identity data into core
    planning.  The AST walk of ``enrich_for_target`` must find no such
    reference — neither a ``<expr>.native`` attribute access nor a
    ``getattr(<expr>, "native", ...)`` call — so identity projection
    stays the runtime's responsibility via :data:`SenderProjectionFn`.
    """
    func = _enrich_for_target_func(enricher_tree)
    assert not _references_name(func, "native"), (
        "Found a '.native' attribute/getattr reference inside "
        "enrich_for_target — core planning must not traverse target "
        "event native metadata directly.  Wire a SenderProjectionFn instead."
    )


def test_enrich_for_target_has_sender_projection_fn_parameter(
    enricher_tree: ast.Module,
) -> None:
    """The ``enrich_for_target`` signature must accept a projection callback.

    This is the positive half of the layering contract: native identity
    fallback is replaced by an injection point.  The check inspects the
    actual function arguments (positional and keyword-only) rather than
    matching a source string, so it survives annotation reformatting.
    """
    func = _enrich_for_target_func(enricher_tree)
    arg_names = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
    assert "project_sender_fn" in arg_names, (
        "SenderProjectionFn parameter missing from enrich_for_target — "
        "the generic projection injection point is required."
    )


def test_enrich_for_target_references_source_transport_id(
    enricher_tree: ast.Module,
) -> None:
    """The generic ``source_transport_id`` terminal fallback is referenced.

    The fallback must read the target event's generic, adapter-neutral
    ``source_transport_id`` (the no-projection terminal fallback for
    ``original_sender``) and not any native identity key.  The check
    accepts either spelling — attribute access (``event.source_transport_id``)
    or ``getattr(event, "source_transport_id", ...)`` — by walking the
    function body's AST.
    """
    func = _enrich_for_target_func(enricher_tree)
    assert _references_name(func, "source_transport_id"), (
        "Generic source_transport_id terminal fallback missing from "
        "enrich_for_target — required for the no-projection path."
    )
