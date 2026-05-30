"""Replay harness for deterministic re-processing of historical events.

This package provides the machinery to re-process canonical events that have
already been persisted in storage through selected pipeline stages.  Different
:class:`~medre.core.engine.replay.types.ReplayMode` values control which stages
are executed and whether side-effects (delivery to adapters) are allowed.

Submodules
----------
types
    Data types: :class:`ReplayMode`, :class:`ReplayRequest`,
    :class:`ReplayResult`, :class:`ReplayRouteAttribution`,
    :class:`ReplayState`, :func:`collect_replay_state`.
summary
    :class:`ReplaySummary`, :func:`collect_replay_summary`,
    :func:`_build_summary`.
engine
    :class:`ReplayEngine` -- the main replay orchestrator.
selection
    Event selection, counting, and missing-event handling.
store
    Event existence and integrity verification stage.
helpers
    Internal helpers: filter conversion, stage resolution, timing.
protocols
    Collaboration protocols: :class:`_RealPipelineProtocol`,
    :class:`_StubPipelineProtocol`.
routing
    Route metadata cleanup and loop-prevention filtering.
planning
    Delivery plan construction stage.
rendering
    Transform and render event output stage.
delivery
    Delivery envelope, adapter filtering, capability filtering.
"""
