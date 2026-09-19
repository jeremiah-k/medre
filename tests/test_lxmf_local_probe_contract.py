"""SDK-free structural guards for the LXMF local-integration probe."""

from __future__ import annotations

import socket

from tests.helpers.lxmf_local_probe import _pick_free_port_pair


def test_loopback_port_pair_is_distinct_and_released_after_selection() -> None:
    first, second = _pick_free_port_pair()

    assert first != second
    # Selection reserves both ports at the same time, then deliberately
    # releases them for the two Reticulum processes to bind.
    for port in (first, second):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", port))
