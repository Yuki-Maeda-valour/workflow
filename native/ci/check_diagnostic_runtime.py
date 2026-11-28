#!/usr/bin/env python3
"""R4D-2: 診断の実行の土台(厳密な JSON・base64・確認用の値・応答の符号化・上限付きの読み取り)の旧版と Rust の比較器。

製品の入口ではない。Rust の検査 native/crates/cli/tests/diagnostic_runtime.rs が起動する。

使い方:
  --generate <空の一時フォルダ>
      入力と筋書きの一覧(cases.json)と旧版の結果(legacy.json)を書き、J8 の stable/ を作る。
      旧版は、この比較器と同じ Python の中で diagnostic-runtime.py を固有の名前で読み込んで呼ぶ。
      R の筋書きの木は、決めた絶対パスに作り、旧版の後に消す(Rust の検査が同じパスに作り直す)。
  --compare <legacy.json> <Rust の結果の JSON> [--expect-mismatch]
      全件の結果が並びまで一致し、組ごとの件数が下限以上のときだけ終了値 0。
      --expect-mismatch は、差し込みを入れた Rust の結果(差し込んだ件だけ)を比べ、全件が不一致のときだけ終了値 0。

前提: Python 3.10 系・root 以外・sys.get_int_max_str_digits() が 4300・sys.getrecursionlimit() が 1000。
欠ければ失敗にする(skip しない)。
"""
import argparse
import contextlib
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import resource
import shutil
import stat
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
LEGACY_PATH = ROOT / 'plugins/dev-workflow/skills/do-task/scripts/diagnostic-runtime.py'
# scripts/ は末尾に足す。先頭に足すと、同じ名前のテストの読み込み先を変えてしまう。
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
from support_load import load_script  # noqa: E402
from support_native import run_command  # noqa: E402

NO_SELFTEST_ENV = 'R4D2_NO_SELFTEST'
ALLOW_MINI_ENV = 'R4D2_ALLOW_MINI'
MIB = 1024 * 1024
RECURSION_LIMIT = 1000
INT_DIGITS = 4300
R7_SEED_LIMIT = 64
CHILD_TIMEOUT = 120
J2_DOCUMENTS = 2000
J4_VALUES = 2000
J6_RANDOM = 500
B64_LENGTHS = tuple(range(71)) + (1000,)

GROUPS = ('j1-decode-corpus', 'j2-decode-random', 'j3-decode-depth', 'j4-encode', 'j5-response',
          'j6-base64', 'j7-keys', 'j8-metadata', 'r1-read-limit', 'r2-paths', 'r3-links-types',
          'r4-owner', 'r5-names', 'r6-verify', 'r7-missing-order', 'r8-injection', 'r9-budget', 'r10-cwd')
# 件数の下限のうち、決まった数(Task 2.2)。作れなかった入力の分だけ減らしてよい。
FIXED_MINIMUM = {'j2-decode-random': 3 * J2_DOCUMENTS, 'j3-decode-depth': 168, 'r1-read-limit': 53,
                 'r2-paths': 10, 'r3-links-types': 9, 'r4-owner': 18, 'r5-names': 8, 'r6-verify': 13,
                 'r7-missing-order': 1, 'r8-injection': 16, 'r9-budget': 24, 'r10-cwd': 3}
# Task 2.1 の表の項目ごとの名前。全部のタグの入力が 1 件以上なければ失敗にする(一覧を縮めて下限を下げないため)。
REQUIRED_TAGS = {
    'j1-decode-corpus': ('types', 'whitespace', 'bom', 'trailing-data', 'control-character', 'escapes',
                         'hex-case', 'truncated-u', 'surrogate-pair', 'surrogate-high', 'surrogate-low',
                         'surrogate-reversed', 'utf8-overlong', 'utf8-surrogate', 'utf8-truncated', 'utf8-ff',
                         'minus-zero', 'leading-zero', 'float-edges', 'float-overflow', 'float-underflow',
                         'digits-4300', 'digits-4301', 'constant-top', 'constant-nested', 'duplicate-same',
                         'duplicate-spelling', 'duplicate-nested', 'duplicate-in-array', 'empty', 'whitespace-only'),
    'j2-decode-random': ('valid', 'mutated'),
    'j3-decode-depth': ('arrays', 'dicts', 'nested-nan', 'nested-duplicate', 'missing-value-array',
                        'missing-value-dict', 'errmsg-array', 'errmsg-dict', 'scanstring-control',
                        'scanstring-escape', 'scanstring-unterminated', 'scanstring-short-u', 'nested-digits',
                        'flat-bom', 'flat-utf8', 'flat-extra-data', 'flat-valid', 'flat-missing-value'),
    'j4-encode': ('random', 'zero', 'minus-zero', 'float-1e16', 'float-1e15', 'float-1e-4', 'float-1e-5',
                  'float-min', 'float-max', 'infinity', 'nan', 'int-zero', 'int-minus-one', 'int-2-63',
                  'digits-4300', 'digits-4301', 'ascii', 'code-points', 'lone-surrogates', 'surrogate-pair',
                  'key-order', 'empty-containers', 'nested', 'float-power-of-two'),
    'j5-response': ('encodable', 'plan-created', 'plan-extended', 'observed', 'infinity-position',
                    'float-power-of-two'),
    'j6-base64': ('canonical', 'padding', 'remaining-bits', 'whitespace', 'non-ascii', 'equals-position', 'nul',
                  'empty', 'random-canonical', 'random-noncanonical', 'non-string', 'b64'),
    'j7-keys': ('exact', 'missing', 'extra', 'non-dict', 'separators'),
    'j8-metadata': ('regular', 'empty', 'year-2400'),
    'r1-read-limit': ('size-limit-kind', 'default-limit'),
    'r2-paths': ('absolute', 'relative', 'dotdot', 'dot', 'double-slash', 'trailing-slash', 'empty', 'nul-middle',
                 'nul-last', 'non-utf8'),
    'r3-links-types': ('link-last', 'link-middle', 'fifo', 'read-directory', 'directory-file', 'missing-true',
                       'missing-false', 'no-permission-directory', 'no-permission-file'),
    'r4-owner': ('foreign', 'own-directory', 'own-read', 'own-verify', 'same-path-twice', 'double-slash-owner',
                 'double-slash-directory'),
    'r5-names': ('empty', 'one', 'limit', 'over-limit', 'worktree-entries', 'record-true', 'record-false', 'changed'),
    'r6-verify': ('replace-same', 'chmod', 'append', 'mtime', 'missing-created', 'name-added', 'name-removed',
                  'directory-renamed', 'directory-link', 'directory-removed', 'unchanged', 'direct-missing',
                  'direct-files'),
    'r7-missing-order': ('two-reasons',),
    'r8-injection': ('read-empty', 'append-after-read', 'replace-before-open', 'read-io', 'open-io', 'names-io',
                     'cwd-failure', 'close-directory-step', 'close-directory-child', 'close-directory-other',
                     'close-lstat', 'close-read-file', 'close-read-parent', 'close-names', 'close-verify-dirs',
                     'close-verify-sets'),
    'r9-budget': ('read', 'names', 'verify'),
    'r10-cwd': ('relative', 'deep', 'deleted'),
}
# 作れないことがある入力(OS の制限)。Linux では作れなければ失敗にする。
OPTIONAL_INPUTS = {'j8-metadata/year-2400', 'r2-paths/non-utf8', 'r10-cwd/deep'}


class CheckError(Exception):
    """比較器の失敗。メッセージを標準エラーへ出して終了値 1 にする。"""


# ---------------------------------------------------------------- 前提

def check_prerequisites():
    """Python 3.10 系・root 以外・整数の桁の上限 4300・再帰の上限 1000。欠ければ CheckError(skip にしない)。"""
    problems = []
    if sys.version_info[:2] != (3, 10):
        problems.append('Python 3.10 系が要る(今は %d.%d)' % sys.version_info[:2])
    if os.geteuid() == 0:
        problems.append('root 以外の利用者で動かす必要がある(所有者と権限の検査が意味を失う)')
    digits = sys.get_int_max_str_digits() if hasattr(sys, 'get_int_max_str_digits') else None
    if digits != INT_DIGITS:
        problems.append('整数の桁の上限が %d でない(今は %r。PYTHONINTMAXSTRDIGITS を外す)' % (INT_DIGITS, digits))
    if sys.getrecursionlimit() != RECURSION_LIMIT:
        problems.append('再帰の上限が %d でない(今は %d)' % (RECURSION_LIMIT, sys.getrecursionlimit()))
    if problems:
        raise CheckError('R4D-2 前提が満たされない: ' + ' / '.join(problems))


def resource_line():
    parts = []
    for name in ('RLIMIT_NOFILE', 'RLIMIT_STACK', 'RLIMIT_AS'):
        soft, hard = resource.getrlimit(getattr(resource, name))
        show = lambda v: 'unlimited' if v == resource.RLIM_INFINITY else str(v)  # noqa: E731
        parts.append('%s=%s/%s' % (name, show(soft), show(hard)))
    return 'R4D-2 資源の上限: ' + '・'.join(parts)


_modules = {}


def legacy():
    """旧版 diagnostic-runtime.py(固有の名前で読み込む)。"""
    if 'rt' not in _modules:
        _modules['rt'] = load_script(LEGACY_PATH, 'r4d2_legacy_runtime')
    return _modules['rt']


# ---------------------------------------------------------------- 値の表し方

def int_text(value):
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        return str(value)
    finally:
        sys.set_int_max_str_digits(previous)


def int_value(text):
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        return int(text)
    finally:
        sys.set_int_max_str_digits(previous)


def float_hex(value):
    return struct.pack('>d', value).hex()


def cps(text):
    return [ord(c) for c in text]


def typed(value):
    """浅い値を、型を付けた ASCII だけの JSON にする。"""
    if value is None:
        return {'t': 'null'}
    if value is True or value is False:
        return {'t': 'bool', 'v': value}
    if type(value) is int:
        return {'t': 'int', 'v': int_text(value)}
    if type(value) is float:
        return {'t': 'float', 'v': float_hex(value)}
    if type(value) is str:
        return {'t': 'str', 'v': cps(value)}
    if type(value) is list:
        return {'t': 'list', 'v': [typed(item) for item in value]}
    if type(value) is dict:
        return {'t': 'dict', 'v': [[cps(k), typed(v)] for k, v in value.items()]}
    raise TypeError(type(value))


def untyped(data):
    kind = data['t']
    if kind == 'null':
        return None
    if kind == 'bool':
        return data['v']
    if kind == 'int':
        return int_value(data['v'])
    if kind == 'float':
        return struct.unpack('>d', bytes.fromhex(data['v']))[0]
    if kind == 'str':
        return ''.join(map(chr, data['v']))
    if kind == 'list':
        return [untyped(item) for item in data['v']]
    if kind == 'dict':
        return {''.join(map(chr, k)): untyped(v) for k, v in data['v']}
    raise ValueError(kind)


