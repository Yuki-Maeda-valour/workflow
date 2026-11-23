#!/usr/bin/env python3
"""S3I-A の比較器自身の検査。check_implement_guard.py の中から必ず流す(subject に比較器の本体が入る)。

正しい例(今回ビルドした検査専用入口)を先に通し、次に 1 か所ずつ壊した偽の入口(追加の決定 4 の名前を
受け継いだ値と違う値で渡すものを含む)・時間切れ・実行不能・0 件・
境界の印の誤用・版の違う道具の文・musl の strerror の文・selftest の許す失敗の一覧の外れと中の成功・
Rust を通った行の不足を与え、すべて失敗になることを確かめる。bash 4.0 以上が無い Mac をまねた範囲 mac・signed
(偽の bash 3.2 を PATH の先頭に置く)で、Mac の固定期待の全件が比べられて合格することも確かめる。偽の入口は C で組み、本物を起動してから
(または起動の前に)1 か所だけを変える。詰めた FIFO の段の見分け方(偽の /proc)と、段の始めの試しの各経路・
待ち切りの上限・main の配線(試しの失敗は問題、write の番号を持たない機械は未実施)も確かめる。
"""
import contextlib
import dataclasses
import importlib.util
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

subject = None  # check_implement_guard(比較器が入れる。単独で流すときは読み込む)
MAC = sys.platform == 'darwin'
MAC_SKIP = MAC
NO_BASH4 = 'bash 4.0 以上が無い(旧版を流せない)'
MAC_RESTORE = ('Mac は restore-taskmd の停止だけを比べる(旧 helper の子の環境の __CF_USER_TEXT_ENCODING と '
               'cp の観測器の扱いを比べない。テスト計画の「Mac」)')


def get_subject():
    global subject
    if subject is None:
        spec = importlib.util.spec_from_file_location('implement_guard_subject',
                                                      Path(__file__).with_name('check_implement_guard.py'))
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


OLD_AS_FIXTURE_C = r'''/* 単独で流すときの正しい例: 旧版をそのまま起動する入口(比較は旧版どうしになる)。 */
#include <unistd.h>
int main(int argc, char **argv) {
    char *args[argc + 3];
    args[0] = "bash"; args[1] = OLD;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = 0;
    execv(BASH, args);
    return 127;
}
'''

