"""Bounded local Graphviz execution; native adapters and remote services are unused."""

from __future__ import annotations

import shutil
import subprocess
import tempfile


class GraphvizError(RuntimeError):
    """Local export failed without exposing graph labels or renderer stderr."""


def render_graphviz(source: str, output_format: str) -> bytes:
    """Render one closed format with a process deadline and bounded memory reads.

    Output is spooled into anonymous files rather than unbounded PIPE buffers.
    No shell is invoked and no configured path becomes an argument to Graphviz.
    Renderer stderr is private and is never echoed because it can contain
    topology labels. The caller publishes only successfully returned bytes.
    """
    if output_format not in {"svg", "png", "pdf"}:
        raise ValueError("unsupported Graphviz output format")
    data = source.encode("utf-8")
    if len(data) > 512 * 1024:
        raise GraphvizError("Graph is too large for image export; use --view adapters")
    executable = shutil.which("dot")
    if executable is None:
        raise GraphvizError(
            "Graphviz 'dot' is not installed; install Graphviz or use --format terminal"
        )
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [executable, f"-T{output_format}"],
                input=data,
                stdout=output,
                stderr=errors,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GraphvizError(
                "Graphviz exceeded its 20-second rendering deadline"
            ) from None
        except OSError:
            raise GraphvizError("Graphviz could not be started") from None
        if result.returncode != 0:
            raise GraphvizError(
                "Graphviz rendering failed; check the local Graphviz installation"
            )
        if not 0 < output.tell() <= 64 * 1024 * 1024:
            raise GraphvizError("Graphviz returned empty or oversized output")
        output.seek(0)
        return output.read(64 * 1024 * 1024 + 1)
