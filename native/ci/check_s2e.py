#!/usr/bin/env python3
"""S2E 専用比較(#470)。旧 environment-guard.py を変えず、同じ物理入力を旧版と Rust で順に流して比べる。

- 旧版は、比較用の配布物の `skills/ship-task/scripts/environment-guard.py` に置いた旧 guard から
  `python3 -B` で直接起動する(U5 の基準の起動)。Rust は `--fixture` の実行物。
- 生の標準出力・標準エラー・終了コードを、加工せずに比べる。加工は「認めた差」の関数に限る:
  U4(traceback の途中の行)・旧版の実行環境のパス・Current thread の値・U7(閉じた標準の fd)。
  認めた差は通常の一致の件数に混ぜず、旧新それぞれの固定期待を別に確かめる。
- 未起動・時間切れ・件数 0 は合格にしない。本体がまだ無い経路は「未実装の印」で件数から外し、一覧に出す。
- 既存の 117 テストの対応表は `TEST_MAPPING`(と `native/ci/s2e_fixtures/test_mapping.md`)。
- 9 コマンドのうち bootstrap・verify・host-settings・host-allowlist と内部 reader の旧新比較(同じ物理入力を実際に順に流す。bootstrap の出力一式の
  バイト比較・読取りだけの前後確認・相互読取り・競合・上限の端(旧版で測る)・SIGINT・書込みの失敗・U1・U2・U6)は `s2e_commands.py`。
  各項目に「全機種で必須」か「Linux だけ」の印(と理由)を付け、機種ごとの項目数を完全一致で確かめる。どの項目も Rust を起動した比較が 1 件以上
  ないと不合格。Mac の経路(旧版の bootstrap は patched・Linux だけの項目は流さない)は、環境変数 S2E_SIMULATE_MAC=1 で Linux から模擬できる
  (比較器の検査用。Mac 実機の動きの確認ではない。模擬の合格は終了コード 3 で、最後の行も「S2E: 合格」ではない)。read・exec・hook・verify-pair は 2 段目 b-2。
"""
import argparse
import dataclasses
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import RunResult  # noqa: E402

GUARD = ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/environment-guard.py'
GUARD_SHA256 = '5770210377a1c5ef79effbcbf8f5f0678946f14ad9f75143fd1c219daf64d796'
EXISTING_TESTS = ROOT / 'scripts/test_environment_guard.py'
MAPPING_MD = Path(__file__).resolve().parent / 's2e_fixtures/test_mapping.md'
FIXTURE = None
OLD_TIMEOUT = 30
COMMANDS = ('bootstrap', 'verify', 'exec', 'read', 'hook', 'verify-pair', 'host-settings', 'host-allowlist', 'host-paths')


def fixture_path(value):
    if not value or not Path(value).is_absolute():
        raise ValueError('検査入口は絶対パスで指定してください')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('検査入口は実行可能な通常ファイルである必要があります')
    return path


# ---------------------------------------------------------------- 実行

