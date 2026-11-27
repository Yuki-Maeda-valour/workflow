#!/usr/bin/env python3
"""S3I-A: 旧版 implement-guard.sh を基準の環境で動かし、比較器で同じ時点を作れない結果を固定期待に書く。

- 信号(道具の地点): 旧版が起動する道具(stat・sha256sum・mktemp・realpath・cmp・sed・rm・find・grep)の偽を
  PATH の先頭に置き、n 回目の起動で止める。偽は C で組んだ観測器(起動を数えて記録し、止める回なら止まった印を
  書いて解く印を待ち、本物を exec する。信号の扱いは変えない)。止まった所で TERM・INT・HUP を旧版の bash へ
  送り、偽を終わらせてから結果(終了値・stdout・stderr・`.work-*` の残り・記録と印の有無)を測る。地点は、
  数えるだけの 1 回目の起動で全部を数え上げる(漏れが無い)。
- 同じ手順を地点ごとに 6 回流す(止まったと確かめられなかった回は数えずに流し直す)。旧版どうしで揺れない
  地点は全部のバイトで 1 つの固定期待。揺れる地点は、見た結果と回数を全部書く。決定の表の「旧 bash の
  信号」の規則の 2 通り(罠が道具の直後に動いた結果と、次の組み込みの命令の後に動いた結果。Rust の単体テストが
  作って書き出す)のどれに当たるかを、見た結果ごとに書く。Rust の結果(道具の直後)が旧版で出ていなければ、
  30 回まで増やし、それでも出なければ失敗にする(止まって Issue に書く)。
- 照らし(--check)は、揺れない地点の固定期待の結果と、Rust の結果(規則の「道具の直後」)が今の測定にも出る
  ことを確かめる。固定期待で見ていない結果が新しく出たときは、旧版どうしの競り合いとして記録し、失敗にしない
  (Rust に許す結果は、固定期待で見た規則の結果のまま変えない)。
- 起こせない失敗: C で組んだ LD_PRELOAD の部品で、旧版の bash と道具の特定のパスへの open・mkdir・unlinkat・
  read・getdelim を失敗させて測る。
- 測れない地点は「未測定」と理由を書く(類推の期待で合格にしない)。

使い方(手元の基準の容器。CI では流さない):
  1. 規則の 2 通りを書き出す: Rust の単体テスト
     `S3IA_EXPORT_RULE_OUTCOMES=<rules.json> cargo test --locked -p dev-workflow-cli --example
      implement_guard_contract signals_at_tool_points`
  2. python3 -B native/ci/implement_guard_measure.py --write <出力 JSON> --rules <rules.json>  # 固定期待を作る
  3. python3 -B native/ci/implement_guard_measure.py --check [<固定期待 JSON>]   # 今の旧版で測り直して照らす
     (値を省くと native/ci/implement_guard_fixtures/expected_old.json)
Python 3.10 系・root 以外・基準の容器(bash 5.2.21・coreutils 9.4)で流す。
"""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_implement_guard as base  # noqa: E402

TOOLS = ('stat', 'sha256sum', 'mktemp', 'realpath', 'cmp', 'sed', 'rm', 'find', 'grep', 'python3')
REPEAT = 6
MAX_REPEAT = 30
RUST_REPEAT = 3

