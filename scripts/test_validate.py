#!/usr/bin/env python3
"""scripts/validate.py の回帰テスト。"""

import contextlib
import io
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_REPO = Path(__file__).resolve().parent.parent


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "workflow"
        shutil.copytree(
            SOURCE_REPO,
            self.repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        self.home = self.root / "home"
        (self.home / "dev").mkdir(parents=True)

    def run_validator(self):
        script = self.repo / "scripts" / "validate.py"
        with mock.patch("pathlib.Path.home", return_value=self.home):
            namespace = runpy.run_path(str(script), run_name="validate_under_test")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = namespace["main"]()
        return status, namespace["ERRORS"], output.getvalue()

    def append_to_checked_file(self, text):
        target = (
            self.repo
            / "plugins"
            / "dev-workflow"
            / "skills"
            / "update-doc"
            / "scripts"
            / "check_links.py"
        )
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"\n# {text}\n")

    def test_base_directory_is_treated_as_generic(self):
        (self.home / "dev" / "base").mkdir()

        status, errors, _ = self.run_validator()

        self.assertEqual(0, status, errors)
        self.assertEqual([], errors)

    def test_project_specific_directory_name_is_detected(self):
        project_name = "customer-portal"
        (self.home / "dev" / project_name).mkdir()
        self.append_to_checked_file(project_name)

        status, errors, _ = self.run_validator()

        self.assertEqual(1, status)
        self.assertTrue(
            any("周辺プロジェクト固有名の混入" in error and project_name in error for error in errors),
            errors,
        )

    def test_user_absolute_path_is_detected(self):
        absolute_path = self.home / "private" / "notes"
        self.append_to_checked_file(str(absolute_path))

        status, errors, _ = self.run_validator()

        self.assertEqual(1, status)
        self.assertTrue(
            any("ユーザー環境の絶対パス" in error and str(self.home) in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