WRAP_C = r'''/* 壊した偽の入口。本物(REAL)を子として起動し、MODE の 1 か所だけを変える。 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include <fcntl.h>
extern char **environ;

static size_t slurp(int fd, char *buf, size_t cap) {
    size_t n = 0; ssize_t r;
    while (n < cap && (r = read(fd, buf + n, cap - n)) > 0) n += (size_t)r;
    return n;
}

static void put(int fd, const char *buf, size_t n) {
    while (n > 0) { ssize_t w = write(fd, buf, n); if (w <= 0) return; buf += w; n -= (size_t)w; }
}

static const char *arg_after(int argc, char **argv, const char *name) {
    for (int i = 1; i + 1 < argc; i++) if (strcmp(argv[i], name) == 0) return argv[i + 1];
    return NULL;
}

int main(int argc, char **argv) {
    const char *mode = MODE;
    argv[0] = REAL;
    if (strcmp(mode, "env") == 0) { setenv("S3IA_EXTRA", "1", 1); execv(REAL, argv); return 127; }
    if (strcmp(mode, "decision4") == 0) {
        /* 追加の決定 4 の名前を、受け継いだ値と違う値で子へ渡す。 */
        const char *v = getenv("CAP"); char buf[4096];
        snprintf(buf, sizeof buf, "%s-changed", v ? v : "");
        setenv("CAP", buf, 1); execv(REAL, argv); return 127;
    }
    if (strcmp(mode, "decision4-add") == 0) { setenv("helper", "/added", 1); execv(REAL, argv); return 127; }
    if (strcmp(mode, "tool") == 0) {
        /* ほかの道具の起動: 本物を exec する前に、PATH の上の sha256sum を 1 回起動する。起動できなければ失敗する。 */
        pid_t tool = fork();
        if (tool == 0) {
            int null = open("/dev/null", O_RDWR);
            if (null >= 0) { dup2(null, 0); dup2(null, 1); dup2(null, 2); }
            char *tool_argv[] = {"sha256sum", "/dev/null", NULL};
            execvp("sha256sum", tool_argv);
            _exit(127);
        }
        int tool_status = 0;
        waitpid(tool, &tool_status, 0);
        if (!WIFEXITED(tool_status) || WEXITSTATUS(tool_status) != 0) { put(2, "tool missing\n", 13); return 127; }
        execv(REAL, argv);
        return 127;
    }
    if (strcmp(mode, "boundary") == 0) { return 99; }
    if (strcmp(mode, "sleep") == 0) { sleep(200); }
    if (strcmp(mode, "git") == 0) { if (system("git --version >/dev/null 2>&1") < 0) return 126; execv(REAL, argv); return 127; }
    int out[2], err[2];
    if (pipe(out) || pipe(err)) return 126;
    pid_t pid = fork();
    if (pid == 0) {
        dup2(out[1], 1); dup2(err[1], 2); close(out[0]); close(err[0]); close(out[1]); close(err[1]);
        execv(REAL, argv); _exit(127);
    }
    close(out[1]); close(err[1]);
    static char obuf[1 << 20], ebuf[1 << 20];
    size_t on = slurp(out[0], obuf, sizeof obuf), en = slurp(err[0], ebuf, sizeof ebuf);
    int status = 0; waitpid(pid, &status, 0);
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    if (strcmp(mode, "exit") == 0) code = code + 1;
    if (strcmp(mode, "stdout") == 0 && on > 0) { obuf[0] = obuf[0] == 'X' ? 'Y' : 'X'; }
    if (strcmp(mode, "stderr") == 0) { if (en > 0) ebuf[0] = ebuf[0] == 'X' ? 'Y' : 'X'; else { memcpy(ebuf, "x\n", 2); en = 2; } }
    if (strcmp(mode, "musl") == 0 || strcmp(mode, "version") == 0) {
        const char *from = strcmp(mode, "musl") == 0 ? "No such file or directory" : "cannot remove";
        const char *to = strcmp(mode, "musl") == 0 ? "No such file or dir.ectory" : "cannot remove file";
        static char rbuf[1 << 20]; size_t rn = 0;
        for (size_t i = 0; i < en;) {
            size_t fl = strlen(from);
            if (i + fl <= en && memcmp(ebuf + i, from, fl) == 0) { memcpy(rbuf + rn, to, strlen(to)); rn += strlen(to); i += fl; }
            else rbuf[rn++] = ebuf[i++];
        }
        memcpy(ebuf, rbuf, rn); en = rn;
    }
    const char *state = arg_after(argc, argv, "--state");
    if (strcmp(mode, "file") == 0 && state) { char p[8192]; snprintf(p, sizeof p, "%s/zz-extra", state); int fd = open(p, O_CREAT | O_WRONLY, 0600); if (fd >= 0) close(fd); }
    if (strcmp(mode, "mark") == 0 && state) {
        char p[8192]; const char *slash = strrchr(state, '/');
        snprintf(p, sizeof p, "%.*s/owner-zz", (int)(slash ? slash - state : 0), state);
        int fd = open(p, O_CREAT | O_WRONLY, 0600); if (fd >= 0) close(fd);
    }
    const char *cwd = arg_after(argc, argv, "--cwd");
    if (strcmp(mode, "task") == 0 && cwd) { char p[8192]; snprintf(p, sizeof p, "%s/task/t.md", cwd); int fd = open(p, O_WRONLY | O_APPEND); if (fd >= 0) { if (write(fd, "x", 1) < 0) {} close(fd); } }
    put(1, obuf, on); put(2, ebuf, en);
    return code;
}
'''