FAKE_C = r'''/* 偽の道具(測る道具の観測器)。自分の置き場所の count で起動を数え、calls に「名前<TAB>引数」を足す。
   block に書いた回なら ready を作り、release ができるまで待つ。そのあと本物(REAL)を同じ引数で exec する。
   信号の扱いと止め(mask)は変えない。 */
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static long read_number(const char *path) {
    long value = -1;
    FILE *f = fopen(path, "r");
    if (f) { if (fscanf(f, "%ld", &value) != 1) value = -1; fclose(f); }
    return value;
}

int main(int argc, char **argv) {
    char self[PATH_MAX], path[PATH_MAX + 32];
    ssize_t len = readlink("/proc/self/exe", self, sizeof self - 1);
    if (len <= 0) return 126;
    self[len] = 0;
    char *slash = strrchr(self, '/');
    if (!slash) return 126;
    *slash = 0;
    snprintf(path, sizeof path, "%s/count", self);
    long n = read_number(path);
    n = n < 0 ? 1 : n + 1;
    FILE *f = fopen(path, "w");
    if (f) { fprintf(f, "%ld\n", n); fclose(f); }
    snprintf(path, sizeof path, "%s/calls", self);
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) {
        static char line[1 << 16];
        size_t used = (size_t)snprintf(line, sizeof line, "%s\t", TOOL);
        for (int i = 1; i < argc && used < sizeof line - 2; i++) {
            int w = snprintf(line + used, sizeof line - used - 1, "%s%s", i > 1 ? " " : "", argv[i]);
            if (w < 0) break;
            used += (size_t)w;
            if (used > sizeof line - 2) used = sizeof line - 2;
        }
        line[used++] = '\n';
        if (write(fd, line, used) < 0) {}
        close(fd);
    }
    snprintf(path, sizeof path, "%s/block", self);
    if (read_number(path) == n) {
        snprintf(path, sizeof path, "%s/ready", self);
        fd = open(path, O_WRONLY | O_CREAT, 0600);
        if (fd >= 0) close(fd);
        snprintf(path, sizeof path, "%s/release", self);
        struct timespec pause = {0, 10 * 1000 * 1000};
        for (int i = 0; i < 12000 && access(path, F_OK) != 0; i++) nanosleep(&pause, NULL);
    }
    /* パスで起動された(旧版が realpath した python3 など)ときは、偽が無ければ渡ったはずの本物のパスを
       argv[0] にする(CPython は argv[0] から自分の置き場所を求める)。名前で起動されたときは、そのまま。 */
    if (strchr(argv[0], '/') != NULL) argv[0] = (char *)REAL;
    execv(REAL, argv);
    return 127;
}
'''

PRELOAD_C = r'''/* 旧版だけに故障を起こす部品。S3IA_FAIL=<操作>:<パスの一部>:<errno>。S3IA_FAIL_COMM があれば、
   その名前のプログラムの中だけで起こす。read は fd の指す先のパスで見分ける。 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern char *program_invocation_short_name;

static int match(const char *op, const char *path) {
    const char *spec = getenv("S3IA_FAIL");
    const char *comm = getenv("S3IA_FAIL_COMM");
    if (!spec || !path) return 0;
    if (comm && strcmp(comm, program_invocation_short_name) != 0) return 0;
    size_t oplen = strlen(op);
    if (strncmp(spec, op, oplen) != 0 || spec[oplen] != ':') return 0;
    const char *tail = spec + oplen + 1;
    const char *colon = strrchr(tail, ':');
    if (!colon) return 0;
    size_t n = (size_t)(colon - tail);
    char want[4096];
    if (n >= sizeof(want)) return 0;
    memcpy(want, tail, n);
    want[n] = 0;
    if (!strstr(path, want)) return 0;
    errno = atoi(colon + 1);
    return 1;
}

int open(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "open");
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
    if (match("open", path)) return -1;
    return real(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    static int (*real)(const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "open64");
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
    if (match("open", path)) return -1;
    return real(path, flags, mode);
}

int mkdir(const char *path, mode_t mode) {
    static int (*real)(const char *, mode_t);
    if (!real) real = dlsym(RTLD_NEXT, "mkdir");
    if (match("mkdir", path)) return -1;
    return real(path, mode);
}

int unlinkat(int dirfd, const char *path, int flags) {
    static int (*real)(int, const char *, int);
    if (!real) real = dlsym(RTLD_NEXT, "unlinkat");
    if (match((flags & AT_REMOVEDIR) ? "rmdir" : "unlink", path)) return -1;
    return real(dirfd, path, flags);
}

static int fd_matches(int fd) {
    char link[64], target[4096];
    int saved = errno;
    snprintf(link, sizeof link, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, target, sizeof target - 1);
    errno = saved;
    if (n <= 0) return 0;
    target[n] = 0;
    return match("read", target);
}

ssize_t read(int fd, void *buf, size_t count) {
    static ssize_t (*real)(int, void *, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "read");
    if (fd_matches(fd)) return -1;
    return real(fd, buf, count);
}

ssize_t __read_chk(int fd, void *buf, size_t count, size_t size) {
    static ssize_t (*real)(int, void *, size_t, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "__read_chk");
    if (fd_matches(fd)) return -1;
    return real(fd, buf, count, size);
}

ssize_t getdelim(char **line, size_t *size, int delim, FILE *stream) {
    static ssize_t (*real)(char **, size_t *, int, FILE *);
    if (!real) real = dlsym(RTLD_NEXT, "getdelim");
    if (fd_matches(fileno(stream))) { stream->_flags |= 0x20; /* _IO_ERR_SEEN */ return -1; }
    return real(line, size, delim, stream);
}
'''

