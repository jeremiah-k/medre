# AI-assisted setup

We're working to make MEDRE configuration easier. Start with the
[guided configuration](guided-configuration.md) commands to build and preview
a route. If you use an agent, it can help you choose settings for your first
route or reason through a change involving several conversations.

Give the agent the MEDRE version you run, the corresponding guide, and just
enough detail to describe the route. Use consistent stand-ins for private
room IDs, hostnames, and adapter names. Keep credentials, identity files, and
databases local. Run checks against your real configuration on your own machine
and share only the relevant sanitized output.

## Set up a first route

```text
My Matrix and Meshtastic connections are already in connections.yaml. Help
me forward text from one Matrix test room to Meshtastic channel 0, one way.
Use MEDRE's guided configuration and transport setup guides for my version.

Show me where to put the connection names, Matrix room ID, and radio
channel number. Use placeholders for anything I haven't supplied. Give me
a config bridge command with the direction explicit, a preview I can
review before saving, and the checks to run against the new file. Tell me
which direction I should see in the plan. Leave connecting to the services
and sending a test message to me.
```

## Review a change to an existing bridge

```text
I want two Matrix announcement rooms to feed the same radio channel, with
no radio traffic forwarded back. I'll share the sanitized route definitions
and current output of medre routes plan --json.

List which room should send to which channel. Check whether an existing
route already forwards either room's messages or sends radio traffic back.
Use MEDRE's configuration reference to propose the smallest change in a
separate draft file. Preserve unrelated routes and policies. Show which
connections and directions should change in the before and after plans.
```

## Investigate a missing message

```text
I expected a message from radio channel 2 to match my bridge route, but it
didn't appear in the destination room. Give me a medre routes explain
command that uses the radio connection name, channel number, and event
kind. If the route checks a sender allowlist, include the sender input
too. Use placeholders for private values.

I'll share the sanitized report. Point to the rule or field that determined
the match. If a route matches and policy allows it, tell me what evidence
I'd need to check delivery. Keep this investigation read-only.
```

Review the output of `medre config check` and `medre routes plan` before
starting a bridge. The [route explanation guide](guided-configuration.md#explain-a-route-decision)
covers what you can verify offline. Use [troubleshooting](troubleshooting.md)
when the question involves a live connection or delivery.
