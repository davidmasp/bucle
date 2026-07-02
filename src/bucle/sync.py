from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit

from bucle.helpers import ConfigError, is_mapping

if TYPE_CHECKING:
    from bucle.cli import BucleConfig

AGENT_LINE_PATTERN = re.compile(r"^[ \t]*agent:[ \t]*(?P<agent>\S+)[ \t]*\r?$", re.MULTILINE)
CWD_LINE_PATTERN = re.compile(
    r"^[ \t]*cwd:[ \t]*(?P<cwd>\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)[ \t]*\r?$",
    re.MULTILINE,
)
ISSUE_YAML_BLOCK_PATTERN = re.compile(
    r"\A(?:[ \t]*#{1,6}[^\r\n]*\r?\n+)?"
    r"[ \t]*```(?:ya?ml)[ \t]*\r?\n(?P<yaml>.*?)(?:\r?\n)```[ \t]*(?:\r?\n)?",
    re.DOTALL | re.IGNORECASE,
)
ISSUE_YAML_LINE_PATTERN = re.compile(
    r"^[ \t]*(?P<key>[^:#\r\n][^:\r\n]*):[ \t]*(?P<value>.*?)[ \t]*$"
)


@dataclass(frozen=True)
class SyncResult:
    added: int
    skipped: int
    messages: list[str]


def sync_github_issues(
    config: BucleConfig, author: str, tag: str, reverse: bool = False
) -> SyncResult:
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

        cwd = extract_issue_cwd(body)
        prompt = remove_issue_agent_line(body)
        insert_index = added if reverse else None
        append_task(
            config,
            name=title,
            agent=agent,
            prompt=prompt,
            cwd=cwd,
            index=insert_index,
        )
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
    metadata = extract_issue_metadata(body)
    if metadata is not None:
        return metadata.get("agent")

    match = AGENT_LINE_PATTERN.search(body)
    if match is None:
        return None
    return match.group("agent")


def extract_issue_cwd(body: str) -> str | None:
    metadata = extract_issue_metadata(body)
    if metadata is not None:
        return metadata.get("cwd") or None

    match = CWD_LINE_PATTERN.search(body)
    if match is None:
        return None
    return parse_issue_yaml_scalar(match.group("cwd")) or None


def extract_issue_metadata(body: str) -> dict[str, str] | None:
    match = ISSUE_YAML_BLOCK_PATTERN.match(body)
    if match is None:
        return None

    metadata: dict[str, str] = {}
    for line in match.group("yaml").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        line_match = ISSUE_YAML_LINE_PATTERN.match(line)
        if line_match is None:
            continue

        key = normalize_issue_metadata_key(line_match.group("key"))
        if key is None:
            continue

        value = parse_issue_yaml_scalar(line_match.group("value"))
        metadata[key] = value

    return metadata


def normalize_issue_metadata_key(key: str) -> str | None:
    normalized = key.strip().strip("<>").lower().replace("-", "_")
    if normalized in {"agent", "var_for_agent"}:
        return "agent"
    if normalized in {"cwd", "dir", "var_for_dir"}:
        return "cwd"
    return None


def parse_issue_yaml_scalar(value: str) -> str:
    value = value.strip()
    if value in {r'\"\"', r"\'\'"}:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.replace(r'\"', '"').replace(r"\'", "'").strip()


def remove_issue_agent_line(body: str) -> str:
    match = ISSUE_YAML_BLOCK_PATTERN.match(body)
    if match is not None:
        return body[match.end() :].lstrip("\r\n")

    prompt = body
    for pattern in (AGENT_LINE_PATTERN, CWD_LINE_PATTERN):
        prompt = remove_issue_line(prompt, pattern)
    return prompt


def remove_issue_line(body: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(body)
    if match is None:
        return body

    start, end = match.span()
    if end < len(body) and body[end : end + 2] == "\r\n":
        end += 2
    elif end < len(body) and body[end] in "\r\n":
        end += 1
    elif start > 0 and body[start - 1] == "\n":
        start -= 1
        if start > 0 and body[start - 1] == "\r":
            start -= 1

    prompt = body[:start] + body[end:]
    if start == 0:
        prompt = prompt.lstrip("\r\n")
    return prompt


def task_name_exists(config: BucleConfig, name: str) -> bool:
    return any(str(task["name"]) == name for task in config.document["tasks"])


def append_task(
    config: BucleConfig,
    name: str,
    agent: str,
    prompt: str,
    cwd: str | None = None,
    index: int | None = None,
) -> None:
    task = tomlkit.table()
    task.add("name", name)
    task.add("agent", agent)
    if cwd:
        task.add("cwd", cwd)
    toml_prompt = prompt
    if not toml_prompt.endswith(("\r", "\n")):
        toml_prompt = f"{toml_prompt}\n"
    task.add("prompt", tomlkit.string(f"\n{toml_prompt}", multiline=True))
    if index is None:
        config.document["tasks"].append(task)
    else:
        config.document["tasks"].insert(index, task)
