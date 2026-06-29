from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def sync(
    author: str = typer.Option(
        ...,
        "--author",
        "--user",
        "-a",
        help="GitHub issue author to sync.",
    ),
    tag: str = typer.Option(
        "bucle",
        "--tag",
        "--label",
        "-l",
        help="GitHub issue label to sync.",
    ),
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """Import open GitHub issues into the bucle config."""
    from bucle.cli import load_config, sync_github_issues

    try:
        bucle_config = load_config(config)
        result = sync_github_issues(bucle_config, author=author, tag=tag)
    except ConfigError as error:
        typer.echo(f"Sync failed: {error}", err=True)
        raise typer.Exit(1) from error

    for message in result.messages:
        typer.echo(message)
    typer.echo(f"Synced GitHub issues: {result.added} added, {result.skipped} skipped.")
