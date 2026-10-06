"""loop-permission.py の symlink・再帰削除・保護 cwd の回帰テスト。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "plugins/dev-workflow/skills/ship-task/scripts/loop-permission.py"
SPEC = importlib.util.spec_from_file_location("loop_permission", SCRIPT)
PERMISSION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PERMISSION)


class LoopPermissionSymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.wt = root / "worktree"
        self.pr = root / "plugin"
        self.wt.mkdir()
        self.pr.mkdir()
        (self.wt / ".claude/reviews").mkdir(parents=True)
        (self.wt / ".claude/worktrees").mkdir(parents=True)
        (self.wt / ".claude/settings.json").write_text("settings")
        (self.wt / ".mcp.json").write_text("mcp")
        (self.wt / "safe.txt").write_text("safe")
        (self.wt / "ordinary/deep").mkdir(parents=True)
        (self.wt / "ordinary/deep/file.txt").write_text("ordinary")
        self.env = {"worktree": str(self.wt), "plugin_root": str(self.pr), "permlog": str(root / "log"),
                    "allow": [{"kind": "prefix", "words": ["cat"]},
                              {"kind": "prefix", "words": ["git"]},
                              {"kind": "prefix", "words": ["echo"]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool, payload, cwd=None):
        return PERMISSION.decide({"tool_name": tool, "tool_input": payload, "cwd": str(cwd or self.wt)}, self.env)

    def bash(self, command):
        return self.decide("Bash", {"command": command})

    def expect(self, command, behavior, kind=None):
        got = self.bash(command)
        self.assertEqual((behavior, kind), got[:2], got)

    def test_final_symlink_to_protected_is_denied_for_write_and_read(self):
        os.symlink(".claude/settings.json", self.wt / "one")
        self.expect("echo x > ./one", "deny", "protected")
        self.expect("cat ./one", "deny", "other")
        self.expect("touch ./one", "deny", "protected")
        self.expect("tee ./one", "deny", "protected")
        self.expect("cp safe.txt ./one", "deny", "protected")
        self.expect("sed -i s/a/b/ ./one", "deny", "protected")

    def test_multistage_relative_and_dangling_links_to_protected_are_denied(self):
        os.symlink(".claude/settings.json", self.wt / "relative")
        os.symlink("relative", self.wt / "multi")
        os.symlink(".claude/missing.json", self.wt / "dangling")
        for name in ("relative", "multi", "dangling"):
            self.expect(f"echo x > ./{name}", "deny", "protected")
            self.expect(f"cat ./{name}", "deny", "other")

    def test_relative_link_from_subdirectory_to_protected_is_denied(self):
        (self.wt / "sub").mkdir()
        os.symlink("../.claude/settings.json", self.wt / "sub/up")
        self.expect("echo x > sub/up", "deny", "protected")
        self.expect("cat sub/up", "deny", "other")

    def test_bare_link_is_checked_in_args_options_and_assignments(self):
        os.symlink(".claude/settings.json", self.wt / "bare")
        self.expect("cat bare", "deny", "other")
        self.expect("git --path=bare", "deny", "other")
        self.expect("LANG=bare cat safe.txt", "deny", "other")

    def test_plugin_alias_to_protected_worktree_path_is_denied(self):
        os.symlink(str(self.wt / ".claude/settings.json"), self.pr / "alias")
        self.expect(f"cat {self.pr}/alias", "deny", "other")

    def test_command_name_keeps_path_check_but_not_bare_path_lookup(self):
        os.symlink(".claude/settings.json", self.wt / "git")
        os.symlink(".claude/settings.json", self.wt / "command-link")
        self.env["allow"] = [{"kind": "all", "words": []}]
        self.expect("git status", "allow")
        self.expect("./command-link status", "deny", "other")

    def test_safe_paths_and_state_area_remain_allowed(self):
        os.symlink("safe.txt", self.wt / "safe-link")
        self.expect("cat safe-link", "allow")
        self.expect("echo x > safe.txt", "allow")
        self.expect("mkdir .claude/reviews/new", "allow")
        self.expect("echo x > .claude/reviews/out", "allow")
        self.expect("echo x > .claude/worktrees/ordinary", "allow")
        self.expect("echo x > safe-link", "allow")
        self.expect("echo x > .claude/grasp.md", "allow")
        self.expect("echo x > .claude/.understand-project-done", "allow")
        self.expect("echo x > /dev/null", "allow")
        (self.pr / "ordinary").write_text("plugin")
        self.expect(f"cat {self.pr}/ordinary", "allow")

    def test_external_link_is_denied_and_w_symlink_does_not_get_exception(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("outside")
        os.symlink(str(outside), self.wt / "outside-link")
        os.symlink(".claude/reviews/out", self.wt / "w-link")
        self.expect("echo x > ./outside-link", "deny", "other")
        self.expect("cat ./outside-link", "deny", "other")
        self.expect("echo x > ./w-link", "deny", "protected")
        self.expect("cat ./w-link", "deny", "other")

    def test_rm_and_mv_operate_on_safe_link_itself(self):
        os.symlink(".claude/settings.json", self.wt / "removable")
        self.expect("rm removable", "allow")
        self.expect("mv removable renamed", "allow")
        subprocess.run(["mv", "removable", "renamed"], cwd=self.wt, check=True)
        subprocess.run(["rm", "renamed"], cwd=self.wt, check=True)
        self.assertFalse((self.wt / "removable").exists())
        self.assertFalse((self.wt / "renamed").exists())
        self.assertEqual("settings", (self.wt / ".claude/settings.json").read_text())

    def test_protected_link_location_is_still_denied(self):
        os.symlink("../../safe.txt", self.wt / ".claude/reviews/protected-link")
        self.expect("cat .claude/reviews/protected-link", "deny", "other")
        self.expect("echo x > .claude/reviews/protected-link", "deny", "protected")
        self.expect("rm .claude/reviews/protected-link", "deny", "protected")

    def test_root_protected_file_is_checked_through_a_link(self):
        os.symlink(".mcp.json", self.wt / "mcp-link")
        self.expect("cat mcp-link", "deny", "other")
        self.expect("echo x > mcp-link", "deny", "protected")

    def test_input_redirect_contract_is_unchanged(self):
        os.symlink(".claude/settings.json", self.wt / "input-link")
        self.expect("cat < ./input-link", "allow")

    def test_other_command_cannot_cancel_protected_link_read(self):
        os.symlink(".claude/settings.json", self.wt / "joined")
        for command in (
            "cat joined; rm joined",
            "cat joined && mv joined moved",
            "cat joined || rm joined",
            "cat joined | tee out; rm joined",
            "rm joined; cat joined",
            "LANG=joined cat safe.txt; rm joined",
            "CDPATH= cd -P -- . && cat joined; rm joined",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")

    def test_other_denial_still_takes_priority_over_protected_write(self):
        os.symlink(".claude/settings.json", self.wt / "mixed")
        for command in (
            "echo x > .claude/settings.json; cat mixed; rm mixed",
            "cat mixed; echo x > .claude/settings.json; rm mixed",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")

    def test_separate_safe_commands_preserve_link_removal_and_write_kinds(self):
        os.symlink(".claude/settings.json", self.wt / "removable")
        self.expect("rm removable; echo done", "allow")
        self.expect("mv removable moved && echo done", "allow")
        self.expect("echo x > removable; echo done", "deny", "protected")
        self.expect("echo done; echo x > removable", "deny", "protected")

    def test_protected_link_after_option_terminator_is_a_path(self):
        for name in ("-alias", "--path=alias"):
            os.symlink(".claude/settings.json", self.wt / name)
            with self.subTest(name=name):
                self.expect(f"cat -- {name}", "deny", "other")
                self.expect(f"cp -- {name} copy", "deny", "other")
                self.expect(f"cat -- {name}; rm -- {name}", "deny", "other")

    def test_option_terminator_preserves_safe_paths_and_link_removal(self):
        os.symlink("safe.txt", self.wt / "-safe")
        os.symlink(".claude/settings.json", self.wt / "-protected")
        self.expect("cat -- -safe", "allow")
        self.expect("rm -- -protected", "allow")
        self.expect("mv -- -protected moved", "allow")

    def test_h27_protected_cwd_checks_bare_values_even_when_missing(self):
        for directory in (".claude", ".config/git", "nested/.claude"):
            cwd = self.wt / directory
            cwd.mkdir(parents=True, exist_ok=True)
            for command in ("git checkout -- settings.json", "cat missing", "git --path=missing",
                            "LANG=missing cat", "export LANG=missing", "cat -- -missing"):
                for prefix in (False, True):
                    with self.subTest(directory=directory, command=command, prefix=prefix):
                        actual = f"CDPATH= cd -P -- {directory} && {command}" if prefix else command
                        got = self.decide("Bash", {"command": actual}, self.wt if prefix else cwd)
                        self.assertEqual(("deny", "other"), got[:2], got)

    def test_h27_symlink_cwd_resolves_to_protected_directory(self):
        os.symlink(".claude", self.wt / "alias")
        (self.wt / ".claude/child").mkdir()
        for directory in ("alias", "alias/child"):
            for prefix in (False, True):
                with self.subTest(directory=directory, prefix=prefix):
                    command = "git checkout -- settings.json"
                    if prefix:
                        command = f"CDPATH= cd -P -- {directory} && {command}"
                    got = self.decide("Bash", {"command": command},
                                      self.wt if prefix else self.wt / directory)
                    self.assertEqual(("deny", "other"), got[:2], got)

    def test_h27_nested_protected_name_inside_reviews_keeps_existing_w_contract(self):
        # W includes every real descendant of reviews, including a directory named .git.
        cwd = self.wt / ".claude/reviews/.git"
        cwd.mkdir()
        for prefix in (False, True):
            command = "git checkout -- config"
            if prefix:
                command = "CDPATH= cd -P -- .claude/reviews/.git && " + command
            got = self.decide("Bash", {"command": command}, self.wt if prefix else cwd)
            self.assertEqual(("allow", None), got[:2], got)

    def test_h27_write_kinds_and_bare_command_names(self):
        cwd = self.wt / ".claude"
        for command in ("touch settings.json", "tee settings.json", "cp grasp.md settings.json",
                        "> settings.json"):
            with self.subTest(command=command):
                got = self.decide("Bash", {"command": command}, cwd)
                # A redirection alone is outside the supported grammar.
                expected = "other" if command.startswith(">") else "protected"
                self.assertEqual(("deny", expected), got[:2], got)
        for command in ("git", "touch grasp.md", "touch reviews/out", "echo > reviews/out"):
            with self.subTest(command=command):
                got = self.decide("Bash", {"command": command}, cwd)
                self.assertEqual(("allow", None), got[:2], got)
        got = self.decide("Bash", {"command": "echo data > reviews/out"}, cwd)
        self.assertEqual(("deny", "other"), got[:2], got)

    def test_h27_fallthrough_checks_original_protected_cwd(self):
        for operator in ("||", ";"):
            command = f"CDPATH= cd -P -- ../ordinary && git {operator} git checkout -- settings.json"
            got = self.decide("Bash", {"command": command}, self.wt / ".claude")
            self.assertEqual(("deny", "other"), got[:2], got)
        got = self.decide("Bash", {"command":
            "CDPATH= cd -P -- ../ordinary && git checkout -- settings.json"}, self.wt / ".claude")
        self.assertEqual(("allow", None), got[:2], got)

    def test_h27_w_and_ordinary_cwd_keep_bare_arguments(self):
        for directory in ("ordinary", ".claude/reviews", ".claude/worktrees"):
            for command in ("git checkout -- settings.json", "touch missing", "echo data > out",
                            "git --path=missing", "LANG=missing cat", "cat -- -missing"):
                for prefix in (False, True):
                    with self.subTest(directory=directory, command=command, prefix=prefix):
                        actual = f"CDPATH= cd -P -- {directory} && {command}" if prefix else command
                        got = self.decide("Bash", {"command": actual},
                                          self.wt if prefix else self.wt / directory)
                        self.assertEqual(("allow", None), got[:2], got)
        os.symlink("../settings.json", self.wt / ".claude/reviews/alias")
        got = self.decide("Bash", {"command": "cat alias"}, self.wt / ".claude/reviews")
        self.assertEqual(("deny", "other"), got[:2], got)

    def test_h24_h27_bundled_flags_keep_w_file_operations_from_protected_cwd(self):
        cwd = self.wt / ".claude"
        source = cwd / "grasp.md"
        source.write_text("grasp")
        for prefix in (False, True):
            for command in ("cp -pv grasp.md reviews/out", "rm -rf reviews/sub"):
                with self.subTest(prefix=prefix, command=command):
                    target = cwd / ("reviews/out" if command.startswith("cp") else "reviews/sub")
                    if command.startswith("rm"):
                        target.mkdir(exist_ok=True)
                        (target / "file").write_text("remove")
                    actual = f"CDPATH= cd -P -- .claude && {command}" if prefix else command
                    run_cwd = self.wt if prefix else cwd
                    got = self.decide("Bash", {"command": actual}, run_cwd)
                    self.assertEqual(("allow", None), got[:2], got)
                    subprocess.run(["bash", "-c", actual], cwd=run_cwd, check=True,
                                   capture_output=True, text=True)
                    if command.startswith("cp"):
                        self.assertEqual("grasp", target.read_text())
                        target.unlink()
                    else:
                        self.assertFalse(target.exists())
                    self.assertEqual("settings", (cwd / "settings.json").read_text())

    def test_h27_stdin_hook_and_real_git_checkout(self):
        git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.wt, env=git_env, check=True,
                                  capture_output=True, text=True)
        git("init")
        paths = (".claude/settings.json", ".claude/reviews/settings.json", "ordinary/settings.json")
        for path in paths:
            (self.wt / path).write_text("original")
        git("add", "--", *paths)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        hook_env = dict(git_env, DEV_WORKFLOW_LOOP_WORKTREE=str(self.wt),
                        DEV_WORKFLOW_LOOP_PLUGIN_ROOT=str(self.pr),
                        DEV_WORKFLOW_LOOP_PERMLOG=self.env["permlog"],
                        DEV_WORKFLOW_LOOP_ALLOW=json.dumps(self.env["allow"]))
        for path in paths:
            target = self.wt / path
            for prefix in (False, True):
                for missing in (False, True):
                    with self.subTest(path=path, prefix=prefix, missing=missing):
                        target.write_text("changed")
                        if missing:
                            target.unlink()
                        directory = str(Path(path).parent)
                        command = "git checkout -- settings.json"
                        if prefix:
                            command = f"CDPATH= cd -P -- {directory} && {command}"
                        cwd = self.wt if prefix else target.parent
                        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
                        run = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                                             text=True, capture_output=True, env=hook_env, check=True)
                        decision = json.loads(run.stdout)["hookSpecificOutput"]["decision"]
                        log = json.loads(Path(self.env["permlog"]).read_text().splitlines()[-1])
                        expected = "deny" if path == paths[0] else "allow"
                        self.assertEqual(expected, decision["behavior"], decision)
                        self.assertEqual((expected, "other" if expected == "deny" else None),
                                         (log["decision"], log["kind"]), log)
                        self.assertEqual(str(cwd), log["cwd"])
                        self.assertEqual(command, log["subject"])
                        if expected == "allow":
                            subprocess.run(["bash", "-c", command], cwd=cwd, env=git_env, check=True,
                                           capture_output=True, text=True)
                            self.assertEqual("original", target.read_text())
                        elif missing:
                            self.assertFalse(target.exists())
                        else:
                            self.assertEqual("changed", target.read_text())

    def test_h37_untracked_directory_moves_are_denied_before_allow_rules(self):
        self.env["allow"] = [{"kind": "all", "words": []}]
        for command in (
            "pushd .",
            "popd",
            "builtin cd .",
            "builtin -- cd .",
            "command cd .",
            "command -p cd .",
            "command -pp -- builtin -- cd .",
            "command command -p -- pushd .",
            "builtin command -p -- popd",
            "echo ok && command cd .",
            "echo ok || pushd .",
            "echo ok; popd",
            "echo ok | command -p cd .",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")
        for allow in (
            [{"kind": "prefix", "words": ["builtin"]}],
            [{"kind": "exact", "words": ["command", "cd", "."]}],
        ):
            self.env["allow"] = allow
            with self.subTest(allow=allow):
                command = "builtin cd ." if allow[0]["kind"] == "prefix" else "command cd ."
                self.expect(command, "deny", "other")
        self.env["allow"] = [{"kind": "all", "words": []}]
        (self.wt / "sub").mkdir()
        for command in (
            "CDPATH= cd -P -- sub && builtin cd ../sub && echo changed > local.txt",
            "LANG=C builtin cd sub > .claude/reviews/h37.txt",
            "command -p -- command -pp -- builtin -- cd sub",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")

    def test_h37_reserved_prefixes_and_command_queries_keep_their_contract(self):
        self.env["allow"] = [{"kind": "all", "words": []}]
        for command in ("! echo ok", "time -p echo ok", "! time echo ok", "time ! echo ok"):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")
        for command in ("!'' echo ok", "time'' echo ok", "command -v cd", "command -V pushd",
                        "echo cd pushd popd", "command -x cd"):
            with self.subTest(command=command):
                self.expect(command, "allow")
        for command in ("command -v echo; builtin cd .", "command -x echo; command -p cd ."):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")
        for command in (
            "builtin echo ok", "builtin -- echo ok", "command -p echo ok", "command -pp -- echo ok",
            "command command -p -- builtin -- echo ok", "'builtin' echo ok", "\\command -p echo ok",
            "command -pv cd", "command -Vp pushd", "command -- echo ok", "builtin -- echo ok",
        ):
            with self.subTest(command=command):
                self.expect(command, "allow")
        for command in ("command -- cd .", "builtin -- pushd ."):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")
        deep = " ".join(["command"] * 2000 + ["cd", "."])
        self.expect(deep, "deny", "other")

    def test_h37_all_unquoted_reserved_prefixes_are_denied(self):
        self.env["allow"] = [{"kind": "all", "words": []}]
        (self.wt / "sub").mkdir()
        reserved = {
            "!", "time", "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done", "select",
            "case", "esac", "in", "function", "coproc",
        }
        for word in sorted(reserved):
            with self.subTest(word=word):
                self.expect(f"{word} echo ok", "deny", "other")
        for command in (
            "if builtin cd sub; then echo changed > local.txt; fi",
            "while builtin cd sub; do echo changed > local.txt; done",
            "until builtin cd sub; do echo changed > local.txt; done",
            "for x in once; do builtin cd sub; done",
            "elif builtin cd sub; then echo changed > local.txt; fi",
            "select x in once; do builtin cd sub; done",
            "echo ok && if builtin cd sub; then echo changed > local.txt; fi",
            "echo ok | time builtin cd sub",
            "CDPATH= cd -P -- sub && if builtin cd ..; then echo changed > local.txt; fi",
            "CDPATH= cd -P -- sub && time builtin cd ..",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")
        for command in ("t''ime echo ok", "''time echo ok", "!'' echo ok", "\\time echo ok", "'if' echo ok"):
            with self.subTest(command=command):
                self.expect(command, "allow")

    def test_h37_existing_cd_form_and_prefix_remain_allowed(self):
        self.env["allow"] = [{"kind": "all", "words": []}]
        (self.wt / "sub").mkdir()
        self.expect("CDPATH= cd -P -- sub && pwd -P", "allow")
        self.expect("CDPATH= cd -P -- sub && echo ok", "allow")

    def test_h26_recursive_remove_rejects_worktree_and_protected_descendants(self):
        (self.wt / "protected/.claude").mkdir(parents=True)
        (self.wt / "protected/.claude/settings.json").write_text("settings")
        (self.wt / "git-child/.config/git").mkdir(parents=True)
        (self.wt / "name-child").mkdir()
        (self.wt / "name-child/.mcp.json").write_text("mcp")
        (self.wt / "dotgit-child/.git").mkdir(parents=True)
        (self.wt / "dotgit-file").mkdir()
        (self.wt / "dotgit-file/.git").write_text("gitdir")
        (self.wt / "sub").mkdir()
        os.symlink("protected", self.wt / "protected-link")

        for path in (str(self.wt), f"{self.wt}/", ".."):
            with self.subTest(worktree=path):
                got = self.decide("Bash", {"command": f"rm -rf {path}"}) if path != ".." else \
                    PERMISSION.decide({"tool_name": "Bash", "tool_input": {"command": f"rm -rf {path}"},
                                       "cwd": str(self.wt / "sub")}, self.env)
                self.assertEqual(("deny", "other"), got[:2], got)
        for path in ("protected", "protected/", "protected/../protected", "git-child", "name-child", "dotgit-child",
                     "dotgit-file", "protected-link/", "protected-link//"):
            with self.subTest(path=path):
                self.expect(f"rm -rf {path}", "deny", "protected")

    def test_h26_recursive_remove_preserves_normal_w_and_link_contracts(self):
        (self.wt / ".claude/worktrees/ordinary").mkdir(parents=True)
        (self.wt / ".claude/worktrees/ordinary/file.txt").write_text("ordinary")
        (self.wt / ".claude/reviews/d").mkdir(parents=True)
        (self.wt / ".claude/reviews/d/file.txt").write_text("review")
        (self.wt / ".claude/reviews/safe").mkdir()
        (self.wt / ".claude/reviews/safe/file.txt").write_text("review")
        os.symlink(".claude/settings.json", self.wt / "safe-link")
        os.symlink("ordinary", self.wt / "ordinary-link")
        os.symlink("ordinary-link", self.wt / "multi-link")
        os.symlink(".claude/reviews/safe", self.wt / "review-link")
        os.symlink(".", self.wt / "worktree-link")
        os.symlink("safe.txt", self.wt / ".claude/reviews/protected-link")
        os.symlink("safe.txt", self.wt / ".claude/reviews/d/child-link")
        os.symlink(".claude/settings.json", self.wt / "ordinary/protected-link")

        for path in ("ordinary", "ordinary/", "ordinary/../ordinary", ".claude/worktrees/ordinary"):
            with self.subTest(path=path):
                self.expect(f"rm -rf {path}", "allow")
        self.expect("rm -rf missing", "allow")
        self.expect("rm -rf safe-link", "allow")
        self.expect("rm -rf ordinary-link/", "allow")
        self.expect("rm -rf multi-link//", "allow")
        self.expect("rm -rf review-link/", "allow")
        self.expect("rm -rf safe-link/", "deny", "protected")
        self.expect("rm -rf worktree-link/", "deny", "other")
        self.expect("rm -rf .claude/reviews/protected-link", "deny", "protected")
        self.expect("rm -rf .claude/reviews/d", "deny", "protected")
        self.expect("rm -rf safe-link/.", "deny", "protected")

    def test_h26_recursive_remove_rejects_plugin_link_even_when_its_target_is_safe(self):
        os.symlink(str(self.wt / "ordinary"), self.pr / "ordinary-alias")
        self.expect(f"rm -rf {self.pr}/ordinary-alias/", "deny", "other")
        self.expect(f"rm -rf {self.pr}/ordinary-alias//", "deny", "other")
        os.symlink("ordinary", self.wt / "ordinary-alias")
        self.expect("rm -rf ordinary-alias/", "allow")

    def test_h26_recursive_remove_rejects_worktree_link_to_plugin_only_with_trailing_slash(self):
        (self.pr / "ordinary").mkdir()
        (self.pr / "ordinary/file.txt").write_text("plugin")
        os.symlink(str(self.pr / "ordinary"), self.wt / "plugin-alias")
        self.expect("rm -rf plugin-alias", "allow")
        self.expect("rm -rf plugin-alias/", "deny", "other")
        self.expect("rm -rf plugin-alias//", "deny", "other")

    def test_h26_recursive_remove_denies_uninspectable_descendants_without_recursion_limit(self):
        current = self.wt / "deep"
        current.mkdir()
        for index in range(1200):
            current = current / "d"
            current.mkdir()
        self.expect("rm -rf deep", "allow")
        subprocess.run(["rm", "-rf", str(self.wt / "deep")], check=True)

        with mock.patch.object(PERMISSION.os, "scandir", side_effect=OSError("blocked")):
            self.expect("rm -rf ordinary", "deny", "other")

    def test_h26_recursive_remove_denies_disappearing_and_unreadable_children(self):
        with mock.patch.object(PERMISSION.os, "scandir", side_effect=FileNotFoundError("gone")):
            self.expect("rm -rf ordinary", "deny", "other")

        class BrokenEntries:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                yield object()
                raise OSError("interrupted")

        with mock.patch.object(PERMISSION.os, "scandir", return_value=BrokenEntries()):
            self.expect("rm -rf ordinary", "deny", "other")

        original_lstat = PERMISSION.os.lstat
        child = str(self.wt / "ordinary/deep")

        def unreadable(path):
            if os.fspath(path) == child:
                raise PermissionError("blocked")
            return original_lstat(path)

        with mock.patch.object(PERMISSION.os, "lstat", side_effect=unreadable):
            self.expect("rm -rf ordinary", "deny", "other")

    def test_h26_recursive_remove_keeps_rmdir_and_mv_non_recursive(self):
        (self.wt / "move-source/.claude").mkdir(parents=True)
        (self.wt / "move-source/.claude/settings.json").write_text("settings")
        self.expect("rmdir move-source", "allow")
        self.expect("mv move-source moved", "allow")

    def test_h24_attached_short_option_path_candidates_are_denied(self):
        os.symlink(".claude/settings.json", self.wt / "attached-link")
        for command in (
            "git format-patch -1 -o.git HEAD",
            "git format-patch -1 -o.mcp.json HEAD",
            "git format-patch -1 -ko.git HEAD",
            "git format-patch -1 -klefthook.yml HEAD",
            "git format-patch -1 -oattached-link HEAD",
            "git format-patch -1 -ofoo/bar HEAD",
            "git format-patch -1 -o~ HEAD",
            "git format-patch -1 -o'.git' HEAD",
            "git format-patch -1 -o\\.git HEAD",
        ):
            with self.subTest(command=command):
                self.expect(command, "deny", "other")

    def test_h24_short_option_normal_flags_and_option_terminator_remain_allowed(self):
        self.expect("git format-patch -1 -n -pv HEAD", "allow")
        self.expect("git format-patch -- -o.git", "allow")
        os.symlink(".claude/settings.json", self.wt / "-o.git")
        self.expect("git format-patch -- -o.git", "deny", "other")

    def test_h24_long_option_and_file_operation_contracts_remain_unchanged(self):
        self.expect("git format-patch --output=.git HEAD", "deny", "other")
        self.expect("rm -rf ordinary", "allow")
        self.expect("cp -pv safe.txt copy.txt", "allow")

    def test_h24_real_git_positive_control_and_hook_denial_do_not_create_patch(self):
        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        template = self.wt / "git-template"
        template.mkdir()
        subprocess.run(["git", "init", "-q", f"--template={template}"], cwd=self.wt, env=git_env, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.wt, env=git_env, check=True)
        subprocess.run(["git", "config", "user.name", "Loop Test"], cwd=self.wt, env=git_env, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.wt, env=git_env, check=True)
        subprocess.run(["git", "add", "safe.txt"], cwd=self.wt, env=git_env, check=True)
        subprocess.run(["git", "commit", "-qm", "probe"], cwd=self.wt, env=git_env, check=True)
        original = "git format-patch -1 -o.git HEAD > .claude/reviews/z"
        subprocess.run(["bash", "-c", original], cwd=self.wt, env=git_env, check=True)
        self.assertTrue(any((self.wt / ".git").glob("*.patch")))
        self.assertTrue((self.wt / ".claude/reviews/z").exists())
        for patch in (self.wt / ".git").glob("*.patch"):
            patch.unlink()
        (self.wt / ".claude/reviews/z").unlink()

        def snapshot(root):
            entries = {}
            for path in sorted(root.rglob("*")):
                rel = str(path.relative_to(root))
                if path.is_symlink():
                    entries[rel] = ("symlink", os.readlink(path))
                elif path.is_dir():
                    entries[rel] = ("directory", "")
                else:
                    entries[rel] = ("file", path.read_bytes())
            return entries

        git_before = snapshot(self.wt / ".git")
        request = {"tool_name": "Bash", "tool_input": {"command": original},
                   "cwd": str(self.wt)}
        hook_env = os.environ | {
            "DEV_WORKFLOW_LOOP_WORKTREE": str(self.wt),
            "DEV_WORKFLOW_LOOP_PLUGIN_ROOT": str(self.pr),
            "DEV_WORKFLOW_LOOP_PERMLOG": self.env["permlog"],
            "DEV_WORKFLOW_LOOP_ALLOW": json.dumps(self.env["allow"]),
        }
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(request), text=True,
                                capture_output=True, env=hook_env, check=True)
        response = json.loads(result.stdout)
        self.assertEqual("deny", response["hookSpecificOutput"]["decision"]["behavior"])
        if response["hookSpecificOutput"]["decision"]["behavior"] == "allow":
            subprocess.run(["bash", "-c", original], cwd=self.wt, env=git_env, check=True)
        self.assertEqual(git_before, snapshot(self.wt / ".git"))
        self.assertFalse((self.wt / ".claude/reviews/z").exists())
        logged = json.loads(Path(self.env["permlog"]).read_text().splitlines()[-1])
        self.assertEqual(("deny", "other"), (logged["decision"], logged["kind"]))

        patches = self.wt / "patches"
        patches.mkdir()
        allowed = "git format-patch -1 -opatches HEAD > .claude/reviews/z"
        request["tool_input"]["command"] = allowed
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(request), text=True,
                                capture_output=True, env=hook_env, check=True)
        response = json.loads(result.stdout)
        self.assertEqual("allow", response["hookSpecificOutput"]["decision"]["behavior"])
        if response["hookSpecificOutput"]["decision"]["behavior"] == "allow":
            subprocess.run(["bash", "-c", allowed], cwd=self.wt, env=git_env, check=True)
        self.assertTrue(any(patches.glob("*.patch")))
        self.assertTrue((self.wt / ".claude/reviews/z").exists())

    def test_h24_hook_allows_normal_output_and_runs_only_after_allow(self):
        output = self.wt / ".claude/reviews/h24.txt"
        request = {"tool_name": "Bash", "tool_input": {"command": "echo h24 > .claude/reviews/h24.txt"},
                   "cwd": str(self.wt)}
        hook_env = os.environ | {
            "DEV_WORKFLOW_LOOP_WORKTREE": str(self.wt),
            "DEV_WORKFLOW_LOOP_PLUGIN_ROOT": str(self.pr),
            "DEV_WORKFLOW_LOOP_PERMLOG": self.env["permlog"],
            "DEV_WORKFLOW_LOOP_ALLOW": json.dumps(self.env["allow"]),
        }
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(request), text=True,
                                capture_output=True, env=hook_env, check=True)
        response = json.loads(result.stdout)
        self.assertEqual("allow", response["hookSpecificOutput"]["decision"]["behavior"])
        self.assertFalse(output.exists())
        subprocess.run(["bash", "-c", request["tool_input"]["command"]], cwd=self.wt, check=True)
        self.assertEqual("h24\n", output.read_text())


if __name__ == "__main__":
    unittest.main()
