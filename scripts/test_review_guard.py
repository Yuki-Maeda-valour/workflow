"""review-guard.py の review/commit 照合の回帰テスト。"""

import json
import hashlib
import importlib.util
from unittest import mock
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "plugins/dev-workflow/skills/ship-task/scripts/review-guard.py"
LINUX = sys.platform.startswith("linux")
DARWIN = sys.platform == "darwin"


# 不在の確かめ方と後始末。test_check_process.py にも同じ関数を置く(試験ファイルはそれぞれ単独で読み込まれる)。
def probe(send, target):
    try:
        send(target, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "denied"
    return "alive"


def assert_absent_now(pid, send=os.kill):
    # 製品が「止めて回収した」と返した直後に使う。生きていれば待たずに失敗にする。
    # EPERM だけは macOS の回収直後の一過性の応答として、1 秒以内で 0.02 秒ごとに確かめ直す。
    deadline = time.monotonic() + 1
    while True:
        state = probe(send, pid)
        if state == "absent":
            return
        if state == "alive":
            raise AssertionError(f"{pid} が残っている")
        if time.monotonic() >= deadline:
            raise AssertionError(f"{pid} の不在を 1 秒以内に確かめられない(EPERM が続く)")
        time.sleep(.02)


def assert_group_absent_now(pgid):
    assert_absent_now(pgid, os.killpg)


def wait_absent(pid, timeout=5, send=os.kill):
    # 自分で終わるはずのプロセスと後始末だけに使う。期限まで待つ。
    deadline = time.monotonic() + timeout
    while probe(send, pid) != "absent":
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
    return True


# 後続コマンドの中で、assert_absent_now と同じ規則で不在を確かめる関数(品質コマンドに埋め込む)。
ABSENT_NOW = """def absent_now(send, target):
    end = time.monotonic() + 1
    while True:
        try:
            send(target, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            if time.monotonic() >= end:
                return False
            time.sleep(.02)
            continue
        return False
"""


def record(path):
    # 子と孫が自分の pid と pgid を JSON で書く 1 文。書きかけを読ませないよう、置き換えで書く。
    part = str(path) + ".part"
    return (f"Path({part!r}).write_text(json.dumps({{'pid': os.getpid(), 'pgid': os.getpgrp()}})); "
            f"os.replace({part!r}, {str(path)!r})")


def read_record(path):
    try:
        value = json.loads(Path(path).read_text())
        return int(value["pid"]), int(value["pgid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def command_line(pid):
    found = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
    return found.stdout


def owned(root, pid, pgid):
    # 信号を送る直前に、今の pgid が記録と同じで、コマンド行に試験の領域を含むかを確かめる。
    try:
        if os.getpgid(pid) != pgid:
            return False
    except OSError:
        return False
    return str(root) in command_line(pid)


def cleanup(root, stop=(), children=(), groups=(), records=(), raw=()):
    # 試験が起動したものだけを止めて回収し、残ったものの番号を返す。
    # stop: 監督(run-checks)の Popen。children: 試験の直接の子の Popen(差し替えで捕まえた anchor・無関係なプロセス)。
    # groups: 元の群の pgid を書いた記録。records: JSON の記録。raw: 素の pid(既存の Linux の試験の形)。
    found = [value for value in map(read_record, records) if value]
    groups = [value[1] for value in map(read_record, groups) if value]
    pids = [pid for pid, _ in found] + list(raw)
    # 手順 1: 監督がまだ動いていれば、TERM で製品の停止の道を先に通す。
    for process in stop:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    # 手順 2: 元の群の先頭が生きていて、コマンド行に試験の領域を含むときだけ、群ごと止める。
    for pgid in groups:
        if owned(root, pgid, pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    # 手順 3: 記録した各プロセスを止める。素の pid は、まだ終わっていない自分の子だけ。
    for pid, pgid in found:
        if owned(root, pid, pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for pid in raw:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if not done:
            os.kill(pid, signal.SIGKILL)
    # 手順 4: 直接の子と、試験のプロセスが引き取った孤児(Linux の subreaper)を回収する。
    for process in children:
        if process.poll() is None:
            process.kill()
        process.wait()
    for pid in pids:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    # 手順 5: 記録したすべての pid と元の群が不在になるまで待つ。
    left = [pid for pid in pids if not wait_absent(pid)]
    return left + [pgid for pgid in groups if not wait_absent(pgid, send=os.killpg)]


class GuardCliFixture:
    # 実 CLI で使い捨てリポジトリの状態を作る準備。test_ で始まるメソッドを持たない。
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wf-proc-test-")
        # macOS の /var はリンク。製品はリンクの親を拒否するので、試験の側で物理パスにする。
        self.root = Path(os.path.realpath(self.tmp.name))
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


class ReviewGuardTest(GuardCliFixture, unittest.TestCase):
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

    def test_cli_run_checks_keeps_exit_codes_and_output_hashes(self):
        # 実 CLI の start → take → seal → run-checks。状態の認証を置き換えない。
        self.begin()
        self.capture(prove=False)
        codes = ["import sys; print('out0'); print('err0', file=sys.stderr)",
                 "import sys; print('out7'); print('err7', file=sys.stderr); sys.exit(7)"]
        plan = self.root / "exit-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": str(i), "argv": [sys.executable, "-c", code, str(self.root)]}
                                                 for i, code in enumerate(codes)]}))
        out = self.root / "exit-receipt.json"
        # 検証用コピーを試験の領域に置き、削除を確かめる。
        copies = self.root / "tmp"
        copies.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(copies)}):
            self.guard(*self.context("run-checks"), "--checks-file", str(plan),
                       "--expect-checks-sha256", self.digest(plan), "--out", str(out), expect=1)
        results = json.loads(out.read_text())["results"]
        self.assertEqual([0, 7], [item["exit_code"] for item in results])
        for item, number in zip(results, (0, 7)):
            self.assertEqual(hashlib.sha256(f"out{number}\n".encode()).hexdigest(), item["stdout_sha256"])
            self.assertEqual(hashlib.sha256(f"err{number}\n".encode()).hexdigest(), item["stderr_sha256"])
        self.assertEqual([], list(copies.iterdir()))

    def test_symlinked_parent_is_still_refused(self):
        # 試験は物理パスを使うが、製品のリンクの親の拒否は残っている。
        alias = self.root / "alias"
        alias.symlink_to(self.repo)
        result = self.guard("start", "--cwd", str(alias), "--state", str(self.start_state),
                            "--task-md", str(alias / "tasks/進行中_guard.md"),
                            "--exclude-ere", r"(^|/)[.]env($|[.])", expect=2)
        self.assertIn("Not a directory", result.stderr)
        self.assertFalse(self.start_state.exists() or self.start_state.is_symlink())

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

    def test_run_checks_inside_reviewed_code_does_not_create_bytecode(self):
        # 配布コード自身が検査対象でも、外部の環境設定なしで run-checks が成功する。
        original = SCRIPT
        copied = self.repo / 'plugins/dev-workflow/skills/ship-task/scripts/review-guard.py'
        shutil.copytree(original.parents[4], self.repo / 'plugins',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.git('add', 'plugins')
        self.git('commit', '-qm', 'guard fixture')
        with mock.patch(__name__ + '.SCRIPT', copied):
            # 今回の変更前から存在する task-digest 読込は準備工程だけで使う。
            with mock.patch.dict(os.environ, {'PYTHONDONTWRITEBYTECODE': '1'}):
                self.begin()
                self.capture(prove=False)
            self.assertFalse(list(self.repo.rglob('*.pyc')))
            plan = self.root / 'checks.json'
            plan.write_text(json.dumps({'commands': [{'name': 'pass', 'argv': [sys.executable, '-c', 'pass']}]}))
            out = self.root / 'receipt.json'
            with mock.patch.dict(os.environ):
                os.environ.pop('PYTHONDONTWRITEBYTECODE', None)
                self.guard(*self.context('run-checks'), '--checks-file', str(plan),
                           '--expect-checks-sha256', self.digest(plan), '--out', str(out))
            self.assertFalse(list(self.repo.rglob('*.pyc')))
            self.assertEqual(0, json.loads(out.read_text())['results'][0]['exit_code'])

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


@unittest.skipUnless(LINUX or DARWIN, "Linux・macOS 専用: 実 CLI で品質コマンドの停止と検証用コピーの扱いを検査")
class CliProcessLifecycleTest(GuardCliFixture, unittest.TestCase):
    # 状態の認証を置き換えない。start → take → seal を実 CLI で行い、run-checks を子プロセスで起動する。
    life = 3  # 子と孫が自分で終わるまでの秒数

    def setUp(self):
        super().setUp()
        self.running = []
        self.children = []
        self.ready = self.root / "ready"
        self.child = self.root / "child"
        self.grand = self.root / "grand"
        # 製品に止められるはずの親・子・孫が、自然に終わる直前に書く印。製品が戻った直後に無いことを確かめる。
        self.naturals = [self.root / (name + "-natural") for name in ("parent", "child", "grand")]

    def tearDown(self):
        try:
            left = cleanup(self.root, stop=self.running, children=self.children, groups=[self.ready],
                           records=[self.ready, self.child, self.grand])
        finally:
            super().tearDown()
        self.assertEqual([], left, "試験のプロセスが残った")

    def plan(self, escape, wait_after_ready):
        # 1 つ目(対象): 作業フォルダを記録し、TERM を無視する子と孫を起動する。子と孫の記録を見てから準備完了を書く。
        # 群外化では、子を別の群で起動する(孫なし)。子は life 秒後に印を書いて終わる。
        cwd, later, finished = (self.root / name for name in ("cwd", "later", "finished"))
        if escape:
            child = ("import json, os, time; from pathlib import Path; "
                     f"{record(self.child)}; time.sleep({self.life}); Path({str(finished)!r}).write_text('self-exited')")
            waits = [str(self.child)]
        else:
            parent_natural, child_natural, grand_natural = self.naturals
            grand = ("import json, os, signal, time; from pathlib import Path; "
                     f"signal.signal(signal.SIGTERM, signal.SIG_IGN); {record(self.grand)}; time.sleep({self.life}); "
                     f"Path({str(grand_natural)!r}).write_text('natural')")
            child = ("import json, os, signal, subprocess, sys, time; from pathlib import Path; "
                     f"signal.signal(signal.SIGTERM, signal.SIG_IGN); {record(self.child)}; "
                     f"subprocess.Popen([sys.executable, '-c', {grand!r}]); time.sleep({self.life}); "
                     f"Path({str(child_natural)!r}).write_text('natural')")
            waits = [str(self.child), str(self.grand)]
        finish = f"time.sleep(3); Path({str(self.naturals[0])!r}).write_text('natural')" if wait_after_ready else ""
        target = f"""import json, os, subprocess, sys, time
from pathlib import Path
Path({str(cwd)!r}).write_text(str(Path.cwd()))
subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session={escape!r})
end = time.monotonic() + 3
while not all(Path(name).exists() for name in {waits!r}):
    if time.monotonic() >= end:
        raise SystemExit(3)
    time.sleep(.01)
{record(self.ready)}
{finish}
"""
        # 2 つ目(後続): 元の群と、元の群にいた子と孫の不在を確かめてから印を書く。確かめられなければ終了 1。
        following = f"""import json, os, time
from pathlib import Path
{ABSENT_NOW}
pgid = json.loads(Path({str(self.ready)!r}).read_text())['pgid']
ok = absent_now(os.killpg, pgid)
for name in ({str(self.child)!r}, {str(self.grand)!r}):
    if ok and Path(name).exists():
        value = json.loads(Path(name).read_text())
        if value['pgid'] == pgid:
            ok = absent_now(os.kill, value['pid'])
if not ok:
    raise SystemExit(1)
Path({str(later)!r}).write_text('absent')
"""
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "target", "argv": [sys.executable, "-c", target]},
                                                 {"name": "following", "argv": [sys.executable, "-c", following]}]}))
        return plan, cwd, later, finished

    def run_case(self, case, number=None):
        self.begin()
        self.capture(prove=False)
        escape = case == "escape"
        plan, cwd, later, finished = self.plan(escape, case in ("timeout", "signal"))
        out = self.root / "receipt.json"
        # 検証用コピーを試験の領域に置き、削除と保持を確かめ、後始末で消す。
        copies = self.root / "tmp"
        copies.mkdir()
        argv = [sys.executable, str(SCRIPT), *self.context("run-checks"), "--checks-file", str(plan),
                "--expect-checks-sha256", self.digest(plan), "--out", str(out),
                "--timeout", "1" if case == "timeout" else "10"]
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(self.root)],
                                     start_new_session=True)
        self.children.append(unrelated)
        try:
            running = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=dict(os.environ, TMPDIR=str(copies)))
            self.running.append(running)
            deadline = time.monotonic() + 5
            while not self.ready.exists() and running.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.ready.exists(), "準備完了が 5 秒以内に書かれない")
            self.assertTrue(self.child.exists() and (escape or self.grand.exists()), "子と孫の記録が無い")
            if number:
                # 回収済みの番号へ送らない。終わっていれば失敗にし、送るのは Popen 経由にする(回収済みなら送らない)。
                self.assertIsNone(running.poll(), "run-checks が信号を送る前に終わった")
                running.send_signal(number)
            stdout, stderr = running.communicate(timeout=20)
            self.assertEqual([], [path.name for path in self.naturals if path.exists()], "止めずに自然に終わるのを待った")
            pgid = read_record(self.ready)[1]
            if escape:
                # 群の外へ移った子は保証外。戻った時点で生きている。
                escaped = read_record(self.child)
                self.assertNotEqual(pgid, escaped[1])
                self.assertEqual("alive", probe(os.kill, escaped[0]))
            # 戻った直後に、元の群と、元の群にいた子と孫の不在を確かめる。
            assert_group_absent_now(pgid)
            for path in (self.child, self.grand):
                found = read_record(path)
                if found and found[1] == pgid:
                    assert_absent_now(found[0])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()
        copy = Path(cwd.read_text())
        receipt = json.loads(out.read_text()) if out.exists() else None
        if case == "signal":
            self.assertEqual(2, running.returncode, stderr)
            self.assertIn("検証用コピーを保持", stderr)
            self.assertRegex(stderr, f"終了通知 {int(number)} を受けた")
            self.assertIsNone(receipt)
            self.assertTrue(copy.exists())
            self.assertFalse(later.exists())
            return
        if case == "timeout":
            self.assertEqual(1, running.returncode, stderr)
            self.assertEqual(1, len(receipt["results"]))
            self.assertEqual("timeout", receipt["results"][0]["error"])
            self.assertFalse(later.exists())
        else:
            self.assertEqual(0, running.returncode, stderr)
            self.assertTrue(later.exists())
        self.assertFalse(copy.exists())
        scope = "owned-descendants" if LINUX else "original-process-group-only"
        self.assertEqual([scope] * len(receipt["results"]), [item["recovery_scope"] for item in receipt["results"]])
        if escape:
            self.assertTrue(wait_absent(escaped[0]), "群の外へ移った子が 5 秒以内に終わらない")
            self.assertTrue(finished.exists())

    def test_cli_parent_exit_stops_remaining_group_before_next_command(self):
        self.run_case("exit")

    def test_cli_timeout_stops_group_and_removes_copy(self):
        self.run_case("timeout")

    def test_cli_term_keeps_copy_and_stops_group(self):
        self.run_case("signal", signal.SIGTERM)

    def test_cli_int_keeps_copy_and_stops_group(self):
        self.run_case("signal", signal.SIGINT)

    def test_cli_hup_keeps_copy_and_stops_group(self):
        self.run_case("signal", signal.SIGHUP)

    @unittest.skipUnless(DARWIN, "macOS 専用: 群の外へ移る子は POSIX 方式の保証外。Linux の監督は方式が違う")
    def test_cli_group_escape_is_outside_guarantee(self):
        self.run_case("escape")


