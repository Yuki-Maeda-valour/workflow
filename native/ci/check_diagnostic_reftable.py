#!/usr/bin/env python3
"""R4D-1: 診断の reftable 読み取りの旧版(Python)と Rust の比較器。製品の入口ではない。

使い方(Rust の検査 native/crates/cli/tests/diagnostic_reftable.rs が起動する):
  --generate <空の一時フォルダ> --relative-base <フォルダ>
      入力の組を作り、旧版の read_stack と予算の操作を同じ条件で動かし、
      <一時フォルダ>/manifest.json に入力の一覧と旧版の結果を書く。
  --compare <manifest.json> <Rust の結果の JSON> [--expect-mismatch]
      全件の結果が並びまで一致し、件数が下限以上のときだけ終了値 0。
      --expect-mismatch は「量」の条件の入力だけを比べ、不一致が 1 件以上あるときだけ終了値 0。

製品の符号化を使わない。table は scripts/test_diagnostic_reftable.py の符号化の関数で作る。
前提: Python 3.10 系・root 以外の利用者・reftable を作れる git(2.45 以上)。欠ければ失敗にする(skip しない)。
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
LEGACY_DIR = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
# scripts/ は末尾に足す。先頭に足すと、同じ名前のテストの読み込み先を変えてしまう。
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
from support_load import load_script  # noqa: E402
from support_native import run_command  # noqa: E402

MIN_GIT = (2, 45)
MAX_INPUT = 200 * 1024 * 1024
RUN_LIMIT = 300000
NO_SELFTEST_ENV = 'R4D1_NO_SELFTEST'
ALLOW_MINI_ENV = 'R4D1_ALLOW_MINI'
GIT_TIMEOUT = 60

OPS = (('x01', lambda b: b ^ 0x01), ('x80', lambda b: b ^ 0x80),
       ('z00', lambda b: 0x00), ('xff', lambda b: 0xff))

# 件数の種類名(Task 2.2)。
GROUPS = ('g1-truncate', 'g1-mutate', 'g1h-field', 'g2-representative', 'g3-boundary',
          'g4-argument', 'g5-budget', 'g6-git-stack', 'g7-nonutf8-directory')
PROPERTIES = ('table-object-section', 'git-sha1', 'git-sha256', 'git-multi-table',
              'git-multi-block-index', 'git-tombstone', 'git-symref', 'git-stash',
              'git-worktree', 'git-for-each-ref-common', 'git-for-each-ref-worktree',
              'nonutf8-synthetic', 'nonutf8-git')
# G2〜G5 の名前付きの入力の一覧が、各項目を 1 件以上含むことの確かめ。
REQUIRED_TAGS = {
    'g2-representative': ('versions-hash-formats', 'tombstone', 'empty-stack', 'empty-table',
                          'aligned-blocks', 'log-only', 'two-level-index', 'refs-2001',
                          'footer-broken', 'section-bounds', 'block-broken', 'list-grammar',
                          'stack-order', 'missing-table', 'symlink-table'),
    'g3-boundary': ('total-limit', 'list-line', 'name-length', 'target-length', 'varint',
                    'broken-table-then-list', 'broken-table-and-order'),
    'g4-argument': ('replaced-table', 'replaced-list', 'missing-directory', 'missing-table',
                    'unreadable', 'link-in-path', 'trailing-slash', 'relative-path', 'nul-path',
                    'empty-path', 'hash-format'),
    'g5-budget': ('limits', 'overflow', 'deadline', 'stop', 'remaining'),
}


class CheckError(Exception):
    """比較器の失敗。メッセージを標準エラーへ出して終了値 1 にする。"""


# ---------------------------------------------------------------- 前提の検査

def git_version():
    result = run_command(['git', '--version'], cwd=os.getcwd(), timeout=GIT_TIMEOUT)
    text = result.stdout.decode('utf-8', 'replace').strip()
    match = re.match(r'git version (\d+)\.(\d+)\.(\d+)', text)
    if result.returncode != 0 or not match:
        raise CheckError('git の版を読めない: %r' % text)
    return tuple(int(part) for part in match.groups()), text.split(' ', 2)[2].split(' ')[0]


def check_prerequisites():
    """Python 3.10 系・root 以外・git 2.45 以上。欠ければ CheckError(skip にしない)。返す値は git の版の文字列。"""
    problems = []
    if sys.version_info[:2] != (3, 10):
        problems.append('Python 3.10 系が要る(今は %d.%d)' % sys.version_info[:2])
    if os.geteuid() == 0:
        problems.append('root 以外の利用者で動かす必要がある(権限の検査が意味を失う)')
    version, text = git_version()
    if version < MIN_GIT:
        problems.append('git の版が足りない: %s(reftable を作れる 2.45 以上が要る)' % text)
    if problems:
        raise CheckError('R4D-1 前提が満たされない: ' + ' / '.join(problems))
    return text


# ---------------------------------------------------------------- 旧版と符号化

_modules = {}


def legacy():
    """旧版の reftable・予算の部品と、独立した符号化の関数。"""
    if not _modules:
        _modules['rd'] = load_script(LEGACY_DIR / 'diagnostic-reftable.py', 'r4d1_legacy_reftable')
        _modules['rt'] = load_script(LEGACY_DIR / 'diagnostic-runtime.py', 'r4d1_legacy_runtime')
        _modules['enc'] = load_script(SCRIPTS / 'test_diagnostic_reftable.py', 'r4d1_legacy_encoders')
    return _modules['rd'], _modules['rt'], _modules['enc']


def uint(value, size):
    return value.to_bytes(size, 'big')


def index_entry(name, offset, enc=None):
    """index block の記録(値の種類 0 と、子の block の位置)。"""
    enc = enc or legacy()[2]
    return b'\0' + enc.varint(len(name) << 3) + name + enc.varint(offset)


def with_footer(header, body, fields):
    footer = header + b''.join(uint(value, 8) for value in fields)
    return body + footer + uint(zlib.crc32(footer), 4)


def build_t7():
    """2 段の index。参照 3 件(Rust の reftable.rs の T7_TABLE と同じバイト列)。"""
    enc = legacy()[2]
    header = enc.table()[:24]
    a = enc.ref_block([enc.record(b'refs/heads/a', value=b'\x11' * 20)], header)
    b = enc.ref_block([enc.record(b'refs/heads/b', value=b'\x22' * 20)])
    c = enc.ref_block([enc.record(b'refs/heads/c', 3, b'refs/heads/a')])
    lower_position = len(a) + len(b) + len(c)
    lower = enc.ref_block([index_entry(b'refs/heads/a', 0), index_entry(b'refs/heads/b', len(a)),
                           index_entry(b'refs/heads/c', len(a) + len(b))], kind=b'i')
    body = a + b + c + lower
    upper_position = len(body)
    body += enc.ref_block([index_entry(b'refs/heads/c', lower_position)], kind=b'i')
    return with_footer(header, body, [upper_position, 0, 0, 0, 0])


def build_log_only(version, fmt):
    enc = legacy()[2]
    header = enc.table(version=version, fmt=b's256' if version == 2 else b'sha1')[:24 if version == 1 else 28]
    key = b'refs/heads/main\0' + uint((1 << 64) - 2, 8)
    entry = b'\0' + enc.varint(len(key) << 3) + key
    expanded = entry + uint(len(header) + 4, 3) + uint(1, 2)
    log = header + b'g' + uint(len(header) + 4 + len(expanded), 3) + zlib.compress(expanded)
    footer = header + b'\0' * 40
    return log + footer + uint(zlib.crc32(footer), 4)


def hdr(table_bytes):
    return 24 if table_bytes[4] == 1 else 28


def table_info(data):
    """footer の欄: (header の大きさ, [ref_index, object, object_index, log, log_index], object の長さ)。"""
    size = hdr(data)
    footer = data[-(size + 44):]
    fields = [int.from_bytes(footer[size + i * 8:size + i * 8 + 8], 'big') for i in range(5)]
    return size, [fields[0], fields[1] >> 5, fields[2], fields[3], fields[4]], fields[1] & 31


def mutate_header_or_field(data, index, op):
    """header(header の大きさ個)と footer の欄(40 個)の通し番号 index の 1 バイトを変え、footer の header と CRC を作り直す。"""
    size = hdr(data)
    footer_start = len(data) - (size + 44)
    head = bytearray(data[:size])
    fields = bytearray(data[footer_start + size:footer_start + size + 40])
    if index < size:
        head[index] = op(head[index])
    else:
        fields[index - size] = op(fields[index - size])
    body = bytearray(data[:footer_start])
    body[:size] = head
    footer = bytes(head) + bytes(fields)
    return bytes(body) + footer + uint(zlib.crc32(footer), 4)


def mutate_byte(data, position, op):
    changed = bytearray(data)
    changed[position] = op(changed[position])
    return bytes(changed)


# ---------------------------------------------------------------- 予算の条件と旧版の実行

class Clock:
    """時計を呼んだ回数を数える。mode: const(いつも 0)・expired(2 回目から 1000)・seq(呼ぶたびに 0,1,2,...)。"""

    def __init__(self, mode='const'):
        self.mode = mode
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.mode == 'const':
            return 0.0
        if self.mode == 'expired':
            return 0.0 if self.calls == 1 else 1000.0
        return float(self.calls - 1)


def result_of(call):
    """旧版の呼び出しの結果を、JSON にできる形にする。成功は並びを保った一覧、失敗は種類と理由と終了値。"""
    rd, rt, _ = legacy()
    try:
        value = call()
    except rd.ReftableError as error:
        return ['err', 'reftable', error.reason, error.code]
    except rt.Failure as error:
        return ['err', 'budget', error.reason, error.code]
    except ValueError:
        return ['err', 'other', 'ValueError', 0]
    except BaseException as error:  # 想定外の例外は、旧版の結果として記録し、Rust と合わなければ不一致になる
        return ['err', 'other', type(error).__name__, 0]
    return ['ok', [[name.hex(), oid, None if target is None else target.hex()]
                   for name, (oid, target) in value.items()]]


def make_budget(cond, clock=None):
    """条件 ['normal'] ['expired'] ['stop'] ['n', N] ['q', L] から旧版の予算を作る。"""
    rd, rt, _ = legacy()
    name = cond[0]
    if name == 'normal':
        return rt.Budget(clock=clock or Clock('const'), seconds=300.0)
    if name == 'expired':
        return rt.Budget(clock=clock or Clock('expired'), seconds=300.0)
    if name == 'stop':
        budget = rt.Budget(clock=clock or Clock('const'), seconds=300.0)
        budget.stopped = True
        return budget
    if name == 'n':
        return rt.Budget(clock=clock or Clock('seq'), seconds=float(cond[1]))
    if name == 'q':
        limits = dict(rt.LIMITS)
        limits['management_output'] = cond[1]
        return rt.Budget(clock=clock or Clock('const'), seconds=300.0, limits=limits)
    raise ValueError(cond)


def recording_budget():
    """通常の実行。確かめの回数(時計の回数)と、記録ごとの management_output の累計を取る。"""
    rd, rt, _ = legacy()
    clock = Clock('const')

    class Recording(rt.Budget):
        cumulative = []

        def charge(self, kind, count):
            super().charge(kind, count)
            if kind == 'management_output':
                self.cumulative.append(self.used[kind])

    budget = Recording(clock=clock, seconds=300.0)
    budget.cumulative = []
    return budget, clock


def evenly(k):
    return [max(1, round(j * (k + 1) / 10)) for j in range(1, 11)]


def conditions(plan, k, cumulative):
    """旧版の「通常」の実行の確かめの回数 k と累計から、予算の条件の並びを作る。"""
    total = cumulative[-1] if cumulative else 0
    conds = [['normal']]

    def add(cond):
        if cond not in conds:
            conds.append(cond)

    def quantities(values):
        for value in values:
            if value >= 0:
                add(['q', value])
    if plan == 'normal':
        return conds
    if plan == 'full':
        quantities([0, 1])
        for value in cumulative:
            quantities([value, value - 1])
        for n in range(1, k + 2):
            add(['n', n])
    elif plan == 'totals':
        quantities([total, total - 1])
    elif plan == 'header':
        quantities([0, 1, total, total - 1])
        for n in (1, -(-k // 2), k, k + 1):
            add(['n', n])
    elif plan == 'representative':
        add(['expired'])
        add(['stop'])
        quantities([total, total - 1])
        for n in [1, k, k + 1] + evenly(k):
            add(['n', n])
    elif plan == 'section':
        quantities([total, total - 1])
        for n in range(1, k + 2):
            add(['n', n])
    elif plan == 'guard':
        add(['stop'])
        add(['expired'])
    else:
        raise ValueError(plan)
    return conds


def tree_signature(path):
    """フォルダとその直下の実体の状態の要約(大きさ・時刻・種類・番号)。読んだ後に変わっていないことの確かめ。"""
    parts = []
    try:
        entries = sorted(os.scandir(path), key=lambda e: os.fsencode(e.name))
    except OSError:
        return None
    info = os.lstat(path)
    parts.append(('.', info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        parts.append((os.fsdecode(entry.name), info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


# ---------------------------------------------------------------- 入力の組み立て

class Builder:
    """入力のフォルダを一時フォルダに作り、旧版を動かして、manifest の材料を集める。"""

    def __init__(self, root, base):
        self.root = Path(os.path.realpath(root))
        self.base = Path(os.path.realpath(base))
        self.inputs = []
        self.named = {name: [] for name in REQUIRED_TAGS}
        self.tables = {}
        self.runs = 0
        self.by_group_runs = {}
        self.not_created = []
        self.git_stacks = []
        self.git_notes = {'common_compared': 0, 'worktree_compared': 0, 'excluded_dangling': 0}
        self.signatures = {}
        (self.root / 'in').mkdir()

    def directory(self, name):
        path = self.root / 'in' / name
        path.mkdir(parents=True)
        return path

    def stack_dir(self, name, files, listing):
        """files: {名前: bytes または 大きさ(穴の空いたファイル)}。listing: tables.list の内容(bytes)。"""
        path = self.directory(name)
        for filename, content in files.items():
            with open(path / filename, 'wb') as handle:
                if isinstance(content, int):
                    handle.truncate(content)
                else:
                    handle.write(content)
        if listing is not None:
            (path / 'tables.list').write_bytes(listing)
        return path

    def add(self, name, group, path, plan='normal', fmt='sha1', props=(), tag=None, path_bytes=None):
        """path のフォルダを入力にする。旧版を動かし、結果を記録する。path_bytes は旧版に渡すパス(既定は path)。"""
        shown = os.fsencode(path) if path_bytes is None else path_bytes
        # str・PathLike・bytes(UTF-8 でないパス)のどれも、読んだ後に変わっていないことの確かめの対象にする。
        watch = path is not None and os.path.isdir(path) and not os.path.islink(path)
        before = tree_signature(path) if watch else None
        budget, clock = recording_budget()
        normal = result_of(lambda: legacy()[0].read_stack(shown, fmt, budget))
        checks = clock.calls - 1
        conds = conditions(plan, checks, budget.cumulative)
        results = [normal]
        for cond in conds[1:]:
            results.append(result_of(lambda cond=cond: legacy()[0].read_stack(shown, fmt, make_budget(cond))))
        self.runs += len(conds)
        self.by_group_runs[group] = self.by_group_runs.get(group, 0) + len(conds)
        if self.runs > RUN_LIMIT:
            raise CheckError('旧版の実行の回数が上限 %d 回を超えた(%s の途中)。件数を減らす順は設計に従う' % (RUN_LIMIT, name))
        if before is not None:
            after = tree_signature(path)
            if before != after:
                raise CheckError('読んだ後に入力のフォルダが変わった: %s' % name)
            self.signatures[name] = [os.fsencode(path).hex(), after]  # UTF-8 でないパスも JSON に書けるよう 16 進にする
        record = {'id': name, 'group': group, 'props': sorted(props), 'path': shown.hex(), 'hash': fmt,
                  'conds': conds, 'legacy': results}
        if tag:
            record['tag'] = tag
            self.named[group].append(tag)
        self.inputs.append(record)
        return normal


def listing_of(*names):
    return b''.join(n.encode() + b'\n' for n in names)


# ---- G1: 小さい table(全数)

def small_tables():
    """T1〜T8 の合成の table と、T5・T6 の stack の相手。T8・T9 は git で作る(git_tables)。"""
    enc = legacy()[2]
    record, table = enc.record, enc.table
    t = {}
    t['T1'] = table([[record(b'refs/heads/a', value=b'\x11' * 20), record(b'refs/heads/b', value=b'\x12' * 20),
                      record(b'refs/tags/v1', 2, b'\x13' * 40)]], version=1, index=1)
    t['T2'] = table([[record(b'HEAD', 3, b'refs/heads/main'), record(b'refs/heads/main', value=b'\x21' * 20)]],
                    version=2, fmt=b'sha1', index=1)
    t['T3'] = table([[record(b'refs/heads/a', value=b'\x31' * 32), record(b'refs/heads/b', 2, b'\x32' * 64)]],
                    version=2, fmt=b's256', index=1)
    t['T4'] = table([[record(b'refs/heads/a', value=b'\x41' * 20),
                      record(b'refs/heads/\xff\xfe', 3, b'refs/heads/\xfe\xff'),
                      record(b'refs/tags/\x80', value=b'\x42' * 20)]], version=1, index=1)
    t['T5'] = table([[record(b'refs/heads/a', value=b'\x51' * 20), record(b'refs/heads/b', value=b'\x52' * 20),
                      record(b'refs/heads/c', value=b'\x53' * 20)]], index=1)
    t['T6'] = table([[record(b'refs/heads/a', 0), record(b'refs/heads/b', value=b'\x62' * 20),
                      record(b'refs/heads/d', value=b'\x64' * 20)]], index=2)
    t['T7'] = build_t7()
    return t


GIT_ENV_FIXED = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_AUTHOR_NAME': 'R4D', 'GIT_AUTHOR_EMAIL': 'r4d@example.invalid',
    'GIT_COMMITTER_NAME': 'R4D', 'GIT_COMMITTER_EMAIL': 'r4d@example.invalid',
    'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z',
    'GIT_TEST_REFTABLE_AUTOCOMPACTION': 'false', 'GIT_TERMINAL_PROMPT': '0',
    'LC_ALL': 'C', 'GIT_PAGER': 'cat',
}


class Repo:
    """利用者の設定を読ませない環境の git のリポジトリ。git の各呼び出しに上限時間(1 回 60 秒)を付ける。"""

    def __init__(self, path, home, fmt='sha1', config=()):
        self.path = Path(path)
        self.env = dict(GIT_ENV_FIXED, HOME=str(home), PATH=os.environ.get('PATH', ''))
        self.fmt = fmt
        self.git('init', '-q', '-b', 'main', '--ref-format=reftable', '--object-format=' + fmt, str(self.path),
                 cwd=str(home))
        for key, value in config:
            self.git('config', key, value)
        for key, value in (('gc.auto', '0'), ('maintenance.auto', 'false'), ('commit.gpgSign', 'false'),
                           ('core.logAllRefUpdates', 'true')):
            self.git('config', key, value)

    def git(self, *args, cwd=None, input=None, check=True):
        argv = ['git'] + [a if isinstance(a, bytes) else str(a) for a in args]
        argv = [os.fsencode(a) if isinstance(a, bytes) else a for a in argv]
        result = run_command(argv, cwd=cwd or str(self.path), env=self.env, input=input, timeout=GIT_TIMEOUT)
        if result.timed_out:
            raise CheckError('git が %d 秒の上限に当たった: %r' % (GIT_TIMEOUT, argv[1:4]))
        if check and result.returncode != 0:
            raise CheckError('git が失敗した(%d): %r %s' % (result.returncode, argv[1:5],
                                                          result.stderr.decode('utf-8', 'replace')[:300]))
        return result.stdout

    def commit(self, name='f', text='x'):
        (self.path / name).write_text(text)
        self.git('add', name)
        self.git('commit', '-q', '-m', 'c ' + text)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def update(self, commands):
        """update-ref --stdin で 1 回の取引にする。commands は 'create <ref> <oid>' などの行(bytes)の並び。"""
        self.git('update-ref', '--stdin', input=b'\n'.join(commands) + b'\n')

    def reftable(self, worktree=None):
        if worktree is None:
            return self.path / '.git' / 'reftable'
        return self.path / '.git' / 'worktrees' / worktree / 'reftable'


def newest_table(repo):
    names = (repo.reftable() / 'tables.list').read_text().split()
    return (repo.reftable() / names[-1]).read_bytes()


def git_tables(builder, home):
    """T8(sha1・1.2KB 以下)と T9(sha256・3.2KB 以下)。本物の git が作った table を 1 つ取り出す。"""
    repo = Repo(Path(home) / 't8', home, 'sha1', [('reftable.restartInterval', '2')])
    repo.commit()
    oid = repo.git('rev-parse', 'HEAD').decode().strip()
    repo.update([('create refs/heads/feature/%s %s' % (name, oid)).encode()
                 for name in ('alpha', 'alpine', 'beta', 'gamma')] +
                [('create refs/tags/v%d %s' % (n, oid)).encode() for n in (1, 2)])
    t8 = newest_table(repo)
    repo = Repo(Path(home) / 't9', home, 'sha256', [('reftable.blockSize', '256')])
    repo.commit()
    oid = repo.git('rev-parse', 'HEAD').decode().strip()
    repo.update([('create refs/heads/branch-%02d %s' % (n, oid)).encode() for n in range(1, 21)])
    t9 = newest_table(repo)
    return t8, t9


def check_git_tables(tables):
    """T8・T9 の大きさ・合計・T9 の 4 つの section の位置を確かめ、満たさなければ失敗にする。"""
    problems = []
    if len(tables['T8']) > 1200:
        problems.append('T8 が 1.2KB を超えた(%d バイト)' % len(tables['T8']))
    if len(tables['T9']) > 3200:
        problems.append('T9 が 3.2KB を超えた(%d バイト)' % len(tables['T9']))
    total = sum(len(v) for v in tables.values())
    if total > 6144:
        problems.append('T1〜T9 の合計が 6KB を超えた(%d バイト)' % total)
    size, fields, _ = table_info(tables['T9'])
    for label, value in zip(('ref index', 'object', 'object index', 'log', 'log index'), fields):
        if label != 'object index' and value == 0:
            problems.append('T9 の footer の %s の位置が 0' % label)
    if problems:
        raise CheckError('R4D-1 T8・T9 の確かめに失敗: ' + ' / '.join(problems))


def add_g1(builder, tables):
    """T1〜T9 の切り詰め・変異・header と footer の欄の変異。"""
    partner = {'T5': ('T6', 1), 'T6': ('T5', 0)}
    for tname, data in tables.items():
        size, fields, object_length = table_info(data)
        builder.tables[tname] = {'size': len(data), 'header_size': size,
                                 'object_section': fields[1] != 0}
        props = ('nonutf8-synthetic',) if tname == 'T4' else ()

        def build(body, tname=tname):
            """body を置いた stack のフォルダを作る(T5・T6 は、もう一方をそのまま残す)。"""
            if tname == 'T5':
                return {'table-0.ref': body, 'table-1.ref': tables['T6']}, listing_of('table-0.ref', 'table-1.ref')
            if tname == 'T6':
                return {'table-0.ref': tables['T5'], 'table-1.ref': body}, listing_of('table-0.ref', 'table-1.ref')
            return {'table-0.ref': body}, listing_of('table-0.ref')

        fmt = 'sha256' if tname in ('T3', 'T9') else 'sha1'
        for cut in range(len(data) + 1):
            name = 'g1t-%s-%05d' % (tname, cut)
            files, listing = build(data[:cut])
            builder.add(name, 'g1-truncate', builder.stack_dir(name, files, listing), 'full', fmt, props)
        heads = []
        if tname == 'T9':
            ref_index, object_position, _, log_position, log_index = fields
            heads = [p + i for p in (ref_index, object_position, log_position, log_index) for i in range(4)]
        for position in range(len(data)):
            for op_name, op in OPS:
                name = 'g1m-%s-%05d-%s' % (tname, position, op_name)
                if tname in ('T7', 'T8') and op_name == 'x01':
                    plan = 'full'
                elif tname == 'T9':
                    plan = 'section' if (op_name == 'x01' and position in heads) else 'normal'
                elif tname == 'T7' or tname == 'T8':
                    plan = 'totals'
                else:
                    plan = 'totals'
                files, listing = build(mutate_byte(data, position, op))
                builder.add(name, 'g1-mutate', builder.stack_dir(name, files, listing), plan, fmt, props)
        for index in range(size + 40):
            for op_name, op in OPS:
                name = 'g1h-%s-%03d-%s' % (tname, index, op_name)
                files, listing = build(mutate_header_or_field(data, index, op))
                builder.add(name, 'g1h-field', builder.stack_dir(name, files, listing), 'header', fmt, props)


# ---- G2〜G5・G7

def add_g2(builder, tables):
    enc = legacy()[2]
    record, table, ref_block = enc.record, enc.table, enc.ref_block

    def one(name, tag, files, listing, plan='representative', fmt='sha1', props=()):
        builder.add(name, 'g2-representative', builder.stack_dir(name, files, listing), plan, fmt, props, tag)

    def single(name, tag, data, fmt='sha1', props=(), plan='representative'):
        one(name, tag, {'table-0.ref': data}, listing_of('table-0.ref'), plan, fmt, props)

    # 既存の 12 件の検査と同じ内容。
    for version, fmt, size in [(1, 'sha1', 20), (2, 'sha1', 20), (2, 'sha256', 32)]:
        value, name, target = b'\x12' * size, b'refs/heads/\xff', b'refs/heads/\xfe'
        single('g2-versions-%d-%s' % (version, fmt), 'versions-hash-formats',
               table([[record(b'refs/heads/a', value=value), record(name, 3, target)]], version=version,
                     fmt=b's256' if size == 32 else b'sha1'), fmt, ('nonutf8-synthetic',))
    one('g2-tombstone', 'tombstone', {'table-0.ref': table([[record(b'a', value=b'1' * 20), record(b'b', value=b'2' * 20)]], index=1),
                                      'table-1.ref': table([[record(b'a', 0), record(b'b', value=b'3' * 20)]], index=2)},
        listing_of('table-0.ref', 'table-1.ref'))
    one('g2-empty-stack', 'empty-stack', {}, b'')
    single('g2-empty-table', 'empty-table', table())
    single('g2-aligned', 'aligned-blocks',
           table([[record(b'a', value=b'1' * 20)], [record(b'b', value=b'2' * 20)]], alignment=256))
    for version, fmt in [(1, 'sha1'), (2, 'sha256')]:
        single('g2-logonly-%d' % version, 'log-only', build_log_only(version, fmt), fmt)
    header = table()[:24]
    first = ref_block([record(b'a', value=b'1' * 20)], header)
    second = ref_block([record(b'b', value=b'2' * 20)])
    lower_position = len(first) + len(second)
    lower = ref_block([index_entry(b'a', 0), index_entry(b'b', len(first))], kind=b'i')
    for upper in (False, True):
        body = first + second + lower
        index_position = lower_position
        if upper:
            index_position = len(body)
            body += ref_block([index_entry(b'b', lower_position)], kind=b'i')
        single('g2-index-%s' % ('two' if upper else 'one'), 'two-level-index', with_footer(header, body, [index_position, 0, 0, 0, 0]))
    single('g2-t7', 'two-level-index', tables['T7'])
    for count in (2001, 2500):
        records = [record(('refs/heads/r%04d' % n).encode(), value=b'a' * 20) for n in range(count)]
        single('g2-refs-%d' % count, 'refs-2001', table([records]))
    good = table([[record(b'a', value=b'1' * 20)]])
    crc = good[:-1] + bytes([good[-1] ^ 1])
    header_bad = b'BAD!' + good[4:]
    section = table([[record(b'a', value=b'1' * 20)]], fields=[999999, 0, 0, 0, 0])
    for key, broken in (('crc', crc), ('header', header_bad), ('short4', good[:4]), ('shortfoot', good[:-20])):
        single('g2-footer-%s' % key, 'footer-broken', broken)
    single('g2-section-bounds', 'section-bounds', section)
    single('g2-hash-mismatch', 'footer-broken', good, 'sha256')
    footer_size = 68
    restart = bytearray(good)
    restart[-footer_size - 5:-footer_size - 2] = uint(1, 3)
    prefix = bytearray(good)
    prefix[28] = 1
    unknown = table([[record(b'a', kind=7)]])
    excessive = table([[b'\xff' * 11 + b'\0']])
    for key, broken in (('restart', bytes(restart)), ('prefix', bytes(prefix)), ('unknown', unknown), ('varint', excessive)):
        single('g2-block-%s' % key, 'block-broken', broken)
    one('g2-order', 'stack-order', {'table-0.ref': table(index=2), 'table-1.ref': table(index=1)},
        listing_of('table-0.ref', 'table-1.ref'))
    for n, raw in enumerate((b'../outside\n', b'table-0.ref\ntable-0.ref\n', b'table-0.ref', b'\n', b'.\n', b'tables.list\n', b'a/b\n', b'..\n')):
        one('g2-list-%d' % n, 'list-grammar', {'table-0.ref': table()}, raw)
    one('g2-missing-table', 'missing-table', {}, listing_of('table-0.ref'))
    path = builder.stack_dir('g2-symlink-table', {'other': table()}, listing_of('table-0.ref'))
    os.symlink(path / 'other', path / 'table-0.ref')
    builder.add('g2-symlink-table', 'g2-representative', path, 'representative', 'sha1', (), 'symlink-table')
    single('g2-t4-nonutf8', 'versions-hash-formats', tables['T4'], props=('nonutf8-synthetic',))


def add_g3(builder):
    enc = legacy()[2]
    record, table, ref_block = enc.record, enc.table, enc.ref_block

    def one(name, tag, files, listing, fmt='sha1'):
        builder.add(name, 'g3-boundary', builder.stack_dir(name, files, listing), 'normal', fmt, (), tag)

    # tables.list の 2 バイト('x' と改行)と合わせて、合計が上限ちょうどと上限+1。
    for size, suffix in ((MAX_INPUT - 2, 'exact'), (MAX_INPUT - 1, 'over')):
        one('g3-total-%s' % suffix, 'total-limit', {'x': size}, b'x\n')
    long_name = 'x' * 4096
    one('g3-list-4096', 'list-line', {}, long_name.encode() + b'\n')
    one('g3-list-4097', 'list-line', {}, (long_name + 'x').encode() + b'\n')
    for length in (1024, 1025):
        name = b'r' * length
        one('g3-name-%d' % length, 'name-length', {'table-0.ref': table([[record(name, value=b'1' * 20)]])},
            listing_of('table-0.ref'))
        target = b't' * length
        one('g3-target-%d' % length, 'target-length', {'table-0.ref': table([[record(b'a', 3, target)]])},
            listing_of('table-0.ref'))
    one('g3-varint-unterminated', 'varint', {'table-0.ref': table([[b'\xff' * 11 + b'\0']])}, listing_of('table-0.ref'))
    one('g3-varint-overflow-prefix', 'varint', {'table-0.ref': table([[b'\xff' * 9 + b'\x7f' + b'\0']])},
        listing_of('table-0.ref'))
    one('g3-varint-overflow-delta', 'varint', {'table-0.ref': table([[b'\0' + enc.varint(1 << 3) + b'a' + b'\xff' * 9 + b'\x7f']])},
        listing_of('table-0.ref'))
    broken = table([[record(b'a', value=b'1' * 20)]])[:-3]
    one('g3-broken-then-list', 'broken-table-then-list', {'table-0.ref': broken}, b'table-0.ref\n/bad\n')
    one('g3-broken-and-order', 'broken-table-and-order', {'table-0.ref': table(index=2)[:-3], 'table-1.ref': table(index=1)},
        listing_of('table-0.ref', 'table-1.ref'))
    one('g3-good-then-bad-line', 'broken-table-then-list', {'table-0.ref': table()}, b'table-0.ref\n/bad\n')


def add_g4(builder):
    enc = legacy()[2]
    record, table = enc.record, enc.table
    good = table([[record(b'refs/heads/a', value=b'1' * 20)]])

    def stack(name):
        return builder.stack_dir(name, {'table-0.ref': good}, listing_of('table-0.ref'))

    def add(name, tag, path, plan='normal', fmt='sha1', path_bytes=None):
        builder.add(name, 'g4-argument', path, plan, fmt, (), tag, path_bytes)

    # ファイルの代わりのリンク・FIFO・フォルダ(table と tables.list の両方)。
    for kind in ('symlink', 'fifo', 'dir'):
        for target_name, tag in (('table-0.ref', 'replaced-table'), ('tables.list', 'replaced-list')):
            name = 'g4-%s-%s' % (kind, target_name.replace('.', '-'))
            path = stack(name)
            victim = path / target_name
            keep = path / 'keep'
            keep.write_bytes(good if target_name == 'table-0.ref' else listing_of('table-0.ref'))
            victim.unlink()
            if kind == 'symlink':
                os.symlink(keep, victim)
            elif kind == 'fifo':
                os.mkfifo(victim)
            else:
                victim.mkdir()
            add(name, tag, path)
    add('g4-missing-directory', 'missing-directory', None, path_bytes=os.fsencode(builder.root / 'in' / 'g4-no-such-directory'))
    path = builder.stack_dir('g4-missing-table', {}, listing_of('table-0.ref'))
    add('g4-missing-table', 'missing-table', path)
    path = builder.stack_dir('g4-missing-list', {'table-0.ref': good}, None)
    add('g4-missing-list', 'missing-table', path)
    for victim in ('table-0.ref', 'tables.list'):
        name = 'g4-unreadable-%s' % victim.replace('.', '-')
        path = stack(name)
        os.chmod(path / victim, 0)
        add(name, 'unreadable', path)  # 権限は戻さない(Rust も同じ読めない状態で読む。ファイルは親フォルダの権限で消せる)
    # 途中の段がリンクのパスと、末尾にスラッシュの付いたリンクのパス。
    real = stack('g4-link-real')
    parent = builder.directory('g4-link-parent')
    shutil.copytree(real, parent / 'stack')
    holder = builder.directory('g4-link-holder')
    os.symlink(parent, holder / 'up')
    add('g4-link-in-path', 'link-in-path', holder / 'up' / 'stack', path_bytes=os.fsencode(holder / 'up' / 'stack'))
    add('g4-link-final', 'link-in-path', holder / 'up', path_bytes=os.fsencode(holder / 'up'))
    link = builder.root / 'in' / 'g4-link-to-stack'
    os.symlink(real, link)
    add('g4-trailing-slash', 'trailing-slash', link, path_bytes=os.fsencode(link) + b'/')
    add('g4-trailing-slash-none', 'trailing-slash', link, path_bytes=os.fsencode(link))
    # --relative-base から見た相対パス(両方の実際のパスから作り、dev と ino が同じことを確かめる)。
    relative = os.path.relpath(os.path.realpath(real), builder.base)
    cwd = os.getcwd()
    os.chdir(builder.base)
    try:
        same = os.stat(relative)
        absolute = os.stat(real)
        if (same.st_dev, same.st_ino) != (absolute.st_dev, absolute.st_ino):
            raise CheckError('相対パスと絶対パスが同じ実体を指さない: %s' % relative)
    finally:
        os.chdir(cwd)
    add('g4-relative-path', 'relative-path', real, path_bytes=os.fsencode(relative))
    add('g4-relative-dot', 'relative-path', real, path_bytes=b'.')
    # パスに NUL・空のパス・誤ったハッシュの形式(順の確認に停止と期限切れも当てる)。
    nul = os.fsencode(real) + b'\0x'
    add('g4-nul-path', 'nul-path', None, 'guard', path_bytes=nul)
    add('g4-nul-path-bad-hash', 'nul-path', None, 'guard', 'sha512', path_bytes=nul)
    add('g4-empty-path', 'empty-path', None, 'normal', path_bytes=b'')
    for fmt in ('SHA1', '', 'sha512'):
        add('g4-hash-%s' % (fmt or 'empty'), 'hash-format', real, 'guard', fmt)
    add('g4-hash-sha256-on-sha1', 'hash-format', real, 'normal', 'sha256')


def add_g7(builder, tables):
    """UTF-8 でないバイトを名前に含むフォルダの下の stack(Linux だけ。Mac の APFS は名前を拒む)。"""
    raw = os.fsencode(builder.root / 'in') + b'/g7-\xff\xfe-dir'
    try:
        os.mkdir(raw)
        os.mkdir(raw + b'/stack')
        (Path(os.fsdecode(raw + b'/stack'))).joinpath('table-0.ref').write_bytes(tables['T1'])
        (Path(os.fsdecode(raw + b'/stack'))).joinpath('tables.list').write_bytes(listing_of('table-0.ref'))
    except OSError as error:
        builder.not_created.append('g7-nonutf8-directory(%s)' % sys.platform)
        print('R4D-1 作れなかった入力: g7-nonutf8-directory(%s)' % sys.platform, file=sys.stderr)
        return
    stack_path = raw + b'/stack'
    builder.add('g7-nonutf8-directory', 'g7-nonutf8-directory', stack_path, 'normal', 'sha1', (), None,
                path_bytes=stack_path)


# ---- G5: 予算だけの操作

KINDS = ('management_copy', 'management_output', 'management_entries', 'worktree_entries', 'body',
         'patch_copy', 'git_output', 'stderr', 'json')


def budget_cases():
    rd, rt, _ = legacy()
    cases = []
    for kind in KINDS:
        limit = rt.LIMITS[kind]
        cases.append({'id': 'g5-limit-%s' % kind, 'tag': 'limits', 'start': 0.0, 'seconds': 300.0,
                      'ops': [['charge', kind, limit - 1], ['charge', kind, 1], ['charge', kind, 1],
                              ['charge', kind, 0], ['check']]})
        cases.append({'id': 'g5-whole-%s' % kind, 'tag': 'limits', 'start': 0.0, 'seconds': 300.0,
                      'ops': [['charge', kind, limit], ['charge', kind, 1]]})
    cases.append({'id': 'g5-overflow', 'tag': 'overflow', 'start': 0.0, 'seconds': 300.0,
                  'ops': [['charge', 'body', 2 ** 64 - 1], ['charge', 'body', 2 ** 64 - 1], ['charge', 'json', 2 ** 64 - 1],
                          ['charge', 'stderr', 1]]})
    cases.append({'id': 'g5-overflow-sum', 'tag': 'overflow', 'start': 0.0, 'seconds': 300.0,
                  'ops': [['charge', 'management_output', 2 ** 63], ['charge', 'management_output', 2 ** 63],
                          ['charge', 'management_output', 1]]})
    cases.append({'id': 'g5-deadline', 'tag': 'deadline', 'start': 10.0, 'seconds': 1.0,
                  'ops': [['remaining'], ['set', 10.5], ['remaining'], ['set', 10.75], ['remaining'],
                          ['charge', 'body', 1], ['set', 10.999], ['check'], ['set', 11.0], ['check'], ['remaining'],
                          ['charge', 'body', 1], ['set', 5.0], ['remaining'], ['check'], ['set', 12.0], ['remaining']]})
    cases.append({'id': 'g5-deadline-fixed', 'tag': 'deadline', 'start': 100.0, 'seconds': 0.25,
                  'ops': [['set', 100.125], ['remaining'], ['set', 100.25], ['remaining'], ['check'], ['set', 100.0],
                          ['remaining'], ['check']]})
    cases.append({'id': 'g5-stop', 'tag': 'stop', 'start': 0.0, 'seconds': 300.0,
                  'ops': [['check'], ['stop'], ['check'], ['charge', 'body', 1], ['remaining'], ['unstop'], ['check'],
                          ['charge', 'body', 1], ['set', 1000.0], ['stop'], ['check'], ['unstop'], ['check']]})
    cases.append({'id': 'g5-remaining', 'tag': 'remaining', 'start': 0.0, 'seconds': 300.0,
                  'ops': [['remaining'], ['set', 0.25], ['remaining'], ['set', 299.75], ['remaining'], ['set', 300.0],
                          ['remaining'], ['set', 400.0], ['remaining']]})
    return cases


def run_budget_ops(case):
    rd, rt, _ = legacy()
    now = [case['start']]
    budget = rt.Budget(clock=lambda: now[0], seconds=case['seconds'])
    results = []
    for op in case['ops']:
        name = op[0]
        try:
            if name == 'charge':
                budget.charge(op[1], op[2])
                results.append(['ok'])
            elif name == 'check':
                budget.check()
                results.append(['ok'])
            elif name == 'remaining':
                results.append(['ok', budget.remaining()])
            elif name == 'set':
                now[0] = op[1]
                results.append(['ok'])
            elif name == 'stop':
                budget.stopped = True
                results.append(['ok'])
            elif name == 'unstop':
                budget.stopped = False
                results.append(['ok'])
            else:
                raise ValueError(op)
        except rt.Failure as error:
            results.append(['fail', error.reason, error.code])
    return results


def add_g5(builder):
    cases = budget_cases()
    for case in cases:
        case['group'] = 'g5-budget'
        case['legacy'] = run_budget_ops(case)
        builder.named['g5-budget'].append(case['tag'])
    builder.budget_cases = cases
    return cases


# ---------------------------------------------------------------- 本物の git(Task 2.3・2.4)

class TrackingDict(dict):
    """旧版の _table が削除の印で pop を呼ぶ回数を数える(table に削除の印が残っているかの確かめ)。"""
    pops = 0

    def pop(self, key, default=None):
        TrackingDict.pops += 1
        return super().pop(key, default)


class MemorySource:
    def __init__(self, data):
        self.data = data
        self.size = len(data)
        self.budget = legacy()[1].Budget()

    def read(self, offset, count):
        return self.data[offset:offset + count]


def stack_facts(directory, fmt):
    """stack の table の数・削除の印の数・ref index を持つ table の数。旧版の解釈を使う。"""
    rd = legacy()[0]
    names = (Path(directory) / 'tables.list').read_text().split()
    tombstones = 0
    indexed = 0
    for name in names:
        data = (Path(directory) / name).read_bytes()
        before = TrackingDict.pops
        merged = TrackingDict()
        rd._table(MemorySource(data), fmt, merged)
        tombstones += TrackingDict.pops - before
        if table_info(data)[1][0]:
            indexed += 1
    return {'tables': len(names), 'tombstones': tombstones, 'indexed': indexed}


def parse_for_each_ref(raw):
    rows = []
    for line in raw.split(b'\n'):
        if line:
            name, oid, symref = line.split(b'\0')
            rows.append((name, oid.decode(), symref))
    return rows


def compare_with_git(repo, result, prefixes, cwd=None, label=''):
    """旧版の結果を git 自身の一覧と比べる(旧版が正しく読めている裏付け)。比べた件数と外した件数を返す。"""
    fmt = '--format=%(refname)%00%(objectname)%00%(symref)'
    out = repo.git('for-each-ref', fmt, *prefixes, cwd=cwd)
    theirs = {name: (oid, symref) for name, oid, symref in parse_for_each_ref(out)}
    entries = {bytes.fromhex(n): (o, None if t is None else bytes.fromhex(t)) for n, o, t in result[1]}
    mine = {n: v for n, v in entries.items() if any(n.startswith(p.encode() if isinstance(p, str) else p) for p in prefixes)}
    dangling = 0
    for name, (oid, target) in list(mine.items()):
        if target is not None:
            seen = set()
            current = target
            while current in entries and entries[current][1] is not None and current not in seen:
                seen.add(current)
                current = entries[current][1]
            if current not in entries:
                dangling += 1
                del mine[name]
    if set(mine) != set(theirs):
        raise CheckError('git の for-each-ref と旧版の参照の名前が合わない(%s): 旧版だけ %r・git だけ %r' % (
            label, sorted(set(mine) - set(theirs))[:3], sorted(set(theirs) - set(mine))[:3]))
    for name, (oid, target) in mine.items():
        their_oid, their_symref = theirs[name]
        if target is None:
            if oid != their_oid or their_symref:
                raise CheckError('git の OID と旧版の OID が合わない(%s): %r' % (label, name))
        elif their_symref != target:
            raise CheckError('git の symref と旧版の指す先が合わない(%s): %r' % (label, name))
    if not mine:
        raise CheckError('git と比べた件数が 0(%s)' % label)
    return len(mine), dangling


def copy_stack(source, dest):
    shutil.copytree(source, dest)
    return dest


def build_repos(builder, home):
    """10 個の本物の git のリポジトリを作り、reftable のフォルダをコピーして G6 の入力にする。"""
    specs = [
        ('sha1-basic', 'sha1', [], 'basic'), ('sha256-basic', 'sha256', [], 'basic'),
        ('sha1-two-worktrees', 'sha1', [], 'basic2'),
        ('sha1-big', 'sha1', [('reftable.blockSize', '1024')], 'big'),
        ('sha256-big', 'sha256', [('reftable.blockSize', '1024')], 'big'),
        ('sha1-nonutf8', 'sha1', [], 'nonutf8'), ('sha256-nonutf8', 'sha256', [], 'nonutf8'),
        ('sha1-many-tables', 'sha1', [('reftable.restartInterval', '1')], 'many'),
        ('sha256-many-tables', 'sha256', [], 'many'),
        ('sha1-compacted', 'sha1', [('reftable.blockSize', '256')], 'compacted'),
    ]
    for name, fmt, config, kind in specs:
        repo = Repo(Path(home) / name, home, fmt, config)
        oid = repo.commit()
        extra = []
        if kind in ('basic', 'basic2'):
            repo.git('branch', 'feature/a')
            repo.git('branch', 'feature/b')
            repo.git('tag', 'v1')
            repo.git('tag', '-a', 'v2', '-m', 'annotated')
            repo.git('branch', '-D', 'feature/b')
            repo.git('symbolic-ref', 'refs/heads/sym', 'refs/heads/main')
            repo.git('symbolic-ref', 'refs/heads/dangling', 'refs/heads/no-such-branch')
            (repo.path / 'f').write_text('changed')
            repo.git('stash', 'push', '-q')
            for wt in ('wt1',) + (('wt2',) if kind == 'basic2' else ()):
                repo.git('worktree', 'add', '-q', str(Path(home) / (name + '-' + wt)), '-b', 'b-' + wt)
                other = Path(home) / (name + '-' + wt)
                repo.git('update-ref', 'refs/worktree/x', 'HEAD', cwd=str(other))
                repo.git('update-ref', 'refs/bisect/y', 'HEAD', cwd=str(other))
                extra.append(wt)  # worktrees/<フォルダ名> の名前は、フォルダ名(<リポジトリ名>-<wt>)になる
        elif kind == 'big':
            repo.update([('create refs/heads/big/%05d %s' % (n, oid)).encode() for n in range(2200)])
            repo.update([('delete refs/heads/big/%05d' % n).encode() for n in (3, 1500, 2100)])
            repo.git('branch', 'late')
            repo.git('tag', '-a', 'tagged', '-m', 'annotated')
        elif kind == 'nonutf8':
            repo.update([b'create refs/heads/\xff\xfe ' + oid.encode(), b'create refs/tags/\x80\x81 ' + oid.encode(),
                         b'create refs/heads/plain ' + oid.encode()])
            repo.git(b'symbolic-ref', b'refs/heads/sym\xff', b'refs/heads/\xff\xfe')
            repo.git(b'symbolic-ref', b'refs/heads/sym-dangling', b'refs/heads/\xfe\xfe')
            repo.git('update-ref', '-d', 'refs/heads/plain')
        elif kind == 'many':
            for n in range(8):
                repo.git('branch', 'b%d' % n)
                if n % 3 == 2:
                    repo.git('branch', '-D', 'b%d' % (n - 1))
            repo.git('tag', '-a', 'tagged', '-m', 'annotated')
        elif kind == 'compacted':
            for n in range(60):
                repo.git('update-ref', 'refs/heads/c%02d' % n, oid)
            repo.git('pack-refs', '--all')
        stacks = [(None, 'shared')]
        stacks += [(w, 'worktree') for w in extra]
        for worktree, label in stacks:
            source = repo.reftable(None if worktree is None else name + '-' + worktree)
            dest = Path(home) / 'copies' / ('%s-%s' % (name, worktree or 'shared'))
            dest.parent.mkdir(exist_ok=True)
            copy_stack(source, dest)
            builder.git_stacks.append({'repo': name, 'kind': label, 'fmt': fmt, 'dir': dest,
                                       'repo_obj': repo, 'worktree': worktree})
    return specs


def add_g6(builder, home):
    builder.git_stack_count = 0
    build_repos(builder, home)
    for stack in builder.git_stacks:
        name = 'g6-%s-%s' % (stack['repo'], stack['worktree'] or 'shared')
        target = builder.directory(name)
        target.rmdir()
        shutil.copytree(stack['dir'], target)
        facts = stack_facts(target, stack['fmt'])
        expects_history = stack['kind'] == 'shared' and any(k in stack['repo'] for k in ('basic', 'big', 'many', 'nonutf8', 'two-worktrees'))
        if expects_history and (facts['tables'] < 2 or facts['tombstones'] < 1):
            # 自動の圧縮を止めても table が 1 つに戻る・削除の印が残らないなら、入力を作れていない(skip にしない)。
            raise CheckError('git の stack に、複数の table と削除の印が残っていない: %s %r' % (name, facts))
        props = ['git-sha1' if stack['fmt'] == 'sha1' else 'git-sha256']
        if facts['tables'] >= 2:
            props.append('git-multi-table')
        if facts['indexed']:
            props.append('git-multi-block-index')
        if facts['tombstones']:
            props.append('git-tombstone')
        if stack['kind'] == 'worktree':
            props.append('git-worktree')
        if 'nonutf8' in stack['repo']:
            props.append('nonutf8-git')
        normal = builder.add(name, 'g6-git-stack', target, 'representative', stack['fmt'], props)
        if normal[0] != 'ok':
            raise CheckError('本物の git の stack を旧版が読めない: %s %r' % (name, normal[:4]))
        entries = {bytes.fromhex(n): t for n, o, t in normal[1]}
        if any(t is not None for t in entries.values()):
            builder.inputs[-1]['props'] = sorted(set(builder.inputs[-1]['props']) | {'git-symref'})
        if b'refs/stash' in entries:
            builder.inputs[-1]['props'] = sorted(set(builder.inputs[-1]['props']) | {'git-stash'})
        repo = stack['repo_obj']
        if stack['kind'] == 'shared':
            compared, dangling = compare_with_git(repo, normal, [b'refs/'], None, name)
            builder.git_notes['common_compared'] += 1
            tagged = 'git-for-each-ref-common'
        else:
            wtpath = Path(home) / (stack['repo'] + '-' + stack['worktree'])
            compared, dangling = compare_with_git(repo, normal, [b'refs/worktree/', b'refs/bisect/', b'refs/rewritten/'],
                                                  str(wtpath), name)
            builder.git_notes['worktree_compared'] += 1
            tagged = 'git-for-each-ref-worktree'
        builder.git_notes['excluded_dangling'] += dangling
        builder.inputs[-1]['props'] = sorted(set(builder.inputs[-1]['props']) | {tagged})
        builder.inputs[-1]['git_compared'] = compared
    return builder.git_stacks


# ---------------------------------------------------------------- manifest

def required_counts(manifest):
    """件数の下限(Task 2.2 の表)。値が (種類, 'exact' か 'min')。manifest の table と一覧から数える。"""
    tables = manifest['tables']
    required = {
        'g1-truncate': (sum(t['size'] for t in tables.values()) + len(tables), 'exact'),
        'g1-mutate': (sum(t['size'] for t in tables.values()) * 4, 'exact'),
        'g1h-field': (sum(t['header_size'] + 40 for t in tables.values()) * 4, 'exact'),
        'g6-git-stack': (manifest['git_stack_count'], 'exact'),
        'table-object-section': (1, 'min'),
    }
    for group in ('g2-representative', 'g3-boundary', 'g4-argument', 'g5-budget'):
        required[group] = (len(manifest['named'][group]), 'exact')
    for prop in PROPERTIES[1:]:
        required[prop] = (1, 'min')
    required['g7-nonutf8-directory'] = (1 if manifest['g7_expected'] else 0, 'min')
    if manifest['scope'] == 'mini':
        # 自己検査用の小さい組が満たす件数だけを求める。
        keep = ('g1-truncate', 'g2-representative', 'g3-boundary', 'g4-argument', 'g5-budget', 'g6-git-stack',
                'g7-nonutf8-directory')
        required = {key: value for key, value in required.items() if key in keep}
    return required


def count_inputs(manifest):
    counts = {group: 0 for group in GROUPS}
    counts.update({prop: 0 for prop in PROPERTIES})
    for item in manifest['inputs']:
        counts[item['group']] += 1
        for prop in item['props']:
            counts[prop] += 1
    counts['g5-budget'] = len(manifest['budget_cases'])
    counts['table-object-section'] = sum(1 for t in manifest['tables'].values() if t['object_section'])
    return counts


def runs_by_group(manifest):
    runs = {}
    for item in manifest['inputs']:
        runs[item['group']] = runs.get(item['group'], 0) + len(item['conds'])
    for case in manifest['budget_cases']:
        runs['g5-budget'] = runs.get('g5-budget', 0) + len(case['ops'])
    return runs


def generate(destination, relative_base, scope='full'):
    started = time.monotonic()
    version = check_prerequisites() if scope == 'full' else git_version()[1]
    destination = Path(os.path.realpath(destination))
    relative_base = os.path.realpath(relative_base)
    if not destination.is_dir() or any(destination.iterdir()):
        raise CheckError('--generate の出力先は空のフォルダでなければならない: %s' % destination)
    base = Path(relative_base)
    if not base.is_dir():
        raise CheckError('--relative-base がフォルダでない: %s' % relative_base)
    cwd = os.getcwd()
    os.chdir(base)
    try:
        builder = Builder(destination, base)
        home = Path(os.path.realpath(destination)) / 'work'
        home.mkdir()
        tables = small_tables()
        if scope == 'full':
            tables['T8'], tables['T9'] = git_tables(builder, home)
        tables = {key: tables[key] for key in sorted(tables)}
        if scope == 'full':
            check_git_tables(tables)
        else:
            tables = {key: tables[key] for key in ('T1', 'T4', 'T5', 'T6', 'T7')}
        t7 = build_t7()
        add_g1(builder, tables) if scope == 'full' else add_g1_mini(builder, tables)
        add_g2(builder, tables)
        add_g3(builder)
        add_g4(builder)
        add_g5(builder)
        add_g7(builder, tables)
        if scope == 'full':
            home_git = home / 'git'
            home_git.mkdir()
            add_g6(builder, home_git)
        manifest = {
            'scope': scope, 'git_version': version, 'relative_base': str(builder.base),
            't7_hex': t7.hex(), 'tables': builder.tables, 'inputs': builder.inputs,
            'budget_cases': builder.budget_cases, 'named': builder.named,
            'git_stack_count': len(builder.git_stacks), 'git_notes': builder.git_notes,
            'g7_expected': sys.platform.startswith('linux'), 'not_created': builder.not_created,
            'signatures': builder.signatures, 'legacy_runs': builder.runs,
            'generation_seconds': time.monotonic() - started,
        }
        manifest['counts'] = count_inputs(manifest)
        manifest['runs_by_group'] = runs_by_group(manifest)
        if manifest['counts']['g7-nonutf8-directory'] == 0 and manifest['g7_expected']:
            raise CheckError('Linux では g7-nonutf8-directory の入力を作れなければならない')
        if scope == 'full':
            for group, label in (('g1-truncate', 'g1-truncate'), ('g1-mutate', 'g1-mutate'), ('g1h-field', 'g1h-field')):
                print('R4D-1 生成: %s 入力 %d 件・旧版の実行 %d 回' % (label, manifest['counts'][group],
                                                              manifest['runs_by_group'].get(group, 0)), file=sys.stderr)
            print('R4D-1 生成: 旧版の実行は合計 %d 回(上限 %d 回)・%.1f 秒・git %s' % (
                builder.runs, RUN_LIMIT, manifest['generation_seconds'], version), file=sys.stderr)
        text = json.dumps(manifest, separators=(',', ':'))
        (destination / 'manifest.json').write_text(text)
        manifest_path = destination / 'manifest.json'
        return manifest_path
    finally:
        os.chdir(cwd)


def add_g1_mini(builder, tables):
    """自己検査用の小さい組(T1・T4・T5・T6・T7 の切り詰めと変異の一部)。製品の検査では使わない。"""
    for tname, data in tables.items():
        size, fields, _ = table_info(data)
        builder.tables[tname] = {'size': len(data), 'header_size': size, 'object_section': False}
    # 件数の式を満たす全数は作らない(自己検査は、満たさない件数を失敗として確かめるために使う)。
    for tname, data in tables.items():
        for cut in range(len(data) + 1):
            name = 'g1t-%s-%05d' % (tname, cut)
            if tname == 'T5':
                files, listing = {'table-0.ref': data[:cut], 'table-1.ref': tables['T6']}, listing_of('table-0.ref', 'table-1.ref')
            elif tname == 'T6':
                files, listing = {'table-0.ref': tables['T5'], 'table-1.ref': data[:cut]}, listing_of('table-0.ref', 'table-1.ref')
            else:
                files, listing = {'table-0.ref': data[:cut]}, listing_of('table-0.ref')
            builder.add(name, 'g1-truncate', builder.stack_dir(name, files, listing), 'full', 'sha1', ())


# ---------------------------------------------------------------- 比較

def summarize(item):
    """表示用に結果を短くする。"""
    text = json.dumps(item)
    return text if len(text) <= 300 else text[:300] + '...(%d 文字)' % len(text)


def compare_results(manifest, rust, only_quantity=False):
    """全件の結果を比べ、不一致の説明(文字列)の一覧と、比べた件数を返す。"""
    mismatches = []
    compared = 0
    missing = 0
    results = rust.get('results', {})
    expected_ids = set()
    for item in manifest['inputs']:
        wanted = [i for i, cond in enumerate(item['conds']) if (not only_quantity or cond[0] == 'q')]
        if not wanted:
            continue
        expected_ids.add(item['id'])
        got = results.get(item['id'])
        if got is None:
            missing += len(wanted)
            mismatches.append('%s: Rust の結果が無い' % item['id'])
            continue
        for index in wanted:
            compared += 1
            other = got.get(str(index))
            if other is None:
                missing += 1
            if other != item['legacy'][index]:
                mismatches.append('%s 条件 %s: 旧版 %s / Rust %s' % (
                    item['id'], summarize(item['conds'][index]), summarize(item['legacy'][index]), summarize(other)))
        extra = set(got) - {str(i) for i in wanted}
        if extra and not only_quantity:
            mismatches.append('%s: 比べる条件の外の Rust の結果がある: %s' % (item['id'], sorted(extra)[:3]))
    if not only_quantity:
        for case in manifest['budget_cases']:
            got = rust.get('budget', {}).get(case['id'])
            compared += 1
            if got is None:
                missing += 1
            if got != case['legacy']:
                mismatches.append('%s: 旧版 %s / Rust %s' % (case['id'], summarize(case['legacy']), summarize(got)))
        stray = set(results) - expected_ids
        if stray:
            mismatches.append('manifest に無い入力の Rust の結果がある: %s' % sorted(stray)[:3])
    return mismatches, compared, missing


def check_counts(manifest):
    """入力の数が、件数の下限の表を満たすか。満たさない説明の一覧を返す。"""
    problems = []
    counts = count_inputs(manifest)
    if manifest['scope'] != 'full' and not os.environ.get(ALLOW_MINI_ENV):
        problems.append('manifest の範囲が full でない(%s)' % manifest['scope'])
    if sum(counts[g] for g in GROUPS) != len(manifest['inputs']) + len(manifest['budget_cases']):
        problems.append('入力の数が、組の件数の合計と同じでない')
    for kind, (value, mode) in required_counts(manifest).items():
        actual = counts.get(kind, 0)
        if mode == 'exact' and actual != value:
            problems.append('件数が下限(全数)と違う: %s=%d(要る件数 %d)' % (kind, actual, value))
        elif mode == 'min' and actual < value:
            problems.append('件数が下限に足りない: %s=%d(下限 %d)' % (kind, actual, value))
    for group, tags in REQUIRED_TAGS.items():
        missing = [t for t in tags if t not in manifest['named'][group]]
        if missing:
            problems.append('名前付きの入力の一覧に項目が無い: %s %s' % (group, missing))
    for item in manifest['inputs']:
        if item['group'] == 'g6-git-stack' and not item.get('git_compared', 0) >= 1:
            problems.append('git と比べた件数が 0: %s' % item['id'])
    return problems


def verify_tree(manifest):
    """読んだ後も、入力のフォルダが manifest を作ったときのまま。"""
    problems = []
    for item in manifest['inputs']:
        want = manifest['signatures'].get(item['id'])
        if want is None:
            continue
        if tree_signature(bytes.fromhex(want[0])) != want[1]:
            problems.append('入力のフォルダが変わった: %s' % item['id'])
    return problems


def print_counts(manifest, counts):
    runs = manifest['runs_by_group']
    for kind in GROUPS + PROPERTIES:
        line = 'R4D-1 件数: %s=%d' % (kind, counts.get(kind, 0))
        if kind in ('g1-truncate', 'g1-mutate', 'g1h-field'):
            line += ' 旧版の実行=%d' % runs.get(kind, 0)
        print(line, file=sys.stderr)


def compare_main(manifest_path, rust_path, expect_mismatch):
    started = time.monotonic()
    version = check_prerequisites()
    manifest = json.loads(Path(manifest_path).read_text())
    rust = json.loads(Path(rust_path).read_text())
    if expect_mismatch:
        mismatches, compared, missing = compare_results(manifest, rust, only_quantity=True)
        quantity_total = sum(1 for item in manifest['inputs'] for cond in item['conds'] if cond[0] == 'q')
        if compared != quantity_total or compared == 0 or missing:
            raise CheckError('「量」の条件の Rust の結果がそろっていない(%d / %d)' % (compared, quantity_total))
        if not mismatches:
            raise CheckError('差し込みを入れた Rust の結果が、旧版と全件一致した。比較が働いていない')
        print('R4D-1 reftable の比較(差し込み): 不一致 %d 件・期待どおり失敗' % len(mismatches), file=sys.stderr)
        return 0
    if os.environ.get(NO_SELFTEST_ENV):
        print('R4D-1 比較器の自己検査: 飛ばした(%s が設定されている)' % NO_SELFTEST_ENV, file=sys.stderr)
    else:
        run_selftest()
    mismatches, compared, _ = compare_results(manifest, rust)
    problems = check_counts(manifest) + verify_tree(manifest)
    t7 = bytes.fromhex(manifest['t7_hex'])
    if rust.get('t7_hex') != manifest['t7_hex'] or t7 != build_t7():
        problems.append('Rust の T7 の定数が比較器の T7 と違う')
    counts = count_inputs(manifest)
    print_counts(manifest, counts)
    for line in manifest.get('not_created', []):
        print('R4D-1 作れなかった入力: %s' % line, file=sys.stderr)
    notes = manifest['git_notes']
    print('R4D-1 git の比較: 共有の stack %d 件・worktree の stack %d 件・指す先が無く外した symref %d 件' % (
        notes['common_compared'], notes['worktree_compared'], notes['excluded_dangling']), file=sys.stderr)
    total_inputs = sum(counts[g] for g in GROUPS)
    seconds = manifest['generation_seconds'] + (time.monotonic() - started)
    print('R4D-1 reftable の比較: 入力 %d 件・不一致 %d 件・git %s・%.1f 秒' % (
        total_inputs, len(mismatches), version, seconds), file=sys.stderr)
    for line in mismatches[:20]:
        print('不一致: ' + line, file=sys.stderr)
    for line in problems:
        print('失敗: ' + line, file=sys.stderr)
    return 0 if not mismatches and not problems else 1


def run_selftest():
    """比較器の自己検査。失敗なら CheckError(終了値 0 にしない)。自己検査の子は、この印で再帰しない。"""
    import importlib.util
    import unittest
    path = Path(__file__).resolve().with_name('test_diagnostic_reftable.py')
    spec = importlib.util.spec_from_file_location('r4d1_checker_selftest', path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    # unittest のモジュール単位の準備(setUpModule)は、sys.modules に登録された名前で探す。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    finally:
        sys.dont_write_bytecode = previous
        sys.modules.pop(spec.name, None)
    if not result.wasSuccessful():
        raise CheckError('比較器の自己検査が失敗した')
    print('R4D-1 比較器の自己検査: %d 件合格' % result.testsRun, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--generate', metavar='DIR')
    parser.add_argument('--relative-base', metavar='DIR')
    parser.add_argument('--compare', nargs=2, metavar=('MANIFEST', 'RUST_RESULTS'))
    parser.add_argument('--expect-mismatch', action='store_true')
    parser.add_argument('--scope', choices=('full', 'mini'), default='full', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if bool(args.generate) == bool(args.compare):
            raise CheckError('--generate と --compare のどちらか 1 つを指定する')
        if args.generate:
            if not args.relative_base:
                raise CheckError('--generate には --relative-base が要る')
            generate(args.generate, args.relative_base, args.scope)
            return 0
        return compare_main(args.compare[0], args.compare[1], args.expect_mismatch)
    except CheckError as error:
        print('失敗: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
