from __future__ import annotations

import random
import re
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
SUCCESS_MARKER = "success.txt"
FAILURE_MARKER = "failure.txt"
HTML_REPORT = "index.html"
LOG_FILENAME_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)_"
    r"(?P<task>.+)\.(?P<agent>[^.]+)\.log$"
)
DEFAULT_INIT_CONFIG = """[metadata]
name = "my-project"
preprompt = "You are a helpful assistant."
postprompt = " "

[agents.codex]
cmd = "codex exec {{prompt}}"

[[tasks]]
name = "task1"
agent = "codex"
prompt = "Make a small, safe improvement and report success."

[[tasks]]
name = "task2"
agent = "codex"
prompt = "Add or update a focused test for the previous change."
auto-reset = true
"""

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


@dataclass(frozen=True)
class BucleLog:
    path: Path
    html_path: Path
    filename: str
    html_filename: str
    absolute_path: str
    task_name: str
    agent: str
    timestamp: str
    display_date: str
    contents: str


def main() -> None:
    app()


@app.command()
def init() -> None:
    """Create the default bucle files in the current directory."""
    try:
        init_project(Path.cwd())
    except ConfigError as error:
        typer.echo(f"Init failed: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo("Initialized bucle project.")


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
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of tasks to list.",
    ),
) -> None:
    """List configured tasks and their current status."""
    try:
        bucle_config = load_config(config)
    except ConfigError as error:
        typer.echo(f"Invalid config: {error}", err=True)
        raise typer.Exit(1) from error

    print_tasks(bucle_config, limit=limit)


@app.command()
def render(
    config: Path = typer.Option(
        Path(".bucle.toml"),
        "--config",
        "-c",
        help="Path to the .bucle.toml file.",
    ),
) -> None:
    """Render task and log HTML files into the .bucle directory."""
    try:
        bucle_config = load_config(config)
        report_path = render_site(bucle_config)
    except ConfigError as error:
        typer.echo(f"Render failed: {error}", err=True)
        raise typer.Exit(1) from error

    typer.echo(f"Rendered bucle report: {report_path}")


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


def init_project(root: Path) -> None:
    gitignore_path = root / ".gitignore"
    if not gitignore_path.exists():
        raise ConfigError(".gitignore does not exist")
    if not gitignore_path.is_file():
        raise ConfigError(".gitignore is not a file")

    config_path = root / ".bucle.toml"
    if config_path.exists():
        raise ConfigError(".bucle.toml already exists")

    bucle_dir = root / BUCLE_DIR
    bucle_dir.mkdir(exist_ok=True)
    append_gitignore_entry(gitignore_path, f"{BUCLE_DIR}/")
    config_path.write_text(DEFAULT_INIT_CONFIG)


def append_gitignore_entry(path: Path, entry: str) -> None:
    text = path.read_text()
    ignored_entries = {line.strip() for line in text.splitlines()}
    if entry in ignored_entries or entry.rstrip("/") in ignored_entries:
        return

    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{entry}\n")


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
    config: BucleConfig,
    reverse: bool = False,
    shuffle: bool = False,
    verbose: bool = False,
    limit: int | None = None,
) -> list[RunTask]:
    validate_limit(limit)
    config.output_dir.mkdir(exist_ok=True)
    config.success_marker.write_text("")
    config.failure_marker.write_text("")

    pending_tasks = get_pending_tasks(config)
    if reverse:
        pending_tasks.reverse()
    if shuffle:
        random.shuffle(pending_tasks)
    if limit is not None:
        pending_tasks = pending_tasks[:limit]
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


def print_tasks(config: BucleConfig, limit: int | None = None) -> None:
    validate_limit(limit)
    table = Table(title=f"Tasks for {config.document['metadata']['name']}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Task", style="bold")
    table.add_column("Agent", style="cyan")
    table.add_column("Status")

    tasks = config.document["tasks"]
    if limit is not None:
        tasks = tasks[:limit]
    for index, task in enumerate(tasks, start=1):
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


