from __future__ import annotations

import curses
import json
import random
import re
import shlex
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomlkit
import typer
from rich.console import Console
from rich.table import Table

from bucle.commands.check import check
from bucle.commands.init import init
from bucle.commands.render import render
from bucle.commands.reset import reset
from bucle.commands.run import run
from bucle.commands.sync import sync
from bucle.commands.tasks import tasks
from bucle.commands.tui import tui
from bucle.helpers import (
    ConfigError,
    cleanup_marker,
    display_log_timestamp,
    format_task_status,
    format_utc_timestamp,
    is_mapping,
    load_template,
    read_marker_entries,
    render_html_template,
    require_string,
    require_table,
    safe_filename,
)

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
AGENT_LINE_PATTERN = re.compile(r"^\s*agent:\s*(?P<agent>\S+)\s*$", re.MULTILINE)
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
BUCLE_JUSTFILE_RECIPES = """# runs bucle tasks
bucle:
    bucle run --reverse -v

# lists bucle tasks
bucle-list:
    bucle list
"""

app = typer.Typer(help="Run agent tasks from a .bucle.toml file.")

app.command()(init)
app.command()(check)
app.command()(run)
app.command()(reset)
app.command()(sync)
app.command()(tasks)
app.command("list")(tasks)
app.command()(tui)
app.command()(render)


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


@dataclass(frozen=True)
class TaskListRow:
    index: int
    name: str
    agent: str
    prompt: str
    status_text: str
    status_style: str


@dataclass(frozen=True)
class SyncResult:
    added: int
    skipped: int
    messages: list[str]


def main() -> None:
    app()


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
    append_justfile_recipes(root / "Justfile")
    config_path.write_text(DEFAULT_INIT_CONFIG)


def append_gitignore_entry(path: Path, entry: str) -> None:
    text = path.read_text()
    ignored_entries = {line.strip() for line in text.splitlines()}
    if entry in ignored_entries or entry.rstrip("/") in ignored_entries:
        return

    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{entry}\n")


