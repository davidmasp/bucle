from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import tomlkit
from typer.testing import CliRunner

from bucle.cli import (
    ConfigError,
    app,
    format_task_status,
    load_config,
    reconcile_results,
    render_prompt,
    reset_task,
    run_pending_tasks,
)

runner = CliRunner()


VALID_CONFIG = """
[metadata]
name = "example"
preprompt = "Before"
postprompt = "After"

[agents.fake]
cmd = "{cmd}"

[[tasks]]
name = "task1"
agent = "fake"
prompt = "Do task one"
"""


class ConfigValidationTest(unittest.TestCase):
    def test_valid_config_passes(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            self.assertEqual(load_config(config_path).path, config_path.resolve())

    def test_missing_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ConfigError, "does not exist"):
                load_config(Path(temp_dir) / ".bucle.toml")

    def test_duplicate_task_names_fail(self) -> None:
        config = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + '\n[[tasks]]\nname = "task1"\nagent = "fake"\nprompt = "Again"\n'
        )
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "duplicate task name"):
                load_config(config_path)

    def test_missing_task_field_fails(self) -> None:
        config = """
        [metadata]
        name = "example"
        preprompt = "Before"
        postprompt = "After"

        [agents.fake]
        cmd = "echo {{prompt}}"

        [[tasks]]
        name = "task1"
        agent = "fake"
        """
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "prompt"):
                load_config(config_path)

    def test_unknown_agent_fails(self) -> None:
        config = VALID_CONFIG.format(cmd="echo {{prompt}}").replace(
            'agent = "fake"', 'agent = "missing"'
        )
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "unknown agent"):
                load_config(config_path)

    def test_missing_prompt_placeholder_fails(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo nope")) as config_path:
            with self.assertRaisesRegex(ConfigError, "must contain"):
                load_config(config_path)

    def test_invalid_status_fails(self) -> None:
        config = VALID_CONFIG.format(cmd="echo {{prompt}}") + '\nstatus = "done"\n'
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "status"):
                load_config(config_path)


class PromptRenderingTest(unittest.TestCase):
    def test_prompt_contains_expected_contract(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            task = run_task(config, "task1")
            prompt = render_prompt(config, task)

        self.assertIn("Before", prompt)
        self.assertIn("After", prompt)
        self.assertIn("Task name: task1", prompt)
        self.assertIn("Task prompt:\nDo task one", prompt)
        self.assertIn(".bucle/success.json", prompt)
        self.assertIn(".bucle/failure.json", prompt)
        self.assertIn("success, failure, uncompleted", prompt)


class RunReconciliationTest(unittest.TestCase):
    def test_success_marker_updates_toml_and_removes_markers(self) -> None:
        with temp_config(config_for_fake_agent("success")) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(document["tasks"][0]["status"], "success")
            self.assertNotIn("failure_reason", document["tasks"][0])
            self.assertFalse((config_path.parent / ".bucle" / "success.json").exists())
            self.assertFalse((config_path.parent / ".bucle" / "failure.json").exists())
            self.assert_log_contains(config_path.parent, "exit_code:", "stdout:", "stderr:")

    def test_failure_marker_updates_toml_with_reason(self) -> None:
        with temp_config(config_for_fake_agent("failure")) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(document["tasks"][0]["status"], "failure")
            self.assertEqual(document["tasks"][0]["failure_reason"], "bad result")

    def test_missing_marker_sets_uncompleted(self) -> None:
        with temp_config(config_for_fake_agent("none")) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(document["tasks"][0]["status"], "uncompleted")

    def test_completed_tasks_are_skipped(self) -> None:
        config_text = config_for_fake_agent("success") + '\nstatus = "success"\n'
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            self.assertEqual(ran_tasks, [])
            self.assertEqual(list((config_path.parent / ".bucle").glob("*.log")), [])

    def assert_log_contains(self, root: Path, *needles: str) -> None:
        logs = list((root / ".bucle").glob("*.log"))
        self.assertEqual(len(logs), 1)
        log_text = logs[0].read_text()
        for needle in needles:
            self.assertIn(needle, log_text)


class ResetTaskTest(unittest.TestCase):
    def test_reset_task_removes_status_and_failure_reason(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + '\nstatus = "failure"\nfailure_reason = "bad result"\n'
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            reset_task(config, "task1")

            document = tomlkit.parse(config_path.read_text())
            self.assertNotIn("status", document["tasks"][0])
            self.assertNotIn("failure_reason", document["tasks"][0])

    def test_reset_missing_task_fails(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)

            with self.assertRaisesRegex(ConfigError, "task not found: missing"):
                reset_task(config, "missing")


class TaskListTest(unittest.TestCase):
    def test_format_task_status_marks_success_done(self) -> None:
        self.assertEqual(format_task_status({"status": "success"}), ("✅ done", "green"))

    def test_format_task_status_marks_missing_status_not_done(self) -> None:
        self.assertEqual(format_task_status({}), ("⏳ not done", "yellow"))

    def test_tasks_command_lists_statuses(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + """
            status = "success"

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"

            [[tasks]]
            name = "task3"
            agent = "fake"
            prompt = "Do task three"
            status = "failure"
            failure_reason = "bad result"
            """
        )
        with temp_config(config_text) as config_path:
            result = runner.invoke(app, ["tasks", "--config", str(config_path)])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Tasks for example", result.output)
        self.assertIn("task1", result.output)
        self.assertIn("✅ done", result.output)
        self.assertIn("task2", result.output)
        self.assertIn("⏳ not done", result.output)
        self.assertIn("task3", result.output)
        self.assertIn("❌ not done: bad result", result.output)
        self.assertNotIn("Prompt", result.output)
        self.assertNotIn("Do task one", result.output)
        self.assertNotIn("Do task two", result.output)
        self.assertNotIn("Do task three", result.output)

    def test_list_command_aliases_tasks(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            tasks_result = runner.invoke(app, ["tasks", "--config", str(config_path)])
            list_result = runner.invoke(app, ["list", "--config", str(config_path)])

        self.assertEqual(tasks_result.exit_code, 0)
        self.assertEqual(list_result.exit_code, 0)
        self.assertEqual(list_result.output, tasks_result.output)


def config_for_fake_agent(mode: str) -> str:
    script = Path(__file__).parent / "support" / "fake_agent.py"
    cmd = f"{shlex_quote(sys.executable)} {shlex_quote(str(script))} {mode} {{{{prompt}}}}"
    return VALID_CONFIG.format(cmd=cmd)


def run_task(config, name: str):
    for index, task in enumerate(config.document["tasks"]):
        if task["name"] == name:
            from bucle.cli import RunTask

            return RunTask(
                name=str(task["name"]),
                agent=str(task["agent"]),
                prompt=str(task["prompt"]),
                index=index,
            )
    raise AssertionError(f"missing task {name}")


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


class temp_config:
    def __init__(self, config_text: str) -> None:
        self.config_text = textwrap.dedent(config_text).strip() + "\n"
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / ".bucle.toml"
        self.path.write_text(self.config_text)
        return self.path

    def __exit__(self, *args) -> None:
        assert self.temp_dir is not None
        self.temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
