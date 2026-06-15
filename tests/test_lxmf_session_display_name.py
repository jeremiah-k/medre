"""Tests for ``LxmfSession.resolve_display_name`` — local announce-cache lookup.

``resolve_display_name`` is a synchronous, never-raising method that bridges
the RNS/LXMF announce cache (``RNS.Identity.known_destinations``) to
adapter-native enrichment. It returns a stripped display name string when the
sender is known locally, or ``None`` for any guard failure.

All tests use mocks — no real RNS/LXMF SDK required. The method is
synchronous, so tests are plain ``def`` functions (no async needed).

Covered guard cases
-------------------
* Type/value: None, non-str, empty, whitespace-only inputs
* Hex decode: non-hex, odd-length
* SDK availability: HAS_LXMF=False, _reticulum=None (lifecycle sentinel)
* recall_app_data: raises, returns None
* display_name_from_app_data: raises, returns None, returns non-str,
  returns empty/whitespace, returns padded string
* Adversarial: never raises for any input
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A valid 32-char hex destination hash (16 bytes) used across happy-path tests.
_VALID_HASH = "0123456789abcdef0123456789abcdef"


def _make_config(**overrides: Any) -> LxmfConfig:
    """Build a fake-mode LxmfConfig with test defaults."""
    defaults: dict[str, Any] = dict(adapter_id="lxmf-display-test")
    defaults.update(overrides)
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    """Construct an LxmfSession WITHOUT starting it.

    In fake mode ``_reticulum`` stays ``None`` from ``__init__``. Tests that
    need a non-None sentinel set ``session._reticulum = MagicMock()`` directly
    after construction.
    """
    config = _make_config(**config_overrides)
    return LxmfSession(config=config, adapter_id=config.adapter_id)


def _mock_rns_lxmf() -> tuple[MagicMock, MagicMock]:
    """Build a mock (RNS, lxmf) module pair for patching _require_lxmf."""
    mock_rns = MagicMock()
    mock_lxmf = MagicMock()
    return mock_rns, mock_lxmf


def _patch_sdk(mock_rns: MagicMock, mock_lxmf: MagicMock, has_lxmf: bool = True):
    """Context manager stack patching HAS_LXMF and _require_lxmf.

    Returns a tuple of patchers that must be started/stopped. Callers use
    this inside a ``with`` statement via ``ExitStack`` or individual patches.
    """
    return (
        patch("medre.adapters.lxmf.session.HAS_LXMF", has_lxmf),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    )


# ===================================================================
# Happy path
# ===================================================================


def test_resolve_display_name_returns_name_for_known_hash() -> None:
    """When recall returns app_data and the parser returns a name,
    the stripped display name is returned.

    Contract: a known peer with a valid announce produces a plain string.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"\x91\xa5Alice"
    mock_lxmf.display_name_from_app_data.return_value = "Alice"

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result == "Alice"
    mock_rns.Identity.recall_app_data.assert_called_once_with(
        bytes.fromhex(_VALID_HASH)
    )
    mock_lxmf.display_name_from_app_data.assert_called_once()


def test_resolve_display_name_returns_none_for_unknown_hash() -> None:
    """When recall_app_data returns None (peer not in announce cache),
    None is returned.

    Contract: an unknown sender produces None, not an error.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = None

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None
    # display_name_from_app_data must NOT be called when app_data is None.
    mock_lxmf.display_name_from_app_data.assert_not_called()


# ===================================================================
# Type and value guards
# ===================================================================


def test_resolve_display_name_returns_none_for_empty_hash() -> None:
    """An empty string source_hash returns None.

    Contract: empty input short-circuits before any SDK access.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name("")

    assert result is None


def test_resolve_display_name_returns_none_for_none_input() -> None:
    """A None source_hash returns None.

    Contract: None input never raises; returns None.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name(None)  # type: ignore[arg-type]

    assert result is None


@pytest.mark.parametrize("bad_input", [123, {}, b"bytes", []])
def test_resolve_display_name_returns_none_for_non_str_input(
    bad_input: Any,
) -> None:
    """Non-string source_hash values all return None.

    Contract: int, dict, bytes, list inputs never raise; return None.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name(bad_input)  # type: ignore[arg-type]

    assert result is None


def test_resolve_display_name_returns_none_for_whitespace_only_hash() -> None:
    """A whitespace-only source_hash returns None.

    Contract: whitespace short-circuits before hex decode or SDK access.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name("   ")

    assert result is None


# ===================================================================
# Hex decode guards
# ===================================================================


def test_resolve_display_name_returns_none_for_non_hex_hash() -> None:
    """A non-hexadecimal source_hash returns None.

    Contract: bytes.fromhex ValueError is caught; returns None.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name("xyz")

    assert result is None


