# Qualify merged Meshtastic multipart queue ownership

Adopt Meshtastic #15 at merged SHA
`06f0b6030c5d6524467d1611f050244359413c88`, after its fake/installed-SDK CI passed.
Align the adapter configuration schema and example with its integer
`max_text_parts` opt-in, bounded 1–128 with default 1. Share schema-drift coverage
across both merged radio providers. Native packet acceptance remains local
transport evidence; partial/uncertain work stays held and no hardware recovery
claim is introduced. Package versions, SQLite shape and marker are unchanged.
