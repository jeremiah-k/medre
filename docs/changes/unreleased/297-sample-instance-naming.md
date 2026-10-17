# Sample instances are named after their provider

`medre config sample` and the guided bridge workflow name generated instances
`<provider>_<number>` (for example `matrix_1`, `meshtastic_1`) instead of
generic `sample_N` IDs. Core still assigns the IDs, so independently authored
providers never coordinate instance names, while the transport prefix makes
listings, plans, and saved files self-describing. Numbering counts per
provider, so further instances of the same provider extend the pattern
(`matrix_2`) under the `adapters.<provider>` group.