def append_justfile_recipes(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise ConfigError("Justfile is not a file")

    text = path.read_text()
    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{BUCLE_JUSTFILE_RECIPES}")


def sync_github_issues(config: BucleConfig, author: str, tag: str) -> SyncResult:
    author = author.strip()
    tag = tag.strip()
    if not author:
        raise ConfigError("author must be a non-empty string")
    if not tag:
        raise ConfigError("tag must be a non-empty string")

    added = 0
    skipped = 0
    messages: list[str] = []
    issues = list_github_issues(config, author=author, tag=tag)
    for issue in issues:
        if not is_open_issue(issue):
            continue

        number = issue_number(issue)
        details = view_github_issue(config, number)
        title = issue_title(details, fallback=issue.get("title"))
        body = str(details.get("body") or "")
        issue_label = f"issue #{number} {title}"

        agent = extract_issue_agent(body)
        if agent is None:
            skipped += 1
            messages.append(f"Skipped {issue_label}: missing agent line.")
            continue
        if task_name_exists(config, title):
            skipped += 1
            messages.append(f"Skipped {issue_label}: task already exists.")
            continue
        if agent not in config.document["agents"]:
            skipped += 1
            messages.append(f"Skipped {issue_label}: unknown agent {agent}.")
            continue

        append_task(config, name=title, agent=agent, prompt=body)
        added += 1
        messages.append(f"Added {issue_label}.")

    if added:
        config.path.write_text(tomlkit.dumps(config.document))

    return SyncResult(added=added, skipped=skipped, messages=messages)


def list_github_issues(config: BucleConfig, author: str, tag: str) -> list[Any]:
    search = f'author:{author} label:"{tag}"'
    value = run_gh_json(
        config.root,
        [
            "issue",
            "list",
            "--search",
            search,
            "--json",
            "id,author,title,state,labels,number",
        ],
    )
    if not isinstance(value, list):
        raise ConfigError("gh issue list returned invalid JSON")
    return value


def view_github_issue(config: BucleConfig, number: int) -> dict[str, Any]:
    value = run_gh_json(
        config.root,
        ["issue", "view", str(number), "--json", "body,title"],
    )
    if not isinstance(value, dict):
        raise ConfigError(f"gh issue view {number} returned invalid JSON")
    return value


def run_gh_json(cwd: Path, args: list[str]) -> Any:
    command = ["gh", *args]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ConfigError("gh command not found; install GitHub CLI") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        message = f"gh command failed: {shlex.join(command)}"
        if detail:
            message = f"{message}: {detail}"
        raise ConfigError(message)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ConfigError("gh returned invalid JSON") from error


def is_open_issue(issue: Any) -> bool:
    if not is_mapping(issue):
        raise ConfigError("gh issue list returned an invalid issue")
    state = issue.get("state")
    return state is None or str(state).lower() == "open"


def issue_number(issue: Any) -> int:
    if not is_mapping(issue):
        raise ConfigError("gh issue list returned an invalid issue")
    number = issue.get("number")
    if not isinstance(number, int):
        raise ConfigError("gh issue list returned an issue without a numeric number")
    return number


def issue_title(issue: dict[str, Any], fallback: Any = None) -> str:
    title = issue.get("title") or fallback
    if not isinstance(title, str) or not title.strip():
        raise ConfigError("gh issue returned an issue without a title")
    return title.strip()


def extract_issue_agent(body: str) -> str | None:
    match = AGENT_LINE_PATTERN.search(body)
    if match is None:
        return None
    return match.group("agent")


def task_name_exists(config: BucleConfig, name: str) -> bool:
    return any(str(task["name"]) == name for task in config.document["tasks"])


def append_task(config: BucleConfig, name: str, agent: str, prompt: str) -> None:
    task = tomlkit.table()
    task.add("name", name)
    task.add("agent", agent)
    task.add("prompt", prompt)
    config.document["tasks"].append(task)


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
    table = Table(title=f"Tasks for {config.document['metadata']['name']}")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Task", style="bold")
    table.add_column("Agent", style="cyan")
    table.add_column("Status")

    for row in build_task_rows(config, limit=limit):
        table.add_row(
            str(row.index),
            row.name,
            row.agent,
            row.status_text,
            style=row.status_style,
        )

    console = Console()
    console.print(table)


def build_task_rows(config: BucleConfig, limit: int | None = None) -> list[TaskListRow]:
    validate_limit(limit)
    tasks = config.document["tasks"]
    if limit is not None:
        tasks = tasks[:limit]

    rows = []
    for index, task in enumerate(tasks, start=1):
        status_text, status_style = format_task_status(task)
        rows.append(
            TaskListRow(
                index=index,
                name=str(task["name"]),
                agent=str(task["agent"]),
                prompt=str(task["prompt"]),
                status_text=status_text,
                status_style=status_style,
            )
        )
    return rows


def launch_tui(config: BucleConfig, limit: int | None = None) -> None:
    curses.wrapper(run_tui, config.path, limit)


def run_tui(screen: Any, config_path: Path, limit: int | None = None) -> None:
    curses.curs_set(0)
    screen.keypad(True)
    selection = 0
    message = "Use ↑/↓ or j/k to move, p to inspect prompt, r to reset, q to quit."

    while True:
        config = load_config(config_path)
        rows = build_task_rows(config, limit=limit)
        if rows:
            selection = max(0, min(selection, len(rows) - 1))
        else:
            selection = 0

        draw_tui(screen, config, rows, selection, message)
        key = screen.getch()

        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_UP, ord("k")) and selection > 0:
            selection -= 1
            continue
        if key in (curses.KEY_DOWN, ord("j")) and selection < len(rows) - 1:
            selection += 1
            continue
        if key == ord("p"):
            if not rows:
                message = "No task selected."
                continue
            show_task_prompt(screen, rows[selection])
            continue
        if key == ord("r"):
            if not rows:
                message = "No task selected."
                continue
            reset_task(config, rows[selection].name)
            message = f"Reset task: {rows[selection].name}"


