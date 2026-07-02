from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit
from typer.testing import CliRunner

from bucle.cli import (
    ConfigError,
    TaskListRow,
    app,
    build_task_rows,
    collect_log_files,
    draw_prompt_window,
    extract_issue_agent,
    extract_issue_cwd,
    extract_issue_metadata,
    format_task_status,
    init_project,
    load_config,
    reconcile_results,
    remove_issue_agent_line,
    reset_auto_tasks,
    render_site,
    render_prompt,
    reset_task,
    run_pending_tasks,
    sync_github_issues,
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

    def test_invalid_auto_reset_fails(self) -> None:
        config = VALID_CONFIG.format(cmd="echo {{prompt}}") + '\nauto-reset = "yes"\n'
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "auto-reset"):
                load_config(config_path)

    def test_invalid_cwd_fails(self) -> None:
        config = VALID_CONFIG.format(cmd="echo {{prompt}}") + '\ncwd = "/tmp"\n'
        with temp_config(config) as config_path:
            with self.assertRaisesRegex(ConfigError, "cwd"):
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
        self.assertIn('echo "task1" >> .bucle/success.txt', prompt)
        self.assertIn('echo "task1,<reason>" >> .bucle/failure.txt', prompt)
        self.assertIn("success, failure, uncompleted", prompt)

    def test_prompt_uses_marker_paths_relative_to_task_cwd(self) -> None:
        config_text = VALID_CONFIG.format(cmd="echo {{prompt}}") + '\ncwd = "packages/app"\n'
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            task = run_task(config, "task1")
            prompt = render_prompt(config, task)

        self.assertIn('echo "task1" >> ../../.bucle/success.txt', prompt)
        self.assertIn('echo "task1,<reason>" >> ../../.bucle/failure.txt', prompt)


class InitProjectTest(unittest.TestCase):
    def test_init_creates_bucle_files_and_updates_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("__pycache__/\n")

            init_project(root)

            self.assertTrue((root / ".bucle").is_dir())
            self.assertIn(".bucle/", (root / ".gitignore").read_text().splitlines())

            config = load_config(root / ".bucle.toml")
            self.assertEqual(config.document["metadata"]["name"], "my-project")
            self.assertEqual(len(config.document["tasks"]), 2)
            self.assertEqual(config.document["tasks"][0]["name"], "task1")
            self.assertEqual(config.document["tasks"][1]["name"], "task2")

    def test_init_fails_without_gitignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            with self.assertRaisesRegex(ConfigError, ".gitignore does not exist"):
                init_project(root)

            self.assertFalse((root / ".bucle").exists())
            self.assertFalse((root / ".bucle.toml").exists())

    def test_init_does_not_duplicate_gitignore_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text(".bucle/\n")

            init_project(root)

            self.assertEqual((root / ".gitignore").read_text(), ".bucle/\n")

    def test_init_appends_bucle_recipes_to_existing_justfile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("")
            justfile_path = root / "Justfile"
            justfile_path.write_text("test:\n    pytest\n")

            init_project(root)

            self.assertEqual(
                justfile_path.read_text(),
                "test:\n"
                "    pytest\n"
                "# runs bucle tasks\n"
                "bucle:\n"
                "    bucle run --reverse -v\n"
                "\n"
                "# lists bucle tasks\n"
                "bucle-list:\n"
                "    bucle list\n",
            )

    def test_init_does_nothing_when_justfile_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("")

            init_project(root)

            self.assertFalse((root / "Justfile").exists())

    def test_init_cli_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("")

            with patch("bucle.cli.Path.cwd", return_value=root):
                result = runner.invoke(app, ["init"])

            self.assertEqual(result.exit_code, 0)
            self.assertIn("Initialized bucle project.", result.output)


