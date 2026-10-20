"""Synthetic plugins exercising the canonical execution seam without providers."""

from dataclasses import replace

from medre.core.engine.plugin_executor import PluginBinding
from medre.plugins import (
    Allow,
    Pass,
    PluginCapability,
    PluginInstanceConfig,
    PluginStage,
)
from tests.helpers.plugin_spec import make_plugin_spec


class ScriptedPlugin:
    def __init__(self, action=None):
        self.action = action
        self.context = None
        self.calls = []
        self.started = 0
        self.stopped = 0

    async def initialize(self, context):
        self.context = context
        self.started += 1

    async def shutdown(self):
        self.stopped += 1

    async def _invoke(self, stage, event):
        self.calls.append((stage, event.event_id))
        if self.action is None:
            return Allow() if stage is PluginStage.EVENT_POLICY else Pass()
        return await self.action(self.context, event)

    async def enrich(self, event):
        return await self._invoke(PluginStage.ENRICH, event)

    async def transform(self, event):
        return await self._invoke(PluginStage.TRANSFORM, event)

    async def evaluate(self, event):
        return await self._invoke(PluginStage.EVENT_POLICY, event)


def binding(name, stage=PluginStage.ENRICH, action=None, *, required=True, grants=None):
    caps = {PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS}
    if stage in {PluginStage.TRANSFORM, PluginStage.EVENT_POLICY}:
        caps.add(PluginCapability.SUPPRESS_EVENTS)
    spec = replace(
        make_plugin_spec(name),
        stages={stage},
        capabilities=caps,
        transform_inputs=("message.created",) if stage is PluginStage.TRANSFORM else (),
        transform_outputs=(
            ("message.created",) if stage is PluginStage.TRANSFORM else ()
        ),
    )
    instance = PluginInstanceConfig(
        name, "main", grants=spec.capabilities if grants is None else grants
    )
    return PluginBinding(spec, instance, ScriptedPlugin(action), required=required)