def tokens(value):
    """decode の結果を、前から順に並べた印の列にする(再帰を使わない)。"""
    out = []
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is tuple:
            out += ['K', cps(item[1])]
        elif item is None:
            out.append('N')
        elif item is True:
            out.append('T')
        elif item is False:
            out.append('F')
        elif type(item) is int:
            out += ['I', int_text(item)]
        elif type(item) is float:
            out += ['R', float_hex(item)]
        elif type(item) is str:
            out += ['S', cps(item)]
        elif type(item) is list:
            out += ['L', len(item)]
            stack.extend(reversed(item))
        elif type(item) is dict:
            out += ['D', len(item)]
            for key, child in reversed(list(item.items())):
                stack.append(child)
                stack.append(('K', key))
        else:
            raise TypeError(type(item))
    return out


# ---------------------------------------------------------------- 予算の条件と操作の列

class Clock:
    """const(いつも 0)・expired(2 回目から 1000)・seq(呼ぶたびに 0,1,2,...)。"""

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


def recording_class():
    """check と charge を呼んだ順に記録する旧版の予算(charge の記録の後に、その中の check が並ぶ)。"""
    if 'recording' not in _modules:
        rt = legacy()

        class Recording(rt.Budget):
            def __init__(self, **options):
                self.log = []
                super().__init__(**options)

            def check(self):
                self.log.append('c')
                return super().check()

            def charge(self, kind, count):
                self.log.append('%s=%d' % (kind, count))
                return super().charge(kind, count)

        _modules['recording'] = Recording
    return _modules['recording']


def make_budget(cond):
    """条件 ['normal'] ['expired'] ['stop'] ['n', N] ['q', 量, L] の旧版の予算。check と charge の列を記録する。"""
    rt = legacy()
    name = cond[0]
    limits = dict(rt.LIMITS)
    seconds, mode = 300.0, 'const'
    if name == 'expired':
        mode = 'expired'
    elif name == 'n':
        seconds, mode = float(cond[1]), 'seq'
    elif name == 'q':
        limits[cond[1]] = cond[2]
    elif name not in ('normal', 'stop'):
        raise ValueError(cond)
    budget = recording_class()(clock=Clock(mode), seconds=seconds, limits=limits)
    if name == 'stop':
        budget.stopped = True
    return budget


def log_text(budget):
    return ';'.join(budget.log)