def render_site(config: BucleConfig) -> Path:
    config.output_dir.mkdir(exist_ok=True)
    logs = collect_log_files(config)
    generated_at = format_utc_timestamp(datetime.now(timezone.utc))

    project_name = str(config.document["metadata"]["name"])
    for log in logs:
        log.html_path.write_text(
            render_html_template(
                log_template(),
                {
                    "project_name": project_name,
                    "index_href": HTML_REPORT,
                    "log": log,
                },
            ),
            encoding="utf-8",
        )

    tasks, unmatched_logs = build_render_tasks(config, logs)
    report_path = config.output_dir / HTML_REPORT
    report_path.write_text(
        render_html_template(
            main_report_template(),
            {
                "project_name": project_name,
                "config_path": str(config.path),
                "generated_at": generated_at,
                "tasks": tasks,
                "task_count": len(tasks),
                "log_count": len(logs),
                "unmatched_logs": unmatched_logs,
            },
        ),
        encoding="utf-8",
    )
    return report_path


def collect_log_files(config: BucleConfig) -> list[BucleLog]:
    if not config.output_dir.exists():
        return []

    logs = []
    for path in sorted(config.output_dir.glob("*.log"), reverse=True):
        metadata = parse_log_filename(path)
        logs.append(
            BucleLog(
                path=path,
                html_path=path.with_suffix(".html"),
                filename=path.name,
                html_filename=path.with_suffix(".html").name,
                absolute_path=str(path.resolve()),
                task_name=metadata["task_name"],
                agent=metadata["agent"],
                timestamp=metadata["timestamp"],
                display_date=metadata["display_date"],
                contents=path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return logs


def parse_log_filename(path: Path) -> dict[str, str]:
    match = LOG_FILENAME_PATTERN.match(path.name)
    if match is None:
        stem = path.name.removesuffix(".log")
        return {
            "task_name": stem,
            "agent": "unknown",
            "timestamp": "unknown",
            "display_date": "Unknown date",
        }

    timestamp = match.group("timestamp")
    return {
        "task_name": match.group("task"),
        "agent": match.group("agent"),
        "timestamp": timestamp,
        "display_date": display_log_timestamp(timestamp),
    }


def display_log_timestamp(timestamp: str) -> str:
    raw_timestamp = timestamp.removesuffix("Z")
    date, separator, time = raw_timestamp.partition("T")
    if not separator:
        return timestamp
    return f"{date} {time.replace('-', ':')} UTC"


def build_render_tasks(
    config: BucleConfig, logs: list[BucleLog]
) -> tuple[list[dict[str, Any]], list[BucleLog]]:
    logs_by_task: dict[str, list[BucleLog]] = {}
    for log in logs:
        logs_by_task.setdefault(log.task_name, []).append(log)

    matched_log_paths: set[Path] = set()
    tasks = []
    for index, task in enumerate(config.document["tasks"], start=1):
        task_name = str(task["name"])
        task_logs = unique_logs(
            logs_by_task.get(safe_filename(task_name), [])
            + logs_by_task.get(task_name, [])
        )
        matched_log_paths.update(log.path for log in task_logs)
        status = str(task.get("status") or "pending")
        tasks.append(
            {
                "index": index,
                "name": task_name,
                "agent": str(task["agent"]),
                "prompt": str(task["prompt"]),
                "status": status,
                "status_class": status_class(status),
                "failure_reason": task.get("failure_reason"),
                "auto_reset": task.get("auto-reset") is True,
                "logs": task_logs,
            }
        )

    unmatched_logs = [log for log in logs if log.path not in matched_log_paths]
    return tasks, unmatched_logs


def unique_logs(logs: list[BucleLog]) -> list[BucleLog]:
    seen_paths: set[Path] = set()
    unique = []
    for log in logs:
        if log.path in seen_paths:
            continue
        seen_paths.add(log.path)
        unique.append(log)
    return unique


def status_class(status: str) -> str:
    if status in VALID_STATUSES:
        return status
    return "pending"


def render_html_template(template_source: str, context: dict[str, Any]) -> str:
    try:
        from jinja2 import Environment, select_autoescape
    except ImportError as error:
        raise ConfigError(
            "render requires jinja2; install project dependencies with `uv sync`"
        ) from error

    environment = Environment(
        autoescape=select_autoescape(
            enabled_extensions=("html", "xml"),
            default_for_string=True,
        )
    )
    return environment.from_string(template_source).render(**context)


def main_report_template() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ project_name }} bucle report</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --panel: #ffffff;
      --text: #151a23;
      --muted: #667085;
      --border: #d9dee8;
      --accent: #1955d6;
      --success: #18794e;
      --failure: #ba1a1a;
      --uncompleted: #9a6700;
      --pending: #475467;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }
    header {
      background: #111827;
      color: white;
      padding: 28px clamp(18px, 5vw, 56px);
    }
    header h1 {
      margin: 0 0 6px;
      font-size: clamp(1.8rem, 3vw, 2.8rem);
      font-weight: 760;
    }
    header p {
      margin: 0;
      color: #cbd5e1;
      overflow-wrap: anywhere;
    }
    main {
      width: min(1120px, calc(100% - 32px));
      margin: 24px auto 48px;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .summary-item, .task-card, .unmatched {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
    }
    .summary-item {
      padding: 14px 16px;
    }
    .summary-item strong {
      display: block;
      font-size: 1.35rem;
    }
    .summary-item span {
      color: var(--muted);
      font-size: 0.88rem;
    }
    .task-list {
      display: grid;
      gap: 14px;
    }
    .task-card {
      padding: 18px;
    }
    .task-header {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
      margin-bottom: 12px;
    }
    .task-title {
      min-width: 0;
    }
    .task-title h2 {
      margin: 0 0 4px;
      font-size: 1.2rem;
      overflow-wrap: anywhere;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 0.9rem;
    }
    .badge {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 0.82rem;
      font-weight: 700;
      white-space: nowrap;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }
    .status-success { color: var(--success); border-color: #b7e4ce; background: #edfdf4; }
    .status-failure { color: var(--failure); border-color: #f3b8b8; background: #fff1f1; }
    .status-uncompleted { color: var(--uncompleted); border-color: #f7d98d; background: #fff8e5; }
    .status-pending { color: var(--pending); border-color: #d0d5dd; background: #f8fafc; }
    .prompt {
      margin: 12px 0 14px;
      padding: 14px;
      border-radius: 8px;
      background: #f8fafc;
      border: 1px solid #e6e9ef;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 0.9rem;
    }
    .failure {
      margin: 0 0 14px;
      color: var(--failure);
      font-weight: 650;
    }
    details {
      border-top: 1px solid var(--border);
      padding-top: 12px;
    }
    summary {
      cursor: pointer;
      font-weight: 720;
      color: #202939;
    }
    .log-list {
      list-style: none;
      padding: 0;
      margin: 10px 0 0;
      display: grid;
      gap: 8px;
    }
    .log-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-top: 1px solid #edf0f5;
    }
    .log-meta {
      min-width: 0;
    }
    .log-meta strong {
      display: block;
      overflow-wrap: anywhere;
    }
    .log-meta span {
      color: var(--muted);
      font-size: 0.86rem;
    }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    button, .button {
      appearance: none;
      border: 1px solid #b8c2d6;
      background: white;
      color: #172033;
      border-radius: 7px;
      padding: 7px 10px;
      font: inherit;
      font-size: 0.9rem;
      line-height: 1;
      text-decoration: none;
      cursor: pointer;
    }
    button:hover, .button:hover {
      border-color: var(--accent);
      color: var(--accent);
    }
    .muted {
      color: var(--muted);
      margin: 10px 0 0;
    }
    .unmatched {
      margin-top: 22px;
      padding: 18px;
    }
    .unmatched h2 {
      margin: 0 0 10px;
      font-size: 1.1rem;
    }
    @media (max-width: 680px) {
      .task-header, .log-row {
        display: grid;
      }
      .actions {
        justify-content: flex-start;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>{{ project_name }}</h1>
    <p>{{ config_path }} | generated {{ generated_at }}</p>
  </header>
  <main>
    <section class="summary" aria-label="Report summary">
      <div class="summary-item"><strong>{{ task_count }}</strong><span>tasks</span></div>
      <div class="summary-item"><strong>{{ log_count }}</strong><span>log files</span></div>
    </section>
    <section class="task-list" aria-label="Tasks">
      {% for task in tasks %}
      <article class="task-card">
        <div class="task-header">
          <div class="task-title">
            <h2>{{ task.index }}. {{ task.name }}</h2>
            <div class="meta">
              <span>Agent: {{ task.agent }}</span>
              {% if task.auto_reset %}<span>Auto-reset</span>{% endif %}
            </div>
          </div>
          <span class="badge status-{{ task.status_class }}">{{ task.status }}</span>
        </div>
        <div class="prompt">{{ task.prompt }}</div>
        {% if task.failure_reason %}
        <p class="failure">Failure reason: {{ task.failure_reason }}</p>
        {% endif %}
        <details>
          <summary>{{ task.logs|length }} log file{% if task.logs|length != 1 %}s{% endif %}</summary>
          {% if task.logs %}
          <ul class="log-list">
            {% for log in task.logs %}
            <li class="log-row">
              <div class="log-meta">
                <strong>{{ log.filename }}</strong>
                <span>{{ log.display_date }} | {{ log.agent }}</span>
              </div>
              <div class="actions">
                <button type="button" data-copy-path="{{ log.absolute_path }}">Copy path</button>
                <a class="button" href="{{ log.html_filename }}">Visualize</a>
              </div>
            </li>
            {% endfor %}
          </ul>
          {% else %}
          <p class="muted">No log files found for this task.</p>
          {% endif %}
        </details>
      </article>
      {% endfor %}
    </section>
    {% if unmatched_logs %}
    <section class="unmatched">
      <h2>Unmatched log files</h2>
      <ul class="log-list">
        {% for log in unmatched_logs %}
        <li class="log-row">
          <div class="log-meta">
            <strong>{{ log.filename }}</strong>
            <span>{{ log.display_date }} | {{ log.task_name }} | {{ log.agent }}</span>
          </div>
          <div class="actions">
            <button type="button" data-copy-path="{{ log.absolute_path }}">Copy path</button>
            <a class="button" href="{{ log.html_filename }}">Visualize</a>
          </div>
        </li>
        {% endfor %}
      </ul>
    </section>
    {% endif %}
  </main>
  <script>
    async function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const textArea = document.createElement("textarea");
      textArea.value = text;
      textArea.style.position = "fixed";
      textArea.style.left = "-9999px";
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();
      document.execCommand("copy");
      textArea.remove();
    }

    document.querySelectorAll("[data-copy-path]").forEach((button) => {
      button.addEventListener("click", async () => {
        const original = button.textContent;
        try {
          await copyText(button.dataset.copyPath);
          button.textContent = "Copied";
        } catch {
          button.textContent = "Copy failed";
        }
        window.setTimeout(() => {
          button.textContent = original;
        }, 1400);
      });
    });
  </script>
</body>
</html>
"""


def log_template() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ log.filename }}</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101318;
      --panel: #181d25;
      --text: #ecf0f5;
      --muted: #aab4c0;
      --border: #2c3441;
      --accent: #8db4ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      line-height: 1.5;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      padding: 20px clamp(16px, 4vw, 36px);
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 1.25rem;
      overflow-wrap: anywhere;
    }
    p {
      margin: 0;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    a {
      color: var(--accent);
      text-decoration: none;
      white-space: nowrap;
    }
    pre {
      margin: 0;
      padding: 24px clamp(16px, 4vw, 36px) 40px;
      white-space: pre-wrap;
      word-break: break-word;
      tab-size: 2;
    }
    @media (max-width: 680px) {
      header {
        display: grid;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{{ log.task_name }}</h1>
      <p>{{ log.display_date }} | {{ log.agent }} | {{ log.filename }}</p>
    </div>
    <a href="{{ index_href }}">Index</a>
  </header>
  <pre>{{ log.contents }}</pre>
</body>
</html>
"""


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


def validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ConfigError("limit must be greater than or equal to 0")


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
        f"- When task '{task.name}' succeeds, run: "
        f"echo \"{task.name}\" >> {BUCLE_DIR}/{SUCCESS_MARKER}.\n"
        f"- When task '{task.name}' fails, run: "
        f"echo \"{task.name},<reason>\" >> {BUCLE_DIR}/{FAILURE_MARKER}.\n"
        "- Update exactly one marker file for this task.\n"
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
    entries: list[dict[str, Any]] = []
    for index, raw_line in enumerate(path.read_text().splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if marker_name == "failure":
            name, separator, reason = line.partition(",")
            if not name:
                raise ConfigError(f"{path}:{index + 1} must start with a task name")
            entry = {"name": name}
            if separator and reason:
                entry["reason"] = reason
        else:
            entry = {"name": line}
        entries.append(entry)
    return entries


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
