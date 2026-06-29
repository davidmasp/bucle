from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def tasks(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of tasks to list.",
    ),
) -> None:
    """List configured tasks and their current status."""
    from bucle.cli import load_config, print_tasks

    try:
        bucle_config = load_config(config)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    print_tasks(bucle_config, limit=limit)