def conditions_from(log, kind):
    """通常の実行の操作の列から、8 つの予算の条件を作る(期限切れ・停止・N 回目の 4 点・量の上限 L と L−1)。"""
    checks = log.count('c')
    total = sum(int(op.split('=')[1]) for op in log if op.startswith(kind + '='))
    return [['expired'], ['stop'], ['n', 1], ['n', (checks + 1) // 2], ['n', checks], ['n', checks + 1],
            ['q', kind, total], ['q', kind, total - 1]]


def cond_name(cond):
    return '-'.join(str(part) for part in cond)


# ---------------------------------------------------------------- 決まった深さから呼ぶ

def frame_count(frame):
    count = 0
    while frame is not None:
        count += 1
        frame = frame.f_back
    return count


def invoke(depth, function, *args):
    """`function` を呼ぶフレームの深さが `depth` になるように積んでから呼ぶ(深さ 0 は最も浅い自然な深さ)。

    S2E の比較器の call_at(gen_cases.py:194-215)と同じ手順。(呼んだフレームの深さ, 結果か例外) を返す。
    """
    box = {}

    def inner():
        box['site'] = frame_count(sys._getframe())
        return function(*args)

    def climb():
        if frame_count(sys._getframe()) < depth - 1:
            return climb()
        return inner()

    try:
        value = climb()
    except BaseException as error:  # noqa: BLE001 旧版の例外をそのまま記録する
        return box.get('site'), ('error', error)
    return box.get('site'), ('ok', value)


def measure_frames():
    """普通の Python の関数を 1 段ずつ積み、RecursionError になる前に積めた段数(最初のフレームは数えない)。"""
    count = [0]

    def deeper():
        count[0] += 1
        deeper()

    try:
        deeper()
    except RecursionError:
        pass
    return count[0]


def direct(function):
    return function()


def checked_depth(depth, call=direct):
    """呼出しの場所の深さを測り、深さ + 積めた段数が上限−1 と同じことを確かめる。違えば CheckError。"""
    site, (kind, value) = call(lambda: invoke(depth, measure_frames))
    if kind != 'ok' or site is None:
        raise CheckError('深さを測れない(%r)' % depth)
    if site + value != RECURSION_LIMIT - 1:
        raise CheckError('深さの測り方の確かめが合わない: 呼出しの深さ %d + 積めた段数 %d が %d でない'
                         '(C の関数の呼出しの中から呼んでいる)' % (site, value, RECURSION_LIMIT - 1))
    return site


def decode_result(outcome):
    rt = legacy()
    kind, value = outcome
    if kind == 'ok':
        return ['ok', tokens(value)]
    if isinstance(value, rt.Failure):
        return ['err', 'schema'] if (value.reason, value.code) == ('schema', 2) else \
            ['err', 'failure', value.reason, value.code]
    if isinstance(value, RecursionError):
        return ['err', 'recursion']
    return ['err', 'other', type(value).__name__]


def legacy_decode(raw, depth, call=direct):
    site, outcome = call(lambda: invoke(depth, legacy().decode, raw))
    return site, decode_result(outcome)


# ---------------------------------------------------------------- 組み立て

class Builder:
    def __init__(self, root, scope):
        self.root = Path(os.path.realpath(root))
        self.scope = scope
        self.groups = {group: [] for group in GROUPS}
        self.results = {}
        self.alternatives = {}
        self.not_created = []
        self.bias = {}

    def add(self, group, tag, item, result):
        item = dict(item, id='%s/%04d-%s' % (group, len(self.groups[group]), tag), group=group, tag=tag)
        if item['id'] in self.results:
            raise CheckError('入力の名前が重なる: %s' % item['id'])
        self.groups[group].append(item)
        self.results[item['id']] = result
        return item

    def missing_input(self, key, reason):
        name = key.split('/', 1)[1]
        if sys.platform.startswith('linux'):
            raise CheckError('Linux では %s を作れなければならない: %s' % (key, reason))
        self.not_created.append({'key': key, 'line': '%s(%s)' % (name, sys.platform)})


# ---- J1・J2・J3

def j1_corpus():
    """名前付きの一覧: (タグ, 入力)。"""
    big = b'7' * INT_DIGITS
    over = b'7' * (INT_DIGITS + 1)
    items = [
        ('types', b'{"a":[1,-2.5,"s",true,false,null,{},[]],"b":{"c":0}}'),
        ('types', b'[1e5,1E+5,1e-5,0.5,-0,123456789012345678901234567890]'),
        ('whitespace', b' \t\n\r[ 1 , 2 ]\r\n\t '),
        ('whitespace', b'[\x0b1]'),
        ('whitespace', '[\u00a01]'.encode()),
        ('bom', '\ufeff[]'.encode()),
        ('bom', '\ufeff'.encode()),
        ('bom', '[\ufeff]'.encode()),
        ('trailing-data', b'[] []'),
        ('trailing-data', b'1 2'),
        ('trailing-data', b'"a"x'),
        ('trailing-data', b'{}\x00'),
        ('control-character', b'"\x00"'),
        ('control-character', b'"\x1f"'),
        ('control-character', b'"\x7f"'),
        ('escapes', b'"\\"\\\\\\/\\b\\f\\n\\r\\t"'),
        ('escapes', b'"\\a"'),
        ('escapes', b'"\\'),
        ('escapes', b'"\\"'),
        ('hex-case', b'"\\u00e9\\u00E9\\uABCD\\uabcd"'),
        ('hex-case', b'"\\u00g0"'),
        ('truncated-u', b'"\\u12"'),
        ('truncated-u', b'"\\u123'),
        ('truncated-u', b'"\\u1234'),
        ('truncated-u', b'"\\ud83d\\ude0"'),
        ('surrogate-pair', b'"\\ud83d\\ude00"'),
        ('surrogate-pair', b'"\\uD83D\\uDE00x"'),
        ('surrogate-pair', b'"\\ud83d\\ude00'),
        ('surrogate-high', b'"\\ud800"'),
        ('surrogate-high', b'"\\ud800x"'),
        ('surrogate-high', b'"\\ud800\\u0041"'),
        ('surrogate-low', b'"\\udc00"'),
        ('surrogate-reversed', b'"\\udc00\\ud800"'),
        ('utf8-overlong', b'"\xc0\x80"'),
        ('utf8-overlong', b'"\xe0\x80\x80"'),
        ('utf8-surrogate', b'"\xed\xa0\x80"'),
        ('utf8-truncated', b'"\xe3\x81"'),
        ('utf8-truncated', b'\xf0\x9f\x98'),
        ('utf8-ff', b'\xff'),
        ('utf8-ff', b'"a\xffb"'),
        ('minus-zero', b'-0'),
        ('minus-zero', b'[-0.0,-0e0]'),
        ('leading-zero', b'01'),
        ('leading-zero', b'[00]'),
        ('leading-zero', b'-01'),
        ('float-edges', b'[1.0,1e0,1E-0,1.5e+3,0.0001,1e-7,1.7976931348623157e308,5e-324,2.2250738585072014e-308]'),
        ('float-edges', b'[1.,.5,1e,1e+,1.e5]'),
        ('float-edges', b'1e'),
        ('float-edges', b'-'),
        ('float-edges', b'[0.1e1, 123.456e-2]'),
        ('float-overflow', b'1e400'),
        ('float-overflow', b'-1e400'),
        ('float-underflow', b'1e-400'),
        ('digits-4300', big),
        ('digits-4300', b'-' + big),
        ('digits-4300', b'[' + big + b']'),
        ('digits-4301', over),
        ('digits-4301', b'-' + over),
        ('digits-4301', b'{"a":' + over + b'}'),
        ('constant-top', b'NaN'),
        ('constant-top', b'Infinity'),
        ('constant-top', b'-Infinity'),
        ('constant-top', b'nan'),
        ('constant-top', b'Inf'),
        ('constant-nested', b'[1,NaN]'),
        ('constant-nested', b'{"a":Infinity}'),
        ('constant-nested', b'[[-Infinity]]'),
        ('duplicate-same', b'{"a":1,"a":1}'),
        ('duplicate-spelling', b'{"a":1,"\\u0061":2}'),
        ('duplicate-spelling', b'{"\\ud83d\\ude00":1,"\xf0\x9f\x98\x80":2}'),
        ('duplicate-nested', b'{"x":{"a":1,"a":2},"y":1}'),
        ('duplicate-in-array', b'[1,{"b":1,"b":2}]'),
        ('empty', b''),
        ('whitespace-only', b' \n\t\r '),
        ('types', b'{"":"","\\u0000":"\\u0000"}'),
        ('types', b'[true,false,null,truex]'),
        ('types', b'[tru]'),
        ('types', b'{"a" : 1 , "b" : [ ] }'),
    ]
    return items


def random_string(rng, keyish=False):
    """JSON の文字列の字面と、読んだ後の値。"""
    text, value = [], []
    for _ in range(rng.randrange(0, 4 if keyish else 12)):
        pick = rng.random()
        if pick < 0.4:
            c = chr(rng.randrange(0x20, 0x7f))
            if c in '"\\':
                text.append('\\' + c)
            else:
                text.append(c)
            value.append(c)
        elif pick < 0.5:
            escape = rng.choice('"\\/bfnrt')
            text.append('\\' + escape)
            value.append({'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}[escape])
        elif pick < 0.65:
            code = rng.choice([rng.randrange(0x00, 0x20), rng.randrange(0x80, 0x800), rng.randrange(0xd800, 0xe000),
                               rng.randrange(0xe000, 0x10000)])
            digits = '%04x' % code
            if rng.random() < 0.5:
                digits = digits.upper()
            text.append('\\u' + digits)
            value.append(chr(code))
        elif pick < 0.75:
            code = rng.randrange(0x10000, 0x110000)
            high, low = 0xd800 + ((code - 0x10000) >> 10), 0xdc00 + ((code - 0x10000) & 0x3ff)
            text.append('\\u%04x\\u%04x' % (high, low))
            value.append(chr(code))
        else:
            code = rng.choice([rng.randrange(0x80, 0xd800), rng.randrange(0xe000, 0x110000)])
            text.append(chr(code))
            value.append(chr(code))
    # 読んだ後に隣り合う孤立したサロゲートが組にならないことは、旧版の読み方で決まる(値は比べない)。
    return '"' + ''.join(text) + '"', ''.join(value)


def random_number(rng):
    sign = '-' if rng.random() < 0.3 else ''
    whole = rng.choice(['0', str(rng.randrange(1, 10)), str(rng.randrange(1, 10 ** rng.randrange(1, 30)))])
    fraction = '.' + ''.join(rng.choice('0123456789') for _ in range(rng.randrange(1, 18))) if rng.random() < 0.4 else ''
    exponent = ''
    if rng.random() < 0.3:
        exponent = rng.choice('eE') + rng.choice(['', '+', '-']) + str(rng.randrange(0, 400))
    return sign + whole + fraction + exponent


def random_json(rng, depth=0):
    space = lambda: rng.choice(['', '', ' ', '\n', '\t ', '\r\n'])  # noqa: E731
    pick = rng.random()
    if depth >= 8 or pick < 0.45:
        scalar = rng.random()
        if scalar < 0.3:
            return random_string(rng)[0]
        if scalar < 0.6:
            return random_number(rng)
        return rng.choice(['true', 'false', 'null'])
    if pick < 0.7:
        items = [space() + random_json(rng, depth + 1) + space() for _ in range(rng.randrange(0, 5))]
        return '[' + (','.join(items) if items else space()) + ']'
    keys, seen = [], set()
    for _ in range(rng.randrange(0, 5)):
        literal, value = random_string(rng, keyish=True)
        if value in seen:
            continue
        seen.add(value)
        keys.append(space() + literal + space() + ':' + space() + random_json(rng, depth + 1) + space())
    return '{' + (','.join(keys) if keys else space()) + '}'


def add_decoding(builder):
    natural = legacy_decode(b'0', 0)[0]
    checked_depth(natural)
    corpus = j1_corpus()
    if builder.scope == 'mini':
        corpus = corpus[:6] + [item for item in corpus if item[0] == 'digits-4301'][:1]
    for tag, raw in corpus:
        site, result = legacy_decode(raw, natural)
        builder.add('j1-decode-corpus', tag, {'raw': raw.hex(), 'depth': site}, result)
    rng = random.Random(484002)
    documents = J2_DOCUMENTS if builder.scope == 'full' else 3
    for _ in range(documents):
        raw = (random_json(rng) if rng.random() < 0.9 else ' ' + random_json(rng) + ' ').encode('utf-8', 'surrogatepass')
        site, result = legacy_decode(raw, natural)
        builder.add('j2-decode-random', 'valid', {'raw': raw.hex(), 'depth': site}, result)
        for _ in range(2):
            changed = bytearray(raw)
            if changed:
                changed[rng.randrange(len(changed))] = rng.randrange(256)
            site, result = legacy_decode(bytes(changed), natural)
            builder.add('j2-decode-random', 'mutated', {'raw': bytes(changed).hex(), 'depth': site}, result)


J3_NESTED = (
    ('arrays', lambda n: b'[' * n + b']' * n),
    ('dicts', lambda n: b'{"a":' * n + b'{}' + b'}' * n),
    ('nested-nan', lambda n: b'[' * n + b'NaN' + b']' * n),
    ('nested-duplicate', lambda n: b'[' * n + b'{"a":1,"a":2}' + b']' * n),
    ('missing-value-array', lambda n: b'[' * n + b'x'),
    ('missing-value-dict', lambda n: b'{"a":' * n + b'x'),
    ('errmsg-array', lambda n: b'[' * n + b'1 2' + b']' * n),
    ('errmsg-dict', lambda n: b'[' * n + b'{"a" 1}' + b']' * n),
    ('scanstring-control', lambda n: b'[' * n + b'"\x01"' + b']' * n),
    ('scanstring-escape', lambda n: b'[' * n + b'"\\q"' + b']' * n),
    ('scanstring-unterminated', lambda n: b'[' * n + b'"abc'),
    ('scanstring-short-u', lambda n: b'[' * n + b'"\\u12"' + b']' * n),
    ('nested-digits', lambda n: b'[' * n + b'7' * (INT_DIGITS + 1) + b']' * n),
)
J3_FLAT = (('flat-bom', '\ufeff0'.encode()), ('flat-utf8', b'\xff'), ('flat-extra-data', b'0 x'), ('flat-valid', b'0'),
           ('flat-missing-value', b'x'))


def recursion_at(raw, depth, call):
    site, result = legacy_decode(raw, depth, call)
    if result == ['err', 'recursion']:
        return True
    if site != depth:
        raise CheckError('決まった深さから呼べない(%d を求めて %r)' % (depth, site))
    return False


def natural_point(call):
    """J3 の呼出しの最も浅い深さ(recursion_at と同じ入れ子から測る)。"""
    return legacy_decode(b'0', 0, call)[0]


def add_depths(builder, call=direct):
    """J3: 失敗を作る場所ごとの境界。各点で深さの測り方を確かめ、境界の ±1 を作る。"""
    natural = natural_point(call)
    points = [natural, natural + 100, natural + 500, 900]
    nested, flat = J3_NESTED, J3_FLAT
    if builder.scope == 'mini':
        points, nested, flat = [natural], nested[:1], flat[:1]
    for depth in points:
        checked_depth(depth, call)
        for tag, make in nested:
            low, high = 0, 1100
            if recursion_at(make(low), depth, call) or not recursion_at(make(high), depth, call):
                raise CheckError('J3 の境界を探せない: %s(深さ %d)' % (tag, depth))
            while high - low > 1:
                middle = (low + high) // 2
                if recursion_at(make(middle), depth, call):
                    high = middle
                else:
                    low = middle
            for n in (low - 1, low, low + 1):
                site, result = legacy_decode(make(n), depth, call)
                if site not in (None, depth):
                    raise CheckError('決まった深さから呼べない(%d を求めて %r)' % (depth, site))
                item = builder.add('j3-decode-depth', tag, {'raw': make(n).hex(), 'depth': depth, 'n': n,
                                                            'boundary': low}, result)
                if n == low and depth == points[0] and tag == nested[0][0]:
                    builder.bias['j3'] = item['id']
    for tag, raw in flat:
        low, high = natural, RECURSION_LIMIT + 1
        if recursion_at(raw, low, call):
            raise CheckError('J3 の境界を探せない: %s' % tag)
        while high - low > 1:
            middle = (low + high) // 2
            if recursion_at(raw, middle, call):
                high = middle
            else:
                low = middle
        for depth in (high - 1, high, high + 1):
            checked_depth(depth, call)
            site, result = legacy_decode(raw, depth, call)
            if site not in (None, depth):
                raise CheckError('決まった深さから呼べない(%d を求めて %r)' % (depth, site))
            builder.add('j3-decode-depth', tag, {'raw': raw.hex(), 'depth': depth, 'boundary': high}, result)


# ---- J4・J5

def random_text_value(rng):
    chars = []
    for _ in range(rng.randrange(0, 10)):
        pick = rng.random()
        if pick < 0.4:
            chars.append(chr(rng.randrange(0x20, 0x7f)))
        elif pick < 0.5:
            chars.append(chr(rng.randrange(0, 0x20)))
        elif pick < 0.6:
            chars.append(chr(rng.randrange(0xd800, 0xe000)))
        elif pick < 0.8:
            chars.append(chr(rng.randrange(0x80, 0xd800)))
        else:
            chars.append(chr(rng.randrange(0xe000, 0x110000)))
    return ''.join(chars)


def random_value(rng, depth=0):
    pick = rng.random()
    if depth >= 8 or pick < 0.5:
        scalar = rng.random()
        if scalar < 0.1:
            return None
        if scalar < 0.2:
            return rng.random() < 0.5
        if scalar < 0.4:
            return rng.choice([rng.randrange(-1000, 1000), rng.randrange(-2 ** 70, 2 ** 70),
                               int_value('9' * rng.randrange(1, 60))])
        if scalar < 0.6:
            if rng.random() < 0.05:
                return rng.choice([float('inf'), float('-inf'), float('nan')])
            if rng.random() < 0.5:
                return struct.unpack('>d', rng.getrandbits(64).to_bytes(8, 'big'))[0]
            return rng.uniform(-1e6, 1e6) * 10 ** rng.randrange(-30, 30)
        return random_text_value(rng)
    if pick < 0.75:
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))]
    return {random_text_value(rng): random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))}


def power_of_two(exponent):
    """2 の exponent 乗(−1074〜1023)を、ビットの並びから正確に作る。"""
    if exponent >= -1022:
        bits = (exponent + 1023) << 52
    else:
        bits = 1 << (exponent + 1074)
    return struct.unpack('>d', bits.to_bytes(8, 'big'))[0]


def powers_of_two():
    """±2^e(e = −1074〜1023)の全部。ちょうど 2 の累乗の小数は、元に戻る幅が下側だけ半分になる。"""
    values = []
    for exponent in range(-1074, 1024):
        values += [power_of_two(exponent), -power_of_two(exponent)]
    return values


