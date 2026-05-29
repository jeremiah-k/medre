"""Deterministic conformance runtime harness for MEDRE.

Conformance tests assert MEDRE runtime contracts across ingress,
rendering, capability, delivery/evidence, and replay paths.  They use
deterministic JSON fixtures and real codecs/renderers/services -- never
real SDK network or hardware.

See docs/spec/conformance.md for the fixture workflow and what an
adapter must satisfy to claim MEDRE conformance.
"""