SIGNALS = (('TERM', signal.SIGTERM), ('INT', signal.SIGINT), ('HUP', signal.SIGHUP))
REPEAT = 6

def rust_points(sequence, world):
    """道具の起動の列を、Rust の差し込み点(名前・何回目)へ対応づける。Rust に地点が無い起動は None
    (python3 と同梱 helper の確かめは決定 D5)。"""
    counts = {}
    seen = {}
    out = []
    state = world.state
    for tool, args in sequence:
        seen[tool] = seen.get(tool, 0) + 1
        n = seen[tool]
        point = None
        if tool == 'stat':
            point = {f'-c %a -- /': 'resolve-stat', f'-c %a -- {state}': 'stat-mode-state',
                     f'-c %a -- {state}/taskmd-body': 'stat-mode-body', f'-c %d:%i -- {state}': 'stat-identity',
                     f'-c %s -- {state}/taskmd-body': 'stat-size-before'}.get(args, 'stat-size-after')
        elif tool == 'sha256sum':
            point = 'sha256-manifest' if n == 1 else 'sha256-body' if n == 11 else 'snapshot-digest'
        elif tool == 'mktemp':
            point = 'mktemp'
        elif tool == 'realpath':
            target = args[3:] if args.startswith('-- ') else args
            if target == str(world.repo):
                point = 'realpath-cwd'
            elif target == world.real or target.endswith('/task/t.md'):
                point = 'realpath-selected'
            elif os.path.basename(target) == 'cp':
                point = 'realpath-cp'
        elif tool == 'cmp':
            point = 'cmp-norm' if 'before.norm' in args else 'cmp-same'
        elif tool == 'sed':
            point = 'sed-before' if n == 1 else 'sed-after'
        elif tool == 'rm':
            if args == f'-rf -- {state}':
                point = 'rm-state'
            elif args.startswith(f'-rf -- {state}/.work-'):
                # EXIT の罠の cleanup_tmp の `rm -rf -- "$WORK"`(Rust は Shell::exit の後の地点)。
                point = 'rm-work'
            elif args.startswith('-f -- '):
                point = 'rm-mark'
        elif tool in ('find', 'grep'):
            point = tool
        if point == 'snapshot-digest':
            # Rust はスナップショットの全部を 1 つの地点で数える(旧版の sha256sum の何回目でも同じ地点)。
            out.append((point, 1))
        elif point is not None:
            counts[point] = counts.get(point, 0) + 1
            out.append((point, counts[point]))
        else:
            out.append((None, 0))
    return out


def build_fakes(root):
    """偽の道具を、道具ごとに C で組む(本物のパスを埋め込む)。"""
    out = Path(root) / 'fakebin'
    out.mkdir(parents=True, exist_ok=True)
    source = out / 'fake.c'
    source.write_text(FAKE_C)
    for tool in TOOLS:
        real = shutil.which(tool)
        if not real:
            continue
        done = subprocess.run([base.compiler(), '-O1', f'-DTOOL="{tool}"', f'-DREAL="{os.path.realpath(real)}"',
                               '-o', str(out / tool), str(source)], capture_output=True)
        if done.returncode:
            raise AssertionError('偽の道具を組めない: ' + done.stderr.decode(errors='replace'))
    return out


