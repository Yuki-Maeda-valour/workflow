#!/usr/bin/env python3
"""S3I-A専用比較。旧版 implement-guard.sh(sha256 b958cc12…)を変えず、同じ配置・同じ argv・同じ環境で、
検査専用入口 implement_guard_contract と比べる。

- 起動は比較器の専用の関数(launch。新しいセッションを作らない。環境は並びのまま C の起動器で渡す)。
- 比べるもの: 終了値(信号で終わったらその番号)・stdout・stderr・記録の中のファイル・記録のフォルダの
  有無・所属の印・タスク本文・cp の観測器が記録した子の環境と argv・偽の git の起動の記録。
- 置き換えるのは、旧版の実行環境のパスの既知の欄(bash の文の先頭の呼出名)と、`Current thread` の値と、
  R4R と同じ一時フォルダの 16 進 32 桁と cp の argv の fd 番号だけ。`.work-` の後ろは残ったときだけ形を見る。
- 時間切れ・0 件・境界の印(終了値 99)を比べるケースで受けたことは、成功に数えない。
- 子の環境は、追加の決定 4 の 6 個(CAP・RP・SP_DIR・helper・helper_dir・python_path)だけを両方の集合から外して
  比べ、外した名前ごとの旧版と Rust の有無と値をケースごとに出す。Rust の側は、受け継いだ値と同じか、受け継いで
  いなければ無いことを確かめる。
- 範囲 full の段: 群・決定 D5・PATH を絞った Rust・詰めた FIFO の信号(helper の引数の誤りの応答を書く間の
  SIGINT〈KeyboardInterrupt〉を含む)・Python で書く記録(bash 4.0 以上が無いときの配置。旧版の take の記録と
  同じこと)・strace・/dev/null を開けない起動(名前空間を作れる機械だけ)・fd 0〜2 を
  閉じた起動(08:55 の決定。旧版と Rust をそれぞれの固定期待で確かめ、互いには比べない)・旧 selftest の 2 通り。
- 旧版の測定の照らし(implement_guard_measure.py --check)は、ここでは流さない(手元の基準の容器で流す。CI の
  時間。2026-11-12 12:59 の決定)。ここでは、その固定期待の件数と sha256 だけを確かめる。
"""
import argparse
import contextlib
import ctypes
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
OLD = SCRIPTS / 'implement-guard.sh'
HELPER = SCRIPTS / 'restore-taskmd.py'
SELFTEST = SCRIPTS / 'implement-guard-selftest.sh'
PENDING = SCRIPTS / 'pending-implementation.py'
FIXTURES = Path(__file__).with_name('implement_guard_fixtures')
USAGE_TXT = ROOT / 'native/crates/cli/src/commands/implement_guard/usage.txt'
ERRNO_GLIBC = ROOT / 'native/crates/cli/src/commands/restore_taskmd/errno_glibc.rs'
OLD_SHA256 = 'b958cc12b60c26364d3eff11d9f31a98b532363d9556221f7e58b8d2a50a6981'
HELPER_SHA256 = '0f33bf55b247ec289acb935db0c24061617deebd6b6e6dc70c52738bfdf97a90'
SYSTEM = 'linux' if sys.platform.startswith('linux') else 'darwin' if sys.platform == 'darwin' else 'other'
LINUX = SYSTEM == 'linux'
FIXTURE = None
BOUNDARY = 99
VERSIONS = {
    'bash': '5.2.21', 'coreutils': '9.4', 'findutils': '4.9.0', 'sed': '4.9', 'grep': '3.11', 'glibc': '2.39',
    'python': '3.10.11',
}


class Mismatch(Exception):
    """旧版と Rust の結果の不一致。"""


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def fixture_path(value):
    """検査入口の絶対パス。省略・空・相対・不在・通常ファイル以外・実行不能は失敗。"""
    if not value or not os.path.isabs(value):
        raise ValueError('--fixture は今回ビルドした implement_guard_contract の絶対パスで指定してください')
    path = Path(value)
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('--fixture は実行可能な通常ファイルである必要があります')
    return path


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_sources():
    """旧版と旧 helper の sha256、使い方の文が旧版の 2 行目から番兵の前までと同じこと。"""
    if sha256_file(OLD) != OLD_SHA256:
        raise AssertionError('旧版の sha256 が違う(固定期待と Rust の使い方の文を直す合図)')
    if sha256_file(HELPER) != HELPER_SHA256:
        raise AssertionError('旧 helper の sha256 が違う')
    lines = OLD.read_bytes().split(b'\n')
    end = lines.index(b'# --- end usage ---')
    if b''.join(line + b'\n' for line in lines[1:end]) != USAGE_TXT.read_bytes():
        raise AssertionError('Rust の使い方の文が旧版の 2 行目から番兵の前までと違う')


# ---- 道具の版 ----

def first_line(argv):
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30, env={'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return done.stdout.decode(errors='replace').splitlines()[0] if done.stdout else ''


def glibc_version():
    try:
        libc = ctypes.CDLL(None)
        function = libc.gnu_get_libc_version
        function.restype = ctypes.c_char_p
        return function().decode()
    except (OSError, AttributeError):
        return ''


def tool_versions():
    found = {
        'bash': re.search(r'version (\d+\.\d+\.\d+)', first_line(['bash', '--version'])),
        'coreutils': re.search(r'coreutils\) (\S+)', first_line(['cp', '--version'])),
        'findutils': re.search(r'findutils\) (\S+)', first_line(['find', '--version'])),
        'sed': re.search(r'GNU sed\) (\S+)', first_line(['sed', '--version'])),
        'grep': re.search(r'GNU grep\) (\S+)', first_line(['grep', '--version'])),
    }
    versions = {name: (match.group(1) if match else '') for name, match in found.items()}
    versions['glibc'] = glibc_version()
    versions['python'] = '.'.join(map(str, sys.version_info[:3]))
    return versions


def baseline():
    """基準の版と違う道具の名前の一覧(空なら基準の環境)。"""
    versions = tool_versions()
    return {name: versions.get(name, '') for name, want in VERSIONS.items() if versions.get(name) != want}


# ---- C の部品 ----

LAUNCHER_C = r'''/* 起動器: launcher ENTRY... -- PATH ARGV0 ARGS... 。環境を並びのまま(重複・名前の無い項目も)渡す。 */
#include <stdio.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    static char *env[65536];
    int i = 1, n = 0;
    for (; i < argc && strcmp(argv[i], "--") != 0; i++) {
        if (n < 65535) env[n++] = argv[i];
    }
    env[n] = 0;
    if (i + 2 >= argc) return 125;
    execve(argv[i + 1], argv + i + 2, env);
    perror("launcher: execve");
    return 126;
}
'''

FAKE_GIT = '''#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
exit 1
'''


def compiler():
    found = shutil.which('cc')
    if not found:
        raise AssertionError('cc が無いので、起動器と観測器を組めない')
    return found


class Tools:
    """比較器が組む C の部品と偽の道具。"""

    def __init__(self, base):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        source = self.base / 'launcher.c'
        source.write_text(LAUNCHER_C)
        self.launcher = self.base / 'launcher'
        built = subprocess.run([compiler(), '-O1', '-o', str(self.launcher), str(source)], capture_output=True)
        if built.returncode:
            raise AssertionError('起動器を組めない: ' + built.stderr.decode(errors='replace'))
        self.git_dir = self.base / 'fakegit'
        self.git_dir.mkdir(exist_ok=True)
        self.git_log = self.base / 'git.log'
        git = self.git_dir / 'git'
        git.write_text(FAKE_GIT.format(log=self.git_log))
        git.chmod(0o755)
        self.fixed_dir = self.base / 'fixedtmp'
        self.fixed_dir.mkdir(exist_ok=True)
        real_mktemp = shutil.which('mktemp')
        stub = self.fixed_dir / 'mktemp'
        stub.write_text('#!/usr/bin/env bash\nlast="${*: -1}"\ncase "$last" in\n  */guard-XXXXXX)\n'
                        '    p="${last%XXXXXX}${FIXED_GUARD_SUFFIX:-fixed}"\n    mkdir -m 700 -- "$p" || exit 1\n'
                        '    printf \'%s\\n\' "$p"\n    exit 0 ;;\nesac\nexec "' + real_mktemp + '" "$@"\n')
        stub.chmod(0o755)

    def git_calls(self):
        if not self.git_log.exists():
            return 0
        return len(self.git_log.read_text().splitlines())

    def reset_git(self):
        if self.git_log.exists():
            self.git_log.unlink()


# ---- 起動と記録 ----

@dataclasses.dataclass(frozen=True)
class Result:
    code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    seconds: float


class RawFd:
    """Popen へ数のまま渡す fd(フォルダは Python のファイルにできない)。"""

    def __init__(self, fd):
        self.fd = fd

    def close(self):
        os.close(self.fd)


def stream_spec(kind, base, label):
    """stdout・stderr の向き先。(Popen に渡す値, 閉じる物, 後で読むファイル)。"""
    if kind == 'pipe':
        return subprocess.PIPE, None, None
    if kind == 'full':
        handle = open('/dev/full', 'wb')
        return handle, handle, None
    if kind == 'closed':
        read, write = os.pipe()
        os.close(read)
        handle = os.fdopen(write, 'wb')
        return handle, handle, None
    if kind == 'dir':
        fd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
        return fd, RawFd(fd), None
    if kind.startswith('file'):
        path = Path(base) / f'out-{label}'
        handle = open(path, 'wb')
        return handle, handle, path
    if kind == 'null':
        handle = open(os.devnull, 'wb')
        return handle, handle, None
    raise ValueError(kind)


def launch(program, argv0, args, env, *, cwd, stdout='pipe', stderr='pipe', base=None, timeout=60,
           preexec=None, during=None, label='x', wrapper=()):
    """新しいセッションを作らず、stdin は /dev/null。環境は bytes の並びのまま渡す。wrapper は起動器の前に
    置くコマンド(/dev/null を開けない起動だけ)。"""
    command = [*wrapper, str(TOOLS.launcher), *env, '--', str(program), argv0, *args]
    out, out_close, out_path = stream_spec(stdout, base or cwd, label + '-1')
    err, err_close, err_path = stream_spec(stderr, base or cwd, label + '-2')
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd=cwd,
                                   preexec_fn=preexec, close_fds=True)
    finally:
        for handle in (out_close, err_close):
            if handle is not None:
                handle.close()
    if during is not None:
        try:
            during(process)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    timed_out = False
    try:
        got_out, got_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        got_out, got_err = process.communicate()
    if out_path is not None:
        got_out = out_path.read_bytes()
        out_path.unlink()
    if err_path is not None:
        got_err = err_path.read_bytes()
        err_path.unlink()
    return Result(process.returncode, got_out or b'', got_err or b'', timed_out, time.monotonic() - started)


BASH = None


