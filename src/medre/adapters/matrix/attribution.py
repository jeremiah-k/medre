"""Matrix-native-to-generic attribution projection.

Projects Matrix sender metadata (MXID, display name) into generic
:class:`~medre.core.rendering.attribution.RelayAttribution`-compatible
fields so that adapter renderers can build attribution without relying on
core-internal extraction helpers.

This module imports **no adapter packages**; it may import core event/model
types when needed.

Public symbols
--------------
* :func:`project_matrix_attribution` — dispatch-oriented projection from native dict (used by ``_attribution_dispatch``).
* :func:`extract_mxid_localpart` — safe MXID localpart extraction.
* :class:`MatrixSenderFields` — immutable projection result (used by ``project_matrix_sender``).
* :func:`project_matrix_sender` — pre-split-args projection (primarily for direct unit tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "MatrixSenderFields",
    "extract_mxid_localpart",
    "project_matrix_attribution",
    "project_matrix_sender",
]


# ---------------------------------------------------------------------------
# MXID helpers
# ---------------------------------------------------------------------------


def extract_mxid_localpart(mxid: str) -> str:
    """Extract the localpart from a Matrix MXID (``@user:domain``).

    Returns the bare localpart when the MXID is well-formed; returns the
    input unchanged when it does not start with ``@`` or has no colon
    separator.

    Examples
    --------
    >>> extract_mxid_localpart("@alice:example.com")
    'alice'
    >>> extract_mxid_localpart("@bob")
    'bob'
    >>> extract_mxid_localpart("plain")
    'plain'
    >>> extract_mxid_localpart("@:example.com")
    ''
    """
    if mxid.startswith("@"):
        rest = mxid[1:]
        colon = rest.find(":")
        if colon >= 0:
            # colon == 0 means an empty localpart (``@:domain``); slice
            # returns ``""`` in that case.
            return rest[:colon]
        return rest
    return mxid


# ---------------------------------------------------------------------------
# Projection result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixSenderFields:
    """Immutable projection of Matrix sender metadata into generic fields.

    All fields are optional (``None`` signals absence).  Field names match
    the corresponding ``RelayAttribution`` canonical names minus the
    ``source_`` prefix used for keyword construction.

    Attributes
    ----------
    sender_id:
        Native sender identifier — the full MXID.
    sender_handle:
        Sender handle / address — the full MXID.
    sender_label:
        Primary human-readable label — displayname if present, else
        MXID localpart, else full MXID.
    sender_short_label:
        Abbreviated label — MXID localpart when available, else ``None``.
    """

    sender_id: str | None = None
    sender_handle: str | None = None
    sender_label: str | None = None
    sender_short_label: str | None = None

    def to_relay_fields(self) -> dict[str, str | None]:
        """Return a dict keyed by ``RelayAttribution`` canonical field names.

        Useful for merging into a ``RelayAttribution`` construction call:

        >>> fields = projection.to_relay_fields()
        >>> RelayAttribution(**fields)
        """
        return {
            "source_sender_id": self.sender_id,
            "source_sender_handle": self.sender_handle,
            "source_sender_label": self.sender_label,
            "source_sender_short_label": self.sender_short_label,
        }


# ---------------------------------------------------------------------------
# Projection function
# ---------------------------------------------------------------------------


def project_matrix_sender(
    mxid: str | None,
    displayname: str | None = None,
) -> MatrixSenderFields:
    """Project Matrix sender metadata into generic attribution fields.

    Parameters
    ----------
    mxid:
        Full Matrix user ID (``@user:domain``).  ``None`` when unavailable.
    displayname:
        Display name from the Matrix profile.  ``None`` when unavailable.

    Returns
    -------
    MatrixSenderFields
        Frozen projection with the following mapping:

        * ``sender_id`` ← *mxid*
        * ``sender_handle`` ← *mxid*
        * ``sender_label`` ← *displayname* if truthy, else
          MXID localpart (when *mxid* is available), else *mxid*
        * ``sender_short_label`` ← MXID localpart (when *mxid* is
          available), else ``None``

    Examples
    --------
    >>> project_matrix_sender("@alice:example.com", "Alice Liddell")
    MatrixSenderFields(sender_id='@alice:example.com', ...)

    >>> project_matrix_sender("@bob:matrix.org")
    MatrixSenderFields(sender_label='bob', ...)
    """
    if mxid is None:
        return MatrixSenderFields(
            sender_id=None,
            sender_handle=None,
            sender_label=displayname or None,
            sender_short_label=None,
        )

    localpart = extract_mxid_localpart(mxid)

    # Fallback chain for sender_label: displayname → localpart → mxid
    if displayname:
        label: str = displayname
    elif localpart:
        label = localpart
    else:
        label = mxid

    return MatrixSenderFields(
        sender_id=mxid,
        sender_handle=mxid,
        sender_label=label,
        sender_short_label=localpart or None,
    )


# ---------------------------------------------------------------------------
# Native-data projection (dispatch-oriented)
# ---------------------------------------------------------------------------


def _str(value: object) -> str | None:
    """Coerce *value* to ``str`` or return ``None`` for missing/empty."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def project_matrix_attribution(
    native_data: dict[str, Any],
) -> dict[str, str | None]:
    """Project Matrix-native metadata dict into generic attribution fields.

    This is the dispatch-oriented entry point that extracts sender identity
    from a raw native-metadata dict (as produced by the codec pipeline).
    It complements :func:`project_matrix_sender` which accepts pre-split
    mxid/displayname arguments.

    Extracted fields:

    * ``source_sender_id`` ← ``native_data["sender"]`` (full MXID).
    * ``source_sender_label`` ← ``native_data["displayname"]`` or
      ``native_data["display_name"]`` (display name only; no localpart
      fallback).
    * ``source_sender_short_label`` ← MXID localpart (when sender is
      available), else ``None``.
    * ``source_sender_handle`` ← full MXID (same as sender_id).

    Parameters
    ----------
    native_data:
        Raw Matrix native metadata dict.  Missing keys are treated as
        absent (not an error).

    Returns
    -------
    dict[str, str | None]
        Generic attribution fields keyed by ``RelayAttribution`` canonical
        names.  Fields are ``None`` when no value could be resolved.
    """
    sender_str = _str(native_data.get("sender"))
    display_name = native_data.get("displayname") or native_data.get("display_name")
    display_str = _str(display_name)
    short_label: str | None = None
    if sender_str:
        short_label = extract_mxid_localpart(sender_str) or None
    return {
        "source_sender_id": sender_str,
        "source_sender_label": display_str,
        "source_sender_short_label": short_label,
        "source_sender_handle": sender_str,
    }
