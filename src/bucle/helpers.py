from __future__ import annotations

from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when a .bucle.toml file is invalid."""


def format_utc_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def display_log_timestamp(timestamp: str) -> str:
    raw_timestamp = timestamp.removesuffix("Z")
    date, separator, time = raw_timestamp.partition("T")
    if not separator:
        return timestamp
    return f"{date} {time.replace('-', ':')} UTC"


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


def load_template(name: str) -> str:
    return (
        files("bucle")
        .joinpath("templates", name)
        .read_text(encoding="utf-8")
    )


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
