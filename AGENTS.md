# AGENTS.md

This repository contains `bucle`, a Python CLI that runs sequential agent tasks from a `.bucle.toml` file and reconciles results through marker files in `.bucle/`.

## Stack

- Python `>=3.13`
- Packaging/build: `uv`, `hatchling`
- CLI: `typer`
- Terminal output: `rich`
- Config editing/parsing: `tomlkit`
- HTML rendering: `jinja2`
- Tests: `unittest` via `typer.testing.CliRunner`

## Repository layout

- `src/bucle/cli.py`: main CLI commands and core workflow
- `src/bucle/helpers.py`: shared validation, formatting, template, and marker helpers
- `src/bucle/templates/`: Jinja templates for rendered reports/log views
- `tests/test_cli.py`: primary automated coverage
- `tests/support/fake_agent.py`: helper for task-runner tests

## Local setup

```sh
uv sync
```

## Common commands

Run tests:

```sh
uv run python -m unittest
```

Run a specific test module:

```sh
uv run python -m unittest tests.test_cli
```

Check the CLI manually:

```sh
uv run bucle --help
uv run bucle check
uv run bucle list
uv run bucle run --reverse -v
```

## Behavioral constraints

- The config file is `.bucle.toml` by default.
- Valid task statuses are `success`, `failure`, and `uncompleted`.
- Agent command templates must contain the literal `{{prompt}}` placeholder.
- Task completion is communicated through:
  - `.bucle/success.txt`
  - `.bucle/failure.txt`
- `bucle init` expects an existing `.gitignore` and should not overwrite an existing `.bucle.toml`.
- If a `Justfile` exists, `bucle init` appends `bucle` and `bucle-list` recipes.

## Editing guidance

- Keep changes narrow and consistent with the existing `typer` CLI structure.
- Prefer updating validation and tests together when changing config semantics.
- Preserve `tomlkit`-based document editing so formatting/comments in `.bucle.toml` survive updates.
- When changing report or log rendering, verify both templates under `src/bucle/templates/`.
- Add or update tests in `tests/test_cli.py` for user-visible CLI behavior.

## Before finishing a change

At minimum, run:

```sh
uv run python -m unittest
```
