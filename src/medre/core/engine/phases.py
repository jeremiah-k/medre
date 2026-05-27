"""Pipeline phase enumeration.

Defines the canonical set of pipeline stages implemented by
:class:`~medre.core.engine.pipeline.PipelineRunner`.  Each value
corresponds to an actual processing boundary inside
:meth:`PipelineRunner.handle_ingress`.

These phases are used for diagnostic instrumentation — logging the
current phase during processing and counting events per phase — without
changing pipeline behaviour.
"""

from __future__ import annotations

from enum import StrEnum


class PipelinePhase(StrEnum):
    """The six implemented pipeline stages.

    Members
    -------
    INGRESS:
        Event received; validation running.
    DEDUP:
        Duplicate native-ref detection (suppress echoes).
    RESOLVE_RELATIONS:
        Cross-adapter relation resolution (native → canonical IDs).
    STORE:
        Persist the canonical event and inbound native ref.
    ROUTE:
        Route matching and delivery plan creation.
    DELIVER:
        Per-target adapter delivery (render → send → receipt).
    """

    INGRESS = "ingress"
    DEDUP = "dedup"
    RESOLVE_RELATIONS = "resolve_relations"
    STORE = "store"
    ROUTE = "route"
    DELIVER = "deliver"