# J5 にも入れる 2 の累乗(符号付きの指数)。上限ちょうどの json の量の境目で、字面の長さの差が出る。
J5_POWERS = (-1074, -1022, -44, -24, 0, 53, 89, 1023)


def j4_named():
    surrogates = ['\ud800', '\udbff', '\udc00', '\udfff']
    keyed = {'\U00010000': 1, '\ue000': 2, 'b': 3, 'a': 4, '\ud800': 5, '': 6}
    return [
        ('zero', 0.0), ('minus-zero', -0.0), ('float-1e16', 1e16), ('float-1e15', 1e15), ('float-1e-4', 1e-4),
        ('float-1e-5', 1e-5), ('float-min', 5e-324), ('float-max', sys.float_info.max),
        ('infinity', float('inf')), ('infinity', float('-inf')), ('nan', float('nan')),
        ('int-zero', 0), ('int-minus-one', -1), ('int-2-63', 2 ** 63), ('int-2-63', -2 ** 63),
        ('digits-4300', int_value('9' * INT_DIGITS)), ('digits-4300', -int_value('9' * INT_DIGITS)),
        ('digits-4301', int_value('9' * (INT_DIGITS + 1))),
        ('ascii', ''.join(map(chr, range(0x80)))),
        ('code-points', '\x80'), ('code-points', '\u07ff'), ('code-points', '\u0800'), ('code-points', '\uffff'),
        ('code-points', '\U00010000'), ('code-points', '\U0010ffff'),
        ('lone-surrogates', surrogates[0]), ('lone-surrogates', surrogates[1]), ('lone-surrogates', surrogates[2]),
        ('lone-surrogates', surrogates[3]),
        ('surrogate-pair', '\ud83d\ude00'), ('surrogate-pair', '\U0001f600'),
        ('key-order', keyed), ('key-order', {'\U00010000': [], '\ue000': {}}),
        ('empty-containers', []), ('empty-containers', {}), ('empty-containers', [[], {}]),
        ('nested', {'a': [1, {'b': [None, True, False, 1.5, 'x']}], 'c': {'d': {'e': {}}}}),
        ('nested', [[[[[[[[0]]]]]]]]),
    ] + [('float-power-of-two', value) for value in powers_of_two()]


def legacy_encodings(value):
    rt = legacy()
    out = []
    for function in (rt.encoded, lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True,
                                                      separators=(',', ':')).encode('utf-8')):
        try:
            out.append(['ok', function(value).hex()])
        except ValueError:
            out.append(['err', 'value'])
        except BaseException as error:  # noqa: BLE001
            out.append(['err', 'other', type(error).__name__])
    return out


def legacy_response(value, cond):
    rt = legacy()
    budget = make_budget(cond)
    holder = type('Holder', (), {})()
    holder.budget = budget
    try:
        result = ['ok', rt.Runtime.response_bytes(holder, value).hex()]
    except rt.Failure as failure:
        result = ['err', 'failure', failure.reason, failure.code]
    except ValueError:
        result = ['err', 'value']
    except BaseException as error:  # noqa: BLE001
        result = ['err', 'other', type(error).__name__]
    return {'result': result, 'budget': log_text(budget)}


def result_shapes():
    binding = {'manifest_sha256': 'a' * 64, 'snapshot_sha256': 'b' * 64}
    created = {'version': 1, 'status': 'plan-created', 'plan_path_b64': 'L3RtcC9wbGFu', 'plan_sha256': 'c' * 64,
               'context_id': 0, 'state_binding': binding}
    extended = dict(created, status='plan-extended', input_plan_sha256='d' * 64, effective_exclusions_sha256='e' * 64)
    observed = {'version': 1, 'status': 'observed', 'operation': 'status', 'context_id': 2, 'view_policy': 'raw-selected-v1',
                'scope': {'paths': ['YQ==', 'Yg=='], 'excluded': []},
                'data': {'records': [{'path_b64': 'YQ==', 'stages': [{'mode': 33188, 'oid': 'f' * 40}], 'size': 3}],
                         'truncated': False, 'counts': {'entries': 1}},
                'warnings': ['settings-not-applied'], 'effective_exclusions_sha256': 'e' * 64, 'plan_sha256': 'c' * 64,
                'baseline_record_updated': False, 'resume_authorized': False}
    return [('plan-created', created), ('plan-extended', extended), ('observed', observed)]


def add_encoding(builder):
    rng = random.Random(484004)
    count = J4_VALUES if builder.scope == 'full' else 3
    values = [('random', random_value(rng)) for _ in range(count)]
    named = j4_named() if builder.scope == 'full' else j4_named()[:4]
    encodable = []
    for tag, value in values + named:
        result = legacy_encodings(value)
        builder.add('j4-encode', tag, {'value': typed(value)}, result)
        # 2 の累乗は J5 に数件だけ入れる(J5_POWERS)。
        if result[0][0] == 'ok' and tag != 'float-power-of-two':
            encodable.append(value)
    inputs = [('encodable', value) for value in encodable]
    for exponent in J5_POWERS:
        inputs.append(('float-power-of-two', power_of_two(exponent)))
        inputs.append(('float-power-of-two', -power_of_two(exponent)))
    for tag, value in result_shapes():
        inputs.append((tag, value))
        if builder.scope == 'mini':
            break
    for position in ('first', 'middle', 'last', 'nested-key'):
        shape = {'a': 1, 'b': [1, 2, 3], 'c': {'d': 'x'}}
        if position == 'first':
            shape['a'] = float('inf')
        elif position == 'middle':
            shape['b'][1] = float('-inf')
        elif position == 'last':
            shape['c']['d'] = float('nan')
        else:
            shape['c'] = {'d': 'x', 'z': [float('inf')]}
        inputs.append(('infinity-position', shape))
        if builder.scope == 'mini':
            break
    for tag, value in inputs:
        normal = legacy_response(value, ['normal'])
        log = normal['budget'].split(';') if normal['budget'] else []
        # 通常・json の上限=合計と合計−1・停止・期限切れ・N 回目(1・中央・末尾・末尾+1)の 9 条件。
        for cond in [['normal']] + conditions_from(log, 'json'):
            result = normal if cond == ['normal'] else legacy_response(value, cond)
            builder.add('j5-response', tag, {'value': typed(value), 'cond': cond}, result)


# ---- J6・J7・J8

def legacy_unb64(value):
    rt = legacy()
    try:
        return ['ok', rt.unb64(value).hex()]
    except rt.Failure as failure:
        return ['err', 'failure', failure.reason, failure.code]
    except BaseException as error:  # noqa: BLE001
        return ['err', 'other', type(error).__name__]


def j6_named():
    """J6 の unb64 の名前付きの一覧(文字列でない値を含む)。"""
    return [
        ('canonical', 'Zg=='), ('canonical', 'Zm8='), ('canonical', 'Zm9v'), ('canonical', '+/8='),
        ('canonical', 'AQIDBA=='),
        ('padding', 'Zg'), ('padding', 'Zg='), ('padding', 'Zg==='), ('padding', 'Zm9v='), ('padding', 'Z==='),
        ('remaining-bits', 'Zh=='), ('remaining-bits', 'Zm9='), ('remaining-bits', 'AB=='),
        ('whitespace', 'Zm 9v'), ('whitespace', 'Zm9v\n'), ('whitespace', ' Zm9v'), ('whitespace', 'Zm9v\t'),
        ('non-ascii', 'Zm9v\u00e9'), ('non-ascii', '\udc80AAA'), ('non-ascii', 'Zm9v\U0001f600'), ('non-ascii', '-_8='),
        ('equals-position', '=Zg='), ('equals-position', 'Z=g='), ('equals-position', 'Zg==Zg=='),
        ('equals-position', 'Zg=a'),
        ('nul', 'AA=='), ('nul', 'AAAA'), ('nul', 'YQBi'),
        ('empty', ''),
    ] + [('non-string', value) for value in (0, 1.5, True, False, None, [], {}, ['Zg=='], {'a': 'Zg=='})]


def add_base64(builder):
    rt = legacy()
    named = j6_named()
    rng = random.Random(484006)
    count = J6_RANDOM if builder.scope == 'full' else 2
    randoms = []
    for _ in range(count):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))
        randoms.append(('random-canonical', rt.b64(raw)))
    for _ in range(count):
        text = list(rt.b64(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))))
        how = rng.randrange(4)
        position = rng.randrange(len(text))
        if how == 0:
            del text[position]
        elif how == 1:
            text.insert(position, rng.choice('=+/A\n \u00e9'))
        elif how == 2:
            text[position] = rng.choice('=-_.!\x00')
        else:
            text.append('=')
        randoms.append(('random-noncanonical', ''.join(text)))
    if builder.scope == 'mini':
        named = named[:3]
    for tag, value in named + randoms:
        builder.add('j6-base64', tag, {'op': 'unb64', 'value': typed(value)}, legacy_unb64(value))
    lengths = B64_LENGTHS if builder.scope == 'full' else (0, 1, 2)
    for length in lengths:
        raw = bytes(rng.randrange(256) for _ in range(length))
        builder.add('j6-base64', 'b64', {'op': 'b64', 'raw': raw.hex()}, ['ok', cps(rt.b64(raw))])


def j7_named():
    return [
        ('exact', {'a': 1, 'b': 2}, 'a b'), ('exact', {'b': 1, 'a': 2}, 'a b'), ('exact', {}, ''),
        ('exact', {'a': 1}, 'a a'),
        ('missing', {'a': 1}, 'a b'), ('missing', {}, 'a'),
        ('extra', {'a': 1, 'b': 2, 'c': 3}, 'a b'), ('extra', {'a': 1}, ''),
        ('non-dict', [], ''), ('non-dict', ['a'], 'a'), ('non-dict', 'a', 'a'), ('non-dict', None, ''),
        ('non-dict', True, ''), ('non-dict', 1, ''),
        ('separators', {'a': 1, 'b': 2}, '  a   b  '), ('separators', {'a': 1, 'b': 2}, 'a\tb'),
        ('separators', {'a': 1, 'b': 2}, 'a\nb\r\n'), ('separators', {'a': 1, 'b': 2}, 'a\x0b\x0cb'),
        ('separators', {'a': 1, 'b': 2}, 'a\x1cb'), ('separators', {'a': 1, 'b': 2}, 'a　b '),
        ('separators', {'a': 1, 'b': 2}, 'a\x85b\xa0'), ('separators', {'a b': 1}, 'a b'),
    ]


def add_keys(builder):
    rt = legacy()
    named = j7_named()
    if builder.scope == 'mini':
        named = named[:2]
    for tag, value, names in named:
        try:
            rt.keys(value, names)
            result = ['ok']
        except rt.Failure as failure:
            result = ['err', 'failure', failure.reason, failure.code]
        builder.add('j7-keys', tag, {'value': typed(value), 'names': cps(names)}, result)


