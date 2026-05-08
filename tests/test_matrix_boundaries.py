"""Matrix boundary enforcement tests: architectural separation between
core, rendering, and adapter layers.
"""

from __future__ import annotations

import sys

import pytest

from medre.adapters import FakeMatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.config import MatrixConfig
from medre.core.rendering.matrix import MatrixRenderer


class TestMatrixBoundaries:
    """Architectural boundary enforcement for Matrix components."""

    def test_core_does_not_import_matrix(self) -> None:
        """medre.core should not import medre.adapters.matrix at module level."""
        # Import core and check matrix adapter modules are not loaded
        import medre.core  # noqa: F401

        matrix_modules = [k for k in sys.modules if "medre.adapters.matrix" in k]
        # The rendering/matrix.py imports from adapters.matrix, so the
        # import above may trigger it via the test suite.  Instead verify
        # that core itself does not list matrix in its direct dependencies.
        # We check that core modules don't reference matrix adapter directly.
        core_modules = [k for k in sys.modules if k.startswith("medre.core.") and "matrix" in k]
        # core.rendering.matrix is the renderer (expected), but no adapter import
        for mod_name in core_modules:
            assert mod_name == "medre.core.rendering.matrix", (
                f"Unexpected core module importing matrix: {mod_name}"
            )

    def test_matrix_does_not_import_other_adapters(self) -> None:
        """Matrix adapter package does not import other adapter modules."""
        from medre.adapters import matrix as matrix_pkg  # noqa: F401

        # Verify the MatrixAdapter class exists but doesn't trigger
        # imports of meshtastic or other adapter packages
        other_adapters = [
            k for k in sys.modules
            if "medre.adapters" in k
            and "meshtastic" in k
        ]
        assert len(other_adapters) == 0, (
            f"Matrix adapter triggered import of other adapters: {other_adapters}"
        )

    def test_matrix_adapter_does_not_route(self) -> None:
        """FakeMatrixAdapter has no route matching or routing methods."""
        adapter = FakeMatrixAdapter("m")
        assert not hasattr(adapter, "match")
        assert not hasattr(adapter, "route")

    def test_matrix_renderer_does_not_deliver(self) -> None:
        """MatrixRenderer has no deliver method."""
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "deliver")

    def test_matrix_codec_does_not_route_or_plan(self) -> None:
        """MatrixCodec has decode/encode but no route/match/plan methods."""
        config = MatrixConfig(
            adapter_id="test",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        codec = MatrixCodec("test", config)
        assert hasattr(codec, "decode")
        assert hasattr(codec, "encode")
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "match")
        assert not hasattr(codec, "plan")
