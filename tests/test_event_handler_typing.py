# medre-test-tier: compatibility
"""Consumer typing for positional event callbacks and incompatible payloads."""

import subprocess
import sys
from pathlib import Path


def test_event_handlers_accept_parameter_names_but_reject_incompatible_types(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    client = tmp_path / "handler_client.py"
    source = """from medre.core.events.bus import EventBus
from medre.core.events.canonical import CanonicalEvent

async def valid(message: CanonicalEvent) -> None:
    pass

async def invalid(message: int) -> None:
    pass

bus = EventBus()
bus.subscribe("message.created", valid)
bus.subscribe("message.created", invalid)
"""
    client.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--config-file",
            str(root / "pyproject.toml"),
            str(client),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    rejected_line = (
        source.splitlines().index('bus.subscribe("message.created", invalid)') + 1
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(errors) == 1, result.stdout + result.stderr
    assert f":{rejected_line}: error:" in errors[0]
    assert "[arg-type]" in errors[0]
