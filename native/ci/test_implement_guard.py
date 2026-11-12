#!/usr/bin/env python3
"""S3I-A の比較器自身の検査。check_implement_guard.py の中から必ず流す(subject に比較器の本体が入る)。

正しい例(今回ビルドした検査専用入口)を先に通し、次に 1 か所ずつ壊した偽の入口(追加の決定 4 の名前を
受け継いだ値と違う値で渡すものを含む)・時間切れ・実行不能・0 件・
境界の印の誤用・版の違う道具の文・musl の strerror の文・selftest の許す失敗の一覧の外れと中の成功・
Rust を通った行の不足を与え、すべて失敗になることを確かめる。偽の入口は C で組み、本物を起動してから
(または起動の前に)1 か所だけを変える。
"""
import dataclasses
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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
