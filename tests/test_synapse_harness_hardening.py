"""Synapse harness hardening: per-run credentials and daemon probing.

The starter's registration secret and account passwords exist only for
one container's lifetime; fixed committed values would hand any process
on the bench host an admin account on the harness homeserver.  The
Docker executable gate stays import-time cheap; the daemon probe runs
only when the matrix leg is opted in.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.helpers.docker_probe import docker_daemon_reachable
from tests.helpers.synapse_starter import _fresh_credentials


def test_fresh_credentials_differ_across_runs() -> None:
    """Each call yields new secrets; nothing is committed or reused."""
    first = _fresh_credentials()
    second = _fresh_credentials()
    assert first != second
    assert len(set(first)) == 3
    for value in (*first, *second):
        assert value
        assert not any(ch.isspace() for ch in value)
        assert len(value) >= 20


def test_fresh_credentials_shape() -> None:
    """Registration secret is the longest value; both passwords qualify."""
    secret, bot_password, peer_password = _fresh_credentials()
    assert len(secret) >= len(bot_password)
    assert bot_password == bot_password.strip()
    assert peer_password == peer_password.strip()


@pytest.fixture(autouse=True)
def _reset_daemon_cache():
    """Isolate the lru_cache between probes."""
    docker_daemon_reachable.cache_clear()
    yield
    docker_daemon_reachable.cache_clear()


def test_daemon_probe_false_without_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Docker executable: no subprocess is spawned at all."""
    monkeypatch.setattr("tests.helpers.docker_probe.HAS_DOCKER", False)

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess must not run without the CLI")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert docker_daemon_reachable() is False


def test_daemon_probe_true_on_responsive_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-exit `docker info` marks the daemon reachable."""
    monkeypatch.setattr("tests.helpers.docker_probe.HAS_DOCKER", True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )
    assert docker_daemon_reachable() is True


def test_daemon_probe_caches_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs at most once per process regardless of call count."""
    monkeypatch.setattr("tests.helpers.docker_probe.HAS_DOCKER", True)
    calls: list[int] = []

    def _counting_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(1)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    assert docker_daemon_reachable() is True
    assert docker_daemon_reachable() is True
    assert len(calls) == 1


def test_daemon_probe_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung daemon probe reads as unreachable, not as a test failure."""
    monkeypatch.setattr("tests.helpers.docker_probe.HAS_DOCKER", True)

    def _hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=15.0)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert docker_daemon_reachable() is False
