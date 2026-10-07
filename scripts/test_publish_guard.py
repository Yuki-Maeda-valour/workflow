"""Scratch regressions for the exact-SHA publish boundary (Issue #190)."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/publish-guard.py"
DIGEST = ROOT / "plugins/dev-workflow/skills/ship-task/scripts/git-config-digest.py"
BODY_LIMIT = 1024 * 1024


class PublishGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.wt = self.root / "worktree"
        self.log = self.root / "gh.log"
        self.real_git = shutil.which("git")
        assert self.real_git
        self.git("init", "-q", "--bare", str(self.remote), cwd=self.root)
        self.git("init", "-q", str(self.wt), cwd=self.root)
        self.git("config", "user.name", "test", cwd=self.wt)
        self.git("config", "user.email", "test@example.invalid", cwd=self.wt)
        (self.wt / "normal.txt").write_text("normal\n", encoding="utf-8")
        self.git("add", "normal.txt", cwd=self.wt)
        self.git("commit", "-qm", "initial", cwd=self.wt)
        self.git("switch", "-q", "-c", "task/publish-test", cwd=self.wt)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.wt)
        self.sha = self.git("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
        (self.wt / ".claude/reviews").mkdir(parents=True)
        self.body = self.wt / ".claude/reviews/body.md"
        self.body.write_text("body\n", encoding="utf-8")
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        self.make_git_stub()
        self.make_gh_stub()
        # A repository hook and fsmonitor would mutate these markers if any
        # guard Git invocation were allowed to execute local configuration.
        self.hook_marker = self.root / "hook-ran"
        self.monitor_marker = self.root / "monitor-ran"
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text(f"#!/bin/sh\ntouch {self.hook_marker}\n", encoding="utf-8")
        (hooks / "pre-push").chmod(0o755)
        monitor = self.root / "monitor"
        monitor.write_text(
            f"#!/bin/sh\ntouch {self.monitor_marker}\nprintf '%s\\n' 2\nprintf '%s\\n' token\n",
            encoding="utf-8",
        )
        monitor.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks), cwd=self.wt)
        self.git("config", "core.fsmonitor", str(monitor), cwd=self.wt)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.real_git, *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True)

    def make_git_stub(self) -> None:
        # origin-repo.py must see the canonical public identity, while the
        # actual transport stays a bare local remote for this regression.
        path = self.fakebin / "git"
        path.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *' remote get-url --all origin '*|*' remote get-url --push --all origin '*)\n"
            "    if [ -n \"${PG_PUSH_ONLY:-}\" ]; then printf '%s\\n' \"${PG_PUSH_URLS:-${PG_PUSH_URL}}\"; else printf '%s\\n' https://github.com/o/r.git; fi; exit 0;;\n"
            "  *' push --dry-run --porcelain --no-follow-tags --recurse-submodules=no origin '*refs/heads/task/publish-test*)\n"
            "    if [ \"${PG_REMOTE_MODE:-}\" = bad ]; then printf ' \\t%s\\t%s\\n' \"${PG_REMOTE_BAD_SHA:-0000000000000000000000000000000000000000}:refs/heads/task/publish-test\" rejected; else printf '=\\t%s\\t[up to date]\\n' \"${PG_REMOTE_EXPECT_SHA}:refs/heads/task/publish-test\"; fi; exit 0;;\n"
            "esac\n"
            "case \" $* \" in *' cat-file -e '*)\n"
            "  if [ -n \"${PG_SWAP_SHA:-}\" ]; then " + self.real_git + " -C \"${PG_SWAP_WT}\" update-ref refs/heads/task/publish-test \"${PG_SWAP_SHA}\"; fi;; esac\n"
            f"exec {self.real_git} \"$@\"\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def make_gh_stub(self) -> None:
        path = self.root / "gh"
        payload = {
            "html_url": "https://github.com/o/r/pull/17", "number": 17,
            "head": {"ref": "task/publish-test", "sha": self.sha,
                     "repo": {"full_name": "o/r", "owner": {"login": "o"}}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}},
        }
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['PG_GH_LOG'], 'a', encoding='utf-8') as f: f.write(' '.join(sys.argv[1:])+'\\n')\n"
            "if sys.argv[1:3] == ['pr', 'create']:\n"
            " print('https://github.com/o/r/pull/17'); raise SystemExit(0)\n"
            "if sys.argv[1] == 'api' and sys.argv[2:4] == ['--hostname', 'github.com'] and sys.argv[4] == 'repos/o/r/pulls/17':\n"
            f" value = {json.dumps(payload)}\n"
            " if os.environ.get('PG_GH_MODE') == 'bad-head': value['head']['sha'] = '0'*40\n"
            " if os.environ.get('PG_GH_MODE') == 'bad-repo': value['head']['repo']['full_name'] = 'evil/r'\n"
            " print(json.dumps(value)); raise SystemExit(0)\n"
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        self.gh = path

    def digest(self, *, extra_env: dict[str, str] | None = None) -> str:
        return subprocess.run(["python3", str(DIGEST), "--dir", str(self.wt)], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                              env={**os.environ, **(extra_env or {})}).stdout.strip()

    def push_url_digest(self, *, extra_env: dict[str, str] | None = None) -> str:
        url = subprocess.run([self.real_git, "remote", "get-url", "--push", "origin"], cwd=self.wt,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                             env={**os.environ, **(extra_env or {})}).stdout.strip()
        return "sha256:" + hashlib.sha256(url.encode("utf-8", "surrogateescape")).hexdigest()

    def guard(self, *, sha: str | None = None, digest: str | None = None, mode: str = "",
              remote_mode: str = "", swap_sha: str = "", upstream: bool = False, unattended: bool = False,
              push_only: bool = False, push_url_digest: str | None = None, stub_push_url: str | None = None,
              stub_push_urls: str | None = None, use_git_stub: bool = True,
              extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PATH": f"{self.fakebin}:{os.environ['PATH']}" if use_git_stub else os.environ['PATH'], "PG_GH_LOG": str(self.log),
               "PG_GH_MODE": mode, "PG_REMOTE_MODE": remote_mode, "GH_REPO": "evil/default",
               "PG_SWAP_SHA": swap_sha, "PG_SWAP_WT": str(self.wt), "PG_REMOTE_EXPECT_SHA": sha or self.sha}
        if push_only:
            env["PG_PUSH_ONLY"] = "1"
            env["PG_PUSH_URL"] = stub_push_url or self.git("remote", "get-url", "--push", "origin", cwd=self.wt).stdout.strip()
            if stub_push_urls is not None:
                env["PG_PUSH_URLS"] = stub_push_urls
        if unattended:
            env["DEV_WORKFLOW_LOOP_ITER"] = "loop-1"
        env.update(extra_env or {})
        argv = ["python3", str(GUARD), "--dir", str(self.wt), "--sha", sha or self.sha,
             "--branch", "task/publish-test", "--config-digest", digest or self.digest(), "--gh", str(self.gh)]
        if push_only:
            argv += ["--push-only", "--push-url-digest", push_url_digest or self.push_url_digest()]
        else:
            argv += ["--repo", "github.com/o/r", "--base", "main",
                     "--body-file", str(self.body.relative_to(self.wt)), "--title", "title"]
        if upstream:
            argv.append("--set-upstream")
        return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def test_bare_remote_normal_exact_sha_and_no_local_helpers(self) -> None:
        result = self.guard()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("https://github.com/o/r/pull/17\n", result.stdout)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=self.remote).stdout.strip())
        self.assertFalse(self.hook_marker.exists())
        self.assertFalse(self.monitor_marker.exists())
        self.assertEqual(["pr create -R github.com/o/r --base main --head task/publish-test --title title --body-file -",
                          "api --hostname github.com repos/o/r/pulls/17"],
                         self.log.read_text(encoding="utf-8").splitlines())

    def test_config_or_origin_change_stops_before_pr(self) -> None:
        expected = self.digest()
        self.git("config", "remote.origin.pushurl", "https://github.com/evil/r.git", cwd=self.wt)
        result = self.guard(digest=expected)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists(), result.stderr)

    def test_held_sha_is_the_only_sent_source(self) -> None:
        extra = self.wt / "later.txt"
        extra.write_text("not reviewed\n", encoding="utf-8")
        self.git("add", "later.txt", cwd=self.wt)
        self.git("commit", "-qm", "later", cwd=self.wt)
        result = self.guard()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists())
        no_ref = subprocess.run([self.real_git, "show-ref"], cwd=self.remote, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual("", no_ref.stdout)

    def test_branch_swap_after_sha_check_still_sends_held_sha(self) -> None:
        (self.wt / "later.txt").write_text("later\n", encoding="utf-8")
        self.git("add", "later.txt", cwd=self.wt)
        self.git("commit", "-qm", "later", cwd=self.wt)
        later = self.git("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
        self.git("update-ref", "refs/heads/task/publish-test", self.sha, cwd=self.wt)
        result = self.guard(swap_sha=later)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=self.remote).stdout.strip())

    def test_mismatched_pr_is_reported_without_close(self) -> None:
        result = self.guard(mode="bad-head")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("https://github.com/o/r/pull/17", result.stderr)
        calls = self.log.read_text(encoding="utf-8")
        self.assertIn("pr create", calls)
        self.assertIn("api --hostname github.com", calls)
        self.assertNotIn("close", calls)

    def test_fake_remote_sha_stops_before_pr(self) -> None:
        result = self.guard(remote_mode="bad")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists())

    def test_fake_pr_repository_is_rejected_without_close(self) -> None:
        result = self.guard(mode="bad-repo")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("https://github.com/o/r/pull/17", result.stderr)
        self.assertNotIn("close", self.log.read_text(encoding="utf-8"))

    def test_push_only_local_remote_needs_no_gh_or_pr(self) -> None:
        result = self.guard(push_only=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("PUSH_ONLY\n", result.stdout)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=self.remote).stdout.strip())
        self.assertFalse(self.log.exists(), result.stderr)
        self.assertFalse(self.hook_marker.exists())
        self.assertFalse(self.monitor_marker.exists())

    def test_push_only_rejects_tracking_before_send(self) -> None:
        result = self.guard(push_only=True, upstream=True)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists())
        self.assertEqual("", subprocess.run([self.real_git, "show-ref"], cwd=self.remote, text=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout)

    def test_push_only_url_change_multiple_url_and_remote_mismatch_stop_without_gh(self) -> None:
        held = self.push_url_digest()
        secret = "https://user:secret-value@example.invalid/other.git"
        self.git("remote", "set-url", "origin", secret, cwd=self.wt)
        changed = self.guard(push_only=True, push_url_digest=held)
        self.assertNotEqual(0, changed.returncode)
        self.assertNotIn("secret-value", changed.stderr)
        self.assertFalse(self.log.exists())
        self.git("remote", "set-url", "origin", str(self.remote), cwd=self.wt)
        self.git("config", "--add", "remote.origin.pushurl", str(self.remote), cwd=self.wt)
        self.git("config", "--add", "remote.origin.pushurl", str(self.root / "second.git"), cwd=self.wt)
        multiple = self.guard(push_only=True, push_url_digest=held)
        self.assertNotEqual(0, multiple.returncode)
        self.assertFalse(self.log.exists())
        # The two-url case must be removed before the remote-mismatch case:
        # otherwise Git rejects the real push before dry-run porcelain runs.
        self.git("config", "--unset-all", "remote.origin.pushurl", cwd=self.wt)
        mismatch = self.guard(push_only=True, remote_mode="bad")
        self.assertNotEqual(0, mismatch.returncode)
        self.assertIn("送信後の remote branch", mismatch.stderr)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=self.remote).stdout.strip())
        self.assertFalse(self.log.exists())

    def test_push_only_branch_swap_still_sends_held_sha(self) -> None:
        (self.wt / "later.txt").write_text("later\n", encoding="utf-8")
        self.git("add", "later.txt", cwd=self.wt)
        self.git("commit", "-qm", "later", cwd=self.wt)
        later = self.git("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
        self.git("update-ref", "refs/heads/task/publish-test", self.sha, cwd=self.wt)
        result = self.guard(push_only=True, swap_sha=later)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=self.remote).stdout.strip())
        self.assertFalse(self.log.exists())

    def test_push_only_uses_origin_once_for_chained_instead_of(self) -> None:
        first = self.root / "first.git"
        second = self.root / "second.git"
        self.git("init", "-q", "--bare", str(first), cwd=self.root)
        self.git("init", "-q", "--bare", str(second), cwd=self.root)
        self.git("config", "remote.origin.url", "alias:", cwd=self.wt)
        self.git("config", "--add", f"url.{first}.insteadOf", "alias:", cwd=self.wt)
        self.git("config", "--add", f"url.{second}.insteadOf", str(first), cwd=self.wt)
        held = "sha256:" + hashlib.sha256(str(first).encode()).hexdigest()
        result = self.guard(push_only=True, push_url_digest=held, use_git_stub=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=first).stdout.strip())
        self.assertEqual("", subprocess.run([self.real_git, "show-ref"], cwd=second, text=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout)

    def test_push_only_preserves_explicit_pushurl_before_push_instead_of(self) -> None:
        first = self.root / "explicit-first.git"
        second = self.root / "explicit-second.git"
        self.git("init", "-q", "--bare", str(first), cwd=self.root)
        self.git("init", "-q", "--bare", str(second), cwd=self.root)
        self.git("config", "remote.origin.url", str(first), cwd=self.wt)
        self.git("config", "remote.origin.pushurl", str(first), cwd=self.wt)
        self.git("config", "--add", f"url.{second}.pushInsteadOf", str(first), cwd=self.wt)
        held = "sha256:" + hashlib.sha256(str(first).encode()).hexdigest()
        result = self.guard(push_only=True, push_url_digest=held, use_git_stub=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.sha, self.git("rev-parse", "refs/heads/task/publish-test", cwd=first).stdout.strip())
        self.assertEqual("", subprocess.run([self.real_git, "show-ref"], cwd=second, text=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout)

    def test_push_only_global_rewrite_changes_effective_digest_while_local_digest_is_unchanged(self) -> None:
        first = self.root / "global-first.git"
        second = self.root / "global-second.git"
        self.git("init", "-q", "--bare", str(first), cwd=self.root)
        self.git("init", "-q", "--bare", str(second), cwd=self.root)
        self.git("config", "remote.origin.url", "alias:", cwd=self.wt)
        global_config = self.root / "global.gitconfig"
        changed_env = {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"}
        global_config.write_text(f'[url "{first}"]\n\tinsteadOf = alias:\n', encoding="utf-8")
        held_url = self.push_url_digest(extra_env=changed_env)
        held_config = self.digest(extra_env=changed_env)
        global_config.write_text(f'[url "{second}"]\n\tinsteadOf = alias:\n', encoding="utf-8")
        self.assertEqual(held_config, self.digest(extra_env=changed_env))
        result = self.guard(push_only=True, push_url_digest=held_url, digest=held_config,
                            use_git_stub=False, extra_env=changed_env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("push URL", result.stderr)
        for remote in (first, second):
            self.assertEqual("", subprocess.run([self.real_git, "show-ref"], cwd=remote, text=True,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout)

    def test_body_symlink_is_rejected_before_pr_creation(self) -> None:
        linked = self.wt / ".claude/reviews/body-link.md"
        linked.symlink_to(self.body.name)
        self.body = linked
        result = self.guard()
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.log.exists())

    def test_body_limit_is_inclusive_and_overflow_never_creates_pr(self) -> None:
        self.body.write_bytes(b"x" * BODY_LIMIT)
        self.assertEqual(0, self.guard().returncode)
        self.log.unlink()
        self.body.write_bytes(b"x" * (BODY_LIMIT + 1))
        self.assertNotEqual(0, self.guard().returncode)
        self.assertFalse(self.log.exists())

    def test_fifo_and_parent_symlink_are_rejected_before_pr(self) -> None:
        self.body.unlink()
        os.mkfifo(self.body)
        self.assertNotEqual(0, self.guard().returncode)
        self.assertFalse(self.log.exists())

    def test_interactive_tracking_is_after_publish_and_unattended_rejects_it(self) -> None:
        result = self.guard(upstream=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("origin", self.git("config", "--get", "branch.task/publish-test.remote", cwd=self.wt).stdout.strip())
        self.log.unlink()
        result = self.guard(upstream=True, unattended=True)
        self.assertNotEqual(0, result.returncode)

    def test_real_git_markers_have_positive_and_safe_negative_controls(self) -> None:
        marker = self.root / "marker"
        tool = self.root / "marker-tool"
        tool.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        tool.chmod(0o755)
        hooks = self.root / "hooks"
        (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        (hooks / "pre-commit").chmod(0o755)
        self.git("config", "filter.pwn.clean", str(tool), cwd=self.wt)
        (self.wt / ".gitattributes").write_text("marked.txt filter=pwn\n", encoding="utf-8")
        self.git("add", ".gitattributes", cwd=self.wt)
        self.git("commit", "-qm", "attrs", cwd=self.wt)
        self.git("config", "commit.gpgSign", "true", cwd=self.wt)
        self.git("config", "gpg.program", str(tool), cwd=self.wt)
        marker.unlink(missing_ok=True)
        (self.wt / "marked.txt").write_text("x", encoding="utf-8")
        self.git("add", "marked.txt", cwd=self.wt)
        self.assertTrue(marker.exists(), "unsafe clean filter positive control")
        self.git("reset", "--", "marked.txt", cwd=self.wt)
        marker.unlink()
        safe = ["--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false", "-c", "filter.pwn.clean="]
        self.git(*safe, "add", "marked.txt", cwd=self.wt)
        self.git(*safe, "commit", "-qm", "safe", cwd=self.wt)
        self.git(*safe, "push", "--no-follow-tags", "--recurse-submodules=no", "origin", "HEAD:refs/heads/task/marker", cwd=self.wt)
        self.assertFalse(marker.exists())
        self.body.unlink()
        review = self.wt / ".claude/reviews"
        moved = self.wt / "reviews-real"
        review.rename(moved)
        review.symlink_to(moved.name)
        (moved / "body.md").write_text("body", encoding="utf-8")
        self.body = review / "body.md"
        self.assertNotEqual(0, self.guard().returncode)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
