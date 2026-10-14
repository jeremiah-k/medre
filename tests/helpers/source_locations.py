"""Find source files across independently installed MEDRE adapter packages."""

from importlib.util import find_spec
from pathlib import Path


def source_file(repo: Path, relative_path: str) -> Path:
    """Resolve a core or installed adapter source file without importing SDKs."""
    local = repo / relative_path
    if local.is_file():
        return local
    prefix = "src/medre/"
    if not relative_path.startswith(prefix) or not relative_path.endswith(".py"):
        raise FileNotFoundError(relative_path)
    dotted = "medre." + relative_path[len(prefix) : -3].replace("/", ".")
    spec = find_spec(dotted)
    if spec is None or spec.origin is None:
        raise FileNotFoundError(f"No installed adapter source for {dotted}")
    return Path(spec.origin)
