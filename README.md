# bucle

Run a sequential agent loop from `.bucle.toml`.

## Config

```toml
[metadata]
name = "my-project"
preprompt = "You are jack sparrow."
postprompt = " "

[agents.codex]
cmd = "codex exec {{prompt}}"

[[tasks]]
name = "task1"
agent = "codex"
prompt = "say hi!"
```

Each pending task must have `name`, `agent`, and `prompt`. Completed tasks have
`status = "success"`, `status = "failure"`, or `status = "uncompleted"`.

## Commands

```sh
bucle check --config .bucle.toml
bucle tasks --config .bucle.toml
bucle run --config .bucle.toml
bucle reset task1 --config .bucle.toml
```

`bucle tasks` lists every configured task in a Rich table with emoji status
markers: ✅ for done tasks, ❌ or ⚠️ for tasks that did not complete, and ⏳ for
pending tasks.

`bucle run` creates `.bucle/`, resets temporary success/failure marker JSON
files, runs pending tasks in order, writes per-task logs, reconciles marker
files back into `.bucle.toml`, and then deletes the marker files.

`bucle reset <task-name>` removes `status` and `failure_reason` from the named
task so it is pending again.