class Broken(unittest.TestCase):
    """壊した偽の入口が、狙った所で失敗と判定されること。"""

    @classmethod
    def setUpClass(cls):
        get_subject()
        # 保護領域は /tmp・$TMPDIR の外でなければ作れないので、HOME の下に作る。
        cls.dir = Path(tempfile.mkdtemp(prefix='s3ia-selftest-', dir=str(Path.home())))
        cls.standalone = subject.FIXTURE is None
        if cls.standalone and subject.BASH is None:
            # 単独で流すときは、比較器の main と同じく bash 4.0 以上を探す(無ければ旧版を起動する件を省く)。
            subject.BASH = subject.find_bash4()
        if cls.standalone:
            source = cls.dir / 'old.c'
            source.write_text(OLD_AS_FIXTURE_C)
            target = cls.dir / 'old-as-fixture'
            done = subprocess.run([subject.compiler(), '-O1', f'-DOLD="{subject.OLD}"',
                                   f'-DBASH="{shutil.which("bash")}"', '-o', str(target), str(source)],
                                  capture_output=True)
            if done.returncode:
                raise AssertionError('旧版を起動する入口を組めない: ' + done.stderr.decode(errors='replace'))
            subject.FIXTURE = target
        cls.real = subject.FIXTURE
        cls.source = cls.dir / 'wrap.c'
        cls.source.write_text(WRAP_C)
        cls.root = cls.dir / 'root'
        cls.root.mkdir()
        if subject.TOOLS is None:
            subject.TOOLS = subject.Tools(cls.dir / 'tools')

    @classmethod
    def tearDownClass(cls):
        if cls.standalone:
            subject.FIXTURE = None
        subject.make_writable(cls.dir)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def build(self, mode):
        target = self.dir / f'fake-{mode}'
        done = subprocess.run([subject.compiler(), '-O1', f'-DMODE="{mode}"', f'-DREAL="{self.real}"', '-o', str(target),
                               str(self.source)], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors='replace'))
        return target

    def need_old(self):
        """旧版を起動する件は、bash 4.0 以上が無ければ省く(比較器が未実施として記録する。設計 Task 4.1 の Mac)。"""
        if subject.BASH is None:
            self.skipTest(NO_BASH4)

    def outcome(self, fixture, cases):
        self.need_old()
        saved = subject.FIXTURE
        subject.FIXTURE = fixture
        try:
            outcome = subject.Outcome()
            for case in cases:
                subject.run_case(case, self.root, outcome)
            return outcome
        finally:
            subject.FIXTURE = saved

    def expect(self, mode, cases, needle):
        outcome = self.outcome(self.build(mode), cases)
        self.assertTrue(outcome.failures, f'{mode}: 壊した入口が通ってしまった')
        self.assertTrue(any(needle in failure for failure in outcome.failures),
                        f'{mode}: 狙った所の失敗ではない: {outcome.failures}')

    def cases(self, *labels, group=None):
        found = []
        for _, factory in subject.GROUPS:
            for case in factory():
                if case.label in labels and (group is None or case.group == group):
                    found.append(case)
        self.assertEqual(len(found), len(labels), f'ケースが見つからない: {labels}')
        return found

    def test_00_real_fixture_passes(self):
        labels = ['引数なし', 'same', '成功(印あり)']
        if not MAC:
            labels.append('子の環境 GIT_ の変数')
        if not self.standalone and not MAC:
            # 旧版を入口にした単独の流しでは、旧版が 6 個に自分の値を入れるので、今回の入口のときだけ流す。
            labels.append('子の環境 決定 4 の 6 個')
        outcome = self.outcome(self.real, self.cases(*labels))
        self.assertEqual(outcome.failures, [])
        self.assertEqual(outcome.compared, len(labels))

    def test_exit_code(self):
        self.expect('exit', self.cases('引数なし'), '終了値')

    def test_stdout_line(self):
        self.expect('stdout', self.cases('same'), 'stdout')

    def test_stderr_line(self):
        self.expect('stderr', self.cases('引数なし'), 'stderr')

    def test_record_file(self):
        self.expect('file', self.cases('same'), 'ファイル')

    def test_owner_mark(self):
        self.expect('mark', self.cases('成功(印あり)'), 'ファイル')

    def test_task_body(self):
        self.expect('task', self.cases('same'), 'ファイル')

    @unittest.skipIf(MAC_SKIP, MAC_RESTORE)
    def test_child_environment(self):
        self.expect('env', self.cases('子の環境 GIT_ の変数'), 'cp の子の環境')

    @unittest.skipIf(MAC_SKIP, MAC_RESTORE)
    def test_not_reproduced_name_changed(self):
        # 追加の決定 4 の 6 個の 1 つを、受け継いだ値と違う値で渡す Rust。
        self.expect('decision4', self.cases('子の環境 決定 4 の 6 個'), '決定 4 の名前 CAP')

    @unittest.skipIf(MAC_SKIP, MAC_RESTORE)
    def test_not_reproduced_name_added(self):
        # 受け継いでいない 6 個の 1 つを渡す Rust。
        self.expect('decision4-add', self.cases('子の環境 GIT_ の変数'), '決定 4 の名前 helper')

    def test_fake_git_call(self):
        self.expect('git', self.cases('taskmd-diff 偽の git'), '偽の git')

    def test_boundary_misuse(self):
        self.expect('boundary', self.cases('same'), '境界の印')

    def test_timeout(self):
        case = dataclasses.replace(self.cases('引数なし')[0], timeout=2)
        self.expect('sleep', [case], '時間切れ')

    def test_musl_strerror(self):
        self.expect('musl', self.cases('taskmd-diff 4-meta.txt が無い'), 'stderr')

    @unittest.skipIf(MAC_SKIP, 'Mac の rm は BSD の文を出す(GNU の rm の文を書き換える偽が空振りする)')
    def test_tool_version_text(self):
        self.expect('version', self.cases('中に書けないフォルダ'), 'stderr')

    def test_other_tool_launch(self):
        # 本物の前に PATH の上の道具を起動する偽を、strace の段と PATH を絞った段の両方が不合格にする。
        self.need_old()   # 配置(make_world)は旧版の take で作る。
        saved = subject.FIXTURE
        subject.FIXTURE = self.build('tool')
        try:
            if MAC:
                self.skipTest('strace と PATH を絞った段は Linux だけ')
            found = subject.run_restricted_path(self.root)
            self.assertTrue(found, 'PATH を絞った段が、ほかの道具を起動する入口を通した')
            found, reason = subject.run_strace(self.root)
            if reason is not None:
                self.skipTest(f'strace による確かめ: {reason}')
            self.assertTrue(any('cp のほかを exec した' in p for p in found), found)
        finally:
            subject.FIXTURE = saved

    @unittest.skipIf(MAC_SKIP, 'Mac の errno の文は Rust の単体テストが os.strerror と照らす')
    def test_broken_errno_table(self):
        table = self.dir / 'errno_glibc.rs'
        text = subject.ERRNO_GLIBC.read_text(encoding='utf-8')
        self.assertEqual(subject.check_errno_table(subject.ERRNO_GLIBC), [])
        table.write_text(text.replace('"No such file or directory"', '"No such file or dir"', 1), encoding='utf-8')
        self.assertTrue(subject.check_errno_table(table))

    def test_not_executable(self):
        target = self.build('exit')
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            subject.fixture_path(str(target))
        with self.assertRaises(ValueError):
            subject.fixture_path('relative/path')
        with self.assertRaises(ValueError):
            subject.fixture_path('')
        with self.assertRaises(ValueError):
            subject.fixture_path(str(self.dir / 'missing'))

    def test_zero_cases(self):
        with self.assertRaises(AssertionError):
            subject.judge_totals(0, 0, [])
        with self.assertRaises(AssertionError):
            subject.judge_totals(5, 1, [])
        with self.assertRaises(AssertionError):
            subject.judge_totals(5, 0, ['x'])
        subject.judge_totals(5, 0, [])

    def test_normalization_is_narrow(self):
        other = subject.Result(0, b'', b'/elsewhere/implement-guard.sh: line 3: x\n', False, 0)
        self.assertEqual(subject.normalize(other, True).stderr, other.stderr)
        own = subject.Result(0, b'', str(subject.OLD).encode() + b': line 3: x\n', False, 0)
        self.assertEqual(subject.normalize(own, True).stderr, b'implement-guard.sh: line 3: x\n')
        self.assertEqual(subject.normalize(own, False).stderr, own.stderr)


