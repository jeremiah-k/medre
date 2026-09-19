"""Regression tests for the configured adapter identifier contract.

One contract (``medre.config.identifiers``) governs the four configuration
seams an adapter identifier flows through:

1. the ``adapters.<transport>.<instance_name>`` YAML mapping key,
2. the explicit ``adapter_id`` value inside an instance table,
3. the ``ADAPTER_ID`` value of environment-first adapter creation,
4. the per-adapter state-directory component (``MedrePaths``).

These tests exercise the public consumers (``load_config``,
``apply_env_overrides``, ``medre config check``, ``MedrePaths``) — never
source strings or duplicated validator truth tables.

Baseline-red selectors (fail on the pre-fix tree, pass after):
``test_load_rejects_dangerous_instance_ids`` (backslash / dot-segment /
whitespace / overlong rows), ``test_path_defense_rejects_unsafe_ids``
(backslash / dot-segment / whitespace-only / NUL rows),
``test_env_first_rejects_unsafe_explicit_adapter_id``,
``test_env_first_adapter_id_used_verbatim``, and
``test_config_check_validates_env_first_adapters`` — the pre-fix loader,
path helper, env layer, and ``config check`` all accepted those values.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import (
    AdapterConfigSet,
    MeshtasticRuntimeConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError
from medre.config.sample import generate_sample_config
from tests.helpers.cli import _run_cli_raw

SECRET = "super-secret-token-do-not-echo"

#: Minimal single-matrix-instance YAML.  ``{key}`` is the instance mapping
#: key (quoted — many rejections under test need quoting to be valid YAML).
_MATRIX_INSTANCE_TEMPLATE = """\
runtime:
  name: id-contract

adapters:
  matrix:
    '{key}':
      homeserver: "https://matrix.test"
      user_id: "@bot:test"
      access_token: "tok"
"""


def _write_config(tmp_path: Path, yaml_content: str) -> Path:
    """Write YAML content to a temp config file and return its path."""
    config_file = tmp_path / "adapter-identifier-contract.yaml"
    config_file.write_text(yaml_content, encoding="utf-8")
    return config_file


@pytest.fixture(autouse=True)
def _no_ambient_medre_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient MEDRE_* vars from leaking into override application."""
    for var in list(os.environ):
        if var.startswith("MEDRE_"):
            monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# YAML load: dangerous / ambiguous identifiers rejected before any FS work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",  # dot-segment traversal component
        "..",
        ".",
        "...",  # dot-only, and normalizes to an empty env token
        "-.-",  # normalizes to an empty env token
        "a/b",  # path separator
        "win\\main",  # Windows-style separator on a POSIX host
        "a b",  # whitespace
        "  ",  # whitespace-only
        ".hidden",  # leading dot: hidden/unsafe component
        "-lead",  # leading hyphen: option-like component
        "C:drive",  # drive-letter-like component
        "x" * 256,  # exceeds portable component length
        "CON",
        "con.txt",
        "NuL.log",
        "COM1",
        "lpt9.cfg",
        "radio.",
    ],
    ids=repr,
)
def test_load_rejects_dangerous_instance_ids(tmp_path: Path, bad_id: str) -> None:
    """The YAML instance key is a configured identifier and must obey the
    contract — rejected at load time, before any state directory exists."""
    config_file = _write_config(tmp_path, _MATRIX_INSTANCE_TEMPLATE.format(key=bad_id))
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.section_path == f"adapters.matrix.{bad_id}"
    assert exc_info.value.transport == "matrix"


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a\\b", " . "])
def test_load_rejects_dangerous_adapter_id_overrides(
    tmp_path: Path, bad_id: str
) -> None:
    """Explicit string adapter_id values obey the instance-key contract.
    (``repr`` keeps every row a *quoted* string; the YAML-bool hazard is
    covered separately below.)"""

    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        "    good-key:\n"
        f"      adapter_id: {bad_id!r}\n"
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
        '      access_token: "tok"\n'
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.section_path == "adapters.matrix.good-key"


@pytest.mark.parametrize("raw_value", ["no", "yes", "on", "123"])
def test_load_rejects_yaml_scalar_adapter_id(tmp_path: Path, raw_value: str) -> None:
    """Unquoted YAML scalars (bool/int) as adapter_id are rejected as
    non-strings at load time, exactly like numeric instance keys."""
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        "    good-key:\n"
        f"      adapter_id: {raw_value}\n"
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
        '      access_token: "tok"\n'
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError, match="must be a string"):
        load_config(str(config_file))