def run_process(argv, *, env, cwd, redirects=None, stdin=None, timeout=OLD_TIMEOUT):
    """別プロセスグループで argv を起動する。`redirects` は {fd: モード}。

    モード: 'close'(閉じる)・'dir'(ディレクトリを読み取りで開く)・'full'(/dev/full へ)・
    'pipe-closed'(読み手が閉じたパイプへ。fd 1 と 2 だけ)。起動できなければ OSError をそのまま投げる。
    """
    redirects = dict(redirects or {})
    targets = {1: subprocess.PIPE, 2: subprocess.PIPE}
    handles = []
    shell = []
    for fd, mode in sorted(redirects.items()):
        if mode == 'close':
            shell.append(f'{fd}<&-' if fd == 0 else f'{fd}>&-')
        elif mode == 'dir':
            shell.append(f'{fd}</')
        elif mode == 'full':
            shell.append(f'{fd}>/dev/full')
        elif mode == 'pipe-closed':
            read_end, write_end = os.pipe()
            os.close(read_end)
            targets[fd] = write_end
            handles.append(write_end)
        else:
            raise ValueError('未知のモード: ' + mode)
    command = list(argv)
    if shell:
        command = ['/bin/sh', '-c', 'exec "$0" "$@" ' + ' '.join(shell)] + command
    try:
        with subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True,
                              stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              stdout=targets[1], stderr=targets[2]) as process:
            try:
                timed_out = False
                try:
                    out, err = process.communicate(input=stdin, timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    for number in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(process.pid, number)
                        except ProcessLookupError:
                            pass
                        try:
                            process.communicate(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    out, err = process.communicate()
                return RunResult(process.returncode, out or b'', err or b'', timed_out)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
    finally:
        for handle in handles:
            os.close(handle)


def stage_distribution(base):
    """旧 guard を、比較用の配布物の固定の位置へ置く(旧 guard の SHA-256 を確かめる)。"""
    if hashlib.sha256(GUARD.read_bytes()).hexdigest() != GUARD_SHA256:
        raise AssertionError('旧 guard の SHA-256 が違う')
    target = base / 'dist/skills/ship-task/scripts/environment-guard.py'
    target.parent.mkdir(parents=True)
    shutil.copyfile(GUARD, target)
    target.chmod(0o644)
    return target


# ---------------------------------------------------------------- 比較(加工しない)

def same_result(old, new):
    if old.timed_out or new.timed_out:
        raise AssertionError('比較対象が時間切れになった')
    if (old.returncode, old.stdout, old.stderr) != (new.returncode, new.stdout, new.stderr):
        raise AssertionError('終了値または出力が一致しない')


def fixed(result, code, stdout=b'', stderr=b''):
    if result.timed_out or (result.returncode, result.stdout, result.stderr) != (code, stdout, stderr):
        raise AssertionError('固定期待と一致しない')


def fixed_pair(old, new, old_expected, new_expected):
    """認めた差: 旧新それぞれの固定期待を別に確かめる。"""
    fixed(old, *old_expected)
    fixed(new, *new_expected)


# ---------------------------------------------------------------- 認めた差の関数(加工はここだけ)

def strip_traceback_middle(stderr):
    """U4: `Traceback (most recent call last):` の見出しと、そのブロックの最後の行の間の、
    字下げした行(ファイルの所在とソースの行)だけを除く。最後の行・因果の区切り・出力の順は除かない。"""
    out = []
    in_block = False
    for line in stderr.split(b'\n'):
        if line.startswith(b'Traceback (most recent call last):'):
            in_block = True
            out.append(line)
        elif in_block and line.startswith(b'  '):
            continue
        else:
            in_block = False
            out.append(line)
    return b'\n'.join(out)


def fixed_old_paths(stderr, *, script, stdlib):
    """旧版の実行環境のパス(旧スクリプト自身のパスと、旧版を動かす Python の標準ライブラリの導入先)を、
    固定のファイル名へ置き換える。利用者が渡したパス・作業場所のパス・ほかの値には広げない。"""
    # 前が行頭・空白・引用符のときだけ置き換える(利用者のパスの途中に同じ並びがあっても残す)。
    head = rb'(?<![^\s"\'])'
    text = re.sub(head + re.escape(str(script).encode()), b'environment-guard.py', stderr)
    return re.sub(head + re.escape(str(stdlib).encode()) + rb'/([A-Za-z0-9_./]+\.py)', lambda m: m.group(1).rsplit(b'/', 1)[-1], text)


def approved_u4(old, new, *, script, stdlib):
    """U4: 終了コードと標準出力は旧版どおり。標準エラーは、旧版の traceback の途中の行と旧版の実行環境の
    パスだけを除いた物が、Rust の出力と同じ。"""
    if old.timed_out or new.timed_out:
        raise AssertionError('比較対象が時間切れになった')
    if old.returncode != new.returncode or old.stdout != new.stdout:
        raise AssertionError('U4: 終了値または標準出力が違う')
    expected = fixed_old_paths(strip_traceback_middle(old.stderr), script=script, stdlib=stdlib)
    if expected != new.stderr:
        raise AssertionError('U4: 標準エラーが旧版の最後の行・因果の区切りと一致しない')


CURRENT_THREAD = re.compile(rb'Current thread 0x[0-9a-f]+ \(most recent call first\):')
FATAL_SHAPE = re.compile(
    rb'\AFatal Python error: [^\n]+\nPython runtime state: [^\n]+\n(?:[A-Za-z]+Error: [^\n]*\n)?\n'
    rb'Current thread 0x[0-9a-f]+ \(most recent call first\):\n  <no Python frame>\n\Z')


def normalize_current_thread(stderr):
    """Current thread の値(実行ごとに変わる)だけを固定の印へ置き換える。出る位置と形式は別に確かめる。"""
    return CURRENT_THREAD.sub(b'Current thread 0x<THREAD> (most recent call first):', stderr)


def fatal_shape_ok(stderr):
    return FATAL_SHAPE.match(stderr) is not None


def approved_fatal(old, new):
    """起動時に落ちる経路: 終了値・標準出力・診断の全文は旧版どおり。Current thread の値だけ一致を求めず、
    出る位置と形式を検査する。"""
    if old.timed_out or new.timed_out:
        raise AssertionError('比較対象が時間切れになった')
    if not fatal_shape_ok(old.stderr) or not fatal_shape_ok(new.stderr):
        raise AssertionError('Fatal 診断の形式が違う')
    if (old.returncode, old.stdout) != (new.returncode, new.stdout):
        raise AssertionError('Fatal: 終了値または標準出力が違う')
    if normalize_current_thread(old.stderr) != normalize_current_thread(new.stderr):
        raise AssertionError('Fatal: 診断の本文が違う')


# ---------------------------------------------------------------- ケース

@dataclasses.dataclass
class Case:
    group: str
    name: str
    args: list
    env: dict = dataclasses.field(default_factory=dict)
    redirects: dict = dataclasses.field(default_factory=dict)
    kind: str = 'same'            # same・parse(解析だけで終わる)・u4auto(出力の故障。traceback なら U4)・u7・fatal・fatal-silent(fd 2 がディレクトリ)
    u7: object = None             # 旧版の通常の実行から、旧新の固定期待を作る関数


def base_env(extra=None):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C.UTF-8', 'HOME': '/nonexistent-s2e-home'}
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


REQUIRED = {
    'bootstrap': ['--root', '--output'],
    'verify': ['--state', '--expect-sha256'],
    'exec': ['--state', '--expect-sha256', '--path'],
    'read': ['--state', '--expect-sha256', '--path'],
    'hook': ['--state', '--expect-sha256', '--path'],
    'verify-pair': ['--state', '--expect-sha256', '--current-inventory', '--guard-sha256', '--go-timeout'],
    'host-settings': ['--state', '--expect-sha256'],
    'host-allowlist': ['--state', '--expect-sha256'],
    'host-paths': [],
}
VALUES = {'--go-timeout': '5'}
COLUMNS = (None, '', '1', '5', '11', '20', '30', '40', '60', '79', '80', '81', '120', '1000', 'abc', '0', '-5', ' 70 ', '٧٠')


def argument_cases():
    cases = []

    def add(name, args, env=None, kind='parse'):
        cases.append(Case('args', name, args, base_env(env), kind=kind))

    for columns in COLUMNS:
        env = {'COLUMNS': columns}
        for command in (None, *COMMANDS):
            tail = [command] if command else []
            add(f'help/{command or "top"}/columns={columns!r}', tail + ['-h'], env)
    top = [[], ['bogus'], ['--'], ['-x'], ['--he'], ['-hx'], ['-hh'], ['-hfoo'], ['--help=x'], ['--', 'verify'], ['verify', '--'], ['-h', 'bogus'],
           ['--bogus', 'verify'], ['verify\udcff'], ['é'], ['-5'], ['-1.5'], ['Verify'], ['verif']]
    for index, args in enumerate(top):
        add(f'top/{index}', args)
    for command in COMMANDS:
        required = REQUIRED[command]
        add(f'{command}/unknown-option', [command, '--bogus'] if command == 'host-paths' else [command, '--bogus'])
        add(f'{command}/help-long', [command, '--help'])
        add(f'{command}/help-explicit', [command, '--help=x'])
        add(f'{command}/help-short-tail', [command, '-hx'])
        if not required:
            add(f'{command}/positional', [command, 'x'])
            add(f'{command}/two-unknown', [command, '--a', '--b'])
            continue
        add(f'{command}/nothing', [command])
        full = []
        for flag in required:
            full += [flag, VALUES.get(flag, 'v')]
        for flag in required:
            partial = []
            for other in required:
                if other != flag:
                    partial += [other, VALUES.get(other, 'v')]
            add(f'{command}/missing{flag}', [command] + partial)
        add(f'{command}/value-missing', [command, required[0]])
        add(f'{command}/value-is-option', [command, required[0], '--bogus'])
        add(f'{command}/empty-values', [command] + [x for flag in required[:1] for x in (flag, '')])
        add(f'{command}/dup-and-missing', [command, required[0], 'a', required[0], 'b'] if len(required) > 1 else [command, required[0], 'a', required[0], 'b', '--bogus'])
        add(f'{command}/equals-and-missing', [command, f'{required[0]}=x'] if len(required) > 1 else [command, f'{required[0]}=x', '--bogus'])
        add(f'{command}/dash-dash-value', [command, f'{required[0]}=--'] if len(required) > 1 else [command, f'{required[0]}=--', '--bogus'])
        add(f'{command}/abbreviation', [command, required[0][:4]] if len(required) > 1 else [command, required[0][:4], 'v', '--bogus'])
        if command not in ('exec',):
            add(f'{command}/full-plus-unknown', [command] + full + ['--bogus'])
            add(f'{command}/full-plus-positional', [command] + full + ['extra'])
        add(f'{command}/non-utf8-value', [command, required[0], 'a\udcffb'])
    add('bootstrap/ambiguous-s', ['bootstrap', '--s', 'x', '--root', 'r', '--output', 'o'])
    add('bootstrap/ambiguous-setting', ['bootstrap', '--setting-x'])
    add('bootstrap/abbrev-skills', ['bootstrap', '--skills', 'x'])
    for text in ('x', '', '1.5', '٣x', '-', '+', '1_', '_1', '0x1', '9' * 4301, ' ', '--', '1e3'):
        add(f'verify-pair/invalid-int/{text[:12]!r}', ['verify-pair', '--state', 's', '--expect-sha256', 'h', '--current-inventory', 'c',
                                                         '--guard-sha256', 'g', '--go-timeout', text])
    add('verify-pair/go-timeout-dash-dash', ['verify-pair', '--state', 's', '--expect-sha256', 'h', '--current-inventory', 'c', '--guard-sha256', 'g',
                                             '--go-timeout=--', '--bogus'])
    add('host-allowlist/append-and-error', ['host-allowlist', '--allowed-tools', 'a', '--allowed-tools=b', '--bogus'])
    for text in ('x', 'a\udcffb'):
        add(f'help-with-bytes/{text!r}', ['verify', '--state', text, '-h'])
    return cases


HOMES = [b'/h', b'/h/', b'~', b'', b'rel', b'/h/..', b'//h', b'/h/e\xcc\x81', b'/h/\xff', b'/h/\xe6\x97\xa5\xe6\x9c\xac', b'/h/"q', b'/h/\x01', b'/h/\xe2\x80\xa8']
CONFIGS = [None, b'/c', b'', b'~', b'//c', b'~/x', b'/c/..', b'rel', b'/c//x/./', b'/c/\xc3\xa9', b'/c/\xffz']


def host_paths_cases():
    cases = []
    for home in (*HOMES, None):
        for config in CONFIGS:
            env = {}
            if home is not None:
                env['HOME'] = home
            env['CLAUDE_CONFIG_DIR'] = config
            full = base_env()
            full.pop('HOME')
            for key, value in env.items():
                if value is not None:
                    full[key] = value
            cases.append(Case('host-paths', f'env/HOME={home!r}/CONFIG={config!r}', ['host-paths'], full))
    base = base_env({'HOME': '/h'})
    cases.append(Case('host-paths', 'extra-arg', ['host-paths', 'x'], base, kind='parse'))
    cases.append(Case('host-paths', 'cwd-independent-relative-home', ['host-paths'], base_env({'HOME': 'h'})))
    return cases


def output_failure_cases():
    cases = []
    for unbuffered in (None, '1', '0', ' 0', '00', '+0', 'abc', '', '0 ', '-1'):
        for target in ('full', 'pipe-closed'):
            good = base_env({'HOME': '/h', 'PYTHONUNBUFFERED': unbuffered})
            cases.append(Case('output', f'host-paths/stdout={target}/unbuffered={unbuffered!r}', ['host-paths'], good, {1: target}, kind='u4auto'))
            bad = base_env({'HOME': 'rel', 'PYTHONUNBUFFERED': unbuffered})
            cases.append(Case('output', f'host-paths-error/stderr={target}/unbuffered={unbuffered!r}', ['host-paths'], bad, {2: target}, kind='u4auto'))
            cases.append(Case('output', f'host-paths-error/both={target}/unbuffered={unbuffered!r}', ['host-paths'], bad, {1: target, 2: target}, kind='u4auto'))
            cases.append(Case('output', f'help/stdout={target}/unbuffered={unbuffered!r}', ['-h'], good, {1: target}, kind='u4auto'))
            cases.append(Case('output', f'usage-error/stderr={target}/unbuffered={unbuffered!r}', ['verify'], good, {2: target}, kind='u4auto'))
    for digits in ('640', '639', 'abc', '', '0', '99999999999999', ' 640'):
        cases.append(Case('output', f'int-max-str-digits={digits!r}', ['host-paths'], base_env({'HOME': '/h', 'PYTHONINTMAXSTRDIGITS': digits}), kind='u4auto'))
        cases.append(Case('output', f'int-max-str-digits-parse={digits!r}', ['verify-pair', '--state', 's', '--expect-sha256', 'h', '--current-inventory', 'c',
                                                                         '--guard-sha256', 'g', '--go-timeout', '9' * 700, '--bogus'],
                          base_env({'PYTHONINTMAXSTRDIGITS': digits}), kind='u4auto'))
    return cases


def _usage_part(stderr):
    """標準エラーの `<prog>: error:` の行より前(使い方の行)。"""
    lines = stderr.split(b'\n')
    for index, line in enumerate(lines):
        if b': error: ' in line:
            return b'\n'.join(lines[:index]) + b'\n'
    raise AssertionError('error: の行が無い')


def u7_cases():
    """閉じた標準の fd(U7)。旧版は `sys.stdout` などが `None` になる。Rust は開き直した /dev/null を使う。
    旧新それぞれの固定期待を、旧版の通常の実行から作る(表示の本文を字面で持たない)。"""
    cases = []
    good = base_env({'HOME': '/h'})
    bad = base_env({'HOME': 'rel'})
    cases.append(Case('u7', 'help-with-stdout-closed', ['-h'], good, {1: 'close'}, kind='u7',
                      u7=lambda normal: ((0, b'', normal.stdout), (0, b'', b''))))
    cases.append(Case('u7', 'sub-help-with-stdout-closed', ['verify', '--help'], good, {1: 'close'}, kind='u7',
                      u7=lambda normal: ((0, b'', normal.stdout), (0, b'', b''))))
    cases.append(Case('u7', 'usage-error-with-stderr-closed', ['bogus'], good, {2: 'close'}, kind='u7',
                      u7=lambda normal: ((1, _usage_part(normal.stderr), b''), (2, b'', b''))))
    cases.append(Case('u7', 'sub-usage-error-with-stderr-closed', ['verify'], good, {2: 'close'}, kind='u7',
                      u7=lambda normal: ((1, _usage_part(normal.stderr), b''), (2, b'', b''))))
    cases.append(Case('u7', 'host-paths-error-with-stderr-closed', ['host-paths'], bad, {2: 'close'}, kind='u7',
                      u7=lambda normal: ((20, normal.stderr, b''), (20, b'', b''))))
    # 通常の一致になる(旧新が同じ結果)閉じ方。
    for name, redirects in (('stdout-closed', {1: 'close'}), ('stdin-closed', {0: 'close'}), ('all-closed', {0: 'close', 1: 'close', 2: 'close'})):
        cases.append(Case('u7-same', f'host-paths-{name}', ['host-paths'], good, redirects))
    cases.append(Case('u7-same', 'host-paths-error-stdout-closed', ['host-paths'], bad, {1: 'close'}))
    cases.append(Case('u7-same', 'host-paths-error-stdin-closed', ['host-paths'], bad, {0: 'close'}))
    return cases


def fatal_cases():
    """標準の fd がディレクトリのとき、旧版は起動時に落ちる。Rust の再現は 2 段目 b の申し送りで、
    再現していない 4 件(fd 2 は旧版が診断を書けず、終了コード 1・出力なし)は `UNIMPLEMENTED_FATAL`(静的な一覧)で「未実装の印」にする(形式の検査は旧版側に常に行う。fd 2 の件は、Fatal の診断を旧版が書けないので、旧版の固定期待〈1・空・空〉を照らす)。"""
    good = base_env({'HOME': '/h'})
    return [Case('fatal', f'host-paths-fd{fd}-directory', ['host-paths'], good, {fd: 'dir'}, kind='fatal') for fd in (0, 1)] + \
           [Case('fatal', 'host-paths-fd2-directory', ['host-paths'], good, {2: 'dir'}, kind='fatal-silent')] + \
           [Case('fatal', 'verify-pair-fd0-directory', ['verify-pair', '--state', 's', '--expect-sha256', 'h', '--current-inventory', 'c',
                                                        '--guard-sha256', 'g', '--go-timeout', '5'], good, {0: 'dir'}, kind='fatal')]


# 標準の fd がディレクトリのときの Fatal 診断を、Rust がまだ再現しない件(2 段目 b の申し送り)。
# 名前の一覧で決め、Rust の今の結果(終了コード・標準出力・標準エラー)を固定期待にする。
# 再現を実装したら、この一覧から外す(固定期待が合わなくなって落ちる)。
UNIMPLEMENTED_FATAL = {
    'host-paths-fd0-directory': (0, b'{"home":"/h","config":"/h/.claude","user_settings":"/h/.claude/settings.json","cache_settings":"/h/.claude/remote-settings.json"}\n', b''),
    'host-paths-fd1-directory': (120, b'', b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 9] Bad file descriptor\n"),
    'host-paths-fd2-directory': (0, b'{"home":"/h","config":"/h/.claude","user_settings":"/h/.claude/settings.json","cache_settings":"/h/.claude/remote-settings.json"}\n', b''),
    'verify-pair-fd0-directory': (20, b'', 'ERROR [environment-guard] このコマンドの本体は段階 2b で実装する\n'.encode()),
}


# 2 段目 b-2 で足す本体の成功経路。今は数えない(skip として合格にしない)。b-1d で足した bootstrap・verify・host-settings・host-allowlist・
# U1・U2・U6・相互読取りは、`s2e_commands.py` の比較に入れた。
PENDING_2B = (
    'read・exec: 生バイト・子の終了コードとシグナル・PATH の探索',
    'hook: 照合と保持値の確認(U3: 確認の後の Rust は終了コード 20)',
    'verify-pair: verified・go の待ち・2 回の照合・guard のコピーの再照合',
    'U4: 捕まえない例外の表示(最後の行・因果の区切り)',
    'U7: fd 0 が閉じた verify-pair・exec の子の fd',
    'U5: PYTHONUNBUFFERED と PYTHONINTMAXSTRDIGITS だけを比べる。ほかの起動を変える環境変数(PYTHONHASHSEED・PYTHONMALLOC・PYTHONHOME など)は、'
    '2026-11-11 22:31 の利用者の決定で比較の前提の外(比べるケースは足さない)',
)


# ---------------------------------------------------------------- 実行

def stdlib_dir():
    import argparse as module
    return Path(module.__file__).parent


class Runner:
    def __init__(self, fixture, old_script, cwd, timeout=OLD_TIMEOUT):
        self.fixture, self.old_script, self.cwd, self.timeout = fixture, old_script, cwd, timeout

    def old(self, case, redirects=None):
        argv = [sys.executable, '-B', str(self.old_script), *map(self._arg, case.args)]
        return run_process(argv, env=case.env, cwd=self.cwd, timeout=self.timeout,
                           redirects=case.redirects if redirects is None else redirects)

    def new(self, case):
        return run_process([str(self.fixture), *map(self._arg, case.args)], env=case.env, cwd=self.cwd, timeout=self.timeout,
                           redirects=case.redirects)

    @staticmethod
    def _arg(value):
        return os.fsencode(value) if isinstance(value, str) else value


def run_case(runner, case, tally):
    old, new = runner.old(case), runner.new(case)
    label = f'{case.group}/{case.name}'
    try:
        if case.kind == 'u4auto':
            # 出力の故障で、捕まえない例外(traceback)になる経路がある。同じなら通常の一致、
            # 旧版が traceback を出したときだけ、U4 の関数で比べて「認めた差」に数える。
            try:
                same_result(old, new)
                tally['matched'] += 1
            except AssertionError:
                if b'Traceback (most recent call last):' not in old.stderr:
                    raise
                approved_u4(old, new, script=runner.old_script, stdlib=stdlib_dir())
                tally['approved'] += 1
                tally['u4'] += 1
        elif case.kind in ('same', 'parse'):
            if case.kind == 'parse' and old.returncode not in (0, 2):
                raise AssertionError('解析だけで終わる前提が崩れた(旧版の終了値が 0・2 以外)')
            same_result(old, new)
            tally['matched'] += 1
        elif case.kind == 'u7':
            normal = runner.old(case, redirects={})
            old_expected, new_expected = case.u7(normal)
            fixed_pair(old, new, old_expected, new_expected)
            tally['approved'] += 1
        elif case.kind == 'fatal-silent':
            # 標準エラーがディレクトリ: 旧版は Fatal の診断を書けず、終了コード 1・出力なし。旧版の固定期待を別に照らす。
            fixed(old, 1)
            if case.name in UNIMPLEMENTED_FATAL:
                fixed(new, *UNIMPLEMENTED_FATAL[case.name])
                tally['unimplemented'].append(label)
            else:
                same_result(old, new)
                tally['matched'] += 1
        elif case.kind == 'fatal':
            if not fatal_shape_ok(old.stderr):
                raise AssertionError('旧版の Fatal 診断の形式が想定と違う')
            if case.name in UNIMPLEMENTED_FATAL:
                # 未実装の印は、出力の形ではなく静的な一覧だけで決める。時間切れ・panic・形の崩れた Fatal は
                # 固定期待と合わないので落ちる。
                fixed(new, *UNIMPLEMENTED_FATAL[case.name])
                tally['unimplemented'].append(label)
            else:
                approved_fatal(old, new)
                tally['approved'] += 1
        else:
            raise AssertionError('未知のケースの種類')
    except AssertionError as error:
        raise AssertionError(f'{label}: {error}') from None


def all_cases():
    return argument_cases() + host_paths_cases() + output_failure_cases() + u7_cases() + fatal_cases()


def smoke_cases():
    """比較器自身の検査が使う、少数の代表ケース。"""
    return [
        Case('args', 'help/top', ['-h'], base_env(), kind='parse'),
        Case('args', 'usage-error', ['verify'], base_env(), kind='parse'),
        Case('host-paths', 'ok', ['host-paths'], base_env({'HOME': '/h'})),
        Case('host-paths', 'error', ['host-paths'], base_env({'HOME': 'rel'})),
        Case('output', 'full', ['host-paths'], base_env({'HOME': '/h'}), {1: 'full'}),
        Case('output', 'help-full-unbuffered', ['-h'], base_env({'HOME': '/h', 'PYTHONUNBUFFERED': '1'}), {1: 'full'}, kind='u4auto'),
    ]


def run_contract(fixture, cases, *, old_script, cwd, timeout=OLD_TIMEOUT):
    runner = Runner(fixture, old_script, cwd, timeout)
    tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
    started = time.monotonic()
    for case in cases:
        run_case(runner, case, tally)
    tally['elapsed'] = time.monotonic() - started
    if tally['matched'] + tally['approved'] == 0:
        raise AssertionError('比較したケースが 0 件')
    return tally


def run_suite(suite):
    count = suite.countTestCases()
    if count == 0:
        raise AssertionError('検査ケースが 0 件')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful() or result.testsRun != count or result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        raise AssertionError('未実行・失敗・省略したケースがある')
    return count


# ---------------------------------------------------------------- 既存テストの対応表

# (テスト名, 分類, 対応先または理由, 補足)。分類: both=旧新の両方で比較・rust=Rust の検査へ置換・out=対象外。
# 「予定」は 2 段目 b で作る。「実装済み」は今ある比較か Rust のテスト。
B, R, O = 'both', 'rust', 'out'
TEST_MAPPING = [
    ('test_full_distribution_is_retained_and_binary_changes_are_rejected', B, '比較[x-real-distribution, x-real-distribution-flip](配布物の全体の保持と、Rust のバイナリ・launch.sh・resolve-task-dir.py の 1 ビットの書き換えの拒否と戻した後。旧版との比較)。Budget の計上は budget.rs::tick_stops_just_after_ten_thousand_items。check_plugin_launch は対象外(D2。固定の読み込み器と launch の入口)', ''),
    ('test_normal_and_copy_exec', B, 'bootstrap と verify は 比較[basic]。exec は次の段(予定)', ''),
    ('test_source_fixed_output_and_skill_change', B, '比較[verify-edit-plugin-file, verify-edit-plugin-file-same-size, verify-skill-edit, x-source-guard-changed]', ''),
    ('test_copy_modified', B, '比較[verify-copy-edit, verify-copy-remove, verify-copy-add, verify-copy-chmod, verify-copy-dir-chmod]', ''),
    ('test_manifest_and_hash_forgery', B, '比較[x-forged-state, verify-state-edit, verify-state-remove]', ''),
    ('test_new_and_removed_empty_directories', B, '比較[x-empty-dir-plugin-add, x-empty-dir-plugin-remove, x-empty-dir-copy-add, x-empty-dir-copy-remove, x-empty-dir-skills-add, x-empty-dir-skills-remove]', ''),
    ('test_missing_then_added_user_setting', B, '比較[verify-cache-created, verify-shell-created, verify-xdg-config-created]', ''),
    ('test_quoted_continued_include_normal_and_change', B, '比較[include-quoted, include-plain, include-chain, git-duplicate-include]', ''),
    ('test_special_file_and_parent_symlink_fail_without_read', B, '比較[x-special-fifo-in-plugin, x-special-parent-symlink, x-special-settings-fifo]', ''),
    ('test_state_fifo_no_block', B, '比較[x-state-fifo]', ''),
    ('test_reused_output_never_executes_previous_guard', B, '比較[x-reused-output, basic]', ''),
    ('test_component_full_stat_rejects_restored_content', B, '比較[x-component-restored]', ''),
    ('test_read_components_returns_held_bytes_and_stable_policy_digest', R, '実装済み: 内部 reader の旧新比較(全流れの終わりに reader 3 つの型の印つきの値を比べる。比較[options-readers, options, reader-foreign-component, basic])と、state.rs::component_digest_and_descriptors_match_python_3_10', ''),
    ('test_component_tree_follows_multihop_directory_link_and_rejects_cycle', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(component の木)と 比較[x-multihop-managed-roots, x-install-link-official]', ''),
    ('test_component_tree_allows_skill_install_link_but_not_agent_link', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-install-link-official, x-install-link-agent-kind]', ''),
    ('test_empty_directory_budget_and_file_budget', R, '実装済み: budget.rs::tick_stops_just_after_ten_thousand_items と budget.rs::reserve_checks_the_file_limit_just_before_and_after と 比較[x-limit-entries-after, x-settings-size-over]', ''),
    ('test_parent_replacement_does_not_read_external_secret', R, '実装済み: fsio.rs::a_swapped_parent_is_rejected_after_the_body と race_tests.rs::a_subdirectory_swapped_between_lstat_and_open_is_refused と 比較[x-race-verify-parent-swap, x-parent-swap-host-settings]', ''),
    ('test_shell_and_xdg_settings_change', B, '比較[verify-shell-created, verify-xdg-config-created]', ''),
    ('test_active_plugin_inventory_and_content_change', B, '比較[options](verify --current-inventory)と、実装済みの pyjson の plugin_inventory の固定期待', ''),
    ('test_direct_read_exec_no_original_import', B, 'read・exec/direct(予定)', ''),
    ('test_replaced_external_receipt_is_not_an_isolation_boundary', B, '比較[x-receipt-replaced]', ''),
    ('test_permission_sources_preserve_identity_presence_and_private_values', R, '実装済み: settings.rs::read_host_settings_cases_match_python_3_10 と 内部 reader の旧新比較(source の値を含む型の印つきの値の一致。比較[options-readers])', ''),
    ('test_permission_reader_keeps_bash_conversion_cli_user_only', R, '実装済み: settings::host_allowlist_cases_match_python_3_10 と rule_cases', ''),
    ('test_host_paths_are_absolute_nfc_and_cwd_independent', B, '実装済み: check_s2e.py の host-paths/env/*', ''),
    ('test_forbidden_setting_env_and_helpers_are_rejected_without_values', R, '実装済み: settings.rs::check_host_setting_cases_match_python_3_10 と 比較[host-env-home, host-env-source, host-env-managed, host-policy-helper, host-hooks-disabled](値を出さない確認は host-settings の標準出力・標準エラーの一致)', ''),
    ('test_strict_json_and_verified_allowlist', R, '実装済み: pyjson の strict_settings の固定期待と settings::host_allowlist_cases', ''),
    ('test_setting_change_after_boot_is_rejected', B, '比較[verify-settings-edit, verify-settings-remove, verify-settings-to-link]', ''),
    ('test_reader_rechecks_bytes_mode_missing_and_link_after_verify', R, '実装済み: settings.rs::read_host_settings_cases_match_python_3_10(順序)と 比較[x-race-host-settings-settings-edit, x-race-host-settings-settings-fifo, x-race-host-settings-settings-link, x-race-host-settings-parent-swap]', ''),
    ('test_reader_rechecks_final_link_set_for_user_cache_and_managed', R, '実装済み: settings::read_host_settings_cases の @file-link の照合', ''),
    ('test_parser_budget_and_unused_output_are_checked_after_parse', R, '実装済み: settings::allowlist_rules_trim_unused_lines_to_300_after_replacing_controls と host_allowlist_cases', ''),
    ('test_host_setting_descriptors_are_paths_and_kinds_only', R, '実装済み: settings::host_settings_specs_cases と read_host_settings_cases の不正な項目', ''),
    ('test_single_setting_symlink_is_supported_but_dropin_tree_symlink_is_rejected', B, '比較[x-settings-single-link, x-dropin-tree-link, settings-link]', ''),
    ('test_settings_parent_swap_fifo_mode_and_budget_reject', B, '比較[x-parent-swap-host-settings, x-special-settings-fifo, x-special-settings-mode-0200, x-settings-size-over, x-settings-size-limit, x-config-dir-fifo]', ''),
    ('test_managed_dropin_names_match_the_host_scope', R, '実装済み: settings::host_settings_specs_cases(並べ替えと名前の規則)', ''),
    ('test_new_managed_dropin_between_snapshot_and_descriptors_is_rejected', R, '比較[x-race-bootstrap-new-managed-dropin, x-race-verify-new-managed-dropin](PATH の偽の git が解析中に書き足す。旧版の回と Rust の回で、終了コード・標準エラー・Git の起動回数・出力フォルダの構成を比べる)', ''),
    ('test_update_doc_standalone_entry_binds_to_verified_copy_before_modes', O, 'SKILL.md の文書と固定の読み込み器の字面の検査(読み込み器を通す)', ''),
    ('test_held_launcher_rejects_replaced_fixed_success_code', O, 'loop.sh 側の固定の読み込み器(Python の字面)の検査。D2 の混在の条件で扱う', ''),
    ('test_post_check_execution_gap_is_detected_only_on_next_check', B, 'exec/post-check-gap(予定)', ''),
    ('test_symlink_dotdot_is_not_lexically_hidden', B, '比較[x-git-include-dotdot-symlink]', ''),
    ('test_regular_dotdot_include_and_git_path_expansion', B, '比較[x-git-include-expansion, include-absolute]', ''),
    ('test_official_global_install_and_target_change', B, '比較[x-install-link-official, x-install-link-agents]', ''),
    ('test_link_retarget_rejected_before_target_content_read', R, '実装済み: race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused と 比較[x-install-link-retarget]', ''),
    ('test_relative_install_link_and_internal_link_rejection', B, '比較[x-install-link-relative]', ''),
    ('test_fixed_hook_loader_rejects_permission_and_guard_modification', O, '固定の読み込み器と hook の字面の検査(読み込み器を通す)', ''),
    ('test_other_host_dynamic_skills_and_settings_directories', B, '比較[x-dynamic-directories]', ''),
    ('test_normal_dotfile_links_and_include_target_change', B, '比較[x-dotfile-links, include-link-config]', ''),
    ('test_dotfile_retarget_rejected_before_content', R, '実装済み: race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused と 比較[x-dotfile-retarget]', ''),
    ('test_dotfile_target_replacement_and_cycle', B, '比較[x-dotfile-target-replaced, x-dotfile-cycle]', ''),
    ('test_timeout_and_oserror_do_not_print_argument_or_path_secret', R, '実装済み: error.rs::detail_hides_the_exception_arguments と output.rs::caught_exceptions_become_error_lines_with_exit_20 と 比較[x-special-settings-mode-0200, x-config-dir-fifo]', ''),
    ('test_target_replacement_between_metadata_and_read_is_not_read', R, '実装済み: race_tests.rs::a_file_replaced_after_the_record_lstat_is_refused_before_reading と fsio.rs::a_fifo_swapped_in_after_lstat_is_opened_without_blocking_and_rejected', ''),
    ('test_git_path_expansion_is_bounded_while_reading', R, '実装済み: gitconfig.rs::output_is_limited_to_eight_mib_while_it_is_received', ''),
    ('test_stderr_is_discarded_and_timeout_is_fixed', R, '実装済み: gitconfig.rs::the_child_runs_in_the_root_directory_with_stderr_discarded と gitconfig.rs::a_slow_child_is_stopped_at_the_deadline_and_killed', ''),
    ('test_changed_known_root_stops_before_new_include', B, '比較[verify-gitconfig-edit, x-race-verify-gitconfig-edit, x-preread-root-edit-fifo, x-preread-root-edit-0200](読んではならない先を FIFO・権限の無いファイルにして、別の理由で止まらないことで確かめる)', ''),
    ('test_new_root_candidate_stops_before_its_body_and_include', B, '比較[verify-xdg-config-created, git-global-env, x-preread-new-root-fifo, x-preread-new-root-0200](新しい根の候補自身を FIFO・権限の無いファイルにする)', ''),
    ('test_changed_known_include_stops_before_new_nested_target', B, '比較[verify-include-target-edit, x-race-verify-include-target-edit, x-preread-include-edit-fifo, x-preread-include-edit-0200]', ''),
    ('test_absent_include_candidate_stops_before_new_body', B, '比較[include-missing-target, x-race-bootstrap-new-include-target, x-preread-absent-include-fifo, x-preread-absent-include-0200]', ''),
    ('test_known_nested_and_missing_graph_stays_normal', B, '比較[include-chain-two, include-missing-target, x-preread-nested-missing-graph]', ''),
    ('test_go_after_verified_succeeds_with_nothing_on_stderr', B, 'verify-pair/go(予定)', ''),
    ('test_go_arriving_in_pieces_still_proceeds', B, 'verify-pair/go-pieces(予定)', ''),
    ('test_stop_eof_wrong_word_and_unterminated_go_exit_20_silently', B, 'verify-pair/stop-eof-word(予定)', ''),
    ('test_stop_through_loader_adds_no_false_diagnostic', O, '読み込み器を通す(os._exit(20) が読み込み器の変換を迂回する性質の検査)。D2 で扱う', ''),
    ('test_waiting_for_go_has_a_time_limit', B, 'verify-pair/go-timeout(予定)', ''),
    ('test_first_failure_never_prints_verified', B, 'verify-pair/first-failure(予定)', ''),
    ('test_second_check_compares_the_inventory', B, 'verify-pair/second-inventory(予定)', ''),
    ('test_second_check_reads_the_state_again_and_sees_a_change_between', B, 'verify-pair/change-between(予定)', ''),
    ('test_guard_body_rewritten_between_the_checks_is_found', B, 'verify-pair/guard-rewritten(予定。U2: コピーを照らす)', ''),
    ('test_a_different_guard_hash_is_rejected_after_go', B, 'verify-pair/guard-hash(予定)', ''),
    ('test_bad_arguments_are_rejected_before_any_check', B, 'verify-pair/bad-run-pair-arguments(予定。引数の解析は実装済みの args/verify-pair/* で比べる。解析を通った後の run_pair の検査〈guard の SHA-256 が 64 桁の 16 進でない・--go-timeout が 0 → ERROR の行と 20〉は verify-pair の本体で比べる)', ''),
    ('test_existing_verify_is_unchanged', B, '比較[basic]', ''),
    ('test_no_include_means_no_git_start_on_verify_but_always_on_bootstrap_snapshot', R, '実装済み: command_tests.rs::include_text_is_matched_by_bytes_ignoring_ascii_case と 比較[verify-gitconfig-edit-keeps-include-text, include-comment-upper, basic](Git の起動回数を旧新で比べる)', ''),
    ('test_include_spellings_still_reach_git_and_follow_the_target', B, '比較[include-comment-upper, include-section-mixed, include-quoted, include-absolute](Git の起動回数も比べる)', ''),
    ('test_include_word_without_a_destination_is_parsed_and_stays_normal', B, '比較[verify-gitconfig-edit-keeps-include-text, include-missing-target]', ''),
    ('test_changed_config_gives_the_same_result_with_or_without_include', B, '比較[verify-gitconfig-edit, verify-gitconfig-edit-keeps-include-text]', ''),
    ('test_three_entries_and_parent_hold_identical_loader', O, '3 つの入口の読み込み器の字面の一致(Python の字面。読み込み器を通す)', ''),
    ('test_three_normal_direct_entries_execute_only_matching_bytes', O, '読み込み器を通す(照合したバイト列だけを実行する)。D2 で扱う', ''),
    ('test_real_bootstrap_and_direct_verify_stay_normal', B, 'bootstrap と verify は CLI で旧新を比べる 比較[basic, x-real-distribution, x-real-distribution-flip]。固定の読み込み器(-I)を通す verify は対象外(D2 で扱う)', ''),
    ('test_link_fifo_and_oversize_reject_before_body_open', O, '読み込み器自身の拒否(1 MiB 超など。読み込み器を通す)', ''),
    ('test_changed_path_after_hash_never_executes_replacement', O, '読み込み器を通す(照合と実行の間の差し替え)', ''),
    ('test_read_timeout_uses_fixed_diagnostic', O, '読み込み器自身の 15 秒の診断', ''),
    ('test_fixed_loader_and_hook_ignore_cwd_and_pythonpath_modules', O, '読み込み器の `-I` の性質(Python の内部の観察)', ''),
    ('test_profile_keeps_a_normal_user_site_yaml_dependency', O, 'Python の user site と PyYAML 依存の観察(Rust に関係しない)', ''),
    ('test_plain_scoped_skill_and_direct_scoped_agents', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[options, x-install-link-agent-kind]', ''),
    ('test_absolute_relative_multihop_intermediate_and_dotdot', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-multihop-managed-roots, x-install-link-relative]', ''),
    ('test_same_physical_target_reads_once_and_keeps_aliases', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(同じ物理ファイルは 1 回だけ読む。予算の項目数と量を旧版の値と比べる)と 比較[x-multihop-managed-roots]', ''),
    ('test_cycles_dangling_and_41_links_are_not_missing', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(導入リンクの 40 と 41 の端を含む)', ''),
    ('test_fifo_socket_and_nested_links_are_rejected', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と 比較[x-special-fifo-in-plugin, x-install-link-agent-kind]', ''),
    ('test_all_replacements_stop_before_new_body_read', R, '実装済み: race_tests.rs::a_file_rewritten_after_its_stat_was_kept_is_refused_before_reading と race_tests.rs::a_file_replaced_by_a_link_between_the_walk_and_the_read_is_refused と race_tests.rs::a_cached_file_is_still_rechecked_after_its_stat_for_every_logical_name', ''),
    ('test_changed_link_target_directory_is_not_opened', R, '実装済み: race_tests.rs::an_installation_link_target_swapped_during_the_walk_is_refused と race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused', ''),
    ('test_missing_root_and_entry_creation_reads_no_body', R, '実装済み: race_tests.rs::a_manifest_created_between_the_two_missing_checks_is_refused と race_tests.rs::a_file_added_after_the_names_were_compared_stops_without_reading_it と 比較[verify-agents-dir-created, x-enterprise-created]', ''),
    ('test_post_read_link_replacement_rejects_result', R, '実装済み: race_tests.rs::the_link_replaced_after_its_target_was_read_is_refused と race_tests.rs::a_link_target_replaced_after_it_was_read_is_refused', ''),
    ('test_byte_entry_and_time_budgets_cover_link_walk', R, '実装済み: budget.rs::tick_stops_just_after_ten_thousand_items と budget.rs::tick_stops_only_after_the_deadline と observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input(導入リンクの先も同じ予算)', ''),
    ('test_bootstrap_multihop_and_additive_managed_roots', B, '比較[x-multihop-managed-roots, options]。reader の戻りの kind・raw・relative は 内部 reader の旧新比較(比較[options-readers, x-multihop-managed-roots])', ''),
    ('test_missing_enterprise_commands_creation_is_rejected', B, '比較[x-enterprise-created]', ''),
    ('test_reader_uses_walker_bytes_and_rejects_old_descriptors', R, '実装済み: command_tests.rs::rewritten_state_flows_match_the_old_python_3_10 と 比較[legacy-state, reader-foreign-component](reader は旧い記述子の状態を拒否する)', ''),
    ('test_policy_digest_excludes_all_runtime_identity', R, '実装済み: state::component_digest_and_descriptors_match_python_3_10 と digest_depth', ''),
    ('test_selected_marketplace_is_held_without_inactive_tree', R, '実装済み: inventory.rs::component_inventory_cases_match_python_3_10(full・manifest-*)と 比較[options]', ''),
    ('test_marketplace_missing_ambiguous_and_external_sources_fail', R, '実装済み: inventory::component_inventory_cases(manifest-*・installed-*)', ''),
    ('test_synced_and_local_inventory_do_not_need_marketplace_registry', R, '実装済み: inventory::component_inventory_cases(synced・no-marketplace)', ''),
    ('test_skills_dir_inventory_merges_only_exact_held_root_and_manifest', R, '実装済み: inventory::component_inventory_cases(skills-dir)', ''),
    ('test_skills_dir_rejects_unknown_ids_wrong_name_missing_manifest_and_outside', R, '実装済み: inventory::component_inventory_cases(skills-dir-*)', ''),
    ('test_skills_dir_outside_and_ambiguous_sources_are_rejected_without_opening_target', R, '実装済み: inventory.rs::component_inventory_cases_match_python_3_10(skills-dir-two-candidates)と race_tests.rs::a_link_target_swapped_before_it_is_read_is_refused', ''),
    ('test_skills_dir_installation_link_uses_held_chain_and_rejects_replacement', R, '実装済み: race_tests.rs::an_installation_link_replaced_after_the_walk_is_refused と race_tests.rs::an_installation_link_replaced_after_its_walk_is_refused と 比較[x-install-link-retarget]', ''),
    ('test_skills_dir_reordering_preserves_missing_descriptors_and_shared_budget', R, '実装済み: inventory.rs::component_inventory_cases_match_python_3_10 と budget.rs::tick_stops_just_after_ten_thousand_items と 比較[options]', ''),
    ('test_legacy_state_verify_retains_previous_contract', B, '比較[legacy-state, legacy-state-edit-plugin-file, legacy-state-copy-edit, legacy-state-settings-edit, legacy-state-gitconfig-edit, legacy-state-inventory, legacy-state-inventory-edit-install, legacy-state-inventory-add-install, legacy-state-inventory-remove-install](旧い状態〈version 3 の記述子なし〉の verify と、reader が旧い状態を拒否すること)', ''),
    ('test_exactly_40_links_are_allowed', R, '実装済み: observe_tests.rs::observation_matches_the_old_python_on_the_same_physical_input と depth.rs::checks_fail_just_above_the_limit_with_the_old_messages(導入リンクの 40 と 41)', ''),
    ('test_legacy_comparison_reads_replacement_but_component_stops_first', R, '実装済み: race_tests.rs::a_file_rewritten_after_its_stat_was_kept_is_refused_before_reading と 比較[legacy-state-edit-plugin-file]', ''),
    ('test_unreadable_file_and_agent_install_link_rejected', R, '比較[x-install-link-agent-kind, x-special-settings-mode-0200]', ''),
    ('test_registry_wrong_path_and_duplicate_active_id_rejected', R, '実装済み: inventory::component_inventory_cases(installed-wrong-path)と pyjson の plugin_inventory(重複 ID)', ''),
    ('test_two_selected_entries_share_manifest_without_losing_sources', R, '実装済み: inventory::component_inventory_cases(manifest-existing-*)', ''),
    ('test_missing_manifest_creation_before_return_is_rejected', R, '実装済み: race_tests.rs::a_manifest_created_between_the_two_missing_checks_is_refused', ''),
    ('test_policy_digest_normalizes_only_workflow_run_copy_root', R, '実装済み: state の digest の固定期待(workflow の根だけ @workflow)', ''),
    ('test_policy_digest_ignores_missing_parent_location_but_tracks_component', R, '実装済み: state の digest の固定期待(@route の除外)', ''),
    ('test_success_output_and_return_values', B, 'read の生バイト・exec の子の終了コード・-B・PYTHONDONTWRITEBYTECODE・bash の引数(予定。CLI で比べる)', ''),
    ('test_stop_lines', B, 'read・exec・hook の run_copy の理由の行(予定。Rust にはまだ無い。output::main_cases の変換は実装済みだが、理由の行は本体と一緒に比べる)', ''),
    ('test_seam_order', B, 'bootstrap・verify の拒否の順と観測の順は 比較[order-files, order-files-before-directories, order-directories-before-components, order-components-before-configs, verify-state-edit, x-race-verify-settings-edit, x-race-bootstrap-settings-edit](CLI で比べる。モンキーパッチは使っていない)。read・exec の拒否の順は次の段(予定)', ''),
    ('test_hook_runs_copy_in_same_process', O, 'U3: Rust の hook は確認の後に終了コード 20。Python の同じプロセスでの exec の観察', ''),
    ('test_hook_exceptions_are_caught_by_main', R, '実装済み: output::main_cases_match_python_3_10(例外の変換の部分だけ)。hook の本体を Python が exec する経路は U3 の差。Rust は確認の後に 20 を返すので、2 段目 b で U3 の予定の行へ移す(予定)', ''),
]
CATEGORY_NAMES = {B: '旧新の両方で比較', R: 'Rust の検査へ置換', O: '対象外'}


def existing_test_names():
    """既存のテストファイルから、(クラス名, テスト名, 本文に bootstrap を使うか)を読む。"""
    import ast
    source = EXISTING_TESTS.read_text(encoding='utf-8')
    lines = source.split('\n')
    result = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith('test'):
                    body = '\n'.join(lines[item.lineno - 1:item.end_lineno])
                    uses = bool(re.search(r'bootstrap|self\.boot\(|\.boot\(', body))
                    result.append((node.name, item.name, uses))
    return result


def mapping_summary():
    """対応表の件数の内訳。「予定」を含む行は 2 段目 b で作る物、含まない行は今ある物(実装済みの比較・
    Rust のテスト・対象外)。U2 の差(テストの配布物に guard が無い)で固定入力を作り直すテストは、
    「旧新の両方で比較」の行のうち、既存テストの本文が bootstrap を使うものだけを数える。"""
    tests = existing_test_names()
    mapped = {row[0]: row for row in TEST_MAPPING}
    counts = {B: 0, R: 0, O: 0}
    rebuilt = 0
    for _, name, uses in tests:
        category = mapped[name][1]
        counts[category] += 1
        if uses and category == B:
            rebuilt += 1
    planned = sum(1 for row in TEST_MAPPING if '予定' in row[2])
    return {'total': len(tests), 'counts': counts, 'rebuilt_for_u2': rebuilt, 'planned': planned,
            'compared': len(TEST_MAPPING) - planned,
            'done': sum(1 for row in TEST_MAPPING if '実装済み' in row[2] and '予定' not in row[2])}


def mapping_markdown():
    tests = existing_test_names()
    classes = {name: cls for cls, name, _ in tests}
    summary = mapping_summary()
    lines = ['# scripts/test_environment_guard.py の対応表(S2E・#470)', '',
             '`native/ci/check_s2e.py` の `TEST_MAPPING` から作る。直すときは `TEST_MAPPING` を直し、',
             '`python3 -B native/ci/check_s2e.py --write-mapping` で作り直す。', '',
             f'- 全 {summary["total"]} テスト: 旧新の両方で比較 {summary["counts"][B]}・Rust の検査へ置換 {summary["counts"][R]}・対象外 {summary["counts"][O]}',
             f'- うち予定を含まない(比較済み・実装済みの Rust のテスト・対象外): {summary["compared"]} 件・「予定」(2 段目 b で作る)を含む: {summary["planned"]} 件',
             f'- 「実装済み」を含み、「予定」を含まない行: {summary["done"]} 件',
             f'- U2 の差(テストの配布物に guard が無い): 「旧新の両方で比較」の行のうち、既存テストの本文が bootstrap を使い、旧 guard を足した配布物で固定入力を作り直すテスト: {summary["rebuilt_for_u2"]} 件(「Rust の検査へ置換」・対象外の行は数えない)',
             '- 読み込み器を通すテストと、Python の内部だけを観察するテストは、理由を書いて対象外にした。', '',
             '- 最後の欄は、指す比較のうち「Linux だけ」の印の項目(Mac の経路では流さない)。印は `s2e_commands.py` の項目の一覧から作る。', '',
             '| テスト | 分類 | 対応先・理由 | Linux だけの比較 |', '|---|---|---|---|']
    marks = {item.name: item.mark for item in commands_module().items('/nonexistent-fixture')}
    for name, category, target, _ in TEST_MAPPING:
        refs = [case.strip() for group in CASE_REF.findall(target) for case in group.split(',')]
        only = ', '.join(dict.fromkeys(case for case in refs if marks.get(case) == commands_module().LINUX_ONLY)) or '-'
        lines.append(f'| `{classes[name]}.{name}` | {CATEGORY_NAMES[category]} | {target} | {only} |')
    return '\n'.join(lines) + '\n'


TARGET_WORDS = ('比較[', '.rs::', '予定', '対象外', '実装済み')
RUST_DIR = ROOT / 'native/crates/cli/src/commands/environment_guard'
CASE_REF = re.compile(r'比較\[([^\]]+)\]')
RUST_REF = re.compile(r'([a-z_]+)\.rs::([a-z_0-9]+)')


def commands_module():
    """9 コマンドの旧新比較の部品(`s2e_commands.py`)。"""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import s2e_commands
    return s2e_commands


def check_command_totals(tally, per_group):
    """9 コマンドの比較に 0 件の欄が無いことを確かめる(未起動・省略・時間切れを合格にしない)。流した項目の数を返す。"""
    executed = sum(count for count, _ in per_group.values())
    approved_required = not commands_module().MAC_PATH   # 認めた差(U1・U2)の項目は Linux だけの印。Mac の経路では流さない
    for label, value, required in (('項目', executed, True), ('起動の一致', tally.runs, True), ('bootstrap の出力一式のバイト比較', tally.bootstrap_bytes, True),
                                   ('相互読取り', tally.cross, True), ('内部 reader の値の比較', tally.readers, True),
                                   ('読取りだけの前後確認', tally.readonly, True), ('認めた差の固定期待', tally.approved, approved_required)):
        if value == 0 and required:
            raise AssertionError(f'9 コマンドの比較が 0 件の欄がある: {label}')
    return executed


# 9 コマンドの比較の項目の数。機種ごとに完全一致で確かめる(0 でないことだけ確かめない)。
# 'linux': Linux で流す全項目。'mac': Mac の経路(Linux だけの印の項目を流さない)の項目。キーは (グループ, 印)。値は項目の数。
EXPECTED_ITEMS = {
    'linux': {'total': 197, 'groups': {
        ('extra', 'all'): 38, ('extra', 'linux'): 12, ('failure', 'linux'): 1, ('flow', 'all'): 99, ('flow', 'linux'): 1,
        ('flow-deep', 'linux'): 9, ('limit', 'linux'): 4, ('mac', 'all'): 2, ('race', 'all'): 21, ('race', 'linux'): 6, ('signal', 'all'): 1,
        ('signal', 'linux'): 1, ('u1', 'linux'): 1, ('u2', 'linux'): 1}},
    'mac': {'total': 161, 'groups': {
        ('extra', 'all'): 38, ('flow', 'all'): 99, ('mac', 'all'): 2, ('race', 'all'): 21, ('signal', 'all'): 1}},
}
# Linux だけの印を付けた項目の名前(Mac の経路では流さない)。36 件。
EXPECTED_LINUX_ONLY = (
    'bootstrap-refusals', 'depth-990', 'depth-991', 'depth-992-after-state', 'depth-992-bootstrap', 'depth-copy-992', 'depth-copy-993', 'u1-locale',
    'u2-guard-origin', 'x-deep-managed-989', 'x-deep-managed-990', 'x-deep-settings-989', 'x-dotfile-cycle', 'x-dropin-tree-link',
    'x-fsize-bootstrap', 'x-git-include-dotdot-symlink', 'x-install-link-agent-kind', 'x-limit-bytes-after',
    'x-limit-bytes-before', 'x-limit-entries-after', 'x-limit-entries-before', 'x-race-bootstrap-gitconfig-edit', 'x-race-bootstrap-include-target-edit',
    'x-race-bootstrap-new-include-target', 'x-race-bootstrap-new-managed-dropin', 'x-race-bootstrap-plugin-edit', 'x-race-bootstrap-settings-edit',
    'x-reused-output', 'x-settings-size-over', 'x-sigint-bootstrap', 'x-special-fifo-in-plugin', 'x-special-parent-symlink', 'x-special-settings-fifo',
    'x-special-settings-mode-0200', 'x-tilde-forms', 'x-tilde-home-unset',
)


def check_command_items(per_group, skipped, all_items=None):
    """流した項目の数が、機種ごとの期待(全項目数・グループごとの数・Linux だけにした項目の名前の一覧)と完全に一致することを確かめる。"""
    commands = commands_module()
    machine = 'mac' if commands.MAC_PATH else 'linux'
    if all_items is None:
        all_items = commands.items('/nonexistent-fixture')
    expected = EXPECTED_ITEMS[machine]
    actual = {key: count for key, (count, _) in per_group.items()}
    if actual != expected['groups']:
        raise AssertionError(f'9 コマンドの項目の数がグループごとの期待と違う({machine}): 実際 {sorted(actual.items())}・期待 {sorted(expected["groups"].items())}')
    total = sum(actual.values())
    if total != expected['total']:
        raise AssertionError(f'9 コマンドの項目の総数が期待と違う({machine}): 実際 {total}・期待 {expected["total"]}')
    linux_only = tuple(sorted(item.name for item in all_items if item.mark == commands.LINUX_ONLY))
    if linux_only != EXPECTED_LINUX_ONLY:
        raise AssertionError(f'Linux だけの印を付けた項目が期待と違う: 余分 {sorted(set(linux_only) - set(EXPECTED_LINUX_ONLY))[:5]}・不足 {sorted(set(EXPECTED_LINUX_ONLY) - set(linux_only))[:5]}')
    if commands.MAC_PATH and tuple(sorted(name for name, _ in skipped)) != EXPECTED_LINUX_ONLY:
        raise AssertionError('Mac の経路で流さなかった項目が、Linux だけの印の項目の一覧と違う')
    for item in all_items:
        if item.mark == commands.LINUX_ONLY and not item.reason:
            raise AssertionError(f'Linux だけの印の項目に理由が無い: {item.name}')
    return total


def check_mapping_references(known_names=None):
    """対応表が指す比較のケースの名前(`比較[…]`)と Rust のテストの名前(`<ファイル>.rs::<名前>`)が、実在することを確かめる。"""
    if known_names is None:
        known_names = {item.name for item in commands_module().items('/nonexistent-fixture')}
    count = 0
    for name, _, target, _ in TEST_MAPPING:
        for group in CASE_REF.findall(target):
            for case in (part.strip() for part in group.split(',')):
                if case not in known_names:
                    raise AssertionError(f'対応表 {name}: 比較のケース {case!r} が無い')
                count += 1
        for file, function in RUST_REF.findall(target):
            path = RUST_DIR / f'{file}.rs'
            if not path.is_file() or f'fn {function}(' not in path.read_text(encoding='utf-8'):
                raise AssertionError(f'対応表 {name}: Rust のテスト {file}.rs::{function} が無い')
            count += 1
    return count


def check_mapping():
    """対応表が既存のテストと過不足なく対応していることを確かめる。"""
    names = [name for _, name, _ in existing_test_names()]
    mapped = [row[0] for row in TEST_MAPPING]
    if sorted(names) != sorted(mapped) or len(set(mapped)) != len(mapped):
        missing = sorted(set(names) - set(mapped))
        extra = sorted(set(mapped) - set(names))
        raise AssertionError(f'対応表が既存のテストと合わない: 不足 {missing[:3]} 余分 {extra[:3]}')
    for name, category, target, _ in TEST_MAPPING:
        if category not in CATEGORY_NAMES or not target:
            raise AssertionError('分類か対応先が空')
        # 対応先が途中で切れた行を通さない(「プ」の 1 文字などの誤り)。旧新の比較・Rust の検査には、行き先を示す語が要る。
        if category != O and not any(word in target for word in TARGET_WORDS):
            raise AssertionError(f'対応表 {name}: 対応先に行き先を示す語({"・".join(TARGET_WORDS)})が無い(切れている): {target!r}')
        if category == O and len(target) < 10:
            raise AssertionError(f'対応表 {name}: 対象外の理由が短すぎる(切れている): {target!r}')
    return mapping_summary()


SIMULATION_NOTICE = 'S2E: Mac の経路の模擬(環境変数 S2E_SIMULATE_MAC=1)です。Linux の合格ではありません。Linux だけの項目は流しません'
SIMULATION_PASS = 'S2E: 模擬の合格(Mac の経路の模擬であり、Linux の合格ではない。Mac 実機の確認でもない)'
SIMULATION_EXIT = 3


def is_simulation():
    commands = commands_module()
    return commands.SIMULATE_MAC and not commands.IS_MAC


def finish(simulated):
    """最後の行と終了コード。Mac の経路の模擬は Linux の必須項目を流していないので、『S2E: 合格』と終了コード 0 にしない。"""
    if simulated:
        print(SIMULATION_PASS)
        return SIMULATION_EXIT
    print('S2E: 合格')
    return 0


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    global FIXTURE
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture')
    parser.add_argument('--write-mapping', action='store_true')
    args = parser.parse_args(argv)
    if args.write_mapping:
        check_mapping()
        MAPPING_MD.write_text(mapping_markdown(), encoding='utf-8')
        print(f'作成: {MAPPING_MD}')
        return 0
    if not args.fixture:
        parser.error('--fixture が必要です')
    FIXTURE = fixture_path(args.fixture)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('S2E 比較は Python 3.10 系・非 root で実行してください')
    simulated = is_simulation()
    if simulated:
        print(SIMULATION_NOTICE)
    started = time.monotonic()
    # 比較器自身の検査を先に通す。
    module = load_module(Path(__file__).with_name('test_s2e.py'), 's2e_comparator_tests')
    module.subject = sys.modules[__name__]
    self_checks = run_suite(unittest.defaultTestLoader.loadTestsFromModule(module))
    summary = check_mapping()
    if MAPPING_MD.read_text(encoding='utf-8') != mapping_markdown():
        raise AssertionError('test_mapping.md が TEST_MAPPING と違う(--write-mapping で作り直す)')
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        script = stage_distribution(base)
        work = base / 'work'
        work.mkdir()
        tally = run_contract(FIXTURE, all_cases(), old_script=script, cwd=work)
        # 9 コマンド(bootstrap・verify・host-settings・host-allowlist と内部 reader)の旧新比較。
        commands = commands_module()
        print('S2E: 9 コマンドの旧新比較を流す')
        started_commands = time.monotonic()
        cmd_tally, per_group, skipped, _ = commands.run_items(FIXTURE, os.path.realpath(folder))
        cmd_elapsed = time.monotonic() - started_commands
        executed = check_command_totals(cmd_tally, per_group)
        check_command_items(per_group, skipped)
    references = check_mapping_references()
    elapsed = time.monotonic() - started
    for name in tally['unimplemented']:
        print(f'未実装の印(件数に入れない): {name}')
    for name in PENDING_2B:
        print(f'予定の枠(2 段目 b。件数に入れない): {name}')
    print(f'S2E: 比較 {tally["matched"] + tally["approved"]} 件(通常の一致 {tally["matched"]}・認めた差 {tally["approved"]}。うち U4 {tally["u4"]})'
          f'・未実装の印 {len(tally["unimplemented"])} 件・予定の枠 {len(PENDING_2B)} 件・自己検査 {self_checks} 件'
          f'・既存テストの対応 {summary["total"]} 件(両方 {summary["counts"][B]}・置換 {summary["counts"][R]}・対象外 {summary["counts"][O]}。'
          f'うち予定を含まない {summary["compared"]}・予定 {summary["planned"]})')
    print(f'S2E: 9 コマンドの旧新比較 {executed} 項目(Rust を起動した比較 {cmd_tally.rust_runs} 件): 起動の一致 {cmd_tally.runs} 件(読取りだけのコマンドの前後確認 {cmd_tally.readonly} 件を別に確かめた)'
          f'・bootstrap の出力一式のバイト比較と受領の確認 {cmd_tally.bootstrap_bytes} 件・相互読取り {cmd_tally.cross} 件'
          f'・内部 reader の値の比較 {cmd_tally.readers} 件・認めた差(旧新それぞれの固定期待) {cmd_tally.approved} 件・比較から外した物 {len(cmd_tally.excluded)} 件')
    for (group, mark), (count, seconds) in sorted(per_group.items()):
        print(f'S2E:   {group}: {count} 項目・{seconds:.1f} 秒・印 {"全機種で必須" if mark == commands.ALL else "Linux だけ"}')
    for name, reason in skipped:
        print(f'S2E:   この機種では流さない(Linux だけの印): {name}。理由: {reason}')
    for text in cmd_tally.excluded:
        print(f'S2E:   比較から外した物: {text}')
    for text in cmd_tally.notes:
        print(f'S2E:   測った値: {text}')
    soft, hard = commands.inherited_nofile()
    print(f'S2E: 受け継いだ RLIMIT_NOFILE(比較器は設定しない): soft {soft}・hard {hard}')
    print(f'S2E: 対応表が指す比較のケースと Rust のテストの名前の確認 {references} 件')
    print(f'S2E: 所要時間 比較 {tally["elapsed"]:.1f} 秒・9 コマンド {cmd_elapsed:.1f} 秒・全体 {elapsed:.1f} 秒')
    return finish(simulated)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ValueError, OSError) as error:
        print(f'S2E: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
