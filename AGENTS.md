# Repository Guidelines

## Project Structure & Module Organization

MEDRE is a Python 3.11+ package under `src/medre`. Core routing, storage,
planning, lifecycle, evidence, and replay code is in `src/medre/core`; runtime
assembly is in `src/medre/runtime`; CLI commands are in `src/medre/cli`;
adapters are in `src/medre/adapters`; configuration is in `src/medre/config`.
Tests live in `tests/`, with helpers in `tests/helpers`, fixtures in
`tests/fixtures`, Docker tests in `tests/integration`, and domain
suites in `tests/conformance`, `tests/lifecycle`, and `tests/operational`.
Docs are split into `docs/spec`, `docs/ops`, `docs/dev`, `docs/schemas`, and
`docs/changes`. Example configs are in `examples/configs`.

## Build, Test, and Development Commands

- `pip install -e ".[dev]"`: install the package with pytest and dev
  dependencies.
- `PYTHONPATH=src pytest -q`: run the default suite; live, Docker, and hardware
  tests are deselected by default.
- `PYTHONPATH=src pytest tests/test_pipeline_delivery.py -v`: run a targeted
  file while developing.
- `python -m compileall -q src tests`: verify all Python files compile.
- `PYTHONPATH=src medre smoke --json`: run the Docker-free smoke path through
  the CLI.
- `PYTHONPATH=src pytest -m docker -v` or `-m live -v --tb=short`: run gated
  tiers when prerequisites exist.

## Coding Style & Naming Conventions

Use 4-space indentation, type annotations for public surfaces, and nearby
dataclass/msgspec-style models where they already exist. Modules, functions,
fixtures, and variables use `snake_case`; classes use `PascalCase`; constants
use `UPPER_SNAKE_CASE`. Keep docs in ATX Markdown, wrap prose near 88 columns
where practical, and reserve RFC 2119 terms for `docs/spec`. No formatter or
linter config is committed; match surrounding style.

## Testing Guidelines

Use pytest function style for new tests; `asyncio_mode` is `auto`, so async
decorators are usually unnecessary. Name files `test_*.py` and split by
behavioral domain. Keep test files below the 1,500-line hard ceiling and prefer
a new file before extending one near 1,200 lines. Avoid fixed sleeps; use
deterministic hooks or `tests/helpers/async_utils.py::wait_until`.

## Commit & Pull Request Guidelines

Recent history uses short imperative summaries, often with scoped prefixes such
as `feat(adapters): ...` or `refactor(diagnostics): ...`. Keep commits focused.
Runtime semantic changes require the relevant `docs/spec` page, schema updates
in `docs/schemas`, tests, and a fragment under
`docs/changes/unreleased/NNN-brief-description.md` in the same change. PRs
should describe behavior, list tests run, call out gated tests not run, and
include CLI output when operator-facing behavior changes.

## Security & Configuration Tips

Never commit Matrix tokens, radio identity files, private keys, SQLite state, or
local environment files. Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` environment
overrides for runtime secrets and keep generated configs outside version
control unless they are sanitized examples.
