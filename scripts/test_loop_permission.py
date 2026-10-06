"""loop-permission.py の最終 symlink 判定の回帰テスト。"""

import importlib.util
import os
import subprocess
import tempfile
import unittest
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
        self.env = {"worktree": str(self.wt), "plugin_root": str(self.pr), "permlog": str(root / "log"),
                    "allow": [{"kind": "prefix", "words": ["cat"]},
                              {"kind": "prefix", "words": ["git"]},
                              {"kind": "prefix", "words": ["echo"]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool, payload):
        return PERMISSION.decide({"tool_name": tool, "tool_input": payload, "cwd": str(self.wt)}, self.env)

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


if __name__ == "__main__":
    unittest.main()