FAKEBIN = None


def fake_dir(root):
    """1 回の起動の偽の置き場(組んだ偽を写す。数える印と止める印はこの中に置く)。"""
    directory = Path(root) / 'fakes'
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for tool in TOOLS:
        built = FAKEBIN / tool
        if built.exists():
            shutil.copy2(built, directory / tool)
    return directory


def scenario_worlds():
    """測る流れ(サブコマンド・配置・argv)。"""
    def diff_same(world):
        return base.argv_for('taskmd-diff', world.values())

    def diff_checks(world):
        Path(world.real).write_bytes(b'# t\n- [x] one\n- [ ] two\nbody\n')
        return base.argv_for('taskmd-diff', world.values())

    def restore(world):
        Path(world.real).write_bytes(b'changed\n')
        return base.argv_for('restore-taskmd', world.values())

    def cleanup(world):
        return ['cleanup', '--state', world.state]

    return (('taskmd-diff same', diff_same), ('taskmd-diff checks-only', diff_checks),
            ('restore-taskmd', restore), ('cleanup', cleanup))


def settled(pid, deadline):
    """bash が子を待って止まった(wchan が pipe_read か do_wait で、2 回続けて同じ)。"""
    last = None
    while time.monotonic() < deadline:
        try:
            wchan = Path(f'/proc/{pid}/wchan').read_text().strip()
        except OSError:
            return False
        if wchan in ('pipe_read', 'do_wait') and wchan == last:
            return True
        last = wchan
        time.sleep(0.02)
    return False


def run_old(world, argv, fakes, extra_env=None, during=None, timeout=60):
    env = [b'PATH=' + os.fsencode(f'{fakes}:{os.environ.get("PATH", "")}') if fakes else
           b'PATH=' + os.fsencode(os.environ.get('PATH', '')),
           b'HOME=' + os.fsencode(world.home), b'XDG_STATE_HOME=' + os.fsencode(world.xdg)]
    for key, value in (extra_env or {}).items():
        env.append(f'{key}={value}'.encode())
    bash = shutil.which('bash')
    result = base.launch(bash, 'bash', [str(base.OLD), *argv], env, cwd=world.repo, during=during, timeout=timeout)
    return base.normalize(result, True)


def outcome(world, result):
    work = []
    if os.path.isdir(world.state):
        work = sorted(n for n in os.listdir(world.state) if n.startswith('.work-'))
    mark = os.path.join(os.path.dirname(world.state), 'owner-fixed')
    def place(data):
        text = data.decode('utf-8', 'surrogateescape').replace(world.state, '<state>')
        return text.replace(os.path.dirname(world.state), '<parent>').replace(str(world.base), '<base>')

    return {
        'rc': result.code,
        'stdout': place(result.stdout),
        'stderr': place(result.stderr),
        'work_left': len(work),
        'state_exists': os.path.isdir(world.state),
        'mark_exists': os.path.lexists(mark),
        'timed_out': result.timed_out,
    }


def shown_args(args, probe_world):
    shown = args.replace(probe_world.state, '<state>').replace(str(probe_world.base), '<base>')
    shown = shown.replace(str(base.ROOT), '<root>').replace(os.path.dirname(str(probe_world.base)), '<work>')
    # 印の device・inode は機械ごとに違い、`.work-` の後ろ 6 文字は実行ごとに違うので伏せる。
    shown = re.sub(r'(guard_(?:dev|ino)\t)[0-9]+', r'\1<n>', shown)
    return re.sub(r'(\.work-)[A-Za-z0-9]{6}', r'\1XXXXXX', shown)