FAKE_BASH32 = '''#!/bin/sh
# 偽の bash 3.2(GitHub の macOS の実行機の /bin/bash の版)。版の問い合わせには 3 を返し、ほかの起動(旧版の
# implement-guard.sh の take など)には、旧版が bash 3.2 で出す行と終了値 2 を返す。
case "$1" in -c) case "$2" in *BASH_VERSINFO*) echo 3; exit 0 ;; esac ;; esac
echo "ERROR [usage] bash 4.0 以上が必要" >&2
exit 2
'''
NESTED = 'S3IA_NO_BASH4_NESTED'   # 入れ子の比較器の中で、この件を流さない印


class NoBash4(unittest.TestCase):
    """bash 4.0 以上が無い Mac をまねる: 偽の bash 3.2 を PATH の先頭に置き、BASH=None で範囲 mac と signed を
    流し、Mac の固定期待の全件(配置の要る 8 件は Python で書く記録)が比べられて合格すること。"""

    @unittest.skipIf(MAC, 'Mac の実行機は、bash 4.0 以上が無ければ本物の範囲 mac・signed がこの道を通る')
    @unittest.skipIf(os.environ.get(NESTED), 'この件が起動した入れ子の範囲 mac・signed の中')
    def test_fixed_expectations_without_bash4(self):
        get_subject()
        if subject.FIXTURE is None:
            self.skipTest('単独で流すときは、検査専用入口が無い')
        for other in ('/opt/homebrew/bin/bash', '/usr/local/bin/bash'):
            if os.access(other, os.X_OK):
                done = subprocess.run([other, '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True)
                if done.stdout.strip().isdigit() and int(done.stdout) >= 4:
                    self.skipTest(f'{other} が bash 4.0 以上なので、偽の bash だけでは BASH を空にできない')
        directory = Path(tempfile.mkdtemp(prefix='s3ia-nobash4-', dir=str(Path.home())))
        try:
            fake = directory / 'bin'
            fake.mkdir()
            (fake / 'bash').write_text(FAKE_BASH32)
            (fake / 'bash').chmod(0o755)
            env = {**os.environ, 'PATH': f'{fake}:{os.environ.get("PATH", "")}', NESTED: '1'}
            for scope in ('mac', 'signed'):
                done = subprocess.run([sys.executable, '-B', str(Path(subject.__file__).resolve()), '--fixture',
                                       str(subject.FIXTURE), '--scope', scope], env=env, capture_output=True,
                                      stdin=subprocess.DEVNULL, timeout=1200)
                out = done.stdout.decode(errors='replace')
                err = done.stderr.decode(errors='replace')
                self.assertEqual(done.returncode, 0, f'範囲 {scope}: {out[-2000:]}{err[-2000:]}')
                self.assertIn('S3I-A: 未実施: 旧版との比較の全部: bash 4.0 以上が無い', out)
                self.assertIn(f'旧版と比べた 0 件・固定期待で確かめた {len(subject.MAC_FIXED_CASES)} 件', out)
                self.assertNotIn('S3I-A: 不合格', err)
                self.assertTrue(out.rstrip().endswith('S3I-A: 合格'), out[-500:])
        finally:
            subject.make_writable(directory)
            shutil.rmtree(directory, ignore_errors=True)


X86_WRITE = '1 0x1 0x55d0c2a3b2a0 0x1 0x0 0x0 0x0 0x7ffd3f1e8a38 0x7f1c2b11a8b7'
X86_WRITE_FD2 = '1 0x2 0x55d0c2a3b2a0 0x1 0x0 0x0 0x0 0x7ffd3f1e8a38 0x7f1c2b11a8b7'
X86_READ = '0 0x0 0x55d0c2a3b2a0 0x1 0x0 0x0 0x0 0x7ffd3f1e8a38 0x7f1c2b11a8b7'
ARM_WRITE = '64 0x1 0xaaaad5f3c2a0 0x1 0x0 0x0 0x0 0xffffe1c2a8b0 0xffff9a1b2c3d'
ARM_READ = '63 0x0 0xaaaad5f3c2a0 0x1 0x0 0x0 0x0 0xffffe1c2a8b0 0xffff9a1b2c3d'


class WriteBlock(unittest.TestCase):
    """詰めた FIFO への write で止まったことの見分け方。x86_64・aarch64・wchan が 0 の 3 通りの /proc の中身を、
    偽の /proc(stat・syscall・wchan と、fd 1・2 のリンク)として与える。"""

    @classmethod
    def setUpClass(cls):
        get_subject()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='s3ia-proc-'))
        self.fifo = self.dir / 'fifo'
        os.mkfifo(self.fifo)
        self.other = self.dir / 'other'
        self.other.write_bytes(b'')
        self.fifo_id = subject.fifo_id(self.fifo)
        self.made = 0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def proc(self, syscall, *, state='S', wchan='pipe_write', fd1=None, fd2=None, comm='bash'):
        """偽の /proc/4242 を作り、その根を返す。None の項目は作らない(読めない物になる)。"""
        self.made += 1
        root = self.dir / f'proc-{self.made}'
        folder = root / '4242'
        (folder / 'fd').mkdir(parents=True)
        if state is not None:
            (folder / 'stat').write_text(f'4242 ({comm}) {state} 1 4242 4242 0 -1 4194560 0 0 0 0\n')
        if syscall is not None:
            (folder / 'syscall').write_text(syscall + '\n')
        if wchan is not None:
            (folder / 'wchan').write_text(wchan)
        os.symlink(fd1 or self.fifo, folder / 'fd' / '1')
        os.symlink(fd2 or self.other, folder / 'fd' / '2')
        return root

    def seen(self, root, machine):
        return subject.blocked_in_write(4242, self.fifo_id, subject.FifoWatch(machine, ''), proc_root=root)

    def test_x86_64(self):
        self.assertTrue(self.seen(self.proc(X86_WRITE), 'x86_64'))
        # bash の `>&2` は fd 2 を書く。fd 2 が FIFO を指せば止まりと見なす。
        self.assertTrue(self.seen(self.proc(X86_WRITE_FD2, fd1=self.other, fd2=self.fifo), 'x86_64'))
        self.assertFalse(self.seen(self.proc(X86_WRITE, fd1=self.other), 'x86_64'), 'fd 1 が FIFO でない')
        self.assertFalse(self.seen(self.proc(X86_WRITE.replace(' 0x1 ', ' 0x3 ', 1)), 'x86_64'), 'fd 3 への write')
        self.assertFalse(self.seen(self.proc('running', state='R'), 'x86_64'), '動いている')
        self.assertFalse(self.seen(self.proc(X86_WRITE, state='D'), 'x86_64'), '割り込めない待ち')
        self.assertFalse(self.seen(self.proc(X86_READ, wchan='pipe_read'), 'x86_64'), 'read の待ち')
        self.assertFalse(self.seen(self.proc(ARM_WRITE), 'x86_64'), 'aarch64 の write の番号')
        self.assertFalse(self.seen(self.proc(None), 'x86_64'), 'syscall を読めない')
        self.assertFalse(self.seen(self.proc(X86_WRITE, state=None), 'x86_64'), 'stat を読めない')

    def test_aarch64(self):
        self.assertTrue(self.seen(self.proc(ARM_WRITE), 'aarch64'))
        self.assertTrue(self.seen(self.proc(ARM_WRITE.replace(' 0x1 ', ' 0x2 ', 1), fd2=self.fifo), 'aarch64'))
        self.assertFalse(self.seen(self.proc(X86_WRITE), 'aarch64'), 'x86_64 の write の番号(aarch64 では別の呼び出し)')
        self.assertFalse(self.seen(self.proc(ARM_READ, wchan='pipe_read'), 'aarch64'), 'read の待ち')
        self.assertFalse(self.seen(self.proc(ARM_WRITE, state='R'), 'aarch64'), '動いている')

    def test_wchan_names(self):
        # wchan は必須。パイプへの書き込みで待つ関数の名前の集合(新しいカーネルの名前は推測の読み替え)だけを受ける。
        # 0(名前を返さない)・読めない・ほかの関数は、状態と syscall と fd が合っていても止まりと見なさない。
        for machine, syscall in (('x86_64', X86_WRITE), ('aarch64', ARM_WRITE)):
            for name in ('pipe_write', 'anon_pipe_write', 'fifo_pipe_write', 'pipe_write\n'):
                self.assertTrue(self.seen(self.proc(syscall, wchan=name), machine), (machine, name))
            for name in ('0', '', 'pipe_read', 'do_select', 'pipe_write_x', None):
                self.assertFalse(self.seen(self.proc(syscall, wchan=name), machine), (machine, name))
        view = subject.read_proc(4242, self.proc(X86_WRITE, wchan='0'))
        text = subject.describe_view(view, self.fifo_id)
        self.assertIn("stat '4242 (bash) S 1 4242'", text)
        self.assertIn(f"syscall '{X86_WRITE}'", text)
        self.assertIn("wchan '0'", text)
        self.assertIn('FIFO を指す fd 1', text)
        unreadable = subject.describe_view(subject.read_proc(4242, self.proc(None, wchan=None)), self.fifo_id)
        self.assertIn('syscall 読めない', unreadable)
        self.assertIn('wchan 読めない', unreadable)
        self.assertIn('読めなかった物 syscall:FileNotFoundError,wchan:FileNotFoundError', unreadable)

    def test_wchan_compiler_suffix(self):
        # コンパイラが付ける `.` から後ろの接尾辞は落として比べる(2026-11-19 03:44 の管理担当の判断)。
        # 集合に無い名前は、`.` を含まない別の名前も、接尾辞を落として集合に無い名前も見分けない。
        for machine, syscall in (('x86_64', X86_WRITE), ('aarch64', ARM_WRITE)):
            for name in ('pipe_write.isra.0', 'pipe_write.constprop.0', 'fifo_pipe_write.cold', 'anon_pipe_write.isra.0'):
                self.assertTrue(self.seen(self.proc(syscall, wchan=name), machine), (machine, name))
            for name in ('pipe_writer', 'pipe_write_x', 'pipe_writer.isra.0', '.pipe_write', 'pipe.write'):
                self.assertFalse(self.seen(self.proc(syscall, wchan=name), machine), (machine, name))

    def test_other_machine_and_name(self):
        for machine in ('riscv64', 'arm64', 'i686'):
            self.assertFalse(self.seen(self.proc(X86_WRITE), machine), machine)
            self.assertFalse(self.seen(self.proc(ARM_WRITE), machine), machine)
        # 名前の欄の `)` と状態の文字に惑わされない(最後の `)` の後ろを読む)。
        self.assertTrue(self.seen(self.proc(X86_WRITE, comm='a) R (b'), 'x86_64'))
        self.assertFalse(self.seen(self.proc(X86_WRITE, state='R', comm='a) S (b'), 'x86_64'))

    def test_plan_has_48_cases(self):
        self.assertEqual(len(subject.fifo_plan()), 48)