class SyncGithubIssuesTest(unittest.TestCase):
    def test_bucle_issue_form_includes_sync_metadata_template(self) -> None:
        form_path = Path(__file__).parents[1] / ".github" / "ISSUE_TEMPLATE" / "bucle.yml"
        form = form_path.read_text()

        self.assertIn("name: bucle issue", form)
        self.assertIn("```yaml", form)
        self.assertIn('cwd: ""', form)
        self.assertIn("agent: opencode", form)

    def test_extract_issue_agent_reads_agent_line(self) -> None:
        self.assertEqual(extract_issue_agent("agent:fake\n\nDo work"), "fake")
        self.assertEqual(extract_issue_agent("agent: fake\n\nDo work"), "fake")
        self.assertIsNone(extract_issue_agent("Do work"))

    def test_extract_issue_cwd_reads_cwd_line(self) -> None:
        self.assertEqual(extract_issue_cwd('cwd:"./path/to"\n\nDo work'), "./path/to")
        self.assertEqual(extract_issue_cwd('cwd: "./path/to"\n\nDo work'), "./path/to")
        self.assertIsNone(extract_issue_cwd("Do work"))

    def test_extract_issue_metadata_reads_leading_yaml_block(self) -> None:
        body = '```yaml\n<var_for_dir>: ""\n<var_for_agent>: opencode\n```\n\nDo work'

        self.assertEqual(
            extract_issue_metadata(body),
            {"cwd": "", "agent": "opencode"},
        )
        self.assertEqual(extract_issue_agent(body), "opencode")

    def test_extract_issue_agent_prefers_yaml_block(self) -> None:
        body = "```yaml\nagent: fake\n```\n\nagent: other\n\nDo work"

        self.assertEqual(extract_issue_agent(body), "fake")

    def test_extract_issue_metadata_supports_issue_form_heading(self) -> None:
        body = '### Bucle sync metadata\n\n```yaml\ncwd: ""\nagent: fake\n```\n\n### Task prompt\n\nDo work'

        self.assertEqual(extract_issue_agent(body), "fake")
        self.assertEqual(
            remove_issue_agent_line(body),
            "### Task prompt\n\nDo work",
        )

    def test_remove_issue_agent_line_removes_parsed_agent_line(self) -> None:
        self.assertEqual(
            remove_issue_agent_line("agent:fake\n\nDo work"),
            "Do work",
        )
        self.assertEqual(
            remove_issue_agent_line("Intro\r\nagent: fake\r\nDo work"),
            "Intro\r\nDo work",
        )
        self.assertEqual(
            remove_issue_agent_line('```yaml\nagent: fake\ncwd: ""\n```\n\nDo work'),
            "Do work",
        )
        self.assertEqual(
            remove_issue_agent_line('cwd:"./path/to"\nagent:fake\n\nDo work'),
            "Do work",
        )

    def test_sync_imports_issue_as_task(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            issue_body = "agent:fake\n\nDo work from GitHub.\n\n- first\n- second"
            expected_prompt = "Do work from GitHub.\n\n- first\n- second\n"
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [
                        {
                            "id": "I_1",
                            "author": {"login": "davidmasp"},
                            "title": "list-title",
                            "state": "OPEN",
                            "labels": [{"name": "bucle"}],
                            "number": 1,
                        }
                    ],
                    {"body": issue_body, "title": "gh-task"},
                ],
            ) as run_gh_json:
                result = sync_github_issues(config, author="davidmasp", tag="bucle")

            config_text = config_path.read_text()
            document = tomlkit.parse(config_text)

        self.assertEqual(result.added, 1)
        self.assertEqual(result.skipped, 0)
        self.assertIn("Added issue #1 gh-task.", result.messages)
        self.assertEqual(len(document["tasks"]), 2)
        self.assertEqual(document["tasks"][1]["name"], "gh-task")
        self.assertEqual(document["tasks"][1]["agent"], "fake")
        self.assertEqual(document["tasks"][1]["prompt"], expected_prompt)
        self.assertIn('prompt = """\nDo work from GitHub.\n\n- first\n- second\n"""', config_text)
        self.assertNotIn("agent:fake", config_text)
        self.assertEqual(
            run_gh_json.call_args_list[0].args,
            (
                config.path.parent,
                [
                    "issue",
                    "list",
                    "--search",
                    'author:davidmasp label:"bucle"',
                    "--json",
                    "id,author,title,state,labels,number",
                ],
            ),
        )
        self.assertEqual(
            run_gh_json.call_args_list[1].args,
            (config.path.parent, ["issue", "view", "1", "--json", "body,title"]),
        )

    def test_sync_imports_issue_with_yaml_metadata_as_task(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            issue_body = '```yaml\ncwd: "./pkg"\nagent: fake\n```\n\nDo work from GitHub.'
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "gh-task", "state": "OPEN", "number": 1}],
                    {"body": issue_body, "title": "gh-task"},
                ],
            ):
                result = sync_github_issues(config, author="davidmasp", tag="bucle")

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.added, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(document["tasks"][1]["agent"], "fake")
        self.assertEqual(document["tasks"][1]["cwd"], "./pkg")
        self.assertEqual(document["tasks"][1]["prompt"], "Do work from GitHub.\n")

    def test_sync_imports_issue_with_inline_cwd_as_task(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            issue_body = 'cwd:"./pkg"\nagent:fake\n\nDo work from GitHub.'
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "gh-task", "state": "OPEN", "number": 1}],
                    {"body": issue_body, "title": "gh-task"},
                ],
            ):
                result = sync_github_issues(config, author="davidmasp", tag="bucle")

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.added, 1)
        self.assertEqual(document["tasks"][1]["cwd"], "./pkg")
        self.assertEqual(document["tasks"][1]["prompt"], "Do work from GitHub.\n")

    def test_sync_reverse_imports_issue_before_existing_tasks(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [
                        {"title": "first", "state": "OPEN", "number": 1},
                        {"title": "second", "state": "OPEN", "number": 2},
                    ],
                    {"body": "agent:fake\n\nDo first.", "title": "first"},
                    {"body": "agent:fake\n\nDo second.", "title": "second"},
                ],
            ):
                result = sync_github_issues(
                    config, author="davidmasp", tag="bucle", reverse=True
                )

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.added, 2)
        self.assertEqual(
            [task["name"] for task in document["tasks"]],
            ["first", "second", "task1"],
        )

    def test_sync_skips_issue_without_agent_line(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "gh-task", "state": "OPEN", "number": 1}],
                    {"body": "Do work from GitHub.", "title": "gh-task"},
                ],
            ):
                result = sync_github_issues(config, author="davidmasp", tag="bucle")

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.added, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn("Skipped issue #1 gh-task: missing agent line.", result.messages)
        self.assertEqual(len(document["tasks"]), 1)

    def test_sync_skips_existing_task_title(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            config = load_config(config_path)
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "task1", "state": "OPEN", "number": 1}],
                    {"body": "agent:fake\n\nDo work from GitHub.", "title": "task1"},
                ],
            ):
                result = sync_github_issues(config, author="davidmasp", tag="bucle")

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.added, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn("Skipped issue #1 task1: task already exists.", result.messages)
        self.assertEqual(len(document["tasks"]), 1)

    def test_sync_cli_reports_results(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "gh-task", "state": "OPEN", "number": 1}],
                    {"body": "agent:fake\n\nDo work from GitHub.", "title": "gh-task"},
                ],
            ):
                result = runner.invoke(
                    app,
                    [
                        "sync",
                        "--config",
                        str(config_path),
                        "--author",
                        "davidmasp",
                    ],
                )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Added issue #1 gh-task.", result.output)
        self.assertIn("Synced GitHub issues: 1 added, 0 skipped.", result.output)

    def test_sync_cli_reverse_adds_issue_before_existing_tasks(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            with patch(
                "bucle.cli.run_gh_json",
                side_effect=[
                    [{"title": "gh-task", "state": "OPEN", "number": 1}],
                    {"body": "agent:fake\n\nDo work from GitHub.", "title": "gh-task"},
                ],
            ):
                result = runner.invoke(
                    app,
                    [
                        "sync",
                        "--config",
                        str(config_path),
                        "--author",
                        "davidmasp",
                        "--reverse",
                    ],
                )

            document = tomlkit.parse(config_path.read_text())

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(document["tasks"][0]["name"], "gh-task")
        self.assertEqual(document["tasks"][1]["name"], "task1")


class RenderReportTest(unittest.TestCase):
    def test_collect_log_files_extracts_metadata_from_filename(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            bucle_dir = config_path.parent / ".bucle"
            bucle_dir.mkdir()
            log_path = bucle_dir / "2026-06-11T13-53-32Z_task1.fake.log"
            log_path.write_text("stdout:\nhello\n")

            logs = collect_log_files(load_config(config_path))

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].task_name, "task1")
        self.assertEqual(logs[0].agent, "fake")
        self.assertEqual(logs[0].display_date, "2026-06-11 13:53:32 UTC")
        self.assertEqual(logs[0].html_filename, "2026-06-11T13-53-32Z_task1.fake.html")

    @unittest.skipUnless(importlib.util.find_spec("jinja2"), "requires jinja2")
    def test_render_site_writes_index_and_log_html(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            bucle_dir = config_path.parent / ".bucle"
            bucle_dir.mkdir()
            log_path = bucle_dir / "2026-06-11T13-53-32Z_task1.fake.log"
            log_path.write_text("stdout:\nhello <world>\n")

            report_path = render_site(load_config(config_path))

            index_html = report_path.read_text()
            log_html = log_path.with_suffix(".html").read_text()

        self.assertEqual(report_path.name, "index.html")
        self.assertIn("task1", index_html)
        self.assertIn("Do task one", index_html)
        self.assertIn("Copy path", index_html)
        self.assertIn("Visualize", index_html)
        self.assertIn("2026-06-11T13-53-32Z_task1.fake.html", index_html)
        self.assertIn("hello &lt;world&gt;", log_html)


class RunReconciliationTest(unittest.TestCase):
    def test_success_marker_updates_toml_and_removes_markers(self) -> None:
        with temp_config(config_for_fake_agent("success")) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(document["tasks"][0]["status"], "success")
            self.assertNotIn("failure_reason", document["tasks"][0])
            self.assertFalse((config_path.parent / ".bucle" / "success.txt").exists())
            self.assertFalse((config_path.parent / ".bucle" / "failure.txt").exists())
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

    def test_reverse_runs_pending_tasks_from_last_to_first(self) -> None:
        config_text = (
            config_for_fake_agent("none")
            + """

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"

            [[tasks]]
            name = "task3"
            agent = "fake"
            prompt = "Do task three"
            """
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config, reverse=True)

        self.assertEqual([task.name for task in ran_tasks], ["task3", "task2", "task1"])

    def test_limit_runs_after_reverse_ordering(self) -> None:
        config_text = (
            config_for_fake_agent("none")
            + """

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"

            [[tasks]]
            name = "task3"
            agent = "fake"
            prompt = "Do task three"
            """
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config, reverse=True, limit=2)

        self.assertEqual([task.name for task in ran_tasks], ["task3", "task2"])

    def test_shuffle_runs_pending_tasks_in_random_order(self) -> None:
        config_text = (
            config_for_fake_agent("none")
            + """

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"

            [[tasks]]
            name = "task3"
            agent = "fake"
            prompt = "Do task three"
            """
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            with patch(
                "bucle.cli.random.shuffle",
                side_effect=lambda tasks: tasks.insert(0, tasks.pop()),
            ) as shuffle:
                ran_tasks = run_pending_tasks(config, shuffle=True)

        shuffle.assert_called_once()
        self.assertEqual([task.name for task in ran_tasks], ["task3", "task1", "task2"])

    def test_run_verbose_prints_launch_details(self) -> None:
        with temp_config(config_for_fake_agent("success")) as config_path:
            result = runner.invoke(
                app,
                ["run", "--config", str(config_path), "--verbose"],
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Launching task 1/1: task1", result.output)
        self.assertIn(str(config_path.parent / ".bucle"), result.output)
        self.assertIn("task1.fake.log", result.output)

    def test_run_executes_task_from_configured_cwd(self) -> None:
        config_text = """
        [metadata]
        name = "example"
        preprompt = "Before"
        postprompt = "After"

        [agents.fake]
        cmd = "pwd > ../pwd.txt; echo task1 >> ../.bucle/success.txt; true {{prompt}}"

        [[tasks]]
        name = "task1"
        agent = "fake"
        cwd = "work"
        prompt = "Do task one"
        """
        with temp_config(config_text) as config_path:
            (config_path.parent / "work").mkdir()
            config = load_config(config_path)
            ran_tasks = run_pending_tasks(config)
            reconcile_results(config, ran_tasks)

            document = tomlkit.parse(config_path.read_text())
            pwd = (config_path.parent / "pwd.txt").read_text().strip()

        self.assertEqual(document["tasks"][0]["status"], "success")
        self.assertEqual(pwd, str((config_path.parent / "work").resolve()))

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

    def test_reset_auto_tasks_removes_status_from_marked_tasks(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + """
            status = "success"
            auto-reset = true

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"
            status = "failure"
            failure_reason = "bad result"
            """
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)
            reset_count = reset_auto_tasks(config)

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(reset_count, 1)
            self.assertNotIn("status", document["tasks"][0])
            self.assertEqual(document["tasks"][1]["status"], "failure")
            self.assertEqual(document["tasks"][1]["failure_reason"], "bad result")

    def test_reset_auto_cli_does_not_require_task_name(self) -> None:
        config_text = VALID_CONFIG.format(cmd="echo {{prompt}}") + """
        status = "success"
        auto-reset = true
        """
        with temp_config(config_text) as config_path:
            result = runner.invoke(app, ["reset", "--auto", "--config", str(config_path)])

            document = tomlkit.parse(config_path.read_text())
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Reset auto-reset task(s): 1", result.output)
            self.assertNotIn("status", document["tasks"][0])


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

    def test_list_limit_caps_listed_tasks(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + """

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"

            [[tasks]]
            name = "task3"
            agent = "fake"
            prompt = "Do task three"
            """
        )
        with temp_config(config_text) as config_path:
            result = runner.invoke(app, ["list", "--config", str(config_path), "--limit", "2"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("task1", result.output)
        self.assertIn("task2", result.output)
        self.assertNotIn("task3", result.output)

    def test_build_task_rows_matches_status_formatting(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + '\nstatus = "failure"\nfailure_reason = "bad result"\n'
        )
        with temp_config(config_text) as config_path:
            config = load_config(config_path)

        rows = build_task_rows(config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].prompt, "Do task one")
        self.assertEqual(rows[0].status_text, "❌ not done: bad result")
        self.assertEqual(rows[0].status_style, "red")


class TuiCommandTest(unittest.TestCase):
    def test_tui_command_launches_tui(self) -> None:
        with temp_config(VALID_CONFIG.format(cmd="echo {{prompt}}")) as config_path:
            with patch("bucle.cli.launch_tui") as launch:
                result = runner.invoke(app, ["tui", "--config", str(config_path)])

        self.assertEqual(result.exit_code, 0)
        launch.assert_called_once()

    def test_run_tui_reset_key_resets_selected_task(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + '\nstatus = "failure"\nfailure_reason = "bad result"\n'
        )
        with temp_config(config_text) as config_path:
            screen = FakeScreen([ord("r"), ord("q")])
            with patch("bucle.cli.draw_tui"), patch("bucle.cli.curses.curs_set"):
                from bucle.cli import run_tui

                run_tui(screen, config_path)

            document = tomlkit.parse(config_path.read_text())

        self.assertNotIn("status", document["tasks"][0])
        self.assertNotIn("failure_reason", document["tasks"][0])

    def test_run_tui_prompt_key_shows_selected_task_prompt(self) -> None:
        config_text = (
            VALID_CONFIG.format(cmd="echo {{prompt}}")
            + """

            [[tasks]]
            name = "task2"
            agent = "fake"
            prompt = "Do task two"
            """
        )
        with temp_config(config_text) as config_path:
            screen = FakeScreen([ord("j"), ord("p"), ord("q")])
            with (
                patch("bucle.cli.draw_tui"),
                patch("bucle.cli.show_task_prompt") as show_task_prompt,
                patch("bucle.cli.curses.curs_set"),
            ):
                from bucle.cli import run_tui

                run_tui(screen, config_path)

        shown_row = show_task_prompt.call_args.args[1]
        self.assertEqual(shown_row.name, "task2")
        self.assertEqual(shown_row.prompt, "Do task two")

    def test_draw_prompt_window_renders_prompt_panel(self) -> None:
        row = TaskListRow(
            index=1,
            name="task1",
            agent="fake",
            prompt="Inspect this prompt.",
            status_text="not done",
            status_style="yellow",
        )
        screen = FakeScreen([])

        max_scroll = draw_prompt_window(screen, row, scroll=0)

        drawn_text = "\n".join(screen.drawn_text)
        self.assertEqual(max_scroll, 0)
        self.assertIn("Prompt: task1", drawn_text)
        self.assertIn("Inspect this prompt.", drawn_text)
        self.assertIn("p/q close", drawn_text)


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
                cwd=str(task["cwd"]) if task.get("cwd") is not None else None,
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


class FakeScreen:
    def __init__(self, keys: list[int]) -> None:
        self.keys = list(keys)
        self.keypad_enabled = False
        self.drawn_text: list[str] = []

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def getch(self) -> int:
        return self.keys.pop(0)

    def erase(self) -> None:
        return None

    def getmaxyx(self) -> tuple[int, int]:
        return (24, 80)

    def addnstr(self, *args) -> None:
        self.drawn_text.append(str(args[2]))

    def refresh(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
