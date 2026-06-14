"""Integration tests for the runtime-to-pipeline sender-projection wiring.

These tests lock the production wiring contract that
:class:`~medre.runtime.builder.RuntimeBuilder` constructs a
:class:`~medre.core.engine.pipeline.runner.PipelineRunner` whose
``project_sender_metadata_fn`` is the real adapter-dispatch closure built
by :func:`medre.runtime.builder._build_project_sender_metadata_fn`, and
that the runner forwards that closure into
:class:`~medre.core.planning.relation_enricher.RelationEnricher` when
enriching relations for a target adapter.

The sibling suite ``tests/test_relation_enricher_projected_sender.py``
exercises :class:`RelationEnricher` directly with a hand-rolled
``_real_projection_fn`` mirror of the runtime closure.  That proves the
enricher honours an injected callback, but it does not prove that the
runtime actually wires one in production.  These tests close that gap by
building a real :class:`~medre.runtime.app.MedreApp` and exercising the
built runner.

Why we inspect the built runner's private callback and swap its
enricher's storage rather than driving a full ingress through a started
app: a full ingress requires starting storage, adapters, and the event
bus, and would couple this wiring assertion to adapter lifecycle, route
matching, and rendering — none of which are what this test is locking.
Inspecting ``pipeline_runner._project_sender_metadata_fn`` and replacing
``pipeline_runner._relation_enricher`` with a deterministic in-memory
storage isolates the wiring contract (builder → runner → enricher
callback forwarding) from unrelated subsystems.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.events.canonical import CanonicalEvent, EventRelation
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.planning.relation_enricher import RelationEnricher
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.runtime_builder import clean_path_env, make_all_enabled_config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_path_env(monkeypatch)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


@pytest.fixture()
def built_app(tmp_paths: MedrePaths) -> MedreApp:
    """A MedreApp built from the all-enabled fake-adapter config.

    The config registers four fake adapters so the source-attribution
    registry (adapter_id → platform) is populated.  ``mesh_radio`` maps
    to platform ``meshtastic``.
    """
    config = make_all_enabled_config()
    builder = RuntimeBuilder(config, tmp_paths)
    return builder.build()


class _FakeStorage:
    """Minimal storage stub returning canned events by id.

    Mirrors the duck-typed storage shape consumed by
    :class:`RelationEnricher` (``get`` and
    ``list_native_refs_for_event``).  No native refs are needed for
    sender-projection assertions, so the list call returns an empty
    list.
    """

    def __init__(self, events: dict[str, CanonicalEvent]) -> None:
        self._events = events

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_native_refs_for_event(self, event_id: str) -> list[Any]:
        return []


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_meshtastic_target() -> CanonicalEvent:
    """A target event carrying Meshtastic namespaced identity metadata."""
    return CanonicalEvent(
        event_id="target-mesh-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="mesh_radio",
        source_transport_id="!1234",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "mesh hello", "text": "mesh hello"},
        metadata=EventMetadata(
            native=NativeMetadata(
                data={
                    "meshtastic.from_id": "!1234",
                    "meshtastic.longname": "Alice Node",
                    "meshtastic.shortname": "AlN",
                    "meshtastic.packet_id": 42,
                    "meshtastic.channel": 0,
                }
            )
        ),
    )


def _make_reply_event(target_event_id: str) -> CanonicalEvent:
    rel = EventRelation(
        relation_type="reply",
        target_event_id=target_event_id,
        target_native_ref=None,
        key=None,
        fallback_text=None,
        metadata={},
    )
    return CanonicalEvent(
        event_id="src-reply-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src",
        source_transport_id="src-node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": "a reply"},
        metadata=EventMetadata(),
    )


# ---------------------------------------------------------------------------
# Wiring: builder constructs runner with a non-None projection callback
# ---------------------------------------------------------------------------


def test_built_runner_has_project_sender_metadata_fn(built_app: MedreApp) -> None:
    """RuntimeBuilder wires a non-None projection callback on the runner.

    Without this, relation enrichment would fall back to the generic
    ``source_transport_id`` field and never populate
    ``original_sender_displayname`` from adapter-projected labels.
    """
    runner = built_app.pipeline_runner
    assert runner._project_sender_metadata_fn is not None


# ---------------------------------------------------------------------------
# Wiring: the built callback is the real adapter-dispatch closure
# ---------------------------------------------------------------------------


def test_built_callback_projects_meshtastic_namespaced_identity(
    built_app: MedreApp,
) -> None:
    """The runtime-wired closure delegates to adapter attribution dispatch.

    Feeding a Meshtastic target event (namespaced identity keys) through
    the built callback must yield the generic projected fields produced
    by ``project_meshtastic_attribution``.  This proves the closure is
    the real adapter-dispatch path, not a stub or ``None``.
    """
    runner = built_app.pipeline_runner
    target = _make_meshtastic_target()

    project_fn = runner._project_sender_metadata_fn
    assert project_fn is not None
    projected = project_fn(target)

    assert projected.get("source_platform") == "meshtastic"
    assert projected.get("source_sender_id") == "!1234"
    assert projected.get("source_sender_label") == "Alice Node"
    assert projected.get("source_sender_short_label") == "AlN"


# ---------------------------------------------------------------------------
# Wiring: runner forwards the callback into relation enrichment
# ---------------------------------------------------------------------------


async def test_runner_enrichment_uses_wired_projection(built_app: MedreApp) -> None:
    """The runner forwards its production callback to RelationEnricher.

    A reply whose target carries Meshtastic namespaced identity metadata
    must enrich with ``original_sender_displayname`` and
    ``original_sender`` sourced from the projected generic fields — not
    from native identity keys read by core.  The runner's
    ``_enrich_relations_for_target`` forwards
    ``self._project_sender_metadata_fn`` to the enricher, which is the
    production code path exercised at ingress.

    The runner's built SQLite storage is replaced with a deterministic
    in-memory stub so this test does not depend on storage lifecycle or
    adapter startup; the runner itself and its wired callback remain the
    production instances.
    """
    runner = built_app.pipeline_runner
    target = _make_meshtastic_target()
    reply = _make_reply_event("target-mesh-001")

    # Swap only the enricher's storage; keep the runner's wired callback.
    runner._relation_enricher = RelationEnricher(
        storage=_FakeStorage({"target-mesh-001": target}),
    )

    enriched = await runner._enrich_relations_for_target(
        reply,
        target_adapter="mesh_radio",
        target_channel="0",
    )

    relation = enriched.relations[0]
    # Display name comes from the projected meshtastic.longname, not from
    # a native key read by core planning.
    assert relation.metadata.get("original_sender_displayname") == "Alice Node"
    # Sender id comes from the projected meshtastic.from_id.
    assert relation.metadata.get("original_sender") == "!1234"