YEAR_2400_NS = 13569465600 * 10 ** 9 + 123456789


J8_FILES = (('regular', 'regular', b'diagnostic\n'), ('empty', 'empty', b''), ('year-2400', 'year-2400', b'x'))


def add_metadata(builder):
    rt = legacy()
    stable = builder.root / 'stable'
    stable.mkdir()
    for tag, name, data in J8_FILES:
        path = stable / name
        path.write_bytes(data)
        if tag == 'year-2400':
            try:
                os.utime(path, ns=(YEAR_2400_NS, YEAR_2400_NS))
                ok = os.lstat(path).st_mtime_ns == YEAR_2400_NS
            except (OSError, OverflowError) as error:
                ok = False
                reason = str(error)
            else:
                reason = '時刻を付けても読み戻せない'
            if not ok:
                path.unlink()
                builder.missing_input('j8-metadata/year-2400', reason)
                continue
        raw = os.fsencode(path)
        reader = rt.Reader(make_budget(['normal']))
        info = reader.lstat(raw, False)
        print_ = [str(value) for value in rt.fingerprint(info)]
        builder.add('j8-metadata', tag, {'path': raw.hex()}, ['ok', print_, rt.encoded(rt.metadata(info)).hex()])


# ---------------------------------------------------------------- R の筋書き

def errno_name(number):
    return errno.errorcode.get(number, 'E%d' % number)


REAL = {'open': os.open, 'read': os.read, 'close': os.close, 'scandir': os.scandir, 'getcwdb': os.getcwdb}


def content_of(spec):
    """ファイルの中身: 16 進の字面か、{"size": n, "seed": k}(i 番目のバイトは (i * 7 + k) % 256)。"""
    if isinstance(spec, str):
        return bytes.fromhex(spec)
    size, seed = spec['size'], spec['seed']
    pattern = bytes((i * 7 + seed) % 256 for i in range(256 * 7))
    whole, rest = divmod(size, len(pattern))
    return pattern * whole + pattern[:rest]


def build_tree(root, tree):
    os.mkdir(root, 0o700)
    os.chmod(root, 0o755)
    for entry in tree:
        kind, rel = entry[0], bytes.fromhex(entry[1])
        path = os.path.join(root, rel)
        if kind == 'dir':
            os.mkdir(path)
            os.chmod(path, entry[2])
        elif kind == 'file':
            with open(path, 'wb') as handle:
                handle.write(content_of(entry[2]))
            os.chmod(path, entry[3])
        elif kind == 'link':
            os.symlink(bytes.fromhex(entry[2]), path)
        elif kind == 'fifo':
            os.mkfifo(path)
            os.chmod(path, entry[2])
        elif kind == 'files':
            # entry[2] 個の空のファイル(名前は 5 桁の番号)。
            for index in range(entry[2]):
                with open(os.path.join(path, b'%05d' % index), 'wb'):
                    pass
        elif kind == 'chmod':
            os.chmod(path, entry[2])
        else:
            raise CheckError('木の項目が不明: %r' % (entry,))


def remove_tree(root):
    """権限を戻してから消す(リンクは辿らない)。"""
    if not os.path.lexists(root):
        return
    for directory, names, files in os.walk(root, topdown=True):
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o700)
    shutil.rmtree(root)


def mutate(root, op):
    kind = op[0]
    path = os.path.join(root, bytes.fromhex(op[1])) if len(op) > 1 else None
    if kind == 'm-append':
        with open(path, 'ab') as handle:
            handle.write(bytes.fromhex(op[2]))
    elif kind == 'm-replace':
        temporary = path + b'.new'
        with open(temporary, 'wb') as handle:
            handle.write(bytes.fromhex(op[2]))
        os.chmod(temporary, op[3])
        os.replace(temporary, path)
    elif kind == 'm-file':
        with open(path, 'wb') as handle:
            handle.write(bytes.fromhex(op[2]))
    elif kind == 'm-chmod':
        os.chmod(path, op[2])
    elif kind == 'm-mtime':
        os.utime(path, ns=(op[2], op[2]))
    elif kind == 'm-mkdir':
        os.mkdir(path)
    elif kind == 'm-rmdir':
        os.rmdir(path)
    elif kind == 'm-unlink':
        os.unlink(path)
    elif kind == 'm-rename':
        os.replace(path, os.path.join(root, bytes.fromhex(op[2])))
    elif kind == 'm-symlink':
        os.symlink(bytes.fromhex(op[2]), path)
    else:
        raise CheckError('変更の操作が不明: %r' % (op,))


def summary(info):
    if info is None:
        return None
    return [stat.S_IFMT(info.st_mode), info.st_mode, info.st_size, info.st_uid == os.geteuid()]


class LegacyFaults:
    """R8 の差し込み。Reader の操作の間だけ os の関数を差し替える。close は本当に閉じてから失敗を出す。"""

    def __init__(self, spec, root, cwd):
        self.spec = spec or {}
        self.root = root
        self.cwd = cwd
        self.counts = {'open': 0, 'read': 0, 'close': 0, 'scandir': 0}
        self.labels = []
        self.first_read_done = False

    def number(self, key):
        value = self.spec.get(key)
        return None if value is None else (value[0], getattr(errno, value[-1]))

    @contextlib.contextmanager
    def installed(self):
        saved = {name: getattr(os, name) for name in REAL}
        os.open, os.read, os.close, os.scandir, os.getcwdb = self.open, self.read, self.close, self.scandir, self.getcwdb
        try:
            yield
        finally:
            for name, function in saved.items():
                setattr(os, name, function)

    def open(self, path, flags, *args, **kwargs):
        self.counts['open'] += 1
        if flags & os.O_NONBLOCK and self.spec.get('replace_before_open'):
            source, target = self.spec['replace_before_open']
            os.replace(os.path.join(self.root, bytes.fromhex(source)), os.path.join(self.root, bytes.fromhex(target)))
        error = self.number('open_error')
        if error and error[0] == self.counts['open']:
            raise OSError(error[1], 'injected')
        return REAL['open'](path, flags, *args, **kwargs)

    def read(self, fd, count):
        self.counts['read'] += 1
        error = self.number('read_error')
        if error and error[0] == self.counts['read']:
            raise OSError(error[1], 'injected')
        if self.spec.get('read_empty_at') == self.counts['read']:
            return b''
        data = REAL['read'](fd, count)
        if self.spec.get('append_after_first_read') and not self.first_read_done:
            self.first_read_done = True
            name, extra = self.spec['append_after_first_read']
            with open(os.path.join(self.root, bytes.fromhex(name)), 'ab') as handle:
                handle.write(bytes.fromhex(extra))
        return data

    def close(self, fd):
        self.counts['close'] += 1
        caller = sys._getframe(1)
        self.labels.append((caller.f_code.co_name, caller.f_lineno))
        error = self.number('close_error')
        if error and error[0] == self.counts['close']:
            try:
                REAL['close'](fd)
            except OSError:
                pass
            raise OSError(error[1], 'injected')
        return REAL['close'](fd)

    def scandir(self, fd):
        self.counts['scandir'] += 1
        iterator = REAL['scandir'](fd)
        spec = self.spec.get('names_error')
        if spec and spec[0] == self.counts['scandir']:
            return FailingScandir(iterator, spec[1], getattr(errno, spec[2]))
        return iterator

    def getcwdb(self):
        error = self.spec.get('cwd_error')
        if error:
            raise OSError(getattr(errno, error), 'injected')
        if self.cwd is not None:
            return self.cwd
        return REAL['getcwdb']()


class FailingScandir:
    """M 件を渡した後に OSError を出す一覧(本物の一覧は閉じる)。"""

    def __init__(self, real, after, number):
        self.real, self.after, self.number, self.count = real, after, number, 0

    def __enter__(self):
        return self

    def __exit__(self, *details):
        self.real.close()

    def __iter__(self):
        return self

    def __next__(self):
        if self.count == self.after:
            self.real.close()
            raise OSError(self.number, 'injected')
        self.count += 1
        return next(self.real)


def outside_lstat(path):
    return os.lstat(path)


def legacy_op(reader, op, root, faults):
    rt = legacy()
    name = op[0]
    path = bytes.fromhex(op[1]) if len(op) > 1 and isinstance(op[1], str) else None
    with faults.installed():
        try:
            if name == 'owner':
                info = None if op[2] == 'none' else outside_lstat(path)
                reader.owner(path, info, op[3])
                return ['ok', None]
            if name == 'directory':
                fd = reader.directory(path)
                REAL['close'](fd)
                return ['ok', 'fd']
            if name == 'lstat':
                return ['ok', summary(reader.lstat(path, op[2]))]
            if name == 'read':
                data = reader.read(path, op[2], op[3] or '', op[4])
                return ['ok', None if data is None else data.hex()]
            if name == 'names':
                return ['ok', [value.hex() for value in reader.names(path, op[2], op[3])]]
            if name == 'verify':
                reader.verify()
                return ['ok', None]
            if name == 'add-missing':
                reader.missing.add(path)
                return ['ok', None]
            if name == 'put-file':
                reader.files[path] = (rt.fingerprint(outside_lstat(bytes.fromhex(op[2]))), op[3])
                return ['ok', None]
            if name == 'get-file':
                held = reader.files.get(path)
                return ['ok', None if held is None else held[1]]
        except rt.Failure as failure:
            return ['err', 'failure', failure.reason, failure.code]
        except OSError as error:
            return ['err', 'os', errno_name(error.errno)]
        except ValueError as error:
            if 'null' in str(error):
                return ['err', 'nul']
            return ['err', 'other', 'ValueError']
        except BaseException as error:  # noqa: BLE001
            return ['err', 'other', type(error).__name__]
    raise CheckError('操作が不明: %r' % (op,))


def records(reader):
    return {
        'files': [[path.hex(), held[1]] for path, held in reader.files.items()],
        'dirs': [[path.hex(), held[2]] for path, held in reader.dirs.items()],
        'missing': sorted(path.hex() for path in reader.missing),
        'sets': [[path.hex(), [name.hex() for name in names]] for path, names in reader.sets.items()],
        'owners': [[path.hex(), held[1], held[0] == os.geteuid()] for path, held in reader.owners.items()],
    }


def check_missing_count(item, op, reader):
    """R7 のほかは、確かめ直しの時点の 1 つの Reader の missing を 1 件までにする。

    旧版の missing は集合で、確かめ直しの順が hash の乱数で実行ごとに変わり、2 件以上だと予算の操作の列が揺れる。
    そのような筋書きは、比較器の誤りとして失敗にする(R7 の筋書きだけが several_missing を持つ)。
    """
    if op[0] == 'verify' and not item.get('several_missing') and len(reader.missing) > 1:
        raise CheckError('確かめ直しの時点で missing が %d 件ある筋書き(R7 のほかは 1 件まで): 根 %s'
                         % (len(reader.missing), os.fsdecode(bytes.fromhex(item['root']))))


