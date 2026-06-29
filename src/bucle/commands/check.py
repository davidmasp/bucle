from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def check(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """Validate a bucle config file."""
    from bucle.cli import load_config

    try:
        load_config(config)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Config OK: {config}")