def test_load_rejects_non_string_instance_key(tmp_path: Path) -> None:
    """An unquoted numeric YAML key becomes an int adapter_id — rejected
    with guidance instead of a TypeError later at path derivation."""
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        "    123:\n"
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
        '      access_token: "tok"\n'
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError, match="string"):
        load_config(str(config_file))


def test_load_rejects_cross_transport_duplicate_id(tmp_path: Path) -> None:
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        "    main:\n"
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
        '      access_token: "tok"\n'
        "  meshtastic:\n"
        "    radio:\n"
        "      adapter_id: main\n"
        "      connection_type: fake\n"
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError, match="unique across all"):
        load_config(str(config_file))


@pytest.mark.parametrize(
    ("matrix_id", "meshtastic_id"),
    [
        ("radio.a", "radio_a"),  # distinct values, same normalized token
        ("Main", "main"),  # case-folded token collision across transports
    ],
)
def test_load_rejects_env_token_collision(
    tmp_path: Path, matrix_id: str, meshtastic_id: str
) -> None:
    """Two IDs that normalize to one MEDRE_ADAPTER__<TOKEN> are ambiguous
    for env overrides and rejected at load time (all transports)."""
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        f'    "{matrix_id}":\n'
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
        '      access_token: "tok"\n'
        "  meshtastic:\n"
        f'    "{meshtastic_id}":\n'
        "      connection_type: fake\n"
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError, match="env token collision"):
        load_config(str(config_file))


def test_identifier_error_does_not_echo_secrets(tmp_path: Path) -> None:
    """Rejection messages carry the (non-secret) identifier only."""
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        '    "../escape":\n'
        f'      access_token: "{SECRET}"\n'
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:test"\n'
    )
    config_file = _write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(str(config_file))
    assert SECRET not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Direct path defense (defense in depth for direct callers)
# ---------------------------------------------------------------------------


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    from medre.config.paths import resolve

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    return resolve()


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "  ",
        ".",
        "..",
        "...",
        "a/b",
        "a\\b",  # rejected on every host, not only where os.sep matches
        "a\x00b",  # NUL would fail mkdir late — rejected here instead
        "C:drive",
        "CON",
        "con.txt",
        "radio.",
        "x" * 256,
    ],
    ids=repr,
)
def test_path_defense_rejects_unsafe_ids(paths: MedrePaths, bad_id: str) -> None:
    with pytest.raises(MedrePathsError):
        paths.adapter_state_dir(bad_id)


@pytest.mark.parametrize(
    "good_id",
    ["main", "radio", "radio-a", "radio.a", "mc_node", "meshcore_lab", "a1"],
)
def test_path_defense_accepts_valid_ids(paths: MedrePaths, good_id: str) -> None:
    result = paths.adapter_state_dir(good_id)
    assert result == paths.state_dir / "adapters" / good_id
    assert result.parent == paths.state_dir / "adapters"


# ---------------------------------------------------------------------------
# Env-first creation: same contract, no silent normalization
# ---------------------------------------------------------------------------


def _base_config():
    from medre.config.model import RuntimeConfig

    return RuntimeConfig()


def test_env_first_rejects_unsafe_explicit_adapter_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDRE_ADAPTER__EVIL__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__EVIL__ADAPTER_ID", "../escape")
    with pytest.raises(ConfigValidationError) as exc_info:
        apply_env_overrides(_base_config())
    msg = str(exc_info.value)
    assert "MEDRE_ADAPTER__EVIL__ADAPTER_ID" in msg
    assert "../escape" in msg


