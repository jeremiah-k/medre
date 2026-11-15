# Prefix truncation variables

The shared relay-prefix formatter accepts a per-renderer declaration of
truncatable variables: each declared name resolves `{nameN}` placeholders
to the first N Unicode code points of its value, counting code points and
never bytes, with N ≥ 1. Undeclared stems keep treating numeric-suffixed
placeholders as unknown, so constrained-display transports opt in while
roomy transports expose no truncation surface, and an invalid declaration
name reports through the formatter's error diagnostics without raising.
