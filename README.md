# bucle

Run a sequential agent loop from a `.bucle.toml` config file. **bucle** reads
pending tasks, shells out to an external AI agent (Codex, OpenCode, etc.) for
each one, and records the result back into the TOML config via a completion
contract protocol.

## Installation

```sh
uv sync
```

## Config (`.bucle.toml`)

```toml
[metadata]
name = "my-project"
preprompt = "You are a helpful assistant."
postprompt = " "

[agents.codex]
cmd = "codex exec {{prompt}}"

[[tasks]]
name = "task1"
agent = "codex"
prompt = "say hi!"
```

### `[metadata]`

| Field       | Description                                    |
|-------------|------------------------------------------------|
| `name`      | Project name (injected into every agent prompt) |
| `preprompt` | Prepended to every agent prompt                |
| `postprompt`| Appended after the task prompt                 |

### `[agents.<name>]`

| Field | Description                                            |
|-------|--------------------------------------------------------|
| `cmd` | Shell command template containing `{{prompt}}`          |

### `[[tasks]]`

| Field            | Description                                      |
|------------------|--------------------------------------------------|
| `name`           | Unique task identifier                           |
| `agent`          | References a named agent                         |
| `prompt`         | Instruction sent to the agent                    |
| `status`         | (managed by bucle) `success`, `failure`, `uncompleted` |
| `failure_reason` | (managed by bucle) Human-readable failure reason |

## Commands

```
bucle check   [--config / -c]    Validate the config file
bucle tasks   [--config / -c]    List tasks in a Rich table (alias: `bucle list`)
bucle run     [--config / -c]    Run pending tasks and reconcile results
bucle reset   <task-name> [-c]   Reset a task to pending
```

All commands accept `--config / -c` (defaults to `.bucle.toml`).

### `bucle check`

Validates the config file — checks for missing fields, duplicate task names,
unknown agent references, and ensures agent commands contain the `{{prompt}}`
placeholder.

### `bucle tasks` / `bucle list`

Prints a Rich table with task index, name, agent, and emoji status:

| Emoji | Status       |
|-------|--------------|
| ✅    | done         |
| ❌    | failed       |
| ⚠️    | uncompleted  |
| ⏳    | pending      |

### `bucle run`

1. Creates the `.bucle/` output directory.
2. Writes empty `success.json` and `failure.json` marker arrays.
3. Identifies pending tasks (no `status` or status not in `success`/`failure`/`uncompleted`).
4. For each pending task (in order):
   - Renders the full prompt (preprompt + task context + postprompt + **completion contract**).
   - Executes the agent command via `subprocess.run`.
   - Writes a log file to `.bucle/<timestamp>_<task>.<agent>.log`.
5. **Reconciles results** — reads marker files written by the agent, updates
   `status`/`failure_reason` in `.bucle.toml`, deletes marker files.

Tasks that complete successfully set `status = "success"`. Tasks whose agent
writes a failure marker get `status = "failure"` and an optional
`failure_reason`. Tasks that ran but wrote no marker get
`status = "uncompleted"`.

### `bucle reset <task-name>`

Removes `status` and `failure_reason` from the named task so it is treated as
pending on the next `bucle run`.

## Completion Contract

Every agent prompt includes a **completion contract** that instructs the agent
to signal completion by writing JSON marker files:

| Outcome  | File                            | Contents                                |
|----------|---------------------------------|-----------------------------------------|
| Success  | `.bucle/success.json`           | `[{"name": "<task-name>"}]`             |
| Failure  | `.bucle/failure.json`           | `[{"name": "<task-name>", "reason": "..."}]` |

The agent must preserve valid JSON arrays and write exactly one marker file.
Bucle reads these after execution to determine each task's outcome.

## Output Directory (`.bucle/`)

```
.bucle/
├── success.json          # (temporary) success markers during a run
├── failure.json          # (temporary) failure markers during a run
└── <timestamp>_<task>.<agent>.log   # per-task execution logs
```

Marker files are created before execution and deleted after reconciliation.
Logs persist across runs and include the command, exit code, stdout, stderr,
and timestamps.
