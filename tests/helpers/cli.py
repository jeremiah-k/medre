"""Shared CLI test helpers: config constants and runner utilities.

Imported by focused test_cli_*.py files split from the original test_cli.py
monolith.  Contains no pytest fixtures — those live in each test file so
pytest can discover them.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from medre.cli import main

# ---------------------------------------------------------------------------
# Sample TOML configs used across route / config / diagnostics / run tests
# ---------------------------------------------------------------------------

CONFIG_WITH_ROUTES = """\
[runtime]
name = "test-routes"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
connection_type = "serial"
serial_port = "/dev/ttyACM0"
origin_label = "TestMesh"

[routes.matrix_to_radio]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true

[routes.radio_to_matrix]
source_adapters = ["radio"]
dest_adapters = ["main"]
directionality = "source_to_dest"
enabled = false

[routes.bidirectional_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "bidirectional"
enabled = true
source_room = "!room:test"
dest_channel = "1"

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
"""

CONFIG_NO_ROUTES = """\
[runtime]
name = "test-no-routes"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"
"""

CONFIG_WITH_ROUTE_TARGETING = """\
[runtime]
name = "test-targets"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.src]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.matrix.dst]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot2:test"
access_token = "tok2"
room_allowlist = ["!room2:test"]
encryption_mode = "plaintext"

[routes.targeted_route]
source_adapters = ["src"]
dest_adapters = ["dst"]
directionality = "bidirectional"
enabled = true
source_room = "!room:test"
dest_room = "!room2:test"

[routes.targeted_route.policy]
allowed_event_types = ["message", "reaction"]
"""

CONFIG_ROUTE_UNKNOWN_ADAPTERS = """\
[runtime]
name = "test-unknown"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[routes.orphan_route]
source_adapters = ["nonexistent"]
dest_adapters = ["also_missing"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_MINIMAL = """\
[runtime]
"""

CONFIG_BAD_LIMITS = """\
[runtime]
name = "test-bad-limits"

[runtime.limits]
max_inflight_deliveries = -1

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"
"""

CONFIG_NO_ADAPTERS = """\
[runtime]
name = "test-no-adapters"

[storage]
backend = "sqlite"
path = "{state}/test.db"
"""

CONFIG_DISABLED_ADAPTER_IN_ROUTE = """\
[runtime]
name = "test-disabled-adapter-route"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.offline]
enabled = false
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.matrix.active]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot2:test"
access_token = "tok2"
room_allowlist = ["!room2:test"]
encryption_mode = "plaintext"

[routes.uses_disabled]
source_adapters = ["active"]
dest_adapters = ["offline"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_DISABLED_ROUTE_UNKNOWN_REFS = """\
[runtime]
name = "test-disabled-unknown"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[routes.ghost_route]
source_adapters = ["phantom"]
dest_adapters = ["specter"]
directionality = "source_to_dest"
enabled = false
"""


# ---------------------------------------------------------------------------
# Inspect-related config constants
# ---------------------------------------------------------------------------

CONFIG_INSPECT_SQLITE = """\
[runtime]
name = "test-inspect"

[storage]
backend = "sqlite"
path = "{state}/inspect.db"
"""

CONFIG_INSPECT_MEMORY = """\
[runtime]
name = "test-inspect-memory"

[storage]
backend = "memory"
"""

CONFIG_FAKE_MULTI = """\
[runtime]
name = "workflow-test"
shutdown_timeout_seconds = 5

[runtime.limits]
max_inflight_deliveries = 50
max_inflight_replay_events = 25
shutdown_drain_timeout_seconds = 3
delivery_acquire_timeout_seconds = 0.5

[logging]
level = "INFO"
format = "text"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake_tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
origin_label = "TestMesh"

[routes.matrix_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:fake.local"
dest_channel = "1"

[routes.mesh_to_matrix]
source_adapters = ["fake_mesh"]
dest_adapters = ["fake_matrix"]
directionality = "source_to_dest"
enabled = false

[routes.bidirectional_bridge]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "bidirectional"
enabled = true

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
"""

CONFIG_SINGLE_ADAPTER = """\
[runtime]
name = "single-adapter"

[storage]
backend = "memory"

[adapters.matrix.solo]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_single"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str, tmp_path: Path | None = None) -> str:
    """Run CLI with given args, capture stdout, and return output."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        # SystemExit(0) is fine (e.g. --help); non-zero is an error
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return stdout.getvalue(), stderr.getvalue(), code
