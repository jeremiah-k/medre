"""Process-isolated real LXMF/RNS probe used by local integration tests."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any


def _make_identity(path: Path) -> None:
    import RNS

    identity = RNS.Identity()
    if not identity.to_file(str(path)):
        raise RuntimeError("failed to persist local LXMF integration identity")


def _config(base: Path, identity_path: Path, *, storage_name: str = "router") -> Any:
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-local-integration",
        connection_type="reticulum",
        identity_path=str(identity_path),
        storage_path=str(base / storage_name),
        announce_interval_seconds=0,
        message_delay_seconds=0,
    )


def _session(config: Any) -> Any:
    from medre.adapters.lxmf.session import LxmfSession

    return LxmfSession(
        adapter_id="lxmf-local-integration",
        config=config,
        logger=logging.getLogger("test.lxmf.local"),
    )


async def _run_suite(base: Path) -> dict[str, Any]:
    from medre.adapters.lxmf.errors import LxmfConnectionError

    identity_path = base / "identity"
    _make_identity(identity_path)
    hashes: list[str] = []
    callback_count = 0

    def on_message(_payload: dict[str, Any]) -> None:
        nonlocal callback_count
        callback_count += 1

    session = _session(_config(base, identity_path))
    for _ in range(3):
        await session.start(on_message)
        assert session.connected is True
        assert session.router_running is True
        destination_hash = session._delivery_destination_hash
        if destination_hash is None:
            raise AssertionError(
                "real LXMRouter did not register a delivery destination"
            )
        hashes.append(destination_hash.hex())

        # Malformed SDK callback payloads must be dropped without reaching the
        # adapter callback or destabilising the active router.
        session._on_lxmf_delivery(object())
        assert callback_count == 0

        await session.stop()
        assert session.connected is False
        assert session.router_running is False

        # Reticulum callbacks can race with teardown; late callbacks are dropped.
        session._on_lxmf_delivery(object())
        assert callback_count == 0

    bad_storage = base / "storage-is-a-file"
    bad_storage.write_text("not a directory", encoding="utf-8")
    failed = _session(_config(base, identity_path, storage_name=bad_storage.name))
    startup_failure = None
    try:
        await failed.start(on_message)
    except LxmfConnectionError as exc:
        startup_failure = type(exc).__name__
    else:
        raise AssertionError("invalid LXMF storage path unexpectedly started")

    assert failed.connected is False
    assert failed.router_running is False
    assert failed._router is None
    assert failed._identity is None

    return {
        "cycles": len(hashes),
        "stable_destination": len(set(hashes)) == 1,
        "callback_count": callback_count,
        "startup_failure": startup_failure,
    }


async def _run_soak(base: Path) -> dict[str, Any]:
    identity_path = base / "identity"
    _make_identity(identity_path)
    session = _session(_config(base, identity_path))
    hashes: list[str] = []
    for _ in range(10):
        await session.start(lambda _payload: None)
        destination_hash = session._delivery_destination_hash
        if destination_hash is None:
            raise AssertionError(
                "real LXMRouter did not register a delivery destination"
            )
        hashes.append(destination_hash.hex())
        await session.stop()
    return {"cycles": len(hashes), "stable_destination": len(set(hashes)) == 1}


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: lxmf_local_probe <suite|soak> <working-directory>")
    scenario = sys.argv[1]
    base = Path(sys.argv[2]).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if scenario == "suite":
        result = asyncio.run(_run_suite(base))
    elif scenario == "soak":
        result = asyncio.run(_run_soak(base))
    else:
        raise SystemExit(f"unknown scenario: {scenario}")
    print("MEDRE_LOCAL_INTEGRATION_RESULT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
