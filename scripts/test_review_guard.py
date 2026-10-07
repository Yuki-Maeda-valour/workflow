"""review-guard.py の review/commit 照合の回帰テスト。"""

import json
import hashlib
import importlib.util
from unittest import mock
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "plugins/dev-workflow/skills/ship-task/scripts/review-guard.py"


class ReviewGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "tester")
        self.git("config", "user.email", "tester@example.invalid")
        (self.repo / "source.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "tasks").mkdir()
        self.task = self.repo / "tasks/進行中_guard.md"
        self.task.write_text("# task\n\n- [ ] work\n\n## 追加修正記録\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.state = self.root / "state"
        self.start_state = self.root / "start"
        self.review = self.root / "review.md"
        self.review.write_text("review input\n", encoding="utf-8")
        self.state_sha256 = ""
        self.start_sha256 = ""

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def guard(self, *args, expect=0):
        if getattr(self, "task_root_override", None):
            args = (*args, "--task-root", str(self.task_root_override))
        if getattr(self, "accept", None):
            args = (*args, "--accept", self.accept)
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(expect, proc.returncode, proc.stdout + proc.stderr)
        if expect == 2:
            self.assertIn("ERROR:", proc.stderr)
            self.assertNotIn("usage:", proc.stderr)
        return proc

    def begin(self):
        if self.start_state.exists() or self.start_state.is_symlink():
            shutil.rmtree(self.start_state)
        if self.state.exists() or self.state.is_symlink():
            shutil.rmtree(self.state)
        started = self.guard("start", "--cwd", str(self.repo), "--state", str(self.start_state),
                             "--task-md", str(self.task), "--exclude-ere", r"(^|/)[.]env($|[.])")
        self.start_sha256 = next(line.split("=", 1)[1] for line in started.stdout.splitlines()
                                 if line.startswith("START_SHA256="))
        return started

    def capture(self, phase="implementation", prove=True):
        result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state),
                          "--task-md", str(self.task),
                          "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                          "--exclude-ere", r"(^|/)[.]env($|[.])", "--phase", phase)
        self.state_sha256 = next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        if phase == "hold":
            return result
        sealed = self.guard("seal", "--cwd", str(self.repo), "--state", str(self.state),
                            "--expect-state-sha256", self.state_sha256,
                            "--review-input", str(self.review),
                            "--exclude-ere", r"(^|/)[.]env($|[.])")
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        if prove:
            self.prove()
        return sealed

    def take(self):
        self.begin()
        return self.capture()

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def context(self, command):
        return [command, "--cwd", str(self.repo), "--state", str(self.state),
                "--expect-state-sha256", self.state_sha256, "--exclude-ere", r"(^|/)[.]env($|[.])"]

    def prove(self):
        plan = self.root / "checks-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "normal", "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('source.txt').exists()"]}]}))
        checks = self.root / "checks.json"
        checks.unlink(missing_ok=True)
        self.guard(*self.context("run-checks"), "--checks-file", str(plan), "--expect-checks-sha256", self.digest(plan), "--out", str(checks))
        manifest = json.loads((self.state / "manifest.json").read_text())
        reply = self.root / "reply.json"
        reply.write_text(json.dumps({"reviewer": "reviewer", "verdict": "APPROVED", "issues": [], "review_binding_sha256": manifest["review_binding_sha256"]}))
        self.proof = self.root / "evidence.json"
        self.proof.unlink(missing_ok=True)
        self.guard(*self.context("attest"), "--checks", str(checks), "--expect-checks-sha256", self.digest(checks),
                   "--review-result", str(reply), "--expect-review-sha256", self.digest(reply), "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)

    def complete(self, mode="complete"):
        expected = self.root / "expected-completed.md"
        expected.unlink(missing_ok=True)
        self.guard(*self.context("finalize"), "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash,
                   "--transition", mode, "--out", str(expected))
        self.expected_hash = self.digest(expected)
        completed = self.task.with_name(("完了_" if mode == "complete" else "保留_") + "guard.md")
        completed.write_bytes(expected.read_bytes())
        self.task.unlink()
        return completed, expected

    def verify(self, *args, expect=0):
        extra = ["--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash]
        if "--expected-task" in args:
            extra += ["--expect-task-sha256", self.expected_hash]
        return self.guard("verify", "--cwd", str(self.repo), "--state", str(self.state),
                          "--expect-state-sha256", self.state_sha256,
                          "--exclude-ere", r"(^|/)[.]env($|[.])", *extra, *args,
                          expect=expect)

    def test_clean_state_is_verified_and_review_binding_is_reported(self):
        out = self.take().stdout
        self.assertIn("STATE_SHA256=", out)
        self.assertIn("REVIEW_BINDING_SHA256=", out)
        self.verify()

    def test_seal_rejects_change_during_snapshot_generation(self):
        self.take()
        sha = self.state_sha256
        shutil.rmtree(self.state)
        started = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                             "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                             "--exclude-ere", r"(^|/)[.]env($|[.])")
        sha = next(line.split("=", 1)[1] for line in started.stdout.splitlines() if line.startswith("STATE_SHA256="))
        (self.repo / "source.txt").write_text("changed while snapshot\n", encoding="utf-8")
        self.guard("seal", "--cwd", str(self.repo), "--state", str(self.state),
                   "--expect-state-sha256", sha, "--review-input", str(self.review),
                   "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)

    def test_state_inside_reviews_does_not_change_review_set(self):
        reviews = self.repo / ".claude/reviews"
        reviews.mkdir(parents=True)
        self.state = reviews / "state"
        self.start_state = reviews / "start"
        self.take()
        state = self.state
        sha = self.state_sha256
        sealed = self.guard("seal", "--cwd", str(self.repo), "--state", str(state),
                            "--expect-state-sha256", sha, "--review-input", str(self.review),
                            "--exclude-ere", r"(^|/)[.]env($|[.])")
        sha = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines() if line.startswith("STATE_SHA256="))
        self.guard("verify", "--cwd", str(self.repo), "--state", str(state), "--expect-state-sha256", sha,
                   "--exclude-ere", r"(^|/)[.]env($|[.])")

    def capture_ignored_reviews(self, *extra_ignores):
        reviews = self.repo / ".claude/reviews"
        reviews.mkdir(parents=True, exist_ok=True)
        (self.repo / ".gitignore").write_text("\n".join((".claude/reviews/", *extra_ignores)) + "\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore review records")
        self.state = reviews / "state"
        self.start_state = reviews / "start"
        self.review = reviews / "snapshot.md"
        self.begin()
        taken = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state),
                           "--task-md", str(self.task), "--start-state", str(self.start_state),
                           "--expect-start-sha256", self.start_sha256,
                           "--exclude-ere", r"(^|/)[.]env($|[.])")
        self.state_sha256 = next(line.split("=", 1)[1] for line in taken.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        # take 自身の manifest と、その後に生成する snapshot が seal を妨げないこと。
        self.review.write_text("reviewed snapshot\n", encoding="utf-8")
        sealed = self.guard(*self.context("seal"), "--review-input", str(self.review))
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        self.guard(*self.context("verify"))
        return reviews

    def test_ignored_reviews_accept_generated_state_snapshot_and_later_records(self):
        reviews = self.capture_ignored_reviews()
        (reviews / "review-result.json").write_text('{"verdict": "APPROVED"}\n')
        (reviews / "fresh-issue-body.md").write_bytes(self.task.read_bytes())
        self.guard(*self.context("verify"))
        # 正規の検証根拠を取得し、記録追加後も stage の照合まで通す。
        self.prove()
        self.git("add", "-A")
        self.verify("--mode", "stage")

    def test_ignored_reviews_still_reject_modified_sealed_review_input(self):
        self.capture_ignored_reviews()
        self.review.write_text("changed snapshot\n", encoding="utf-8")
        self.guard(*self.context("verify"), expect=1)

    def test_ignored_reviews_still_reject_new_ignored_file_outside_reviews(self):
        generated = self.repo / "generated"
        generated.mkdir()
        self.capture_ignored_reviews("generated/")
        (generated / "output.txt").write_text("new ignored output\n")
        self.guard(*self.context("verify"), expect=1)

    def test_ignored_reviews_still_reject_changed_ignore_rules(self):
        self.capture_ignored_reviews()
        ignore = self.repo / ".gitignore"
        ignore.write_text(ignore.read_text() + "source.txt\n")
        self.guard(*self.context("verify"), expect=1)

    def test_ignored_reviews_still_reject_similarly_named_paths(self):
        names = (".claude/reviews-other/output.txt", ".claude/reviews.md", "other.claude/reviews/output.txt")
        # 親ディレクトリの追加だけで拒否される対照にしない。
        for name in names:
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        self.capture_ignored_reviews(*names)
        for name in names:
            with self.subTest(path=name):
                path = self.repo / name
                path.write_text("new ignored file\n")
                self.guard(*self.context("verify"), expect=1)
                path.unlink()
                self.guard(*self.context("verify"))

    def test_tracked_untracked_index_and_commit_changes_are_rejected(self):
        cases = (
            ("tracked", lambda: (self.repo / "source.txt").write_text("changed\n", encoding="utf-8")),
            ("untracked", lambda: (self.repo / "new.txt").write_text("new\n", encoding="utf-8")),
            ("index", lambda: self.git("update-index", "--chmod=+x", "source.txt")),
            ("commit", lambda: (self.git("commit", "--allow-empty", "-qm", "later"))),
        )
        for name, change in cases:
            with self.subTest(name=name):
                self.take()
                change()
                self.verify(expect=1)
                shutil.rmtree(self.state)
                self.git("reset", "--hard", "HEAD~1" if name == "commit" else "HEAD")
                for path in (self.repo / "new.txt",):
                    path.unlink(missing_ok=True)

    def test_mode_and_same_size_mtime_change_are_rejected(self):
        self.take()
        source = self.repo / "source.txt"
        old = source.stat()
        source.write_text("next\n", encoding="utf-8")
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.verify(expect=1)
        shutil.rmtree(self.state)
        self.git("checkout", "--", "source.txt")
        self.take()
        source.chmod(source.stat().st_mode | stat.S_IXUSR)
        self.verify(expect=1)

    def test_unexpected_task_state_files_and_task_edit_are_rejected(self):
        self.take()
        (self.repo / "tasks/中断_other.md").write_text("other\n", encoding="utf-8")
        self.verify(expect=1)
        shutil.rmtree(self.state)
        (self.repo / "tasks/中断_other.md").unlink()
        self.take()
        self.task.write_text("# forged\n", encoding="utf-8")
        self.verify(expect=1)

    def test_review_input_and_secret_path_metadata_are_checked_without_reading_secret(self):
        secret = self.repo / ".env.hidden"
        secret.write_text("DO_NOT_READ\n", encoding="utf-8")
        # secret は hash を作らず、path/mode/kind だけを manifest に保持する。
        self.take()
        manifest = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
        hidden = next(item for item in manifest["excluded_worktree"] if item["path"] == ".env.hidden")
        self.assertEqual({"path", "mode", "kind"}, set(hidden))
        self.review.write_text("replaced\n", encoding="utf-8")
        self.verify(expect=1)

    def test_ere_is_gnu_case_insensitive_and_accepts_newline_path(self):
        secret = self.repo / ".ENV.UPPER\nname"
        secret.write_text("DO_NOT_READ\n", encoding="utf-8")
        self.take()
        manifest = json.loads((self.state / "manifest.json").read_text(encoding="utf-8"))
        paths = {item["path"] for item in manifest["excluded_worktree"]}
        self.assertIn(".ENV.UPPER\nname", paths)

    def test_take_does_not_rewrite_index_and_stage_matches_reviewed_tree(self):
        index = self.repo / ".git/index"
        before = index.read_bytes()
        self.take()
        self.assertEqual(before, index.read_bytes())
        self.git("add", "-A")
        self.verify("--mode", "stage")

    def test_expected_completion_commit_matches_tree(self):
        self.take()
        completed, expected = self.complete()
        self.git("add", "-A")
        self.verify("--mode", "stage", "--task-transition", "complete", "--expected-task", str(expected))
        self.git("commit", "-qm", "complete")
        self.verify("--mode", "commit", "--task-transition", "complete", "--expected-task", str(expected))

    def test_exact_expected_completion_transition_is_allowed(self):
        self.take()
        completed, expected = self.complete()
        self.verify("--task-transition", "complete", "--expected-task", str(expected))
        completed.write_text("# task\n\n- [ ] work\n\n## final\nforged\n", encoding="utf-8")
        self.verify("--task-transition", "complete", "--expected-task", str(expected), expect=1)

    def test_completion_transition_cannot_hide_unreviewed_source_change(self):
        self.take()
        completed, expected = self.complete()
        (self.repo / "source.txt").write_text("UNREVIEWED\n", encoding="utf-8")
        self.verify("--task-transition", "complete", "--expected-task", str(expected), expect=1)

    def test_commit_cannot_hide_unreviewed_blob_by_restoring_worktree(self):
        self.take()
        completed, expected = self.complete()
        (self.repo / "source.txt").write_text("UNREVIEWED\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "forged")
        (self.repo / "source.txt").write_text("base\n", encoding="utf-8")
        self.verify("--mode", "commit", "--task-transition", "complete", "--expected-task", str(expected), expect=1)

    def test_secret_index_change_is_rejected_without_reading_secret_contents(self):
        secret = self.repo / ".env.tracked"
        secret.write_text("ORIGINAL\n", encoding="utf-8")
        self.git("add", ".env.tracked")
        self.git("commit", "-qm", "secret base")
        self.take()
        secret.write_text("FORGED\n", encoding="utf-8")
        self.git("add", ".env.tracked")
        self.verify("--mode", "stage", expect=1)

    def test_state_tampering_and_unsafe_state_are_rejected(self):
        self.take()
        manifest = self.state / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["review_sha256"] = "0" * 64
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.verify(expect=2)
        shutil.rmtree(self.state)
        os.symlink(self.root / "missing", self.state)
        self.guard("take", "--cwd", str(self.repo), "--state", str(self.state),
                   "--task-md", str(self.task), "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                   "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)

    def test_fifo_and_task_symlink_are_not_accepted(self):
        pipe = self.repo / "pipe"
        os.mkfifo(pipe)
        self.guard("start", "--cwd", str(self.repo), "--state", str(self.start_state),
                   "--task-md", str(self.task),
                   "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
        pipe.unlink()
        real = self.repo / "tasks/real.md"
        real.write_text("task\n", encoding="utf-8")
        self.task.unlink()
        os.symlink(real.name, self.task)
        self.guard("start", "--cwd", str(self.repo), "--state", str(self.start_state),
                   "--task-md", str(self.task),
                   "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)

    def test_preexisting_untracked_is_preserved_not_committed(self):
        personal = self.repo / "personal.txt"
        personal.write_text("private unrelated")
        self.begin()
        (self.repo / "source.txt").write_text("reviewed change")
        (self.repo / "new.txt").write_text("new reviewed file")
        self.capture()
        self.git("add", "source.txt", "new.txt")
        self.verify("--mode", "stage")
        self.git("add", "personal.txt")
        self.verify("--mode", "stage", expect=1)
        self.git("reset", "HEAD", "personal.txt")
        self.git("commit", "-qm", "reviewed")
        self.verify("--mode", "commit")
        self.assertEqual("private unrelated", personal.read_text())

    def test_doc_no_rename_add_delete_rename_and_mode_normal_flow(self):
        completed = self.task.with_name("完了_guard.md")
        self.task.rename(completed)
        self.task = completed
        (self.repo / "old.md").write_text("delete me")
        (self.repo / "rename.md").write_text("rename me")
        self.git("add", "-A")
        self.git("commit", "-qm", "completed base")
        self.begin()
        (self.repo / "old.md").unlink()
        (self.repo / "rename.md").rename(self.repo / "renamed.md")
        (self.repo / "new.md").write_text("new docs")
        (self.repo / "source.txt").chmod(0o755)
        self.capture(phase="doc")
        self.verify()
        self.git("add", "-A")
        self.verify("--mode", "stage")
        self.git("commit", "-qm", "docs")
        self.verify("--mode", "commit")
        pr = self.guard(*self.context("pr-evidence"), "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash)
        self.assertIn("APPROVED", pr.stdout)

    def test_unreviewed_hold_has_separate_evidence_and_normal_commit(self):
        self.begin()
        (self.repo / "source.txt").write_text("unfinished work")
        self.capture(phase="hold")
        self.proof = self.root / "hold-evidence.json"
        self.guard(*self.context("attest"), "--hold-reason", "D1 needs decision", "--hold-next", "要件を確認する", "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)
        self.assertEqual("UNAPPROVED", json.loads(self.proof.read_text())["verdict"])
        completed, expected = self.complete("pending")
        self.verify("--task-transition", "pending", "--expected-task", str(expected))
        self.git("add", "-A")
        self.verify("--mode", "stage", "--task-transition", "pending", "--expected-task", str(expected))
        self.git("commit", "-qm", "hold")
        self.verify("--mode", "commit", "--task-transition", "pending", "--expected-task", str(expected))
        self.assertIn("UNAPPROVED", completed.read_text())

    def test_other_state_tasks_are_protected_from_start_before_take(self):
        other = self.repo / "tasks/中断_other.md"
        other.write_text("other")
        self.begin()
        for operation in ("edit", "delete", "rename", "add"):
            with self.subTest(operation=operation):
                if operation == "edit":
                    other.write_text("forged")
                elif operation == "delete":
                    other.unlink()
                elif operation == "rename":
                    other.rename(other.with_name("保留_other.md"))
                else:
                    other.with_name("候補_extra.md").write_text("new")
                result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                                   "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                                   "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
                self.assertIn("対象外の状態名", result.stderr)
                for path in self.task.parent.glob("*other.md"):
                    path.unlink()
                other.with_name("候補_extra.md").unlink(missing_ok=True)
                other.write_text("other")

    def test_task_body_or_expected_task_replacement_is_rejected(self):
        self.begin()
        original = self.task.read_text()
        self.task.write_text(original.replace("work", "different scope"))
        result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                           "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                           "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
        self.assertIn("本文 digest", result.stderr)
        self.task.write_text(original)
        self.capture()
        completed, expected = self.complete()
        expected.write_text(expected.read_text().replace("work", "forged scope"))
        completed.write_bytes(expected.read_bytes())
        self.expected_hash = self.digest(expected)
        self.git("add", "-A")
        result = self.verify("--mode", "stage", "--task-transition", "complete", "--expected-task", str(expected), expect=2)
        self.assertIn("正規変換", result.stderr)

    def test_seal_second_observation_cannot_adopt_new_source(self):
        self.begin()
        self.capture(prove=False)
        spec = importlib.util.spec_from_file_location("review_guard_race", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.current
        count = 0
        def racing(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                (self.repo / "source.txt").write_text("unreviewed racing change")
            return original(*args, **kwargs)
        args = module.parser().parse_args(self.context("seal") + ["--review-input", str(self.review)])
        with mock.patch.object(module, "current", side_effect=racing):
            with self.assertRaisesRegex(module.GuardError, "seal の収集中"):
                module.seal(args)

    def test_ignore_all_sources_quality_disclosure_and_clean_checkout(self):
        global_ignore = self.root / "global-ignore"
        global_ignore.write_text("global-old\n")
        self.git("config", "core.excludesFile", str(global_ignore))
        pre = subprocess.run(["bash", str(SCRIPT.parents[2] / "do-task/scripts/diff-snapshot.sh"), "--cwd", str(self.repo), "--precheck"], capture_output=True, text=True)
        self.accept = re.search(r"承認ダイジェスト: ([0-9a-f]{64})", pre.stderr).group(1)
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_existing.py").write_text("assert True\n")
        self.git("add", "tests")
        self.git("commit", "-qm", "test base")
        self.begin()
        (self.repo / ".gitignore").write_text("generated/\n")
        (self.repo / ".git/info/exclude").write_text("local-hidden\n")
        global_ignore.write_text("global-old\nglobal-hidden\n")
        (self.repo / "generated").mkdir()
        (self.repo / "generated/output").write_text("build output")
        (self.repo / "local-hidden").write_text("hidden")
        (self.repo / "global-hidden").write_text("hidden")
        (tests / "test_existing.py").write_text("# skip all\n")
        (tests / "test_new.py").write_text("assert 2+2 == 4\n")
        self.capture(prove=False)
        manifest = json.loads((self.state / "manifest.json").read_text())
        report = manifest["disclosures"]
        self.assertEqual(3, len(report["ignore_changes"]))
        self.assertEqual({"generated/output", "local-hidden", "global-hidden"}, set(report["new_ignored_paths"]))
        self.assertEqual({"tests/test_existing.py", "tests/test_new.py"}, {item["path"] for item in report["quality_changes"]})
        plan = self.root / "clean-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "clean", "argv": [sys.executable, "-c", "from pathlib import Path; assert not Path('generated').exists(); assert not Path('local-hidden').exists(); assert not Path('global-hidden').exists(); exec(Path('tests/test_new.py').read_text())"]}]}))
        receipt = self.root / "clean-checks.json"
        self.guard(*self.context("run-checks"), "--checks-file", str(plan), "--expect-checks-sha256", self.digest(plan), "--out", str(receipt))
        self.assertTrue(json.loads(receipt.read_text())["clean_checkout"])
        self.prove()
        self.git("add", "-A")
        self.verify("--mode", "stage")
        self.git("commit", "-qm", "ignore and test changes")
        self.verify("--mode", "commit")
        pr = self.guard(*self.context("pr-evidence"), "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash)
        self.assertIn("test_existing.py", pr.stdout)
        self.assertIn("global-hidden", pr.stdout)

    def test_secret_quality_path_is_rejected_before_open(self):
        secret = self.repo / ".env.quality"
        secret.write_text("secret")
        self.begin()
        result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                           "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                           "--quality-path", str(secret), "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
        self.assertIn("機密除外が 品質入口", result.stderr)

    def test_forged_log_cannot_supply_review_or_check_results(self):
        self.take()
        forged = self.root / "forged.json"
        forged.write_text(json.dumps({"verdict": "APPROVED", "record": "実施済"}))
        result = self.guard(*self.context("attest"), "--checks", str(forged), "--expect-checks-sha256", self.digest(forged),
                           "--review-result", str(forged), "--expect-review-sha256", self.digest(forged),
                           "--out", str(self.root / "bad-proof"), expect=2)
        self.assertIn("review 返答", result.stderr)
        proof = json.loads(self.proof.read_text())
        proof["verdict"] = "FORGED"
        self.proof.write_text(json.dumps(proof))
        self.git("add", "-A")
        result = self.verify("--mode", "stage", expect=2)
        self.assertIn("外部保持 sha256", result.stderr)

    def test_failed_real_check_cannot_be_attested_approved(self):
        self.take()
        plan = self.root / "fail-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "failure", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}]}))
        receipt = self.root / "failed-checks.json"
        self.guard(*self.context("run-checks"), "--checks-file", str(plan), "--expect-checks-sha256", self.digest(plan), "--out", str(receipt), expect=1)
        self.assertEqual(7, json.loads(receipt.read_text())["results"][0]["exit_code"])
        reply = self.root / "reply.json"
        result = self.guard(*self.context("attest"), "--checks", str(receipt), "--expect-checks-sha256", self.digest(receipt),
                           "--review-result", str(reply), "--expect-review-sha256", self.digest(reply),
                           "--out", str(self.root / "bad-proof"), expect=2)
        self.assertIn("失敗", result.stderr)

    def test_all_entrypoints_run_precheck_before_collecting_contents(self):
        self.take()
        (self.repo / ".gitattributes").write_text("*.txt filter=unsafe\n")
        self.git("config", "filter.unsafe.clean", "false")
        result = self.verify(expect=2)
        self.assertIn("precheck が拒否", result.stderr)
        result = self.guard(*self.context("seal"), "--review-input", str(self.review), expect=2)
        self.assertIn("precheck が拒否", result.stderr)
        result = self.guard("start", "--cwd", str(self.repo), "--state", str(self.root / "next-start"),
                           "--task-md", str(self.task), "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
        self.assertIn("precheck が拒否", result.stderr)

    def test_unmerged_index_and_manifest_fifo_are_real_errors(self):
        self.take()
        manifest = self.state / "manifest.json"
        manifest.unlink()
        os.mkfifo(manifest)
        result = self.verify(expect=2)
        self.assertIn("通常ファイル", result.stderr)


    def test_external_task_body_issue_flow_has_no_local_task_rename(self):
        # remote 呼出は trusted caller の責務。fixture はその fresh な本文の控え。
        body = self.root / "issue-body.txt"
        body.write_bytes(self.task.read_bytes())
        self.task.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "external task source")
        source = ["--task-body", str(body), "--expect-task-body-sha256", self.digest(body)]
        started = self.guard("start", "--cwd", str(self.repo), "--state", str(self.start_state),
                             "--task-id", "issue:188", "--task-dir", str(self.repo / "tasks"),
                             "--exclude-ere", r"(^|/)[.]env($|[.])", *source)
        self.start_sha256 = next(line.split("=", 1)[1] for line in started.stdout.splitlines() if line.startswith("START_SHA256="))
        (self.repo / "source.txt").write_text("implemented")
        result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--start-state", str(self.start_state),
                            "--expect-start-sha256", self.start_sha256, "--exclude-ere", r"(^|/)[.]env($|[.])", *source)
        self.state_sha256 = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("STATE_SHA256="))
        old_context = self.context
        self.context = lambda command: old_context(command) + ["--task-body", str(body), "--expect-task-body-sha256", self.digest(body)]
        sealed = self.guard(*self.context("seal"), "--review-input", str(self.review))
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines() if line.startswith("STATE_SHA256="))
        self.prove()
        expected = self.root / "expected-issue.txt"
        self.guard(*self.context("finalize"), "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash,
                   "--transition", "complete", "--out", str(expected))
        body.write_bytes(expected.read_bytes())
        flags = ["--task-transition", "complete", "--expected-task", str(expected), "--expect-task-sha256", self.digest(expected),
                 "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash]
        self.git("add", "source.txt")
        self.guard(*self.context("verify"), "--mode", "stage", *flags)
        self.git("commit", "-qm", "external task implementation")
        self.guard(*self.context("verify"), "--mode", "commit", *flags)
        self.assertFalse(list((self.repo / "tasks").glob("*.md")))
        original = body.read_bytes()
        body.write_text(body.read_text().replace("work", "forged scope") + "\nAPPROVED 実施済\n")
        result = self.guard(*self.context("verify"), "--mode", "commit", *flags, expect=1)
        self.assertIn("mismatch", result.stdout)
        body.write_bytes(original)

        # 完了後の外部本文を使う文書経路も、ローカル task MD を新設しない。
        self.start_state = self.root / "doc-start"
        self.state = self.root / "doc-state"
        source = ["--task-body", str(body), "--expect-task-body-sha256", self.digest(body)]
        started = self.guard("start", "--cwd", str(self.repo), "--state", str(self.start_state),
                             "--task-id", "issue:188", "--task-dir", str(self.repo / "tasks"),
                             "--exclude-ere", r"(^|/)[.]env($|[.])", *source)
        self.start_sha256 = next(line.split("=", 1)[1] for line in started.stdout.splitlines() if line.startswith("START_SHA256="))
        (self.repo / "guide.md").write_text("updated documentation")
        result = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--start-state", str(self.start_state),
                            "--expect-start-sha256", self.start_sha256, "--phase", "doc", "--exclude-ere", r"(^|/)[.]env($|[.])", *source)
        self.state_sha256 = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("STATE_SHA256="))
        sealed = self.guard(*self.context("seal"), "--review-input", str(self.review))
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines() if line.startswith("STATE_SHA256="))
        self.prove()
        self.git("add", "guide.md")
        self.guard(*self.context("verify"), "--mode", "stage", "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash)
        self.git("commit", "-qm", "external task documentation")
        self.guard(*self.context("verify"), "--mode", "commit", "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash)


    def test_secret_staged_before_take_cannot_enter_commit(self):
        secret = self.repo / ".env.tracked"
        secret.write_text("initial")
        self.git("add", ".env.tracked")
        self.git("commit", "-qm", "secret baseline")
        self.begin()
        secret.write_text("unreviewed secret")
        self.git("add", ".env.tracked")
        self.capture()
        self.verify("--mode", "stage", expect=1)

    def test_gitlink_normal_tree_and_submodule_commit_changes(self):
        module = self.repo / "module"
        module.mkdir()
        def subgit(*args):
            return subprocess.run(["git", "-C", str(module), *args], check=True, capture_output=True)
        subgit("init", "-q")
        subgit("config", "user.name", "test")
        subgit("config", "user.email", "test@example.invalid")
        (module / "code").write_text("first")
        subgit("add", "code")
        subgit("commit", "-qm", "module first")
        self.git("add", "module")
        self.git("commit", "-qm", "gitlink baseline")
        self.begin()
        (module / "code").write_text("second")
        subgit("add", "code")
        subgit("commit", "-qm", "module second")
        self.capture()
        self.git("add", "module")
        self.verify("--mode", "stage")
        self.git("commit", "-qm", "reviewed gitlink")
        self.verify("--mode", "commit")
        subgit("commit", "--allow-empty", "-qm", "unreviewed module change")
        self.verify("--mode", "commit", expect=1)

    def test_invalid_index_cannot_succeed(self):
        self.take()
        (self.repo / ".git/index").write_bytes(b"invalid index")
        result = self.verify(expect=2)
        self.assertRegex(result.stderr, "precheck|git")

    def test_hold_record_retains_loop_template_contract(self):
        self.begin()
        self.capture(phase="hold")
        self.proof = self.root / "hold-proof.json"
        self.guard(*self.context("attest"), "--hold-reason", "G1 denied", "--hold-next", "権限を確認する", "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)
        completed, expected = self.complete("pending")
        lines = completed.read_text().splitlines()
        holds = [line for line in lines if re.match(r"^- \*\*保留\*\*\(ship-task・[0-9]{4}-[0-9]{2}-[0-9]{2}\): G1 ", line)]
        self.assertEqual(1, len(holds))
        self.assertIn("UNAPPROVED", holds[0])
        self.assertNotIn("検証器記録", completed.read_text())


    def test_parent_child_task_root_preserves_normal_completion(self):
        parent = self.root / "parent"
        parent.mkdir()
        (parent / "tasks").mkdir()
        remote_task = parent / "tasks/進行中_guard.md"
        remote_task.write_bytes(self.task.read_bytes())
        self.task.unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "child source only")
        self.task = remote_task
        self.task_root_override = parent
        other = parent / "tasks/候補_other.md"
        other.write_text("other task")
        self.begin()
        (self.repo / "source.txt").write_text("parent child implementation")
        self.capture()
        completed, expected = self.complete()
        self.verify("--task-transition", "complete", "--expected-task", str(expected))
        self.git("add", "source.txt")
        self.verify("--mode", "stage", "--task-transition", "complete", "--expected-task", str(expected))
        self.git("commit", "-qm", "child implementation")
        self.verify("--mode", "commit", "--task-transition", "complete", "--expected-task", str(expected))
        other.write_text("forged")
        self.verify("--mode", "commit", "--task-transition", "complete", "--expected-task", str(expected), expect=1)


    def load_guard_module(self):
        spec = importlib.util.spec_from_file_location("review_guard_io", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_growing_file_and_read_deadline_are_finite(self):
        module = self.load_guard_module()
        path = self.root / "growing"
        path.write_bytes(b"a")
        calls = []
        def endless(size_fd, size):
            calls.append(size)
            return b"x" * min(size, 1)
        with mock.patch.object(module.os, "read", side_effect=endless):
            with self.assertRaisesRegex(module.GuardError, "増大"):
                module.read_regular(path, "増大試験")
        self.assertLessEqual(len(calls), 2)
        with mock.patch.object(module, "MAX_READ_SECONDS", -1):
            with self.assertRaisesRegex(module.GuardError, "時間上限"):
                module.read_regular(path, "時間試験")

    def test_parent_directory_swap_never_reads_external_secret(self):
        module = self.load_guard_module()
        inside = self.root / "inside"
        outside = self.root / "outside"
        inside.mkdir()
        outside.mkdir()
        (inside / "file").write_bytes(b"public")
        (outside / "file").write_bytes(b"SECRET")
        original_open = module.os.open
        swapped = False
        def swap_after_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original_open(path, flags, *args, **kwargs)
            if path == "inside" and not swapped:
                swapped = True
                inside.rename(self.root / "moved")
                inside.symlink_to(outside, target_is_directory=True)
            return fd
        with mock.patch.object(module.os, "open", side_effect=swap_after_open):
            with self.assertRaises(module.GuardError):
                module.read_regular(inside / "file", "親差替え")
        self.assertEqual(b"SECRET", (outside / "file").read_bytes())

    def test_output_parent_swap_never_writes_external_directory(self):
        module = self.load_guard_module()
        inside = self.root / "inside"
        outside = self.root / "outside"
        inside.mkdir()
        outside.mkdir()
        original_open = module.os.open
        swapped = False
        def swap_after_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original_open(path, flags, *args, **kwargs)
            if path == "inside" and not swapped:
                swapped = True
                inside.rename(self.root / "moved")
                inside.symlink_to(outside, target_is_directory=True)
            return fd
        with mock.patch.object(module.os, "open", side_effect=swap_after_open):
            with self.assertRaises((module.GuardError, OSError)):
                module.create_bytes(inside / "output", b"safe output")
        self.assertFalse((outside / "output").exists())

    def test_ignored_large_build_file_is_metadata_only_and_clean_build_passes(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        (self.repo / "node_modules").mkdir()
        large = self.repo / "node_modules/large.bin"
        with large.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024)
        self.take()
        manifest = json.loads((self.state / "manifest.json").read_text())
        item = next(i for i in manifest["worktree"] if i["path"] == "node_modules/large.bin")
        self.assertTrue(item["ignored"])
        self.assertNotIn("sha256", item)
        self.assertNotIn("oid", item)
        large.rename(self.repo / "public-large.bin")
        result = self.verify(expect=2)
        self.assertIn("読取上限", result.stderr)


    def test_secret_exclusion_can_expand_but_cannot_drop_start_patterns(self):
        scripts = self.repo / "scripts"
        scripts.mkdir()
        private = scripts / "private.py"
        private.write_text("original public bytes")
        original_hash = self.digest(private)
        self.git("add", "scripts/private.py")
        self.git("commit", "-qm", "public baseline")
        self.begin()
        private.write_text("new secret bytes")
        extra = r"^scripts/private[.]py$"
        common = ["take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                  "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256]
        result = self.guard(*common, "--exclude-ere", extra, expect=2)
        self.assertIn("ERE が削除", result.stderr)
        result = self.guard(*common, "--exclude-ere", r"(^|/)[.]env($|[.])", "--exclude-ere", extra)
        self.state_sha256 = next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("STATE_SHA256="))
        self.assertNotIn(original_hash, result.stdout)
        manifest_text = (self.state / "manifest.json").read_text()
        self.assertNotIn(original_hash, manifest_text)
        old_context = self.context
        self.context = lambda command: old_context(command) + ["--exclude-ere", extra]
        sealed = self.guard(*self.context("seal"), "--review-input", str(self.review))
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines() if line.startswith("STATE_SHA256="))
        self.prove()



if __name__ == "__main__":
    unittest.main()
