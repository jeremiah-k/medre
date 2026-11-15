# Plan bounded radio text fragments

A shared pure planner creates immutable UTF-8 wire fragments with repeated
attribution, numbered parts and content digests. It preserves whitespace,
accounts for marker digit growth and rejects exceeded part limits.

Production splitting remains gated on durable per-part progress, exact-attempt
feedback and adapter pacing integration. Existing radio rendering is unchanged.