def test_resolve_display_name_returns_none_for_odd_length_hex() -> None:
    """An odd-length hex source_hash returns None.

    Contract: bytes.fromhex ValueError for odd-length input is caught.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name("abc")

    assert result is None


def test_resolve_display_name_returns_none_for_incorrect_length_hex() -> None:
    """A hex string of incorrect length (not 32 chars / 16 bytes) returns None.

    Contract: valid hex that decodes to != 16 bytes is rejected before
    any SDK lookup.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name("0123456789abcdef")

    assert result is None


# ===================================================================
# SDK availability guards
# ===================================================================


def test_resolve_display_name_returns_none_when_has_lxmf_false() -> None:
    """When HAS_LXMF is False (SDK not installed), None is returned.

    Contract: no SDK means no lookup; returns None without touching modules.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", False),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None
    # _require_lxmf must NOT be called when HAS_LXMF is False.
    mock_rns.Identity.recall_app_data.assert_not_called()


def test_resolve_display_name_returns_none_when_reticulum_none() -> None:
    """When _reticulum is None (before start, after stop, fake mode),
    None is returned.

    Contract: the lifecycle sentinel gates all SDK lookups. This test
    does NOT set _reticulum, so it stays None from __init__.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    session = _make_session(connection_type="fake")
    # Deliberately do NOT set session._reticulum — it stays None.

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None
    mock_rns.Identity.recall_app_data.assert_not_called()


# ===================================================================
# recall_app_data failure guards
# ===================================================================


def test_resolve_display_name_returns_none_when_recall_raises() -> None:
    """When recall_app_data raises any exception, None is returned.

    Contract: recall can raise AttributeError when
    RNS.Reticulum.get_instance() returns None; this must be swallowed.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.side_effect = AttributeError(
        "'NoneType' object has no attribute '_used_destination_data'"
    )

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None


# ===================================================================
# display_name_from_app_data failure guards
# ===================================================================


def test_resolve_display_name_returns_none_when_display_name_from_app_data_raises() -> (
    None
):
    """When display_name_from_app_data raises any exception, None is returned.

    Contract: parser errors are swallowed; returns None.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.side_effect = ValueError("bad msgpack")

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None


def test_resolve_display_name_returns_none_for_empty_display_name() -> None:
    """When the parser returns an empty string, None is returned.

    Contract: empty display name normalises to None, not "".
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.return_value = ""

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None


def test_resolve_display_name_returns_none_for_whitespace_display_name() -> None:
    """When the parser returns a whitespace-only string, None is returned.

    Contract: whitespace display name normalises to None after strip().
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.return_value = "   "

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None


@pytest.mark.parametrize("non_str_value", [123, {"key": "val"}, None, b"bytes"])
def test_resolve_display_name_returns_none_for_non_str_display_name(
    non_str_value: Any,
) -> None:
    """When the parser returns a non-str value, None is returned.

    Contract: int, dict, None, bytes display names are rejected by
    isinstance check; returns None.
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.return_value = non_str_value

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result is None


# ===================================================================
# Whitespace stripping
# ===================================================================


def test_resolve_display_name_strips_surrounding_whitespace() -> None:
    """When the parser returns a padded string, surrounding whitespace
    is stripped.

    Contract: "  Alice  " becomes "Alice".
    """
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.return_value = "  Alice  "

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(_VALID_HASH)

    assert result == "Alice"


# ===================================================================
# Integrity: hash never returned as name
# ===================================================================


def test_resolve_display_name_does_not_return_source_hash_as_name() -> None:
    """The source hash itself is never returned as the display name.

    Contract: resolve_display_name returns the announce-derived name,
    not the raw source hash. Even if the parser returned the hash (which
    it should not), the result must be the parsed value.
    """
    source_hash = _VALID_HASH
    mock_rns, mock_lxmf = _mock_rns_lxmf()
    mock_rns.Identity.recall_app_data.return_value = b"app_data"
    mock_lxmf.display_name_from_app_data.return_value = "Alice"

    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        result = session.resolve_display_name(source_hash)

    assert result is not None
    assert result != source_hash
    assert result == "Alice"


# ===================================================================
# Adversarial: never raises
# ===================================================================


@pytest.mark.parametrize(
    "adversarial",
    [
        None,
        123,
        b"bytes",
        {},
        [],
        Exception("boom"),
        object(),
    ],
    ids=[
        "none",
        "int",
        "bytes",
        "dict",
        "list",
        "exception",
        "object",
    ],
)
def test_resolve_display_name_never_raises(adversarial: Any) -> None:
    """No adversarial input causes resolve_display_name to raise.

    Contract: the method is total — it returns None for any input
    type or value without raising. This is the never-raise invariant.
    """
    session = _make_session(connection_type="fake")
    session._reticulum = MagicMock()

    with patch("medre.adapters.lxmf.session.HAS_LXMF", True):
        result = session.resolve_display_name(adversarial)  # type: ignore[arg-type]

    assert result is None