def find_bash4():
    """bash 4.0 以上(PATH・/opt/homebrew/bin/bash・/usr/local/bin/bash の順)。無ければ None。"""
    for candidate in (shutil.which('bash'), '/opt/homebrew/bin/bash', '/usr/local/bin/bash'):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        done = subprocess.run([candidate, '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True)
        if done.returncode == 0 and done.stdout.strip().isdigit() and int(done.stdout) >= 4:
            return candidate
    return None


def old_command():
    bash = BASH or shutil.which('bash')
    return bash, 'bash', [str(OLD)]


def rust_command():
    return FIXTURE, str(FIXTURE), []


THREAD = re.compile(rb'Current thread 0x[0-9a-f]{16} \(most recent call first\)')
# 旧版の printf の `%(` は、書式の終わりより後ろのメモリの 1 文字を出す(実行ごとに変わる)。22:31 の決定の
# 「実行ごとに変わる出力」の範囲として採り(設計 v18・3 回目の依頼)、その 1 文字だけを伏せる。ほかの文字は全部比べる。
TIME_FORMAT = re.compile(rb"(printf: warning: `).(': invalid time format specification)")
TEMP_HEX = re.compile(rb'(\.restore-taskmd-)[0-9a-f]{32}')


def normalize(result, legacy):
    """旧版の呼出名(bash の文の先頭)を固定名に置き換え、`Current thread` の値を伏せる。"""
    err = result.stderr
    if legacy:
        err = err.replace(str(OLD).encode() + b': line ', b'implement-guard.sh: line ')
    err = THREAD.sub(b'Current thread 0x<thread> (most recent call first)', err)
    err = TIME_FORMAT.sub(rb'\1?\2', err)
    out = TEMP_HEX.sub(lambda m: m.group(1) + b'X' * 32, result.stdout)
    return dataclasses.replace(result, stdout=out, stderr=err)


def tree(path):
    """フォルダの中身(種類・mode・中身・リンクの字面)。`.work-` の後ろ 6 文字は伏せる(形は確かめる)。"""
    path = Path(path)
    if not os.path.lexists(path):
        return None
    record = {}
    for current, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            full = Path(current) / name
            rel = os.path.relpath(full, path)
            if re.fullmatch(r'\.work-[A-Za-z0-9]{6}', name):
                rel = os.path.join(os.path.dirname(rel), '.work-XXXXXX')
            elif name.startswith('.work-'):
                raise Mismatch(f'.work- の名前の形が違う: {name}')
            record[rel] = entry(full)
    record['.'] = entry(path)
    return record


def entry(path):
    try:
        info = os.lstat(path)
    except PermissionError as error:
        # 検索できないフォルダの中の名前(読めるが x の無いフォルダ)は、種類を取れない。名前と番号だけを記録する。
        return {'unstatable': error.errno}
    kind = stat.S_IFMT(info.st_mode)
    value = {'kind': kind, 'mode': stat.S_IMODE(info.st_mode)}
    if stat.S_ISLNK(info.st_mode):
        value['link'] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        try:
            data = Path(path).read_bytes()
            if os.path.basename(path).startswith('owner-'):
                # 所属の印の device・inode は、配置を写すたびに変わる(写した後に合わせ直す)。
                data = re.sub(rb'(_(?:dev|ino)\t)[0-9]+', rb'\1<n>', data)
            value['sha256'] = hashlib.sha256(data).hexdigest()
        except OSError as error:
            value['sha256'] = f'unreadable:{error.errno}'
    return value


def file_record(path):
    if not os.path.lexists(path):
        return None
    return entry(path)


# ---- 比較の入力に使わない環境変数(追加の決定 3) ----

EXCLUDED_ENV = FIXTURES / 'excluded_env.json'
EXCLUDED_ENV_FIXED = (15, 'e851b9ecbf022124bbe5d1df36a93228b49d090d6d88afda30ff26f810aaf827')


def excluded_env():
    data = EXCLUDED_ENV.read_bytes()
    parsed = json.loads(data)
    if (len(parsed['names']), hashlib.sha256(data).hexdigest()) != EXCLUDED_ENV_FIXED:
        raise AssertionError('比較に使わない環境変数の一覧の件数か sha256 が固定と違う')
    prefixes = list(parsed['prefixes']) + (list(parsed['mac_prefixes']) if SYSTEM == 'darwin' else [])
    return set(parsed['names']), tuple(prefixes)


def check_input_env(entries, allow=()):
    """ケースが渡す変数が、一覧(決定 3)に入っていないこと。警告のケースだけ allow の名前を許す。"""
    names, prefixes = excluded_env()
    for entry in entries:
        name = entry.split(b'=', 1)[0].decode(errors='replace')
        if name in allow:
            continue
        if name in names or name.startswith(prefixes):
            raise AssertionError(f'比較の入力に使わない環境変数をケースが渡した: {name}')


# ---- 配置 ----

@dataclasses.dataclass
class World:
    """1 つのケースの配置(リポジトリ・XDG・HOME)。"""
    base: Path
    repo: Path
    xdg: Path
    home: Path
    state: str = ''
    man: str = ''
    snap: str = ''
    real: str = ''

    def values(self, **override):
        values = {'cwd': str(self.repo), 'state': self.state, 'man': self.man, 'snap': self.snap}
        values.update(override)
        return values


def run_git(cwd, *args):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                          env={**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null'})
    if done.returncode:
        raise AssertionError('git の準備が失敗した: ' + done.stderr.decode(errors='replace'))


TASK_TEXT = b'# t\n- [ ] one\n- [ ] two\nbody\n'


def make_world(base, *, task=TASK_TEXT, task_path='task/t.md', suffix='fixed', prepare=None, take=True,
               select=None):
    """git のリポジトリと未追跡のタスク本文を作り、旧版の take で記録を作る(guard-<suffix> の名前に決める)。
    bash 4.0 以上が無ければ、同じ形の記録を Python で書く(take_record)。"""
    base = Path(base)
    if base.exists():
        make_writable(base)
        shutil.rmtree(base)
    repo = base / 'repo'
    xdg = base / 'xdg'
    home = base / 'home'
    for directory in (repo, xdg, home):
        directory.mkdir(parents=True)
    run_git(repo, 'init', '-q', '.')
    run_git(repo, 'config', 'user.email', 'a@example.invalid')
    run_git(repo, 'config', 'user.name', 'n')
    (repo / 'a.txt').write_bytes(b'x\n')
    run_git(repo, 'add', 'a.txt')
    run_git(repo, 'commit', '-qm', 'init')
    task_file = repo / os.fsdecode(task_path) if isinstance(task_path, bytes) else repo / task_path
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_bytes(task)
    world = World(base, repo, xdg, home)
    if prepare is not None:
        prepare(world)
    if take:
        env = {**os.environ, 'XDG_STATE_HOME': str(xdg), 'HOME': str(home), 'FIXED_GUARD_SUFFIX': suffix,
               'PATH': f'{TOOLS.fixed_dir}:{os.environ.get("PATH", "")}', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        env.pop('TMPDIR', None)
        selected = select if select is not None else task_path
        fields = take_record(repo, selected, env, '旧版の take が失敗した: ')
        world.state = os.fsdecode(fields[b'STATE_DIR'])
        world.man = fields[b'MANIFEST_SHA256'].decode()
        world.snap = fields[b'SNAPSHOT_SHA256'].decode()
        world.real = os.fsdecode(unquote(fields[b'TASKMD_REAL']))
    return world


ESCAPES = {b'a': b'\a', b'b': b'\b', b't': b'\t', b'n': b'\n', b'v': b'\v', b'f': b'\f', b'r': b'\r',
           b'"': b'"', b'\\': b'\\'}


def unquote(value):
    """旧版の C 風引用(path_field)を戻す。"""
    if not (value.startswith(b'"') and value.endswith(b'"') and len(value) >= 2):
        return value
    body = value[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i:i + 1]
        if c != b'\\':
            out += c
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in ESCAPES:
            out += ESCAPES[nxt]
            i += 2
        else:
            out.append(int(body[i + 1:i + 4], 8))
            i += 4
    return bytes(out)


def make_writable(path):
    for current, dirs, files in os.walk(path):
        for name in dirs:
            full = os.path.join(current, name)
            if not os.path.islink(full):
                os.chmod(full, 0o700)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, 0o700)


def snapshot_digest(state):
    """旧版の snapshot_digest と同じ値(読めない・通常ファイルでない名前は、旧版が失敗するので仮の値)。"""
    snapshot = Path(state) / 'snapshot'
    names = sorted(n for n in os.listdir(os.fsencode(snapshot)) if not n.startswith(b'.'))
    text = b''
    for name in names:
        path = snapshot / os.fsdecode(name)
        try:
            digest = sha256_file(path) if path.is_file() and not path.is_symlink() else '-'
        except OSError:
            digest = '-'
        text += digest.encode() + b'  ' + name + b'\n'
    return hashlib.sha256(text).hexdigest()


def reseal(world):
    """記録を書き換えた後に、2 つのダイジェストを合わせ直す。"""
    world.man = sha256_file(Path(world.state) / 'manifest.tsv')
    world.snap = snapshot_digest(world.state)


# ---- 旧版の take と同じ形の記録(bash 4.0 以上が無いとき。設計 Task 4.1・テスト計画の「Mac」) ----
#
# bash 4.0 以上が無い機械(GitHub の macOS の実行機の Bash 3.2.57)では、旧版の take が `bash 4.0 以上が必要` で
# 止まる。範囲 mac・signed の固定期待の配置は、旧版の cmd_take を比較器の配置(1 つのリポジトリ。下のリポジトリ・
# .gitmodules・include の無い設定・unmerged が無い)の範囲で写した Python で書く。同じ配置で旧版の take の記録と
# 同じこと(記録の中のファイルの名前・種類・mode・中身、take の stdout と stderr、記録を読む旧版と Rust の結果)は、
# Linux の範囲 full の段 run_record_writer が確かめる。範囲の外の配置は推し量らずに止める。

TAKE_GIT_PRE = ('--no-pager', '--no-replace-objects', '-c', 'core.quotePath=false', '-c', 'core.fsmonitor=',
                '-c', 'core.hooksPath=/dev/null', '-c', 'core.ignoreCase=false', '-c', 'core.splitIndex=false')
TAKE_GIT_PRE_HOOKS = ('--no-pager', '--no-replace-objects', '-c', 'core.quotePath=false', '-c', 'core.fsmonitor=',
                      '-c', 'core.ignoreCase=false', '-c', 'core.splitIndex=false')
TAKE_DIFF_CFG = ('-c', 'diff.autoRefreshIndex=false', '-c', 'core.ignoreStat=false')
TAKE_CONFIG_ENV = ('HOME', 'XDG_CONFIG_HOME', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM',
                   'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS')
TAKE_APPLE_GIT = re.compile(rb'git version [^ ]+ \(Apple Git-[0-9]+(\.[0-9]+)*\)')
TAKE_MAN_PATH = re.compile(rb'/.+/share/man')
QUOTES = {value: key for key, value in ESCAPES.items() if key not in (b'"', b'\\')}
RECORD_CHECK = None   # list を入れると、take_record が旧版の take と Python の両方で記録を作り、違いを足す


class TakeOutside(AssertionError):
    """Python の記録が写していない配置(旧版の take なら縮退か別の記録になる)。"""


def path_field(value):
    """旧版の path_field(needs_quote のときだけ cquote。LC_ALL=C の 1 バイトずつ)。"""
    if not (value.startswith(b'"') or b'"' in value or b'\\' in value or any(c < 0x20 or c == 0x7f for c in value)):
        return value
    out = bytearray(b'"')
    for c in value:
        ch = bytes([c])
        if ch in (b'"', b'\\'):
            out += b'\\' + ch
        elif ch in QUOTES:
            out += b'\\' + QUOTES[ch]
        elif c < 0x20 or c == 0x7f:
            out += b'\\%03o' % c
        else:
            out += ch
    return bytes(out + b'"')


def split_path(value):
    """旧版の split_path → (SP_DIR, SP_BASE)。"""
    if re.fullmatch(rb'.*/.+', value, re.S):
        head, _, tail = value.rpartition(b'/')
        return head or b'/', tail
    if value == b'/':
        return b'/', b''
    return b'.', value


def norm_rest(rest):
    """旧版の norm_rest(まだ無い部分の `..` を語彙的に打ち消す。打ち消しきれなければ None)。"""
    out = []
    for segment in rest.split(b'/'):
        if segment in (b'', b'.'):
            continue
        if segment == b'..':
            if not out:
                return None
            out.pop()
        else:
            out.append(segment)
    return b'/'.join(out)


def mode_text(path):
    """旧版の stat_mode(リンクを辿らない 8 進。Rust の mode_text と同じ)。"""
    return b'%o' % (os.lstat(path).st_mode & 0o7777)


def identity(path):
    """旧版の stat_identity(`stat -c '%d:%i'`。リンクを辿らない)。"""
    info = os.lstat(path)
    return b'%d:%d' % (info.st_dev, info.st_ino)


def sha_hex(data):
    return hashlib.sha256(data).hexdigest().encode()


class PyTake:
    """旧版の cmd_take の写し。引数・環境は旧版の take の起動と同じ(env は起動に渡す dict)。"""

    def __init__(self, cwd, select, env):
        self.cwd = os.fsencode(cwd)
        self.task_md = os.fsencode(select)
        self.env = dict(env)
        self.git_env = {k: v for k, v in env.items()
                        if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX')}
        self.git_env.update(LC_ALL='C', GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0')
        self.stderr = bytearray()
        self.top = self.cwd

    def var(self, name):
        value = self.env.get(name)
        return None if value is None else os.fsencode(value)

    def git(self, *args, cwd=None, env=None, stdin=None):
        done = subprocess.run(['git', *args], cwd=cwd if cwd is not None else self.top,
                              env=env if env is not None else self.git_env, input=stdin, capture_output=True)
        return done.returncode, done.stdout

    def capture(self, *args, **kw):
        """capture_path(末尾の改行 1 個を落とす)。"""
        rc, out = self.git(*args, **kw)
        return rc, out[:-1] if out.endswith(b'\n') else out

    def must(self, *args, **kw):
        rc, out = self.git(*args, **kw)
        if rc:
            raise TakeOutside(f'git {args[-1]!r} が終了値 {rc} を返した')
        return out

    def run(self):
        mask = os.umask(0o077)
        try:
            return self._run()
        finally:
            os.umask(mask)

    def _run(self):
        if not os.path.isdir(self.cwd):
            raise TakeOutside('--cwd が無い')
        rc, top_raw = self.capture(*TAKE_GIT_PRE, '-C', self.cwd, 'rev-parse', '--show-toplevel', cwd=self.cwd)
        if rc or not top_raw:
            raise TakeOutside('not-git')
        self.top = os.path.realpath(top_raw)
        self.cwd_phys = os.path.realpath(self.cwd)
        if not (self.cwd_phys == self.top or self.cwd_phys.startswith(self.top + b'/')):
            raise TakeOutside('toplevel が --cwd を含まない')
        reviews = b'.claude/reviews'
        self.excl = reviews if self.cwd_phys == self.top else self.cwd_phys[len(self.top) + 1:] + b'/' + reviews
        self.excl_tmp = os.path.realpath(b'/tmp') if os.path.isdir(b'/tmp') else b''
        tmpdir = self.var('TMPDIR')
        self.excl_tmpdir = os.path.realpath(tmpdir) if tmpdir and os.path.isdir(tmpdir) else b''
        state_base = self.resolve_state_base()
        owner = self.prepare_owner_mark()
        suffix = self.var('FIXED_GUARD_SUFFIX') or b'fixed'   # 比較器の mktemp(guard-XXXXXX の名前を決める)
        new_state = state_base + b'/guard-' + suffix
        os.mkdir(new_state, 0o700)
        os.chmod(new_state, 0o700)
        work = new_state + b'/.work'
        for name in (work, new_state + b'/snapshot', new_state + b'/files'):
            os.mkdir(name)
        mark = self.write_owner_mark(new_state, owner, work)
        self.taskmd(new_state, mark, owner)
        # 旧版はここで cd -P -- "$TOP" する。ここから先の git は、cwd を TOP にして起動する(self.git の既定)。
        self.config_origins(new_state, work)
        self.collect_git(new_state, work)
        return self.backup(new_state, work, mark)

    # -- 保護領域(resolve_state_base) --
    def forbidden(self, path):
        return any(d and (path == d or path.startswith(d + b'/')) for d in (self.excl_tmp, self.excl_tmpdir, self.top))

    def resolve_future(self, path):
        rest = b''
        while True:
            if os.path.isdir(path):
                real = os.path.realpath(path)
                if rest:
                    rest = norm_rest(rest)
                    if rest is None:
                        return None
                    if rest:
                        real = real + b'/' + rest
                return real
            if os.path.lexists(path) or path in (b'/', b'.', b''):
                return None
            path, base = split_path(path)
            rest = base + b'/' + rest if rest else base

    def resolve_state_base(self):
        home = self.var('HOME') or b''
        for candidate in (self.var('XDG_STATE_HOME') or b'', home + b'/.local/state', home + b'/.cache'):
            if candidate in (b'', b'/.local/state', b'/.cache') or not candidate.startswith(b'/'):
                continue
            want = self.resolve_future(candidate + b'/dev-workflow')
            if want is None or self.forbidden(want):
                continue
            try:
                os.makedirs(want, exist_ok=True)
            except OSError:
                continue
            with contextlib.suppress(OSError):
                os.chmod(want, 0o700)
            if not os.path.isdir(want):
                continue
            real = os.path.realpath(want)
            if not self.forbidden(real):
                return real
        raise TakeOutside('no-protected-area')

    # -- 所属の印(prepare_owner_mark・write_owner_mark) --
    def prepare_owner_mark(self):
        path = os.fsdecode(self.var('PATH') or b'')
        for tool in ('link', 'od'):
            if shutil.which(tool, path=path) is None:
                return {'skip': f'{tool} が無い'.encode()}
        rc, common = self.capture(*TAKE_GIT_PRE, '-C', self.top, 'rev-parse', '--git-common-dir')
        if rc or not common:
            return {'skip': '共通の置き場を求められない'.encode()}
        common = os.path.realpath(common if common.startswith(b'/') else self.top + b'/' + common)
        task = self.task_md if self.task_md.startswith(b'/') else self.cwd_phys + b'/' + self.task_md
        if not os.path.exists(task):
            return {'skip': 'タスク MD の在るフォルダを求められない'.encode()}
        taskdir = os.path.realpath(split_path(os.path.realpath(task))[0])
        tmpdir = self.var('TMPDIR') or b''
        if tmpdir:
            if not tmpdir.startswith(b'/'):
                return {'skip': 'TMPDIR が相対パス(外部の実装役の起動場所によって指す先が変わる)'.encode()}
            if not self.excl_tmpdir:
                return {'skip': 'TMPDIR の物理パスを求められない'.encode()}
            tmpdir = self.excl_tmpdir
        return {'skip': b'', 'common': common, 'taskdir': taskdir, 'tmpdir': tmpdir,
                'ids': [identity(p) for p in (self.top, common, taskdir)]}

    def write_owner_mark(self, new_state, owner, work):
        if owner['skip']:
            self.stderr += 'NOTE: 所属の印を書けない: '.encode() + owner['skip'] + b'\n'
            return False
        guard = identity(new_state).split(b':')
        lines = [b'version\t1', b'guard_dev\t' + guard[0], b'guard_ino\t' + guard[1]]
        for key, value, ident in ((b'top', self.top, owner['ids'][0]), (b'common', owner['common'], owner['ids'][1]),
                                  (b'taskdir', owner['taskdir'], owner['ids'][2])):
            dev, ino = ident.split(b':')
            lines += [key + b'\t' + value.hex().encode(), key + b'_dev\t' + dev, key + b'_ino\t' + ino]
        lines.append(b'tmpdir\t' + owner['tmpdir'].hex().encode())
        tmp = work + b'/owner-mark'
        with open(tmp, 'wb') as handle:
            handle.write(b''.join(line + b'\n' for line in lines))
        os.chmod(tmp, 0o600)
        mark = os.path.dirname(new_state) + b'/owner-' + os.path.basename(new_state)[len(b'guard-'):]
        if os.path.lexists(mark):
            os.unlink(tmp)
            self.stderr += 'NOTE: 所属の印を書けない: 印の名前に既にエントリが在る: '.encode() + path_field(mark) + b'\n'
            return False
        os.link(tmp, mark)
        os.unlink(tmp)
        return True

    # -- タスク MD の実体と taskmd.txt --
    def taskmd(self, new_state, mark, owner):
        select = self.task_md if self.task_md.startswith(b'/') else self.cwd_phys + b'/' + self.task_md
        parent, base = split_path(select)
        if not os.path.isdir(parent):
            raise TakeOutside('taskmd(親が無い)')
        self.sel = os.path.realpath(parent) + b'/' + base
        kind = b'symlink' if os.path.islink(self.sel) else b'file'
        if not os.path.exists(self.sel):
            raise TakeOutside('taskmd(無い)')
        self.real = os.path.realpath(self.sel)
        if os.path.islink(self.real) or not os.path.isfile(self.real) or not os.access(self.real, os.R_OK):
            raise TakeOutside('taskmd(通常ファイルでない・読めない)')
        if mark and split_path(self.real)[0] != owner['taskdir']:
            raise TakeOutside('タスク MD の在るフォルダが、印を書いた後に変わった')
        self.real_rel = self.real[len(self.top) + 1:] if self.real.startswith(self.top + b'/') else b'\x01'
        self.sel_rel = self.sel[len(self.top) + 1:] if self.sel.startswith(self.top + b'/') else b'\x01'
        with open(new_state + b'/snapshot/taskmd.txt', 'wb') as handle:
            handle.write(b'selected\t' + path_field(self.sel) + b'\nreal\t' + path_field(self.real) + b'\nkind\t'
                         + kind + b'\n')

    # -- 設定の出典(collect_config_origins・collect_context_plan) --
    def env_row(self, name):
        value = self.var(name)
        state = b'unset' if value is None else b'set'
        return b'env\t' + name.encode() + b'\t' + state + b'\t' + sha_hex(name.encode() + b':' + state + b':'
                                                                        + (value or b''))

    def isolated(self, work, *args):
        """isolated_config_git(設定の選択を隔離した git config)。"""
        parse = work + b'/config-parse'
        if not os.path.isdir(parse + b'/repo.git'):
            for name in (b'/repo.git/objects', b'/repo.git/refs'):
                os.makedirs(parse + name)
            Path(os.fsdecode(parse + b'/repo.git/config')).write_bytes(b'')
            Path(os.fsdecode(parse + b'/repo.git/HEAD')).write_bytes(b'ref: refs/heads/guard\n')
        env = {'PATH': self.env.get('PATH', ''), 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_COUNT': '0',
               'GIT_DIR': os.fsdecode(parse + b'/repo.git'), 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'}
        if 'HOME' in self.env:
            env['HOME'] = self.env['HOME']
        return self.git(*TAKE_GIT_PRE, '-C', parse, *args, cwd=parse, env=env)

    def walk(self, target, rows):
        """candidate_walk(take)。'file' か None(無い)を返す。"""
        if not target.startswith(b'/'):
            raise TakeOutside('候補が絶対パスでない')
        rest, path, links = target[1:], b'/', 0
        while True:
            link = b'-'
            if os.path.islink(path):
                kind, link = b'l', os.readlink(path)
            elif os.path.isdir(path):
                kind = b'd'
            elif os.path.isfile(path):
                kind = b'f'
            elif os.path.exists(path):
                raise TakeOutside('設定の候補が通常ファイルでもフォルダでもない')
            else:
                rows.append(b'missing\t' + path_field(path))
                return None
            rows.append(b'node\t' + kind + b'\t' + identity(path) + b'\t' + path_field(link) + b'\t'
                        + path_field(path))
            if kind == b'l':
                links += 1
                if links > 40 or not link:
                    raise TakeOutside('設定の候補のリンクが多すぎる')
                if rest:
                    link = link + b'/' + rest
                if link.startswith(b'/'):
                    path, rest = b'/', link[1:]
                else:
                    path, rest = split_path(path)[0], link
                continue
            if not rest:
                if kind != b'f' or not os.access(path, os.R_OK):
                    raise TakeOutside('設定の候補を読めない')
                rows.append(b'data\t' + sha_hex(Path(os.fsdecode(path)).read_bytes()))
                return 'file'
            if kind != b'd':
                raise TakeOutside('設定の候補の途中がフォルダでない')
            part, _, rest = rest.partition(b'/')
            if part:
                path = (path[:-1] if path.endswith(b'/') else path) + b'/' + part

    def config_origins(self, new_state, work):
        rows = [b'config-origins-v3']
        queue = []
        seen = set()

        def enqueue(path, kind='config'):
            if not path.startswith(b'/'):
                raise TakeOutside('設定の候補が絶対パスでない')
            if path not in seen:
                seen.add(path)
                queue.append((path, kind))

        def admin_entry(path):
            enqueue(path + b'/.implement-guard-admin-entry' if os.path.isdir(path) else path, 'admin')

        def config_path(path):
            if path.startswith(b'/'):
                enqueue(path)
            else:
                enqueue(self.cwd_phys + b'/' + path)
                if self.cwd_phys != self.top:
                    enqueue(self.top + b'/' + path)

        def absolute(path):
            return path if path.startswith(b'/') else self.top + b'/' + path

        def rev_parse(*args):
            rc, out = self.capture(*TAKE_GIT_PRE, '-C', self.top, 'rev-parse', *args)
            if rc or not out:
                raise TakeOutside(f'rev-parse {args}')
            return absolute(out)

        rows += [self.env_row(name) for name in TAKE_CONFIG_ENV]
        count = self.var('GIT_CONFIG_COUNT') or b'0'
        if not count.isdigit():
            raise TakeOutside('GIT_CONFIG_COUNT が数でない')
        for index in range(int(count)):
            rows += [self.env_row(f'GIT_CONFIG_KEY_{index}'), self.env_row(f'GIT_CONFIG_VALUE_{index}')]
        enqueue(rev_parse('--git-path', 'config'))
        enqueue(rev_parse('--git-path', 'config.worktree'))
        common = rev_parse('--git-common-dir')
        enqueue(common + b'/config')
        enqueue(common + b'/config.worktree')
        gitdir = rev_parse('--git-dir')
        enqueue(gitdir + b'/config')
        enqueue(gitdir + b'/config.worktree')
        rows.append(b'plan-admin\t' + path_field(gitdir) + b'\t' + path_field(common))
        admin_entry(self.top + b'/.git')
        admin_entry(gitdir)
        enqueue(gitdir + b'/commondir', 'admin')
        admin_entry(common)
        global_config = self.var('GIT_CONFIG_GLOBAL')
        home = self.var('HOME')
        if global_config is not None:
            if global_config and global_config != b'/dev/null':
                config_path(global_config)
        else:
            if home is not None:
                config_path(home + b'/.gitconfig')
            if self.var('XDG_CONFIG_HOME'):
                config_path(self.var('XDG_CONFIG_HOME') + b'/git/config')
            elif home is not None:
                config_path(home + b'/.config/git/config')
        system = self.var('GIT_CONFIG_NOSYSTEM') is None
        if not system:
            raise TakeOutside('GIT_CONFIG_NOSYSTEM がある')
        system_config = self.var('GIT_CONFIG_SYSTEM')
        self.system_selected = b''
        if system_config != b'/dev/null' and (system_config is None or system_config):
            rc, selected = self.capture(*TAKE_GIT_PRE, '-C', self.top, 'var', 'GIT_CONFIG_SYSTEM')
            if rc or not selected:
                raise TakeOutside('system-var')
            self.system_selected = selected
            if selected != b'/dev/null':
                config_path(selected)
            if system_config:
                config_path(system_config)
        self.xcode = b''
        rc, version = self.capture(*TAKE_GIT_PRE, '-C', self.top, 'version')
        if rc:
            raise TakeOutside('git version')
        if b'\n' not in version and TAKE_APPLE_GIT.fullmatch(version):
            rc, man = self.capture(*TAKE_GIT_PRE, '-C', self.top, '--man-path')
            if rc or b'\n' in man or not TAKE_MAN_PATH.fullmatch(man):
                raise TakeOutside('apple-layout')
            self.xcode = man[:-len(b'/share/man')] + b'/share/git-core/gitconfig'
            config_path(self.xcode)
        git_config = self.var('GIT_CONFIG')
        if git_config and git_config != b'/dev/null':
            config_path(git_config)
        roots = [path for path, kind in queue if kind == 'config']
        rows += [b'plan-root\t' + path_field(path) for path in roots]
        drained = self.drain(queue, 0, rows, work, enqueue)
        # collect_context_plan(文脈は 1 つ。下のリポジトリと .gitmodules は写さない)
        formats = []
        for key, allowed in (('extensions.objectformat', (b'sha1', b'sha256')), ('extensions.refstorage',
                                                                                 (b'files', b'reftable'))):
            value = b''
            if os.path.exists(common + b'/config'):
                rc, out = self.isolated(work, 'config', '--file', common + b'/config', '--no-includes', '--null',
                                        '--get', key)
                if rc == 0:
                    value = out.split(b'\0', 1)[0]
                elif rc != 1 or out:
                    raise TakeOutside(f'{key} を読めない')
            formats.append(value or allowed[0])
            if formats[-1] not in allowed:
                raise TakeOutside(f'{key} が既知でない')
        rc, head = self.git(*TAKE_GIT_PRE, 'rev-parse', '--verify', '--quiet', 'HEAD')
        head = head.rstrip(b'\n') if rc == 0 else b'-'
        if rc not in (0, 1):
            raise TakeOutside('rev-parse HEAD')
        listing = self.must(*TAKE_GIT_PRE, 'ls-files', '--stage', '-z').split(b'\0')
        if head != b'-':
            listing += self.must(*TAKE_GIT_PRE, 'ls-tree', '-r', '-z', head).split(b'\0')
        for row in filter(None, listing):
            meta, _, name = row.partition(b'\t')
            if meta.startswith(b'160000 ') or name == b'.gitmodules':
                raise TakeOutside('下のリポジトリか .gitmodules がある')
        enqueue(self.top + b'/.gitmodules', 'admin')
        self.drain(queue, drained, rows, work, enqueue)
        if os.path.lexists(self.top + b'/.gitmodules'):
            raise TakeOutside('.gitmodules がある')
        Path(os.fsdecode(new_state + b'/snapshot/config-origins.txt')).write_bytes(b''.join(r + b'\n' for r in rows))
        context = [b'0', b'-1', b'0', b'1', path_field(self.top), path_field(gitdir), path_field(common), formats[0],
                   formats[1], head, b'0', b'none', b'-', b'-']
        text = b'context-plan-v1\ncontext\t' + b'\t'.join(context) + b'\n'
        text += b''.join(b'root\t0:0\t' + path_field(path) + b'\n' for path in roots)
        Path(os.fsdecode(new_state + b'/snapshot/config-contexts.txt')).write_bytes(text)

    def drain(self, queue, start, rows, work, enqueue):
        """候補を順に辿る(admin でない通常ファイルは include を足す)。辿った数を返す。"""
        index = start
        while index < len(queue):
            path, kind = queue[index]
            index += 1
            if index > 2000:
                raise TakeOutside('設定の候補が多すぎる')
            rows.append(b'candidate\t' + path_field(path))
            if self.walk(path, rows) == 'file' and kind != 'admin':
                self.includes(path, work, enqueue)
        return index

    def includes(self, path, work, enqueue):
        rc, keys = self.isolated(work, 'config', '--file', path, '--no-includes', '--null', '--name-only',
                                 '--get-regexp', r'^(include\.path|includeif\..*\.path)$')
        if rc:
            if rc != 1 or keys:
                raise TakeOutside('include の鍵を読めない')
            return
        parent = os.path.dirname(path) or b'/'
        done = set()
        for key in filter(None, keys.split(b'\0')):
            if key in done:
                continue
            done.add(key)
            rc, values = self.isolated(work, 'config', '--file', path, '--no-includes', '--null', '--type=path',
                                       '--get-all', key)
            if rc:
                raise TakeOutside('include の値を読めない')
            for value in filter(None, values.split(b'\0')):
                enqueue(value if value.startswith(b'/') else parent + b'/' + value)

    # -- ①②④⑤(collect_git) --
    def git_path_abs(self, pre, name):
        for fmt in (('--path-format=absolute',), ()):
            rc, out = self.capture(*pre, 'rev-parse', *fmt, '--git-path', name)
            if rc == 0 and out and not out.startswith(b'-'):
                return out if out.startswith(b'/') else self.top + b'/' + out
        raise TakeOutside(f'--git-path {name}')

    def hooks_lines(self, hooks):
        real = digest = '(無し)'.encode()
        listing = b''
        if os.path.lexists(hooks):
            real = os.path.realpath(hooks) if os.path.exists(hooks) else '(無し)'.encode()
            if os.path.isdir(hooks) and os.path.exists(hooks):
                names = []
                for current, dirs, files in os.walk(real):
                    names += [os.path.join(current, name) for name in dirs + files]
                for path in sorted(names):
                    info = os.lstat(path)
                    value = b'-'
                    if stat.S_ISLNK(info.st_mode):
                        kind, value = b'l', sha_hex(os.readlink(path))
                    elif stat.S_ISDIR(info.st_mode):
                        kind = b'd'
                    elif stat.S_ISREG(info.st_mode):
                        if os.access(path, os.R_OK):
                            kind, value = b'f', sha_hex(Path(os.fsdecode(path)).read_bytes())
                        else:
                            kind = b'u'
                    else:
                        kind = b'o'
                    listing += (b'hook\t' + kind + b'\t' + mode_text(path) + b'\t' + value + b'\t'
                                + path_field(path[len(real) + 1:]) + b'\n')
                digest = sha_hex(listing)
            else:
                digest = '(ディレクトリでない)'.encode()
        return (b'hooks-path\t' + path_field(hooks) + b'\nhooks-real\t' + path_field(real) + b'\nhooks-digest\t'
                + digest + b'\n' + listing)

    @staticmethod
    def config_digest(path):
        if not os.path.lexists(path):
            return '(無し)'.encode()
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return sha_hex(Path(os.fsdecode(path)).read_bytes())
        return '(読めない)'.encode()

    def collect_git(self, new_state, work):
        snapshot = new_state + b'/snapshot/'
        rc, head = self.git(*TAKE_GIT_PRE, 'rev-parse', '--verify', '--quiet', 'HEAD')
        if rc not in (0, 1) or (rc == 0 and not head.strip(b'\n')):
            raise TakeOutside('rev-parse HEAD')
        head = head.rstrip(b'\n') if rc == 0 else '(HEAD 無し)'.encode()
        stage = self.must(*TAKE_GIT_PRE, 'ls-files', '-s', '-z')
        index_env = {**self.git_env, 'GIT_INDEX_FILE': os.fsdecode(work + b'/index.tmp')}
        self.must(*TAKE_GIT_PRE, 'update-index', '-z', '--index-info', env=index_env, stdin=stage)
        rc, tree = self.git(*TAKE_GIT_PRE, 'write-tree', env=index_env)
        tree = tree.rstrip(b'\n')
        if rc or not tree:
            raise TakeOutside('unmerged')
        rc, branch = self.git(*TAKE_GIT_PRE, 'symbolic-ref', '-q', 'HEAD')
        if rc not in (0, 1):
            raise TakeOutside('symbolic-ref HEAD')
        branch = branch.rstrip(b'\n') if rc == 0 else b'(detached)'
        refs = self.must(*TAKE_GIT_PRE, 'for-each-ref', '--format=%(objectname) %(refname)')
        hooks = self.git_path_abs(TAKE_GIT_PRE_HOOKS, 'hooks')
        config = self.git_path_abs(TAKE_GIT_PRE, 'config')
        config_worktree = self.git_path_abs(TAKE_GIT_PRE, 'config.worktree')
        meta = (b'toplevel\t' + path_field(self.top) + b'\ncwd\t' + path_field(self.cwd_phys) + b'\nexclude\t'
                + path_field(self.excl) + b'\nhead\t' + head + b'\nindex-tree\t' + tree + b'\nbranch\t' + branch
                + b'\n')
        meta += self.hooks_lines(hooks)
        meta += (b'config-path\t' + path_field(config) + b'\nconfig-digest\t' + self.config_digest(config)
                 + b'\nconfig-worktree-path\t' + path_field(config_worktree) + b'\nconfig-worktree-digest\t'
                 + self.config_digest(config_worktree) + b'\n')
        meta += b''.join(b'ref\t' + line + b'\n' for line in refs.split(b'\n')[:-1])
        Path(os.fsdecode(snapshot + b'4-meta.txt')).write_bytes(meta)
        base = b'HEAD'
        if head == '(HEAD 無し)'.encode():
            base = self.must(*TAKE_GIT_PRE, 'hash-object', '-t', 'tree', '/dev/null').rstrip(b'\n')
        exclude = b':(exclude,literal)' + self.excl
        Path(os.fsdecode(snapshot + b'1-diff.bin')).write_bytes(self.must(
            *TAKE_GIT_PRE, *TAKE_DIFF_CFG, 'diff', '--binary', '--no-ext-diff', '--no-textconv', '--submodule=short',
            '--ignore-submodules=dirty', base, '--', '.', exclude))
        Path(os.fsdecode(snapshot + b'2-status.z')).write_bytes(self.must(
            *TAKE_GIT_PRE, '-c', 'core.ignoreStat=false', 'status', '--porcelain=v1', '-z', '-uall',
            '--ignore-submodules=dirty', '--', '.', exclude))
        rc, _ = self.git(*TAKE_GIT_PRE, 'rev-parse', '--verify', '--quiet', 'refs/stash')
        if rc == 0:
            stash = self.must(*TAKE_GIT_PRE, 'reflog', 'show', '--format=%H %gd %gs', 'refs/stash')
        elif rc == 1:
            stash = 'stash 無し\n'.encode()
        else:
            raise TakeOutside('rev-parse refs/stash')
        Path(os.fsdecode(snapshot + b'5-stash.txt')).write_bytes(stash)
        Path(os.fsdecode(snapshot + b'6-contexts.txt')).write_bytes(b'')

    # -- ③ の退避とマニフェスト --
    def backup(self, new_state, work, mark):
        raw = self.must(*TAKE_GIT_PRE, 'ls-files', '-o', '--exclude-standard', '-z')
        chosen = sorted(f for f in raw.split(b'\0') if f and (f in (self.real_rel, self.sel_rel) or not (
            f == self.excl or f.startswith(self.excl + b'/'))))
        entries = total = unreadable = 0
        for name in chosen:
            entries += 1
            path = self.top + b'/' + name
            if not name.endswith(b'/') and not os.path.islink(path) and os.path.isfile(path):
                total += os.lstat(path).st_size
            if entries > 2000 or total > 209715200:
                raise TakeOutside('over-limit')
        body = new_state + b'/taskmd-body'
        shutil.copy2(self.real, body)
        manifest = (b'T\t' + mode_text(body) + b'\t' + sha_hex(Path(os.fsdecode(body)).read_bytes()) + b'\t'
                    + path_field(self.real) + b'\n')
        for name in chosen:
            kind, value, mode = self.row(new_state, name)
            manifest += kind + b'\t' + mode + b'\t' + value + b'\t' + path_field(name) + b'\n'
            if kind == b'u':
                unreadable += 1
                self.stderr += b'NOTE: unreadable ' + path_field(name) + b'\n'
        Path(os.fsdecode(new_state + b'/manifest.tsv')).write_bytes(manifest)
        shutil.rmtree(work)
        state = os.fsdecode(new_state)
        out = (b'STATE_DIR=' + path_field(new_state) + b'\nMANIFEST_SHA256=' + sha_hex(manifest)
               + b'\nSNAPSHOT_SHA256=' + snapshot_digest(state).encode() + b'\nENTRIES=%d\nBYTES=%d\nUNREADABLE=%d\n'
               % (entries, total, unreadable) + b'TASKMD_REAL=' + path_field(self.real) + b'\nOWNER_MARK='
               + (b'yes' if mark else b'no') + b'\n')
        return out, bytes(self.stderr)

    def row(self, new_state, name):
        """build_row(take) → (種別, 値, mode)。"""
        path = self.top + b'/' + name
        try:
            mode = mode_text(path)
        except OSError:
            mode = b'-'
        if name.endswith(b'/'):
            return b'o', b'-', mode
        dest = new_state + b'/files/' + name
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except OSError:
            return b'u', b'-', mode
        if os.path.islink(path):
            link = os.readlink(path)
            os.symlink(link, dest)
            return b'l', path_field(link), mode
        if not os.path.exists(path):
            return b'u', b'-', mode
        if not os.path.isfile(path):
            return b'o', b'-', mode
        if not os.access(path, os.R_OK):
            return b'u', b'-', mode
        shutil.copy2(path, dest)
        return b'f', sha_hex(Path(os.fsdecode(dest)).read_bytes()), mode


def write_take_record(cwd, select, env):
    """旧版の take と同じ形の記録を書き、(take の stdout, stderr)を返す。"""
    return PyTake(cwd, select, env).run()


def read_record(state):
    """記録と所属の印の中身(相対の名前 → (種類, mode, リンクの数, 中身))。印の guard の番号は、記録の
    フォルダの番号と同じことを確かめてから字面を外す(作り直した記録は番号が変わる)。"""
    state = os.fsencode(state)
    found = {}
    for current, dirs, files in os.walk(state):
        for name in dirs + files:
            path = os.path.join(current, name)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                data = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                data = Path(os.fsdecode(path)).read_bytes()
            else:
                data = b''
            found[os.path.relpath(path, state)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                                   info.st_nlink if not stat.S_ISDIR(info.st_mode) else 0, data)
    found[b'.'] = (stat.S_IFDIR, stat.S_IMODE(os.lstat(state).st_mode), 0, b'')
    mark = os.path.dirname(state) + b'/owner-' + os.path.basename(state)[len(b'guard-'):]
    if os.path.lexists(mark):
        info = os.lstat(mark)
        guard = os.lstat(state)
        lines = Path(os.fsdecode(mark)).read_bytes().split(b'\n')
        want = {b'guard_dev': b'%d' % guard.st_dev, b'guard_ino': b'%d' % guard.st_ino}
        kept = []
        for line in lines:
            key, _, value = line.partition(b'\t')
            if key in want:
                kept.append(key + (b'\t<guard>' if value == want[key] else b'\t<other>'))
            else:
                kept.append(line)
        found[b'<owner>'] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_nlink, b'\n'.join(kept))
    return found


def take_record(cwd, select, env, failure):
    """take の記録を作り、take の stdout の欄を返す。旧版を流せない(bash 4.0 以上が無い)ときは、同じ形の記録を
    Python で書く。RECORD_CHECK(list)があるときは、旧版の take の後に、同じ配置で記録を Python で書き直し、
    中身・stdout・stderr の違いを RECORD_CHECK に足す(配置には Python の記録が残る)。
    旧版の take を流す bash は main が決めた BASH。main を通らない呼び出し(測定の道具)では、ここで探す。"""
    bash = BASH if BASH is not None else find_bash4()
    if bash is None:
        out, _ = write_take_record(cwd, select, env)
    else:
        done = subprocess.run([bash, str(OLD), 'take', '--cwd', str(cwd), '--task-md', select], env=env,
                              capture_output=True, cwd=cwd)
        if done.returncode:
            raise AssertionError(failure + done.stderr.decode(errors='replace'))
        out = done.stdout
        if RECORD_CHECK is not None:
            state = unquote(dict(line.split(b'=', 1) for line in out.splitlines())[b'STATE_DIR'])
            old = read_record(state)
            mark = os.path.dirname(state) + b'/owner-' + os.path.basename(state)[len(b'guard-'):]
            make_writable(os.fsdecode(state))
            shutil.rmtree(state)
            if os.path.lexists(mark):
                os.unlink(mark)
            out, err = write_take_record(cwd, select, env)
            new = read_record(state)
            if out != done.stdout:
                RECORD_CHECK.append(f'take の stdout が違う: 旧版 {done.stdout!r} / Python {out!r}')
            if err != done.stderr:
                RECORD_CHECK.append(f'take の stderr が違う: 旧版 {done.stderr!r} / Python {err!r}')
            for name in sorted(set(old) | set(new)):
                if old.get(name) != new.get(name):
                    RECORD_CHECK.append(f'記録の {os.fsdecode(name)} が違う: 旧版 {clip(repr(old.get(name)))} / '
                                        f'Python {clip(repr(new.get(name)))}')
    return dict(line.split(b'=', 1) for line in out.splitlines())


def argv_for(sub, values, *, names=('--cwd', '--state', '--manifest-sha256', '--snapshot-sha256'), extra=()):
    keys = {'--cwd': 'cwd', '--state': 'state', '--manifest-sha256': 'man', '--snapshot-sha256': 'snap'}
    argv = [sub]
    for name in names:
        argv += [name, values[keys[name]]]
    return argv + list(extra)


# ---- 1 つのケース ----

@dataclasses.dataclass
class Case:
    group: str
    label: str
    argv: object                    # World → list[str|bytes]
    build: object = None            # Path → World(各側の前に作り直す。None は配置なし)
    env: object = ()                # World → list[bytes](基本の変数に足す)
    mutates: bool = False           # 記録かタスク本文を変える(各側の前に作り直す)
    stdout: str = 'pipe'
    stderr: str = 'pipe'
    cwd: object = None              # World → Path
    observe: object = None          # World → dict(名前 → パス)
    tweak: object = None            # World → None(各側の前に配置へ当てる変更)
    path: object = None             # World → PATH の値(None は既定)
    compare: bool = True
    cp_stub: bool = False
    git_stub: bool = False
    allow_env: tuple = ()
    preexec: object = None
    during: object = None
    timeout: float = 60
    base_env: bool = True
    after: object = None            # (World, Side) → dict(比べる追加の値。cleanup の後の一覧など)
    drop_env: tuple = ()            # 基本の変数から外す名前
    cp_mode: str = 'gnu'            # 偽の cp の動作(check_r4r の観測器の mode)
    boundary_ok: bool = False       # config-check・compare: 記録の読み取りを通ったら境界(99)を許す
    mac_skip: str = ''              # 範囲 mac で比べない理由(空なら比べる。テスト計画の「Mac」)


@dataclasses.dataclass
class Side:
    result: Result
    files: dict
    git_calls: int
    stub: list
    extra: dict
    env_input: list = dataclasses.field(default_factory=list)   # 起動に渡した環境(並びのまま)


def default_env(world, path=None):
    entries = [b'PATH=' + os.fsencode(path if path is not None else os.environ.get('PATH', '/usr/bin:/bin')),
               b'HOME=' + os.fsencode(world.home if world else Path.home())]
    if world is not None:
        entries.append(b'XDG_STATE_HOME=' + os.fsencode(world.xdg))
    return entries


def encode_all(items):
    return [os.fsencode(item) if isinstance(item, str) else item for item in items]


def observe_files(world, case):
    if world is None:
        return {}
    paths = {'state': world.state, 'task': world.real or str(world.repo / 'task/t.md'),
             'xdg': str(world.xdg / 'dev-workflow')}
    if case.observe is not None:
        paths.update(case.observe(world))
    record = {}
    for name, path in paths.items():
        if not path:
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            record[name] = tree(path)
        else:
            record[name] = file_record(path)
    return record


def run_side(case, base, world, legacy, stub=None):
    if case.tweak is not None and world is not None:
        case.tweak(world)
    path = case.path(world) if case.path is not None else None
    if path is None:
        path = os.environ.get('PATH', '/usr/bin:/bin')
    if case.git_stub:
        path = f'{TOOLS.git_dir}:{path}'
    if case.cp_stub and stub is not None:
        path = f'{stub[0]}:{path}'
    extra = encode_all(case.env(world) if callable(case.env) else case.env)
    check_input_env(extra, case.allow_env)
    base_entries = default_env(world, path) if case.base_env else []
    base_entries = [e for e in base_entries if e.split(b'=', 1)[0].decode() not in case.drop_env]
    env = base_entries + extra
    program, argv0, prefix = old_command() if legacy else rust_command()
    args = encode_all(prefix + list(case.argv(world)))
    cwd = case.cwd(world) if case.cwd is not None else (world.repo if world is not None else base)
    TOOLS.reset_git()
    if stub is not None:
        stub[1].drain()
    result = launch(program, argv0, args, env, cwd=cwd, stdout=case.stdout, stderr=case.stderr, base=base,
                    timeout=case.timeout, preexec=case.preexec, during=case.during,
                    label='old' if legacy else 'new')
    result = normalize(result, legacy)
    records = r4r_module().read_stub(stub[1]) if stub is not None else []
    side = Side(result, observe_files(world, case), TOOLS.git_calls(), records, {}, env)
    if case.after is not None:
        side.extra = case.after(world, side)
    return side


def fix_mark(world):
    """写した配置の所属の印の番号(device・inode)を、写した後の実体に合わせる。"""
    if world is None or not world.state:
        return
    mark = Path(os.path.dirname(world.state)) / ('owner-' + os.path.basename(world.state)[len('guard-'):])
    if not mark.is_file() or mark.is_symlink():
        return
    lines = mark.read_bytes().split(b'\n')
    paths = {}
    for line in lines:
        key, _, value = line.partition(b'\t')
        if key in (b'top', b'common', b'taskdir', b'tmpdir') and value:
            paths[key] = bytes.fromhex(value.decode())
    paths[b'guard'] = os.fsencode(world.state)
    out = []
    for line in lines:
        key, _, value = line.partition(b'\t')
        for name, target in paths.items():
            if key in (name + b'_dev', name + b'_ino') and os.path.exists(target):
                info = os.lstat(target)
                value = str(info.st_dev if key.endswith(b'_dev') else info.st_ino).encode()
                line = key + b'\t' + value
        out.append(line)
    mark.write_bytes(b'\n'.join(out))


def copy_readable(source, target):
    """読めないファイルも写す(一時に読めるようにして、写した後に mode を戻す)。"""
    mode = stat.S_IMODE(os.lstat(source).st_mode)
    if not os.access(source, os.R_OK):
        os.chmod(source, mode | 0o400)
        try:
            shutil.copy2(source, target)
        finally:
            os.chmod(source, mode)
        os.chmod(target, mode)
        return target
    return shutil.copy2(source, target)


def copy_world(source, target):
    if target.exists():
        make_writable(target)
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True, copy_function=copy_readable)


_R4R = None


def r4r_module():
    global _R4R
    if _R4R is None:
        _R4R = load(Path(__file__).with_name('check_r4r.py'), 'check_r4r_for_s3ia')
    return _R4R


def place_text(data, base):
    return data.replace(os.fsencode(base), b'<base>')


def compare_sides(case, old, new, base):
    if old.result.timed_out or new.result.timed_out:
        raise Mismatch('時間切れ(成功に数えない)')
    if new.result.code == BOUNDARY or old.result.code == BOUNDARY:
        raise Mismatch('比べるケースで境界の印(終了値 99)を受けた')
    pairs = [
        ('終了値', old.result.code, new.result.code),
        ('stdout', old.result.stdout, new.result.stdout),
        ('stderr', old.result.stderr, new.result.stderr),
        ('ファイル', old.files, new.files),
        ('偽の git の起動', old.git_calls, new.git_calls),
        ('追加の観測', old.extra, new.extra),
    ]
    for name, a, b in pairs:
        if a != b:
            raise Mismatch(f'{name}が違う: {difference(a, b)}')
    return compare_stubs(old.stub, new.stub, base, inherited_values(new.env_input))


def difference(a, b, prefix=''):
    """違う所だけを短く示す。"""
    if isinstance(a, dict) and isinstance(b, dict):
        parts = []
        for key in sorted(set(a) | set(b), key=str):
            if a.get(key) != b.get(key):
                parts.append(difference(a.get(key), b.get(key), f'{prefix}{key}/'))
        return '; '.join(parts)
    return f'{prefix} 旧 {clip(a, 300)} / Rust {clip(b, 300)}'


STATE_REASONS = (b'REASON=state-missing', b'REASON=manifest-digest', b'REASON=snapshot-digest', b'REASON=digest')
BEFORE_BOUNDARY = ('保護領域の記録に必要な項目が無い', '--cwd が take のときと違う', '保護領域に一時ディレクトリを作れない',
                   '予期しない失敗')


def check_boundary(old, new):
    """Rust が境界(99)で止まったとき、旧版が記録の読み取りを通ったこと(境界より前の失敗が無い)と、
    境界の行の前の stderr が旧版の stderr の先頭と同じことを確かめる。"""
    if any(reason in old.result.stdout for reason in STATE_REASONS):
        raise Mismatch('旧版は保護領域の確かめで止まったのに、Rust が境界の印を返した')
    text = old.result.stderr.decode(errors='replace')
    if any(marker in text for marker in BEFORE_BOUNDARY):
        raise Mismatch('旧版は記録の読み取りで止まったのに、Rust が境界の印を返した')
    lines = new.result.stderr.split(b'\n')
    if len(lines) < 2 or not lines[-2].startswith(b'ERROR [s3i-a-boundary] '):
        raise Mismatch(f'境界の印の行が最後に無い: {clip(new.result.stderr)}')
    before = b'\n'.join(lines[:-2]) + (b'\n' if len(lines) > 2 else b'')
    if not old.result.stderr.startswith(before):
        raise Mismatch(f'境界の前の stderr が旧版と違う: 旧 {clip(old.result.stderr)} / Rust {clip(before)}')
    if new.result.stdout:
        raise Mismatch('境界で止まった Rust が stdout に書いた')


def clip(value, limit=600):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + '…'


def inherited_values(entries):
    """呼出元の環境から bash が取り込む、追加の決定 4 の名前の値(同じ名前は後ろが勝つ)。"""
    found = {}
    for entry in entries:
        name, sep, value = entry.partition(b'=')
        if sep and name.decode(errors='replace') in NOT_REPRODUCED_NAMES:
            found[name.decode()] = value
    return found


def split_not_reproduced(entries):
    """子の環境の集合から追加の決定 4 の名前を外し、外した名前の値を返す。"""
    kept, taken = set(), {}
    for entry in entries:
        name, _, value = entry.partition(b'=')
        if name.decode(errors='replace') in NOT_REPRODUCED_NAMES:
            if name.decode() in taken:
                raise Mismatch(f'子の環境に {name.decode()} が 2 つある')
            taken[name.decode()] = value
        else:
            kept.add(entry)
    return kept, taken


def compare_stubs(old, new, base, inherited=None):
    """cp の観測器の記録を比べる。追加の決定 4 の名前は比べずに、旧版と Rust の有無と値を返す。"""
    inherited = inherited or {}
    if len(old) != len(new):
        raise Mismatch(f'cp の起動の回数が違う: 旧 {len(old)} / Rust {len(new)}')
    records = []
    for a, b in zip(old, new):
        a, b = dict(a), dict(b)
        env_a, taken_a = split_not_reproduced(env_set(a.pop('env'), base))
        env_b, taken_b = split_not_reproduced(env_set(b.pop('env'), base))
        if env_a != env_b:
            raise Mismatch(f'cp の子の環境が違う: 旧だけ {sorted(env_a - env_b)} / Rust だけ {sorted(env_b - env_a)}')
        if a != b:
            raise Mismatch(f'cp の起動が違う: 旧 {clip(a)} / Rust {clip(b)}')
        for name in NOT_REPRODUCED_NAMES:
            want = inherited.get(name)
            if want is not None and taken_b.get(name) != place_text(want, base):
                raise Mismatch(f'決定 4 の名前 {name} を、Rust が受け継いだ値と違う値で渡した: '
                               f'受け継いだ {want!r} / Rust {taken_b.get(name)!r}')
            if want is None and name in taken_b:
                raise Mismatch(f'決定 4 の名前 {name} を受け継いでいないのに、Rust が渡した: {taken_b[name]!r}')
        records.append({name: (taken_a.get(name), taken_b.get(name)) for name in NOT_REPRODUCED_NAMES})
    return records


def describe_not_reproduced(records):
    """追加の決定 4 の名前の記録を 1 行にする(cp の起動ごと。同じ記録が続けば 1 つにまとめる)。旧版の値の
    旧版のフォルダは `<旧版のフォルダ>` と書く。"""
    def shown(value):
        if value is None:
            return '無し'
        return repr(os.fsdecode(value).replace(str(SCRIPTS), '<旧版のフォルダ>'))
    parts = []
    for index, record in enumerate(records):
        present = [(n, o, r) for n, (o, r) in record.items() if o is not None or r is not None]
        if not present:
            text = f'{len(record)} 個とも旧版にも Rust にも無い'
        else:
            absent = len(record) - len(present)
            text = '・'.join(f'{n} 旧 {shown(o)} / Rust {shown(r)}' for n, o, r in present)
            text += f'(ほかの {absent} 個はどちらにも無い)' if absent else ''
        if parts and parts[-1][1] == text:
            parts[-1][0].append(index + 1)
        else:
            parts.append(([index + 1], text))
    return '; '.join(f'cp {",".join(map(str, numbers))} 回目: {text}' for numbers, text in parts)


TIME_NAMES = (b'EPOCHSECONDS=', b'EPOCHREALTIME=')


def env_set(hexed, base):
    """子の環境の集合。実行の時刻の値は形と時刻の範囲を確かめてから伏せる(06:58 の決定)。"""
    entries = bytes.fromhex(hexed).split(b'\0') if hexed else []
    out = set()
    now = time.time()
    for entry in entries:
        if entry.startswith(TIME_NAMES):
            name, _, value = entry.partition(b'=')
            pattern = rb'[0-9]+' if name == b'EPOCHSECONDS' else rb'[0-9]+\.[0-9]{6}'
            if not re.fullmatch(pattern, value) or not now - 600 <= float(value) <= now + 1:
                raise Mismatch(f'{name.decode()} の形か時刻が違う: {value!r}')
            entry = name + b'=<time>'
        out.add(place_text(entry, base))
    return out


@dataclasses.dataclass
class Outcome:
    compared: int = 0
    recorded: int = 0
    boundary: int = 0
    failures: list = dataclasses.field(default_factory=list)
    seconds: dict = dataclasses.field(default_factory=dict)
    not_reproduced: list = dataclasses.field(default_factory=list)   # (ケース, 決定 4 の名前の記録)


def run_case(case, root, outcome):
    base = Path(root) / f'case-{outcome.compared + outcome.recorded + outcome.boundary + len(outcome.failures)}'
    base.mkdir(parents=True, exist_ok=True)
    stub = None
    try:
        if case.cp_stub:
            directory = base / 'cpstub'
            stub_path, channel = r4r_module().make_stub(directory, mode=case.cp_mode)
            (directory / 'cp').symlink_to(stub_path)
            stub = (directory, channel)
        world = case.build(base / 'w') if case.build is not None else None
        template = base / 'template'
        if world is not None:
            copy_world(world.base, template)
        old = run_side(case, base, world, True, stub)
        if world is not None:
            copy_world(template, world.base)
            fix_mark(world)
        new = run_side(case, base, world, False, stub)
        if not case.compare:
            if new.result.code != BOUNDARY:
                raise Mismatch(f'境界で止まるはずの Rust が終了値 {new.result.code} を返した')
            outcome.boundary += 1
            return
        if case.boundary_ok and new.result.code == BOUNDARY:
            check_boundary(old, new)
            outcome.boundary += 1
            return
        records = compare_sides(case, old, new, base)
        if records:
            outcome.not_reproduced.append((f'{case.group}/{case.label}', records))
        outcome.compared += 1
    except Exception as error:  # noqa: BLE001 - 1 件の失敗で残りのケースを止めない。
        outcome.failures.append(f'{case.group}/{case.label}: {type(error).__name__}: {error}')
    finally:
        if stub is not None:
            stub[1].close()
        if base.exists():
            make_writable(base)
            shutil.rmtree(base, ignore_errors=True)


# ---- ケースの群 ----

SUBS = ('take', 'config-check', 'compare', 'taskmd-diff', 'restore-taskmd', 'cleanup')
NAMES = ('--cwd', '--task-md', '--state', '--manifest-sha256', '--snapshot-sha256', '--run-rc')
NEEDS = {
    'take': ('--cwd', '--task-md'),
    'config-check': ('--cwd', '--state', '--manifest-sha256', '--snapshot-sha256'),
    'compare': ('--cwd', '--state', '--manifest-sha256', '--snapshot-sha256', '--run-rc'),
    'taskmd-diff': ('--cwd', '--state', '--manifest-sha256', '--snapshot-sha256'),
    'restore-taskmd': ('--cwd', '--state', '--manifest-sha256', '--snapshot-sha256'),
    'cleanup': ('--state',),
}
BAD_STATE = ['--state', '/nonexistent/guard-a', '--manifest-sha256', 'a', '--snapshot-sha256', 'b']


def fixed(argv):
    return lambda world: list(argv)


def full_args(sub, value='v'):
    argv = [sub]
    for name in NEEDS[sub]:
        argv += [name, '0' if name == '--run-rc' else value]
    return argv


def group_arguments():
    cases = []
    add = lambda label, argv, compare=True: cases.append(Case('引数と使い方', label, fixed(argv), compare=compare))
    add('引数なし', [])
    add('不明なサブコマンド', ['bogus'])
    add('-h', ['-h'])
    add('--help', ['--help'])
    add('UTF-8 でない値', [b'\xff\xfe'])
    for sub in SUBS:
        add(f'{sub} -h', [sub, '-h'])
        add(f'{sub} 後ろの --help', [sub, '--cwd', 'x', '--help'])
        add(f'{sub} 不明な引数', [sub, '--bogus'])
        add(f'{sub} 必要な名前が無い', [sub])
        for name in NAMES:
            add(f'{sub} {name} の値が無い', [sub, name])
        other = next(n for n in NAMES if n not in NEEDS[sub])
        add(f'{sub} 使えない名前 {other}', [sub, other, 'x'] + full_args(sub)[1:])
        for name in NEEDS[sub]:
            argv = full_args(sub)
            index = argv.index(name)
            if name != '--run-rc':
                argv[index + 1] = ''
                add(f'{sub} {name} が空', argv)
        add(f'{sub} -- で始まる値', [sub, '--cwd', '--state'] + full_args(sub)[1:], compare=sub != 'take')
        add(f'{sub} 同じ名前の重複', [sub, '--state', 'x'] + full_args(sub)[1:])
        add(f'{sub} UTF-8 でない値', [sub, '--state', b'/x\xff/guard-a'] + full_args(sub)[1:])
    for value in ('', '-1', '1.5', ' 1', 'abc', '1\n2', '007', '12a'):
        argv = full_args('compare')
        argv[argv.index('--run-rc') + 1] = value
        argv[argv.index('--state') + 1] = '/nonexistent/guard-a'
        add(f'compare --run-rc {value!r}', argv)
    add('take 正しい引数(境界)', ['take', '--cwd', '.', '--task-md', 't.md'], compare=False)
    return cases


def standard(base):
    return make_world(base)


def with_values(sub, extra=(), **override):
    return lambda world: argv_for(sub, world.values(**override), extra=extra)


def state_args(sub):
    if sub == 'cleanup':
        return lambda world: ['cleanup', '--state', world.state]
    if sub == 'compare':
        return with_values('compare', extra=('--run-rc', '0'))
    return with_values(sub)


def tweak_state(action):
    def build(base):
        world = make_world(base)
        action(world)
        return world
    return build


def group_state():
    """保護領域の確かめ。cleanup は check_state_dir の不合格だけを比べる(合格なら消すので別の群)。"""
    cases = []
    subs = ('taskmd-diff', 'restore-taskmd', 'config-check', 'compare', 'cleanup')

    def add(label, sub, build=standard, argv=None, env=(), cwd=None, mutates=False, drop=()):
        cases.append(Case('保護領域の確かめ', f'{sub} {label}', argv or state_args(sub), build=build, env=env,
                          cwd=cwd, mutates=mutates, drop_env=drop,
                          boundary_ok=sub in ('config-check', 'compare')))

    def rename(world, name):
        target = os.path.join(os.path.dirname(world.state), name)
        os.rename(world.state, target)
        world.state = target

    for sub in subs:
        add('guard- でない名前', sub, tweak_state(lambda w: rename(w, 'notguard')))
        add('guard- だけの名前', sub, tweak_state(lambda w: rename(w, 'guard-')))

        def link(world):
            target = world.state + '-link'
            os.symlink(world.state, target)
            world.state = target
        add('リンク', sub, tweak_state(link))

        def plain(world):
            target = os.path.join(os.path.dirname(world.state), 'guard-file')
            Path(target).write_bytes(b'x')
            world.state = target
        add('フォルダでない', sub, tweak_state(plain))
        add('無い', sub, tweak_state(lambda w: setattr(w, 'state', w.state + 'x')))

        def outside(world):
            target = world.base / 'other' / 'guard-x'
            target.mkdir(parents=True, mode=0o700)
            world.state = str(target)
        add('候補の外', sub, tweak_state(outside))
        add('置き場そのもの', sub, tweak_state(lambda w: setattr(w, 'state', os.path.dirname(w.state))))
        if sub == 'cleanup':
            continue
        add('末尾の /', sub, argv=lambda w, s=sub: state_args(s)(dataclasses.replace(w, state=w.state + '//')))
        add('相対パス', sub, argv=lambda w, s=sub: state_args(s)(dataclasses.replace(w, state=os.path.basename(w.state))),
            cwd=lambda w: Path(os.path.dirname(w.state)))
        add('XDG_STATE_HOME が無い', sub, drop=('XDG_STATE_HOME',))
        add('XDG_STATE_HOME が相対', sub, env=lambda w: [b'XDG_STATE_HOME=xdg'], cwd=lambda w: w.base)
        add('TMPDIR が候補を含む', sub, env=lambda w: [b'TMPDIR=' + os.fsencode(w.base)])
        add('TMPDIR が相対', sub, env=lambda w: [b'TMPDIR=xdg'], cwd=lambda w: w.base)
        # `cd -P -- -` は OLDPWD へ移る(bash 5.2.21)。OLDPWD が候補を含むフォルダなら、候補は TMPDIR の下になる。
        add('TMPDIR が - で OLDPWD が候補を含む', sub, env=lambda w: [b'TMPDIR=-', b'OLDPWD=' + os.fsencode(w.base)])
        add('TMPDIR が - で OLDPWD が無い', sub, env=lambda w: [b'TMPDIR=-'])
        add('TMPDIR が - で OLDPWD がフォルダでない', sub, env=lambda w: [b'TMPDIR=-', b'OLDPWD=/nonexistent'])
        add('mode 755', sub, tweak_state(lambda w: os.chmod(w.state, 0o755)))
        add('mode 1700', sub, tweak_state(lambda w: os.chmod(w.state, 0o1700)))
        for name in ('manifest.tsv', 'snapshot'):
            add(f'{name} が無い', sub, tweak_state(lambda w, n=name: move_aside(w, n)))
            add(f'{name} がリンク', sub, tweak_state(lambda w, n=name: link_aside(w, n)))
        add('manifest.tsv がフォルダ', sub, tweak_state(lambda w: swap_kind(w, 'manifest.tsv', 'dir')))
        add('snapshot がファイル', sub, tweak_state(lambda w: swap_kind(w, 'snapshot', 'file')))
        add('マニフェストのダイジェストの違い', sub, argv=lambda w, s=sub: state_args(s)(dataclasses.replace(w, man='0' * 64)))
        add('スナップショットのダイジェストの違い', sub, argv=lambda w, s=sub: state_args(s)(dataclasses.replace(w, snap='0' * 64)))
        add('ダイジェストが大文字', sub, argv=lambda w, s=sub: state_args(s)(
            dataclasses.replace(w, man=w.man.upper(), snap=w.snap.upper())))
        for label, action in SNAPSHOT_TWEAKS:
            add(f'snapshot の中の{label}', sub, tweak_state(lambda w, a=action: (a(w), reseal(w))))
    # 候補の解決(selftest の C10): まだ無い中間を `..` で打ち消す XDG_STATE_HOME。
    for sub in ('taskmd-diff', 'cleanup'):
        add('.. を含む候補', sub, build=lambda base: make_world_dotdot(base))
        add('HOME の候補', sub, build=lambda base: make_world_home(base, '.local/state'), drop=('XDG_STATE_HOME',))
        add('HOME の .cache', sub, build=lambda base: make_world_home(base, '.cache'), drop=('XDG_STATE_HOME',))
        add('HOME が /', sub, build=lambda base: make_world_home(base, '.local/state', home_root=True),
            drop=('XDG_STATE_HOME',))
        add('HOME が無い', sub, build=lambda base: make_world_home(base, '.local/state'),
            drop=('XDG_STATE_HOME', 'HOME'))
        add('XDG_STATE_HOME が空', sub, build=lambda base: make_world_home(base, '.local/state'),
            env=lambda w: [b'XDG_STATE_HOME='], drop=('XDG_STATE_HOME',))
    return cases


def move_aside(world, name):
    os.rename(os.path.join(world.state, name), os.path.join(world.state, name + '.aside'))


def link_aside(world, name):
    path = os.path.join(world.state, name)
    os.rename(path, path + '.real')
    os.symlink(path + '.real', path)


def swap_kind(world, name, kind):
    path = os.path.join(world.state, name)
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    if kind == 'dir':
        os.mkdir(path, 0o700)
    else:
        Path(path).write_bytes(b'x')


def snap(world, name):
    return os.path.join(world.state, 'snapshot', name)


SNAPSHOT_TWEAKS = (
    ('リンク', lambda w: os.symlink(snap(w, '4-meta.txt'), snap(w, 'zz-link'))),
    ('切れたリンク', lambda w: os.symlink('/nonexistent', snap(w, 'zz-dangling'))),
    ('フォルダ', lambda w: os.mkdir(snap(w, 'zz-dir'))),
    ('. で始まる名前', lambda w: Path(snap(w, '.hidden')).write_bytes(b'x')),
    ('読めないファイル', lambda w: (Path(snap(w, 'zz-unreadable')).write_bytes(b'x'), os.chmod(snap(w, 'zz-unreadable'), 0))),
    ('名前の * ', lambda w: Path(snap(w, '*')).write_bytes(b'star')),
    ('改行の名前', lambda w: Path(snap(w, 'a\nb')).write_bytes(b'nl')),
    ('空', lambda w: [os.unlink(snap(w, n)) for n in os.listdir(os.path.join(w.state, 'snapshot'))]),
)


def make_world_dotdot(base):
    """XDG_STATE_HOME=<xdg>/new/../ の形(まだ無い中間を打ち消す)。take も同じ値で作る。"""
    world = make_world(base, take=False)
    xdg = world.xdg / 'new' / '..'
    env = {**os.environ, 'XDG_STATE_HOME': str(xdg), 'HOME': str(world.home), 'PATH': f'{TOOLS.fixed_dir}:{os.environ.get("PATH", "")}'}
    env.pop('TMPDIR', None)
    fields = take_record(world.repo, 'task/t.md', env, '旧版の take(.. を含む候補)が失敗した: ')
    world.state = os.fsdecode(fields[b'STATE_DIR'])
    world.man = fields[b'MANIFEST_SHA256'].decode()
    world.snap = fields[b'SNAPSHOT_SHA256'].decode()
    world.real = os.fsdecode(fields[b'TASKMD_REAL'])
    world.xdg = xdg
    return world


def make_world_home(base, where, home_root=False):
    """XDG_STATE_HOME を渡さず、HOME の候補に記録を作る。"""
    world = make_world(base, take=False)
    env = {**os.environ, 'HOME': str(world.home), 'PATH': f'{TOOLS.fixed_dir}:{os.environ.get("PATH", "")}'}
    env.pop('TMPDIR', None)
    env.pop('XDG_STATE_HOME', None)
    if where == '.cache':
        # .local/state を作れないようにして、2 つ目の候補を使わせる。
        (world.home / '.local').write_bytes(b'')
    fields = take_record(world.repo, 'task/t.md', env, '旧版の take(HOME の候補)が失敗した: ')
    world.state = os.fsdecode(fields[b'STATE_DIR'])
    world.man = fields[b'MANIFEST_SHA256'].decode()
    world.snap = fields[b'SNAPSHOT_SHA256'].decode()
    world.real = os.fsdecode(fields[b'TASKMD_REAL'])
    world.xdg = Path('/nonexistent-xdg')
    if home_root:
        world.home = Path('/')
    return world


def edit_record(name, change):
    """snapshot/<name> を change(bytes → bytes)で書き換え、ダイジェストを合わせ直す。"""
    def build(base):
        world = make_world(base)
        path = Path(world.state) / 'snapshot' / name
        path.write_bytes(change(path.read_bytes()))
        reseal(world)
        return world
    return build


def replace_line(key, value):
    def change(data):
        out = []
        for line in data.split(b'\n'):
            if line.split(b'\t', 1)[0] == key:
                line = key + b'\t' + value
            out.append(line)
        return b'\n'.join(out)
    return change


def drop_line(key):
    return lambda data: b'\n'.join(l for l in data.split(b'\n') if l.split(b'\t', 1)[0] != key)


# `"/x\\1%("` の値は、旧版の cunquote の printf が `%(` の誤りの文を出す。その 1 文字は実行ごとに変わるので、
# normalize の TIME_FORMAT が伏せる(22:31 の決定の「実行ごとに変わる出力」の範囲として採った)。
QUOTED_VALUES = (
    b'"/x\\7zz"', b'"/x\\012y"', b'"/x\\000y"', b'"/x\\1%d"', b'"/x\\1%s"', b'"/x\\1%q"', b'"/x\\1%%"',
    b'"/x\\7%z"', b'"/x\\1%"', b'"/x\\1%k"', b'"/x\\q"', b'"/x\\t\\"y\\\\"', b'"/x\\1%("', b'"/x\\0\\u"',
    b'"/x', b'/x"', b'"', b'"/x\\"',
)


def group_meta():
    cases = []
    subs = ('taskmd-diff', 'restore-taskmd', 'config-check', 'compare')

    def add(label, build, sub='taskmd-diff', argv=None, cwd_value=None, compare=None):
        if argv is None:
            argv = state_args(sub) if cwd_value is None else (
                lambda w, s=sub: state_args(s)(dataclasses.replace(w, repo=cwd_value(w))))
        boundary = sub in ('config-check', 'compare')
        cases.append(Case('記録の読み取り', f'{sub} {label}', argv, build=build, boundary_ok=boundary))

    for sub in subs:
        def drop(name):
            def build(base):
                world = make_world(base)
                os.unlink(Path(world.state) / 'snapshot' / name)
                reseal(world)
                return world
            return build
        add('4-meta.txt が無い', drop('4-meta.txt'), sub, compare=True)
        add('taskmd.txt が無い', drop('taskmd.txt'), sub, compare=True)
        add('必要な項目の欠落', edit_record('4-meta.txt', drop_line(b'exclude')), sub, compare=True)
        add('空の値', edit_record('taskmd.txt', replace_line(b'kind', b'')), sub, compare=True)
        add('--cwd の違い', standard, sub, cwd_value=lambda w: w.base, compare=True)
        add('--cwd が無い', standard, sub, cwd_value=lambda w: w.base / 'nonexistent')
    for index, value in enumerate(QUOTED_VALUES):
        add(f'引用した cwd {index}', edit_record('4-meta.txt', replace_line(b'cwd', value)),
            cwd_value=lambda w: w.base)
        add(f'引用した real {index}', edit_record('taskmd.txt', replace_line(b'real', value)))
    add('引用を戻さないキーの引用', edit_record('4-meta.txt', replace_line(b'head', b'"\\101"')))
    add('同じキー', edit_record('4-meta.txt', lambda d: d + b'cwd\t/somewhere\n'), cwd_value=lambda w: w.base)
    add('前後のタブ', edit_record('4-meta.txt', lambda d: d.replace(b'\ncwd\t', b'\n\tcwd\t\t')))
    add('後ろのタブ', edit_record('taskmd.txt', lambda d: d.replace(b'\nkind\tfile', b'\nkind\tfile\t\t')))
    add('改行で終わらない最後の行', edit_record('taskmd.txt', lambda d: d.rstrip(b'\n')))
    add('NUL を含む行', edit_record('taskmd.txt', lambda d: d.replace(b'kind\tfile', b'kind\tfi\0le')))

    def via_link(world):
        link = world.base / 'repo-link'
        os.symlink(world.repo, link)
        return link
    add('リンクを通した同じ --cwd', standard, cwd_value=via_link)
    return cases


def set_task(text):
    def tweak(world):
        Path(world.real).write_bytes(text)
    return tweak


def task_case(label, tweak, build=standard, **kw):
    return Case('taskmd-diff', label, state_args('taskmd-diff'), build=build, tweak=tweak, **kw)


def group_taskmd_diff():
    cases = [task_case('same', None)]
    checks = {
        '-': b'# t\n- [x] one\n- [ ] two\nbody\n',
        'X': b'# t\n- [X] one\n- [x] two\nbody\n',
    }
    for label, text in checks.items():
        cases.append(task_case(f'checks-only {label}', set_task(text)))
    for label, before, after in (
        ('*', b'* [ ] a\n', b'* [x] a\n'),
        ('+', b'+ [ ] a\n', b'+ [X] a\n'),
        ('字下げ', b'   - [ ] a\n', b'   - [x] a\n'),
        ('タブ', b'\t-\t[ ] a\n', b'\t-\t[x] a\n'),
        ('1 行に 2 つ', b'- [ ] a [ ]\n', b'- [x] a [x]\n'),
        ('行頭でない', b'a - [ ] b\n', b'a - [x] b\n'),
        ('CRLF', b'- [ ] a\r\n', b'- [x] a\r\n'),
        ('[-]', b'- [ ] a\n', b'- [-] a\n'),
        ('[a]', b'- [ ] a\n', b'- [a] a\n'),
        ('大きさの違い', b'- [ ] a\n', b'- [x] ab\n'),
        ('ほかの変更', b'- [ ] a\nx\n', b'- [x] a\ny\n'),
        ('箇条書きの後ろに空白が無い', b'-[ ] a\n', b'-[x] a\n'),
        ('改行で終わらない', b'- [ ] a', b'- [x] a'),
    ):
        cases.append(task_case(label, set_task(after), build=lambda base, t=before: make_world(base, task=t)))

    def to_link(world):
        real = Path(world.real)
        moved = real.with_name('moved.md')
        os.rename(real, moved)
        os.symlink(moved, real)
    cases.append(task_case('mapping-changed ファイル→リンク', to_link))

    def link_world(base):
        def prepare(world):
            os.symlink('real.md', world.repo / 'task/link.md')
            (world.repo / 'task/real.md').write_bytes(TASK_TEXT)
        return make_world(base, prepare=prepare, select='task/link.md')

    def link_to_file(world):
        link = world.repo / 'task/link.md'
        os.unlink(link)
        link.write_bytes(TASK_TEXT)
    cases.append(task_case('mapping-changed リンク→ファイル', link_to_file, build=link_world))

    def retarget(world):
        link = world.repo / 'task/link.md'
        (world.repo / 'task/other.md').write_bytes(TASK_TEXT)
        os.unlink(link)
        os.symlink('other.md', link)
    cases.append(task_case('mapping-changed 張り替え', retarget, build=link_world))

    def dangle(world):
        os.unlink(world.repo / 'task/real.md')
    cases.append(task_case('mapping-changed リンク切れ', dangle, build=link_world))

    def to_dir(world):
        os.unlink(world.real)
        os.mkdir(world.real)
    cases.append(task_case('mapping-changed 実体がフォルダ', to_dir))
    cases.append(task_case('読めない実体', lambda w: os.chmod(w.real, 0)))

    def body_mode(world):
        os.chmod(Path(world.state) / 'taskmd-body', 0o600)
    cases.append(task_case('taskmd-body の mode', body_mode))

    def body_text(world):
        (Path(world.state) / 'taskmd-body').write_bytes(b'changed\n')
    cases.append(task_case('taskmd-body の中身', body_text))
    for label, change in (
        ('T 行の sha', lambda d: d.replace(b'\t', b'\tX', 2)),
        ('T 行のパス', lambda d: d.replace(b'/task/t.md', b'/task/u.md', 1)),
        ('T 行の余分な欄', lambda d: d.replace(b'\n', b'\textra\n', 1)),
        ('T 行の改行が無い', lambda d: d.split(b'\n', 1)[0]),
    ):
        def build(base, c=change):
            world = make_world(base)
            path = Path(world.state) / 'manifest.tsv'
            path.write_bytes(c(path.read_bytes()))
            reseal(world)
            return world
        cases.append(task_case(label, None, build=build))
    for label, name in (('改行で終わる名前', b'task/t\n'), ('UTF-8 でない名前', b'task/t\xff.md'),
                        ('日本語の名前', 'task/日本語.md'.encode())):
        cases.append(task_case(label, set_task(b'# t\n- [x] one\n- [ ] two\nbody\n'),
                               build=lambda base, n=name: make_world(base, task_path=n, select=n)))
    return cases


TOOLBOX_NAMES = ('bash', 'sh', 'env', 'sha256sum', 'stat', 'realpath', 'readlink', 'sed', 'mktemp', 'rm', 'find', 'grep',
                 'cmp', 'od', 'tr', 'cut', 'link', 'python3', 'cat', 'ls', 'dirname', 'basename', 'git', 'timeout')


def toolbox(world, name='toolbox', cp='real'):
    """道具の別名だけを置いたフォルダ。cp は real(本物へのリンク)・none・dir・plain(実行できない)・chain。"""
    box = world.base / name
    box.mkdir(exist_ok=True)
    for tool in TOOLBOX_NAMES:
        found = shutil.which(tool)
        if found and not (box / tool).exists():
            os.symlink(os.path.realpath(found), box / tool)
    real_cp = os.path.realpath(shutil.which('cp'))
    if cp == 'real':
        os.symlink(real_cp, box / 'cp')
    elif cp == 'dir':
        (box / 'cp').mkdir()
    elif cp == 'plain':
        (box / 'cp').write_bytes(b'#!/bin/sh\nexit 0\n')
        os.chmod(box / 'cp', 0o644)
    elif cp == 'chain':
        os.symlink(real_cp, box / 'cp2')
        os.symlink('cp2', box / 'cp1')
        os.symlink(str(box / 'cp1'), box / 'cp')
    return str(box)


def restore_case(label, tweak=None, build=standard, **kw):
    kw.setdefault('cp_stub', True)
    return Case('restore-taskmd', label, state_args('restore-taskmd'), build=build, tweak=tweak, **kw)


def empty_component(base):
    """記録の実体パス(taskmd.txt の real と T 行のパス)に空の要素を入れ、ダイジェストを合わせ直す。
    旧版の bash の確かめは通り、旧 helper が引数の誤り(internal-argument)で応答する。"""
    world = make_world(base)
    real = os.fsencode(world.real)
    doubled = real.replace(b'/task/', b'//task/', 1)
    state = Path(world.state)
    manifest = state / 'manifest.tsv'
    head, sep, rest = manifest.read_bytes().partition(b'\n')
    manifest.write_bytes(head.replace(real, doubled) + sep + rest)
    taskmd = state / 'snapshot' / 'taskmd.txt'
    taskmd.write_bytes(replace_line(b'real', doubled)(taskmd.read_bytes()))
    reseal(world)
    return world


def parent_to_link(world):
    task = world.repo / 'task'
    os.rename(task, world.repo / 'task.orig')
    elsewhere = world.base / 'elsewhere'
    elsewhere.mkdir()
    (elsewhere / 't.md').write_bytes(b'victim\n')
    os.symlink(elsewhere, task)


def changed_task(world):
    Path(world.real).write_bytes(b'# t\n- [ ] one\nchanged body\n')


def env_entries(*items):
    return lambda world: [item.encode() if isinstance(item, str) else item for item in items]


# 旧スクリプトの変数と同じ名前(追加の決定 4)。再現する名前と再現しない 6 個を、別々に件数と sha256 で固定する。
SCRIPT_ENV_REPRODUCED = FIXTURES / 'script_env_reproduced.json'
SCRIPT_ENV_REPRODUCED_FIXED = (94, '19794fd76472e00a5713c9a624728dad0ac279a3d42089f88a9a2b2e4e7e7e49')
SCRIPT_ENV_NOT_REPRODUCED = FIXTURES / 'script_env_not_reproduced.json'
SCRIPT_ENV_NOT_REPRODUCED_FIXED = (6, 'fd6ec620e754a09adaa1e621447f98e0d422c3c5111d10a7b4ec2b2b32c1eef0')


def fixed_names(path, expected):
    data = path.read_bytes()
    names = json.loads(data)['names']
    if (len(names), hashlib.sha256(data).hexdigest()) != expected:
        raise AssertionError(f'{path.name} の件数か sha256 が固定と違う')
    return tuple(names)


SCRIPT_NAMES = fixed_names(SCRIPT_ENV_REPRODUCED, SCRIPT_ENV_REPRODUCED_FIXED)
NOT_REPRODUCED_NAMES = fixed_names(SCRIPT_ENV_NOT_REPRODUCED, SCRIPT_ENV_NOT_REPRODUCED_FIXED)
if set(SCRIPT_NAMES) & set(NOT_REPRODUCED_NAMES):
    raise AssertionError('再現する名前と再現しない名前の一覧が重なる')
SPECIAL_NAMES = ('BASH', 'BASH_VERSION', 'BASHPID', 'PPID', 'UID', 'EUID', 'GROUPS', 'RANDOM', 'SRANDOM', 'SECONDS',
                 'LINENO', 'BASH_COMMAND', 'BASH_SUBSHELL', 'BASH_VERSINFO', 'HISTCMD', 'IFS', 'OPTIND', 'OPTERR',
                 'PS1', 'PS2', 'PS4', 'COMP_WORDBREAKS', 'TERM', 'TZ', 'COLUMNS', 'EPOCHSECONDS', 'EPOCHREALTIME',
                 'HOSTNAME', 'OSTYPE', 'MACHTYPE', 'SHELL', '_', 'FUNCNAME', 'DIRSTACK', 'PIPESTATUS', 'BASH_REMATCH')


def group_restore():
    cases = [
        restore_case('replaced', changed_task),
        restore_case('copied', lambda w: os.unlink(w.real)),
        restore_case('dest-symlink', lambda w: (os.unlink(w.real), os.symlink('/nonexistent', w.real))),
        restore_case('dest-dir', lambda w: (os.unlink(w.real), os.mkdir(w.real))),
        # 観測器でない本物の cp(Linux は GNU cp で復元する。Mac は BSD の cp が --version に答えず止まる)。
        restore_case('本物の cp(Mac では cp の確かめで止まる)', changed_task, cp_stub=False),
        restore_case('記録の実体パスに空の要素(helper の引数の誤り)', changed_task, build=empty_component),
        # 旧 selftest の J4 と同じ形(変異 e が落ちる所): 復元先の親を、同じ名前のファイルを持つフォルダへの
        # リンクに差し替える。親のリンクを辿らない開き方と段ごとの確かめがあれば、リンク先に触れない。
        restore_case('復元先の親がリンクに差し替わった', parent_to_link,
                     observe=lambda w: {'elsewhere': str(w.base / 'elsewhere'), 'parent': str(w.repo / 'task')}),
        restore_case('prepare-failed', lambda w: (changed_task(w), os.chmod(os.path.dirname(w.real), 0o500))),
        restore_case('stdout が /dev/full', changed_task, stdout='full'),
        restore_case('stdout が閉じたパイプ', changed_task, stdout='closed'),
        restore_case('stdout が上限 40 バイト', changed_task, stdout='file', preexec=fsize_limit(40)),
        restore_case('stdout がフォルダ', changed_task, stdout='dir', cp_stub=False),
        restore_case('stderr がフォルダ', changed_task, stderr='dir', cp_stub=False),
        restore_case('RESTORE_TEMP_STATE を受け継いだ失敗',
                     None, build=edit_manifest(lambda d: d.replace(b'\n', b'\tx\n', 1)),
                     env=env_entries('RESTORE_TEMP_STATE=kept')),
    ]
    for label, change in (
        ('T 行の余分な欄', lambda d: d.replace(b'\n', b'\textra\n', 1)),
        ('T 行の mode', lambda d: d.replace(b'T\t644', b'T\t6448', 1)),
        ('T 行の sha', lambda d: d.replace(b'\t', b'\tz', 2)),
        ('T 行のパス', lambda d: d.replace(b'/task/t.md', b'/task/u.md', 1)),
        ('T 行の種類', lambda d: b'F' + d[1:]),
        ('T 行の改行が無い', lambda d: d.split(b'\n', 1)[0]),
        ('T 行の引用の誤り', lambda d: d.replace(b'\t/', b'\t"\\1%z/', 1)),
    ):
        cases.append(restore_case(label, None, build=edit_manifest(change), cp_stub=False))
    for label, kind in (('cp が無い', 'none'), ('cp がフォルダ', 'dir'), ('cp が実行できない', 'plain'),
                        ('cp のリンクの連鎖', 'chain')):
        cases.append(restore_case(label, changed_task, cp_stub=kind == 'chain', path=lambda w, k=kind: toolbox(w, cp=k)))
    cases.append(restore_case('PATH の空の要素', changed_task, cp_stub=False,
                              path=lambda w: ':' + toolbox(w, cp='none'), cwd=lambda w: Path(toolbox(w, 'here'))))
    cases.append(restore_case('PATH の相対の要素', changed_task, cp_stub=False,
                              path=lambda w: 'here:' + toolbox(w, cp='none'),
                              cwd=lambda w: (Path(toolbox(w, 'here')), w.base)[1]))
    # 子の環境(cp の観測器が記録する)。
    for label, entries in (
        ('PWD が同じ実体', ['PWD=<repo>/./']),
        ('PWD が違う', ['PWD=/']),
        ('PWD が相対', ['PWD=repo']),
        ('SHLVL 998', ['SHLVL=998']),
        ('SHLVL 999', ['SHLVL=999']),
        ('SHLVL 数でない', ['SHLVL=abc']),
        ('SHLVL 空白つき', ['SHLVL= 7 ']),
        ('SHLVL 負', ['SHLVL=-5']),
        ('SHLVL あふれ', ['SHLVL=4294967296']),
        ('OLDPWD フォルダ', ['OLDPWD=/']),
        ('OLDPWD フォルダでない', ['OLDPWD=/nonexistent']),
        ('識別子でない名前と重複', ['a-b=1', 'a-b=2', 'noequal', '=empty', 'DUP=1', 'DUP=2']),
        ('GIT_ の変数', ['GIT_DIR=/x', 'GIT_WORK_TREE=/y', 'GIT_INDEX_FILE=/z', 'GIT_COMMON_DIR=/c', 'GIT_PREFIX=p',
                       'GIT_NO_LAZY_FETCH=0', 'GIT_OPTIONAL_LOCKS=1', 'GIT_OTHER=keep']),
        ('LANG と LC_CTYPE', ['LANG=ja_JP.UTF-8', 'LC_CTYPE=C.UTF-8', 'LANGUAGE=ja']),
        ('LC_ALL が入っている名前', ['LC_ALL=C.UTF-8']),
        ('LC_ALL が入っていない名前', ['LC_ALL=xx_YY.UTF-8']),
        ('bash の特殊な変数', [f'{n}=v-{n}' for n in SPECIAL_NAMES]),
        ('旧版の変数の名前', [f'{n}=v-{n}' for n in SCRIPT_NAMES]),
        ('決定 4 の 6 個', [f'{n}=v-{n}' for n in NOT_REPRODUCED_NAMES]),
        ('決定 4 の 6 個と旧版の変数の名前', [f'{n}=w-{n}' for n in SCRIPT_NAMES + NOT_REPRODUCED_NAMES]),
        ('決定 4 の 6 個の重複と空の値', ['CAP=first', 'CAP=second', 'RP=', 'helper=a=b']),
    ):
        def env(world, items=entries):
            return [i.replace('<repo>', str(world.repo)).encode() for i in items]
        cases.append(restore_case(f'子の環境 {label}', changed_task, env=env))
    cases.append(restore_case('子の環境 PATH に bash が無い', changed_task,
                              path=lambda w: toolbox(w, 'nobash', cp='real') + ':/nonexistent',
                              env=env_entries('BASH=/x')))
    return cases


def edit_manifest(change):
    def build(base):
        world = make_world(base)
        path = Path(world.state) / 'manifest.tsv'
        path.write_bytes(change(path.read_bytes()))
        reseal(world)
        return world
    return build


def fsize_limit(size):
    def preexec():
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    return preexec


def pending_scan(world, side):
    """cleanup の後の pending-implementation.py scan の結果(候補の数と結論)。"""
    done = subprocess.run([sys.executable, '-I', '-B', str(PENDING), 'scan', '--cwd', str(world.repo)],
                          capture_output=True, env={'PATH': os.environ.get('PATH', ''), 'HOME': str(world.home),
                                                    'XDG_STATE_HOME': str(world.xdg)})
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return {'rc': done.returncode, 'raw': done.stdout.decode(errors='replace')}
    return {'rc': done.returncode, 'result': data.get('result'), 'candidates': len(data.get('candidates', []))}


def cleanup_case(label, tweak=None, build=standard, argv=None, **kw):
    kw.setdefault('after', pending_scan)
    kw.setdefault('observe', lambda w: {'mark': os.path.join(os.path.dirname(w.state), 'owner-fixed')})
    return Case('cleanup', label, argv or (lambda w: ['cleanup', '--state', w.state]), build=build, tweak=tweak,
                **kw)


def mark_path(world):
    return os.path.join(os.path.dirname(world.state), 'owner-fixed')


def group_cleanup():
    cases = [
        cleanup_case('成功(印あり)'),
        cleanup_case('成功(印なし)', lambda w: os.unlink(mark_path(w))),
        cleanup_case('stderr が書けない', stderr='full'),
        cleanup_case('stderr が閉じたパイプ', stderr='closed'),
        cleanup_case('2 回目', lambda w: (shutil.rmtree(w.state), os.unlink(mark_path(w)))),
        cleanup_case('印がフォルダ', lambda w: (os.unlink(mark_path(w)), os.mkdir(mark_path(w)))),
        cleanup_case('印がリンク', lambda w: (os.unlink(mark_path(w)), os.symlink('/nonexistent', mark_path(w)))),
        cleanup_case('番号の違う印', lambda w: rewrite_mark(w, lambda d: re.sub(rb'guard_ino\t\d+', b'guard_ino\t1', d))),
        cleanup_case('NUL を含む印', lambda w: rewrite_mark(w, lambda d: d.replace(b'guard_dev', b'x\0guard_dev', 1))),
        cleanup_case('読めない印', lambda w: os.chmod(mark_path(w), 0)),
        cleanup_case('印の名前が形に合わない', build=lambda base: make_world(base, suffix='a b'),
                     observe=lambda w: {}),
        cleanup_case('中に書けないフォルダ', lambda w: unwritable_inside(w, 0o500)),
        cleanup_case('中に読めないフォルダ', lambda w: unwritable_inside(w, 0o000)),
        # 読めるが検索できないフォルダ: GNU rm は中の名前を読み、消せない子ごとに文を出す。
        cleanup_case('中に検索できないフォルダ(mode 400)', lambda w: unwritable_inside(w, 0o400)),
        cleanup_case('中に検索できないフォルダ(mode 600)', lambda w: unwritable_inside(w, 0o600)),
        cleanup_case('中に空の読めないフォルダ', lambda w: empty_unreadable(w)),
        cleanup_case('印の置き場に書けない', lambda w: os.chmod(os.path.dirname(w.state), 0o500)),
        cleanup_case('mode 755 の記録', lambda w: os.chmod(w.state, 0o755)),
        cleanup_case('末尾の /', argv=lambda w: ['cleanup', '--state', w.state + '/']),
    ]
    return cases


def rewrite_mark(world, change):
    path = Path(mark_path(world))
    path.write_bytes(change(path.read_bytes()))


def unwritable_inside(world, mode):
    inner = Path(world.state) / 'files' / 'deep'
    inner.mkdir(parents=True, exist_ok=True)
    (inner / 'x').write_bytes(b'x')
    (inner / 'y').write_bytes(b'y')
    os.chmod(inner, mode)


def empty_unreadable(world):
    inner = Path(world.state) / 'files' / 'empty'
    inner.mkdir(parents=True, exist_ok=True)
    os.chmod(inner, 0)


def ignore_pipe():
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def ignore_signals(*numbers):
    def preexec():
        for number in numbers:
            signal.signal(number, signal.SIG_IGN)
    return preexec


def block_signals(*numbers):
    def preexec():
        signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    return preexec


def chain(*functions):
    def preexec():
        for function in functions:
            function()
    return preexec


def output_targets():
    """(名前, stdout, stderr, preexec)。上限のある通常ファイルは stdout だけ(stderr の文には旧版の呼出名が
    入りうるので、22:26・22:38 の切り方を要する。使い方と ERROR の行は呼出名を含まない stderr で別に見る)。"""
    targets = []
    for stream in ('stdout', 'stderr'):
        for kind, preexec in (('full', None), ('closed', None), ('closed', ignore_pipe), ('dir', None)):
            name = f'{stream} {kind}' + (' SIGPIPE 無視' if preexec else '')
            targets.append((name, kind if stream == 'stdout' else 'pipe', kind if stream == 'stderr' else 'pipe',
                            preexec))
    for size in (0, 5, 13, 40):
        targets.append((f'stdout 上限 {size}', 'file', 'pipe', fsize_limit(size)))
    return targets


def group_output():
    cases = []
    sources = (
        ('使い方 -h', None, fixed(['-h'])),
        ('引数の誤り', None, fixed(['bogus'])),
        ('taskmd-diff same', standard, state_args('taskmd-diff')),
        ('state_fail taskmd-diff', None, fixed(['taskmd-diff', '--cwd', '/x'] + BAD_STATE)),
        ('state_fail restore-taskmd', None, fixed(['restore-taskmd', '--cwd', '/x'] + BAD_STATE)),
        ('state_fail config-check', None, fixed(['config-check', '--cwd', '/x'] + BAD_STATE)),
        ('state_fail compare', None, fixed(['compare', '--cwd', '/x'] + BAD_STATE + ['--run-rc', '0'])),
        ('cleanup の NOTE', standard, lambda w: ['cleanup', '--state', w.state]),
        ('restore_fail', edit_manifest(lambda d: d.replace(b'\n', b'\tx\n', 1)), state_args('restore-taskmd')),
    )
    for source, build, argv in sources:
        for name, out, err, preexec in output_targets():
            if source.startswith(('使い方', '引数')) and out == 'dir':
                continue
            cases.append(Case('出力の故障', f'{source} {name}', argv, build=build, stdout=out, stderr=err,
                              preexec=preexec))
    # stderr の上限(使い方と ERROR の行は旧版の呼出名を含まない)。
    for size in (0, 7, 100, 4096, 9000):
        cases.append(Case('出力の故障', f'使い方 -h stderr 上限 {size}', fixed(['-h']), stderr='file',
                          preexec=fsize_limit(size)))
        cases.append(Case('出力の故障', f'引数の誤り stderr 上限 {size}', fixed(['bogus']), stderr='file',
                          preexec=fsize_limit(size)))
    for size in (0, 20):
        cases.append(Case('出力の故障', f'cleanup の NOTE stderr 上限 {size}', lambda w: ['cleanup', '--state', w.state],
                          build=standard, stderr='file', preexec=fsize_limit(size)))
    return cases


def group_startup():
    cases = []

    def add(label, entries, argv=None, build=None, allow=('BASH_COMPAT',), **kw):
        cases.append(Case('起動の状態', label, argv or fixed([]), build=build, env=env_entries(*entries),
                          allow_env=allow, **kw))

    for value in ('998', '999', 'abc', '', ' 5', '2147483647'):
        add(f'SHLVL {value!r}', [f'SHLVL={value}'])
    add('SHLVL が無い', [])
    for value in ('abc', '99', '-1', '1x', ' 2'):
        add(f'BASH_XTRACEFD {value!r}', [f'BASH_XTRACEFD={value}'])
    add('BASH_XTRACEFD 読み取り専用の 0', ['BASH_XTRACEFD=0'])
    add('BASH_XTRACEFD 2', ['BASH_XTRACEFD=2'])
    for value in ('99', '30', '5.3', '4.x', '100', 'abc'):
        add(f'BASH_COMPAT 範囲の外 {value!r}', [f'BASH_COMPAT={value}'])
    for value in ('xx_YY', 'xx_YY.UTF-8', 'ja_JP.UTF-8', 'ja_JP.utf8', 'C.UTF-8', 'C.utf8', 'POSIX', 'a/b',
                  'LC_CTYPE=C;LC_NUMERIC=C'):
        add(f'LC_ALL {value!r}', [f'LC_ALL={value}'])
    add('LANG だけ', ['LANG=xx_YY'])
    add('LC_MESSAGES だけ', ['LC_MESSAGES=xx_YY'])
    add('警告が 4 つ重なる', ['SHLVL=999', 'BASH_XTRACEFD=abc', 'BASH_COMPAT=99', 'LC_ALL=xx_YY'])
    cases.append(Case('起動の状態', 'LOCPATH の下の地域設定', fixed([]), build=locale_world,
                      env=lambda w: [b'LOCPATH=' + os.fsencode(w.base / 'locales'), b'LC_ALL=qq_QQ.UTF-8']))
    cases.append(Case('起動の状態', 'LOCPATH の下に無い地域設定', fixed([]), build=locale_world,
                      env=lambda w: [b'LOCPATH=' + os.fsencode(w.base / 'locales'), b'LC_ALL=rr_RR.UTF-8']))
    add('TERM・INT・HUP を無視した起動', [], argv=state_args('taskmd-diff'), build=standard,
        preexec=ignore_signals(signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
    cases.append(restore_case('SIGCHLD を無視した起動の restore', changed_task,
                              preexec=ignore_signals(signal.SIGCHLD)))
    cases.append(restore_case('SIGCHLD を無視した起動の copy-failed', changed_task, cp_mode='version-only',
                              preexec=ignore_signals(signal.SIGCHLD)))
    cases.append(restore_case('SIGCHLD・TERM・USR1 を止めた起動の restore', changed_task,
                              preexec=block_signals(signal.SIGCHLD, signal.SIGTERM, signal.SIGUSR1)))
    cases.append(restore_case('TERM・INT・HUP を無視した起動の restore', changed_task,
                              preexec=ignore_signals(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)))
    return cases


def locale_world(base):
    """LOCPATH の下に、全部の種類のファイルを置いた地域設定 qq_QQ.utf8 を作る。"""
    world = make_world(base, take=False)
    source = Path('/usr/lib/locale/C.utf8')
    if not source.is_dir():
        raise AssertionError('/usr/lib/locale/C.utf8 が無いので、LOCPATH の地域設定を作れない')
    shutil.copytree(source, world.base / 'locales' / 'qq_QQ.utf8', symlinks=True)
    return world


def group_cross_read():
    """旧 take の記録を Rust の 3 つのサブコマンドが読む。"""
    cases = []

    def tracked(world):
        run_git(world.repo, 'add', 'task/t.md')
        run_git(world.repo, 'commit', '-qm', 't')

    def linked(world):
        os.rename(world.repo / 'task/t.md', world.repo / 'task/real.md')
        os.symlink('real.md', world.repo / 'task/t.md')

    layouts = (
        ('追跡済み', {'prepare': tracked}),
        ('リポジトリの外', {'task_path': '../outside/t.md'}),
        ('.claude/reviews の下', {'task_path': '.claude/reviews/t.md'}),
        ('リンクのタスク本文', {'prepare': linked}),
        ('日本語と改行と UTF-8 でない名前', {'task_path': b'task/\xe6\x97\xa5\n\xff.md'}),
    )
    for label, options in layouts:
        def build(base, o=options):
            return make_world(base, **o)
        for sub, tweak in (('taskmd-diff', set_task(b'# t\n- [x] one\n- [ ] two\nbody\n')),
                           ('restore-taskmd', changed_task), ('cleanup', None)):
            cases.append(Case('旧版の記録の相互読み', f'{label} {sub}', state_args(sub), build=build, tweak=tweak,
                              cp_stub=sub == 'restore-taskmd',
                              after=pending_scan if sub == 'cleanup' else None))
    return cases


def group_git_and_tools():
    cases = []
    for sub, tweak in (('taskmd-diff', set_task(b'# t\n- [x] one\n- [ ] two\nbody\n')),
                       ('restore-taskmd', changed_task), ('cleanup', None)):
        cases.append(Case('Git と道具の不起動', f'{sub} 偽の git', state_args(sub), build=standard, tweak=tweak,
                          git_stub=True, after=pending_scan if sub == 'cleanup' else None))
    return cases


# ---- 旧 selftest の 2 通り(Linux だけ) ----

SELFTEST_IDS = ('A', 'G', 'H2', 'I', 'J', 'K', 'M6', 'P2', 'P3', 'P5', 'P6', 'P7', 'P8', 'P9', 'B2', 'B4', 'B5', 'B6',
                'B10', 'B11', 'B12', 'C10', 'E9')
SELFTEST_ALLOWED = FIXTURES / 'selftest_allowed.json'
SELFTEST_ALLOWED_FIXED = (28, '5ff804da4e4e2a3c9ceea6ca02dffad3be7243ce1e5d5d96124fdadb646a544a')  # (件数, sha256)
SELFTEST_MIN_RUST_LINES = 583           # Rust を通った起動を含む確かめの行の最低件数(基準の容器の実測)。
RESULT_LINE = re.compile(r'^(PASS|FAIL)  (\S+) (.*)$')

WRAPPER = '''#!/usr/bin/env bash
# S3I-A の Rust 流しの包み。taskmd-diff・restore-taskmd・cleanup と、引数なし・不明なサブコマンド・
# -h・--help は Rust の検査専用入口へ、take・config-check・compare は旧版へ渡す。Rust へ渡した起動を記録する。
case "${{1-}}" in
  take|config-check|compare) exec bash '{old}' "$@" ;;
  *) printf '%s\\n' "${{1-<none>}}" >> '{log}'; exec '{rust}' "$@" ;;
esac
'''


def selftest_lines(output):
    lines = []
    for line in output.decode('utf-8', 'replace').splitlines():
        match = RESULT_LINE.match(line)
        if match:
            lines.append(match.groups())
    return lines


def run_selftest(target, ids, root, log=None):
    """旧 selftest を --only <id> で 1 つずつ流し、(id, 結果の行の列, Rust へ渡した起動の数) を返す。"""
    results = []
    for case_id in ids:
        before = len(log.read_text().splitlines()) if log is not None and log.exists() else 0
        env = {k: v for k, v in os.environ.items() if k not in ('IMPLEMENT_GUARD',)}
        if target is not None:
            env['IMPLEMENT_GUARD'] = str(target)
        done = subprocess.run(['bash', str(SELFTEST), '--only', case_id], env=env, capture_output=True,
                              timeout=900, stdin=subprocess.DEVNULL)
        after = len(log.read_text().splitlines()) if log is not None and log.exists() else 0
        lines = selftest_lines(done.stdout)
        if not lines:
            raise AssertionError(f'selftest の {case_id} が確かめの行を出さなかった(終了値 {done.returncode})')
        results.append((case_id, lines, after - before))
    return results


def load_allowed():
    data = SELFTEST_ALLOWED.read_bytes()
    entries = json.loads(data)
    if (len(entries), hashlib.sha256(data).hexdigest()) != SELFTEST_ALLOWED_FIXED:
        raise AssertionError('selftest の許す失敗の一覧の件数か sha256 が固定と違う')
    return {(e['case'], e['line']) for e in entries}


def judge_selftest(old, rust, allowed, minimum):
    """2 通りの結果を判定する。旧版流しは全部 PASS、Rust 流しの FAIL は一覧の中だけ・一覧の中は FAIL、
    確かめの行の数が同じ、Rust を通った行が最低件数以上。(Rust を通った行, 旧版だけの行) を返す。"""
    problems = []
    rust_lines = 0
    old_only = 0
    seen_allowed = set()
    for (case_id, old_lines, _), (rust_id, new_lines, launches) in zip(old, rust):
        if case_id != rust_id:
            problems.append(f'ケースの並びが違う: {case_id} / {rust_id}')
            continue
        if len(old_lines) != len(new_lines):
            problems.append(f'{case_id}: 確かめの行の数が違う(旧 {len(old_lines)} / Rust {len(new_lines)})')
        for verdict, _, text in old_lines:
            if verdict != 'PASS':
                problems.append(f'{case_id}: 旧版流しの FAIL: {text}')
        for verdict, cid, text in new_lines:
            key = (cid, text)
            if verdict == 'FAIL' and key not in allowed:
                problems.append(f'{case_id}: 一覧の外の FAIL: {text}')
            if verdict == 'PASS' and key in allowed:
                problems.append(f'{case_id}: 一覧の中の PASS: {text}')
            if key in allowed:
                seen_allowed.add(key)
        if launches > 0:
            rust_lines += len(new_lines)
        else:
            old_only += len(new_lines)
    for key in sorted(allowed - seen_allowed):
        problems.append(f'許す失敗の一覧の行が出なかった: {key}')
    if rust_lines < minimum:
        problems.append(f'Rust を通った確かめの行が最低件数より少ない: {rust_lines} < {minimum}')
    return rust_lines, old_only, problems


def selftest_stage(root):
    log = Path(root) / 'wrapper.log'
    wrapper = Path(root) / 'implement-guard-wrapper.sh'
    wrapper.write_text(WRAPPER.format(old=OLD, log=log, rust=FIXTURE))
    wrapper.chmod(0o755)
    started = time.monotonic()
    old = run_selftest(None, SELFTEST_IDS, root)
    middle = time.monotonic()
    rust = run_selftest(wrapper, SELFTEST_IDS, root, log)
    finished = time.monotonic()
    rust_lines, old_only, problems = judge_selftest(old, rust, load_allowed(), SELFTEST_MIN_RUST_LINES)
    total_old = sum(len(lines) for _, lines, _ in old)
    total_rust = sum(len(lines) for _, lines, _ in rust)
    print(f'S3I-A: 旧 selftest: 旧版流し {total_old} 行({middle - started:.1f} 秒)・Rust 流し {total_rust} 行'
          f'(Rust を通った行 {rust_lines}・旧版だけの行 {old_only}・{finished - middle:.1f} 秒)', flush=True)
    return problems, (old, rust)


def dump_selftest(old, rust, path):
    """許す失敗の一覧の下書き(実際の出力から作る)。"""
    entries = []
    for (case_id, _, _), (_, lines, _) in zip(old, rust):
        for verdict, cid, text in lines:
            if verdict == 'FAIL':
                entries.append({'case': cid, 'line': text, 'reason': ''})
    Path(path).write_text(json.dumps(entries, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


# ---- 決定 D5(旧版が runtime-unavailable になる条件) ----

def d5_cases():
    """旧版が python3 と同梱 helper で runtime-unavailable になること(原因の文を確かめる)と、Rust が
    通常の環境の旧版と同じ結果になること。"""
    def no_python(world):
        return _without(world, 'python3')

    def failing_python(world):
        box = Path(_without(world, 'python3'))
        fake = box / 'python3'
        fake.write_text('#!/bin/sh\nexit 1\n')
        fake.chmod(0o755)
        return str(box)

    def normal_path(world):
        return os.environ.get('PATH', '')

    def copy_without_helper(world):
        # helper(restore-taskmd.py)の無いフォルダに置いた旧版のコピー(中身は同じ)。
        folder = world.base / 'old-without-helper'
        folder.mkdir(exist_ok=True)
        target = folder / OLD.name
        shutil.copyfile(OLD, target)
        return target

    return (
        ('python3 が無い', no_python, '復元に必要な Python 3 が無い', None),
        ('python3 が失敗する偽', failing_python, 'Python の必要機能か同梱 helper を読み込めない', None),
        ('helper の無い場所に置いた旧版のコピー', normal_path, '同梱 helper を読めない', copy_without_helper),
    )


def _without(world, name):
    box = world.base / f'without-{name}'
    box.mkdir(exist_ok=True)
    for tool in TOOLBOX_NAMES + ('cp',):
        if tool == name:
            continue
        found = shutil.which(tool)
        if found and not (box / tool).exists():
            os.symlink(os.path.realpath(found), box / tool)
    return str(box)


def run_d5(root):
    """各条件で、旧版(条件あり)が理由の文で止まり、Rust(条件あり)= 旧版(条件なし)。"""
    problems = []
    count = 0
    for label, make_path, reason, make_old in d5_cases():
        base = Path(root) / f'd5-{count}'
        base.mkdir(parents=True, exist_ok=True)
        try:
            world = make_world(base / 'w')
            template = base / 'template'
            copy_world(world.base, template)
            sides = {}
            for name, legacy, special in (('old-special', True, True), ('old-normal', True, False),
                                          ('rust-special', False, True)):
                copy_world(template, world.base)
                fix_mark(world)
                changed_task(world)
                path = make_path(world) if special else os.environ.get('PATH', '')
                env = default_env(world, path)
                program, argv0, prefix = old_command() if legacy else rust_command()
                if legacy and special and make_old is not None:
                    copied = make_old(world)
                    prefix = [str(copied)]
                result = launch(program, argv0, encode_all(prefix + state_args('restore-taskmd')(world)), env,
                                cwd=world.repo, base=base)
                result = normalize(result, legacy)
                sides[name] = ((result.code, result.stdout, result.stderr, result.timed_out),
                               Path(world.real).read_bytes())
            old_special = sides['old-special'][0]
            if b'REASON=runtime-unavailable' not in old_special[1] or reason.encode() not in old_special[2]:
                problems.append(f'D5 {label}: 旧版が印の原因の文で止まらなかった(印の誤り)')
            elif sides['rust-special'] != sides['old-normal']:
                problems.append(f'D5 {label}: Rust が通常の環境の旧版と違う: '
                                f'{difference(str(sides["old-normal"]), str(sides["rust-special"]))}')
            count += 1
        except Exception as error:  # noqa: BLE001
            problems.append(f'D5 {label}: {type(error).__name__}: {error}')
        finally:
            make_writable(base)
            shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: 決定 D5: {count} 件', flush=True)
    return problems


# ---- /dev/null を開けない起動(作れる機械だけ) ----

DEVNULL_CASES = (
    ('-h', None, fixed(['-h'])),
    ('taskmd-diff same', standard, state_args('taskmd-diff')),
    ('restore-taskmd', standard, state_args('restore-taskmd')),
    ('cleanup', standard, lambda w: ['cleanup', '--state', w.state]),
)


def devnull_wrapper(root):
    """利用者の名前空間と mount の名前空間で /dev/null を mode 000 のファイルに差し替え、権限を落として
    起動するコマンド。作れなければ (None, 理由)。"""
    unshare, setpriv = shutil.which('unshare'), shutil.which('setpriv')
    if not unshare or not setpriv or not LINUX:
        return None, 'unshare か setpriv が無い'
    blocked = Path(root) / 'devnull-000'
    blocked.write_bytes(b'')
    blocked.chmod(0)
    wrapper = [unshare, '-U', '-m', '--map-current-user', '--keep-caps', '/bin/sh', '-c',
               'mount --bind "$0" /dev/null && p="$1" && shift && '
               'exec "$p" --inh-caps=-all --ambient-caps=-all --bounding-set=-all "$@"', str(blocked), setpriv]
    # wrapper の後ろに、起動器と引数がそのまま続く(setpriv が権限を落としてから exec する)。
    probe = subprocess.run([*wrapper, '/bin/sh', '-c', 'exec </dev/null'], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    if probe.returncode == 0 or b'Permission denied' not in probe.stderr:
        return None, ('名前空間で /dev/null を差し替えられない: '
                      + probe.stderr.decode(errors='replace').strip()[-300:])
    return wrapper, None


def run_devnull(root):
    """/dev/null を開けない起動(旧版の `exec </dev/null` の失敗)で、旧版と Rust を全部比べる。"""
    wrapper, reason = devnull_wrapper(root)
    if wrapper is None:
        return [], reason
    problems = []
    count = 0
    for label, build, argv in DEVNULL_CASES:
        base = Path(root) / f'devnull-{count}'
        base.mkdir(parents=True, exist_ok=True)
        try:
            world = build(base / 'w') if build is not None else None
            template = base / 'template'
            if world is not None:
                copy_world(world.base, template)
            sides = []
            for legacy in (True, False):
                if world is not None:
                    copy_world(template, world.base)
                    fix_mark(world)
                program, argv0, prefix = old_command() if legacy else rust_command()
                result = launch(program, argv0, encode_all(prefix + list(argv(world))), default_env(world),
                                cwd=world.repo if world is not None else base, base=base, wrapper=wrapper)
                result = normalize(result, legacy)
                sides.append(((result.code, result.stdout, result.stderr, result.timed_out),
                              observe_files(world, Case('', '', None))))
            if sides[0][0][3] or sides[1][0][3]:
                problems.append(f'/dev/null を開けない起動 {label}: 時間切れ')
            elif b'/dev/null: Permission denied' not in sides[0][0][2]:
                problems.append(f'/dev/null を開けない起動 {label}: 旧版が /dev/null を開けずに止まらなかった: '
                                f'{clip(sides[0][0])}')
            elif sides[0] != sides[1]:
                problems.append(f'/dev/null を開けない起動 {label}: {difference(str(sides[0]), str(sides[1]))}')
            count += 1
        except Exception as error:  # noqa: BLE001
            problems.append(f'/dev/null を開けない起動 {label}: {type(error).__name__}: {error}')
        finally:
            make_writable(base)
            shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: /dev/null を開けない起動: {count} 件', flush=True)
    return problems, None


# ---- fd 0・1・2 を閉じた起動(08:55 の決定。旧版と Rust の固定期待をそれぞれ確かめ、互いには比べない) ----

CLOSED_FDS = FIXTURES / 'closed_fds.json'
CLOSED_FDS_FIXED = (32, 'd250be4ea93bea1c84243304d00ed4b38ce27f128eca48f204c932a5e0a3864f')   # (旧版と Rust の記録の数, sha256)
CLOSED_FD_CASES = (
    ('-h', None, None, fixed(['-h'])),
    ('taskmd-diff same', standard, None, state_args('taskmd-diff')),
    ('restore-taskmd', standard, changed_task, state_args('restore-taskmd')),
    ('cleanup', standard, None, lambda w: ['cleanup', '--state', w.state]),
)
CLOSED_FD_SETS = ((0,), (1,), (2,), (0, 1, 2))


def close_fds(fds):
    def preexec():
        for fd in fds:
            os.close(fd)
    return preexec


def closed_fd_record(world, result, base):
    """終了値・取れた stdout と stderr(パスは置き換える)・記録と印とタスク本文の様子。"""
    def place(data):
        data = data.replace(os.fsencode(base), b'<base>').replace(os.fsencode(ROOT), b'<root>')
        return data.decode('utf-8', 'surrogateescape')
    record = {'code': result.code, 'stdout': place(result.stdout), 'stderr': place(result.stderr),
              'timed_out': result.timed_out}
    if world is not None:
        task = Path(world.real)
        record.update({
            'state_exists': os.path.isdir(world.state),
            'mark_exists': os.path.lexists(mark_path(world)),
            'work_left': len([n for n in os.listdir(world.state) if n.startswith('.work-')])
            if os.path.isdir(world.state) else 0,
            'task_sha256': hashlib.sha256(task.read_bytes()).hexdigest() if task.is_file() else None,
        })
    return record


def closed_fd_results(root):
    """旧版と Rust を、fd を閉じた起動で流した記録({ケース: {'old': …, 'rust': …}})。"""
    records = {}
    index = 0
    for label, build, tweak, argv in CLOSED_FD_CASES:
        for fds in CLOSED_FD_SETS:
            name = f'{label} fd {",".join(map(str, fds))} を閉じた'
            records[name] = {}
            for side, legacy in (('old', True), ('rust', False)):
                base = Path(root) / f'closed-{index}'
                index += 1
                base.mkdir(parents=True, exist_ok=True)
                try:
                    world = build(base / 'w') if build is not None else None
                    if tweak is not None and world is not None:
                        tweak(world)
                    program, argv0, prefix = old_command() if legacy else rust_command()
                    result = launch(program, argv0, encode_all(prefix + list(argv(world))), default_env(world),
                                    cwd=world.repo if world is not None else base, base=base,
                                    preexec=close_fds(fds))
                    records[name][side] = closed_fd_record(world, normalize(result, legacy), base)
                finally:
                    make_writable(base)
                    shutil.rmtree(base, ignore_errors=True)
    return records


def write_closed_fds(root, path):
    data = {'about': '08:55 の決定: 起動時に閉じた fd 0〜2 は、Rust の標準の実行時の初期化(閉じた 0〜2 を /dev/null で'
                     '開き直す)をそのまま使い、旧版との差を認める。旧版と Rust の固定期待をそれぞれ記録し、互いには比べない。',
            'cases': closed_fd_results(root)}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return sum(len(v) for v in data['cases'].values())


def run_closed_fds(root):
    """fd を閉じた起動の旧版と Rust の結果が、それぞれの固定期待と全部同じこと。"""
    if not CLOSED_FDS.exists():
        return ['fd を閉じた起動の固定期待(closed_fds.json)が無い']
    data = CLOSED_FDS.read_bytes()
    fixed_cases = json.loads(data)['cases']
    count = sum(len(v) for v in fixed_cases.values())
    if (count, hashlib.sha256(data).hexdigest()) != CLOSED_FDS_FIXED:
        return ['fd を閉じた起動の固定期待の件数か sha256 が固定と違う']
    problems = []
    got = closed_fd_results(root)
    for name in sorted(set(fixed_cases) | set(got)):
        for side in ('old', 'rust'):
            want, have = fixed_cases.get(name, {}).get(side), got.get(name, {}).get(side)
            if want != have:
                problems.append(f'fd を閉じた起動 {name} の{"旧版" if side == "old" else " Rust"}が固定期待と違う: '
                                f'{difference(want, have)}')
    print(f'S3I-A: fd を閉じた起動: 旧版と Rust の {count} 件を、それぞれの固定期待で確かめた', flush=True)
    return problems


# ---- Rust が cp のほかの道具を起動しないこと ----

def run_restricted_path(root):
    """PATH を cp だけ(restore-taskmd)か空のフォルダだけ(ほか)にした Rust の結果が、通常の PATH の結果と同じ。
    PATH を絞ったケースでは、環境に BASH を入れない。"""
    problems = []
    count = 0
    for sub, tweak in (('taskmd-diff', set_task(b'# t\n- [x] one\n- [ ] two\nbody\n')), ('restore-taskmd', changed_task),
                       ('cleanup', None)):
        base = Path(root) / f'path-{sub}'
        base.mkdir(parents=True, exist_ok=True)
        try:
            world = make_world(base / 'w')
            template = base / 'template'
            copy_world(world.base, template)
            results = []
            for restricted in (False, True):
                copy_world(template, world.base)
                fix_mark(world)
                if tweak:
                    tweak(world)
                if restricted:
                    box = world.base / 'only'
                    box.mkdir()
                    if sub == 'restore-taskmd':
                        os.symlink(os.path.realpath(shutil.which('cp')), box / 'cp')
                    path = str(box)
                else:
                    path = os.environ.get('PATH', '')
                env = default_env(world, path)
                result = launch(FIXTURE, str(FIXTURE), encode_all(state_args(sub)(world)), env, cwd=world.repo, base=base)
                result = normalize(result, False)
                results.append(((result.code, result.stdout, result.stderr, result.timed_out),
                                observe_files(world, Case('', '', None))))
            if results[0] != results[1]:
                problems.append(f'PATH を絞った {sub}: 結果が通常の PATH と違う: {difference(str(results[0]), str(results[1]))}')
            count += 1
        except Exception as error:  # noqa: BLE001
            problems.append(f'PATH を絞った {sub}: {type(error).__name__}: {error}')
        finally:
            make_writable(base)
            shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: PATH を絞った Rust: {count} 件', flush=True)
    return problems


def strace_usable(root):
    """strace があり、ptrace で /bin/true の execve を記録できるか。使えなければ理由。"""
    strace = shutil.which('strace')
    if not strace:
        return None, 'strace が無い'
    probe = Path(root) / 'strace-probe.txt'
    done = subprocess.run([strace, '-f', '-qq', '-e', 'trace=execve', '-o', str(probe), '/bin/true'],
                          capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    text = probe.read_text(errors='replace') if probe.exists() else ''
    if done.returncode != 0 or len(re.findall(r'execve\(', text)) != 1:
        return None, 'ptrace が使えない(' + done.stderr.decode(errors='replace').strip()[-200:] + ')'
    return strace, None


def run_strace(root):
    """Linux: strace -f -e trace=execve で、Rust が cp のほかを exec しないこと(restore-taskmd は cp を 1 回以上
    exec すること)。ptrace が使えるかは最初に 1 回だけ確かめ、使えなければ未実施として記録する(成功に数えない)。
    使えるのに記録が空なら不合格。見つけた不合格は最後まで保つ。"""
    strace, reason = strace_usable(root)
    if strace is None:
        return [], reason
    problems = []
    count = 0
    real_cp = os.path.realpath(shutil.which('cp') or '/bin/cp')
    for sub, tweak in (('taskmd-diff', None), ('restore-taskmd', changed_task), ('cleanup', None)):
        base = Path(root) / f'strace-{sub}'
        base.mkdir(parents=True, exist_ok=True)
        try:
            world = make_world(base / 'w')
            if tweak:
                tweak(world)
            trace = base / 'trace'
            env = default_env(world)
            command = [strace, '-f', '-qq', '-e', 'trace=execve', '-o', str(trace), str(FIXTURE)]
            subprocess.run([str(TOOLS.launcher), *env, '--', strace, 'strace', *command[1:],
                            *encode_all(state_args(sub)(world))], cwd=world.repo, capture_output=True,
                           timeout=120, stdin=subprocess.DEVNULL)
            text = trace.read_text(errors='replace') if trace.exists() else ''
            programs = re.findall(r'execve\("([^"]*)"', text)
            if not programs:
                problems.append(f'strace {sub}: ptrace は使えるのに、起動の記録が空')
                continue
            copies = [p for p in programs[1:] if os.path.realpath(p) == real_cp]
            others = [p for p in programs[1:] if os.path.realpath(p) != real_cp]
            if programs[:1] != [str(FIXTURE)] or others:
                problems.append(f'strace {sub}: cp のほかを exec した: {others or programs}')
            elif sub == 'restore-taskmd' and not copies:
                problems.append('strace restore-taskmd: cp を 1 回も exec しなかった')
            elif sub != 'restore-taskmd' and copies:
                problems.append(f'strace {sub}: cp を exec した')
            count += 1
        except Exception as error:  # noqa: BLE001
            problems.append(f'strace {sub}: {type(error).__name__}: {error}')
        finally:
            make_writable(base)
            shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: strace: {count} 件', flush=True)
    return problems, None


# ---- 詰めた FIFO で止めて信号を送る(Linux だけ) ----

FIFO_SOURCES = (
    ('taskmd-diff same', 'stdout', standard, state_args('taskmd-diff')),
    ('state_fail restore-taskmd', 'stdout', None, fixed(['restore-taskmd', '--cwd', '/x'] + BAD_STATE)),
    ('state_fail taskmd-diff', 'stderr', None, fixed(['taskmd-diff', '--cwd', '/x'] + BAD_STATE)),
    ('cleanup の NOTE', 'stderr', standard, lambda w: ['cleanup', '--state', w.state]),
    ('restore_fail stdout', 'stdout', edit_manifest(lambda d: d.replace(b'\n', b'\tx\n', 1)), state_args('restore-taskmd')),
    ('restore_fail stderr', 'stderr', edit_manifest(lambda d: d.replace(b'\n', b'\tx\n', 1)), state_args('restore-taskmd')),
    ('引数の誤り', 'stderr', None, fixed(['bogus'])),
    # 旧 helper の引数の誤りの応答を書く間の信号(SIGINT は KeyboardInterrupt。決定の表の行)。
    ('helper の引数の誤りの応答', 'stdout', empty_component, state_args('restore-taskmd')),
)
KEYBOARD_SOURCES = ('helper の引数の誤りの応答',)
TRACEBACK = b'Traceback (most recent call last):\n'


def keyboard_equal(old, new):
    """KeyboardInterrupt の比べ方(22:35・08:55 の決定): 信号で終わること・終了値・最後の行
    `KeyboardInterrupt`・それまでの出力を比べ、呼出履歴の途中は比べない。"""
    if old.code != -signal.SIGINT or new.code != old.code or old.stdout != new.stdout:
        return False
    parts = []
    for side in (old, new):
        before, found, after = side.stderr.partition(TRACEBACK)
        if not found or not after.endswith(b'KeyboardInterrupt\n') or \
                (after != b'KeyboardInterrupt\n' and not after.endswith(b'\nKeyboardInterrupt\n')):
            return False
        parts.append(before)
    return parts[0] == parts[1]


def blocked_in_write(pid, fifo):
    """write で、詰めた FIFO を指す fd(1 か 2。bash の `>&2` は fd 2 を fd 1 へ写して書く)に止まった。"""
    try:
        wchan = Path(f'/proc/{pid}/wchan').read_text().strip()
        fields = Path(f'/proc/{pid}/syscall').read_text().split()
        if wchan != 'pipe_write' or fields[:1] != ['1'] or len(fields) < 2:
            return False
        fd = int(fields[1], 16)
        return fd in (1, 2) and os.stat(f'/proc/{pid}/fd/{fd}').st_ino == os.stat(fifo).st_ino
    except (OSError, ValueError):
        return False


def run_fifo_side(world, base, argv, stream, number, legacy, ignored):
    path = base / f'fifo-{"old" if legacy else "new"}'
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    filler = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    filled = 0
    try:
        while True:
            filled += os.write(filler, b'x' * 65536)
    except BlockingIOError:
        pass
    writer = os.open(path, os.O_WRONLY)
    os.close(filler)
    drained = bytearray()
    state = {'blocked': False}

    def during(process):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if blocked_in_write(process.pid, path):
                state['blocked'] = True
                break
            if process.poll() is not None:
                break
            time.sleep(0.01)
        if state['blocked']:
            os.kill(process.pid, number)
            time.sleep(0.2)
        # 読み出す(子が終わるまで)。
        os.set_blocking(reader, True)
        while True:
            chunk = os.read(reader, 65536)
            if not chunk:
                break
            drained.extend(chunk)

    env = default_env(world) if world is not None else default_env(None)
    program, argv0, prefix = old_command() if legacy else rust_command()
    preexec = ignore_signals(signal.SIGTERM, signal.SIGINT, signal.SIGHUP) if ignored else None
    out_target = writer if stream == 'stdout' else subprocess.PIPE
    err_target = writer if stream == 'stderr' else subprocess.PIPE
    command = [str(TOOLS.launcher), *env, '--', str(program), argv0, *encode_all(prefix + list(argv(world)))]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out_target, stderr=err_target,
                               cwd=world.repo if world is not None else base, preexec_fn=preexec)
    os.close(writer)
    import threading
    thread = threading.Thread(target=during, args=(process,), daemon=True)
    thread.start()
    try:
        out, err = process.communicate(timeout=60)
        timed_out = False
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        timed_out = True
    thread.join(10)
    os.close(reader)
    data = bytes(drained[filled:])
    if stream == 'stdout':
        out = data
    else:
        err = data
    result = normalize(Result(process.returncode, out or b'', err or b'', timed_out, 0), legacy)
    return result, state['blocked']


def run_fifo(root):
    problems = []
    count = 0
    for label, stream, build, argv in FIFO_SOURCES:
        for name, number in (('TERM', signal.SIGTERM), ('INT', signal.SIGINT), ('HUP', signal.SIGHUP)):
            for ignored in (False, True):
                base = Path(root) / f'fifo-{count}'
                base.mkdir(parents=True, exist_ok=True)
                try:
                    world = build(base / 'w') if build is not None else None
                    template = base / 'template'
                    if world is not None:
                        copy_world(world.base, template)
                    old, old_blocked = run_fifo_side(world, base, argv, stream, number, True, ignored)
                    old_files = observe_files(world, Case('', '', None))
                    if world is not None:
                        copy_world(template, world.base)
                        fix_mark(world)
                    new, new_blocked = run_fifo_side(world, base, argv, stream, number, False, ignored)
                    new_files = observe_files(world, Case('', '', None))
                    tag = f'FIFO {label} {name}' + (' 無視した起動' if ignored else '')
                    if not old_blocked or not new_blocked:
                        problems.append(f'{tag}: 期限内に書き込みで止まったと確かめられない(成功に数えない)')
                    elif old.timed_out or new.timed_out:
                        problems.append(f'{tag}: 時間切れ')
                    elif label in KEYBOARD_SOURCES and number == signal.SIGINT and not ignored:
                        if not keyboard_equal(old, new) or old_files != new_files:
                            problems.append(f'{tag}: KeyboardInterrupt の比べ方で違う: '
                                            f'{difference(dataclasses.asdict(old), dataclasses.asdict(new))}')
                    elif (old.code, old.stdout, old.stderr, old_files) != (new.code, new.stdout, new.stderr, new_files):
                        problems.append(f'{tag}: {difference(dataclasses.asdict(old), dataclasses.asdict(new))}')
                    count += 1
                except Exception as error:  # noqa: BLE001
                    problems.append(f'FIFO {label} {name}: {type(error).__name__}: {error}')
                finally:
                    make_writable(base)
                    shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: 詰めた FIFO の信号: {count} 件', flush=True)
    return problems


# ---- 本体 ----

GROUPS = (
    ('引数と使い方', group_arguments),
    ('保護領域の確かめ', group_state),
    ('記録の読み取り', group_meta),
    ('taskmd-diff', group_taskmd_diff),
    ('restore-taskmd', group_restore),
    ('cleanup', group_cleanup),
    ('出力の故障', group_output),
    ('起動の状態', group_startup),
    ('旧版の記録の相互読み', group_cross_read),
    ('Git と道具の不起動', group_git_and_tools),
)
# Mac で旧版と比べる群(BSD の道具のまま成功の経路が動く群)と、署名後の小さな範囲。
MAC_GROUPS = ('引数と使い方', '保護領域の確かめ', '記録の読み取り', 'taskmd-diff', 'restore-taskmd', 'cleanup')
# restore-taskmd のうち範囲 mac で比べる停止のケース(R4R の Mac の停止と同じ。bash の部分か cp の確かめで止まる)。
MAC_RESTORE_LABELS = ('T 行の', '本物の cp')
SIGNED_LABELS = ('引数なし', 'taskmd-diff 無い', 'same')

TOOLS = None


def work_root():
    """比較の配置の置き場。保護領域は /tmp・$TMPDIR の外でなければ作れないので、HOME の下に作る。"""
    home = Path.home()
    root = Path(tempfile.mkdtemp(prefix='s3ia-check-', dir=str(home)))
    real = os.path.realpath(root)
    for excluded in ('/tmp', os.environ.get('TMPDIR', '')):
        if excluded and (real == os.path.realpath(excluded) or real.startswith(os.path.realpath(excluded) + '/')):
            shutil.rmtree(root)
            raise AssertionError('HOME が /tmp か TMPDIR の下にあるので、保護領域を作れない')
    return root


# 範囲 mac で比べないケース(テスト計画の「Mac」の「未実施と記録する群」)。Mac の実機では未確認の推測を含む。
MAC_TOOL_TEXT = 'BSD の道具の文が GNU の道具の文と違う(GNU の道具の文が出るケース)'
MAC_SKIP_LABELS = {
    'mode 1700': 'BSD の stat -f %Lp は特別な権限の桁の出し方が GNU の stat -c %a と違う(未確認の推測)',
    'snapshot の中の読めないファイル': MAC_TOOL_TEXT,
    '中に書けないフォルダ': MAC_TOOL_TEXT,
    '中に読めないフォルダ': MAC_TOOL_TEXT,
    '中に空の読めないフォルダ': MAC_TOOL_TEXT,
    '中に検索できないフォルダ(mode 400)': MAC_TOOL_TEXT,
    '中に検索できないフォルダ(mode 600)': MAC_TOOL_TEXT,
    'NUL を含む印': 'GNU grep 3.11 は NUL を行の区切りとして扱う。BSD grep では結果が違うと推測される(未確認)',
    '読めない印': MAC_TOOL_TEXT,
    '印の置き場に書けない': MAC_TOOL_TEXT,
}
MAC_RESTORE_STOP = 'テスト計画の「Mac」は restore-taskmd の停止だけを比べる(子の環境と cp の観測器は比べない)'


def mac_skip_reason(group, case):
    """範囲 mac で比べない理由(比べるなら空)。"""
    if case.mac_skip:
        return case.mac_skip
    if 'full' in (case.stdout, case.stderr):
        return '/dev/full が無い'
    if 'UTF-8 でない名前' in case.label:
        return 'APFS は UTF-8 でない名前を作れない'
    for label, reason in MAC_SKIP_LABELS.items():
        if case.label == label or case.label.endswith(' ' + label):
            return reason
    if group == 'restore-taskmd' and not case.label.startswith(MAC_RESTORE_LABELS):
        return MAC_RESTORE_STOP
    return ''


def selected_cases(scope, names=(), skipped=None):
    """範囲の群とケース。skipped(dict)を渡すと、範囲 mac で外したケースの数を理由ごとに足す。"""
    for name, factory in GROUPS:
        if names and name not in names:
            continue
        if scope == 'mac' and name not in MAC_GROUPS:
            continue
        cases = factory()
        if scope == 'mac':
            kept = []
            for case in cases:
                reason = mac_skip_reason(name, case)
                if reason:
                    if skipped is not None:
                        skipped[(name, reason)] = skipped.get((name, reason), 0) + 1
                else:
                    kept.append(case)
            cases = kept
        if scope == 'signed':
            cases = [c for c in cases if c.label in SIGNED_LABELS or c.label.endswith(SIGNED_LABELS)][:3]
            if not cases:
                continue
        yield name, cases


# ケースの一覧(範囲ごとに、群ごとの件数とラベルの一覧の sha256)。設計 Task 4.1 の注意事項。
CASE_LISTS = FIXTURES / 'case_lists.json'
CASE_LISTS_FIXED = (19, '3c19ac2de1794a6ed275d706a281b1ab75376bf5938aaad422d6527a7781cb45')   # (範囲と群の組の数, sha256)


def case_lists():
    data = {}
    for scope in ('full', 'mac', 'signed'):
        data[scope] = {}
        for name, cases in selected_cases(scope):
            labels = '\n'.join(case.label for case in cases).encode()
            data[scope][name] = {'count': len(cases), 'labels_sha256': hashlib.sha256(labels).hexdigest()}
    return data


def check_case_lists():
    """今のケースの一覧が、固定した一覧(件数と sha256)と同じこと。"""
    if not CASE_LISTS.exists():
        return ['ケースの一覧(case_lists.json)が無い']
    raw = CASE_LISTS.read_bytes()
    fixed_lists = json.loads(raw)
    pairs = sum(len(groups) for groups in fixed_lists.values())
    if (pairs, hashlib.sha256(raw).hexdigest()) != CASE_LISTS_FIXED:
        return ['ケースの一覧(case_lists.json)の件数か sha256 が固定と違う']
    now = case_lists()
    problems = []
    for scope in sorted(set(now) | set(fixed_lists)):
        for name in sorted(set(now.get(scope, {})) | set(fixed_lists.get(scope, {}))):
            if now.get(scope, {}).get(name) != fixed_lists.get(scope, {}).get(name):
                problems.append(f'ケースの一覧が固定と違う(範囲 {scope}・群 {name}): '
                                f'今 {now.get(scope, {}).get(name)} / 固定 {fixed_lists.get(scope, {}).get(name)}')
    return problems


def run_groups(scope, names, root, results, skipped=None):
    for name, cases in selected_cases(scope, names, skipped):
        started = time.monotonic()
        outcome = Outcome()
        for case in cases:
            run_case(case, root, outcome)
        outcome.seconds[name] = time.monotonic() - started
        results[name] = outcome
        print(f'S3I-A: 群 {name}: 比べた {outcome.compared} 件・境界 {outcome.boundary} 件・'
              f'不一致 {len(outcome.failures)} 件・{outcome.seconds[name]:.1f} 秒', flush=True)
        for label, records in outcome.not_reproduced:
            print(f'S3I-A: 決定 4 の名前の記録: {label}: {describe_not_reproduced(records)}', flush=True)
        for failure in outcome.failures:
            print(f'S3I-A: 不一致: {failure}', file=sys.stderr, flush=True)


MAC_FIXED = FIXTURES / 'mac_fixed.json'
MAC_FIXED_CASES = ('引数なし', '不明なサブコマンド', '-h', 'taskmd-diff 必要な名前が無い', 'cleanup 必要な名前が無い',
                   'taskmd-diff 無い', 'restore-taskmd 無い', 'cleanup 無い', 'taskmd-diff マニフェストのダイジェストの違い',
                   'same', 'checks-only -', 'ほかの変更', '成功(印あり)')


def mac_cases():
    found = []
    for _, factory in GROUPS:
        for case in factory():
            if case.label in MAC_FIXED_CASES and not any(c.label == case.label for c in found):
                found.append(case)
    return found


def fixed_record(world, side, base):
    def place(data):
        text = data.decode('utf-8', 'surrogateescape')
        return text.replace(str(base), '<base>')
    return {'code': side.result.code, 'stdout': place(side.result.stdout), 'stderr': place(side.result.stderr)}


def write_mac_fixed(root, path):
    """Linux の基準の環境で旧版を流した結果を、Mac の固定期待として書く(パスは <base> に置き換える)。"""
    records = {}
    for index, case in enumerate(mac_cases()):
        base = Path(root) / f'mac-{index}'
        base.mkdir(parents=True)
        world = case.build(base / 'w') if case.build is not None else None
        side = run_side(case, base, world, True)
        records[case.label] = fixed_record(world, side, base)
    Path(path).write_text(json.dumps(records, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return len(records)


def run_mac_fixed(root):
    """旧版を流せない Mac: Rust の結果を、Linux で測った旧版の固定期待と比べる(配置の記録は、旧版の take と
    同じ形のものを Python で書く。take_record)。"""
    fixed = json.loads(MAC_FIXED.read_text(encoding='utf-8'))
    problems = []
    count = 0
    for index, case in enumerate(mac_cases()):
        base = Path(root) / f'mac-{index}'
        base.mkdir(parents=True)
        try:
            world = case.build(base / 'w') if case.build is not None else None
            side = run_side(case, base, world, False)
            if fixed_record(world, side, base) != fixed[case.label]:
                problems.append(f'Mac の固定期待と違う: {case.label}')
            count += 1
        except Exception as error:  # noqa: BLE001
            problems.append(f'Mac の固定期待 {case.label}: {type(error).__name__}: {error}')
    return count, problems


RECORD_EXTRA_LABELS = ('.. を含む候補', 'HOME の候補', 'HOME の .cache')   # XDG_STATE_HOME の外の記録の置き場


def record_cases():
    """Python で書く記録を確かめるケース: Mac の固定期待のうち配置の要るもの(8 件)と、保護領域の置き場を
    make_world の外で決める taskmd-diff・cleanup の 6 件。"""
    found = [case for case in mac_cases() if case.build is not None]
    for case in group_state():
        if case.label.split(' ', 1)[1] in RECORD_EXTRA_LABELS and case.label.split(' ', 1)[0] in ('taskmd-diff',
                                                                                                  'cleanup'):
            found.append(case)
    return found


def run_record_writer(root):
    """範囲 full(旧版を流せる Linux): bash 4.0 以上が無いときに Python で書く記録が、旧版の take の記録と同じこと。
    1. 同じ配置で旧版の take の後に Python で書き直し、記録の中のファイルの名前・種類・mode・リンクの数・中身と
       所属の印(guard の番号は、それぞれの記録のフォルダの番号と同じことを確かめて外す)、take の stdout と
       stderr を比べる(take_record の RECORD_CHECK)。
    2. 旧版の take で作った配置で流した旧版の結果と、Python の記録で流した旧版と Rust の結果(終了値・stdout・
       stderr。<base> に置き換える)が同じこと。Mac の固定期待のケースは、その固定期待とも同じこと。Python の
       記録での旧版と Rust は、群と同じ比べ方(compare_sides)でも比べる。"""
    global RECORD_CHECK
    if BASH is None:
        return ['Python で書く記録の確かめ: bash 4.0 以上が無い(範囲 full で流す)']
    fixed = json.loads(MAC_FIXED.read_text(encoding='utf-8'))
    problems = []
    cases = record_cases()
    for index, case in enumerate(cases):
        name = f'{case.group}/{case.label}'
        take_base = Path(root) / f'record-{index}-take'
        py_base = Path(root) / f'record-{index}-py'
        try:
            take_base.mkdir(parents=True)
            take_world = case.build(take_base / 'w')
            take_old = fixed_record(take_world, run_side(case, take_base, take_world, True), take_base)
            py_base.mkdir(parents=True)
            RECORD_CHECK = []
            try:
                world = case.build(py_base / 'w')
                found = RECORD_CHECK
            finally:
                RECORD_CHECK = None
            problems += [f'Python で書く記録 {name}: {item}' for item in found]
            template = py_base / 'template'
            copy_world(world.base, template)
            old = run_side(case, py_base, world, True)
            copy_world(template, world.base)
            fix_mark(world)
            new = run_side(case, py_base, world, False)
            py_old = fixed_record(world, old, py_base)
            py_new = fixed_record(world, new, py_base)
            if py_old != take_old:
                problems.append(f'Python で書く記録 {name}: 旧版の結果が、旧版の take の記録のときと違う: '
                                f'{clip(repr(take_old))} / {clip(repr(py_old))}')
            if py_new != take_old:
                problems.append(f'Python で書く記録 {name}: Rust の結果が、旧版の take の記録での旧版と違う: '
                                f'{clip(repr(take_old))} / {clip(repr(py_new))}')
            if case.label in fixed and py_new != fixed[case.label]:
                problems.append(f'Python で書く記録 {name}: Rust の結果が Mac の固定期待と違う')
            compare_sides(case, old, new, py_base)
        except Exception as error:  # noqa: BLE001 - 1 件の失敗で残りのケースを止めない。
            problems.append(f'Python で書く記録 {name}: {type(error).__name__}: {error}')
        finally:
            for base in (take_base, py_base):
                if base.exists():
                    make_writable(base)
                    shutil.rmtree(base, ignore_errors=True)
    print(f'S3I-A: Python で書く記録: {len(cases)} 件の配置で、旧版の take の記録と比べた', flush=True)
    return problems


def run_self_tests(unperformed):
    """比較器自身の検査(test_implement_guard.py)。どの範囲でも流す。その OS で成り立たない件は、unittest の
    skip にして、名前と理由を未実施として記録する。"""
    module = load(Path(__file__).with_name('test_implement_guard.py'), 'implement_guard_comparator_tests')
    module.subject = sys.modules[__name__]
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    for test, reason in result.skipped:
        unperformed.append(f'比較器の自己検査 {test.id().rsplit(".", 1)[-1]}: {reason}')
    ran = result.testsRun - len(result.skipped)
    if not result.wasSuccessful() or ran == 0:
        raise AssertionError(f'比較器の自己検査が通らない(流した {result.testsRun} 件・省いた {len(result.skipped)} 件)')
    print(f'S3I-A: 比較器の自己検査: {ran} 件(省いた {len(result.skipped)} 件)', flush=True)


def check_errno_table(table=ERRNO_GLIBC):
    """Linux: 生成物の strerror の表が、この機械の glibc の strerror と同じこと(LC_ALL=C・LANGUAGE なし。
    gen_strerror.py --check)。Mac は Rust の単体テストが、全番号と 1000 を os.strerror と照らす。"""
    if not LINUX:
        return []
    env = {'LC_ALL': 'C', 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    done = subprocess.run([sys.executable, '-B', str(FIXTURES / 'gen_strerror.py'), '--check', str(table)],
                          capture_output=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        return ['errno の文の表がこの機械の glibc の strerror と違う: '
                + done.stderr.decode(errors='replace').strip()[:300]]
    return []


def main(argv=None):
    global FIXTURE, TOOLS
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--scope', choices=('full', 'mac', 'signed'))
    parser.add_argument('--groups', help='開発用: 指定した群だけを流す(合格に数えない。終了値 3)')
    parser.add_argument('--dump-selftest', help='開発用: 許す失敗の一覧の下書きを書く(合格に数えない)')
    parser.add_argument('--write-mac-fixed', help='開発用: Linux の旧版の結果を Mac の固定期待として書く')
    parser.add_argument('--write-closed-fds', help='開発用: fd を閉じた起動の旧版と Rust の固定期待を書く')
    parser.add_argument('--write-case-lists', help='開発用: 範囲ごとのケースの一覧(件数と sha256)を書く')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('S3I-A の比較は Python 3.10 系・root 以外で実行してください')
    global BASH
    FIXTURE = fixture_path(args.fixture)
    scope = args.scope or ('full' if LINUX else 'mac')
    started = time.monotonic()
    BASH = find_bash4()
    check_sources()
    excluded_env()
    missing = baseline()
    if missing and scope == 'full':
        raise AssertionError(f'基準の版と違う道具がある(版に依る欄を比べられない): {missing}')
    problems = check_measured_fixtures() + check_errno_table()
    unperformed = []
    if not args.groups and not args.dump_selftest and not args.write_case_lists:
        problems += check_case_lists()
        stage_started = time.monotonic()
        try:
            run_self_tests(unperformed)
        except AssertionError as error:
            problems.append(str(error))
        print(f'S3I-A: 時間: 比較器の自己検査 {time.monotonic() - stage_started:.1f} 秒', flush=True)
    root = work_root()
    TOOLS = Tools(root / 'tools')
    results = {}
    skipped = {}
    fixed_count = 0
    try:
        if args.write_mac_fixed:
            print(f'S3I-A: Mac の固定期待: {write_mac_fixed(root, args.write_mac_fixed)} 件')
            return 3
        if args.write_case_lists:
            data = case_lists()
            Path(args.write_case_lists).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                                                   encoding='utf-8')
            print(f'S3I-A: ケースの一覧: {sum(len(g) for g in data.values())} 組')
            return 3
        if args.write_closed_fds:
            print(f'S3I-A: fd を閉じた起動の固定期待: {write_closed_fds(root, args.write_closed_fds)} 件')
            return 3
        if BASH is None:
            # bash 4.0 以上が無い(Mac): 旧版との比較の全部を未実施にし、固定期待と比べる(配置の記録は Python で書く)。
            unperformed.append('旧版との比較の全部: bash 4.0 以上が無い')
            fixed_count, found = run_mac_fixed(root)
            problems += found
        if args.dump_selftest:
            found, (old, rust) = selftest_stage(root)
            dump_selftest(old, rust, args.dump_selftest)
            for problem in found:
                print(f'S3I-A: 下書きの時点の問題: {problem}', flush=True)
            return 3
        if BASH is not None:
            names = set(args.groups.split(',')) if args.groups else set()
            run_groups(scope, names, root, results, skipped)
        if not args.groups and scope == 'full':
            for label, stage in (('決定 D5', run_d5), ('PATH を絞った Rust', run_restricted_path),
                                 ('詰めた FIFO の信号', run_fifo), ('Python で書く記録', run_record_writer)):
                stage_started = time.monotonic()
                problems += stage(root)
                print(f'S3I-A: 時間: {label} {time.monotonic() - stage_started:.1f} 秒', flush=True)
            found, missing_reason = run_strace(root)
            problems += found
            if missing_reason:
                unperformed.append(f'strace による exec の確かめ: {missing_reason}')
            stage_started = time.monotonic()
            found, missing_reason = run_devnull(root)
            problems += found
            if missing_reason:
                unperformed.append(f'/dev/null を開けない起動: {missing_reason}')
            print(f'S3I-A: 時間: /dev/null を開けない起動 {time.monotonic() - stage_started:.1f} 秒', flush=True)
            stage_started = time.monotonic()
            problems += run_closed_fds(root)
            print(f'S3I-A: 時間: fd を閉じた起動 {time.monotonic() - stage_started:.1f} 秒', flush=True)
            stage_started = time.monotonic()
            found, _ = selftest_stage(root)
            problems += found
            print(f'S3I-A: 時間: 旧 selftest {time.monotonic() - stage_started:.1f} 秒', flush=True)
            # 旧版の測定の照らし(implement_guard_measure.py --check)は、手元の基準の容器で別に流す(CI の時間)。
            problems += check_expected_old()
    finally:
        make_writable(root)
        shutil.rmtree(root, ignore_errors=True)
    failures = sum(len(o.failures) for o in results.values())
    compared = sum(o.compared for o in results.values())
    boundary = sum(o.boundary for o in results.values())
    if scope == 'mac':
        outside = [name for name, _ in GROUPS if name not in MAC_GROUPS]
        unperformed.append(f'群 {"・".join(outside)}: 範囲 mac の外(テスト計画の「Mac」)')
        unperformed.append('版に依る欄・strace と LD_PRELOAD・詰めた FIFO の信号・/dev/null を開けない起動・'
                           'fd を閉じた起動・決定 D5・PATH を絞った Rust・旧 selftest: 範囲 mac の外(テスト計画の「Mac」)')
        for (group, reason), count in sorted(skipped.items()):
            unperformed.append(f'群 {group} の {count} 件: {reason}')
    if scope == 'signed':
        unperformed.append('範囲 signed は、署名後の実行物が動くことの確かめだけ(群の 3 件と比較器の自己検査)')
    for problem in problems:
        print(f'S3I-A: 不合格: {problem}', file=sys.stderr, flush=True)
    for item in unperformed:
        print(f'S3I-A: 未実施: {item}', flush=True)
    print(f'S3I-A: 結果({SYSTEM}・{scope}): 旧版と比べた {compared} 件・固定期待で確かめた {fixed_count} 件・境界 {boundary} 件・'
          f'未実施 {len(unperformed)} 項目・'
          f'不一致 {failures} 件・問題 {len(problems)} 件・{time.monotonic() - started:.1f} 秒', flush=True)
    if args.groups or args.dump_selftest:
        print('S3I-A: 部分実行(合格に数えない)')
        return 3
    judge_totals(compared + fixed_count, failures, problems)
    print('S3I-A: 合格')
    return 0


def judge_totals(compared, failures, problems):
    """比べた件数が 0・不一致・問題があれば不合格。"""
    if failures or problems or compared == 0:
        raise AssertionError(f'不一致 {failures} 件・問題 {len(problems)} 件・比べた {compared} 件')


EXPECTED_OLD = FIXTURES / 'expected_old.json'
EXPECTED_OLD_FIXED = (201, '6b00d1581dbfc8bcdaa9b173583ac95f8de731e4e7cc84efd9403203c6450eb4')   # (信号の地点の記録の数, sha256)


# 単体テストと Mac の範囲が読む、測った表と固定期待(件数・sha256)。件数は、JSON は項目の数、表は # で
# 始まらない行の数。
MEASURED_FIXED = {
    'mac_fixed.json': (13, '83325ddf21d263e18a9e0805de89f019ff098d67fbcd5222efd835c58793f035'),
    'cunquote_printf.tsv': (13692, '20d8dafc29321257f7977c39d01afb73ea0e6a726bbabdd8485257debd26bdff'),
    'quoting.tsv': (285, 'c39ad84404eddee7ad24c690ab6e3c17073112c00a431221a684d94258b96188'),
}


def check_measured_fixtures():
    """測った表と Mac の固定期待の件数と sha256 が、固定と同じこと。"""
    problems = []
    for name, expected in MEASURED_FIXED.items():
        data = (FIXTURES / name).read_bytes()
        if name.endswith('.json'):
            count = len(json.loads(data))
        else:
            count = sum(1 for line in data.split(b'\n') if line and not line.startswith(b'#'))
        if (count, hashlib.sha256(data).hexdigest()) != expected:
            problems.append(f'{name} の件数か sha256 が固定と違う')
    return problems


def check_expected_old():
    """旧版の測定の固定期待(Rust の単体テストが読む)の件数と sha256。測り直しの照らしは
    implement_guard_measure.py --check で、手元の基準の容器で流す(CI では流さない)。"""
    if not EXPECTED_OLD.exists():
        return ['旧版の固定期待(expected_old.json)が無い']
    data = EXPECTED_OLD.read_bytes()
    count = len(json.loads(data)['signals'])
    if (count, hashlib.sha256(data).hexdigest()) != EXPECTED_OLD_FIXED:
        return ['旧版の固定期待(expected_old.json)の件数か sha256 が固定と違う']
    return []


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, Mismatch, ValueError, OSError) as error:
        print(f'S3I-A: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
