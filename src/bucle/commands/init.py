from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def init() -> None:
    """Create the default bucle files in the current directory."""
    from bucle.cli import init_project

    try:
        init_project(Path.cwd())
    except ConfigError as error:
        typer.echo(f"Init failed: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo("Initialized bucle project.")