def run_scenario(item, keep_labels=False):
    """木を作り、操作を順に流し(Reader の操作が失敗したら止める)、記録と予算の操作の列を返し、木を消す。"""
    rt = legacy()
    root = bytes.fromhex(item['root'])
    remove_tree(root)
    build_tree(root, item['tree'])
    try:
        budget = make_budget(item['cond'])
        reader = rt.Reader(budget)
        cwd = bytes.fromhex(item['cwd']) if item.get('cwd') else None
        faults = LegacyFaults(item.get('faults'), root, cwd)
        results = []
        for op in item['ops']:
            if op[0].startswith('m-'):
                mutate(root, op)
                results.append(['ok', None])
                continue
            check_missing_count(item, op, reader)
            result = legacy_op(reader, op, root, faults)
            results.append(result)
            if result[0] != 'ok':
                break
        outcome = {'ops': results, 'records': records(reader), 'budget': log_text(budget)}
        if keep_labels:
            outcome['labels'] = faults.labels
            outcome['opens'] = faults.counts['open']
        return outcome
    finally:
        remove_tree(root)


def hexpath(*parts):
    return b'/'.join(parts).hex()


class Scenarios:
    """R の筋書きの組み立て。"""

    def __init__(self, builder):
        self.builder = builder
        self.base = builder.root / 'trees'
        self.base.mkdir()
        self.count = 0

    def root(self):
        self.count += 1
        return os.fsencode(self.base / ('t%03d' % self.count))

    def add(self, group, tag, tree, ops, *, cwd=None, faults=None, conds=(('normal',),), child=None):
        root = self.root()
        items = []
        for cond in conds:
            item = {'root': root.hex(), 'tree': tree, 'ops': ops, 'cond': list(cond), 'cwd': cwd,
                    'faults': faults, 'child': child}
            items.append(item)
        for item in items:
            result = run_child(item) if child else run_scenario(item)
            self.builder.add(group, tag, item, result)
        return root


def file_entry(name, data=b'abc', mode=0o644):
    return ['file', name.hex() if isinstance(name, bytes) else name.encode().hex(),
            data.hex() if isinstance(data, bytes) else data, mode]


def rel(name):
    return name.encode().hex() if isinstance(name, str) else name.hex()


def add_r1(scenarios):
    sizes = (0, 1, 65535, 65536, 65537, 131073)
    kinds = (None, 'management_copy', 'body')
    if scenarios.builder.scope == 'mini':
        sizes, kinds = (1,), (None,)
    for size in sizes:
        for limit in (size - 1, size, size + 1):
            if limit < 0:
                continue
            for kind in kinds:
                root = scenarios.base
                tree = [file_entry('f', {'size': size, 'seed': size % 251})]
                target = os.fsencode(root / ('t%03d' % (scenarios.count + 1)) / 'f')
                scenarios.add('r1-read-limit', 'size-limit-kind', tree, [['read', target.hex(), limit, kind, False]])
    if scenarios.builder.scope == 'mini':
        return
    for size in (16 * MIB, 16 * MIB + 1):
        tree = [file_entry('f', {'size': size, 'seed': 3})]
        target = os.fsencode(scenarios.base / ('t%03d' % (scenarios.count + 1)) / 'f')
        scenarios.add('r1-read-limit', 'default-limit', tree,
                      [['read', target.hex(), 16 * MIB, 'management_copy', False]])


def next_root(scenarios):
    return os.fsencode(scenarios.base / ('t%03d' % (scenarios.count + 1)))


def add_r2(scenarios):
    builder = scenarios.builder
    base = [['dir', rel('d'), 0o755], file_entry('f'), file_entry('d/g', b'xyz')]
    cases = [
        ('absolute', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]], False),
        ('relative', lambda r: [['read', b'd/g'.hex(), 100, 'management_copy', False]], True),
        ('dotdot', lambda r: [['read', (r + b'/d/../f').hex(), 100, 'management_copy', False]], False),
        ('dot', lambda r: [['read', (r + b'/./d/./g').hex(), 100, 'management_copy', False]], False),
        ('double-slash', lambda r: [['read', (b'/' + r + b'/f').hex(), 100, 'management_copy', False]], False),
        ('trailing-slash', lambda r: [['lstat', (r + b'/d/').hex(), True]], False),
        ('empty', lambda r: [['lstat', b''.hex(), False]], True),
        ('nul-middle', lambda r: [['read', (r + b'/d\x00x/g').hex(), 100, 'management_copy', False]], False),
        ('nul-last', lambda r: [['read', (r + b'/f\x00').hex(), 100, 'management_copy', False]], False),
    ]
    if builder.scope == 'mini':
        cases = cases[:2]
    for tag, ops, relative in cases:
        root = next_root(scenarios)
        scenarios.add('r2-paths', tag, base, ops(root), cwd=root.hex() if relative else None)
    if builder.scope == 'mini':
        return
    root = next_root(scenarios)
    name = b'\xff\xfe-name'
    probe = builder.root / 'probe-nonutf8'
    try:
        with open(os.fsencode(probe) + b'-' + name, 'wb'):
            pass
        os.unlink(os.fsencode(probe) + b'-' + name)
    except OSError as error:
        builder.missing_input('r2-paths/non-utf8', str(error))
        return
    scenarios.add('r2-paths', 'non-utf8', [file_entry(name, b'raw')],
                  [['read', (root + b'/' + name).hex(), 100, 'management_copy', False],
                   ['names', root.hex(), 'management_entries', True]])


def add_r3(scenarios):
    tree = [file_entry('f'), ['dir', rel('d'), 0o755], file_entry('d/g'), ['link', rel('link'), rel('f')],
            ['link', rel('dlink'), rel('d')], ['fifo', rel('fifo'), 0o600], ['dir', rel('locked'), 0o755],
            file_entry('locked/x'), file_entry('secret', b'no', 0o600), ['chmod', rel('locked'), 0o000],
            ['chmod', rel('secret'), 0o000]]
    cases = [
        ('link-last', lambda r: [['read', (r + b'/link').hex(), 100, None, False]]),
        ('link-middle', lambda r: [['read', (r + b'/dlink/g').hex(), 100, None, False]]),
        ('fifo', lambda r: [['read', (r + b'/fifo').hex(), 100, None, False]]),
        ('read-directory', lambda r: [['read', (r + b'/d').hex(), 100, None, False]]),
        ('directory-file', lambda r: [['directory', (r + b'/f').hex()]]),
        ('missing-true', lambda r: [['read', (r + b'/none/x').hex(), 100, None, True]]),
        ('missing-false', lambda r: [['read', (r + b'/none/x').hex(), 100, None, False]]),
        ('no-permission-directory', lambda r: [['read', (r + b'/locked/x').hex(), 100, None, False]]),
        ('no-permission-file', lambda r: [['read', (r + b'/secret').hex(), 100, None, False]]),
    ]
    if scenarios.builder.scope == 'mini':
        cases = cases[:1]
    for tag, ops in cases:
        scenarios.add('r3-links-types', tag, tree, ops(next_root(scenarios)))


def add_r4(scenarios):
    tree = [file_entry('f'), ['dir', rel('d'), 0o755]]
    mini = scenarios.builder.scope == 'mini'
    for path in (b'/', b'/usr'):
        for code in (22, 2, 23):
            for info in ('none', 'outside'):
                scenarios.add('r4-owner', 'foreign', tree, [['owner', path.hex(), info, code]])
                if mini:
                    return
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'own-directory', tree,
                  [['owner', (root + b'/f').hex(), 'none', 22], ['directory', root.hex()]])
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'own-read', tree,
                  [['owner', (root + b'/f').hex(), 'none', 22], ['read', (root + b'/f').hex(), 100, 'management_copy', False]])
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'own-verify', tree, [['owner', (root + b'/d').hex(), 'outside', 2], ['verify']])
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'same-path-twice', tree,
                  [['owner', (root + b'/f').hex(), 'none', 22], ['owner', (root + b'/d').hex(), 'none', 2],
                   ['owner', (root + b'/f').hex(), 'none', 2], ['verify']])
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'double-slash-owner', tree,
                  [['owner', (b'/' + root + b'/f').hex(), 'none', 23], ['verify']])
    root = next_root(scenarios)
    scenarios.add('r4-owner', 'double-slash-directory', tree,
                  [['directory', (b'/' + root + b'/d').hex()], ['verify']])


def names_tree(count):
    return [['dir', rel('d'), 0o755], ['files', rel('d'), count]]


def add_r5(scenarios):
    cases = [
        ('empty', 0, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]]),
        ('one', 1, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]]),
        ('limit', 2000, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]]),
        ('over-limit', 2001, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]]),
        ('worktree-entries', 2001, lambda r: [['names', (r + b'/d').hex(), 'worktree_entries', True]]),
        ('record-true', 3, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True], ['verify']]),
        ('record-false', 3, lambda r: [['names', (r + b'/d').hex(), 'management_entries', False], ['verify']]),
        ('changed', 3, lambda r: [['names', (r + b'/d').hex(), 'management_entries', True],
                                  ['m-file', rel('d/new'), ''], ['names', (r + b'/d').hex(), 'management_entries', True]]),
    ]
    if scenarios.builder.scope == 'mini':
        cases = cases[1:2]
    for tag, count, ops in cases:
        scenarios.add('r5-names', tag, names_tree(count), ops(next_root(scenarios)))


def verify_setup(root):
    return [['read', (root + b'/f').hex(), 100, 'management_copy', False], ['lstat', (root + b'/gone').hex(), True],
            ['names', (root + b'/d').hex(), 'management_entries', True], ['owner', (root + b'/f').hex(), 'none', 22]]


