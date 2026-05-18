"""Tests for medre.runtime.builder: fake adapter ID propagation,
deterministic build ordering, and all-adapters-build-failure handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContract
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from tests.helpers.runtime_builder import (
    clean_path_env,
    make_fake_lxmf_config,
    make_fake_matrix_config,
    make_fake_meshcore_config,
    make_fake_meshtastic_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clean_path_env(monkeypatch)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Fake adapter ID propagation
# ---------------------------------------------------------------------------


class TestFakeAdapterIdPropagation:
    """Fake adapters receive and report the configured adapter_id."""

    def test_fake_matrix_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MatrixRuntimeConfig(
            adapter_id="custom_matrix_id",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(matrix={"cm": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "custom_matrix_id" in adapters
        assert adapters["custom_matrix_id"].adapter_id == "custom_matrix_id"

    def test_fake_meshtastic_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MeshtasticRuntimeConfig(
            adapter_id="custom_mesh_id",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshtastic={"cm": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "custom_mesh_id" in adapters
        assert adapters["custom_mesh_id"].adapter_id == "custom_mesh_id"

    def test_fake_meshcore_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = MeshCoreRuntimeConfig(
            adapter_id="custom_core_id",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshcore_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(meshcore={"cc": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "custom_core_id" in adapters
        assert adapters["custom_core_id"].adapter_id == "custom_core_id"

    def test_fake_lxmf_adapter_id(self, tmp_paths: MedrePaths) -> None:
        rt = LxmfRuntimeConfig(
            adapter_id="custom_lxmf_id",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_lxmf_config(),
        )
        config = RuntimeConfig(
            adapters=AdapterConfigSet(lxmf={"cl": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        adapters: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters)
        assert "custom_lxmf_id" in adapters
        assert adapters["custom_lxmf_id"].adapter_id == "custom_lxmf_id"

    def test_all_four_fakes_report_configured_ids(self, tmp_paths: MedrePaths) -> None:
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="id-prop-test"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "fm": MatrixRuntimeConfig(
                        adapter_id="fm_id",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_matrix_config(),
                    )
                },
                meshtastic={
                    "ft": MeshtasticRuntimeConfig(
                        adapter_id="ft_id",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_meshtastic_config(),
                    )
                },
                meshcore={
                    "fc": MeshCoreRuntimeConfig(
                        adapter_id="fc_id",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_meshcore_config(),
                    )
                },
                lxmf={
                    "fl": LxmfRuntimeConfig(
                        adapter_id="fl_id",
                        enabled=True,
                        adapter_kind="fake",
                        config=make_fake_lxmf_config(),
                    )
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        assert len(app.adapters) == 4
        assert app.adapters["fm_id"].adapter_id == "fm_id"
        assert app.adapters["ft_id"].adapter_id == "ft_id"
        assert app.adapters["fc_id"].adapter_id == "fc_id"
        assert app.adapters["fl_id"].adapter_id == "fl_id"


# ---------------------------------------------------------------------------
# Deterministic build ordering
# ---------------------------------------------------------------------------


class TestDeterministicBuildOrdering:
    """Adapters are built in (transport, adapter_id) sorted order."""

    def test_build_adapters_sorted_by_transport_then_id(
        self, tmp_paths: MedrePaths
    ) -> None:
        """_build_adapters processes adapters in deterministic (transport, adapter_id) order."""
        # Configure adapters whose (transport, adapter_id) sort order differs
        # from a plain adapter_id sort.
        rt_matrix = MatrixRuntimeConfig(
            adapter_id="z_matrix",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        rt_mesh = MeshtasticRuntimeConfig(
            adapter_id="a_mesh",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        rt_core = MeshCoreRuntimeConfig(
            adapter_id="m_core",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"zm": rt_matrix},
                meshtastic={"am": rt_mesh},
                meshcore={"mc": rt_core},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        # Track the order adapters are built by patching _build_single_adapter.
        build_order: list[str] = []
        original_build = builder._build_single_adapter

        def _tracking_build(
            transport: str, adapter_id: str, rtc: Any
        ) -> AdapterContract:
            build_order.append(f"{transport}.{adapter_id}")
            return original_build(transport, adapter_id, rtc)

        with patch.object(
            builder, "_build_single_adapter", side_effect=_tracking_build
        ):
            adapters: dict[str, AdapterContract] = {}
            builder._build_adapters(adapters)

        # Expected order: sorted by (transport, adapter_id):
        # ("lxmf", ...) — none configured
        # ("matrix", "z_matrix") — matrix < meshcore < meshtastic alphabetically
        # ("meshcore", "m_core")
        # ("meshtastic", "a_mesh")
        assert build_order == [
            "matrix.z_matrix",
            "meshcore.m_core",
            "meshtastic.a_mesh",
        ]

    def test_build_order_reproducible_across_builds(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Two builds with identical config produce identical adapter dict key order."""
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "m1": MatrixRuntimeConfig(
                        adapter_id="m1",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    )
                },
                meshtastic={
                    "t1": MeshtasticRuntimeConfig(
                        adapter_id="t1",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    )
                },
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        adapters1: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters1)
        order1 = list(adapters1.keys())

        adapters2: dict[str, AdapterContract] = {}
        builder._build_adapters(adapters2)
        order2 = list(adapters2.keys())

        assert order1 == order2