class FifoStage(unittest.TestCase):
    """詰めた FIFO の段: 段の始めの試しの各経路・待ち切りの上限・main の配線(定数と関数を差し替えた軽い検査と、
    Linux では本物の試し)。"""

    @classmethod
    def setUpClass(cls):
        get_subject()
        cls.dir = Path(tempfile.mkdtemp(prefix='s3ia-fifo-', dir=str(Path.home())))
        cls.saved = (subject.TOOLS, subject.BASH)
        cls.reason = ''
        if not subject.LINUX or os.uname().machine not in subject.WRITE_SYSCALLS:
            cls.reason = 'Linux の x86_64 か aarch64 でない(試しは /proc を読む)'
        else:
            if subject.BASH is None:
                subject.BASH = subject.find_bash4()
            if subject.BASH is None:
                cls.reason = NO_BASH4
            else:
                subject.TOOLS = subject.Tools(cls.dir / 'tools')

    @classmethod
    def tearDownClass(cls):
        subject.TOOLS, subject.BASH = cls.saved
        subject.make_writable(cls.dir)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def root(self):
        return Path(tempfile.mkdtemp(dir=self.dir))

    def probe(self, **patches):
        """本物の試し(fifo_watch)を、patches の名前を差し替えて流す。"""
        if self.reason:
            self.skipTest(self.reason)
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(subject, name, value))
            return subject.fifo_watch(self.root())

    def failed(self, needle, **patches):
        with self.assertRaises(subject.ProbeFailed) as caught:
            self.probe(**patches)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_probe_sees_blocked_write(self):
        watch, reason = self.probe()
        self.assertIsNone(reason)
        self.assertEqual(watch.machine, os.uname().machine)
        self.assertIn('wchan', watch.note)

    def test_probe_child_writes_before_gate(self):
        # stdin を待たずに書く子: 書かせる前に止まったと見なしたら、問題にする。
        self.failed('書かせる前の子', FIFO_PROBE_SCRIPT='printf x')

    def test_probe_child_ends(self):
        self.failed('書き込みで止まらずに終わった(終了値 3', FIFO_PROBE_SCRIPT='read -r -n 1 go; exit 3')
        self.failed('書かせる前に終わった(終了値 4', FIFO_PROBE_SCRIPT='exit 4')
        real_fill = subject.fill_fifo

        def not_filled(path):
            # FIFO を詰めない(読み手が読まないだけ)。子の 1 バイトは止まらずに書ける。
            reader, writer, filled = real_fill(path)
            os.read(reader, 65536)
            return reader, writer, filled
        self.failed('書き込みで止まらずに終わった(終了値 0', fill_fifo=not_filled)

    def test_probe_not_seen_in_time(self):
        # 書かずに stdin を待ち続ける子と、wchan の名前の集合に入らない機械をまねる差し替え。どちらも期限で問題。
        self.failed('1 秒の中で', FIFO_PROBE_WAIT=1, FIFO_PROBE_SCRIPT='read -r -n 1 go; read -r -t 30 more')
        text = self.failed('1 秒の中で', FIFO_PROBE_WAIT=1, PIPE_WRITE_WCHANS=('not_a_pipe_function',))
        self.assertIn('stat ', text)
        self.assertIn('syscall ', text)
        self.assertIn('wchan ', text)

    def run_fifo_quiet(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            found = subject.run_fifo(self.root())
        return found, out.getvalue()

    def test_probe_failure_is_problem_and_stops(self):
        def fail(_root):
            raise subject.ProbeFailed('最後に読んだ値: wchan 0')

        def never(*_args):
            raise AssertionError('試しの失敗の後にケースを流した')
        with mock.patch.object(subject, 'fifo_watch', fail), mock.patch.object(subject, 'run_fifo_side', never):
            (problems, reason), out = self.run_fifo_quiet()
        self.assertIsNone(reason)
        self.assertEqual(len(problems), 1)
        self.assertIn('段の始めの試しで、書き込みで止まったことを /proc から確かめられない(最後に読んだ値: wchan 0)',
                      problems[0])
        self.assertIn('段のケースを流さない', problems[0])
        self.assertEqual(out, '')

    def test_unsupported_machine_is_unperformed(self):
        # write の番号を持たない機械では、ケースを流さずに未実施の理由を返す(問題にも成功にも数えない)。
        with mock.patch.object(subject, 'WRITE_SYSCALLS', {}):
            (problems, reason), out = self.run_fifo_quiet()
        self.assertEqual(problems, [])
        self.assertTrue(reason)
        self.assertEqual(out, f'S3I-A: 詰めた FIFO の信号: 未実施({reason})\n')
        if subject.LINUX:
            self.assertIn(f'この機械({os.uname().machine})の write の番号を持たない', reason)

    def test_wait_limit_stops_remaining(self):
        plan = [(f'c{i}', 'stdout', None, subject.fixed(['-h']), 'TERM', signal.SIGTERM, False) for i in range(10)]
        calls = []

        def side(*args):
            calls.append(args)
            return subject.Result(0, b'', b'', False, 0), False, True, '最後の値'
        watch = subject.FifoWatch('x86_64', '差し替え')
        with mock.patch.object(subject, 'fifo_watch', lambda _root: (watch, None)), \
                mock.patch.object(subject, 'fifo_plan', lambda: plan), \
                mock.patch.object(subject, 'run_fifo_side', side):
            (problems, reason), out = self.run_fifo_quiet()
        self.assertIsNone(reason)
        self.assertEqual(len(calls), subject.FIFO_WAIT_LIMIT)
        self.assertEqual(len(problems), subject.FIFO_WAIT_LIMIT // 2 + 1)
        self.assertIn('最後に読んだ値: 最後の値', problems[0])
        self.assertIn(f'片側が {subject.FIFO_WAIT_LIMIT} 回になったので、残りの 8 件を流さない', problems[-1])
        self.assertIn('S3I-A: 詰めた FIFO の信号: 2 件', out)

    def wiring(self, found):
        problems, unperformed = [], []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(subject, 'run_fifo', lambda _root: found), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            subject.run_fifo_stage(self.root(), problems, unperformed)
            subject.report('full', {}, problems, unperformed, {}, 0, 0.0)
        return problems, unperformed, out.getvalue(), err.getvalue()

    def test_main_counts_probe_failure_as_problem(self):
        problems, unperformed, out, err = self.wiring((['詰めた FIFO の信号: 試しの失敗'], None))
        self.assertEqual((problems, unperformed), (['詰めた FIFO の信号: 試しの失敗'], []))
        self.assertIn('S3I-A: 不合格: 詰めた FIFO の信号: 試しの失敗\n', err)
        self.assertIn('未実施 0 項目・不一致 0 件・問題 1 件', out)
        with self.assertRaises(AssertionError):
            subject.judge_totals(5, 0, problems)

    def test_main_records_unsupported_machine_as_unperformed(self):
        problems, unperformed, out, err = self.wiring(([], 'この機械(riscv64)の write の番号を持たない'))
        self.assertEqual((problems, unperformed), ([], ['詰めた FIFO の信号: この機械(riscv64)の write の番号を持たない']))
        self.assertIn('S3I-A: 未実施: 詰めた FIFO の信号: この機械(riscv64)の write の番号を持たない\n', out)
        self.assertIn('未実施 1 項目・不一致 0 件・問題 0 件', out)
        self.assertEqual(err, '')
        subject.judge_totals(5, 0, problems)


class Selftest(unittest.TestCase):
    """旧 selftest の 2 通りの判定。"""

    @classmethod
    def setUpClass(cls):
        get_subject()

    def lines(self, *items):
        return [(verdict, case, text) for verdict, case, text in items]

    def test_allowed_failures(self):
        old = [('J', self.lines(('PASS', 'J9', 'a'), ('PASS', 'J1', 'b')), 0)]
        good = [('J', self.lines(('FAIL', 'J9', 'a'), ('PASS', 'J1', 'b')), 2)]
        allowed = {('J9', 'a')}
        self.assertEqual(subject.judge_selftest(old, good, allowed, 2)[2], [])
        outside = [('J', self.lines(('FAIL', 'J9', 'a'), ('FAIL', 'J1', 'b')), 2)]
        self.assertTrue(subject.judge_selftest(old, outside, allowed, 2)[2])
        inside = [('J', self.lines(('PASS', 'J9', 'a'), ('PASS', 'J1', 'b')), 2)]
        self.assertTrue(subject.judge_selftest(old, inside, allowed, 2)[2])

    def test_line_counts_and_minimum(self):
        old = [('J', self.lines(('PASS', 'J1', 'b'), ('PASS', 'J2', 'c')), 0)]
        fewer = [('J', self.lines(('PASS', 'J1', 'b')), 1)]
        self.assertTrue(subject.judge_selftest(old, fewer, set(), 0)[2])
        only_old = [('J', self.lines(('PASS', 'J1', 'b'), ('PASS', 'J2', 'c')), 0)]
        self.assertTrue(subject.judge_selftest(old, only_old, set(), 1)[2])
        failing_old = [('J', self.lines(('FAIL', 'J1', 'b'), ('PASS', 'J2', 'c')), 0)]
        rust = [('J', self.lines(('PASS', 'J1', 'b'), ('PASS', 'J2', 'c')), 1)]
        self.assertTrue(subject.judge_selftest(failing_old, rust, set(), 0)[2])


if __name__ == '__main__':
    unittest.main()