def add_r6(scenarios):
    tree = [file_entry('f'), ['dir', rel('d'), 0o755], file_entry('d/a'), file_entry('d/b'), ['dir', rel('d/sub'), 0o755]]
    cases = [
        ('replace-same', [['m-replace', rel('f'), b'abc'.hex(), 0o644]]),
        ('chmod', [['m-chmod', rel('f'), 0o600]]),
        ('append', [['m-append', rel('f'), b'!'.hex()]]),
        ('mtime', [['m-mtime', rel('f'), 1600000000123456789]]),
        ('missing-created', [['m-file', rel('gone'), '']]),
        ('name-added', [['m-file', rel('d/c'), '']]),
        ('name-removed', [['m-unlink', rel('d/a')]]),
        ('directory-renamed', [['m-rename', rel('d'), rel('moved')]]),
        ('directory-link', [['m-rename', rel('d'), rel('moved')], ['m-symlink', rel('d'), rel('moved')]]),
        ('directory-removed', [['m-unlink', rel('d/a')], ['m-unlink', rel('d/b')], ['m-rmdir', rel('d/sub')],
                               ['m-rmdir', rel('d')]]),
        ('unchanged', []),
    ]
    if scenarios.builder.scope == 'mini':
        cases = cases[:1]
    for tag, changes in cases:
        root = next_root(scenarios)
        scenarios.add('r6-verify', tag, tree, verify_setup(root) + changes + [['verify']])
    if scenarios.builder.scope == 'mini':
        return
    root = next_root(scenarios)
    scenarios.add('r6-verify', 'direct-missing', tree,
                  [['add-missing', (root + b'/later').hex()], ['verify'], ['m-file', rel('later'), ''], ['verify']])
    root = next_root(scenarios)
    sha = hashlib.sha256(b'abc').hexdigest()
    scenarios.add('r6-verify', 'direct-files', tree,
                  [['put-file', (root + b'/f').hex(), (root + b'/f').hex(), sha], ['get-file', (root + b'/f').hex()],
                   ['verify'], ['put-file', (root + b'/d/a').hex(), (root + b'/f').hex(), sha], ['verify']])


def add_r7(scenarios):
    tree = [file_entry('f')]
    root = next_root(scenarios)
    ops = [['lstat', (root + b'/nodir/x').hex(), True], ['lstat', (root + b'/later').hex(), True],
           ['m-file', rel('nodir'), ''], ['m-file', rel('later'), ''], ['verify']]
    builder = scenarios.builder
    scenarios.count += 1
    item = {'root': root.hex(), 'tree': tree, 'ops': ops, 'cond': ['normal'], 'cwd': None, 'faults': None,
            'child': None, 'several_missing': True}
    outcomes = []
    for seed in range(R7_SEED_LIMIT):
        result = run_child(item, seed=seed)
        if result not in outcomes:
            outcomes.append(result)
        if len(outcomes) == 2:
            break
    if len(outcomes) != 2:
        raise CheckError('R7 の旧版の 2 通りの結果が、種 %d 個でそろわない(%d 通り)' % (R7_SEED_LIMIT, len(outcomes)))
    added = builder.add('r7-missing-order', 'two-reasons', item, outcomes[0])
    builder.alternatives[added['id']] = outcomes


def legacy_close_sites():
    """旧版の close の場所(関数の名前と行の番号 → 場所の名前)。旧版の字面から求める。"""
    rt = legacy()
    sites = {}
    wanted = {
        'directory': [('os.close(fd); fd=child', 'directory-step'), ('os.close(fd); raise', 'directory-except')],
        'lstat': [('finally: os.close(fd)', 'lstat-finally')],
        'read': [('if fd is not None: os.close(fd)', 'read-file'), ('os.close(parent)', 'read-parent')],
        'names': [('finally: os.close(fd)', 'names-finally')],
        'verify': [('finally: os.close(fd)', 'verify-dirs'), ('finally: os.close(fd)', 'verify-sets')],
    }
    for function, marks in wanted.items():
        lines, start = inspect.getsourcelines(getattr(rt.Reader, function))
        used = set()
        for text, label in marks:
            index = next(i for i, line in enumerate(lines) if text in line and i not in used)
            used.add(index)
            sites[(function, start + index)] = label
    return sites


def close_index(item, label):
    """通常の実行で、場所 `label` の最初の close が何回目か。"""
    probe = dict(item, faults=None)
    result = run_scenario(probe, keep_labels=True)
    sites = legacy_close_sites()
    for index, site in enumerate(result['labels'], 1):
        if sites.get(tuple(site)) == label:
            return index
    raise CheckError('close の場所 %s を通らない筋書き' % label)


def add_r8(scenarios):
    builder = scenarios.builder
    tree = [file_entry('f', b'abc'), file_entry('g', b'xyz'), ['dir', rel('d'), 0o755], file_entry('d/a'),
            file_entry('d/b'), file_entry('d/c')]
    plain = [
        ('read-empty', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]], {'read_empty_at': 1}),
        ('append-after-read', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]],
         {'append_after_first_read': [rel('f'), b'!'.hex()]}),
        ('replace-before-open', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]],
         {'replace_before_open': [rel('g'), rel('f')]}),
        ('read-io', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]], {'read_error': [1, 'EIO']}),
        ('names-io', lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]],
         {'names_error': [1, 1, 'EIO']}),
        ('cwd-failure', lambda r: [['read', b'f'.hex(), 100, 'management_copy', False]], {'cwd_error': 'EACCES'}),
    ]
    if builder.scope == 'mini':
        plain = plain[:1]
    for tag, ops, faults in plain:
        scenarios.add('r8-injection', tag, tree, ops(next_root(scenarios)), faults=faults)
    if builder.scope == 'mini':
        return
    # 開く I/O の異常: ファイルを開く(最後の open)に当てる。
    root = next_root(scenarios)
    item = {'root': root.hex(), 'tree': tree, 'ops': [['read', (root + b'/f').hex(), 100, 'management_copy', False]],
            'cond': ['normal'], 'cwd': None, 'faults': None}
    opens = run_scenario(item, keep_labels=True)['opens']
    scenarios.add('r8-injection', 'open-io', tree, item['ops'], faults={'open_error': [opens, 'EIO']})
    closes = [
        ('close-directory-step', 'directory-step', lambda r: [['directory', (r + b'/d').hex()]]),
        ('close-directory-child', 'directory-except',
         lambda r: [['directory', (r + b'/d').hex()], ['m-rename', rel('d'), rel('old')], ['m-mkdir', rel('d')],
                    ['directory', (r + b'/d').hex()]]),
        ('close-directory-other', 'directory-except', lambda r: [['directory', (r + b'/none/x').hex()]]),
        ('close-lstat', 'lstat-finally', lambda r: [['lstat', (r + b'/f').hex(), False]]),
        ('close-read-file', 'read-file', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]]),
        ('close-read-parent', 'read-parent', lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False]]),
        ('close-names', 'names-finally', lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]]),
        ('close-verify-dirs', 'verify-dirs', lambda r: [['directory', (r + b'/d').hex()], ['verify']]),
        ('close-verify-sets', 'verify-sets', lambda r: [['names', (r + b'/d').hex(), 'management_entries', True],
                                                         ['verify']]),
    ]
    for tag, label, ops in closes:
        root = next_root(scenarios)
        item = {'root': root.hex(), 'tree': tree, 'ops': ops(root), 'cond': ['normal'], 'cwd': None, 'faults': None}
        index = close_index(item, label)
        scenarios.add('r8-injection', tag, tree, item['ops'], faults={'close_error': [index, 'EIO']})


def add_r9(scenarios):
    builder = scenarios.builder
    plans = [
        ('read', [file_entry('f', {'size': 65537, 'seed': 9})],
         lambda r: [['read', (r + b'/f').hex(), 65537, 'management_copy', False]], 'management_copy'),
        ('names', names_tree(2000), lambda r: [['names', (r + b'/d').hex(), 'management_entries', True]],
         'management_entries'),
        ('verify', [file_entry('f', b'abc')],
         lambda r: [['read', (r + b'/f').hex(), 100, 'management_copy', False], ['m-append', rel('f'), b'!'.hex()],
                    ['verify']], 'management_copy'),
    ]
    if builder.scope == 'mini':
        plans = plans[:1]
    for tag, tree, ops, kind in plans:
        root = next_root(scenarios)
        item = {'root': root.hex(), 'tree': tree, 'ops': ops(root), 'cond': ['normal'], 'cwd': None, 'faults': None}
        normal = run_scenario(item)
        log = normal['budget'].split(';') if normal['budget'] else []
        conds = conditions_from(log, kind)
        scenarios.add('r9-budget', tag, tree, item['ops'], conds=conds)
        if tag == 'read':
            builder.bias['r'] = builder.groups['r9-budget'][-1]['id']
            builder.bias['kind'] = kind


def add_r10(scenarios):
    builder = scenarios.builder
    plans = [('relative', 'plain'), ('deep', 'deep'), ('deleted', 'deleted')]
    if builder.scope == 'mini':
        plans = plans[:1]
    for tag, mode in plans:
        root = next_root(scenarios)
        if mode == 'deleted':
            ops = [['read', b'f'.hex(), 100, 'management_copy', False]]
        else:
            ops = [['read', b'f'.hex(), 100, 'management_copy', False], ['lstat', b'f'.hex(), False],
                   ['names', b'.'.hex(), 'management_entries', True], ['verify']]
        try:
            scenarios.add('r10-cwd', tag, [], ops, child=mode)
        except ChildCreateError as error:
            if tag != 'deep':
                raise CheckError(str(error))
            builder.missing_input('r10-cwd/deep', str(error))


# ---------------------------------------------------------------- 子のプロセス(R7 の種・R10 の作業場所)

class ChildCreateError(Exception):
    """子が筋書きの作業場所を作れなかった。"""


DEEP_LEVEL = b'd' * 200
DEEP_COUNT = 25


