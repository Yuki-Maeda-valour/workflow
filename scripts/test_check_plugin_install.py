"""専用設定・一覧の検査・一致前の不実行を、使い捨ての CLI で確認する。"""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import check_plugin_install as checker


class PluginInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="plugin-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "検証用 コピー"
        self.source = self.root / "plugins/dev-workflow"
        self.source.mkdir(parents=True)
        (self.source / "data").write_text("source\n")
        self.config = self.base / "config"
        self.config.mkdir()
        self.installed = self.config / "cache/plugin"
        self.installed.mkdir(parents=True)
        self.row = {"id": checker.PLUGIN_ID, "scope": "user", "enabled": True,
                    "installPath": str(self.installed)}
        self.calls = []
        self.configs = []
        self.expected_env = {key: os.environ.get(key) for key in ("HOME", "CODEX_HOME")}
        self.info = {"os": "linux", "arch": "x86_64", "resolve_cases": 15,
                     "self_sha256": "a" * 64}

    def fake_cli(self, cli, args, *, root, env):
        self.assertEqual(root, self.root)
        self.assertEqual({key: env.get(key) for key in self.expected_env}, self.expected_env)
        config = Path(env["CLAUDE_CONFIG_DIR"])
        self.assertNotEqual(config, Path(os.environ.get("CLAUDE_CONFIG_DIR", "/not-set")))
        self.assertTrue(config.is_dir())
        self.assertNotIn(self.source, config.parents)
        self.configs.append(config)
        self.calls.append(args)
        if args[0] == "install":
            shutil.copytree(self.source, config / "cache/plugin")
        if args[0] == "list":
            return json.dumps([{**self.row, "installPath": str(config / "cache/plugin")}]).encode()
        return b"ok\n"

    def run_check(self, fake=None, launch=None):
        with mock.patch.object(checker.shutil, "which", return_value="/fixture/claude"), \
                mock.patch.object(checker, "run_cli", side_effect=fake or self.fake_cli), \
                mock.patch.object(checker, "check_plugin_launch", side_effect=launch, return_value=self.info) as started:
            result = checker.check_plugin_install(self.root)
            return result, started

    def test_success_uses_dedicated_config_and_installed_path_then_cleans_own_config(self):
        result, started = self.run_check()
        self.assertEqual(self.calls, [["marketplace", "add", str(self.root)],
                                     ["install", checker.PLUGIN_ID, "--scope", "user"], ["list", "--json"]])
        self.assertEqual(len(set(self.configs)), 1)
        started.assert_called_once_with(self.configs[0] / "cache/plugin")
        self.assertTrue(all(not config.exists() for config in self.configs))
        self.assertEqual(result["plugin_id"], checker.PLUGIN_ID)
        self.assertTrue(self.config.exists())

    def test_cli_missing_is_failure_not_skip(self):
        with mock.patch.object(checker.shutil, "which", return_value=None), mock.patch.object(checker, "check_plugin_launch") as started:
            with self.assertRaisesRegex(ValueError, "claude CLI"):
                checker.check_plugin_install(self.root)
            started.assert_not_called()

    def test_each_cli_step_failure_stops_before_launch_and_cleans_config(self):
        for step in ("marketplace", "install", "list"):
            def failing(cli, args, **kwargs):
                if args[0] == step:
                    raise ValueError("CLI failure")
                return self.fake_cli(cli, args, **kwargs)
            with self.subTest(step=step), mock.patch.object(checker, "check_plugin_launch") as started:
                with mock.patch.object(checker.shutil, "which", return_value="/fixture/claude"), mock.patch.object(checker, "run_cli", side_effect=failing):
                    with self.assertRaisesRegex(ValueError, "CLI failure"):
                        checker.check_plugin_install(self.root)
                started.assert_not_called()
            self.assertTrue(all(not config.exists() for config in self.configs))

    def test_invalid_lists_and_paths_are_rejected(self):
        cases = [b"not json", b"{}", b"[]", b"[null]", json.dumps([self.row, self.row]).encode(),
                 json.dumps([{**self.row, "id": "other"}]).encode(),
                 b'[{"id":"x","id":"y"}]']
        for field, value in (("installPath", None), ("installPath", ""), ("installPath", " "),
                             ("installPath", []), ("installPath", "relative"),
                             ("installPath", str(self.source)), ("installPath", str(self.base)),
                             ("installPath", str(self.config)), ("installPath", str(self.config / "missing")),
                             ("scope", "project"), ("enabled", False), ("enabled", 1)):
            cases.append(json.dumps([{**self.row, field: value}]).encode())
        for field in ("installPath", "scope", "enabled"):
            cases.append(json.dumps([{key: value for key, value in self.row.items() if key != field}]).encode())
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises((ValueError, OSError)):
                checker.select_install(raw, config=self.config, source=self.source)
        linked = self.config / "outside"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            checker.select_install(json.dumps([{**self.row, "installPath": str(linked)}]).encode(),
                                   config=self.config, source=self.source)

    def test_invalid_listing_stops_before_launch(self):
        def bad_list(cli, args, **kwargs):
            raw = self.fake_cli(cli, args, **kwargs)
            return b"[]" if args[0] == "list" else raw
        with mock.patch.object(checker, "check_plugin_launch") as started:
            with mock.patch.object(checker.shutil, "which", return_value="/fixture/claude"), mock.patch.object(checker, "run_cli", side_effect=bad_list):
                with self.assertRaisesRegex(ValueError, "1 件"):
                    checker.check_plugin_install(self.root)
            started.assert_not_called()

    def test_content_permissions_missing_extra_symlink_and_source_changes_stop_before_launch(self):
        for kind in ("content", "permissions", "missing", "extra", "symlink", "source"):
            def mutate(cli, args, **kwargs):
                raw = self.fake_cli(cli, args, **kwargs)
                if args[0] == "list":
                    installed = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"]) / "cache/plugin"
                    target = installed / "data"
                    if kind == "content":
                        target.write_text("changed\n")
                    elif kind == "permissions":
                        target.chmod(0o700)
                    elif kind == "missing":
                        target.unlink()
                    elif kind == "extra":
                        (installed / "extra").write_text("unexpected")
                    elif kind == "symlink":
                        target.unlink()
                        target.symlink_to(self.source / "data")
                    else:
                        (self.source / "data").write_text("changed\n")
                return raw
            with self.subTest(kind=kind), mock.patch.object(checker, "check_plugin_launch") as started:
                with mock.patch.object(checker.shutil, "which", return_value="/fixture/claude"), mock.patch.object(checker, "run_cli", side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, "一致|リンク|元ソース"):
                        checker.check_plugin_install(self.root)
                started.assert_not_called()
            (self.source / "data").write_text("source\n")

    def test_launch_failure_and_launch_mutation_are_failures(self):
        def broken(path):
            raise ValueError("launch failed")
        with self.assertRaisesRegex(ValueError, "launch failed"):
            self.run_check(launch=broken)
        def changed(path):
            (path / "data").write_text("changed\n")
            return self.info
        with self.assertRaisesRegex(ValueError, "起動検査中"):
            self.run_check(launch=changed)

    def test_run_cli_uses_argument_array_and_rejects_status_os_error_and_timeout(self):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config))
        with mock.patch.object(checker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"ok", b"")) as called:
            self.assertEqual(checker.run_cli("/space cli", ["marketplace", "add", str(self.root)], root=self.root, env=env), b"ok")
            argv = called.call_args.args[0]
            self.assertEqual(argv, ["/space cli", "plugin", "marketplace", "add", str(self.root)])
            self.assertNotIn("shell", called.call_args.kwargs)
        for error in (OSError("missing"), subprocess.TimeoutExpired("cli", 120)):
            with mock.patch.object(checker.subprocess, "run", side_effect=error), self.assertRaisesRegex(ValueError, "実行できません"):
                checker.run_cli("cli", ["list"], root=self.root, env=env)
        with mock.patch.object(checker.subprocess, "run", return_value=subprocess.CompletedProcess([], 7, b"", b"failed")), self.assertRaisesRegex(ValueError, "失敗"):
            checker.run_cli("cli", ["list"], root=self.root, env=env)

    def test_real_installed_commands_after_fake_cli_copy(self):
        shutil.rmtree(self.source)
        shutil.copytree(checker.ROOT / "plugins/dev-workflow", self.source)
        with mock.patch.object(checker.shutil, "which", return_value="/fixture/claude"), \
                mock.patch.object(checker, "run_cli", side_effect=self.fake_cli):
            result = checker.check_plugin_install(self.root)
        self.assertEqual(result["resolve_cases"], 15)
        self.assertEqual(result["plugin_id"], checker.PLUGIN_ID)

    def test_cli_reports_failure_and_success(self):
        with mock.patch.object(checker, "check_plugin_install", return_value={**self.info, "files": 1, "plugin_id": checker.PLUGIN_ID}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(checker.main(["--root", str(self.root)]), 0)
            self.assertIn("合格", output.getvalue())
        with mock.patch.object(checker, "check_plugin_install", side_effect=ValueError("bad")), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(checker.main([]), 1)
            self.assertEqual(output.getvalue(), "ERROR: bad\n")


if __name__ == "__main__":
    unittest.main()
