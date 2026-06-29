from __future__ import annotations

from pathlib import Path

import typer

from bucle.helpers import ConfigError


def reset(
    task_name: str | None = typer.Argument(None, help="Name of the task to reset."),
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Reset all tasks marked with auto-reset = true.",
    ),
) -> None:
    """Reset a task so it can be run again."""
    from bucle.cli import load_config, reset_auto_tasks, reset_task

    try:
        bucle_config = load_config(config)
        if auto:
            reset_count = reset_auto_tasks(bucle_config)
        else:
            if task_name is None:
                raise ConfigError("task name is required unless --auto is used")
            reset_task(bucle_config, task_name)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    if auto:
        typer.echo(f"Reset auto-reset task(s): {reset_count}")
    else:
        typer.echo(f"Reset task: {task_name}")
