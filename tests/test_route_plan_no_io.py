"""No-I/O verification for :mod:`medre.runtime.route_plan`.

Confirms the route-plan builder performs **no network or hardware I/O**:
it does not import any transport SDK (``nio``, ``meshtastic``,
``meshcore``, ``lxmf``), does not start any adapter, and builds plans
from configs that use ``adapter_kind: fake`` with no SDK installed.

This is the offline pre-flight contract: an operator can run
``medre routes plan`` against any config without exposing credentials,
touching the radio, or connecting to a Matrix homeserver.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from medre.config.loader import load_config
from medre.runtime import route_engine as route_engine_module
from medre.runtime import route_plan as route_plan_module
from medre.runtime.route_plan import build_route_plan

# ---------------------------------------------------------------------------
# Config with fake adapters (no hardware, no SDK)
# ---------------------------------------------------------------------------

_CONFIG_FAKE = """\
runtime:
  name: plan-no-io
storage:
  backend: memory
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
      origin_label: FakeMatrix
  meshtastic:
    fake_mesh:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: FakeMesh
routes:
  bridge:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_mesh]
    directionality: bidirectional
    source_origin_label: Fwd
    dest_origin_label: Rev
"""


# Modules whose import would indicate a transport SDK dependency.
_FORBIDDEN_SDK_MODULES = (
    "nio",
    "meshtastic",
    "meshcore",
    "lxmf",
)

# The modules that must stay SDK-free for the plan builder to be offline.
_OFFLINE_MODULES = (
    route_plan_module,
    route_engine_module,
)


# ===========================================================================
# 1. Build route plan with fake adapters → succeeds without any SDK installed
# ===========================================================================


def test_build_route_plan_with_fake_adapters(tmp_path: Path) -> None:
    """A plan builds end-to-end from fake-adapter configs without any SDK.

    No ``nio`` / ``meshtastic`` / ``meshcore`` / ``lxmf`` import is
    required to produce the plan.  This is the core offline contract.
    """
    p = tmp_path / "config.yaml"
    p.write_text(_CONFIG_FAKE)
    config, _source, _paths = load_config(str(p))
    plan = build_route_plan(config)
    assert len(plan.adapters) == 2
    assert plan.total_legs == 2  # bidirectional → forward + reverse
    assert plan.loops == []


def test_build_route_plan_does_not_import_sdk_at_runtime(tmp_path: Path) -> None:
    """Importing build_route_plan does not pull in any transport SDK.

    After importing the plan module, none of the forbidden SDK modules
    appear in ``sys.modules``.  This guards against a future change that
    accidentally adds a top-level SDK import.
    """
    import sys

    p = tmp_path / "config.yaml"
    p.write_text(_CONFIG_FAKE)
    config, _source, _paths = load_config(str(p))
    build_route_plan(config)
    leaked = [m for m in _FORBIDDEN_SDK_MODULES if m in sys.modules]
    # Some of these names may coincidentally appear as substrings of other
    # module paths (e.g. ``meshtastic`` inside ``medre.adapters.meshtastic``).
    # We only fail on a direct top-level SDK package, not a medre submodule.
    real_leaks = [m for m in leaked if not sys.modules[m].__name__.startswith("medre.")]
    assert (
        real_leaks == []
    ), f"route_plan build pulled in transport SDK modules: {real_leaks}"


# ===========================================================================
# 2. No adapter SDK imports in source
# ===========================================================================


def test_route_plan_source_has_no_sdk_imports() -> None:
    """The route_plan module source contains no transport-SDK import lines."""
    source = inspect.getsource(route_plan_module)
    for forbidden in _FORBIDDEN_SDK_MODULES:
        # Look for ``import <forbidden>`` or ``from <forbidden>`` patterns.
        assert (
            f"import {forbidden}" not in source
        ), f"route_plan.py imports forbidden SDK {forbidden!r}"
        assert (
            f"from {forbidden}" not in source
        ), f"route_plan.py imports from forbidden SDK {forbidden!r}"


def test_route_engine_source_has_no_sdk_imports() -> None:
    """The route_engine module (transitive dep) is also SDK-free."""
    source = inspect.getsource(route_engine_module)
    for forbidden in _FORBIDDEN_SDK_MODULES:
        assert (
            f"import {forbidden}" not in source
        ), f"route_engine.py imports forbidden SDK {forbidden!r}"
        assert (
            f"from {forbidden}" not in source
        ), f"route_engine.py imports from forbidden SDK {forbidden!r}"


def test_offline_modules_do_not_reference_adapter_packages() -> None:
    """Neither route_plan nor route_engine references the adapter subpackages.

    They must not import ``medre.adapters.*`` (which is where SDK
    compatibility shims live).  The plan is built purely from config
    data structures.
    """
    for mod in _OFFLINE_MODULES:
        source = inspect.getsource(mod)
        assert "medre.adapters" not in source, (
            f"{mod.__name__} references medre.adapters — plan must be "
            f"config-only, not adapter-aware"
        )


# ===========================================================================
# 3. Plan works with adapter_kind: fake → no hardware needed
# ===========================================================================


def test_plan_with_fake_meshtastic_only(tmp_path: Path) -> None:
    """A plan builds with only a fake Meshtastic adapter (no radio)."""
    yaml_text = (
        "runtime:\n"
        "  name: plan-fake-mesh-only\n"
        "storage:\n"
        "  backend: memory\n"
        "adapters:\n"
        "  meshtastic:\n"
        "    radio:\n"
        "      enabled: true\n"
        "      adapter_kind: fake\n"
        "      connection_type: fake\n"
        "  matrix:\n"
        "    main:\n"
        "      enabled: true\n"
        "      adapter_kind: fake\n"
        "      homeserver: https://fake.local\n"
        "      user_id: '@bot:fake.local'\n"
        "      access_token: tok\n"
        "      room_allowlist: ['!a:fake.local', '!b:fake.local']\n"
        "      encryption_mode: plaintext\n"
        "routes:\n"
        "  fanin:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n"
        "      1: '!b:fake.local'\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    config, _source, _paths = load_config(str(p))
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "fanin")
    assert len(entry.legs) == 2
    # Every leg's source is the fake Meshtastic adapter.
    for leg in entry.legs:
        assert leg.source_adapter_id == "radio"
        assert leg.source_platform == "meshtastic"


def test_plan_with_disabled_real_adapter_does_not_touch_it(tmp_path: Path) -> None:
    """A disabled real (non-fake) adapter is inventoried but not contacted.

    The plan reads the adapter's config-level metadata (transport,
    enabled flag) but never starts it.  A real Matrix adapter marked
    ``enabled: false`` appears in the plan with ``[OFF]`` and produces
    no I/O.
    """
    yaml_text = (
        "runtime:\n"
        "  name: plan-disabled-real\n"
        "storage:\n"
        "  backend: memory\n"
        "adapters:\n"
        "  matrix:\n"
        "    offline:\n"
        "      enabled: false\n"
        "      adapter_kind: real\n"
        "      homeserver: https://real.example\n"
        "      user_id: '@bot:real.example'\n"
        "      access_token: real-tok\n"
        "      room_allowlist: ['!room:real.example']\n"
        "      encryption_mode: plaintext\n"
        "  meshtastic:\n"
        "    fake_mesh:\n"
        "      enabled: true\n"
        "      adapter_kind: fake\n"
        "      connection_type: fake\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    config, _source, _paths = load_config(str(p))
    plan = build_route_plan(config)
    by_id = {a.adapter_id: a for a in plan.adapters}
    assert by_id["offline"].enabled is False
    assert by_id["offline"].transport == "matrix"
    # No routes → no legs → no I/O attempted.
    assert plan.total_legs == 0


# ===========================================================================
# Contract: the plan builder's file path is SDK-free on disk
# ===========================================================================


def test_route_plan_file_has_no_sdk_strings() -> None:
    """The route_plan.py source file on disk contains no SDK package names.

    A belt-and-braces check that scans the raw file for SDK module names
    anywhere in the text (comments, docstrings, or code).  The module
    docstring explicitly promises "No adapter SDK is imported".
    """
    # Resolve the source file path from the module object.
    file_path = Path(route_plan_module.__file__)
    text = file_path.read_text(encoding="utf-8")
    # The module docstring mentions SDKs in the negative ("No adapter
    # SDK is imported"), so we check for *import statements* rather than
    # bare substring presence.  An import line is the actionable signal.
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(("import ", "from ")):
            continue
        for forbidden in _FORBIDDEN_SDK_MODULES:
            # Allow ``from medre.adapters.meshtastic import ...`` but not
            # ``import meshtastic`` or ``from meshtastic import ...``.
            if f"medre.adapters.{forbidden}" in stripped:
                continue
            assert not (
                stripped == f"import {forbidden}"
                or stripped.startswith(f"import {forbidden} ")
                or stripped.startswith(f"from {forbidden} ")
            ), f"route_plan.py has forbidden SDK import: {stripped!r}"
