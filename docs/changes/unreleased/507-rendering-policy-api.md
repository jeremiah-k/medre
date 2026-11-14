# Add shared rendering APIs before provider migration

Introduce opt-in relation composition, event text extraction, attribution
precedence, prefix validation and destination-native binding helpers. Existing
rendering entry points retain their behavior while providers adopt the new
APIs. Opt-in callers get explicit reaction keys, every relation description,
neutral kind markers for incomplete mutations, and strict prefix validation
without changing the legacy formatter. Remove superseded paths in the final
coordinated core change before publication. No schema or compatibility pin
changes are required.
