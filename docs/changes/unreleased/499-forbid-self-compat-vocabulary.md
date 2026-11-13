# Forbid self-compat vocabulary in src

Source docstrings and comments may no longer describe MEDRE's own surfaces
as retained compatibility baggage: phrase framing that brands a surface as
a shim, brands an alias, wrapper, or signature as legacy or deprecated
baggage, or asserts that some prior spelling of a name still works is
banned from Python files under the ``medre`` namespace.

The guard extends the shared forbidden-term helpers with a
fragment-assembled phrase family, scans ``src/`` Python files only, and
stays outside the generic durable-term union so docs, tests, and examples
keep their existing enforcer scope. The ban is phrase-level, not
word-level: retention accounting wording, boundary guards that never move
backward, foreign SDK and protocol identifiers used as names, and the
prerelease migration's description of predecessor table layouts all scan
clean.

Foreign-contract exceptions belong in the self-asserting allowlist module
as tight path-prefix and phrase pairs, each entry naming the foreign
contract whose own wording it excuses. An entry whose allowlisted text no
longer exists fails the guard, so stale exceptions cannot accumulate. The
allowlist ships empty: ``src/`` scans clean at these phrases today.
