# External adapter packages

MEDRE no longer ships production Matrix, Meshtastic, MeshCore, or LXMF adapter
implementations in its core wheel. The registry discovers adapter specs from
installed `medre.adapters` entry points. Separately installed distributions
provide the unchanged transport-specific Python module paths. Configuration
models, fakes, and optional SDK extras remain in core pending full packaging
separation.
