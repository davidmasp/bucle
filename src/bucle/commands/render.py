from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def render(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """Render task and log HTML files into the .bucle directory."""
    from bucle.cli import load_config, render_site

    try:
        bucle_config = load_config(config)
        report_path = render_site(bucle_config)
    except ConfigError as error:
        typer.echo(f"Render failed: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Rendered bucle report: {report_path}")
