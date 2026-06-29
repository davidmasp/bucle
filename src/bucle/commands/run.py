from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def run(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
    reverse: bool = typer.Option(
        False,
        "--reverse",
        help="Run pending tasks from last to first.",
    ),
    shuffle: bool = typer.Option(
        False,
        "--shuffle",
        help="Run pending tasks in random order.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print task launch details while running.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of pending tasks to run.",
    ),
) -> None:
    """Run pending tasks and reconcile marker files into the TOML config."""
    from bucle.cli import load_config, reconcile_results, run_pending_tasks

    try:
        bucle_config = load_config(config)
        ran_tasks = run_pending_tasks(
            bucle_config,
            reverse=reverse,
            shuffle=shuffle,
            verbose=verbose,
            limit=limit,
        )
        reconcile_results(bucle_config, ran_tasks)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Completed run: {len(ran_tasks)} pending task(s) processed.")
