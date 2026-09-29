"""Docker availability gates for live harnesses.

``HAS_DOCKER`` is the import-time executable check; the daemon probe
adds a responsiveness check for harnesses that must distinguish "Docker
not installed" from "daemon unreachable" at run time.  The probe is
cached per process and spawns ``docker info`` only when actually
called, so default-suite collection never launches a subprocess.
"""

from __future__ import annotations

import functools
import shutil
import subprocess

HAS_DOCKER = shutil.which("docker") is not None


@functools.lru_cache(maxsize=1)
def docker_daemon_reachable() -> bool:
    """Return True when the Docker CLI exists and the daemon answers."""
    if not HAS_DOCKER:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0
