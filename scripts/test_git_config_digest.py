import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "plugins/dev-workflow/skills/ship-task/scripts/git-config-digest.py"

GIT_TIMEOUT = 10  # スクリプトが git の子プロセスを打ち切る秒数
MARGIN = 30  # 打ち切りの後に待つ余裕。これを過ぎたらプロセスグループごと止めて落とす
DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\n\Z")
SECRET = "SECRETTOKEN"


class GitConfigDigestTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repo = self.base / "repo"
        self.fifos = []
        # 後に足した後片付けが先に走る: 一時領域を消す前に FIFO の待ちを解く
        self.addCleanup(self.release_fifos)
        self.gitconfig = self.base / "gitconfig"
        self.gitconfig.write_text(
            "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        self.git("init", "-q", str(self.repo), cwd=self.base)
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "a")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")

    # ── 道具 ──
    def git(self, *args, cwd=None):
        done = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env, check=True,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return done.stdout

    def git_path(self, name, cwd=None):
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-path", name, cwd=cwd).strip())

    def common(self, cwd=None):
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip())

    def worktree(self, name="wt"):
        path = self.base / name
        self.git("worktree", "add", "-q", "--detach", str(path))
        return path

    def make_fifo(self, path):
        if os.path.lexists(path):
            os.remove(path)
        os.mkfifo(path)
        self.fifos.append(path)

    def release_fifos(self):
        # 読み手が open で待っていれば、書き手として開いて待ちを解く(読み手がいなければ ENXIO)
        for fifo in self.fifos:
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                continue
            os.close(fd)

    def run_raw(self, args):
        # 試験自体が戻らなくならないように: 別のセッションで起動し、時間切れならプロセスグループごと止める
        proc = subprocess.Popen([sys.executable, str(SCRIPT), *args], env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=GIT_TIMEOUT + MARGIN)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.release_fifos()
            proc.communicate()
            self.fail(f"{GIT_TIMEOUT + MARGIN} 秒で終わらない(git の打ち切りが効いていない)")
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    def run_script(self, *args, directory=None):
        return self.run_raw([f"--dir={directory or self.repo}", *args])

    def digest(self, directory=None):
        done = self.run_script(directory=directory)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertRegex(done.stdout, DIGEST)
        return done.stdout.strip()

    def assert_times_out(self, directory=None):
        done = self.run_script(directory=directory)
        self.assertEqual(2, done.returncode, done.stderr)
        self.assertEqual("", done.stdout)
        self.assertIn("時間切れ", done.stderr)

    # ── 同じ状態・--expect ──
    def test_same_state_gives_same_value(self):
        self.assertEqual(self.digest(), self.digest())

    def test_expect_exit_codes(self):
        value = self.digest()
        done = self.run_script(f"--expect={value}")
        self.assertEqual((0, ""), (done.returncode, done.stdout), done.stderr)
        other = "sha256:" + ("1" if value.endswith("0") else "0") * 64
        done = self.run_script(f"--expect={other}")
        self.assertEqual((1, ""), (done.returncode, done.stdout), done.stderr)
        hexpart = value[len("sha256:"):]
        for bad in ("", hexpart, "sha256:" + hexpart[:-1], value + "0", "sha256:" + hexpart.upper(),
                    "SHA256:" + hexpart, "sha1:" + hexpart, " " + value):
            with self.subTest(expect=bad):
                done = self.run_script(f"--expect={bad}")
                self.assertEqual((2, ""), (done.returncode, done.stdout), done.stderr)

    # ── 変わる ──
    def test_config_changes_are_seen(self):
        wt = self.worktree()
        included = self.base / "included"
        included.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
        changes = [
            ("remote.origin.pushurl", ["remote.origin.pushurl", "https://github.com/evil/r.git"]),
            ("remote.origin.push", ["remote.origin.push", "refs/heads/task/x:refs/heads/main"]),
            ("core.hooksPath", ["core.hooksPath", str(self.base / "hooks")]),
            ("credential.helper", ["credential.helper", "store"]),
            ("include.path", ["include.path", str(included)]),
        ]
        before = self.digest(wt)
        for name, args in changes:
            with self.subTest(change=name):
                self.git("config", *args, cwd=wt)
                after = self.digest(wt)
                self.assertNotEqual(before, after)
                before = after

    def test_worktree_config_creation_and_rewrite_are_seen(self):
        self.git("config", "extensions.worktreeConfig", "true")
        wt = self.worktree()
        shared = self.common() / "config"
        shared_bytes = shared.read_bytes()
        path = self.git_path("config.worktree", cwd=wt)
        self.assertFalse(os.path.lexists(path))
        before = self.digest(wt)
        with self.subTest(change="新設"):
            self.git("config", "--worktree", "core.hooksPath", str(self.base / "hooks"), cwd=wt)
            self.assertTrue(path.is_file())
            created = self.digest(wt)
            self.assertNotEqual(before, created)
        with self.subTest(change="書き換え"):
            self.git("config", "--worktree", "core.hooksPath", str(self.base / "hooks2"), cwd=wt)
            self.assertNotEqual(created, self.digest(wt))
        # 前提: 共有の config は変わっていない(変わったのは config.worktree だけ)
        self.assertEqual(shared_bytes, shared.read_bytes())

    def test_gitdir_redirect_is_seen(self):
        wt = self.worktree()
        before = self.digest(wt)
        common = self.common()
        copy = self.base / "copy.git"
        shutil.copytree(common, copy, symlinks=True)
        (wt / ".git").write_text(f"gitdir: {copy / 'worktrees' / wt.name}\n", encoding="utf-8")
        # 前提: 同じ中身の config を持つ写しへ向いた
        self.assertEqual(copy.resolve(), self.common(cwd=wt).resolve())
        self.assertEqual((common / "config").read_bytes(), (copy / "config").read_bytes())
        self.assertNotEqual(before, self.digest(wt))

    def test_symlinked_shared_config(self):
        config = self.common() / "config"
        first = self.base / "config-1"
        second = self.base / "config-2"
        for target in (first, second):
            shutil.copyfile(config, target)
        before = self.digest()
        with self.subTest(change="symlink にする"):
            os.remove(config)
            os.symlink(first, config)
            linked = self.digest()
            self.assertNotEqual(before, linked)
        with self.subTest(change="指す先"):
            os.remove(config)
            os.symlink(second, config)
            repointed = self.digest()
            self.assertNotEqual(linked, repointed)
        with self.subTest(change="指す先の中身"):
            with open(second, "a", encoding="utf-8") as fh:
                fh.write('[remote "origin"]\n\tpushurl = https://github.com/evil/r.git\n')
            self.assertNotEqual(repointed, self.digest())

    def test_symlinked_worktree_config(self):
        # config.worktree の記述は、解く前の置き場(<git-dir>/config.worktree)の種類・symlink の字面・指す先の項目の並び
        self.git("config", "extensions.worktreeConfig", "true")
        first = self.base / "wtconfig-1"
        second = self.base / "wtconfig-2"
        for target in (first, second):
            target.write_text("[core]\n\thooksPath = a\n", encoding="utf-8")
        path = self.git_path("config.worktree")
        os.symlink(first, path)
        before = self.digest()
        with self.subTest(change="指す先の中身"):
            first.write_text("[core]\n\thooksPath = b\n", encoding="utf-8")
            changed = self.digest()
            self.assertNotEqual(before, changed)
        with self.subTest(change="指す先"):
            second.write_text("[core]\n\thooksPath = b\n", encoding="utf-8")
            os.remove(path)
            os.symlink(second, path)
            self.assertNotEqual(changed, self.digest())

    def test_worktree_config_symlink_literal_is_seen(self):
        # 同じ実ファイルを指したまま、symlink の字面だけを変える(絶対 → 相対)。main と linked worktree の両方
        self.git("config", "extensions.worktreeConfig", "true")
        wt = self.worktree()
        for name, cwd in (("main", self.repo), ("linked", wt)):
            with self.subTest(worktree=name):
                target = self.base / f"wtconfig-{name}"
                target.write_text("[core]\n\thooksPath = a\n", encoding="utf-8")
                path = self.git_path("config.worktree", cwd=cwd)
                os.symlink(target, path)
                absolute = self.digest(cwd)
                os.remove(path)
                os.symlink(os.path.relpath(target, path.parent), path)
                # 前提: 同じ実ファイルを指し、git が返す置き場(--git-path)も同じ
                self.assertEqual(target.resolve(), path.resolve())
                self.assertEqual(target.resolve(), self.git_path("config.worktree", cwd=cwd).resolve())
                self.assertNotEqual(absolute, self.digest(cwd))

    # ── 変わらない ──
    def test_round_operations_do_not_change(self):
        with open(self.gitconfig, "a", encoding="utf-8") as fh:
            fh.write("[branch]\n\tautoSetupMerge = always\n")
        before = self.digest()

        def add():
            (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
            self.git("add", "b.txt")

        steps = [
            ("switch --no-track -c", lambda: self.git("switch", "-q", "--no-track", "-c", "task/x")),
            ("add", add),
            ("commit", lambda: self.git("commit", "-q", "-m", "b")),
            ("status", lambda: self.git("status", "--porcelain")),
        ]
        for name, step in steps:
            with self.subTest(step=name):
                step()
                self.assertEqual(before, self.digest())
        # 前提: 追跡つきで作ると共有の config に書く(--no-track を外した対照)
        self.git("switch", "-q", "-c", "task/y")
        self.assertNotEqual(before, self.digest())

    def test_same_value_rewrite_is_not_a_change(self):
        config = self.common() / "config"
        with open(config, "a", encoding="utf-8") as fh:
            fh.write("[core]\n\thookspath = .husky/_\n")  # 手で書いた小文字のキー
        written = config.read_bytes()
        before = self.digest()
        self.git("config", "core.hooksPath", ".husky/_")
        self.git("config", "remote.origin.url", "https://github.com/o/r.git")
        self.assertNotEqual(written, config.read_bytes())  # 前提: バイト列は書き直された
        self.assertEqual(before, self.digest())

    def test_linked_worktree_without_extension(self):
        wt = self.worktree()
        # 前提: 拡張が無いと git config --worktree は失敗する
        done = subprocess.run(["git", "config", "--worktree", "--list"], cwd=wt, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, check=False)
        self.assertNotEqual(0, done.returncode)
        self.digest(wt)

    def test_non_regular_worktree_config_is_not_given_to_git(self):
        # 拡張が無いので git 自身は config.worktree を読まない。FIFO を git に渡すと戻らない
        self.make_fifo(self.git_path("config.worktree"))
        self.digest()

    # ── 戻らない git ──
    def test_fifo_shared_config(self):
        self.digest()  # 対照
        self.make_fifo(self.common() / "config")
        self.assert_times_out()

    def test_fifo_worktree_config_with_extension(self):
        self.git("config", "extensions.worktreeConfig", "true")
        path = self.git_path("config.worktree")
        self.digest()  # 対照
        self.make_fifo(path)
        self.assert_times_out()

    def test_fifo_include_target(self):
        fifo = self.base / "included"
        self.git("config", "include.path", str(fifo))
        self.digest()  # 対照: include の先が無いうちは読める
        self.make_fifo(fifo)
        self.assert_times_out()

    # ── 出力 ──
    def test_values_are_not_printed(self):
        url = f"https://x-access-token:{SECRET}@github.com/o/r.git"
        self.git("config", "remote.origin.pushurl", url)
        self.digest()
        outputs = [self.run_script(), self.run_script("--expect=sha256:" + "0" * 64)]
        # git の失敗: 読めない config.worktree(拡張が無いので、読むのはスクリプトが打つ git だけ)
        self.git_path("config.worktree").write_text(f'[remote "origin"]\n\tpushurl = "{url}\n',
                                                     encoding="utf-8")
        failed = self.run_script()
        self.assertEqual(2, failed.returncode, failed.stderr)
        outputs.append(failed)
        # git の失敗(git の stderr が値を含む): 共有の config の core.abbrev に値を置くと、git が値を含む fatal で止まる
        shared = self.common() / "config"
        with open(shared, "a", encoding="utf-8") as fh:
            fh.write(f"[core]\n\tabbrev = {SECRET}\n")
        probe = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.repo, env=self.env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        # 前提: git 自身の stderr は値を含む(この試験が stderr への漏れを捉えうる)
        self.assertNotEqual(0, probe.returncode)
        self.assertIn(SECRET, probe.stderr)
        fatal = self.run_script()
        self.assertEqual(2, fatal.returncode, fatal.stderr)
        outputs.append(fatal)
        for done in outputs:
            for stream in (done.stdout, done.stderr):
                self.assertNotIn(SECRET, stream)
                self.assertNotIn("x-access-token", stream)

    def test_usage_errors(self):
        self.digest()  # 対照
        plain = self.base / "plain"
        plain.mkdir()
        regular = self.base / "regular"
        regular.write_text("x\n", encoding="utf-8")
        for directory in (self.base / "missing", regular, plain):
            with self.subTest(directory=directory.name):
                done = self.run_script(directory=str(directory))
                self.assertEqual((2, ""), (done.returncode, done.stdout), done.stderr)
                self.assertNotEqual("", done.stderr)
        done = self.run_raw([])
        self.assertEqual((2, ""), (done.returncode, done.stdout), done.stderr)


if __name__ == "__main__":
    unittest.main()