def attempt(root, make_argv, index, number):
    """1 回流す。止まったと確かめて信号を送れた回だけ結果を返す(確かめられなければ None)。"""
    world = base.make_world(Path(root) / 'w')
    fakes = fake_dir(root)
    (fakes / 'block').write_text(str(index))
    argv = make_argv(world)

    def during(process):
        # 混んだ機械でも待てるように、偽が自分で解ける時間(120 秒)と同じだけ待つ。
        deadline = time.monotonic() + 120
        while not (fakes / 'ready').exists():
            if process.poll() is not None or time.monotonic() > deadline:
                return
            time.sleep(0.01)
        # 旧版の bash が道具を exec した(プロセスが偽そのもの)なら、bash の地点ではない。
        try:
            if os.path.dirname(os.readlink(f'/proc/{process.pid}/exe')) == str(fakes):
                (fakes / 'execed').write_text('')
                (fakes / 'release').write_text('')
                return
        except OSError:
            pass
        # 旧版の bash が子を待つ所(パイプの読み取りか waitpid)で止まったことを確かめてから送る。
        if not settled(process.pid, deadline):
            (fakes / 'unsettled').write_text('')
            (fakes / 'release').write_text('')
            return
        os.kill(process.pid, number)
        (fakes / 'sent').write_text('')
        time.sleep(0.3)
        (fakes / 'release').write_text('')

    result = run_old(world, argv, fakes, during=during, timeout=300)
    if (fakes / 'execed').exists():
        return 'execed'
    # 止まったと確かめて信号を送った回だけを数える(期限切れ・確かめられない回は流し直す)。
    if not (fakes / 'sent').exists() or result.timed_out:
        return None
    return outcome(world, result)


def key_of(item):
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def observe(root, make_argv, index, number, want=None):
    """6 回(want が出なければ 30 回まで)流し、見た結果ごとの回数を返す。"""
    counts = collections.Counter()
    items = {}
    runs = retries = 0
    execed = 0
    while runs < REPEAT or (want is not None and key_of(want) not in counts and runs < MAX_REPEAT):
        got = attempt(root, make_argv, index, number)
        if got == 'execed':
            # 旧版の bash が偽を exec した(bash の地点ではない)。続くなら地点の数え方の誤り。
            execed += 1
            if execed >= 3:
                raise AssertionError(f'{index} 番目の起動は、旧版の bash が exec した道具で、bash の地点ではない')
            got = None
        if got is None:
            retries += 1
            if retries > 3 * MAX_REPEAT:
                raise AssertionError(f'{index} 番目の起動で、止まったと確かめられない回が多すぎる')
            continue
        runs += 1
        counts[key_of(got)] += 1
        items[key_of(got)] = got
    return counts, items, runs, retries


def label_of(item, rule):
    if rule is None:
        return None
    if item == rule['tool']:
        return 'tool'
    if rule.get('next_builtin') is not None and item == rule['next_builtin']:
        return 'next-builtin'
    return 'outside-rule'


