# Encodable bridge context

Reject lone-surrogate source claims, relation hints, native references, and
public facts before transport serialization. Invalid optional context is
omitted while valid authored message text remains eligible for relay.