@pytest.mark.parametrize("bad_value", [" win/main ", "a/b", "..", "  ", "x" * 256])
def test_env_first_adapter_id_used_verbatim(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    """ADAPTER_ID values are never silently stripped or renamed: values
    that only differ by surrounding whitespace are rejected, not trimmed."""
    monkeypatch.setenv("MEDRE_ADAPTER__X__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__X__ADAPTER_ID", bad_value)
    with pytest.raises(ConfigValidationError, match="ADAPTER_ID"):
        apply_env_overrides(_base_config())


def test_env_first_valid_explicit_adapter_id_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDRE_ADAPTER__X__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__X__ADAPTER_ID", "custom-radio")
    monkeypatch.setenv("MEDRE_ADAPTER__X__CONNECTION_TYPE", "fake")
    result = apply_env_overrides(_base_config())
    assert "custom-radio" in result.adapters.meshtastic


def test_env_and_yaml_rejections_are_the_same_error_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YAML load and env-first creation reject the same unsafe identifier
    with the same operator-facing error category."""
    bad_id = "../escape"
    config_file = _write_config(tmp_path, _MATRIX_INSTANCE_TEMPLATE.format(key=bad_id))
    with pytest.raises(ConfigValidationError) as yaml_exc:
        load_config(str(config_file))

    monkeypatch.setenv("MEDRE_ADAPTER__Y__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__Y__ADAPTER_ID", bad_id)
    with pytest.raises(ConfigValidationError) as env_exc:
        apply_env_overrides(_base_config())

    assert type(yaml_exc.value) is type(env_exc.value)
    assert bad_id in str(yaml_exc.value)
    assert bad_id in str(env_exc.value)


# ---------------------------------------------------------------------------
# Model seam: hand-built configuration sets obey the same contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name", ["../legacy", "..", "a/b", " a ", "CON", "con.txt", "radio."]
)
def test_model_set_rejects_invalid_instance_names_with_safe_adapter_id(
    bad_name: str,
) -> None:
    rtc = MeshtasticRuntimeConfig(
        adapter_id="safe",
        config=MeshtasticConfig(adapter_id="safe", connection_type="fake"),
    )
    adapters = AdapterConfigSet(meshtastic={bad_name: rtc})

    with pytest.raises(ConfigValidationError) as exc_info:
        adapters.validate()

    assert exc_info.value.section_path == f"adapters.meshtastic.{bad_name}"
    assert exc_info.value.adapter_id == bad_name
    assert "invalid adapter instance name" in str(exc_info.value)


@pytest.mark.parametrize("bad_id", ["a/b", "a\\b", "", "  ", "a\x00b", 123])
def test_model_set_rejects_invalid_ids(bad_id: object) -> None:
    rtc = MeshtasticRuntimeConfig(
        adapter_id=bad_id,  # type: ignore[arg-type]
        config=(
            MeshtasticConfig(adapter_id=str(bad_id), connection_type="fake")
            if isinstance(bad_id, str)
            else None
        ),
    )
    adapters = AdapterConfigSet(meshtastic={"bad": rtc})
    with pytest.raises(ConfigValidationError):
        adapters.validate()


# ---------------------------------------------------------------------------
# CLI config check: pre-flight gate agrees with runtime construction
# ---------------------------------------------------------------------------


def test_config_check_rejects_dangerous_id_without_traceback(
    tmp_path: Path,
) -> None:
    config_file = _write_config(
        tmp_path, _MATRIX_INSTANCE_TEMPLATE.format(key="../escape")
    )
    stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(config_file))
    assert code == 2
    assert "Config error:" in stderr
    assert "../escape" in stderr
    assert "Traceback" not in stderr + stdout


def test_config_check_validates_env_first_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config check applies env overrides like medre run: an env-created
    adapter with an unsafe ADAPTER_ID fails the pre-flight gate instead of
    passing check and failing (or crashing) at startup."""
    config_file = _write_config(tmp_path, "runtime:\n  name: t\n")
    monkeypatch.setenv("MEDRE_ADAPTER__BAD__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__BAD__ADAPTER_ID", "../escape")
    _stdout, stderr, code = _run_cli_raw(
        "config", "check", "--config", str(config_file)
    )
    assert code == 2
    assert "Config error:" in stderr
    assert "MEDRE_ADAPTER__BAD__ADAPTER_ID" in stderr


def test_config_check_accepts_valid_env_first_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write_config(tmp_path, "runtime:\n  name: t\n")
    monkeypatch.setenv("MEDRE_ADAPTER__OK__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__OK__CONNECTION_TYPE", "fake")
    stdout, _stderr, code = _run_cli_raw(
        "config", "check", "--config", str(config_file)
    )
    assert code == 0
    assert "Config valid" in stdout
    assert "meshtastic.ok" in stdout


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["run", "{config}"], id="run"),
        pytest.param(["diagnostics", "--config", "{config}"], id="diagnostics"),
        pytest.param(
            ["replay", "--mode", "dry_run", "--config", "{config}"],
            id="replay",
        ),
    ],
)
def test_command_boundaries_reject_bad_env_adapter_id_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """Every config-loading CLI command applies env overrides inside its
    config-error boundary: an unsafe env ADAPTER_ID exits with the
    actionable config category, not a traceback, and never echoes
    secrets from the config file."""
    config_file = _write_config(
        tmp_path,
        _MATRIX_INSTANCE_TEMPLATE.format(key="main"),
    )
    monkeypatch.setenv("MEDRE_ADAPTER__BAD__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__BAD__ADAPTER_ID", "../escape")

    stdout, stderr, code = _run_cli_raw(
        *[a.format(config=str(config_file)) for a in argv]
    )
    assert code == 2, f"expected config exit 2, got {code}: {stderr}"
    assert "Config error:" in stderr
    assert "../escape" in stderr
    assert "Traceback" not in stderr + stdout
    assert SECRET not in stderr + stdout


