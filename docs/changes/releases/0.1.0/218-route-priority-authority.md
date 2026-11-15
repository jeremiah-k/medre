# Route priority becomes executable ordering authority

- Implement the routing specification's `priority` field end to end with a
  default of `100`; lower values are matched and planned first and ties are
  broken by expanded route ID.
- Preserve priority through standard, bidirectional, and `context_map` route
  expansion, route environment overrides, offline route plans, and CLI output.
- Keep route priority transport-neutral: it controls MEDRE planning order and
  does not imply adapter-native QoS or message priority support.
