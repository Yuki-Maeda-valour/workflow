"""review-guard.py の review/commit 照合の回帰テスト。"""

import json
import hashlib
import importlib.util
import contextlib
import io
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
        # commit が裏の保守処理(詰め直し)を起動し、試験の一時領域へ書き続けないようにする(#292)。
        return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], cwd=self.repo, check=True,
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

    def test_run_checks_clean_copy_commit_starts_no_background_maintenance(self):
        # 検証用コピーを作る commit が、Git の裏の保守処理(詰め直し)を起動しないことを確かめる(#292)。両 OS で動かす。
        # 詰め直しは、1 つずつのファイルで置かれたオブジェクト(loose object)を 1 つのパックへまとめる処理。
        # Git は loose object の数を .git/objects/17 の中の数で見積もる。17 に 28 個あれば、Git 2.34・2.51・2.54 のどれでも詰め直す。
        seeds = []
        for number in range(50000):
            body = f"seed {number}\n".encode()
            if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest().startswith("17"):
                seeds.append(number)
                if len(seeds) == 28:
                    break
        self.assertEqual(28, len(seeds), "blob の名前が 17 で始まる中身が 28 個に届かない")
        (self.repo / "seeds").mkdir()
        for number in seeds:
            (self.repo / f"seeds/{number}.txt").write_text(f"seed {number}\n", encoding="utf-8")
        self.git("add", "seeds")
        self.git("commit", "-qm", "seeds")
        self.begin()
        self.capture(prove=False)
        # 品質コマンドは検証用コピーの .git/objects を見て、理由を検証用コピーの外のファイルに書く。
        # 終了値 5: 起動の時点でパックがある。3: 前提の崩れ(17 が 28 個より少ない)。4: 見ている 3 秒の間に詰め直しが始まった。
        reason = self.root / "maintenance-reason.txt"
        code = """import re, sys, time
from pathlib import Path
objects = Path('.git/objects')
def loose():
    try:
        return sum(1 for item in (objects / '17').iterdir() if re.fullmatch('[0-9a-f]{38}', item.name))
    except FileNotFoundError:
        return 0
def packed():
    return (objects / 'info/packs').exists() or any((objects / 'pack').glob('*.pack'))
def stop(code, text):
    Path(sys.argv[1]).write_text(text, encoding='utf-8')
    sys.exit(code)
start = loose()
if packed():
    stop(5, f'起動の時点でパックあり: 17 の数 {start}')
if start < 28:
    stop(3, f'前提の崩れ: 17 の数 {start}・パックなし')
end = time.monotonic() + 3
while time.monotonic() < end:
    time.sleep(.05)
    now, pack = loose(), packed()
    if pack or now < start:
        stop(4, f'見ている間に詰め直し: 17 の数 {start} から {now}・パック {pack}')
"""
        plan = self.root / "maintenance-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "maintenance", "argv": [sys.executable, "-c", code, str(reason)]}]}))
        out = self.root / "maintenance-receipt.json"
        # 検証用コピーと出力の置き場を試験の領域に置き、削除を確かめる。
        copies = self.root / "tmp"
        copies.mkdir()
        # self.guard は終了値を先に確かめるので使わない(落ちたときに、受領書と理由を失敗の文に入れるため)。
        with mock.patch.dict(os.environ, {"TMPDIR": str(copies)}):
            proc = subprocess.run([sys.executable, str(SCRIPT), *self.context("run-checks"), "--checks-file", str(plan),
                                   "--expect-checks-sha256", self.digest(plan), "--out", str(out)],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        receipt = json.loads(out.read_text()) if out.exists() else None
        detail = (f"受領書: {receipt}; 理由: {reason.read_text(encoding='utf-8') if reason.exists() else 'なし'}; "
                  f"run-checks の標準エラー: {proc.stderr}")
        self.assertIsNotNone(receipt, detail)
        self.assertEqual(0, receipt["results"][0]["exit_code"], detail)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], list(copies.iterdir()))

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

    # --- 落ちたコマンドの出力を残す(#368) ---
    def kept_run(self, commands, env=None, out_name="receipt.json", timeout=None, expect=1, prepare=True):
        # 計画を書き、TMPDIR を試験の領域に向け、umask を 022 にして run-checks を流す。TMPDIR の下は空に戻る。
        if prepare and not self.state.exists():
            self.begin()
            self.capture(prove=False)
        plan = self.root / "kept-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": name, "argv": argv} for name, argv in commands]}))
        copies = self.root / "tmp"
        copies.mkdir(exist_ok=True)
        out = self.root / out_name
        extra = ["--timeout", str(timeout)] if timeout else []
        old = os.umask(0o022)
        try:
            with mock.patch.dict(os.environ, {"TMPDIR": str(copies), **(env or {})}):
                proc = self.guard(*self.context("run-checks"), "--checks-file", str(plan),
                                  "--expect-checks-sha256", self.digest(plan), "--out", str(out), *extra, expect=expect)
        finally:
            os.umask(old)
        self.assertEqual([], list(copies.iterdir()))
        return proc, out

    def kept_notices(self, out, stream_indexes):
        return [f"品質コマンドの出力を残した: {json.dumps(name, ensure_ascii=True)} {stream} "
                f"{json.dumps(str(out.with_name(f'{out.name}.{index}.{stream}')), ensure_ascii=True)}"
                for name, index in stream_indexes for stream in ("stdout", "stderr")]

    def test_run_checks_keeps_masked_output_of_failed_command_only(self):
        secret = "kept-secret-0123456789-abcdefghijklmnop"
        self.assertEqual(39, len(secret))
        cwdfile = self.root / "cwd.txt"
        code = f"""import os, sys
secret = os.environ['WF_KEPT_SECRET']
open({str(cwdfile)!r}, 'w').write(os.getcwd())
print('secret=' + secret)
print('cwd=' + os.getcwd() + '/scripts/x.py')
print('short=' + os.environ['WF_KEPT_SHORT'])
print('eight=' + os.environ['WF_KEPT_EIGHT'])
print('part=second-line-BBBB')
print(' ' * 12)
sys.stdout.write('token=' + secret[:20])
print('err7', file=sys.stderr)
print('secret=' + secret, file=sys.stderr)
sys.exit(7)
"""
        env = {"WF_KEPT_SECRET": secret, "WF_KEPT_SHORT": "abcdefg", "WF_KEPT_EIGHT": "eight-8b",
               "WF_KEPT_MULTI": "first-line-AAAA\nsecond-line-BBBB", "WF_KEPT_SPACES": " " * 12}
        proc, out = self.kept_run([("pass", [sys.executable, "-c", "print('out0')", str(self.root)]),
                                   ("fail", [sys.executable, "-c", code, str(self.root)])], env=env)
        results = json.loads(out.read_text())["results"]
        self.assertEqual({"name", "argv", "exit_code", "stdout_sha256", "stderr_sha256", "recovery_method", "recovery_scope"}, set(results[0]))
        self.assertEqual({"name", "argv", "exit_code", "stdout_sha256", "stderr_sha256", "recovery_method", "recovery_scope", "kept_output"}, set(results[1]))
        self.assertEqual([], list(self.root.glob("receipt.json.0.*")))
        stdout_file = self.root / "receipt.json.1.stdout"
        stderr_file = self.root / "receipt.json.1.stderr"
        expected_out = ("secret=<環境変数 WF_KEPT_SECRET>\ncwd=<検証用コピー>/scripts/x.py\nshort=abcdefg\n"
                        "eight=<環境変数 WF_KEPT_EIGHT>\npart=<環境変数 WF_KEPT_MULTI>\n" + " " * 12 +
                        "\ntoken=<環境変数 WF_KEPT_SECRET>").encode()
        self.assertEqual(expected_out, stdout_file.read_bytes())
        self.assertEqual("err7\nsecret=<環境変数 WF_KEPT_SECRET>\n".encode(), stderr_file.read_bytes())
        raw_out = (f"secret={secret}\ncwd={cwdfile.read_text()}/scripts/x.py\nshort=abcdefg\neight=eight-8b\n"
                   f"part=second-line-BBBB\n{' ' * 12}\ntoken={secret[:20]}").encode()
        raw_err = f"err7\nsecret={secret}\n".encode()
        kept = results[1]["kept_output"]
        self.assertEqual({"stdout", "stderr"}, set(kept))
        for stream, path, raw, masked in (("stdout", stdout_file, raw_out, expected_out), ("stderr", stderr_file, raw_err, None)):
            self.assertEqual(path.name, kept[stream]["file"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), kept[stream]["sha256"])
            self.assertEqual(len(raw), kept[stream]["total_bytes"])
            self.assertEqual(0, kept[stream]["kept_from_byte"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), results[1][stream + "_sha256"])
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(self.kept_notices(out, [("fail", 1)]), proc.stderr.splitlines())
        self.assertEqual(f"CHECKS_SHA256={hashlib.sha256(out.read_bytes()).hexdigest()}\n", proc.stdout)

    def test_run_checks_keeps_last_mebibyte_from_a_line_start(self):
        secret = "kept-secret-0123456789-abcdefghijklmnop"
        code = f"""import os, sys
secret = os.environ['WF_KEPT_SECRET']
head = b'h' * 99 + b'\\n'
middle = ('S=' + secret + '\\n').encode()
rest = (b'r' * 99 + b'\\n') * 10485 + b'z' * 40 + b'\\n'
sys.stdout.buffer.write(head + middle + rest)
sys.exit(1)
"""
        proc, out = self.kept_run([("big", [sys.executable, "-c", code, str(self.root)])], env={"WF_KEPT_SECRET": secret})
        rest = (b"r" * 99 + b"\n") * 10485 + b"z" * 40 + b"\n"
        total = 100 + 42 + len(rest)
        self.assertEqual(1048576 + 107, total)
        kept = json.loads(out.read_text())["results"][0]["kept_output"]["stdout"]
        data = (self.root / "receipt.json.0.stdout").read_bytes()
        self.assertEqual(rest, data)
        self.assertNotIn(secret[5:].encode(), data)
        self.assertNotIn(b"<", data)
        self.assertEqual(total, kept["total_bytes"])
        self.assertEqual(142, kept["kept_from_byte"])

    def test_run_checks_keeps_output_of_timed_out_command(self):
        wait = ("import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); "
                "print('before-timeout', flush=True); time.sleep(30)")
        later = self.root / "later-mark"
        proc, out = self.kept_run([("wait", [sys.executable, "-c", wait, str(self.root)]),
                                   ("later", [sys.executable, "-c", f"open({str(later)!r}, 'w').close()", str(self.root)])],
                                  timeout=1)
        results = json.loads(out.read_text())["results"]
        self.assertEqual(1, len(results))
        self.assertIsNone(results[0]["exit_code"])
        self.assertEqual("timeout", results[0]["error"])
        self.assertEqual(b"before-timeout\n", (self.root / "receipt.json.0.stdout").read_bytes())
        self.assertFalse(later.exists())

    def test_run_checks_refuses_used_output_paths_before_running(self):
        self.begin()
        self.capture(prove=False)
        mark = self.root / "mark"
        for used in ("used.json", "used.json.0.stdout", "used.json.0.stderr"):
            with self.subTest(used=used):
                (self.root / used).write_text("x")
                proc, out = self.kept_run([("mark", [sys.executable, "-c", f"open({str(mark)!r}, 'w').close()", str(self.root)])],
                                          out_name="used.json", expect=2, prepare=False)
                self.assertIn("出力先は未使用のパスが必要", proc.stderr)
                self.assertFalse(mark.exists())
                (self.root / used).unlink()

    def test_hold_checks_keep_output_out_of_pr_evidence_and_task_record(self):
        self.begin()
        (self.repo / "source.txt").write_text("unfinished work")
        self.capture(phase="hold")
        code = "print('KEPT-MARKER-' + '368'); raise SystemExit(3)"
        proc, out = self.kept_run([("fail", [sys.executable, "-c", code, str(self.root)])], out_name="hold-checks.json", prepare=False)
        marker = "KEPT-MARKER-368"
        kept_file = self.root / "hold-checks.json.0.stdout"
        self.assertEqual((marker + "\n").encode(), kept_file.read_bytes())
        receipt = json.loads(out.read_text())
        self.proof = self.root / "hold-evidence.json"
        self.guard(*self.context("attest"), "--checks", str(out), "--expect-checks-sha256", self.digest(out),
                   "--hold-reason", "D1 needs decision", "--hold-next", "要件を確認する", "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)
        self.assertEqual(receipt["results"][0]["kept_output"], json.loads(self.proof.read_text())["checks"]["results"][0]["kept_output"])
        completed, expected = self.complete("pending")
        self.verify("--task-transition", "pending", "--expected-task", str(expected))
        self.git("add", "-A")
        self.verify("--mode", "stage", "--task-transition", "pending", "--expected-task", str(expected))
        self.git("commit", "-qm", "hold")
        pr = self.guard(*self.context("pr-evidence"), "--evidence", str(self.proof), "--expect-evidence-sha256", self.proof_hash,
                        "--task-transition", "pending", "--expected-task", str(expected),
                        "--expect-task-sha256", self.expected_hash)
        self.assertIn("exit=3", pr.stdout)
        kept = receipt["results"][0]["kept_output"]
        hidden = [marker, kept_file.name, "kept_output", kept["stdout"]["sha256"], kept["stderr"]["sha256"]]
        for text in (pr.stdout, completed.read_text()):
            for item in hidden:
                self.assertNotIn(item, text)

    def test_run_checks_all_pass_keeps_receipt_shape_and_writes_no_output(self):
        proc, out = self.kept_run([("one", [sys.executable, "-c", "print('o1')", str(self.root)]),
                                   ("two", [sys.executable, "-c", "import sys; print('e2', file=sys.stderr)", str(self.root)])],
                                  out_name="pass-receipt.json", expect=0)
        receipt = json.loads(out.read_text())
        self.assertEqual({"kind", "review_binding_sha256", "clean_checkout", "plan_sha256", "results"}, set(receipt))
        for item in receipt["results"]:
            self.assertEqual({"name", "argv", "exit_code", "stdout_sha256", "stderr_sha256", "recovery_method", "recovery_scope"}, set(item))
        self.assertEqual([], list(self.root.glob("pass-receipt.json.*")))
        self.assertEqual("", proc.stderr)
        self.assertEqual(f"CHECKS_SHA256={hashlib.sha256(out.read_bytes()).hexdigest()}\n", proc.stdout)

    def test_run_checks_keeps_empty_output_of_command_that_cannot_start(self):
        proc, out = self.kept_run([("absent", [str(self.root / "absent-command")])])
        item = json.loads(out.read_text())["results"][0]
        self.assertEqual(127, item["exit_code"])
        empty = hashlib.sha256(b"").hexdigest()
        for stream in ("stdout", "stderr"):
            path = self.root / f"receipt.json.0.{stream}"
            self.assertEqual({"file": path.name, "sha256": empty, "total_bytes": 0, "kept_from_byte": 0}, item["kept_output"][stream])
            self.assertEqual(b"", path.read_bytes())

    def test_run_checks_masks_real_path_of_copy_under_linked_tmpdir(self):
        self.begin()
        self.capture(prove=False)
        real = self.root / "real-tmp"
        real.mkdir()
        (self.root / "link-tmp").symlink_to(real)
        plan = self.root / "link-plan.json"
        code = "import os; print('cwd=' + os.getcwd()); raise SystemExit(2)"
        plan.write_text(json.dumps({"commands": [{"name": "cwd", "argv": [sys.executable, "-c", code, str(self.root)]}]}))
        out = self.root / "link-receipt.json"
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root / "link-tmp")}):
            self.guard(*self.context("run-checks"), "--checks-file", str(plan), "--expect-checks-sha256", self.digest(plan),
                       "--out", str(out), expect=1)
        self.assertEqual("cwd=<検証用コピー>\n".encode(), (self.root / "link-receipt.json.0.stdout").read_bytes())
        self.assertEqual([], list(real.iterdir()))

    def test_run_checks_notice_lines_stay_one_line_each(self):
        proc, out = self.kept_run([("fail", [sys.executable, "-c", "raise SystemExit(1)", str(self.root)])], out_name="new\nline.json")
        self.assertEqual(self.kept_notices(out, [("fail", 0)]), proc.stderr.splitlines())
        self.assertEqual(2, len(proc.stderr.splitlines()))
        self.assertTrue(proc.stderr.endswith("\n") and proc.stderr.count("\n") == 2)
        for stream in ("stdout", "stderr"):
            self.assertTrue((self.root / f"new\nline.json.0.{stream}").exists())

    def test_run_checks_keeps_masked_output_within_one_mebibyte(self):
        name = "WF_KEPT_" + "N" * 504
        self.assertEqual(512, len(name))
        code = "import sys; sys.stdout.write('k8bytes!\\n' * 100000); sys.exit(1)"
        proc, out = self.kept_run([("many", [sys.executable, "-c", code, str(self.root)])], env={name: "k8bytes!"})
        line = f"<環境変数 {name}>\n".encode()
        self.assertEqual(528, len(line))
        data = (self.root / "receipt.json.0.stdout").read_bytes()
        self.assertEqual(line * 1985, data)
        self.assertEqual(1048080, len(data))
        kept = json.loads(out.read_text())["results"][0]["kept_output"]["stdout"]
        self.assertEqual(900000, kept["total_bytes"])
        self.assertEqual(882135, kept["kept_from_byte"])

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

    def test_hold_reason_with_ascii_colon_is_preserved_in_record(self):
        self.begin()
        self.capture(phase="hold")
        reason = "S4: 承認の後に本文が変わった"
        self.proof = self.root / "hold-proof.json"
        self.guard(*self.context("attest"), "--hold-reason", reason,
                   "--hold-next", "設計レビューをやり直す", "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)
        proof = json.loads(self.proof.read_text())
        self.assertEqual(reason, proof["hold_reason"])
        self.assertEqual("UNAPPROVED", proof["verdict"])
        completed, expected = self.complete("pending")
        self.verify("--task-transition", "pending", "--expected-task", str(expected))
        holds = [line for line in completed.read_text().splitlines()
                 if line.startswith("- **保留**(ship-task・")]
        self.assertEqual(1, len(holds))
        self.assertTrue(holds[0].startswith(
            f"- **保留**(ship-task・{proof['record_date']}): {reason} — 設計レビューをやり直す / UNAPPROVED / "
        ), holds[0])

    def test_hold_reason_rejects_fullwidth_colon_and_parenthesis(self):
        self.begin()
        self.capture(phase="hold")
        proof = self.root / "hold-proof.json"
        for reason in ("S4：承認の後に本文が変わった", "S4(承認の後に本文が変わった)"):
            with self.subTest(reason=reason):
                result = self.guard(*self.context("attest"), "--hold-reason", reason,
                                    "--hold-next", "設計レビューをやり直す", "--out", str(proof), expect=2)
                self.assertIn("人に確かめる場面の番号で始まる理由", result.stderr)
                self.assertFalse(proof.exists())


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

    # 取り込みの commit M の後の doc の経路(Phase 4.5)。review-guard.py は変えず、今の動きを固定する。
    def merged_head(self, conflict=False):
        # 完了_guard.md を commit した後、作業ブランチと別のブランチに 1 commit ずつ作り、--no-ff で M を作る。
        # conflict なら同じ行を両方が変え、解いてから commit する。
        completed = self.task.with_name("完了_guard.md")
        self.task.rename(completed)
        self.task = completed
        self.git("add", "-A")
        self.git("commit", "-qm", "completed base")
        work = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        if conflict:
            (self.repo / "source.txt").write_text("side\n", encoding="utf-8")
        else:
            (self.repo / "side.txt").write_text("from side\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "side")
        self.side = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", work)
        if conflict:
            (self.repo / "source.txt").write_text("work\n", encoding="utf-8")
        else:
            (self.repo / "work.txt").write_text("from work\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "work")
        if conflict:
            merging = subprocess.run(["git", "merge", "--no-ff", "--no-edit", "side"], cwd=self.repo,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(1, merging.returncode, merging.stdout + merging.stderr)
            (self.repo / "source.txt").write_text("resolved\n", encoding="utf-8")
            self.git("add", "source.txt")
            self.git("commit", "-qm", "M")
        else:
            self.git("merge", "--no-ff", "--no-edit", "side")
        self.merge_commit = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(2, len(self.git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) - 1)

    def parents(self, rev="HEAD"):
        return self.git("rev-list", "--parents", "-n", "1", rev).stdout.split()[1:]

    def prove_with(self, code, columns=(r"(^|/)[.]env($|[.])",)):
        # prove() と同じ流れ。品質のコマンドを 1 本差し替え、受領書の終了コードを返す。
        eres = [item for ere in columns for item in ("--exclude-ere", ere)]
        context = lambda command: [command, "--cwd", str(self.repo), "--state", str(self.state),
                                   "--expect-state-sha256", self.state_sha256, *eres]
        plan = self.root / "merged-plan.json"
        plan.write_text(json.dumps({"commands": [{"name": "merged", "argv": [sys.executable, "-c", code]}]}))
        checks = self.root / "merged-checks.json"
        checks.unlink(missing_ok=True)
        self.guard(*context("run-checks"), "--checks-file", str(plan), "--expect-checks-sha256", self.digest(plan),
                   "--out", str(checks))
        codes = [item["exit_code"] for item in json.loads(checks.read_text())["results"]]
        manifest = json.loads((self.state / "manifest.json").read_text())
        reply = self.root / "merged-reply.json"
        reply.write_text(json.dumps({"reviewer": "reviewer", "verdict": "APPROVED", "issues": [],
                                     "review_binding_sha256": manifest["review_binding_sha256"]}))
        self.proof = self.root / "merged-evidence.json"
        self.proof.unlink(missing_ok=True)
        self.guard(*context("attest"), "--checks", str(checks), "--expect-checks-sha256", self.digest(checks),
                   "--review-result", str(reply), "--expect-review-sha256", self.digest(reply), "--out", str(self.proof))
        self.proof_hash = self.digest(self.proof)
        return codes, context

    def test_doc_path_checks_merged_tree_without_new_commit(self):
        self.merged_head()
        self.begin()
        self.capture(phase="doc", prove=False)
        codes, _ = self.prove_with("from pathlib import Path; assert Path('side.txt').exists() and Path('work.txt').exists()")
        self.assertEqual([0], codes)
        self.verify("--mode", "pre-stage")
        self.assertEqual(self.merge_commit, self.git("rev-parse", "HEAD").stdout.strip())

    def test_doc_path_commits_empty_review_after_resolved_merge(self):
        self.merged_head(conflict=True)
        self.begin()
        self.capture(phase="doc")
        self.git("commit", "-q", "--allow-empty", "-m", "P")
        self.verify("--mode", "commit")
        pr = self.guard(*self.context("pr-evidence"), "--evidence", str(self.proof),
                        "--expect-evidence-sha256", self.proof_hash)
        self.assertIn("APPROVED", pr.stdout)
        self.assertEqual([self.merge_commit], self.parents())

    def test_doc_path_commits_file_change_directly_on_merge(self):
        self.merged_head()
        self.begin()
        (self.repo / "work.txt").write_text("changed by the step\n", encoding="utf-8")
        self.capture(phase="doc")
        self.git("add", "-A")
        self.verify("--mode", "stage")
        self.git("commit", "-qm", "P")
        self.verify("--mode", "commit")
        self.assertEqual([self.merge_commit], self.parents())

    def test_doc_path_rejects_second_parent_after_review(self):
        self.merged_head()
        self.begin()
        self.capture(phase="doc")
        forged = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-p", self.side, "-m", "forged").stdout.strip()
        self.git("reset", "-q", "--hard", forged)
        self.assertEqual(2, len(self.parents()))
        self.verify("--mode", "commit", expect=1)

    def test_doc_path_take_with_added_secret_keeps_it_out_of_checks(self):
        # foo.txt は取り込みの前から追跡している(merged_head の最初の commit に入る)。start の HEAD は M。
        (self.repo / "foo.txt").write_text("public\n", encoding="utf-8")
        self.merged_head()
        self.assertEqual(self.merge_commit, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(2, len(self.parents()))
        self.begin()
        # review-guard.py は profile を読まない。profile に足した機密の glob は、ERE の列の後ろへ足して渡す。
        base, extra = r"(^|/)[.]env($|[.])", r"^foo[.]txt$"
        (self.repo / "foo.txt").write_text("NEW SECRET\n", encoding="utf-8")
        taken = self.guard("take", "--cwd", str(self.repo), "--state", str(self.state), "--task-md", str(self.task),
                           "--start-state", str(self.start_state), "--expect-start-sha256", self.start_sha256,
                           "--exclude-ere", base, "--exclude-ere", extra, "--phase", "doc")
        self.state_sha256 = next(line.split("=", 1)[1] for line in taken.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        seal = ["seal", "--cwd", str(self.repo), "--state", str(self.state), "--expect-state-sha256", self.state_sha256,
                "--review-input", str(self.review)]
        self.guard(*seal, "--exclude-ere", base, expect=2)
        sealed = self.guard(*seal, "--exclude-ere", base, "--exclude-ere", extra)
        self.state_sha256 = next(line.split("=", 1)[1] for line in sealed.stdout.splitlines()
                                 if line.startswith("STATE_SHA256="))
        codes, _ = self.prove_with("from pathlib import Path; assert not Path('foo.txt').exists()", columns=(base, extra))
        self.assertEqual([0], codes)
        verify = ["verify", "--cwd", str(self.repo), "--state", str(self.state), "--expect-state-sha256", self.state_sha256,
                  "--exclude-ere", base, "--exclude-ere", extra, "--evidence", str(self.proof),
                  "--expect-evidence-sha256", self.proof_hash, "--mode", "stage"]
        # 対照: foo.txt をステージする前は通る。次の expect=1 の理由は、機密のパスがステージされたことだけ。
        self.guard(*verify)
        self.git("add", "foo.txt")
        self.guard(*verify, expect=1)


class ExternalTaskDirectoryTest(unittest.TestCase):
    """Git 呼出しを差し替え、外部本文と task_dir の境界だけを検査する。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wf-external-task-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.body = self.root / "issue.txt"
        self.body.write_text("# task\n\n- [ ] work\n\n## 追加修正記録\n", encoding="utf-8")
        self.state = self.root / "state"
        self.start_state = self.root / "start"
        spec = importlib.util.spec_from_file_location("review_guard_external_task", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("repo_root", self.repo), ("walk", ([], [])), ("git_index", ([], [])),
                            ("tree", ([], [])), ("ignored", []), ("external_ignore", [])):
            self.stack.enter_context(mock.patch.object(self.module, name, return_value=value))
        self.stack.enter_context(mock.patch.object(self.module, "git", side_effect=self.git))

    def git(self, root, *args, input_data=None, **kwargs):
        if args == ("rev-parse", "HEAD"):
            return b"a" * 40 + b"\n"
        if args == ("ls-files", "-o", "--exclude-standard", "-z"):
            return b""
        if args == ("hash-object", "--stdin"):
            return hashlib.sha1(input_data).hexdigest().encode() + b"\n"
        raise AssertionError("試験で許していない Git 呼出し: " + repr(args))

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def args(self, command, *extra, external=True):
        values = [command, "--cwd", str(self.repo), "--state", str(self.start_state if command == "start" else self.state),
                  "--exclude-ere", r"(^|/)private($|/)"]
        if external:
            values.extend(["--task-body", str(self.body), "--expect-task-body-sha256", self.digest(self.body)])
            if command == "start":
                values.extend(["--task-id", "issue-320"])
        if command == "take":
            values.extend(["--start-state", str(self.start_state), "--expect-start-sha256",
                           self.digest(self.module.start_manifest(self.start_state))])
        elif command != "start":
            values.extend(["--expect-state-sha256", self.digest(self.module.state_file(self.state))])
        return self.module.parser().parse_args([*values, *extra])

    def call(self, command, *extra, external=True):
        with contextlib.redirect_stdout(io.StringIO()):
            return getattr(self.module, command)(self.args(command, *extra, external=external))

    def test_without_directory_start_take_seal_complete_and_verify(self):
        self.assertEqual(self.call("start"), 0)
        started = json.loads(self.module.start_manifest(self.start_state).read_text())
        self.assertEqual(started["task_source"], {"task_id": "issue-320", "task_dir": None})
        self.assertEqual(started["task_files"], [])
        self.assertEqual(self.call("take"), 0)
        review = self.root / "review.txt"
        review.write_text("review input\n", encoding="utf-8")
        self.assertEqual(self.call("seal", "--review-input", str(review)), 0)
        saved = self.module.load_state(self.state, self.digest(self.module.state_file(self.state)))
        proof = self.root / "proof.json"
        proof.write_text(json.dumps({"phase": "implementation", "review_binding_sha256": saved["review_binding_sha256"],
                                    "verdict": "APPROVED", "record_date": "2026-11-04"}), encoding="utf-8")
        evidence = ["--evidence", str(proof), "--expect-evidence-sha256", self.digest(proof)]
        expected = self.root / "expected.txt"
        self.assertEqual(self.call("finalize", "--transition", "complete", "--out", str(expected), *evidence), 0)
        self.body.write_bytes(expected.read_bytes())
        transition = ["--task-transition", "complete", "--expected-task", str(expected),
                      "--expect-task-sha256", self.digest(expected), *evidence]
        self.assertEqual(self.call("verify", *transition), 0)
        self.assertEqual(self.call("verify", "--mode", "stage", *transition), 0)
        self.assertEqual(list(self.repo.iterdir()), [])
        self.body.write_bytes(self.body.read_bytes().replace(b"work", b"changed"))
        self.assertEqual(self.call("verify", *transition), 1)

    def test_existing_directory_keeps_other_task_protection(self):
        directory = self.repo / "tasks"
        directory.mkdir()
        other = directory / "進行中_other.md"
        other.write_text("before", encoding="utf-8")
        self.call("start", "--task-dir", str(directory))
        started = json.loads(self.module.start_manifest(self.start_state).read_text())
        self.assertEqual(started["task_source"]["task_dir"], "tasks")
        self.assertEqual(len(started["task_files"]), 1)
        other.write_text("after", encoding="utf-8")
        with self.assertRaisesRegex(self.module.GuardError, "対象外の状態名"):
            self.call("take")

    def test_explicit_directory_cannot_be_missing_or_linked(self):
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.repo / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        for directory in (self.repo / "missing", linked, outside):
            with self.subTest(directory=directory):
                with self.assertRaises(self.module.GuardError):
                    self.call("start", "--task-dir", str(directory))

    def test_source_identity_cannot_change_after_start(self):
        self.call("start")
        directory = self.repo / "tasks"
        directory.mkdir()
        for arguments in (("--task-dir", str(directory)), ("--task-id", "issue-321")):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(self.module.GuardError, "開始時と違う"):
                    self.call("take", *arguments)

    def test_omitting_existing_directory_does_not_disable_its_validation(self):
        directory = self.repo / "tasks"
        directory.mkdir()
        self.call("start", "--task-dir", str(directory))
        directory.rmdir()
        with self.assertRaisesRegex(self.module.GuardError, "task_dir"):
            self.call("take")

    def test_local_task_still_scans_sibling_tasks_without_task_dir_flag(self):
        directory = self.repo / "tasks"
        directory.mkdir()
        task = directory / "進行中_local.md"
        task.write_bytes(self.body.read_bytes())
        other = directory / "完了_other.md"
        other.write_text("before", encoding="utf-8")
        self.call("start", "--task-md", str(task), external=False)
        started = json.loads(self.module.start_manifest(self.start_state).read_text())
        self.assertIsNone(started["task_source"])
        self.assertEqual(len(started["task_files"]), 2)
        other.write_text("after", encoding="utf-8")
        with self.assertRaisesRegex(self.module.GuardError, "対象外の状態名"):
            self.call("take", external=False)

    def test_external_body_still_requires_id_and_matching_bytes(self):
        args = self.args("start")
        args.task_id = None
        with self.assertRaisesRegex(self.module.GuardError, "task-id"):
            self.module.start(args)
        args = self.args("start")
        args.expect_task_body_sha256 = "0" * 64
        with self.assertRaisesRegex(self.module.GuardError, "sha256"):
            self.module.start(args)


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
            self.assertEqual([], list(self.root.glob("receipt.json*")))
            self.assertTrue(copy.exists())
            self.assertFalse(later.exists())
            return
        if case == "timeout":
            self.assertEqual(1, running.returncode, stderr)
            self.assertEqual(1, len(receipt["results"]))
            self.assertEqual("timeout", receipt["results"][0]["error"])
            self.assertEqual("receipt.json.0.stdout", receipt["results"][0]["kept_output"]["stdout"]["file"])
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

    def invoke(self, codes, timeout=1, current=None):
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
        self.notices = io.StringIO()
        check = {"return_value": True} if current is None else {"side_effect": current}
        with mock.patch.object(self.guard.tempfile, "mkdtemp", side_effect=make_directory), \
             mock.patch.object(self.guard, "read_context", return_value=(self.root, self.saved, [])), \
             mock.patch.object(self.guard, "check_current", **check), \
             mock.patch.object(self.guard, "public_path"), \
             mock.patch.object(self.guard, "safe_output"), \
             mock.patch.object(self.guard, "output_json", side_effect=lambda p, value, *_: p.write_text(json.dumps(value))), \
             contextlib.redirect_stderr(self.notices):
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
        self.assertEqual(b'output\n', (self.root / 'receipt.json.0.stdout').read_bytes())
        self.assertNotIn('kept_output', results[1])
        self.assertIn('品質コマンドの出力を残した', self.notices.getvalue())

    def test_kept_output_is_written_only_after_the_set_is_unchanged(self):
        with self.assertRaisesRegex(self.guard.GuardError, "品質実行中に集合が変わった"):
            self.invoke(["raise SystemExit(5)"], current=[True, False])
        self.assertEqual([], list(self.root.glob("receipt.json*")))

    def test_kept_output_write_failure_writes_no_receipt(self):
        with mock.patch.object(self.guard, "create_bytes", side_effect=self.guard.GuardError("injected")):
            with self.assertRaisesRegex(self.guard.GuardError, "injected"):
                self.invoke(["raise SystemExit(5)"])
        self.assertFalse((self.root / "receipt.json").exists())

    def test_retained_copy_writes_no_kept_output_even_after_a_failed_command(self):
        real = self.process_helper.run_command
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise self.process_helper.RecoveryError("not absent")
            return real(*args, **kwargs)
        with mock.patch.object(self.guard, "check_process_module", return_value=self.process_helper), \
             mock.patch.object(self.process_helper, "run_command", side_effect=run):
            with self.assertRaisesRegex(self.guard.GuardError, "検証用コピーを保持"):
                self.invoke(["raise SystemExit(3)", "pass"])
        self.assertEqual([], list(self.root.glob("receipt.json*")))

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
        self.assert_retained(caught, "プロセスグループの不在を確認できない", later, before)


class KeptOutputRuleTest(unittest.TestCase):
    """残す出力の置き換えと切り方の規則。足した関数を直接呼ぶ。"""
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("kept_output_guard", SCRIPT)
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    @staticmethod
    def label(name):
        return f"<環境変数 {name}>".encode()

    def test_mask_table_uses_long_values_lines_and_copy_paths(self):
        environ = {"B_SAME": "same-value-123", "A_SAME": "same-value-123", "SHORT": "1234567",
                   "LINES": "line-one-aaaa\nline-two-bbbb\nxx", "SPACES": " " * 12,
                   "F_PADDED": " \tpadded-value-1 \r"}
        table = self.guard.mask_table(environ, [Path("/c1"), Path("/real/copy-2")])
        copy = "<検証用コピー>".encode()
        self.assertEqual({b"/c1": copy, b"/real/copy-2": copy,
                          b"same-value-123": self.label("A_SAME"),
                          b"line-one-aaaa\nline-two-bbbb\nxx": self.label("LINES"),
                          b"line-one-aaaa": self.label("LINES"), b"line-two-bbbb": self.label("LINES"),
                          b"padded-value-1": self.label("F_PADDED")}, table)
        self.assertEqual("x= <環境変数 F_PADDED> .".encode(), self.guard.mask_output(b"x= padded-value-1 .", table))

    def test_mask_output_prefers_longer_values_and_does_not_mask_tokens_again(self):
        table = self.guard.mask_table({"LONGER": "/x/y/z/w/v/u", "PYTHONPATH": "/x/y/z/w/v", "VAR_NAME": "PYTHONPATH"}, [])
        self.assertEqual("<環境変数 LONGER> <環境変数 PYTHONPATH> <環境変数 VAR_NAME>".encode(),
                         self.guard.mask_output(b"/x/y/z/w/v/u /x/y/z/w/v PYTHONPATH", table))
        self.assertEqual(b"anything", self.guard.mask_output(b"anything", {}))

    def test_kept_part_starts_at_a_line_when_cut(self):
        self.assertEqual((b"abc\ndef\n", 0), self.guard.kept_part(b"abc\ndef\n", 8, {}))
        self.assertEqual((b"rest\n", 95), self.guard.kept_part(b"partial\nrest\n", 100, {}))
        self.assertEqual((b"", 100), self.guard.kept_part(b"onlypartial", 100, {}))

    def test_mask_output_covers_overlapping_values_and_a_cut_end(self):
        table = self.guard.mask_table({"A_PREFIX": "abcdefgh", "SECRET": "efghSECRET-VALUE-123"}, [])
        self.assertEqual("<環境変数 A_PREFIX><環境変数 SECRET> end".encode(),
                         self.guard.mask_output(b"abcdefghSECRET-VALUE-123 end", table))
        self.assertEqual("x <環境変数 SECRET>".encode(), self.guard.mask_output(b"x efghSECR", table))
        self.assertEqual(b"x efghSEC", self.guard.mask_output(b"x efghSEC", table))

    def test_kept_part_narrows_to_one_mebibyte_after_masking(self):
        name = "WF_KEPT_" + "N" * 504
        table = self.guard.mask_table({name: "k8bytes!"}, [])
        self.assertEqual((b"", 1048576), self.guard.kept_part(b"k8bytes!" * 131072, 1048576, table))
        data = b"k8bytes!\n" * 100000
        line = self.label(name) + b"\n"
        self.assertEqual((line * 1985, 882135), self.guard.kept_part(data, len(data), table))

    def test_mask_table_keeps_newlines_out_of_tokens(self):
        table = self.guard.mask_table({"A\nB": "nl-value"}, [])
        self.assertEqual({b"nl-value": "<環境変数 A\\nB>".encode()}, table)
        self.assertNotIn(b"\n", table[b"nl-value"])
        data = b"nl-value\n" * 60000
        kept, start = self.guard.kept_part(data, len(data), table)
        self.assertEqual(table[b"nl-value"] + b"\n", kept[:20])
        self.assertEqual((table[b"nl-value"] + b"\n") * 52428, kept)
        self.assertEqual(1048560, len(kept))
        self.assertEqual(68148, start)
        self.assertEqual(b"\n"[0], data[start - 1])

    def test_mask_output_covers_a_value_cut_at_the_start(self):
        table = self.guard.mask_table({"PEM": "-----BEGIN-----\nAAAAAAAAAAAA\nBBBBBBBBBBBB\nTU4="}, [])
        self.assertEqual("<環境変数 PEM>\nnext\n".encode(), self.guard.mask_output(b"BBBBBBBBBBBB\nTU4=\nnext\n", table))
        self.assertEqual(b"TU4=\nnext\n", self.guard.mask_output(b"TU4=\nnext\n", table))

    def test_mask_output_prefers_the_longest_fragment_across_values_at_the_end(self):
        table = self.guard.mask_table({"A": "12345678abcdefghijklmnopqrstuvwx", "B": "SECRET-VALUE-12345678Z"}, [])
        self.assertEqual(self.label("B"), self.guard.mask_output(b"SECRET-VALUE-12345678", table))

    def test_mask_output_prefers_the_longest_fragment_across_values_at_the_start(self):
        table = self.guard.mask_table({"A": "x" * 40 + "HELLOWOR", "B": "pre-HELLOWORLDSECRET123"}, [])
        self.assertEqual(self.label("B") + b" rest\n", self.guard.mask_output(b"HELLOWORLDSECRET123 rest\n", table))

    def test_kept_part_masks_a_cut_multiline_value_even_when_a_longer_value_competes(self):
        table = self.guard.mask_table({"PEM": "AAAA\nBBBBBBBBBBBB\nTU4=", "LONG": "x" * 40 + "BBBBBBBB"}, [])
        data = b"AAAA\nBBBBBBBBBBBB\nTU4=\nnext\n"
        self.assertEqual(self.label("PEM") + b"\nnext\n", self.guard.kept_part(data, 10 ** 7, table)[0])

    def test_kept_part_cuts_masked_output_at_its_own_line_starts(self):
        a, b = "A_" + "N" * 510, "B_" + "N" * 510
        value = "L0000000\nL0000001\nL0000002"
        table = self.guard.mask_table({a: "k8bytes!", b: value}, [])
        data = value.encode() * 2000 + b"\nFAIL\nk8bytes!\ndiagnostic\n"
        self.assertEqual(52026, len(data))
        kept, start = self.guard.kept_part(data, len(data), table)
        self.assertEqual(b"FAIL\n" + self.label(a) + b"\ndiagnostic\n", kept)
        self.assertEqual(544, len(kept))
        self.assertEqual(52001, start)


if __name__ == "__main__":
    unittest.main()