def draw_tui(
    screen: Any,
    config: BucleConfig,
    rows: list[TaskListRow],
    selection: int,
    message: str,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    title = f"bucle tui - {config.document['metadata']['name']}"
    screen.addnstr(0, 0, title, width - 1, curses.A_BOLD)
    screen.addnstr(1, 0, "#  Task                       Agent        Status", width - 1)

    available_rows = max(0, height - 4)
    start = 0
    if available_rows and selection >= available_rows:
        start = selection - available_rows + 1

    visible_rows = rows[start : start + available_rows] if available_rows else []
    for offset, row in enumerate(visible_rows, start=2):
        line = f"{row.index:>2}  {row.name:<26.26} {row.agent:<12.12} {row.status_text}"
        attrs = curses.A_REVERSE if row.index - 1 == selection else curses.A_NORMAL
        screen.addnstr(offset, 0, line, width - 1, attrs)

    footer_row = max(2, height - 1)
    screen.addnstr(footer_row, 0, message, width - 1)
    screen.refresh()


def show_task_prompt(screen: Any, row: TaskListRow) -> None:
    scroll = 0
    while True:
        max_scroll = draw_prompt_window(screen, row, scroll)
        scroll = min(scroll, max_scroll)
        key = screen.getch()

        if key in (ord("q"), ord("p"), 27):
            return
        if key in (curses.KEY_UP, ord("k")) and scroll > 0:
            scroll -= 1
            continue
        if key in (curses.KEY_DOWN, ord("j")) and scroll < max_scroll:
            scroll += 1


def draw_prompt_window(screen: Any, row: TaskListRow, scroll: int) -> int:
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 8 or width < 20:
        screen.addnstr(
            0,
            0,
            "Terminal too small for prompt view. Press q to close.",
            max(0, width - 1),
        )
        screen.refresh()
        return 0

    panel_height = min(height - 2, max(8, height * 3 // 4))
    panel_width = min(width - 4, max(40, width * 4 // 5))
    panel_y = max(0, (height - panel_height) // 2)
    panel_x = max(0, (width - panel_width) // 2)
    inner_width = max(1, panel_width - 2)
    content_width = max(1, panel_width - 4)
    content_height = max(1, panel_height - 6)

    prompt_lines = wrap_prompt_lines(row.prompt, content_width)
    max_scroll = max(0, len(prompt_lines) - content_height)
    scroll = max(0, min(scroll, max_scroll))
    visible_lines = prompt_lines[scroll : scroll + content_height]

    top_border = "+" + "-" * inner_width + "+"
    separator = "+" + "-" * inner_width + "+"
    available_width = max(0, width - panel_x - 1)
    screen.addnstr(panel_y, panel_x, top_border, available_width)
    screen.addnstr(
        panel_y + 1,
        panel_x,
        panel_line(f" Prompt: {row.name}", inner_width),
        available_width,
    )
    screen.addnstr(
        panel_y + 2,
        panel_x,
        panel_line(f" Agent: {row.agent} | Status: {row.status_text}", inner_width),
        available_width,
    )
    screen.addnstr(panel_y + 3, panel_x, separator, available_width)

    for offset in range(content_height):
        text = visible_lines[offset] if offset < len(visible_lines) else ""
        screen.addnstr(
            panel_y + 4 + offset,
            panel_x,
            panel_line(f" {text}", inner_width),
            available_width,
        )

    shown_until = min(len(prompt_lines), scroll + content_height)
    footer = f" {scroll + 1}-{shown_until}/{len(prompt_lines)}  j/k scroll  p/q close"
    screen.addnstr(
        panel_y + panel_height - 2,
        panel_x,
        panel_line(footer, inner_width),
        available_width,
    )
    screen.addnstr(panel_y + panel_height - 1, panel_x, top_border, available_width)
    screen.refresh()
    return max_scroll


def wrap_prompt_lines(prompt: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw_line in prompt.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                raw_line,
                width=max(1, width),
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return lines


def panel_line(text: str, width: int) -> str:
    clipped = truncate_text(text, width)
    return f"|{clipped.ljust(width)}|"


def truncate_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3]}..."


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


def main_report_template() -> str:
    return load_template("main_report.html.jinja2")


def log_template() -> str:
    return load_template("log.html.jinja2")


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
        f'echo "{task.name}" >> {BUCLE_DIR}/{SUCCESS_MARKER}.\n'
        f"- When task '{task.name}' fails, run: "
        f'echo "{task.name},<reason>" >> {BUCLE_DIR}/{FAILURE_MARKER}.\n'
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
    log_name = (
        f"{safe_timestamp}_{safe_filename(task.name)}.{safe_filename(task.agent)}.log"
    )
    return config.output_dir / log_name
