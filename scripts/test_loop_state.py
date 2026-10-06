"""Focused normal and attack regressions for loop-state.py."""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import tempfile
import importlib.util
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "plugins/dev-workflow/skills/ship-task/scripts/loop-state.py"
SPEC = importlib.util.spec_from_file_location("loop_state_test_module", SCRIPT)
assert SPEC and SPEC.loader
LOOP_STATE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(LOOP_STATE)


class LoopStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name) / "repo"; self.root.mkdir()
        self.git("init", "-q"); self.git("config", "user.email", "test@example.invalid"); self.git("config", "user.name", "test")
        (self.root / "tracked").write_text("one")
        (self.root / ".gitignore").write_text("ignored\n")
        (self.root / "ignored").write_text("ignored")
        (self.root / ".env").write_text("secret")
        self.git("add", "tracked", ".gitignore"); self.git("commit", "-qm", "base")
        self.common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip()
        self.admin = self.git("rev-parse", "--path-format=absolute", "--git-dir").stdout.decode().strip()

    def tearDown(self) -> None: self.temp.cleanup()
    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def snap(self, name: str, secret_patterns_from: str = "", fresh_config_baseline: bool = False,
             **limits: int) -> subprocess.CompletedProcess[bytes]:
        args = ["python3", str(SCRIPT), "snapshot", "--out", str(self.root.parent / name), "--top", str(self.root),
                "--common", self.common, "--repo-admin", self.admin]
        if secret_patterns_from:
            args += ["--secret-patterns-from", str(self.root.parent / secret_patterns_from)]
        if fresh_config_baseline:
            args += ["--fresh-config-baseline"]
        for key, value in limits.items(): args += ["--" + key.replace("_", "-"), str(value)]
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def compare(self, a: str, b: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["python3", str(SCRIPT), "compare", "--before", str(self.root.parent / a), "--after", str(self.root.parent / b)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def changed(self, mutate) -> subprocess.CompletedProcess[bytes]:
        self.assertEqual(0, self.snap("before.json").returncode)
        mutate()
        self.assertEqual(0, self.snap("after.json").returncode)
        result = self.compare("before.json", "after.json")
        self.assertEqual(1, result.returncode, result.stdout.decode() + result.stderr.decode())
        return result

    def test_normal_snapshot_is_stable_and_secret_is_metadata_only(self) -> None:
        first = self.snap("a.json")
        self.assertEqual(0, first.returncode, first.stderr.decode())
        self.assertEqual(0, self.snap("b.json").returncode)
        self.assertEqual(0, self.compare("a.json", "b.json").returncode)
        state = json.loads((self.root.parent / "a.json").read_text())
        secret = next(item for item in state["worktree_contents"][0]["files"] if item["path"] == ".env")
        self.assertTrue(secret["secret"])
        self.assertNotIn("sha256", secret)

    def test_prior_secret_patterns_keep_each_existing_worktree_metadata_only(self) -> None:
        other = self.root.parent / "human"
        self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        (other / ".claude").mkdir()
        (other / ".claude/project-profile.yml").write_text("secret_paths: [private.txt]\n")
        (other / "private.txt").write_text("must never become a snapshot hash\n")
        self.assertEqual(0, self.snap("secret-a.json").returncode)
        (other / ".claude/project-profile.yml").write_text("secret_paths: []\n")
        self.assertEqual(0, self.snap("secret-b.json", secret_patterns_from="secret-a.json").returncode)
        state = json.loads((self.root.parent / "secret-b.json").read_text())
        files = next(row["files"] for row in state["worktree_contents"] if row["path"] == str(other))
        private = next(row for row in files if row["path"] == "private.txt")
        self.assertTrue(private["secret"]); self.assertNotIn("sha256", private)
        patterns = next(row["patterns"] for row in state["secret_patterns"] if row["path"] == str(other))
        self.assertIn("private.txt", patterns)
        # A different existing worktree contributes its own starting union;
        # no path's profile may widen another path's content observation.
        second = self.root.parent / "human-two"
        self.git("worktree", "add", "-q", "--detach", str(second), "HEAD")
        (second / ".claude").mkdir(); (second / ".claude/project-profile.yml").write_text("secret_paths: [second.txt]\n")
        (second / "second.txt").write_text("second-secret\n")
        self.assertEqual(0, self.snap("secret-c.json", secret_patterns_from="secret-b.json").returncode)
        state = json.loads((self.root.parent / "secret-c.json").read_text())
        two = next(row["files"] for row in state["worktree_contents"] if row["path"] == str(second))
        self.assertTrue(next(row for row in two if row["path"] == "second.txt")["secret"])
        root_files = next(row["files"] for row in state["worktree_contents"] if row["path"] == str(self.root))
        self.assertIn("sha256", next(row for row in root_files if row["path"] == "tracked"))

    def test_ref_and_ignored_file_attacks_are_detected(self) -> None:
        self.assertEqual(0, self.snap("a.json").returncode)
        self.git("branch", "other")
        (self.root / "ignored").write_text("changed")
        self.assertEqual(0, self.snap("b.json").returncode)
        changed = self.compare("a.json", "b.json")
        self.assertEqual(1, changed.returncode)
        self.assertIn(b"refs", changed.stdout)
        self.assertIn(b"worktree_contents", changed.stdout)

    def test_old_state_and_file_limit_fail_closed(self) -> None:
        (self.root.parent / "old.json").write_text("{}")
        self.assertEqual(0, self.snap("new.json").returncode)
        self.assertEqual(1, self.compare("old.json", "new.json").returncode)
        self.assertEqual(20, self.snap("limited.json", max_file_bytes=1).returncode)

    def test_linked_worktree_effective_config_change_is_detected(self) -> None:
        other = self.root.parent / "human"
        self.git("config", "extensions.worktreeConfig", "true")
        self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        subprocess.run(["git", "-C", str(other), "config", "--worktree", "demo.value", "one"], check=True)
        first = self.snap("config-a.json")
        self.assertEqual(0, first.returncode, first.stderr.decode())
        subprocess.run(["git", "-C", str(other), "config", "--worktree", "demo.value", "two"], check=True)
        second = self.snap("config-b.json")
        self.assertEqual(0, second.returncode, second.stderr.decode())
        changed = self.compare("config-a.json", "config-b.json")
        self.assertEqual(1, changed.returncode)
        self.assertIn(b"worktree_configs", changed.stdout)

    def test_all_ref_kinds_and_symbolic_target_changes_are_detected(self) -> None:
        cases = (
            ("branch", lambda: self.git("branch", "other")),
            ("tag", lambda: self.git("tag", "v-test")),
            ("note", lambda: self.git("notes", "add", "-m", "note", "HEAD")),
            ("custom", lambda: self.git("update-ref", "refs/test/custom", "HEAD")),
            ("symref", lambda: self.git("symbolic-ref", "refs/test/alias", "refs/heads/master")),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                # A fresh repository avoids an earlier ref deliberately becoming
                # part of the next normal baseline.
                self.tearDown(); self.setUp()
                self.changed(mutate)

    def test_existing_worktree_files_index_lock_head_and_removal_are_detected(self) -> None:
        other = self.root.parent / "human"
        self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        def modify_index() -> None:
            (other / "tracked").write_text("staged")
            subprocess.run(["git", "-C", str(other), "add", "tracked"], check=True)
        self.changed(modify_index)
        self.tearDown(); self.setUp()
        other = self.root.parent / "human"; self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        self.changed(lambda: self.git("worktree", "lock", "--reason", "changed", str(other)))
        self.tearDown(); self.setUp()
        other = self.root.parent / "human"; self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        self.changed(lambda: self.git("worktree", "remove", "--force", str(other)))

    def test_linked_worktree_private_refs_are_detected(self) -> None:
        other = self.root.parent / "human"
        self.git("worktree", "add", "-q", "--detach", str(other), "HEAD")
        self.assertEqual(0, self.snap("private-a.json").returncode)
        subprocess.run(["git", "-C", str(other), "update-ref", "refs/worktree/bisect/selftest", "HEAD"], check=True)
        self.assertEqual(0, self.snap("private-b.json").returncode)
        changed = self.compare("private-a.json", "private-b.json")
        self.assertEqual(1, changed.returncode, changed.stdout.decode() + changed.stderr.decode())
        state = json.loads((self.root.parent / "private-b.json").read_text())
        private = next(row for row in state["worktree_configs"] if row["path"] == str(other))["private_refs"]
        self.assertTrue(any(item["path"].endswith("bisect/selftest") for item in private["refs"]))

    def test_tracked_untracked_and_ignored_contents_are_detected(self) -> None:
        for label, mutate in (
            ("tracked", lambda: (self.root / "tracked").write_text("two")),
            ("untracked", lambda: (self.root / "new").write_text("new")),
            ("ignored", lambda: (self.root / "ignored").write_text("changed")),
        ):
            with self.subTest(label=label):
                self.tearDown(); self.setUp(); self.changed(mutate)

    def test_secret_globs_do_not_hash_nested_case_or_directory_contents(self) -> None:
        (self.root / "nested").mkdir(); (self.root / "nested" / ".ENV").write_text("s")
        (self.root / "private").mkdir(); (self.root / "private" / "x").write_text("s")
        (self.root / "vault").mkdir(); (self.root / "vault" / "x").write_text("s")
        calls: list[str] = []; original = LOOP_STATE.hash_at
        def spy(parent, name, before, budget):
            calls.append(name); return original(parent, name, before, budget)
        LOOP_STATE.hash_at = spy
        try:
            entries = LOOP_STATE.tree(str(self.root), [".env", "private/**", "vault/"], LOOP_STATE.Budget(1000, 1000000, 1000000, 10))
        finally:
            LOOP_STATE.hash_at = original
        secrets = {x["path"] for x in entries if x.get("secret")}
        # A directory pattern stops descent once the directory itself is marked,
        # which is stronger than opening a descendant.
        self.assertTrue({".env", "nested/.ENV", "private/x", "vault"} <= secrets)
        self.assertNotIn(".env", calls); self.assertNotIn(".ENV", calls); self.assertNotIn("x", calls)

    def test_double_star_zero_directory_and_secret_directory_metadata(self) -> None:
        (self.root / "key.txt").write_text("secret")
        (self.root / "a").mkdir(); (self.root / "a" / "key.txt").write_text("secret")
        (self.root / "secrets").mkdir(); (self.root / "secrets" / "key").write_text("one")
        self.assertTrue(LOOP_STATE.secret_path("key.txt", ["**/key.txt"]))
        self.assertTrue(LOOP_STATE.secret_path("a/key.txt", ["**/key.txt"]))
        self.assertTrue(LOOP_STATE.secret_path("a/key.txt", ["a/**/key.txt"]))
        calls: list[str] = []; old = LOOP_STATE.hash_at
        LOOP_STATE.hash_at = lambda parent, name, before, budget: (calls.append(name), old(parent, name, before, budget))[1]
        try:
            first = LOOP_STATE.tree(str(self.root), ["**/key.txt", "secrets/"], LOOP_STATE.Budget(1000, 1000000, 1000000, 10))
        finally: LOOP_STATE.hash_at = old
        self.assertNotIn("key.txt", calls); self.assertNotIn("key", calls)
        secret = {x["path"]: x for x in first if x.get("secret")}
        self.assertIn("secrets/key", secret)
        self.assertIn("size", secret["secrets/key"]["meta"])
        (self.root / "secrets" / "key").write_text("longer-changed")
        second = LOOP_STATE.tree(str(self.root), ["**/key.txt", "secrets/"], LOOP_STATE.Budget(1000, 1000000, 1000000, 10))
        self.assertNotEqual(first, second)

    def test_profile_normal_merge_is_allowed_but_secret_duplicate_and_bad_glob_stop(self) -> None:
        profile = self.root / ".claude/project-profile.yml"; profile.parent.mkdir()
        profile.write_text('defaults: &d {quality: strict}\n<<: *d\nsecret_paths: []\n')
        self.assertEqual(0, self.snap("merged.json").returncode)
        profile.write_text('defaults: &d {secret_paths: [private.txt], secret_paths: []}\n<<: *d\n')
        self.assertEqual(20, self.snap("duplicate.json").returncode)
        profile.write_text('secret_paths: ["bad[glob"]\n')
        self.assertEqual(20, self.snap("glob.json").returncode)

    def test_dangling_symbolic_ref_and_common_hooks_info_are_detected(self) -> None:
        self.assertEqual(0, self.snap("refs-a.json").returncode)
        self.git("symbolic-ref", "refs/heads/dangling", "refs/heads/absent")
        self.assertEqual(0, self.snap("refs-b.json").returncode)
        self.assertEqual(1, self.compare("refs-a.json", "refs-b.json").returncode)
        self.tearDown(); self.setUp()
        common = pathlib.Path(self.common); (common / "hooks").mkdir(exist_ok=True)
        (common / "hooks" / "probe").write_text("one")
        (common / "info").mkdir(exist_ok=True); (common / "info" / "exclude").write_text("one")
        self.assertEqual(0, self.snap("admin-a.json").returncode)
        (common / "hooks" / "probe").write_text("two")
        self.assertEqual(0, self.snap("admin-b.json").returncode)
        self.assertEqual(1, self.compare("admin-a.json", "admin-b.json").returncode)

    def test_refs_share_the_snapshot_budget_and_config_diagnostics_remain_actionable(self) -> None:
        self.git("symbolic-ref", "refs/heads/dangling", "refs/heads/absent")
        with self.assertRaises(LOOP_STATE.Stop):
            LOOP_STATE.refs(str(self.root), LOOP_STATE.Budget(1, 10_000_000, 1_000_000, 10))
        self.assertTrue(any(x["name"] == "refs/heads/dangling" for x in LOOP_STATE.refs(
            str(self.root), LOOP_STATE.Budget(1000, 10_000_000, 1_000_000, 10))))
        self.tearDown(); self.setUp()
        self.assertEqual(0, self.snap("diag-a.json").returncode)
        self.git("config", "selftest.added", "yes")
        self.assertEqual(0, self.snap("diag-b.json").returncode)
        result = self.compare("diag-a.json", "diag-b.json")
        self.assertEqual(1, result.returncode)
        self.assertIn(b"config: +selftest.added=yes", result.stdout)

    def test_git_output_and_empty_directory_enumeration_obey_shared_limits(self) -> None:
        # The Git reader must stop while draining stdout, before retaining an
        # unbounded ref listing.  Raising the same caller-owned Budget permits
        # the normal operation.
        for number in range(20):
            self.git("update-ref", f"refs/selftest/many/{number}", "HEAD")
        with self.assertRaises(LOOP_STATE.Stop):
            LOOP_STATE.run_git(str(self.root), "for-each-ref", budget=LOOP_STATE.Budget(1000, 64, 1_000_000, 10))
        self.assertGreater(len(LOOP_STATE.run_git(
            str(self.root), "for-each-ref", budget=LOOP_STATE.Budget(1000, 1_000_000, 1_000_000, 10))), 64)
        # Empty directories have almost no readable file bytes.  Listing them
        # still consumes the item budget before their names are accumulated.
        for number in range(20): (self.root / f"empty-{number}").mkdir()
        with self.assertRaises(LOOP_STATE.Stop):
            LOOP_STATE.tree(str(self.root), [], LOOP_STATE.Budget(5, 1_000_000, 1_000_000, 10))

    def test_fifo_socket_symlink_are_metadata_only_and_do_not_block(self) -> None:
        os.mkfifo(self.root / "pipe")
        sock = socket.socket(socket.AF_UNIX); sock.bind(str(self.root / "sock"))
        try:
            os.symlink("tracked", self.root / "link")
            self.assertEqual(0, self.snap("special-a.json").returncode)
            state = json.loads((self.root.parent / "special-a.json").read_text())
            items = {x["path"]: x for x in state["worktree_contents"][0]["files"]}
            self.assertEqual("fifo", items["pipe"]["kind"]); self.assertEqual("socket", items["sock"]["kind"])
            self.assertEqual("tracked", items["link"]["target"])
            os.unlink(self.root / "link"); os.symlink(".gitignore", self.root / "link")
            self.assertEqual(0, self.snap("special-b.json").returncode)
            self.assertEqual(1, self.compare("special-a.json", "special-b.json").returncode)
        finally:
            sock.close()

    def test_secret_symlink_target_bytes_are_outside_the_metadata_only_boundary(self) -> None:
        # This is the intentional, narrow limit: the symlink object in the
        # worktree is observed, while the bytes of its target outside the
        # worktree are never opened.  It does not claim that a normal file can
        # change while retaining ctime/metadata.
        outside = self.root.parent / "outside-secret"; outside.write_text("one")
        os.symlink(outside, self.root / "secret-link")
        calls: list[str] = []; old = LOOP_STATE.hash_at
        def spy(parent, name, before, budget):
            calls.append(name); return old(parent, name, before, budget)
        LOOP_STATE.hash_at = spy
        try:
            first = LOOP_STATE.tree(str(self.root), ["secret-link"], LOOP_STATE.Budget(1000, 1000000, 1000000, 10))
            outside.write_text("changed outside target")
            second = LOOP_STATE.tree(str(self.root), ["secret-link"], LOOP_STATE.Budget(1000, 1000000, 1000000, 10))
        finally: LOOP_STATE.hash_at = old
        self.assertEqual(first, second)
        self.assertNotIn("secret-link", calls)

    def test_include_and_includeif_sources_are_detected(self) -> None:
        inc = self.root.parent / "shared.inc"; inc.write_text("[demo]\nvalue = one\n")
        self.git("config", "include.path", str(inc))
        self.changed(lambda: inc.write_text("[demo]\nvalue = two\n"))
        self.tearDown(); self.setUp()
        conditional = self.root.parent / "conditional.inc"; conditional.write_text("[demo]\nvalue = one\n")
        key = "includeIf.gitdir:" + str(self.root) + "/.path"
        self.git("config", key, str(conditional))
        self.changed(lambda: conditional.write_text("[demo]\nvalue = two\n"))

    def test_include_preflight_preserves_normal_conditions_and_stops_active_unsafe_targets(self) -> None:
        # Git parses quoted values and condition grammar.  The helper asks that
        # same Git binary to evaluate the condition, rather than approximating
        # onbranch/gitdir patterns itself.
        quoted = self.root.parent / "quoted include.inc"; quoted.write_text("[demo]\nvalue = one\n")
        self.git("config", "include.path", str(quoted))
        self.assertEqual(0, self.snap("quoted.json").returncode)
        self.tearDown(); self.setUp()
        inactive = self.root.parent / "machine-only.inc"
        self.git("config", "includeIf.onbranch:never-used-branch.path", str(inactive))
        self.assertEqual(0, self.snap("inactive.json").returncode)
        self.tearDown(); self.setUp()
        branch = self.git("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
        self.git("config", f"includeIf.onbranch:{branch}.path", str(self.root.parent / "missing-active.inc"))
        self.assertEqual(20, self.snap("missing-active.json").returncode)
        self.tearDown(); self.setUp()
        outside = self.root.parent / "outside.inc"; outside.write_text("[demo]\nvalue = outside\n")
        link = self.root.parent / "included-link"; os.symlink(outside, link)
        self.git("config", "include.path", str(link))
        # A normal config leaf link is supported at a fresh start; it is
        # recorded as the link plus its regular target instead of being followed
        # unchecked by Git.
        self.assertEqual(0, self.snap("linked-active.json").returncode)

    def test_config_link_is_fixed_within_a_run_but_refreshed_between_normal_runs(self) -> None:
        first_target = self.root.parent / "first.inc"; first_target.write_text("[demo]\nvalue = one\n")
        second_target = self.root.parent / "second.inc"; second_target.write_text("[demo]\nvalue = private-new-target\n")
        link = self.root.parent / "config-link"; os.symlink(first_target, link)
        self.git("config", "include.path", str(link))
        self.assertEqual(0, self.snap("link-a.json").returncode)
        state = json.loads((self.root.parent / "link-a.json").read_text())
        trusted = LOOP_STATE.trusted_config_origins(state, False)
        # Retargeting is rejected before open_regular_path can open/hash the new
        # target.  It therefore cannot disclose a newly pointed-at secret.
        os.unlink(link); os.symlink(second_target, link)
        calls: list[str] = []; old_open = LOOP_STATE.open_regular_path
        LOOP_STATE.open_regular_path = lambda path, budget: (calls.append(path), old_open(path, budget))[1]
        try:
            with self.assertRaises(LOOP_STATE.Stop):
                LOOP_STATE.config_snapshot(str(self.root), self.common, LOOP_STATE.Budget(10000, 10_000_000, 1_000_000, 10), trusted)
        finally:
            LOOP_STATE.open_regular_path = old_open
        self.assertNotIn(str(second_target), calls)
        # Replacing a normal target's bytes is allowed to be observed; its hash
        # makes compare report the change rather than treating it as a link swap.
        os.unlink(link); os.symlink(first_target, link)
        self.assertEqual(0, self.snap("link-restored.json", secret_patterns_from="link-a.json", fresh_config_baseline=True).returncode)
        trusted = LOOP_STATE.trusted_config_origins(json.loads((self.root.parent / "link-restored.json").read_text()), False)
        new_target = self.root.parent / "new-secret.inc"; new_target.write_text("[demo]\nvalue = never-open-this-new-target\n")
        new_link = self.root.parent / "new-config-link"; os.symlink(new_target, new_link)
        self.git("config", "--add", "include.path", str(new_link))
        calls = []; old_open = LOOP_STATE.open_regular_path
        LOOP_STATE.open_regular_path = lambda path, budget: (calls.append(path), old_open(path, budget))[1]
        try:
            with self.assertRaises(LOOP_STATE.Stop):
                LOOP_STATE.config_snapshot(str(self.root), self.common, LOOP_STATE.Budget(10000, 10_000_000, 1_000_000, 10), trusted)
        finally:
            LOOP_STATE.open_regular_path = old_open
        self.assertNotIn(str(new_target), calls)
        self.git("config", "--unset-all", "include.path")
        self.git("config", "include.path", str(link))
        new_regular = self.root.parent / "new-secret-regular.inc"; new_regular.write_text("[demo]\nvalue = never-read-new-regular\n")
        self.git("config", "--add", "include.path", str(new_regular))
        new_inode = os.lstat(new_regular).st_ino; seen_regular = False; old_read = LOOP_STATE.read_open_regular
        def no_new_regular(fd, before, budget):
            nonlocal seen_regular
            if before.st_ino == new_inode:
                seen_regular = True
                raise AssertionError("new regular include was opened")
            return old_read(fd, before, budget)
        LOOP_STATE.read_open_regular = no_new_regular
        try:
            with self.assertRaises(LOOP_STATE.Stop):
                LOOP_STATE.config_snapshot(str(self.root), self.common, LOOP_STATE.Budget(10000, 10_000_000, 1_000_000, 10), trusted)
        finally:
            LOOP_STATE.read_open_regular = old_read
        self.assertFalse(seen_regular)
        self.git("config", "--unset-all", "include.path")
        self.git("config", "include.path", str(link))
        first_target.write_text("[demo]\nvalue = changed\n")
        self.assertEqual(0, self.snap("link-content.json", secret_patterns_from="link-restored.json").returncode)
        self.assertEqual(1, self.compare("link-restored.json", "link-content.json").returncode)
        # A completed earlier run establishes a fresh config-link baseline while
        # retaining the old secret_paths union.
        os.unlink(link); os.symlink(second_target, link)
        self.assertEqual(0, self.snap("link-fresh.json", secret_patterns_from="link-content.json", fresh_config_baseline=True).returncode)

    def test_hasconfig_uses_later_local_remote_and_boolean_without_value_is_normal(self) -> None:
        # Git evaluates hasconfig against all ordinary sources, including a
        # repository remote declared after the global source.  A hand-written
        # boolean also has no value field in --null --list output.
        target = self.root.parent / "hasconfig.inc"; target.write_text("[demo]\nvalue = yes\n")
        global_config = self.root.parent / "global-config"
        global_config.write_text('[includeIf "hasconfig:remote.*.url:https://example.invalid/**"]\n\tpath = ' + str(target) + '\n')
        config = pathlib.Path(self.common) / "config"
        with config.open("a") as fh:
            fh.write("\n[selftest]\n\tboolean\n")
        self.git("config", "remote.origin.url", "https://example.invalid/repo")
        old_global = os.environ.get("GIT_CONFIG_GLOBAL"); old_system = os.environ.get("GIT_CONFIG_NOSYSTEM")
        os.environ["GIT_CONFIG_GLOBAL"] = str(global_config); os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        try:
            result = self.snap("hasconfig.json")
        finally:
            if old_global is None: os.environ.pop("GIT_CONFIG_GLOBAL", None)
            else: os.environ["GIT_CONFIG_GLOBAL"] = old_global
            if old_system is None: os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
            else: os.environ["GIT_CONFIG_NOSYSTEM"] = old_system
        self.assertEqual(0, result.returncode, result.stderr.decode())
        state = json.loads((self.root.parent / "hasconfig.json").read_text())
        origins = state["config"]["origins"]
        self.assertTrue(any(row["path"].endswith("hasconfig.inc") for row in origins))

    def test_limits_and_safe_saved_state_fail_closed(self) -> None:
        (self.root / "a").write_text("123456"); (self.root / "b").write_text("123456")
        self.assertEqual(20, self.snap("item.json", max_items=1, max_file_bytes=100).returncode)
        self.assertEqual(20, self.snap("bytes.json", max_bytes=10, max_file_bytes=100).returncode)
        self.assertEqual(20, self.snap("one.json", max_file_bytes=1).returncode)
        self.assertEqual(0, self.snap("raised.json", max_items=1000, max_bytes=100000, max_file_bytes=100000).returncode)
        fifo = self.root.parent / "state.fifo"; os.mkfifo(fifo)
        self.assertEqual(0, self.snap("good.json").returncode)
        bad = subprocess.run(["python3", str(SCRIPT), "compare", "--before", str(fifo), "--after", str(self.root.parent / "good.json")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        self.assertEqual(1, bad.returncode)

    def test_read_growth_and_lstat_open_replacement_fail_closed(self) -> None:
        target = self.root / "growth"; target.write_bytes(b"a" * 8)
        before = os.lstat(target); original_read = LOOP_STATE.os.read; changed = False
        def growing_read(fd, amount):
            nonlocal changed
            block = original_read(fd, amount)
            if block and not changed:
                changed = True
                with open(target, "ab") as fh: fh.write(b"b")
            return block
        LOOP_STATE.os.read = growing_read
        try:
            with self.assertRaises(LOOP_STATE.Stop):
                LOOP_STATE.safe_hash(str(target), before, LOOP_STATE.Budget(10, 1000, 1000, 10))
        finally:
            LOOP_STATE.os.read = original_read
        # A symlink substituted before open is rejected without following its
        # target; this is the same O_NOFOLLOW boundary used by profile/config.
        target.write_text("one"); before = os.lstat(target)
        os.unlink(target); os.symlink("tracked", target)
        with self.assertRaises(LOOP_STATE.Stop):
            LOOP_STATE.safe_hash(str(target), before, LOOP_STATE.Budget(10, 1000, 1000, 10))

    def test_profile_symlink_and_output_tmp_symlink_cannot_redirect_reads_or_writes(self) -> None:
        claude = self.root / ".claude"; claude.mkdir()
        outside = self.root.parent / "outside.yml"; outside.write_text("secret_paths: [x]\n")
        os.symlink(outside, claude / "project-profile.yml")
        failed = self.snap("profile.json")
        self.assertEqual(20, failed.returncode)
        os.unlink(claude / "project-profile.yml")
        # The former fixed `out.json.tmp` name is now irrelevant and cannot turn
        # the snapshot write into a write to this external file.
        sentinel = self.root.parent / "sentinel"; sentinel.write_text("keep")
        os.symlink(sentinel, self.root.parent / "out.json.tmp")
        self.assertEqual(0, self.snap("out.json").returncode)
        self.assertEqual("keep", sentinel.read_text())

    def test_self_commit_and_only_matching_remote_tracking_ref_are_allowed(self) -> None:
        own = self.root.parent / "own"
        self.git("worktree", "add", "-q", "--detach", str(own), "HEAD")
        self.git("worktree", "lock", "--reason", "dev-workflow-loop: task", str(own))
        before = self.snap("self-a.json", exclude_worktree=str(own)); self.assertEqual(0, before.returncode, before.stderr.decode())
        subprocess.run(["git", "-C", str(own), "checkout", "-qb", "task/demo"], check=True)
        (own / "tracked").write_text("commit")
        subprocess.run(["git", "-C", str(own), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(own), "commit", "-qm", "task"], check=True)
        oid = subprocess.run(["git", "-C", str(own), "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE).stdout.decode().strip()
        self.git("update-ref", "refs/remotes/origin/task/demo", oid)
        after = self.snap("self-b.json", exclude_worktree=str(own)); self.assertEqual(0, after.returncode, after.stderr.decode())
        result = subprocess.run(["python3", str(SCRIPT), "compare", "--before", str(self.root.parent / "self-a.json"), "--after", str(self.root.parent / "self-b.json"), "--self-worktree", str(own), "--self-ref", "refs/heads/task/demo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, result.returncode, result.stdout.decode() + result.stderr.decode())
        self.git("update-ref", "refs/remotes/origin/other", oid)
        self.assertEqual(0, self.snap("self-c.json", exclude_worktree=str(own)).returncode)
        result = subprocess.run(["python3", str(SCRIPT), "compare", "--before", str(self.root.parent / "self-a.json"), "--after", str(self.root.parent / "self-c.json"), "--self-worktree", str(own), "--self-ref", "refs/heads/task/demo"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(1, result.returncode)

    def test_unchanged_detached_self_worktree_is_a_normal_failed_iteration(self) -> None:
        own = self.root.parent / "own"
        self.git("worktree", "add", "-q", "--detach", str(own), "HEAD")
        self.git("worktree", "lock", "--reason", "dev-workflow-loop: task", str(own))
        self.assertEqual(0, self.snap("unchanged-a.json", exclude_worktree=str(own)).returncode)
        self.assertEqual(0, self.snap("unchanged-b.json", exclude_worktree=str(own)).returncode)
        result = subprocess.run(["python3", str(SCRIPT), "compare", "--before", str(self.root.parent / "unchanged-a.json"), "--after", str(self.root.parent / "unchanged-b.json"), "--self-worktree", str(own), "--self-ref", "refs/heads/task/missing"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, result.returncode, result.stdout.decode() + result.stderr.decode())


if __name__ == "__main__": unittest.main()