# ---------------------------------------------------------------------------
# Preservation: valid identifiers, free prose, native identities
# ---------------------------------------------------------------------------


def test_valid_names_free_labels_and_native_identities_survive(
    tmp_path: Path,
) -> None:
    """The identifier contract must not leak into other namespaces:
    representative valid IDs load, origin labels stay free prose, and
    native Matrix identities (MXIDs, room IDs) pass through untouched."""
    yaml_text = (
        "adapters:\n"
        "  matrix:\n"
        "    main:\n"
        '      homeserver: "https://matrix.test"\n'
        '      user_id: "@bot:example.com"\n'
        '      access_token: "tok"\n'
        '      room_allowlist: ["!room:example.com"]\n'
        '      origin_label: "Matrix ✨ Bridge #1"\n'
        "  meshtastic:\n"
        "    radio.a:\n"
        "      connection_type: fake\n"
        '      origin_label: "My Mesh — node 42"\n'
        "  meshcore:\n"
        "    mc_node:\n"
        "      connection_type: fake\n"
    )
    config_file = _write_config(tmp_path, yaml_text)
    config, _source, _paths = load_config(str(config_file))

    ids = {aid for _t, aid, _rtc in config.adapters.all_configs()}
    assert ids == {"main", "radio.a", "mc_node"}
    assert config.adapters.matrix["main"].config.origin_label == ("Matrix ✨ Bridge #1")
    assert config.adapters.matrix["main"].config.user_id == "@bot:example.com"
    assert config.adapters.matrix["main"].config.room_allowlist == {"!room:example.com"}
    assert config.adapters.meshtastic["radio.a"].config.origin_label == (
        "My Mesh — node 42"
    )
    config.adapters.validate()


def test_generated_sample_config_round_trips(tmp_path: Path) -> None:
    """The generated sample must load cleanly end-to-end, and its
    store_path guidance must not suggest an unsupported placeholder."""
    sample = generate_sample_config()
    config_file = _write_config(tmp_path, sample)
    config, _source, _paths = load_config(str(config_file))

    ids = {aid for _t, aid, _rtc in config.adapters.all_configs()}
    assert {"main", "radio", "mc_node", "lxmf_node"} <= ids
    # The runtime derives the Matrix crypto store; the sample must not set
    # or suggest setting it with a {adapter_id} placeholder.
    assert config.adapters.matrix["main"].config.store_path is None


def test_json_schema_identifier_patterns_match_runtime_authority() -> None:
    import json

    from medre.config.identifiers import ADAPTER_ID_PATTERN_SOURCE

    root = Path(__file__).resolve().parents[1]
    expected = f"^{ADAPTER_ID_PATTERN_SOURCE}$"
    adapter_schema = json.loads(
        (root / "docs/schemas/adapter-config.schema.json").read_text()
    )
    assert {
        arm["properties"]["adapter_id"]["pattern"]
        for arm in adapter_schema["oneOf"]
    } == {expected}

    runtime_schema = json.loads(
        (root / "docs/schemas/runtime-config.schema.json").read_text()
    )
    groups = runtime_schema["properties"]["adapters"]["properties"]
    assert {group["propertyNames"]["pattern"] for group in groups.values()} == {
        expected
    }
