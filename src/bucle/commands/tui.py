from __future__ import annotations

import curses
from pathlib import Path

import typer

from bucle.helpers import ConfigError


def tui(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of tasks to show.",
    ),
) -> None:
    """Open an interactive task list TUI."""
    from bucle.cli import launch_tui, load_config

    try:
        bucle_config = load_config(config)
        launch_tui(bucle_config, limit=limit)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error
    except curses.error as error:
        typer.echo(f"TUI failed: {error}", err=True)
        raise typer.Exit(1) from error