def measure_signals(root, rules):
    """rules: {「流れ<TAB>何回目の起動<TAB>信号」: {'tool': …, 'next_builtin': …}}(Rust の地点の分)。"""
    records = []
    problems = []
    for label, make_argv in scenario_worlds():
        probe_world = base.make_world(Path(root) / 'probe')
        fakes = fake_dir(root)
        run_old(probe_world, make_argv(probe_world), fakes)
        calls = (fakes / 'calls').read_text().splitlines() if (fakes / 'calls').exists() else []
        sequence = [tuple((line.split('\t', 1) + [''])[:2]) for line in calls]
        mapped = rust_points(sequence, probe_world)
        for index, (tool, args) in enumerate(sequence, start=1):
            point, occurrence = mapped[index - 1]
            if tool == 'python3' and ' restore --source ' in f' {args}':
                # 旧版の最後の exec(旧 helper)。この後のプロセスは旧 helper の Python で、bash の地点ではない
                # (UNMEASURED に理由を書く。旧 helper の中の信号は R4R の比較のとおり)。
                continue
            for name, number in SIGNALS:
                key = f'{label}\t{index}\t{name}'
                rule = None
                if point is not None:
                    if key not in rules:
                        raise AssertionError(f'規則の 2 通りが無い(Rust の単体テストで書き出し直す): {key}')
                    rule = rules[key]
                counts, items, runs, retries = observe(root, make_argv, index, number,
                                                       rule['tool'] if rule else None)
                outcomes = []
                for item_key in sorted(counts):
                    item = dict(items[item_key])
                    item['count'] = counts[item_key]
                    item['rule'] = label_of(items[item_key], rule)
                    outcomes.append(item)
                record = {'scenario': label, 'call': index, 'tool': tool, 'args': shown_args(args, probe_world),
                          'signal': name, 'rust_point': point, 'occurrence': occurrence, 'runs': runs,
                          'retries': retries, 'stable': len(outcomes) == 1, 'outcomes': outcomes}
                if rule is not None:
                    record['rule'] = rule
                    if key_of(rule['tool']) not in counts:
                        problems.append(f'{key}: Rust の結果(罠が道具の直後)が、旧版の {runs} 回の測定で出ない')
                records.append(record)
            print(f'S3I-A 測定: {label} の {index}/{len(sequence)} 番目の起動', flush=True)
    return records, problems


def build_preload(root):
    source = Path(root) / 'preload.c'
    source.write_text(PRELOAD_C)
    library = Path(root) / 'preload.so'
    done = subprocess.run([base.compiler(), '-shared', '-fPIC', '-O1', '-o', str(library), str(source), '-ldl'],
                          capture_output=True)
    if done.returncode:
        raise AssertionError('LD_PRELOAD の部品を組めない: ' + done.stderr.decode(errors='replace'))
    return library


FAULTS = (
    # (名前, 流れ, S3IA_FAIL, Rust の差し込み点, errno, 起こすプログラム〈S3IA_FAIL_COMM。None は全部〉)
    ('mktemp の mkdir が EACCES', 'taskmd-diff same', 'mkdir:.work-', 'mktemp', 13, None),
    ('mktemp の mkdir が ENOSPC', 'taskmd-diff same', 'mkdir:.work-', 'mktemp', 28, None),
    ('rm の unlink が EIO', 'cleanup', 'unlink:manifest.tsv', 'rm-unlink', 5, None),
    ('rm の rmdir が EBUSY', 'cleanup', 'rmdir:snapshot', 'rm-rmdir', 16, None),
    ('印の rm が EIO', 'cleanup', 'unlink:owner-fixed', 'rm-mark', 5, None),
    ('load_meta の 4-meta.txt の read が EIO', 'taskmd-diff same', 'read:/snapshot/4-meta.txt', 'read', 5, 'bash'),
    ('load_meta の taskmd.txt の read が EIO', 'taskmd-diff same', 'read:/snapshot/taskmd.txt', 'read', 5, 'bash'),
    ('T 行の read が EIO(taskmd-diff)', 'taskmd-diff same', 'read:/manifest.tsv', 'read', 5, 'bash'),
    ('T 行の read が EIO(restore-taskmd)', 'restore-taskmd', 'read:/manifest.tsv', 'read', 5, 'bash'),
    ('sed の読み取りが EIO(退避した本文)', 'taskmd-diff checks-only', 'read:/taskmd-body', 'read-sed', 5, 'sed'),
    ('sed の読み取りが EIO(タスク本文)', 'taskmd-diff checks-only', 'read:/task/t.md', 'read-sed', 5, 'sed'),
    ('cmp の読み取りが EIO(退避した本文)', 'taskmd-diff same', 'read:/taskmd-body', 'read-cmp', 5, 'cmp'),
    ('cmp の読み取りが EIO(タスク本文)', 'taskmd-diff same', 'read:/task/t.md', 'read-cmp', 5, 'cmp'),
)
# 測らない地点(類推の期待で合格にしない)。
UNMEASURED = (
    ('exec </dev/null の失敗', 'ここでは測らない。比較器の「/dev/null を開けない起動」(利用者の名前空間で /dev/null を'
     '開けないファイルに差し替える)で、旧版と Rust を直接比べる。名前空間を作れない機械では、比較器が未実施と記録する'),
    ('checks-only の cmp(before.norm と after.norm)の読み取りの EIO', 'Rust に同じ地点が無い。Rust は 00:35 の決定で'
     '正規化した中間のファイルを作らず、値で比べるので、そのファイルの読み取りが無い'),
    ('restore-taskmd の旧 helper の exec(python3 -I -B restore-taskmd.py restore …)', '旧版の bash は exec で旧 helper に'
     '置き換わるので、bash の罠の地点ではない。exec の後の信号は旧 helper の Python が受け、R4R の比較のとおり'),
)


