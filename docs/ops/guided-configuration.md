# Guided bridge configuration

Use `medre config bridge` to connect two adapter instances without writing a
route by hand. It asks for conversation addresses, explains message direction,
shows the compiled route plan, and saves a new configuration after review.
It never connects to an account or device, installs packages, or starts MEDRE.

In a uv deployment, prefix the commands below with `uv run --locked`.

## Try an offline bridge

Install your chosen providers in MEDRE's environment, then run:

```bash
medre config bridge --output bridge.yaml
```

Without an input file, MEDRE starts from installed providers' fake samples and
an empty route set. The list shows each instance ID, platform, and whether the
instance is an offline sample or a real connection. Sample instance IDs are
`<provider>_<number>`, such as `matrix_1`, so
each ID names its platform; one sample-contributing provider supplies two
distinct instances, `<provider>_1` and `<provider>_2`, and further instances
of a provider belong under the same `adapters.<provider>` group in the saved
file. A provider without fake sample support can still participate through an
existing configuration. Only the two selected sample instances are saved;
unused sample connections are omitted. An existing input configuration
retains all its connections.

Press Ctrl+C at any prompt to cancel without saving.

1. Choose a source and destination by number or exact instance ID. The
   destination list excludes the selected source; its numbers start at 1.
   If an instance ID is itself a number, menu positions use `#1`, `#2`, and so
   on to avoid confusing an ID with a menu position.
   Noninteractive commands still require exact IDs.
2. Choose a direction by number or name. Press Enter for the default,
   `bidirectional` (both ways). Choose `source_to_dest` or `dest_to_source`
   for one-way traffic.
3. Enter a source context and its destination context. These are native
   conversation addresses: a room ID, channel index, topic, or another address
   defined by the provider. They remain strings, including numeric channels.
   Each prompt shows the provider's declared example address in parentheses;
   the example is a format reference, not a required value. Each pair is
   validated as soon as it is entered: a rejected address — for example a
   room the provider's allowlist omits, a duplicate source context, or a pair
   an existing route already connects — reports the reason and prompts again
   for the source context instead of aborting after all prompts. Enter
   additional pairs as needed, then leave the next source context blank. An
   empty first source context prompts again instead of ending setup.
4. Optionally name each side for attribution. Blank interactive answers keep
   the adapter's label; an explicitly empty `--source-label` or `--dest-label`
   suppresses that label.
5. Read the forward/reverse legs and any loop warnings. Confirm with `yes` to
   save, or decline to leave the filesystem unchanged. `y/n` also work; typos
   prompt again, and Enter defaults to no. Reverse previews show addresses in
   actual message-flow order. After saving, MEDRE
   prints copyable validation commands with the saved path and, for offline
   samples, a smoke command for the first mapped source context.

Two-way bridges normally produce loop/cycle warnings because both directions
are intentional. MEDRE applies native-reference duplicate, self-loop, and
route-trace guards during delivery; see
[loop suppression](../spec/routing-delivery.md#5-loop-suppression) for their
scope. The topology warnings alone do not show whether a native echo will be
recognized by a provider.

The generated route permits `message.created` and `message.text`. Other event
kinds need an explicit policy change. Replies and any attachments carried by
those events continue to follow the existing attachment settings, provider
capabilities, and fallback policies.

Validate the result and exercise one source/context from the displayed plan:

```bash
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre smoke --config bridge.yaml \
  --source-adapter SOURCE_ID --source-context SOURCE_CONTEXT --json
```

Replace the last command's source values with an actual leg from the plan.
Repeat for the reverse leg and other mappings when present. Fake factories
that support context selection accept the generic `channel` argument; MEDRE
rejects a factory that ignores it instead of claiming that context was tested.
A smoke pass covers one source/context and its matched fanout, not every route
or native delivery. `--source-context` applies to normal smoke, not drills or
run sessions. Real configurations are rejected by fake smoke.

## Add a route to native connections

Use `--config` with a file containing your existing adapter connections. Native
connection fields and prerequisites stay with their providers; see
[transport setup guides](README.md#find-the-right-guide). The workflow does not request or
display connection credentials. Existing fields, literal secrets, and path
placeholders are retained in the new file; environment values are not copied.
The route plan includes the current environment overrides, just like
`medre routes plan`.

For example, if `connections.yaml` defines a Matrix instance named `bot` and a
Meshtastic instance named `node`:

```bash
medre config bridge --config connections.yaml --output bridge.yaml \
  --source bot --dest node --direction bidirectional \
  --map '!room:example.org' '0' \
  --source-label 'Private room' --dest-label 'Local channel'
```

Replace the addresses with your actual context IDs. Matrix inbound
contexts must use a room ID (`!room:server`), not a room alias (`#room:server`),
even without a room allowlist: inbound events carry room IDs. Repeat `--map`
for more conversation pairs. A two-way mapping needs an unambiguous reverse address;
mapping two sources to the same destination is supported only for
`source_to_dest` routes. Both endpoints may use the same provider, but this
workflow requires two different adapter instances.

Provider configuration restrictions are checked before preview or saving. For
example, a Matrix instance with an explicit `room_allowlist` must include every
room from which an enabled route receives messages, including reverse legs.
Update the input configuration (or the corresponding environment override)
before authoring; the workflow does not broaden native permissions for you.
For an offline sample, use a context allowed by `medre config sample`.

Existing routes are preserved. Choose a new `--route-id` if `bridge` already
exists. A selected context pair already connected by an enabled text route is
rejected;
edit that route instead of creating duplicate deliveries. Advanced intentional
overlaps and structured destinations remain available through ordinary YAML.
An existing wildcard source for the same explicit target, or a context-less
text route between the same adapters, is also caught. Stale
adapter references in disabled routes need fixing before the draft can be saved,
just as with `medre config check`.

Existing configuration errors are reported before context entry. A bad context
pair can be retried without losing earlier accepted pairs. Fake-smoke next steps
are shown only when every enabled adapter in the saved configuration is fake;
providers also need context-aware fake event support. On Windows, the printed
commands are quoted for PowerShell.

## Preview, automation, and file protection

Add `--preview` to validate and inspect without saving. Add `--json` to print
the existing route-plan JSON format; prompts and guidance go to stderr.
Without a terminal, supply `--source`, `--dest`, `--direction`, and at least one
`--map`. This makes scripts fail on missing answers rather than waiting for
input. Configuration errors exit 2; EOF or interruption exits 1.

The output path needs an existing parent directory and a `.yaml` or `.yml`
extension. Files, directories, and symlinks already at that path are never
overwritten. New files use owner-only permissions on POSIX; on Windows,
restrict the parent directory's ACL before saving. The source file is not
modified. YAML comments and formatting are regenerated.

Provider discovery and SDK package metadata can identify missing prerequisites;
they cannot prove authentication, native address validity, service reachability,
radio provisioning, or recipient delivery. Run
`medre adapters check --config bridge.yaml`, review provider setup, and qualify
native delivery separately. Keep generated configurations out of Git and share
only [sanitized diagnostics](diagnostics-and-evidence.md).
