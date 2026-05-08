# Plugin API Contract

> Extracted from: [Modular Event Communications Runtime Specification](../spec/modular-event-engine-spec.md), Sections 12.3, 19, 20
> Version: 0.1.0 (Draft)

This document defines the complete interface a plugin author writes against. If you are writing a plugin, this is the only contract you need.

> **Phase 1 Limitation: Boundary Scaffolding Only**
>
> The plugin interfaces defined in this document (Plugin, PluginContext, PluginStateStore, PluginCapability, convenience methods) are **spec-level definitions only**. Phase 1 does not implement:
> - No `PluginContext` class or instance construction
> - No `PluginStateStore` implementation (the `plugin_state` SQL table exists in the schema but is not wired to any runtime service)
> - No plugin loader, plugin host, or capability enforcement
> - No convenience methods (reply, send, react, emit)
> - No plugin event routing or lifecycle management
>
> The plugin contract documents the intended interface for future implementation. Plugin authors should treat this as the target API, not as currently available.

---

## 1. Plugin Interface

Every plugin implements the `Plugin` protocol:

```python
class Plugin(Protocol):
    name: str
    version: str
    api_version: int                    # Plugin API version the plugin targets
    capabilities: set[PluginCapability]  # What this plugin can do

    async def initialize(self, context: PluginContext) -> None: ...
    async def handle_event(self, event: CanonicalEvent) -> list[CanonicalEvent]: ...
    async def shutdown(self) -> None: ...
```

### Lifecycle

1. The runtime loads your plugin and calls `initialize` with a `PluginContext`.
2. For each event routed to your plugin, the runtime calls `handle_event`. Return zero or more derived events.
3. On shutdown, the runtime calls `shutdown`. Clean up resources here.

### Error and Timeout Expectations

- If `handle_event` raises an exception, the runtime logs the error with your plugin identity and continues processing. Other plugins and pipeline stages are not affected.
- `handle_event` must complete within a configurable timeout (default TBD). If it exceeds the timeout, the runtime cancels the coroutine and logs the timeout.
- `initialize` and `shutdown` also have timeouts. A plugin that blocks indefinitely during startup or teardown will be cancelled and flagged as unhealthy.
- Returned events that fail validation (missing required fields, invalid event kind) are silently dropped and logged. Your plugin does not receive feedback on dropped events.

---

## 2. Plugin Capabilities

Capabilities control what your plugin is allowed to do. Declare them in your `capabilities` set:

```python
class PluginCapability(str, Enum):
    READ_EVENTS = "read_events"           # Can observe events
    EMIT_EVENTS = "emit_events"           # Can produce new events
    READ_ROUTES = "read_routes"           # Can inspect routing config
    MODIFY_ROUTES = "modify_routes"       # Can add/remove routes
    READ_IDENTITY = "read_identity"       # Can resolve identities
    READ_STORAGE = "read_storage"         # Can query historical events
    ACCESS_TELEMETRY = "access_telemetry" # Can read telemetry data
```

**Capability declaration happens at load time.** Your plugin lists its required capabilities in the `capabilities` set. The runtime grants only what you declare. Attempting to use a service that requires a capability you didn't declare raises a runtime error.

**Minimal example**: A plugin that only observes events needs `READ_EVENTS`. A plugin that reacts to messages needs `READ_EVENTS` and `EMIT_EVENTS`.

---

## 3. PluginContext

The `PluginContext` is your gateway to the runtime. You receive it in `initialize` and store it for use throughout your plugin's lifetime.

### Core Fields

```python
@dataclass
class PluginContext:
    config: dict                        # Plugin-specific configuration from runtime.yaml
    event_bus: EventBus                 # Scoped to plugin's capabilities
    storage: StorageBackend             # Read-only unless READ_STORAGE capability declared
    identity_resolver: IdentityResolver # Scoped to READ_IDENTITY capability
    logger: BoundLogger                 # Structured logger with plugin context
    plugin_id: str                      # Unique runtime identifier for this plugin instance
    state: PluginStateStore             # Scoped key-value store backed by plugin_state table
```