def measure_faults(root):
    library = build_preload(root)
    scenarios = dict(scenario_worlds())
    records = []
    for name, label, fail, point, errno_value, comm in FAULTS:
        world = base.make_world(Path(root) / 'w')
        argv = scenarios[label](world)
        extra = {'LD_PRELOAD': str(library), 'S3IA_FAIL': f'{fail}:{errno_value}'}
        if comm:
            extra['S3IA_FAIL_COMM'] = comm
        result = run_old(world, argv, None, extra_env=extra)
        record = {'fault': name, 'scenario': label, 'point': point, 'errno': errno_value, 'spec': fail,
                  'comm': comm}
        record.update(outcome(world, result))
        records.append(record)
    return records


def measure(root, rules):
    global FAKEBIN
    missing = base.baseline()
    if missing:
        raise AssertionError(f'基準の版と違う道具がある(測らない): {missing}')
    base.TOOLS = base.Tools(Path(root) / 'tools')
    FAKEBIN = build_fakes(root)
    signals, problems = measure_signals(root, rules)
    data = {
        'about': '旧版の測定の固定期待(implement_guard_measure.py)。信号の地点は、同じ手順を repeat 回(Rust の結果が'
                 '出なければ max_repeat 回まで)流した結果と回数。rule は決定の表の「旧 bash の信号」の規則の 2 通り'
                 '(Rust の単体テストが rust_repeat 回ずつ作る)。outcomes の rule は、見た結果が規則のどれに当たるか。',
        'old_sha256': base.OLD_SHA256,
        'repeat': REPEAT,
        'max_repeat': MAX_REPEAT,
        'rust_repeat': RUST_REPEAT,
        'signals': signals,
        'faults': measure_faults(root),
        'unmeasured': [{'point': point, 'reason': reason} for point, reason in UNMEASURED],
    }
    return data, problems


def plain(item):
    return {k: v for k, v in item.items() if k not in ('count', 'rule')}


