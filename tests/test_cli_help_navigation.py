"""Help navigation follows registered command paths without executing handlers."""

from tests.helpers.cli import _run_cli_raw


def test_help_navigates_without_adapter_imports() -> None:
    for words, expected in (
        (("help",), "Modular Event-driven Routing Engine"),
        (("help", "routes"), "diagram"),
        (("help", "routes", "diagram"), "--full-context"),
        (("--help", "routes", "diagram"), "--full-context"),
        (("routes",), "validate"),
    ):
        out, err, code = _run_cli_raw(*words)
        assert code == 0, err
        assert expected in out


def test_bare_nested_groups_are_helpful_without_running_a_handler():
    for words in (("config",), ("adapter",), ("support",), ("dev",), ("trace",)):
        output, error, code = _run_cli_raw(*words)
        assert code == 0, error
        assert "usage:" in output
    _, error, code = _run_cli_raw("config", "typo")
    assert code == 2 and "invalid choice" in error