### Convenience Fields and Methods

```python
class PluginContext:
    # ... core fields from above ...

    current_event: CanonicalEvent | None  # The event currently being handled, or None

    async def reply(self, text: str) -> None:
        """Reply to the current event. Creates a CanonicalEvent with
        relation_type='reply' and target_event_id set to current_event's ID.
        Requires EMIT_EVENTS capability. Requires current_event to be set."""
        ...

    async def send(self, text: str, target: RouteTarget | str | None = None) -> None:
        """Send a message text event.
        - target is a RouteTarget: routes to that structured target.
        - target is a str: interpreted as a route_id.
        - target is None: follows default routing.
        Requires EMIT_EVENTS capability."""
        ...

    async def react(self, key: str) -> None:
        """React to the current event with the given key (e.g., emoji).
        Creates a CanonicalEvent with relation_type='reaction' and the key field.
        Requires EMIT_EVENTS capability. Requires current_event to be set."""
        ...

    async def emit(self, kind: str, payload: dict) -> None:
        """Emit a custom event of the given kind with the given payload.
        For plugin events that don't fit reply/send/react patterns.
        Requires EMIT_EVENTS capability."""
        ...
```

All convenience methods internally create `CanonicalEvent` instances with appropriate `EventRelation` entries and emit them through the event bus. The low-level `event_bus` remains available for plugins that need full control over event construction.

---

## 4. PluginStateStore

`PluginStateStore` provides scoped key-value persistence for your plugin. It is backed by the `plugin_state` SQL table.

```python
class PluginStateStore(Protocol):
    async def get(self, key: str) -> dict | None:
        """Retrieve a JSON value by key from this plugin's scoped state.
        Returns None if the key does not exist."""
        ...

    async def set(self, key: str, value: dict) -> None:
        """Store a JSON value under the given key in this plugin's scoped state.
        Overwrites any existing value for the same key."""
        ...
```

### Scoping Rules

- Keys are scoped to `plugin_id`. Your plugin cannot read or write state belonging to other plugins.
- Values are JSON objects (serialized as `dict` in Python).
- There is no delete operation in the current interface. Set a key to an empty dict `{}` to clear it.
- There is no listing or enumeration of keys in the current interface. Your plugin must track its own key names.

### Backing SQL Table

```sql
CREATE TABLE plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',  -- JSON
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (plugin_id, key)
);
```

The `value` column stores serialized JSON. The `updated_at` column tracks the last write time.

---

## 5. Security Boundaries

### Capability Enforcement

The runtime enforces capability boundaries at the service access level:

| Service | Required Capability | Behavior if Undeclared |
|---|---|---|
| `handle_event` called | `READ_EVENTS` | Plugin never receives events |
| `reply`, `send`, `react`, `emit` | `EMIT_EVENTS` | Runtime error on call |
| `event_bus` (write) | `EMIT_EVENTS` | Event bus write operations fail |
| `identity_resolver` | `READ_IDENTITY` | Resolver calls raise error |
| `storage.query` | `READ_STORAGE` | Storage queries raise error |
| Route inspection | `READ_ROUTES` | Route queries raise error |
| Route modification | `MODIFY_ROUTES` | Route mutations raise error |
| Telemetry access | `ACCESS_TELEMETRY` | Telemetry queries raise error |

### Route Permissions

Plugins that emit events can only send to routes the operator has explicitly allowed for that plugin. This is configured per-plugin in the runtime configuration:

```yaml
plugins:
  - name: my-alert-plugin
    class: plugins.alert_rules.AlertRulesPlugin
    enabled: true
    capabilities: ["read_events", "emit_events"]
    config: {}
    rate_limits: {}
```

The runtime validates emitted events against allowed routes before they enter the pipeline.

### Rate Limits

Each plugin has configurable rate limits for:
- Event emission
- Storage queries
- API calls

Rate limits are set in the plugin's configuration under `rate_limits`. Exceeding a rate limit causes the operation to fail with a rate limit error, not a silent drop.

### Audit Logging