@unittest.skipUnless(LINUX or DARWIN, "Linux・macOS 専用: Linux の subreaper か POSIX 方式の監督で実プロセスを検査")
class CheckLifecycleTest(unittest.TestCase):
    """状態認証だけを置き換え、コピー・Git・品質起動・削除は実行する。"""
    def setUp(self):
        import ctypes
        self.tmp = tempfile.TemporaryDirectory(prefix="wf-proc-test-")
        self.root = Path(os.path.realpath(self.tmp.name))
        source = Path(os.environ.get("REVIEW_GUARD_TEST_SCRIPT", SCRIPT))
        spec = importlib.util.spec_from_file_location("lifecycle_guard", source)
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.seed = self.root / "seed"
        self.seed.write_text("fixture\n")
        self.saved = {"worktree": [{"path": "seed", "kind": "file", "mode": "100644",
                       "sha256": self.guard.sha256(self.seed.read_bytes())}],
                      "commit_paths": ["seed"], "review_binding_sha256": "fixture"}
        self.pids = []
        self.records = []
        self.children = []
        if hasattr(self.guard, "check_process_module"):
            self.process_helper = self.guard.check_process_module()
        if sys.platform.startswith("linux"):
            self.libc = ctypes.CDLL(None)
            old = ctypes.c_int()
            self.libc.prctl(37, ctypes.byref(old), 0, 0, 0)
            self.old_subreaper = old.value
            self.libc.prctl(36, 1, 0, 0, 0)

    def tearDown(self):
        # 素の pid の記録は自分の子だけ、JSON の記録は pgid とコマンド行を確かめてから止める。
        # Linux では、試験のプロセスが引き取った孤児も回収する(回収しないと zombie にも kill(pid, 0) が成功する)。
        try:
            left = cleanup(self.root, children=self.children, groups=self.records, records=self.records, raw=self.pids)
        finally:
            if hasattr(self, "libc"):
                self.libc.prctl(36, self.old_subreaper, 0, 0, 0)
            self.tmp.cleanup()
        self.assertEqual([], left, "試験のプロセスが残った")

    def invoke(self, codes, timeout=1):
        import argparse
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"commands": [{"name": str(i), "argv": [sys.executable, "-c", code, str(self.root)]}
                                                for i, code in enumerate(codes)]}))
        out = self.root / "receipt.json"
        args = argparse.Namespace(checks_file=str(plan), expect_checks_sha256=self.guard.sha256(plan.read_bytes()),
                                  timeout=timeout, out=str(out))
        original_mkdtemp = tempfile.mkdtemp
        def make_directory(suffix=None, prefix=None, dir=None):
            return original_mkdtemp(suffix=suffix, prefix=prefix, dir=self.root)
        with mock.patch.object(self.guard.tempfile, "mkdtemp", side_effect=make_directory), \
             mock.patch.object(self.guard, "read_context", return_value=(self.root, self.saved, [])), \
             mock.patch.object(self.guard, "check_current", return_value=True), \
             mock.patch.object(self.guard, "public_path"), \
             mock.patch.object(self.guard, "output_json", side_effect=lambda p, value, *_: p.write_text(json.dumps(value))):
            return self.guard.run_checks(args)

    def test_timeout_reaps_grandchild_before_checkout_removal_and_stops_plan(self):
        import time
        marker, pidfile, cleanfile, later = (self.root / x for x in ("marker", "pid", "clean", "later"))
        child = f"import time; from pathlib import Path; cwd=Path.cwd(); time.sleep(1.4); Path({str(marker)!r}).write_text(str(cwd.exists()))"
        code = f"""import subprocess,sys,time
from pathlib import Path
Path({str(cleanfile)!r}).write_text(str(Path.cwd()))
p = subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path({str(pidfile)!r}).write_text(str(p.pid))
time.sleep(3)
"""
        try:
            rc = self.invoke([code, f"from pathlib import Path; Path({str(later)!r}).touch()"])
        finally:
            if pidfile.exists(): self.pids.append(int(pidfile.read_text()))
        time.sleep(.8)
        self.assertEqual(1, rc)
        self.assertFalse(marker.exists(), "時間切れの子孫がコピー削除後も動いた")
        self.assertFalse(later.exists(), "時間切れ後に後続コマンドを起動した")
        self.assertFalse(Path(cleanfile.read_text()).exists())
        self.assertEqual("timeout", json.loads((self.root / "receipt.json").read_text())["results"][0]["error"])


    def test_timeout_with_term_ignoring_sibling_stops_grandchild_before_checkout_removal(self):
        # test_timeout_reaps_grandchild_before_checkout_removal_and_stops_plan に、親の別の子として「TERM を無視して 2.5 秒で自分で終わる兄弟」を足した形。両 OS で動かす。
        # TERM を無視する兄弟が、群への KILL で止まることを確かめる。
        # Linux では、親は TERM で終わり、孫はすぐ引き取られて止まる。
        marker, grandfile, siblingfile, cleanfile, later = (self.root / x for x in ("marker", "grand", "sibling", "clean", "later"))
        natural = self.root / "sibling-natural"  # 兄弟が自然に終わる直前に書く印
        self.records += [grandfile, siblingfile]
        child = f"import json, os, time; from pathlib import Path; {record(grandfile)}; cwd=Path.cwd(); time.sleep(1.4); Path({str(marker)!r}).write_text(str(cwd.exists()))"
        sibling = f"import json, os, signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); {record(siblingfile)}; time.sleep(2.5); Path({str(natural)!r}).write_text('natural')"
        code = f"""import subprocess,sys,time
from pathlib import Path
Path({str(cleanfile)!r}).write_text(str(Path.cwd()))
subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.Popen([sys.executable, '-c', {sibling!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
"""
        rc = self.invoke([code, f"from pathlib import Path; Path({str(later)!r}).touch()"])
        # 戻った直後に、兄弟が自然に終わっていないこと(製品が止めたこと)と、孫と兄弟の不在を確かめる。
        self.assertFalse(natural.exists(), "兄弟が自然に終わるのを待った")
        for path in (grandfile, siblingfile):
            found = read_record(path)
            self.assertIsNotNone(found, f"{path.name} の記録が無い")
            assert_absent_now(found[0])
        time.sleep(.8)
        self.assertEqual(1, rc)
        self.assertFalse(marker.exists(), "時間切れの子孫がコピー削除後も動いた")
        self.assertFalse(later.exists(), "時間切れ後に後続コマンドを起動した")
        self.assertFalse(Path(cleanfile.read_text()).exists())
        self.assertEqual("timeout", json.loads((self.root / "receipt.json").read_text())["results"][0]["error"])

    def test_normal_nonzero_preserves_output_and_no_overlap(self):
        import hashlib
        pidfile = self.root / "pid"
        cleanfile = self.root / "clean"
        natural = self.root / "natural"  # 子が自然に終わる直前に書く印
        child = f"import time; from pathlib import Path; time.sleep(3); Path({str(natural)!r}).write_text('natural')"
        command = f"""import subprocess,sys,time
from pathlib import Path
Path({str(cleanfile)!r}).write_text(str(Path.cwd()))
p=subprocess.Popen([sys.executable,'-c',{child!r}, {str(self.root)!r}])
Path({str(pidfile)!r}).write_text(str(p.pid))
print('output')
raise SystemExit(7)
"""
        following = f"""import os, time
from pathlib import Path
{ABSENT_NOW}
assert Path('seed').exists()
assert absent_now(os.kill, int(Path({str(pidfile)!r}).read_text()))
"""
        try:
            self.assertEqual(1, self.invoke([command, following]))
            # 戻った直後に、子が自然に終わっていないこと(製品が止めたこと)を確かめる。
            self.assertFalse(natural.exists(), "子が自然に終わるのを待った")
        finally:
            if pidfile.exists(): self.pids.append(int(pidfile.read_text()))
        results = json.loads((self.root / 'receipt.json').read_text())['results']
        self.assertEqual([7, 0], [item['exit_code'] for item in results])
        self.assertEqual(hashlib.sha256(b'output\n').hexdigest(), results[0]['stdout_sha256'])
        self.assertFalse(Path(cleanfile.read_text()).exists())

    def test_helper_import_restores_bytecode_setting_even_on_failure(self):
        import importlib.machinery
        for original in (False, True):
            with mock.patch.object(sys, 'dont_write_bytecode', original):
                self.guard.check_process_module()
                self.assertIs(original, sys.dont_write_bytecode)
                with mock.patch.object(importlib.machinery.SourceFileLoader, 'exec_module', side_effect=RuntimeError('fixture')):
                    with self.assertRaises(RuntimeError):
                        self.guard.check_process_module()
                self.assertIs(original, sys.dont_write_bytecode)

    def test_invalid_timeouts_rejected_before_context_or_process(self):
        for timeout in (0, -1, True, float('inf'), 10 ** 400):
            with self.subTest(timeout=str(timeout)[:30]), mock.patch.object(self.guard, 'read_context') as context:
                with self.assertRaises(self.guard.GuardError):
                    self.invoke(['raise AssertionError("must not start")'], timeout=timeout)
                context.assert_not_called()
        self.assertFalse(list(self.root.glob('review-guard-clean-*')))

    @unittest.skipUnless(LINUX, "Linux 専用: Linux の監督の回収結果を差し替える")
    def test_bad_or_missing_recovery_proof_retains_copy_and_stops(self):
        for failure in (ValueError('bad JSON'), FileNotFoundError('missing'), self.process_helper.RecoveryError('unknown group')):
            with self.subTest(failure=type(failure).__name__):
                later = self.root / 'later'
                with mock.patch.object(self.guard, 'check_process_module', return_value=self.process_helper), \
                     mock.patch.object(self.process_helper, 'read_result', side_effect=failure):
                    with self.assertRaisesRegex(self.guard.GuardError, '検証用コピーを保持'):
                        self.invoke(['pass', f"from pathlib import Path; Path({str(later)!r}).touch()"])
                self.assertFalse(later.exists())
                self.assertFalse((self.root / 'receipt.json').exists())
                copies = list(self.root.glob('review-guard-clean-*'))
                self.assertTrue(copies)
                self.assertTrue(all((copy / 'seed').exists() for copy in copies))

    def test_stop_confirmation_failure_retains_copy(self):
        with mock.patch.object(self.guard, 'check_process_module', return_value=self.process_helper), \
             mock.patch.object(self.process_helper, 'run_command', side_effect=self.process_helper.RecoveryError('not absent')):
            with self.assertRaisesRegex(self.guard.GuardError, '検証用コピーを保持'):
                self.invoke(['pass', 'raise AssertionError("later")'])
        self.assertTrue(list(self.root.glob('review-guard-clean-*')))
        self.assertFalse((self.root / 'receipt.json').exists())

    def test_parent_signals_keep_copy_even_after_successful_recovery(self):
        import signal
        import threading
        import time
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            pidfile = self.root / ('signal-' + str(number))
            cleanfile = self.root / ('clean-' + str(number))
            natural = self.root / ('natural-' + str(number))  # 品質コマンドが自然に終わる直前に書く印
            later = self.root / 'later'
            def interrupt():
                end = time.monotonic() + 3
                while time.monotonic() < end:
                    if pidfile.exists() and pidfile.stat().st_size:
                        os.kill(os.getpid(), number)
                        return
                    time.sleep(.01)
            thread = threading.Thread(target=interrupt)
            thread.start()
            try:
                # TERM を無視させ、品質コマンドが群への KILL で止まることを確かめる。
                code = f"import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path({str(cleanfile)!r}).write_text(str(Path.cwd())); Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(2.5); Path({str(natural)!r}).write_text('natural')"
                with self.assertRaisesRegex(self.guard.GuardError, '検証用コピーを保持') as caught:
                    self.invoke([code, f"from pathlib import Path; Path({str(later)!r}).touch()"])
            finally:
                thread.join(4)
                if pidfile.exists(): self.pids.append(int(pidfile.read_text()))
            self.assertFalse(natural.exists(), '品質コマンドが自然に終わるのを待った')
            self.assertRegex(str(caught.exception), f'終了通知 {int(number)} を受けた')
            self.assertFalse(thread.is_alive())
            self.assertTrue(Path(cleanfile.read_text()).exists())
            assert_absent_now(int(pidfile.read_text()))
            self.assertFalse(later.exists())
            self.assertFalse((self.root / 'receipt.json').exists())

    @unittest.skipUnless(LINUX, "Linux 専用: Linux の監督を強制終了する")
    def test_supervisor_death_retains_copy_without_receipt(self):
        import signal
        import threading
        import time
        pidfile, cleanfile, later = (self.root / name for name in ('orphan', 'clean', 'later'))
        supervisor = []
        original = subprocess.Popen
        def launch(argv, *args, **kwargs):
            process = original(argv, *args, **kwargs)
            if '--linux' in argv:
                supervisor.append(process.pid)
            return process
        def kill():
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if pidfile.exists() and pidfile.stat().st_size:
                    os.kill(supervisor[0], signal.SIGKILL)
                    return
                time.sleep(.01)
        thread = threading.Thread(target=kill)
        thread.start()
        try:
            with mock.patch.object(self.guard, 'check_process_module', return_value=self.process_helper), \
                 mock.patch.object(self.process_helper.subprocess, 'Popen', side_effect=launch):
                code = f"import os,time; from pathlib import Path; Path({str(cleanfile)!r}).write_text(str(Path.cwd())); Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(3)"
                with self.assertRaisesRegex(self.guard.GuardError, '検証用コピーを保持'):
                    self.invoke([code, f"from pathlib import Path; Path({str(later)!r}).touch()"])
        finally:
            thread.join(4)
            if pidfile.exists(): self.pids.append(int(pidfile.read_text()))
        self.assertTrue(Path(cleanfile.read_text()).exists())
        self.assertFalse(later.exists())
        self.assertFalse((self.root / 'receipt.json').exists())


    def test_preparation_failure_without_launched_command_removes_copy(self):
        with mock.patch.object(self.guard.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['git'])):
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke(['pass'])
        self.assertFalse(list(self.root.glob('review-guard-clean-*')))

    def capture_anchor(self, anchors):
        # anchor の Popen を捕まえる。製品が例外で抜けて回収しない経路でも、試験が最後に wait で回収する。
        original = subprocess.Popen
        def launch(argv, *args, **kwargs):
            process = original(argv, *args, **kwargs)
            if "--anchor-fd" in argv:
                anchors.append(process)
                self.children.append(process)
            return process
        return mock.patch.object(self.process_helper.subprocess, "Popen", side_effect=launch)

    def assert_retained(self, caught, expected, later, before):
        # 検証用コピーを保持して止まり、意図した経路を通ったこと(回収の失敗〈Operation not permitted〉の経路ではないこと)を確かめる。
        message = str(caught.exception)
        self.assertIn(expected, message)
        self.assertNotIn("Operation not permitted", message)
        self.assertFalse(later.exists())
        self.assertFalse((self.root / "receipt.json").exists())
        copies = set(self.root.glob("review-guard-clean-*")) - before
        self.assertEqual(1, len(copies))
        self.assertTrue(all((copy / "seed").exists() for copy in copies))

    @unittest.skipUnless(DARWIN, "macOS 専用: POSIX 方式の anchor を強制終了する")
    def test_anchor_kill_retains_copy_and_stops_plan(self):
        import threading
        command, later = self.root / "command", self.root / "later"
        natural = self.root / "command-natural"  # 品質コマンドが自然に終わる直前に書く印
        self.records.append(command)
        anchors = []
        fired = threading.Event()
        def kill():
            # 待ちは品質コマンドが自分で終わる 2.5 秒より短い 2 秒。製品がまだ回収していない anchor にだけ送る
            # (poll は呼ばない。製品の wait を横取りしないため)。
            end = time.monotonic() + 2
            while time.monotonic() < end:
                if command.exists() and anchors:
                    if anchors[0].returncode is None:
                        os.kill(anchors[0].pid, signal.SIGKILL)
                        fired.set()
                    return
                time.sleep(.01)
        before = set(self.root.glob("review-guard-clean-*"))
        thread = threading.Thread(target=kill)
        thread.start()
        try:
            # 品質コマンドは TERM を無視して眠る(KILL の時点で群に生きた一員を残す)。anchor が失われた後に群の全員が
            # TERM で終わると、macOS は群への KILL を EPERM で返し、診断の文が Operation not permitted に替わるため。
            code = f"import json, os, signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); {record(command)}; time.sleep(2.5); Path({str(natural)!r}).write_text('natural')"
            with mock.patch.object(self.guard, "check_process_module", return_value=self.process_helper), \
                 self.capture_anchor(anchors):
                with self.assertRaisesRegex(self.guard.GuardError, "検証用コピーを保持") as caught:
                    self.invoke([code, f"from pathlib import Path; Path({str(later)!r}).touch()"], timeout=10)
        finally:
            thread.join(4)
        self.assertFalse(natural.exists(), "品質コマンドが自然に終わるのを待った")
        found = read_record(command)
        self.assertIsNotNone(found, "品質コマンドの記録が無い")
        assert_absent_now(found[0])
        self.assertFalse(thread.is_alive())
        self.assertTrue(fired.is_set(), "anchor へ SIGKILL を送っていない")
        self.assert_retained(caught, "anchor が", later, before)
        anchors[0].wait()

    @unittest.skipUnless(DARWIN, "macOS 専用: POSIX 方式の anchor の通知を壊す")
    def test_missing_bad_or_mismatched_anchor_notice_retains_copy(self):
        # 品質コマンドはすぐ終わる。通知を書いた後の anchor は、群への KILL まで生きている。
        original = os.read
        cases = ((b"", "anchor が終了通知の前に失われた"),
                 (b"invalid json\n", "Expecting value"),
                 (b'{"nonce": "wrong", "returncode": 0}\n', "anchor の通知が不正"))
        for index, (payload, expected) in enumerate(cases):
            with self.subTest(payload=payload):
                later = self.root / f"later-{index}"
                before = set(self.root.glob("review-guard-clean-*"))
                def corrupt(fd, size, payload=payload):
                    value = original(fd, size)
                    # anchor の通知を読む呼出し(4097 バイト)だけを差し替える。
                    return payload if size == 4097 else value
                with mock.patch.object(self.guard, "check_process_module", return_value=self.process_helper), \
                     mock.patch.object(self.process_helper.os, "read", side_effect=corrupt), \
                     self.capture_anchor([]):
                    with self.assertRaisesRegex(self.guard.GuardError, "検証用コピーを保持") as caught:
                        self.invoke(["pass", f"from pathlib import Path; Path({str(later)!r}).touch()"])
                self.assert_retained(caught, expected, later, before)

    @unittest.skipUnless(DARWIN, "macOS 専用: POSIX 方式の群の不在の確認を失敗させる")
    def test_group_absence_failure_retains_copy(self):
        later = self.root / "later"
        before = set(self.root.glob("review-guard-clean-*"))
        with mock.patch.object(self.guard, "check_process_module", return_value=self.process_helper), \
             mock.patch.object(self.process_helper, "group_exists", side_effect=PermissionError("injected")), \
             self.capture_anchor([]):
            with self.assertRaisesRegex(self.guard.GuardError, "検証用コピーを保持") as caught:
                self.invoke(["pass", f"from pathlib import Path; Path({str(later)!r}).touch()"])
        self.assert_retained(caught, "プロセス群の不在を確認できない", later, before)


if __name__ == "__main__":
    unittest.main()
