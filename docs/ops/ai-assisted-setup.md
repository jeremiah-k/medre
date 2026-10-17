# AI-assisted setup

AI assistance is strictly optional: MEDRE is usable today without any AI
service, and the guided builder already authors and validates your routes.

MEDRE has many configuration choices. If you get stuck on instances, contexts,
or routes, an AI coding assistant can walk you through `medre config bridge`,
the offline command that authors and validates your first configuration.
[OpenCode](https://opencode.ai/) is one option:

```bash
curl -fsSL https://opencode.ai/v2/install | bash
opencode
```

The [download page](https://opencode.ai/download) lists every installation
method (npm, Homebrew, and others), and the
[official setup guide](https://opencode.ai/v2/docs/) covers first steps.

OpenCode's model catalog changes over time: models rotate, but there is
usually something free to use, and many free models work without creating an
account. Two high-quality free options at the time of writing are **DeepSeek
V4.1 Flash** (`deepseek-v4.1-flash-free`) and **GLM-5.3 Flash**
(`glm-5.3-flash-free`); check
[OpenCode's live model catalog](https://opencode.ai/v2/docs/console/models/)
for what is currently available and its terms.

You can ask an assistant in plain language. Two examples:

Ask for a guided first bridge between two transports:

```text
Using the MEDRE Getting Started guide, walk me through `medre config bridge
--output bridge.yaml` for a first two-way route between my Matrix test room
and Meshtastic channel 0. Explain each prompt: adapter instances, direction,
contexts, and labels. Remind me that Matrix needs a room ID starting with `!`
rather than a `#` alias, and that Meshtastic contexts are channel indexes.
After the command saves, have me run the `medre config check` and
`medre smoke` steps it prints. Do not invent adapter fields; MEDRE validates,
you explain.
```

Ask for a plan to bring to the builder:

```text
Using the MEDRE documentation, help me plan a bridge between my MeshCore node
and my Matrix account. Ask me for whatever details you need, then give me the
list to bring to `medre config bridge`: both adapter instance IDs, each side's
context, a direction, and any labels. Explain each choice, then have me run
the builder and validate the saved file with `medre config check`.
```

Keep tokens, passwords, private keys, and unredacted databases **out of AI
chats**. Review every proposed change. MEDRE does not need an AI service to
operate.
