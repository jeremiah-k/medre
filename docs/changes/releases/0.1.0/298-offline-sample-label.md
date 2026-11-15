# Guided authoring labels adapter kinds in plain language

The `medre config bridge` instance listing renders the internal `fake` adapter
kind as "offline sample"; `real` is shown as-is and the mapping is
display-only, so saved YAML keeps the literal `adapter_kind: fake` values.
The no-input intro line and the two-instance error message use the same
plain-language term instead of "fake sample" jargon.
