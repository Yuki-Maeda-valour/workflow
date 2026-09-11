import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = REPO_ROOT / "plugins/dev-workflow/skills/create-task/scripts/resolve-task-dir.py"


class TaskDirectoryResolverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_resolver(self, task_dir=None, resolver=RESOLVER):
        command = [sys.executable, str(resolver), "--project-root", str(self.root)]
        if task_dir is not None:
            command.append(f"--task-dir={task_dir}")
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        return completed.returncode, json.loads(completed.stdout)

    def state_file(self, directory, name="進行中_作業.md"):
        path = self.root / directory
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("# task\n", encoding="utf-8")

    def test_default_when_no_state_file_exists(self):
        (self.root / "task").mkdir()
        (self.root / "task/.gitkeep").touch()
        code, result = self.run_resolver()
        self.assertEqual(0, code)
        self.assertEqual("docs/tasks", result["task_dir"])
        self.assertEqual("default", result["source"])
        self.assertFalse((self.root / "docs").exists())

    def test_detects_old_task_directory(self):
        self.state_file("task")
        code, result = self.run_resolver()
        self.assertEqual((0, "task", "detected"), (code, result["task_dir"], result["source"]))

    def test_detects_each_state_name(self):
        for state in ("進行中", "完了", "中断", "保留"):
            with self.subTest(state=state):
                shutil.rmtree(self.root / "task", ignore_errors=True)
                self.state_file("task", f"{state}_項目.md")
                self.assertEqual("task", self.run_resolver()[1]["task_dir"])

    def test_ignores_empty_suffix_and_deep_state_file(self):
        self.state_file("docs/archive/deep", "進行中_項目.md")
        self.state_file("task", "進行中_.md")
        self.assertEqual("docs/tasks", self.run_resolver()[1]["task_dir"])

    def test_detects_supported_container_children(self):
        for directory in ("work", "docs/work", ".claude/work"):
            with self.subTest(directory=directory):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.state_file(directory)
                self.assertEqual(directory, self.run_resolver()[1]["task_dir"])

    def test_multiple_candidates_are_sorted_and_stop(self):
        self.state_file("z-tasks")
        self.state_file("docs/a-tasks")
        code, result = self.run_resolver()
        self.assertEqual(2, code)
        self.assertEqual(["docs/a-tasks", "z-tasks"], result["candidates"])

    def test_profile_has_priority_and_accepts_spaces_japanese_and_leading_dash(self):
        self.state_file("task")
        for configured in ("作業 記録", "-tasks"):
            with self.subTest(configured=configured):
                code, result = self.run_resolver(configured)
                self.assertEqual((0, configured, "profile"), (code, result["task_dir"], result["source"]))

    def test_rejects_invalid_profile_paths(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        existing_file = self.root / "file"
        existing_file.touch()
        for configured in ("", str(outside), "../outside", "file"):
            with self.subTest(configured=configured):
                code, result = self.run_resolver(configured)
                self.assertEqual(1, code)
                self.assertIn("error", result)

    def test_internal_directory_symlink_is_allowed_external_is_rejected(self):
        inside = self.root / "inside"
        inside.mkdir()
        (self.root / "internal-link").symlink_to(inside, target_is_directory=True)
        code, result = self.run_resolver("internal-link")
        self.assertEqual(0, code)
        self.assertEqual("inside", result["task_dir"])
        self.assertEqual(str(inside.resolve()), result["path"])

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "external-link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(1, self.run_resolver("external-link")[0])

    def test_symlink_parent_segments_follow_filesystem_semantics(self):
        target = self.root / "a/b"
        target.mkdir(parents=True)
        (self.root / "link").symlink_to(target, target_is_directory=True)
        code, result = self.run_resolver("link/../tasks")
        self.assertEqual(0, code)
        self.assertEqual("a/tasks", result["task_dir"])

    def test_detection_does_not_follow_symlinks(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "進行中_項目.md").touch()
        (self.root / "linked-tasks").symlink_to(outside, target_is_directory=True)
        self.assertEqual("docs/tasks", self.run_resolver()[1]["task_dir"])

    def test_state_symlink_is_not_a_candidate(self):
        directory = self.root / "task"
        directory.mkdir()
        target = self.root / "進行中_実体.md"
        target.touch()
        (directory / "進行中_リンク.md").symlink_to(target)
        self.assertEqual("docs/tasks", self.run_resolver()[1]["task_dir"])

    def test_default_collision_with_file_is_an_error(self):
        (self.root / "docs").mkdir()
        (self.root / "docs/tasks").touch()
        self.assertEqual(1, self.run_resolver()[0])

    def test_parent_segment_cannot_bypass_file_ancestor(self):
        regular_file = self.root / "file"
        regular_file.touch()
        self.assertEqual(1, self.run_resolver("file/..")[0])

        (self.root / "file-link").symlink_to(regular_file)
        self.assertEqual(1, self.run_resolver("file-link/..")[0])

    def test_default_rejects_file_ancestor_and_external_container_symlink(self):
        (self.root / "docs").touch()
        self.assertEqual(1, self.run_resolver()[0])
        (self.root / "docs").unlink()

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "docs").symlink_to(outside, target_is_directory=True)
        self.assertEqual(1, self.run_resolver()[0])

    def test_symlink_loop_returns_json_error(self):
        (self.root / "loop").symlink_to("loop")
        code, result = self.run_resolver("loop/tasks")
        self.assertEqual(1, code)
        self.assertIn("error", result)

    def test_broken_internal_symlink_returns_error(self):
        (self.root / "broken").symlink_to("missing", target_is_directory=True)
        code, result = self.run_resolver("broken/tasks")
        self.assertEqual(1, code)
        self.assertIn("error", result)

    def test_management_root_is_independent_from_source_root(self):
        (self.root / "source").mkdir()
        self.state_file("tasks")
        code, result = self.run_resolver()
        self.assertEqual(0, code)
        self.assertEqual(self.root / "tasks", Path(result["path"]))

    def test_read_only_resolution_has_no_side_effects(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.run_resolver()
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_helper_runs_from_copied_skills_distribution(self):
        copied = Path(self.temp.name) / "installed-skills"
        shutil.copytree(REPO_ROOT / "plugins/dev-workflow/skills", copied)
        resolver = copied / "create-task/scripts/resolve-task-dir.py"
        code, result = self.run_resolver(resolver=resolver)
        self.assertEqual(0, code)
        self.assertEqual("docs/tasks", result["task_dir"])


if __name__ == "__main__":
    unittest.main()
