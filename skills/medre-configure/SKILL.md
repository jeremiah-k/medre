---
name: medre-configure
description: Draft, preview, or troubleshoot MEDRE bridge YAML and routes. Use for first routes, changes to conversation mappings or route policy, and unexpected route matches in an operator's configuration.
---

# Configure MEDRE

## Establish the intended message flow

Use the docs and CLI that match the operator's MEDRE version. Read
[guided configuration](../../docs/ops/guided-configuration.md) for the builder
and [configuration](../../docs/ops/configuration.md) for YAML fields. Open the
relevant [transport setup guide](../../docs/ops/transport-setup/README.md) when
you need an address format, permission, or connection prerequisite.

Translate the request into expected legs. For each leg, record the source
connection name and room, chat, or channel, the destination connection and
conversation, and any event or sender restrictions. A connection name is the
adapter instance ID under `adapters.<transport>`. The adapter defines what
its context means. For example, Matrix uses a room ID beginning with `!`
and Meshtastic uses a channel index such as `"0"`. Quote IDs and channel
strings in YAML. Ask about gaps that would change message flow. Use labeled
placeholders for missing private values.

Work from the minimum sanitized excerpt needed for the task. Keep actual
credentials and identity or state files in the operator's environment. If an
offline command needs the real file or environment overrides, give the operator
the command to run locally and request only the relevant sanitized result.

## Draft a route

- For a first route or a simple addition, use `medre config bridge` with
  the existing connection file. With no input file it uses fake samples.
  Supply an explicit `--direction` in generated commands. Use
  `--preview --json` with a new `--output` path to inspect before saving.
- For an existing route, inspect its definition and compiled legs first.
  Use `--replace-route --route-id ID` for guided text routes. Make a targeted
  YAML edit in a separate draft for advanced policies or structured
  destinations. Preserve other routes and connection settings.
- For fan-in, map each source context to the shared destination only in
  `source_to_dest` mode. Check existing wildcard and explicit legs for
  overlap. A reverse leg needs a distinct destination context per map entry.
  Confirm inbound adapter allowlists for every source, including reverse
  sources when present.

## Check or explain the result

- Use `medre config check --config FILE` for the effective configuration and
  `medre routes plan --config FILE --json` for the expanded legs. Use the same
  intended environment overrides for both. For a change, compare the before
  and after plans against the requested legs, including legs expected to
  disappear and unrelated routes expected to stay.
- For a missing or unexpected match, use `medre routes explain --config FILE
--source-adapter ID --source-context CONTEXT --event-kind KIND --json`.
  Include `--sender-id` when the policy requires it. Trace the result to the
  tested source filters or policy. A missing sender yields an incomplete
  allowlist check.
- Report what was actually checked and which values remain unresolved.
  The plan and explanation are offline evidence. Smoke exercises a selected
  source and context with fake adapters. Use the
  [troubleshooting guide](../../docs/ops/troubleshooting.md) for connectivity
  or delivery questions after a route matches.
