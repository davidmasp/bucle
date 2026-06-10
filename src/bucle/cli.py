from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
import tomlkit
import typer

VALID_STATUSES = {"success", "failure", "uncompleted"}
PROMPT_PLACEHOLDER = "{{prompt}}"
BUCLE_DIR = ".bucle"
SUCCESS_MARKER = "success.json"
FAILURE_MARKER = "failure.json"

app = typer.Typer(help="Run agent tasks from a .bucle.toml file.")


class ConfigError(Exception):
    """Raised when a .bucle.toml file is invalid."""


@dataclass(frozen=True)
class BucleConfig:
    path: Path
    document: Any

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def output_dir(self) -> Path:
        return self.root / BUCLE_DIR

    @property
    def success_marker(self) -> Path:
        return self.output_dir / SUCCESS_MARKER

    @property
    def failure_marker(self) -> Path:
        return self.output_dir / FAILURE_MARKER


@dataclass(frozen=True)
class RunTask:
    name: str
    agent: str
    prompt: str
    index: int


def main() -> None:
    app()


@app.command()
def check(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """Validate a bucle config file."""
    try:
        load_config(config)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Config OK: {config}")


@app.command()
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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print task launch details while running.",
    ),
) -> None:
    """Run pending tasks and reconcile marker files into the TOML config."""
    try:
        bucle_config = load_config(config)
        ran_tasks = run_pending_tasks(bucle_config, reverse=reverse, verbose=verbose)
        reconcile_results(bucle_config, ran_tasks)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Completed run: {len(ran_tasks)} pending task(s) processed.")


@app.command()
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