# ---------------------------------------------------------------------------
# All-adapters-build-failure — builder returns app with empty adapters
# ---------------------------------------------------------------------------


class TestAllAdaptersBuildFailure:
    """When every enabled adapter fails construction, build() still returns
    a MedreApp (with empty adapters + populated build_failures).

    The CLI layer is responsible for checking this condition and exiting
    with EXIT_BUILD.  The builder itself does NOT raise — it records failures.
    """

    def test_all_single_adapter_build_failure(self, tmp_paths: MedrePaths) -> None:
        """Single enabled adapter fails -> build() returns app with empty adapters."""
        rt = MatrixRuntimeConfig(
            adapter_id="broken",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(matrix={"brk": rt}),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        with patch.object(
            builder,
            "_build_single_adapter",
            side_effect=RuntimeConfigError("simulated missing SDK"),
        ):
            app = builder.build()

        assert len(app.adapters) == 0
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "broken"

    def test_all_multiple_adapters_build_failure(self, tmp_paths: MedrePaths) -> None:
        """Multiple enabled adapters all fail -> empty adapters, all recorded."""
        rt1 = MatrixRuntimeConfig(
            adapter_id="broken_a",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        rt2 = MeshtasticRuntimeConfig(
            adapter_id="broken_b",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"ba": rt1},
                meshtastic={"bb": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)

        with patch.object(
            builder,
            "_build_single_adapter",
            side_effect=RuntimeConfigError("simulated missing SDK"),
        ):
            app = builder.build()

        assert len(app.adapters) == 0
        assert len(app.build_failures) == 2
        failed_ids = {bf.adapter_id for bf in app.build_failures}
        assert failed_ids == {"broken_a", "broken_b"}

    def test_partial_failure_not_all_empty(self, tmp_paths: MedrePaths) -> None:
        """One adapter builds, one fails -> adapters non-empty, one build_failure."""
        rt1 = MatrixRuntimeConfig(
            adapter_id="good_one",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_matrix_config(),
        )
        rt2 = MeshtasticRuntimeConfig(
            adapter_id="bad_one",
            enabled=True,
            adapter_kind="fake",
            config=make_fake_meshtastic_config(),
        )
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={"g": rt1},
                meshtastic={"b": rt2},
            ),
        )
        builder = RuntimeBuilder(config, tmp_paths)
        original_build = builder._build_single_adapter

        def _selective_fail(
            transport: str, adapter_id: str, rtc: Any
        ) -> AdapterContract:
            if adapter_id == "bad_one":
                raise RuntimeConfigError("simulated failure")
            return original_build(transport, adapter_id, rtc)

        with patch.object(
            builder, "_build_single_adapter", side_effect=_selective_fail
        ):
            app = builder.build()

        assert len(app.adapters) == 1
        assert "good_one" in app.adapters
        assert len(app.build_failures) == 1
        assert app.build_failures[0].adapter_id == "bad_one"