def run_child(item, seed=None):
    """旧版を子のプロセスで動かす。R7 は -I も -E も付けず、環境を辞書で渡して PYTHONHASHSEED を決める。"""
    work = Path(os.fsdecode(bytes.fromhex(item['root']))).parent
    spec_path = work / ('child-%d-%s.json' % (os.getpid(), seed))
    out_path = work / ('child-%d-%s.out' % (os.getpid(), seed))
    spec_path.write_text(json.dumps(item))
    try:
        env = {'PATH': os.environ.get('PATH', os.defpath), NO_SELFTEST_ENV: '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        if seed is not None:
            env['PYTHONHASHSEED'] = str(seed)
        for name in ('HOME', 'LANG', 'LC_ALL', 'TMPDIR'):
            if name in os.environ:
                env[name] = os.environ[name]
        result = run_command([sys.executable, '-B', str(Path(__file__).resolve()), '--legacy-child',
                              str(spec_path), str(out_path)], cwd=str(work), env=env, timeout=CHILD_TIMEOUT)
        if result.timed_out:
            raise CheckError('旧版の子が上限時間 %d 秒に当たった' % CHILD_TIMEOUT)
        if result.returncode == 3:
            raise ChildCreateError(result.stderr.decode('utf-8', 'replace').strip())
        if result.returncode != 0:
            raise CheckError('旧版の子が失敗した(%d): %s' % (result.returncode,
                                                         result.stderr.decode('utf-8', 'replace')[-2000:]))
        return json.loads(out_path.read_text())
    finally:
        for path in (spec_path, out_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def child_main(spec_path, out_path):
    item = json.loads(Path(spec_path).read_text())
    mode = item.get('child')
    if mode in (None, 'r7'):
        result = run_scenario(item)
    else:
        result = run_cwd_scenario(item, mode)
    Path(out_path).write_text(json.dumps(result))
    return 0


def run_cwd_scenario(item, mode):
    """R10: 子の中で、作業場所を 1 段ずつ相対パスで移ってから、相対パスの筋書きを流す。"""
    rt = legacy()
    root = bytes.fromhex(item['root'])
    remove_tree(root)
    os.mkdir(root, 0o755)
    original = os.open(b'.', os.O_RDONLY | os.O_DIRECTORY)
    chain = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        chain.append(fd)
        if mode == 'deep':
            for _ in range(DEEP_COUNT):
                try:
                    os.mkdir(DEEP_LEVEL, 0o755, dir_fd=chain[-1])
                except OSError as error:
                    print('深い作業場所を作れない: %s' % error, file=sys.stderr)
                    raise SystemExit(3)
                chain.append(os.open(DEEP_LEVEL, os.O_RDONLY | os.O_DIRECTORY, dir_fd=chain[-1]))
        if mode == 'deleted':
            os.mkdir(b'gone', 0o755, dir_fd=chain[-1])
        else:
            handle = os.open(b'f', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=chain[-1])
            os.write(handle, b'abc')
            os.close(handle)
        os.chdir(root)
        if mode == 'deep':
            for _ in range(DEEP_COUNT):
                os.chdir(DEEP_LEVEL)
        if mode == 'deleted':
            os.chdir(b'gone')
            os.rmdir(os.path.join(root, b'gone'))
        budget = make_budget(item['cond'])
        reader = rt.Reader(budget)
        faults = LegacyFaults(None, root, None)
        results = []
        for op in item['ops']:
            check_missing_count(item, op, reader)
            result = legacy_op(reader, op, root, faults)
            results.append(result)
            if result[0] != 'ok':
                break
        return {'ops': results, 'records': records(reader), 'budget': log_text(budget)}
    finally:
        os.fchdir(original)
        os.close(original)
        if mode != 'deleted':
            with contextlib.suppress(FileNotFoundError):
                os.unlink(b'f', dir_fd=chain[-1])
        while len(chain) > 1:
            fd = chain.pop()
            os.close(fd)
            os.rmdir(DEEP_LEVEL, dir_fd=chain[-1])
        for fd in chain:
            os.close(fd)
        remove_tree(root)


# ---------------------------------------------------------------- 生成

def generate(destination, scope='full', j3_call=direct):
    started = time.monotonic()
    check_prerequisites()
    destination = Path(os.path.realpath(destination))
    if not destination.is_dir() or any(destination.iterdir()):
        raise CheckError('--generate の出力先は空のフォルダでなければならない: %s' % destination)
    builder = Builder(destination, scope)
    add_decoding(builder)
    add_depths(builder, j3_call)
    add_encoding(builder)
    add_base64(builder)
    add_keys(builder)
    add_metadata(builder)
    scenarios = Scenarios(builder)
    add_r1(scenarios)
    add_r2(scenarios)
    add_r3(scenarios)
    add_r4(scenarios)
    add_r5(scenarios)
    add_r6(scenarios)
    add_r7(scenarios)
    add_r8(scenarios)
    add_r9(scenarios)
    add_r10(scenarios)
    cases = {'scope': scope, 'groups': builder.groups, 'bias': builder.bias, 'platform': sys.platform}
    cases_text = json.dumps(cases, separators=(',', ':'))
    (destination / 'cases.json').write_text(cases_text)
    legacy_data = {
        'scope': scope, 'cases_sha256': hashlib.sha256(cases_text.encode()).hexdigest(),
        'results': builder.results, 'alternatives': builder.alternatives, 'not_created': builder.not_created,
        'tags': {group: sorted({item['tag'] for item in items}) for group, items in builder.groups.items()},
        'named': {group: len(items) for group, items in builder.groups.items()},
        'groups': {group: [item['id'] for item in items] for group, items in builder.groups.items()},
        'bias': builder.bias, 'generation_seconds': time.monotonic() - started,
        'python': sys.version.split()[0],
    }
    (destination / 'legacy.json').write_text(json.dumps(legacy_data, separators=(',', ':')))
    if scope == 'full':
        for group in GROUPS:
            print('R4D-2 生成: %s %d 件' % (group, len(builder.groups[group])), file=sys.stderr)
        print('R4D-2 生成: 旧版の実行 %.1f 秒・Python %s' % (legacy_data['generation_seconds'], legacy_data['python']),
              file=sys.stderr)
    return destination / 'legacy.json'


# ---------------------------------------------------------------- 比較

def minimum(group, data):
    """件数の下限(Task 2.2)。作れなかった入力の分だけ減らす。"""
    missing = sum(1 for entry in data['not_created'] if entry['key'].startswith(group + '/'))
    if group in FIXED_MINIMUM:
        value = FIXED_MINIMUM[group]
    elif group == 'j1-decode-corpus':
        value = len(j1_corpus())
    elif group == 'j7-keys':
        value = len(j7_named())
    elif group == 'j8-metadata':
        value = len(J8_FILES)
    elif group == 'j4-encode':
        value = J4_VALUES + len(j4_named())
    elif group == 'j5-response':
        # 比較器が作った入力(J4 の符号化できる値〈2 の累乗を除く〉・J5 の 2 の累乗・結果の形・無限大の位置)× 9 条件。
        encodable = sum(1 for item_id in data['groups']['j4-encode'] if data['results'][item_id][0][0] == 'ok'
                        and not item_id.endswith('-float-power-of-two'))
        value = (encodable + 2 * len(J5_POWERS) + len(result_shapes()) + 4) * 9
    else:  # j6-base64
        value = 2 * J6_RANDOM + len(j6_named()) + len(B64_LENGTHS)
    return value - missing


def summarize(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 400 else text[:400] + '...(%d 文字)' % len(text)


def compare_results(data, rust, only=None):
    """(不一致の説明の一覧, 比べた件数, 組ごとの件数) を返す。only があれば、その id だけを比べる。"""
    mismatches = []
    counts = {group: 0 for group in GROUPS}
    got = rust.get('results', {})
    for group in GROUPS:
        for item_id in data['groups'][group]:
            if only is not None and item_id not in only:
                continue
            counts[group] += 1
            expected = data['results'][item_id]
            actual = got.get(item_id)
            if item_id in data['alternatives']:
                if actual not in data['alternatives'][item_id]:
                    mismatches.append('%s: 旧版の 2 通りのどちらとも違う: Rust %s' % (item_id, summarize(actual)))
            elif actual != expected:
                mismatches.append('%s: 旧版 %s / Rust %s' % (item_id, summarize(expected), summarize(actual)))
    known = {item_id for ids in data['groups'].values() for item_id in ids}
    stray = sorted(set(got) - known)
    if stray:
        mismatches.append('一覧に無い入力の Rust の結果がある: %s' % stray[:3])
    return mismatches, sum(counts.values()), counts


def check_counts(data, counts):
    problems = []
    mini = data['scope'] != 'full'
    if mini and not os.environ.get(ALLOW_MINI_ENV):
        problems.append('一覧の範囲が full でない(%s)' % data['scope'])
    for group in GROUPS:
        need = 1 if mini else minimum(group, data)
        if counts[group] < need:
            problems.append('件数が下限に足りない: %s=%d(下限 %d)' % (group, counts[group], need))
        if not mini:
            absent = [tag for tag in REQUIRED_TAGS[group] if tag not in data['tags'][group]
                      and '%s/%s' % (group, tag) not in {e['key'] for e in data['not_created']}]
            if absent:
                problems.append('項目の入力が無い: %s %s' % (group, absent))
    return problems


def compare_main(legacy_path, rust_path, expect_mismatch):
    started = time.monotonic()
    check_prerequisites()
    data = json.loads(Path(legacy_path).read_text())
    rust = json.loads(Path(rust_path).read_text())
    if expect_mismatch:
        injected = set(rust.get('results', {}))
        if not injected:
            raise CheckError('差し込みを入れた Rust の結果が無い')
        mismatches, compared, _ = compare_results(data, rust, only=injected)
        if compared != len(injected):
            raise CheckError('差し込みを入れた結果に、一覧に無い入力がある')
        if len(mismatches) != compared:
            raise CheckError('差し込みを入れた Rust の結果のうち %d 件が旧版と一致した。比較が働いていない'
                             % (compared - len(mismatches)))
        print('R4D-2 比較(差し込み): 不一致 %d 件・期待どおり失敗' % len(mismatches), file=sys.stderr)
        return 0
    if os.environ.get(NO_SELFTEST_ENV):
        print('R4D-2 比較器の自己検査: 飛ばした(%s が設定されている)' % NO_SELFTEST_ENV, file=sys.stderr)
    else:
        run_selftest()
    mismatches, compared, counts = compare_results(data, rust)
    problems = check_counts(data, counts)
    for group in GROUPS:
        print('R4D-2 件数: %s=%d' % (group, counts[group]), file=sys.stderr)
    for entry in data['not_created']:
        print('R4D-2 作れなかった入力: %s' % entry['line'], file=sys.stderr)
    print(resource_line(), file=sys.stderr)
    seconds = data['generation_seconds'] + (time.monotonic() - started)
    print('R4D-2 の比較: 入力 %d 件・不一致 %d 件・%.1f 秒' % (compared, len(mismatches), seconds), file=sys.stderr)
    for line in mismatches[:20]:
        print('不一致: ' + line, file=sys.stderr)
    for line in problems:
        print('失敗: ' + line, file=sys.stderr)
    return 0 if not mismatches and not problems else 1


def run_selftest():
    """比較器の自己検査。失敗なら CheckError(終了値 0 にしない)。"""
    import importlib.util
    import unittest
    path = Path(__file__).resolve().with_name('test_diagnostic_runtime.py')
    spec = importlib.util.spec_from_file_location('r4d2_checker_selftest', path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
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
    print('R4D-2 比較器の自己検査: %d 件合格' % result.testsRun, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--generate', metavar='DIR')
    parser.add_argument('--compare', nargs=2, metavar=('LEGACY', 'RUST_RESULTS'))
    parser.add_argument('--expect-mismatch', action='store_true')
    parser.add_argument('--scope', choices=('full', 'mini'), default='full', help=argparse.SUPPRESS)
    parser.add_argument('--legacy-child', nargs=2, metavar=('SPEC', 'OUT'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.legacy_child:
            return child_main(*args.legacy_child)
        if bool(args.generate) == bool(args.compare):
            raise CheckError('--generate と --compare のどちらか 1 つを指定する')
        if args.generate:
            generate(args.generate, args.scope)
            return 0
        return compare_main(args.compare[0], args.compare[1], args.expect_mismatch)
    except CheckError as error:
        print('失敗: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