@app.command("list")
@app.command()
def tasks(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """List configured tasks and their current status."""
    try:
        bucle_config = load_config(config)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    print_tasks(bucle_config)


def load_config(path: Path) -> BucleConfig:
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise ConfigError(f"{resolved_path} does not exist")
    if not resolved_path.is_file():
        raise ConfigError(f"{resolved_path} is not a file")

    try:
        document = tomlkit.parse(resolved_path.read_text())
    except Exception as error:  # tomlkit raises multiple parser exception types.
        raise ConfigError(f"could not parse TOML: {error}") from error

    config = BucleConfig(path=resolved_path, document=document)
    validate_config(config)
    return config


def validate_config(config: BucleConfig) -> None:
    document = config.document
    metadata = require_table(document, "metadata")
    for field in ("name", "preprompt", "postprompt"):
        require_string(metadata, field, f"metadata.{field}")

    agents = require_table(document, "agents")
    agent_names = set(agents.keys())
    if not agent_names:
        raise ConfigError("agents must define at least one named agent")
    for agent_name, agent_config in agents.items():
        if not is_mapping(agent_config):
            raise ConfigError(f"agents.{agent_name} must be a table")
        cmd = require_string(agent_config, "cmd", f"agents.{agent_name}.cmd")
        if PROMPT_PLACEHOLDER not in cmd:
            raise ConfigError(
                f"agents.{agent_name}.cmd must contain {PROMPT_PLACEHOLDER}"
            )

    tasks = document.get("tasks")
    if tasks is None:
        raise ConfigError("tasks must contain at least one task")
    if not isinstance(tasks, list):
        raise ConfigError("tasks must be an array of tables")

    seen_names: set[str] = set()
    for index, task in enumerate(tasks):
        if not is_mapping(task):
            raise ConfigError(f"tasks[{index}] must be a table")
        task_name = require_string(task, "name", f"tasks[{index}].name")
        if task_name in seen_names:
            raise ConfigError(f"duplicate task name: {task_name}")
        seen_names.add(task_name)

        task_agent = require_string(task, "agent", f"tasks[{index}].agent")
        require_string(task, "prompt", f"tasks[{index}].prompt")
        if task_agent not in agent_names:
            raise ConfigError(
                f"tasks[{index}].agent references unknown agent: {task_agent}"
            )

        status = task.get("status")
        if status is not None and status not in VALID_STATUSES:
            raise ConfigError(
                f"tasks[{index}].status must be one of: "
                f"{', '.join(sorted(VALID_STATUSES))}"
            )

        auto_reset = task.get("auto-reset")
        if auto_reset is not None and not isinstance(auto_reset, bool):
            raise ConfigError(f"tasks[{index}].auto-reset must be a boolean")


def run_pending_tasks(
    config: BucleConfig, reverse: bool = False, verbose: bool = False
) -> list[RunTask]:
    config.output_dir.mkdir(exist_ok=True)
    write_json_array(config.success_marker, [])
    write_json_array(config.failure_marker, [])

    pending_tasks = get_pending_tasks(config)
    if reverse:
        pending_tasks.reverse()
    console = Console() if verbose else None
    total_tasks = len(pending_tasks)
    for task_number, task in enumerate(pending_tasks, start=1):
        command = render_command(config, task)
        started_at = datetime.now(timezone.utc)
        log_path = task_log_path(config, task, started_at)
        if console is not None:
            console.print(
                f"Launching task {task_number}/{total_tasks}: {task.name} "
                f"(log: {log_path})"
            )
            with console.status(f"Running task {task.name}", spinner="moon"):
                result = run_task_command(config, command)
        else:
            result = run_task_command(config, command)
        ended_at = datetime.now(timezone.utc)
        write_task_log(log_path, task, command, started_at, ended_at, result)

    return pending_tasks


def reconcile_results(config: BucleConfig, ran_tasks: list[RunTask]) -> None:
    success_entries = read_marker_entries(config.success_marker, "success")
    failure_entries = read_marker_entries(config.failure_marker, "failure")
    success_names = {entry["name"] for entry in success_entries}
    failure_by_name = {entry["name"]: entry for entry in failure_entries}

    document = config.document
    tasks = document["tasks"]
    ran_names = {task.name for task in ran_tasks}
    for task in tasks:
        task_name = str(task["name"])
        if task_name not in ran_names:
            continue

        if task_name in success_names:
            task["status"] = "success"
            task.pop("failure_reason", None)
        elif task_name in failure_by_name:
            task["status"] = "failure"
            reason = failure_by_name[task_name].get("reason")
            if reason:
                task["failure_reason"] = str(reason)
        else:
            task["status"] = "uncompleted"
            task["failure_reason"] = "Agent did not write a success or failure marker."

    config.path.write_text(tomlkit.dumps(document))
    cleanup_marker(config.success_marker)
    cleanup_marker(config.failure_marker)


def reset_task(config: BucleConfig, task_name: str) -> None:
    for task in config.document["tasks"]:
        if str(task["name"]) != task_name:
            continue

        task.pop("status", None)
        task.pop("failure_reason", None)
        config.path.write_text(tomlkit.dumps(config.document))
        return

    raise ConfigError(f"task not found: {task_name}")


def reset_auto_tasks(config: BucleConfig) -> int:
    reset_count = 0
    for task in config.document["tasks"]:
        if task.get("auto-reset") is not True:
            continue

        task.pop("status", None)
        task.pop("failure_reason", None)
        reset_count += 1

    config.path.write_text(tomlkit.dumps(config.document))
    return reset_count


def print_tasks(config: BucleConfig) -> None:
    table = Table(title=f"Tasks for {config.document['metadata']['name']}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Task", style="bold")
    table.add_column("Agent", style="cyan")
    table.add_column("Status")

    for index, task in enumerate(config.document["tasks"], start=1):
        status_text, status_style = format_task_status(task)
        table.add_row(
            str(index),
            str(task["name"]),
            str(task["agent"]),
            status_text,
            style=status_style,
        )

    console = Console()
    console.print(table)


def format_task_status(task: Any) -> tuple[str, str]:
    status = task.get("status")
    if status == "success":
        return "✅ done", "green"
    if status == "failure":
        reason = task.get("failure_reason")
        detail = f": {reason}" if reason else ""
        return f"❌ not done{detail}", "red"
    if status == "uncompleted":
        return "⚠️ not done", "yellow"
    return "⏳ not done", "yellow"


def get_pending_tasks(config: BucleConfig) -> list[RunTask]:
    pending_tasks: list[RunTask] = []
    for index, task in enumerate(config.document["tasks"]):
        if task.get("status") in VALID_STATUSES:
            continue
        pending_tasks.append(
            RunTask(
                name=str(task["name"]),
                agent=str(task["agent"]),
                prompt=str(task["prompt"]),
                index=index,
            )
        )
    return pending_tasks


def render_command(config: BucleConfig, task: RunTask) -> str:
    agent_config = config.document["agents"][task.agent]
    prompt = render_prompt(config, task)
    return str(agent_config["cmd"]).replace(PROMPT_PLACEHOLDER, shlex.quote(prompt))


def render_prompt(config: BucleConfig, task: RunTask) -> str:
    metadata = config.document["metadata"]
    return "\n\n".join(
        [
            str(metadata["preprompt"]),
            f"Project: {metadata['name']}",
            f"Task name: {task.name}",
            f"Task index: {task.index}",
            f"Task prompt:\n{task.prompt}",
            str(metadata["postprompt"]),
            completion_contract(config, task),
        ]
    )


def completion_contract(config: BucleConfig, task: RunTask) -> str:
    return (
        "Bucle completion contract:\n"
        f"- Do not edit {config.path.name}.\n"
        f"- When task '{task.name}' succeeds, update {BUCLE_DIR}/{SUCCESS_MARKER} "
        f"as a JSON array containing {{\"name\": \"{task.name}\"}}.\n"
        f"- When task '{task.name}' fails, update {BUCLE_DIR}/{FAILURE_MARKER} "
        "as a JSON array containing "
        f"{{\"name\": \"{task.name}\", \"reason\": \"<reason>\"}}.\n"
        "- Update exactly one marker file for this task.\n"
        "- Preserve valid JSON arrays when writing marker files.\n"
        "- Valid final TOML statuses are: success, failure, uncompleted."
    )


def write_task_log(
    log_path: Path,
    task: RunTask,
    command: str,
    started_at: datetime,
    ended_at: datetime,
    result: subprocess.CompletedProcess[str],
) -> None:
    timestamp = format_utc_timestamp(started_at)
    log_path.write_text(
        "\n".join(
            [
                f"task_name: {task.name}",
                f"agent: {task.agent}",
                f"started_at: {timestamp}",
                f"ended_at: {ended_at.isoformat(timespec='seconds').replace('+00:00', 'Z')}",
                f"exit_code: {result.returncode}",
                f"command: {command}",
                "",
                "stdout:",
                result.stdout,
                "",
                "stderr:",
                result.stderr,
            ]
        )
    )


def run_task_command(
    config: BucleConfig, command: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S602 - commands are trusted local config.
        command,
        cwd=config.root,
        shell=True,
        text=True,
        capture_output=True,
        check=False,
    )


def task_log_path(config: BucleConfig, task: RunTask, started_at: datetime) -> Path:
    timestamp = format_utc_timestamp(started_at)
    safe_timestamp = timestamp.replace(":", "-")
    log_name = f"{safe_timestamp}_{safe_filename(task.name)}.{safe_filename(task.agent)}.log"
    return config.output_dir / log_name


def format_utc_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def read_marker_entries(path: Path, marker_name: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(data, list):
        raise ConfigError(f"{path} must contain a JSON array")

    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}[{index}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{path}[{index}].name must be a non-empty string")
        if marker_name == "failure":
            reason = entry.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ConfigError(f"{path}[{index}].reason must be a string")
        entries.append(entry)
    return entries


def write_json_array(path: Path, value: list[Any]) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def cleanup_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def require_table(mapping: Any, key: str) -> Any:
    value = mapping.get(key)
    if not is_mapping(value):
        raise ConfigError(f"{key} must be a table")
    return value


def require_string(mapping: Any, key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def is_mapping(value: Any) -> bool:
    return hasattr(value, "items") and hasattr(value, "get")


def safe_filename(value: str) -> str:
    safe_chars = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            safe_chars.append(char)
        else:
            safe_chars.append("_")
    return "".join(safe_chars).strip("._") or "task"