All plugin actions are logged with:
- Plugin identity (`plugin_id`)
- Capability used
- Action taken (event emitted, storage queried, identity resolved)
- Timestamp

This audit trail is available to operators for monitoring and debugging.

### API Versioning

Plugins declare the runtime plugin API version they target via `api_version`. The runtime supports plugins written for its own current and immediately prior major plugin API version. This means a plugin targeting API version N works on runtimes supporting version N or N-1. This applies only to this runtime's native plugin API, not to any external or legacy system's plugin interface.

### Sandboxing (Future)

Plugins may optionally run in a restricted execution environment (subprocess, WASM, or container) with limited system access. This is a future capability. Current plugins run in-process with capability scoping as the primary isolation mechanism.

---

## 6. Minimal Plugin Example

```python
from core.events.canonical import CanonicalEvent
from plugins import Plugin, PluginCapability, PluginContext


class EchoPlugin(Plugin):
    name = "echo"
    version = "1.0.0"
    api_version = 1
    capabilities = {PluginCapability.READ_EVENTS, PluginCapability.EMIT_EVENTS}

    def __init__(self):
        self._ctx: PluginContext | None = None

    async def initialize(self, context: PluginContext) -> None:
        self._ctx = context
        self._ctx.logger.info("echo plugin initialized")

    async def handle_event(self, event: CanonicalEvent) -> list[CanonicalEvent]:
        # Echo text messages back to the source route
        if event.event_kind == "message.text":
            await self._ctx.reply(f"Echo: {event.payload.get('text', '')}")
        return []

    async def shutdown(self) -> None:
        self._ctx.logger.info("echo plugin shutting down")
```

---

## 7. Event Types Relevant to Plugins

Plugins receive and emit events using the canonical event model. The event kinds most relevant to plugins:

| Event Kind | Description | Typical Plugin Use |
|---|---|---|
| `message.text` | Plain text message | Respond to user messages |
| `message.file` | File or attachment | Process attachments |
| `telemetry.received` | Raw telemetry data received from a node | Alert on thresholds |
| `telemetry.position` | Geographic-position telemetry report | Map visualization |
| `presence.changed` | Node or user presence state changed | Notification triggers |
| `system.audit` | Audit-log entry produced by the framework | Monitoring |
| `system.lifecycle` | Lifecycle event (start, stop, reload) | React to system state |
| `plugin.custom` | Plugin-defined custom event | Inter-plugin communication |

> **Note:** `metrics.update`, `channel.announcement`, `transform.output`, and `policy.action` from earlier spec drafts are not implemented in Phase 1. The canonical taxonomy defines 18 event kinds; the full list is in `docs/contracts/01-canonical-event-contract.md` Section 5.

Plugins that emit custom events should use `plugin.custom` as the event kind and put the custom type in the `payload` dict. This keeps the event kind registry clean while allowing plugins to define their own subtypes.

The full canonical event model, including `CanonicalEvent` fields, `EventRelation`, and metadata namespaces, is defined in the master spec (Sections 5, 14).

---

## 8. Configuration Schema

Plugin configuration in `runtime.yaml`:

```yaml
plugins:
  - name: <plugin-name>
    class: <python-class-path>
    enabled: true
    capabilities: []          # Required capabilities (must match plugin's declared set)
    config: {}                # Plugin-specific configuration, passed as PluginContext.config
    rate_limits: {}           # Per-plugin rate limits for event emission, storage, API
```

The `config` dict is passed directly to your plugin as `PluginContext.config`. Define whatever keys your plugin needs. The runtime does not validate plugin config keys beyond passing them through.

---

## 9. Cross-References

| Topic | Spec Section |
|---|---|
| Canonical Event Model | Spec Section 5 |
| Event Kinds Registry | Spec Section 5.3 |
| Metadata Namespaces | Spec Section 14 |
| Storage Backend Interface | Spec Section 12.4 |
| Identity Resolver | Spec Section 11 |
| Routing and RouteTarget | Spec Section 8 |
| Policy Pipeline | Spec Section 7 |
| Replay and Reprocessing | Spec Section 19 |
| Configuration Reference | Spec Section 28 |