def compare_measurement(fixed, data, notes=None):
    """固定期待と今の測定を照らす。失敗にするのは次のとき:
    - 故障・未測定の一覧・測った地点の集合・地点(道具・引数・Rust の地点・何回目)・規則の 2 通りが違う。
    - 揺れない地点で、固定期待の 1 つの結果が今の測定に出ない(旧版の動きが変わった)。
    - Rust の地点で、Rust の結果(規則の「道具の直後」)が 30 回までに出ない(測る側の observe が確かめる)。
    固定期待で見ていない結果が新しく出たときは、旧版どうしの競り合い(22:35・06:58 の決定の「実行ごとに変わる
    出力」)として notes に書き、失敗にしない。規則の 2 通りかどうかも書く(Rust に許すのは、固定期待で見た規則の
    結果だけのまま)。"""
    problems = []
    notes = [] if notes is None else notes
    if fixed.get('faults') != data.get('faults'):
        problems.append('故障の結果が違う')
    if fixed.get('unmeasured') != data.get('unmeasured'):
        problems.append('未測定の一覧が違う')
    old = {(r['scenario'], r['call'], r['signal']): r for r in fixed.get('signals', [])}
    new = {(r['scenario'], r['call'], r['signal']): r for r in data.get('signals', [])}
    if set(old) != set(new):
        problems.append('測った地点の集合が違う')
    for key in sorted(set(old) & set(new)):
        a, b = old[key], new[key]
        if (a['tool'], a['args'], a['rust_point'], a['occurrence'], a.get('rule')) != \
                (b['tool'], b['args'], b['rust_point'], b['occurrence'], b.get('rule')):
            problems.append(f'{key}: 地点か規則の 2 通りが違う')
            continue
        seen = [plain(o) for o in a['outcomes']]
        now = [plain(o) for o in b['outcomes']]
        rule = a.get('rule') or {}
        allowed = [item for item in (rule.get('tool'), rule.get('next_builtin')) if item is not None]
        if a['stable'] and seen[0] not in now:
            problems.append(f'{key}: 揺れない地点の固定期待の結果が、今の {b["runs"]} 回の測定に出ない: {now}')
        for item in now:
            if item in seen:
                continue
            kind = '規則の 2 通りの 1 つ' if item in allowed else ('規則の外' if rule else 'Rust の地点でない')
            count = next(o['count'] for o in b['outcomes'] if plain(o) == item)
            notes.append(f'{key}: 固定期待で見ていない結果を {b["runs"]} 回のうち {count} 回見た({kind}。'
                         f'旧版どうしの競り合い): rc {item["rc"]}・stdout {item["stdout"][:40]!r}')
    return problems


def rules_of(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return data['points']


def main(argv=None):
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--write')
    group.add_argument('--check', nargs='?', const=str(HERE / 'implement_guard_fixtures' / 'expected_old.json'),
                       help='固定期待(省くと native/ci/implement_guard_fixtures/expected_old.json)')
    parser.add_argument('--rules', help='--write のとき: Rust の単体テストが書き出した規則の 2 通り')
    parser.add_argument('--out', help='--check のとき: 測り直した結果を書く(照らしの記録)')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('Python 3.10 系・root 以外で流してください')
    if args.write and not args.rules:
        raise ValueError('--write には --rules が要る')
    base.check_sources()
    started = time.monotonic()
    if args.write:
        rules = rules_of(args.rules)
    else:
        fixed = json.loads(Path(args.check).read_text(encoding='utf-8'))
        rules = {f"{r['scenario']}\t{r['call']}\t{r['signal']}": r['rule'] for r in fixed['signals'] if 'rule' in r}
    root = base.work_root()
    try:
        data, problems = measure(root, rules)
    finally:
        base.make_writable(root)
        shutil.rmtree(root, ignore_errors=True)
    seconds = time.monotonic() - started
    unstable = sum(1 for r in data['signals'] if not r['stable'])
    runs = sum(r['runs'] for r in data['signals'])
    if args.write:
        text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n'
        Path(args.write).write_text(text, encoding='utf-8')
        print(f'S3I-A 測定: 信号 {len(data["signals"])} 地点(揺れた {unstable} 地点・旧版を流した {runs} 回)・'
              f'故障 {len(data["faults"])} 件・未測定 {len(data["unmeasured"])} 件・{seconds:.0f} 秒')
    else:
        if args.out:
            Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                                      encoding='utf-8')
        notes = []
        problems += compare_measurement(fixed, data, notes)
        for note in notes:
            print(f'S3I-A 測定: 記録: {note}')
    if problems:
        for problem in problems:
            print(f'S3I-A 測定: 不合格: {problem}', file=sys.stderr)
        print('S3I-A 測定: 止まって Issue に書く(旧版の測定が規則か固定期待と合わない)', file=sys.stderr)
        return 1
    if args.check:
        print(f'S3I-A 測定: 固定期待と一致した(信号 {len(data["signals"])} 地点・揺れた {unstable} 地点・'
              f'旧版を流した {runs} 回・{seconds:.0f} 秒)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
