#!/usr/bin/env python3
"""R4D-3: 保護記録の形式の検査・参照の名前と REV と実名の検査・参照の解決の手順の、旧版(Python)と Rust の比較器。
製品の入口ではない。

使い方(Rust の検査 native/crates/cli/tests/diagnostic_state.rs が起動する):
  --generate <空の一時フォルダ>
      入力の組を作り、旧版の関数を差し替えの部品と同じ予算の条件で呼び、
      <一時フォルダ>/cases.json に入力の一覧と旧版の結果を書く。
  --compare <cases.json> <Rust の結果の JSON> [--expect-mismatch]
      全件の結果(成否・失敗の種類と理由と終了値・値・予算の確かめの回数・差し替えの部品の呼出し)が一致し、
      件数が下限以上で、予算を使う組の条件がそろったときだけ終了値 0。
      --expect-mismatch は、わざと差を入れた Rust の結果に不一致が 1 件以上あるときだけ終了値 0。

旧版の関数は、この比較器と同じ Python の中で直接呼ぶ。差し替えの部品(stub)は、旧版のファイルの読み取りと
子の起動の代わりだけをし、旧版の手順を書き直さない。
前提: Python 3.10 系・root 以外の利用者・`sys.get_int_max_str_digits()` が 4300。欠ければ失敗にする(skip しない)。
本物の take の組(T)は、Linux では bash 4.0 以上と git 2.45 以上も前提にする(欠ければ失敗)。
Mac では、bash・git の版と GNU の道具が足りなければ、T の 4 つを作らずに、その行を出す。
"""
import argparse
import base64
import json
import os
from pathlib import Path
import random
import re
import stat
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
LEGACY_DIR = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
GUARD = LEGACY_DIR / 'implement-guard.sh'
# scripts/ は末尾に足す。先頭に足すと、同じ名前のテスト(scripts/test_diagnostic_state.py)の読み込み先を変えてしまう。
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
from support_load import load_script  # noqa: E402
from support_native import run_command  # noqa: E402

NO_SELFTEST_ENV = 'R4D3_NO_SELFTEST'
ALLOW_MINI_ENV = 'R4D3_ALLOW_MINI'
INT_MAX_STR_DIGITS = 4300
MIN_GIT = (2, 45)
MIN_BASH = (4, 0)
COMMAND_TIMEOUT = 60
TAKE_TIMEOUT = 120
SEED = 485
# 差し替えの部品が名乗る git の場所。旧版は [rt.git, *SAFE_GIT, ...] の形で子へ渡す。
FAKE_GIT = '/r4d3-stub/git'

GROUPS = ('F', 'L', 'M', 'C', 'O', 'A', 'P', 'V', 'K', 'R', 'H', 'Q', 'T')
# 件数の下限(入力の数。1 つの入力を予算の条件の数だけ流しても 1 件と数える)。
MINIMUM = {'F': 400, 'L': 80, 'M': 300, 'C': 200, 'O': 120, 'A': 20, 'P': 200, 'V': 100, 'K': 120,
           'R': 200, 'H': 60, 'Q': 80, 'T': 4}
# 全部の N 回目の期限と停止の印を流す組。
BUDGET_GROUPS = ('M', 'K', 'R')
# 予算の確かめの回数を比べない関数(旧版と Rust で予算を使う範囲が違う。旧版の回数は別に確かめる)。
UNCOUNTED_FUNCTIONS = ('parse_records', 'parse_origins')
RESULT_KINDS = ('ok', 'failure', 'value-error', 'unicode-error', 'os-error')
TAKE_REPOS = ('files', 'sha256', 'reftable', 'linked-worktree')
# 名前付きの入力(各組の境目)。一覧から項目が消えたら件数の不足と同じく失敗にする。
REQUIRED_TAGS = {
    'F': ('escape-every-byte', 'octal-every-value', 'octal-close-inside', 'backslash-before-close',
          'inner-quote', 'random'),
    'L': ('digits-4300', 'digits-4301', 'zeros-4300', 'zeros-4301', 'minus-4300', 'minus-4301', 'minus-zero',
          'plus-one', 'full-width'),
    'M': ('take-form', 'two-errors-line', 'two-errors-lines', 'no-final-lf', 'empty'),
    'C': ('valid-nested', 'valid-siblings', 'valid-non-immediate-parent', 'valid-reftable-sha256',
          'digits-4301', 'cwd-and-contexts', 'unterminated-and-old-name', 'cwd-toplevel', 'context-id',
          'row-order-cfield-format', 'row-order-integers', 'row-order-held-size', 'row-order-size-kind',
          'row-order-binding-integers', 'row-order-cfield-integers', 'row-order-cfield-size'),
    'O': ('valid', 'duplicate-after-quote', 'distinct-names', 'no-owner', 'admin-twice', 'admin-missing', 'old-name',
          'empty'),
    'A': ('chain-40', 'branches'),
    'P': ('literal-4096', 'literal-4097', 'under-dot', 'revision-1024', 'revision-1025', 'hex-39-41',
          'hex-63-65', 'upper-hex', 'surrogate', 'not-text'),
    'V': ('length-1024', 'length-1025', 'forbidden-byte', 'parts'),
    'K': ('packed-comment-peeled', 'packed-empty-line', 'packed-cr-crlf', 'packed-spaces', 'packed-width',
          'packed-upper', 'packed-duplicate', 'loose-ref', 'loose-lf-count', 'loose-4097', 'loose-width'),
    'R': ('private-names', 'same-dir', 'other-dir', 'head', 'pseudo-ref', 'dead-end', 'cycle', 'reaches-resolved',
          'order-differs', 'no-ids', 'invalid-name', 'invalid-target', 'cat-file-rc', 'cat-file-rows',
          'cat-file-type', 'cat-file-spacing', 'reftable', 'random'),
    'H': ('ref', 'object-name', 'invalid-target', 'width', 'reftable-missing', 'reftable-direct', 'refs-failing',
          'refs-calls'),
    'Q': ('head', 'unborn', 'refs-found', 'refs-missing', 'hex', 'rev-parse-rc', 'output-spacing', 'output-ascii',
          'output-width', 'refs-surrogate-0', 'refs-surrogate-1', 'refs-surrogate-failing', 'rev-parse-dc80',
          'rev-parse-d800'),
}


class CheckError(Exception):
    """比較器の失敗。メッセージを標準エラーへ出して終了値 1 にする。"""


# ---------------------------------------------------------------- 前提の検査

def int_max_str_digits():
    getter = getattr(sys, 'get_int_max_str_digits', None)
    return getter() if callable(getter) else None


def check_prerequisites():
    """Python 3.10 系・root 以外・桁の上限 4300。欠ければ CheckError(skip にしない)。"""
    problems = []
    if sys.version_info[:2] != (3, 10):
        problems.append('Python 3.10 系が要る(今は %d.%d)' % sys.version_info[:2])
    if os.geteuid() == 0:
        problems.append('root 以外の利用者で動かす必要がある')
    digits = int_max_str_digits()
    if digits != INT_MAX_STR_DIGITS:
        problems.append('整数の桁の上限(sys.get_int_max_str_digits())が %d でない(今は %s)' % (INT_MAX_STR_DIGITS, digits))
    if problems:
        raise CheckError('R4D-3 前提が満たされない: ' + ' / '.join(problems))


def tool_version(argv, pattern):
    """道具の版((主, 副) と 1 行目)。読めなければ (None, 説明)。"""
    try:
        result = run_command(argv, cwd=os.getcwd(), env=dict(os.environ, LC_ALL='C'), timeout=COMMAND_TIMEOUT)
    except OSError as error:
        return None, '起動できない(%s)' % error.strerror
    text = result.stdout.decode('utf-8', 'replace')
    match = re.search(pattern, text)
    first = text.splitlines()[0] if text else ''
    if result.timed_out or result.returncode != 0 or not match:
        return None, first or '版を読めない'
    return tuple(int(part) for part in match.groups()), first


def take_shortages():
    """T の組を作るのに足りない物の一覧。Linux で足りなければ CheckError、Mac では一覧を返す。"""
    missing = []
    bash, bash_text = tool_version(['bash', '--version'], r'version (\d+)\.(\d+)')
    if bash is None or bash < MIN_BASH:
        missing.append('bash %d.%d 以上(今は %s)' % (MIN_BASH + (bash_text,)))
    git, git_text = tool_version(['git', '--version'], r'git version (\d+)\.(\d+)')
    if git is None or git < MIN_GIT:
        missing.append('git %d.%d 以上(今は %s)' % (MIN_GIT + (git_text,)))
    if sys.platform != 'darwin':
        if missing:
            raise CheckError('R4D-3 前提が満たされない(本物の take の組): ' + ' / '.join(missing))
        return []
    # 旧 implement-guard.sh が Mac で要る GNU の道具(runtime-requirements.md の coreutils・findutils・grep)。
    for name in ('stat', 'find', 'grep'):
        try:
            result = run_command([name, '--version'], cwd=os.getcwd(), env=dict(os.environ, LC_ALL='C'),
                                 timeout=COMMAND_TIMEOUT)
            first = result.stdout.decode('utf-8', 'replace').split('\n', 1)[0]
            ok = result.returncode == 0 and not result.timed_out and '(GNU' in first
        except OSError:
            ok = False
        if not ok:
            missing.append('GNU の %s' % name)
    return missing


# ---------------------------------------------------------------- 旧版

_modules = {}


def legacy():
    """旧版の部品(diagnostic-git.py:24-36 と同じつなぎ方)。固有の名前で読み込み、ほかのテストと混ぜない。"""
    if not _modules:
        runtime = load_script(LEGACY_DIR / 'diagnostic-runtime.py', 'r4d3_legacy_runtime')
        state = load_script(LEGACY_DIR / 'diagnostic-state.py', 'r4d3_legacy_state')
        paths = load_script(LEGACY_DIR / 'diagnostic-paths.py', 'r4d3_legacy_paths')
        private = load_script(LEGACY_DIR / 'diagnostic-private.py', 'r4d3_legacy_private')
        git = load_script(LEGACY_DIR / 'implementation-git.py', 'r4d3_legacy_implementation_git')
        state.configure(runtime)
        paths.configure(runtime)
        private.configure(runtime, git.SAFE_GIT)
        private.Private.cfield = staticmethod(state.cfield)
        _modules.update(R=runtime, S=state, X=paths, D=private, G=git)
    return _modules


def unhex(text):
    return bytes.fromhex(text)


def hexo(raw):
    return None if raw is None else raw.hex()


def b64d(text):
    return None if text is None else base64.b64decode(text)


class Clock:
    """時計を呼んだ回数を数える。const はいつも 0、seq は呼ぶたびに 0,1,2,...(N 回目の確かめで期限)。"""

    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.mode == 'const' else float(self.calls - 1)


def make_budget(cond):
    """条件 ['normal'] ['stop'] ['n', N] から旧版の予算と時計を作る。"""
    R = legacy()['R']
    if cond[0] in ('normal', 'stop'):
        clock = Clock('const')
        budget = R.Budget(clock=clock, seconds=300.0)
        budget.stopped = cond[0] == 'stop'
        return budget, clock
    if cond[0] == 'n':
        clock = Clock('seq')
        return R.Budget(clock=clock, seconds=float(cond[1])), clock
    raise CheckError('未知の条件: %r' % (cond,))


def outcome(call):
    """旧版の呼び出しの結果を、種類ごとに JSON にできる形にする。UnicodeError は ValueError の子なので先に見る。"""
    R = legacy()['R']
    try:
        value = call()
    except CheckError:
        raise
    except R.Failure as error:
        return ['failure', error.reason, error.code]
    except UnicodeError:
        return ['unicode-error']
    except ValueError:
        return ['value-error']
    except OSError as error:
        return ['os-error', error.errno]
    except Exception as error:  # 想定外の例外は、比較器の誤りとして生成を止める
        return ['other', type(error).__name__, str(error)[:200]]
    return ['ok', value]


# ---------------------------------------------------------------- 差し替えの部品

class StubRuntime:
    """旧版の rt.process の代わり。文字列の引数を UTF-8 と surrogateescape で bytes にし(変換できなければ同じ例外を出して
    記録しない)、引数の頭が [rt.git, *SAFE_GIT] であることを確かめてから、頭を除いた引数・標準入力・出力の種類・上限を記録し、
    決めた終了値と出力を返す。予算の check は呼ばない(本物の process の最初の check は R4D-3b で比べる)。"""

    def __init__(self, recorder, responses):
        self.git = FAKE_GIT
        self.recorder = recorder
        self.responses = list(responses)

    def process(self, argv, input_bytes=b'', env=None, cwd=None, timeout=30, output_kind='git_output',
                output_limit=None):
        raw = [arg if isinstance(arg, bytes) else arg.encode('utf-8', 'surrogateescape') for arg in argv]
        head = [arg.encode('utf-8') for arg in (self.git, *legacy()['G'].SAFE_GIT)]
        if raw[:len(head)] != head:
            raise CheckError('旧版が子へ渡す引数の頭が [git, *SAFE_GIT] でない: %r' % (raw[:3],))
        self.recorder.append(['process', [arg.hex() for arg in raw[len(head):]], input_bytes.hex(), output_kind,
                              output_limit])
        response = self.responses.pop(0) if self.responses else ['rc', 0, '']
        if response[0] == 'os':
            raise OSError(response[1], 'R4D-3 の差し替えの部品')
        return response[1], unhex(response[2])


def raise_spec(spec):
    """差し替えの部品が出す失敗(['failure', 理由, 終了値] か ['os', errno])。"""
    if spec[0] == 'failure':
        legacy()['R'].fail(spec[1], spec[2])
    if spec[0] == 'os':
        raise OSError(spec[1], 'R4D-3 の差し替えの部品')
    raise CheckError('未知の失敗の指定: %r' % (spec,))


def stub_records(items):
    R = legacy()['R']
    return [{'name_b64': R.b64(unhex(name)), 'oid': oid,
             'symbolic_target_b64': None if target is None else R.b64(unhex(target)), 'resolution': resolution}
            for name, oid, target, resolution in items]


def stub_head_value(value):
    return None if value is None else (value[0], None if value[1] is None else unhex(value[1]))


class StubReader:
    """旧版の Reader の代わり。保護記録のフォルダを 0700 のフォルダと答え、決めた bytes を返す。"""

    def __init__(self, state, files):
        self.state = state
        self.files = files

    def lstat(self, path, missing=True):
        if path != self.state:
            raise CheckError('差し替えの reader に保護記録の外の lstat が来た: %r' % path)
        return os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, os.geteuid(), os.getegid(), 0, 0, 0, 0))

    def owner(self, path, info=None, code=22):
        return None

    def read(self, path, limit=None, kind=None, missing=False):
        prefix = self.state + b'/'
        if not path.startswith(prefix) or path[len(prefix):] not in self.files:
            raise CheckError('差し替えの reader に知らない読み取りが来た: %r' % path)
        return self.files[path[len(prefix):]]

    def names(self, path, kind=None, record=True):
        if path != self.state + b'/snapshot':
            raise CheckError('差し替えの reader に知らない一覧が来た: %r' % path)
        return sorted(name[len(b'snapshot/'):] for name in self.files if name.startswith(b'snapshot/'))


_record_state = []


def record_state_class():
    """State.__init__ の :47-101 を旧版のまま動かす型。後の 2 つの呼出し(:103-104。R4D-3b)だけを置き換える。"""
    if not _record_state:
        S = legacy()['S']

        class RecordState(S.State):
            def origins(self, raw):
                self.stub_calls.append('origins')

            def management_binding(self):
                self.stub_calls.append('management_binding')

        _record_state.append(RecordState)
    return _record_state[0]


# C の組で使う、記録の読み取りまで届く正しい manifest と origins(T の組は本物の bytes を使う)。
RECORD_MANIFEST = b'T\t664\t' + b'5' * 64 + b'\t/r4d3-records/task/t.md\n'
RECORD_ORIGINS = b'config-origins-v3\nplan-admin\t/r4d3-records/.git\t/r4d3-records/.git\n'


# ---------------------------------------------------------------- 旧版の呼び方(関数ごと)

def legacy_cfield(data, budget, recorder, extra):
    return legacy()['S'].cfield(unhex(data['raw'])).hex()


def legacy_lines(data, budget, recorder, extra):
    return [row.hex() for row in legacy()['S'].lines(unhex(data['raw']))]


def legacy_integer(data, budget, recorder, extra):
    return legacy()['S'].integer(unhex(data['raw']), data['min'], data['max'])


def legacy_manifest(data, budget, recorder, extra):
    legacy()['S'].State.manifest(types.SimpleNamespace(budget=budget), unhex(data['raw']))
    return None


def legacy_records(data, budget, recorder, extra):
    R = legacy()['R']
    files = dict(extra['files']) if extra else {
        b'manifest.tsv': RECORD_MANIFEST, b'snapshot/config-origins.txt': RECORD_ORIGINS, b'snapshot/1-diff.bin': b''}
    files[b'snapshot/4-meta.txt'] = unhex(data['meta'])
    files[b'snapshot/config-contexts.txt'] = unhex(data['contexts'])
    state = unhex(data['state'])
    names = sorted(name[len(b'snapshot/'):] for name in files if name.startswith(b'snapshot/'))
    aggregate = b''.join(R.digest(files[b'snapshot/' + name]).encode() + b'  ' + name + b'\n'
                         for name in names if not name.startswith(b'.'))
    args = types.SimpleNamespace(state=os.fsdecode(state), cwd=os.fsdecode(unhex(data['cwd'])),
                                 manifest_sha256=R.digest(files[b'manifest.tsv']), snapshot_sha256=R.digest(aggregate),
                                 context_id=int(data['context_id']))
    runtime = types.SimpleNamespace(reader=StubReader(state, files), budget=budget)
    kind = record_state_class()
    obj = kind.__new__(kind)
    obj.stub_calls = []
    try:
        kind.__init__(obj, args, runtime)
    except BaseException:
        if obj.stub_calls:
            raise CheckError('旧版の State が、失敗の後に R4D-3b の範囲を呼んだ')
        raise
    if obj.stub_calls != ['origins', 'management_binding']:
        raise CheckError('旧版の State が、記録の読み取りの後の 2 つを呼ばなかった: %r' % obj.stub_calls)
    return {'cwd': hexo(obj.meta.get(b'cwd')), 'toplevel': hexo(obj.meta.get(b'toplevel')),
            'contexts': [[c['id'], c['parent'], c['depth'], c['active'], c['wt'].hex(), c['gd'].hex(), c['common'].hex(),
                          c['format'], c['backend'], c['held']] for c in obj.contexts]}


def legacy_origins(data, budget, recorder, extra):
    """State.origins を、戻りの時点の局所の変数 groups と admin を写し取りながら呼ぶ(旧版のコードは変えない)。"""
    S = legacy()['S']
    holder = types.SimpleNamespace(budget=budget, contexts=[])
    code = S.State.origins.__code__
    captured = {}

    def local(frame, event, arg):
        if event == 'return':
            captured['groups'] = frame.f_locals.get('groups')
            captured['admin'] = frame.f_locals.get('admin')
        return local

    def tracer(frame, event, arg):
        return local if frame.f_code is code else None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        S.State.origins(holder, unhex(data['raw']))
    finally:
        sys.settrace(previous)
    groups, admin = captured.get('groups'), captured.get('admin')
    if not isinstance(groups, dict) or not isinstance(admin, tuple):
        raise CheckError('旧版の origins の groups と admin を写し取れなかった')
    return {'groups': [[name.hex(), [[field.hex() for field in row] for row in rows]] for name, rows in groups.items()],
            'admin': [admin[0].hex(), admin[1].hex()]}


def legacy_ancestors(data, budget, recorder, extra):
    holder = types.SimpleNamespace(contexts=[{'parent': parent} for parent in data['parents']])
    return legacy()['S'].State.ancestors(holder, data['n'])


def legacy_literal(data, budget, recorder, extra):
    raw = unhex(data['raw'])
    if legacy()['X'].literal(raw, data['under']) is not raw:
        raise CheckError('旧版の literal が値を変えた')
    return None


NOT_TEXT = {'none': None, 'int': 0, 'bytes': b'HEAD', 'list': ['HEAD'], 'bool': True}


def revision_value(value):
    if 'text' in value:
        return ''.join(map(chr, value['text']))
    return NOT_TEXT[value['not_text']]


def legacy_revision(data, budget, recorder, extra):
    value = revision_value(data['value'])
    if legacy()['X'].revision(value) is not value:
        raise CheckError('旧版の revision が値を変えた')
    return None


def legacy_valid_ref(data, budget, recorder, extra):
    return legacy()['D'].valid_ref(unhex(data['name']))


def legacy_file_refs(data, budget, recorder, extra):
    holder = types.SimpleNamespace(budget=budget, width=data['width'])
    values = legacy()['D'].Private.file_refs(holder, unhex(data['dir']))
    return [[name.hex(), oid, hexo(target)] for name, (oid, target) in values.items()]


class StubReftableError(Exception):
    pass


def ref_dict(items):
    return {unhex(name): (oid, None if target is None else unhex(target)) for name, oid, target in items}


def refs_out(records, head_value):
    return {'records': [[b64d(r['name_b64']).hex(), r['oid'], hexo(b64d(r['symbolic_target_b64'])), r['resolution']]
                        for r in records],
            'head': None if head_value is None else [head_value[0], hexo(head_value[1])]}


def legacy_refs(data, budget, recorder, extra):
    host = types.SimpleNamespace(budget=budget, env={}, work=b'/r4d3-private/worktree',
                                 common=b'/r4d3-private/common', gd=b'/r4d3-private/gitdir')
    host.context = {'backend': data['backend'], 'format': 'sha1', 'gd': b'/r4d3-refs/gitdir',
                    'common': b'/r4d3-refs/gitdir' if data['same_dir'] else b'/r4d3-refs/common'}
    host.rt = StubRuntime(recorder, data['responses'])
    common, local = ref_dict(data['common']), ref_dict(data['local'])

    def values_for(path):
        if path in (host.common, host.common + b'/reftable'):
            return dict(common)
        if path in (host.gd, host.gd + b'/reftable'):
            return dict(local)
        raise CheckError('差し替えの参照の値に知らない場所が来た: %r' % path)
    if data['backend'] == 'files':
        host.file_refs = values_for
    else:
        host.reftable = types.SimpleNamespace(read_stack=lambda path, fmt, budget: values_for(path),
                                              ReftableError=StubReftableError)
    records = legacy()['D'].Private.refs(host)
    return refs_out(records, host.head_value)


def stub_refs_for(host, recorder, spec):
    def refs():
        recorder.append(['refs'])
        if spec.get('fail') is not None:
            raise_spec(spec['fail'])
        host.head_value = stub_head_value(spec.get('head'))
        return stub_records(spec['records'])
    return refs


def legacy_head(data, budget, recorder, extra):
    host = types.SimpleNamespace(context={'backend': data['backend']}, width=data['width'], budget=budget)
    host.refs = stub_refs_for(host, recorder, data['refs'])
    if data['backend'] == 'files':
        host.gd = unhex(data['dir'])
    head = legacy()['D'].Private.head(host)
    return [hexo(b64d(head['symbolic_target_b64'])), head['oid']]


def legacy_oid(data, budget, recorder, extra):
    D, R = legacy()['D'], legacy()['R']
    host = types.SimpleNamespace(width=data['width'], env={}, work=b'/r4d3-private/worktree')
    host.rt = StubRuntime(recorder, data['responses'])
    host.command = types.MethodType(D.Private.command, host)
    host.refs = stub_refs_for(host, recorder, data['refs'])
    head_spec = data['head']

    def head():
        recorder.append(['head'])
        if head_spec.get('fail') is not None:
            raise_spec(head_spec['fail'])
        target = head_spec.get('target')
        return {'symbolic_target_b64': None if target is None else R.b64(unhex(target)), 'oid': head_spec.get('oid')}
    host.head = head
    return D.Private.oid(host, ''.join(map(chr, data['value'])), data['unborn'])


def legacy_take(data, budget, recorder, extra):
    """本物の take の記録を、M・C・O の関数で動かす(それぞれ新しい通常の予算)。"""
    return {
        'manifest': run_legacy('check_manifest', {'raw': data['manifest']}, ['normal']),
        'records': run_legacy('parse_records', {key: data[key] for key in ('meta', 'contexts', 'cwd', 'context_id', 'state')},
                              ['normal'], extra),
        'origins': run_legacy('parse_origins', {'raw': data['origins']}, ['normal']),
    }


LEGACY = {
    'cfield': legacy_cfield, 'lines': legacy_lines, 'integer': legacy_integer, 'check_manifest': legacy_manifest,
    'parse_records': legacy_records, 'parse_origins': legacy_origins, 'ancestors': legacy_ancestors,
    'literal': legacy_literal, 'revision': legacy_revision, 'valid_ref': legacy_valid_ref,
    'file_refs': legacy_file_refs, 'resolve_refs': legacy_refs, 'head': legacy_head, 'oid': legacy_oid,
    'take': legacy_take,
}


def run_legacy(fn, data, cond, extra=None):
    """旧版を 1 つの条件で動かす。結果・予算の確かめの回数(時計の回数から作るときの 1 回を引く)・部品の呼出し。"""
    budget, clock = make_budget(cond)
    recorder = []
    result = outcome(lambda: LEGACY[fn](data, budget, recorder, extra))
    if result[0] not in RESULT_KINDS:
        raise CheckError('旧版が想定外の例外を出した(比較器の入力の誤り): %s %s %s' % (fn, cond, result))
    return {'result': result, 'checks': clock.calls - 1, 'calls': recorder}


# ---------------------------------------------------------------- 入力の組み立て

class Builder:
    def __init__(self, root):
        self.root = root
        self.rng = random.Random(SEED)
        self.cases = []
        self.tags = {group: [] for group in GROUPS}
        self.runs = 0
        self.not_created = []
        self.numbers = {}

    def add(self, group, fn, data, tag=None, extra=None):
        number = self.numbers.get(group, 0)
        self.numbers[group] = number + 1
        first = run_legacy(fn, data, ['normal'], extra)
        conds, outputs = [['normal']], [first]
        if group in BUDGET_GROUPS:
            for n in range(1, first['checks'] + 2):
                conds.append(['n', n])
                outputs.append(run_legacy(fn, data, ['n', n], extra))
            conds.append(['stop'])
            outputs.append(run_legacy(fn, data, ['stop'], extra))
        self.runs += len(conds)
        case = {'id': '%s-%04d' % (group, number), 'group': group, 'fn': fn, 'input': data, 'conds': conds,
                'legacy': outputs}
        if tag is not None:
            case['tag'] = tag
            if tag not in self.tags[group]:
                self.tags[group].append(tag)
        self.cases.append(case)
        return case

    def folder(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True)
        return path


def hexs(rng, width):
    return ''.join(rng.choice('0123456789abcdef') for _ in range(width))


def cquote(raw, force=False):
    """旧 implement-guard.sh の path_field と同じ C 形式の引用(TAB・LF・制御の文字・`"`・`\\` か先頭の `"` のとき)。"""
    special = any(c < 32 or c == 127 or c in b'"\\' for c in raw)
    if not (force or special or raw.startswith(b'"')):
        return raw
    out = bytearray(b'"')
    for c in raw:
        if c == 9:
            out += b'\\t'
        elif c == 10:
            out += b'\\n'
        elif c in (34, 92):
            out += b'\\' + bytes([c])
        elif c < 32 or c == 127:
            out += b'\\%03o' % c
        else:
            out.append(c)
    return bytes(out + b'"')


def add_f(builder):
    rng = builder.rng
    add = lambda raw, tag=None: builder.add('F', 'cfield', {'raw': raw.hex()}, tag)  # noqa: E731
    for raw in (b'', b'abc', b'a b', b'/x/y', b'a"b', b'\\n', b'\xff\xfe', b'a\x01b', b'\x7f', b'a\rb', b'x"'):
        add(raw, 'plain')
    for raw in (b'a\0b', b'\0', b'a\nb', b'\n', b'a\tb', b'\t', b'x\0\n\t'):
        add(raw, 'plain-forbidden')
    for raw in (b'"', b'""', b'"a', b'"a"', b'"""'):
        add(raw, 'short')
    for c in range(256):
        add(b'"\\' + bytes([c]) + b'"', 'escape-every-byte')
    for value in range(0o1000):
        add(b'"\\%03o"' % value, 'octal-every-value')
    for raw in (b'"\\1"', b'"\\12"', b'"\\1""', b'"\\12""', b'"a\\1"'):
        add(raw, 'octal-close-inside')
    for raw in (b'"\\"', b'"a\\"', b'"\\\\\\"'):
        add(raw, 'backslash-before-close')
    for raw in (b'"a"b"', b'""a"', b'"a""'):
        add(raw, 'inner-quote')
    for raw in (b'"\\8"', b'"\\18"', b'"\\0"', b'"\\00"', b'"\\001"', b'"\\400"', b'"\\377\\377"', b'"\\101bc"',
                b'"\0"', b'"a\nb"', b'"a\tb"', b'"\\303\\251"', b'"\\x41"', b'"\\u0041"'):
        add(raw, 'quoted')
    alphabet = b'"\\012345678abntvfr\0\n\t \xff'
    for _ in range(256):
        body = bytes(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        add(b'"' + body + b'"' if rng.random() < 0.6 else body, 'random')


def add_l(builder):
    for raw in (b'', b'\n', b'a', b'a\n', b'a\nb', b'a\nb\n', b'\n\n', b'a\n\n', b'\r\n', b'a\r', b'\0\n',
                b'a\tb\n', b'xyz\n', b'a\nb\nc\n'):
        builder.add('L', 'lines', {'raw': raw.hex()}, 'lines')
    integer = lambda raw, low, high, tag: builder.add('L', 'integer', {'raw': raw.hex(), 'min': low, 'max': high}, tag)  # noqa: E731,E501
    for low, high in ((0, 1999), (-1, 1999), (0, 40), (0, 1), (0, 16 * 1024 * 1024), (0, 2000)):
        for value in (low - 1, low, low + 1, high - 1, high, high + 1):
            integer(str(value).encode(), low, high, 'bounds')
    for raw, tag in ((b'-0', 'minus-zero'), (b'+1', 'plus-one'), (b' 1', 'space'), (b'1 ', 'space'), (b'', 'empty'),
                     (b'-', 'sign-only'), (b'--1', 'sign-twice'), ('١'.encode(), 'arabic-indic'),
                     ('１'.encode(), 'full-width'), (b'0x1', 'hex'), (b'1_0', 'underscore'), (b'1.0', 'dot'),
                     (b'00', 'zeros'), (b'007', 'zeros'), (b'-01', 'zeros'), (b'1\n', 'lf'), (b'\n1', 'lf'),
                     (b'99999999999999999999', 'large'), (b'-9223372036854775809', 'large'),
                     (b'9223372036854775808', 'large')):
        integer(raw, 0, 1999, tag)
        integer(raw, -1, 1999, tag)
    for raw, tag in ((b'1' + b'0' * 4299, 'digits-4300'), (b'1' + b'0' * 4300, 'digits-4301'),
                     (b'0' * 4299 + b'1', 'zeros-4300'), (b'0' * 4300 + b'1', 'zeros-4301'),
                     (b'0' * 4300, 'zeros-4300'), (b'0' * 4301, 'zeros-4301'),
                     (b'-' + b'0' * 4300, 'minus-4300'), (b'-' + b'0' * 4301, 'minus-4301'),
                     (b'-' + b'1' * 4300, 'minus-4300'), (b'-' + b'1' * 4301, 'minus-4301'),
                     (b'+' + b'0' * 4301, 'plus-4301')):
        integer(raw, 0, 1999, tag)
        integer(raw, -1, 16 * 1024 * 1024, tag)


# ---- M: manifest

NAME_PARTS = (b'src', b'a b', b'tab\tname', b'new\nline', b'quote"x', b'back\\slash', '日本'.encode(), b'\x01ctl',
              b'.hidden', b'x', b'lib', b'\xffraw', b'link')


def take_manifest(rng, size):
    """本物の take の形の manifest の行(T が先頭、続きは f・l・o・u)。"""
    rows = [b'\t'.join((b'T', rng.choice((b'644', b'664', b'600')), hexs(rng, 64).encode(),
                        cquote(b'/r4d3-manifest/task/t' + rng.choice(NAME_PARTS) + b'.md')))]
    names = sorted({b'/'.join(rng.choice(NAME_PARTS) for _ in range(rng.randrange(1, 4))) + b'%d' % i
                    for i in range(size - 1)})
    kinds = (b'f', b'l', b'o', b'u')
    for index, name in enumerate(names):
        kind = kinds[index % 4] if index < 4 else rng.choice(kinds)
        path = cquote(name)
        if kind == b'f':
            rows.append(b'\t'.join((kind, rng.choice((b'644', b'664', b'755', b'0644')), hexs(rng, 64).encode(), path)))
        elif kind == b'l':
            rows.append(b'\t'.join((kind, b'777', cquote(rng.choice(NAME_PARTS)), path)))
        elif kind == b'o':
            rows.append(b'\t'.join((kind, rng.choice((b'755', b'-', b'644')), b'-', path)))
        else:
            rows.append(b'\t'.join((kind, b'-', b'-', path)))
    return rows


def joined(rows):
    return b''.join(row + b'\n' for row in rows)


def set_field(row, index, value):
    fields = row.split(b'\t')
    fields[index] = value
    return b'\t'.join(fields)


def row_of_kind(rows, kinds):
    return [index for index, row in enumerate(rows) if index > 0 and row.split(b'\t')[0] in kinds]


def m_mutations(rng, rows):
    """(名前, 変えた行の列) を返す。行の種類が要る変え方は、その種類の行を選ぶ。"""
    out = []
    later = rng.randrange(1, len(rows)) if len(rows) > 1 else None
    pick = lambda kinds: rng.choice(row_of_kind(rows, kinds)) if row_of_kind(rows, kinds) else None  # noqa: E731

    def changed(index, value):
        copy = list(rows)
        copy[index] = value
        return copy
    out.append(('fields-3', changed(0, b'\t'.join(rows[0].split(b'\t')[:3]))))
    out.append(('fields-5', changed(0, rows[0] + b'\tx')))
    out.append(('first-kind', changed(0, set_field(rows[0], 0, rng.choice((b'f', b't', b'X', b''))))))
    out.append(('mode', changed(0, set_field(rows[0], 1, rng.choice((b'', b'6', b'64', b'06444', b'0648', b'rw-',
                                                                     b'--', b' 644', b'644 ', b'0x64'))))))
    out.append(('mode-good', changed(0, set_field(rows[0], 1, rng.choice((b'-', b'0755', b'7777', b'000'))))))
    out.append(('value-hex', changed(0, set_field(rows[0], 2, rng.choice((b'a' * 63, b'a' * 65, b'A' * 64, b'g' * 64,
                                                                          b''))))))
    out.append(('first-relative', changed(0, set_field(rows[0], 3, b'task/t.md'))))
    out.append(('path-empty', changed(0, set_field(rows[0], 3, rng.choice((b'', b'""'))))))
    out.append(('path-cfield', changed(0, set_field(rows[0], 3, rng.choice((b'"/x', b'"\\8"', b'/a\0b', b'"\\000"'))))))
    out.append(('two-errors-line', changed(0, set_field(set_field(rows[0], 1, b'x'), 3, b'"broken'))))
    if later is not None:
        out.append(('later-kind', changed(later, set_field(rows[later], 0, rng.choice((b'T', b'x', b'F', b''))))))
        out.append(('later-absolute', changed(later, set_field(rows[later], 3, b'/abs/path'))))
        out.append(('later-fields', changed(later, rows[later] + b'\t')))
        copy = list(rows)
        copy[later] = set_field(copy[later], 1, b'x')
        first_bad = rng.randrange(0, later) if later > 0 else 0
        copy[first_bad] = set_field(copy[first_bad], 3, b'"broken')
        out.append(('two-errors-lines', copy))
    for name, kinds, values in (('value-hex-f', (b'f',), (b'a' * 63, b'A' * 64, b'')),
                                ('value-link', (b'l',), (b'"x', b'"\\q"', b'a\0b', b'"ok\\tlink"')),
                                ('value-dash', (b'o', b'u'), (b'x', b'', b'--'))):
        index = pick(kinds)
        if index is not None:
            out.append((name, changed(index, set_field(rows[index], 2, rng.choice(values)))))
    blank = list(rows)
    blank.insert(rng.randrange(1, len(rows) + 1), b'')
    out.append(('blank-line', blank))
    return out


def add_m(builder):
    rng = builder.rng
    add = lambda raw, tag: builder.add('M', 'check_manifest', {'raw': raw.hex()}, tag)  # noqa: E731
    for size in (1, 2, 3, 5, 8, 13, 21, 34, 40):
        add(joined(take_manifest(rng, size)), 'take-form')
    for _ in range(31):
        add(joined(take_manifest(rng, rng.randrange(1, 41))), 'take-form')
    for _ in range(18):
        rows = take_manifest(rng, rng.randrange(5, 31))
        for name, changed in m_mutations(rng, rows):
            add(joined(changed), name)
        add(joined(rows)[:-1], 'no-final-lf')
    for raw, tag in ((b'', 'empty'), (b'\n', 'only-lf'), (b'\n\n', 'only-lf'), (b'T', 'no-final-lf')):
        add(raw, tag)


# ---- C: config-contexts.txt と 4-meta.txt

def ctx(n, parent, depth, wt, active=b'1', gd=None, common=None, fmt=b'sha1', backend=b'files', held=b'-', size=b'0',
        kind=b'none', f14=b'-', f15=b'-'):
    gd = wt + b'/.git' if gd is None else gd
    common = gd if common is None else common
    return [b'%d' % n, b'%d' % parent, b'%d' % depth, active, wt, gd, common, fmt, backend, held, size, kind, f14, f15]


def ctx_row(fields):
    return b'\t'.join([b'context'] + fields)


def contexts_raw(rows, header=b'context-plan-v1'):
    return joined([header] + rows)


def meta_raw(top, cwd, extra=()):
    return joined([b'toplevel\t' + cquote(top), b'cwd\t' + cquote(cwd), b'exclude\t.claude/reviews',
                   b'head\t' + b'3' * 40, b'hook\tf\t775\t' + b'4' * 64 + b'\tpre-commit.sample', *extra])


def c_bases(top, rng):
    """(名前, context の欄の列, root の行, 選ぶ番号) の正しい形。"""
    oid1, oid2 = hexs(rng, 40).encode(), hexs(rng, 64).encode()
    single = [ctx(0, -1, 0, top, held=oid1)]
    nested = [ctx(0, -1, 0, top, held=oid1), ctx(1, 0, 1, top + b'/sub', held=b'-'),
              ctx(2, 1, 2, top + b'/sub/inner', fmt=b'sha256', held=oid2, size=b'16777216', kind=b'blob')]
    siblings = [ctx(0, -1, 0, top), ctx(1, 0, 1, top + b'/a'), ctx(2, 0, 1, top + b'/b', active=b'0', gd=b'-',
                                                                     common=b'-'),
                ctx(3, 1, 2, top + b'/a/c', kind=b'file'), ctx(4, 2, 2, top + b'/b/d', kind=b'unmerged')]
    jump = [ctx(0, -1, 0, top), ctx(1, 0, 1, top + b'/a'), ctx(2, 1, 2, top + b'/a/b'), ctx(3, 0, 1, top + b'/c'),
            ctx(4, 2, 3, top + b'/a/b/d')]
    reftable = [ctx(0, -1, 0, top, fmt=b'sha256', backend=b'reftable', held=oid2,
                    gd=top + b'/.git/worktrees/w', common=top + b'/.git')]
    quoted = [ctx(0, -1, 0, top), ctx(1, 0, 1, cquote(top + b'/tab\there'), gd=cquote(top + b'/q"x/.git'),
                                      common=cquote(top + b'/back\\s', force=True))]
    deep = [ctx(0, -1, 0, top)] + [ctx(n, n - 1, n, top + b'/d' * n) for n in range(1, 41)]
    roots = [b'root\t0:0\t' + top + b'/.git/config']
    return [
        ('valid-single', single, roots, 0), ('valid-nested', nested, roots + [b'root\t2:1\t/etc/gitconfig'], 2),
        ('valid-siblings', siblings, roots + [b'root\t4:3\t' + cquote(top + b'/x\ty')], 3),
        ('valid-non-immediate-parent', jump, roots, 4), ('valid-reftable-sha256', reftable, roots, 0),
        ('valid-quoted', quoted, [], 1), ('valid-deep', deep, roots, 40),
    ]


DIGITS_4301 = b'1' + b'0' * 4300


def c_field_mutations(fields, index, rng):
    """1 つの context の 1 つの欄を変えた (名前, 欄の列) の一覧。"""
    out = []

    def put(name, position, value):
        copy = list(fields)
        copy[position] = value
        out.append((name, copy))
    for value in (b'x', b'-1', b'2000', b'+0', b'%d' % (index + 1)) + ((b'%d' % (index - 1),) if index else ()):
        put('id', 0, value)
    put('digits-4301', 0, DIGITS_4301)
    for value in ((b'0', b'-2', b'1') if index == 0 else (b'%d' % index, b'%d' % (index + 1), b'-1', b'')):
        put('parent', 1, value)
    put('digits-4301', 1, b'-' + b'0' * 4301)
    for value in (b'41', b'-1', b'%d' % (int(fields[2]) + 1)) + ((b'0',) if index else ()):
        put('depth', 2, value)
    put('digits-4301', 2, DIGITS_4301)
    for value in (b'2', b'-1', b'yes'):
        put('active', 3, value)
    put('digits-4301', 3, b'0' * 4301)
    for position, values in ((4, (b'"unterminated', b'relative', b'-', b'"\\000"')),
                             (5, (b'relative', b'"x')), (6, (b'relative', b'"x\\'))):
        for value in values:
            put('path', position, value)
    put('path-dash', 5, b'-')
    for value in (b'SHA1', b'md5', b'', b'sha1 '):
        put('format', 7, value)
    for value in (b'Files', b'packed', b''):
        put('backend', 8, value)
    width = 40 if fields[7] == b'sha1' else 64
    for value in (b'a' * (104 - width), b'A' * width, b'x', b''):
        put('object-name', 9, value)
    for value in (b'-1', b'16777217', b'x'):
        put('field-12', 10, value)
    put('digits-4301', 10, DIGITS_4301)
    put('field-12-good', 10, b'16777216')
    for value in (b'tree', b'', b'None'):
        put('field-13', 11, value)
    put('field-14-15', 12, b'anything\0goes')
    copy = list(fields)
    copy[0], copy[7] = b'x', b'md5'
    out.append(('two-errors-row', copy))
    copy = list(fields)
    copy[7], copy[4] = b'md5', b'relative'
    out.append(('two-errors-row', copy))
    copy = list(fields)
    copy[9], copy[8] = b'X', b'packed'
    out.append(('two-errors-row', copy))
    return out


def c_row_order_mutations(fields, index):
    """同じ context の行に、結果の違う 2 つの誤りを入れた (名前, 欄の列)。旧版の確かめる順が入れ替わると結果が変わる。"""
    out = []

    def put(name, **changes):
        copy = list(fields)
        for position, value in changes.items():
            copy[int(position[1:])] = value
        out.append((name, copy))
    zeros = b'0' * 4301
    # 作業ツリー等の引用(:85)は形式(:86)より先: 不正な引用で形式が md5 は state-schema。
    put('row-order-cfield-format', p4=b'"broken', p7=b'md5')
    put('row-order-cfield-format', p6=b'"x\\', p8=b'packed')
    # 4 つの integer(:82)は番号・親・深さ・有効の順。
    put('row-order-integers', p0=b'x', p1=b'-' + zeros)
    put('row-order-integers', p1=b'-' + zeros, p2=b'x')
    put('row-order-integers', p2=zeros, p3=b'x')
    put('row-order-integers', p0=DIGITS_4301, p3=b'2')
    # 第 11 欄(:89)は第 12 欄の integer(:90)より先、第 13 欄(:91)はその後。
    put('row-order-held-size', p9=b'A' * (40 if fields[7] == b'sha1' else 64), p10=DIGITS_4301)
    put('row-order-held-size', p9=b'x', p10=b'-' + zeros)
    put('row-order-size-kind', p10=DIGITS_4301, p11=b'tree')
    put('row-order-size-kind', p10=b'-' + zeros, p11=b'')
    # 番号と親の確かめ(:83)は、深さと有効の integer の後。
    put('row-order-binding-integers', p0=b'%d' % (index + 1), p2=zeros)
    put('row-order-binding-integers', p1=b'%d' % index if index else b'0', p3=zeros)
    # 引用(:85)は integer(:82)の後で、第 12 欄の integer(:90)の前。
    put('row-order-cfield-integers', p0=zeros, p5=b'"broken')
    put('row-order-cfield-integers', p4=b'"broken', p3=DIGITS_4301)
    put('row-order-cfield-size', p5=b'"broken', p10=DIGITS_4301)
    put('row-order-cfield-size', p4=b'"\\8"', p10=zeros)
    return out


def add_c(builder):
    rng = builder.rng
    top = os.fsencode(builder.root)
    state = os.path.join(top, b'guard-r4d3')

    def add(meta, contexts, tag, context_id=0, cwd=top):
        builder.add('C', 'parse_records', {'meta': meta.hex(), 'contexts': contexts.hex(), 'cwd': cwd.hex(),
                                           'context_id': str(context_id), 'state': state.hex()}, tag)
    meta = meta_raw(top, top)
    for name, contexts, roots, chosen in c_bases(top, rng):
        rows = [ctx_row(fields) for fields in contexts] + roots
        add(meta, contexts_raw(rows), name, chosen)
    bases = c_bases(top, rng)
    for name, contexts, roots, chosen in (bases[0], bases[2], bases[1]):
        for index in sorted({0, len(contexts) - 1}):
            for tag, fields in c_field_mutations(contexts[index], index, rng):
                rows = [ctx_row(f) for f in contexts]
                rows[index] = ctx_row(fields)
                add(meta, contexts_raw(rows + roots), tag, chosen if tag.startswith('field-14') else 0)
            for tag, fields in c_row_order_mutations(contexts[index], index):
                rows = [ctx_row(f) for f in contexts]
                rows[index] = ctx_row(fields)
                add(meta, contexts_raw(rows + roots), tag)
        rows = [ctx_row(fields) for fields in contexts]
        count = len(contexts)
        for row in (b'context\t0', ctx_row(contexts[0][:13]), ctx_row(contexts[0] + [b'x']), b'Context\t' + b'\t'.join(
                contexts[0]), b'', b'unknown\tx', b'root\t0:0', b'root\t0:0\t/x\ty'):
            add(meta, contexts_raw(rows + [row]), 'row')
        for root in (b'root\t0\t/x', b'root\t0:0:0\t/x', b'root\t%d:0\t/x' % count, b'root\t0:%d\t/x' % count,
                     b'root\t0:0\tx', b'root\t0:0\t"x', b'root\ta:0\t/x', b'root\t' + DIGITS_4301 + b':0\t/x',
                     b'root\t0:-1\t/x', b'root\t:\t/x'):
            add(meta, contexts_raw(rows + [root]), 'digits-4301' if DIGITS_4301 in root else 'root')
        for context_id in (-1, count, 10 ** 30, -(10 ** 30)):
            add(meta, contexts_raw(rows), 'context-id', context_id)
        inactive = [index for index, fields in enumerate(contexts) if fields[3] == b'0']
        for index in inactive:
            add(meta, contexts_raw(rows), 'context-id', index)
        # 2 つの誤り: cwd の不一致と contexts の誤り(state-binding が先)。LF で終わらず形式名も違う contexts(state-schema)。
        add(meta_raw(top, top + b'/other'), b'broken', 'cwd-and-contexts')
        add(meta_raw(top, top + b'/other'), contexts_raw(rows, b'context-plan-v2'), 'cwd-and-contexts')
        add(meta, contexts_raw(rows, b'context-plan-v2')[:-1], 'unterminated-and-old-name')
        add(meta, contexts_raw(rows)[:-1], 'unterminated-and-old-name')
        add(meta_raw(top + b'/x', top), contexts_raw(rows), 'cwd-toplevel', count)
    rows = [ctx_row(fields) for fields in bases[0][1]] + bases[0][2]
    for meta_variant, tag in (
            (joined([b'cwd\t' + top, b'cwd\t' + top, b'toplevel\t' + top]), 'cwd-toplevel'),
            (joined([b'cwd\t' + top, b'toplevel\t' + top, b'toplevel\t' + top]), 'cwd-toplevel'),
            (joined([b'toplevel\t' + top]), 'cwd-toplevel'), (joined([b'cwd\t' + top]), 'cwd-toplevel'),
            (joined([b'cwd ' + top, b'toplevel\t' + top]), 'meta-line'),
            (joined([b'toplevel\t' + top, b'cwd\t' + cquote(top, force=True)]), 'cwd-toplevel'),
            (joined([b'toplevel\t' + cquote(top, force=True), b'cwd\t' + top]), 'cwd-toplevel'),
            (joined([b'toplevel\t' + top, b'cwd\t' + top + b'/']), 'cwd-toplevel'),
            (joined([b'toplevel\t' + top, b'cwd\t"' + top]), 'meta-line'),
            (joined([b'toplevel\t' + top, b'cwd\t' + top, b'cwdx\tvalue', b'other\t"broken']), 'meta-line'),
            (joined([b'toplevel\t' + top, b'cwd\t' + top])[:-1], 'meta-line'), (b'', 'meta-line')):
        add(meta_variant, contexts_raw(rows), tag)
    for contexts, tag in ((b'', 'contexts-header'), (b'context-plan-v1', 'contexts-header'),
                          (b'context-plan-v1\n', 'contexts-header'), (b'context-plan-v2\n', 'contexts-header'),
                          (b'context-plan-v1 \n' + joined(rows), 'contexts-header'),
                          (b'\ncontext-plan-v1\n' + joined(rows), 'contexts-header')):
        add(meta, contexts, tag)


# ---- O: config-origins.txt

def o_base(rng, candidates):
    rows = [b'config-origins-v3', b'env\tHOME\tset\t' + hexs(rng, 64).encode(),
            b'env\tGIT_CONFIG\tunset\t' + hexs(rng, 64).encode(),
            b'plan-admin\t/r4d3-origins/.git\t' + cquote(b'/r4d3-origins/co\tmmon'),
            b'plan-root\t/r4d3-origins/.git/config']
    for number in range(candidates):
        name = b'/r4d3-origins/c%d' % number + rng.choice((b'', b'/config', b'\tq', b'"x', b'/\xff'))
        rows.append(b'candidate\t' + cquote(name))
        for _ in range(rng.randrange(0, 4)):
            rows.append(rng.choice((b'node\td\t1:%d\t-\t/' % number, b'node\tl\t2:3\t' + cquote(b'target') + b'\t/x',
                                    b'missing\t/r4d3-origins/none%d' % number, b'data\t' + hexs(rng, 64).encode(),
                                    b'node\tany\tthing\tgoes\there')))
    return rows


def add_o(builder):
    rng = builder.rng
    add = lambda rows, tag: builder.add('O', 'parse_origins', {'raw': joined(rows).hex()}, tag)  # noqa: E731
    for candidates in (0, 1, 2, 3, 5, 8, 13, 30):
        add(o_base(rng, candidates), 'valid')
    for _ in range(12):
        add(o_base(rng, rng.randrange(0, 10)), 'valid')
    for _ in range(4):
        rows = o_base(rng, rng.randrange(2, 6))
        place = rng.randrange(5, len(rows) + 1)

        def insert(row, at=place):
            copy = list(rows)
            copy.insert(at, row)
            return copy
        first_candidate = next(i for i, row in enumerate(rows) if row.startswith(b'candidate'))
        for row in (b'node\td\t1\t-', b'node\td\t1\t-\t/\tx', b'missing', b'missing\t/a\t/b', b'data', b'data\ta\tb'):
            add(insert(row, len(rows)), 'field-count')
        for row in (b'node\td\t1:2\t-\t/', b'missing\t/x', b'data\tabc'):
            add(insert(row, first_candidate), 'no-owner')
        add(insert(b'candidate\t/r4d3-dup', len(rows)) + [b'candidate\t"/r4d3-dup"'], 'duplicate-after-quote')
        add(insert(b'candidate\t/r4d3-dup', len(rows)) + [b'candidate\t/r4d3-dup'], 'duplicate')
        for row in (b'candidate\t"broken', b'candidate', b'candidate\t/a\t/b', b'candidate\t"\\000"'):
            add(insert(row), 'candidate')
        for row in (b'plan-admin\t/a\t/b', b'plan-admin\t/a\t/b\t/c'):
            add(insert(row), 'admin-twice')
        for row in (b'plan-admin\t"x\t/b', b'plan-admin\t/a\t"\\8"'):
            add([r for r in rows if not r.startswith(b'plan-admin')] + [row], 'admin-cfield')
        add([r for r in rows if not r.startswith(b'plan-admin')], 'admin-missing')
        add([r for r in rows if not r.startswith(b'plan-admin')] + [b'plan-admin\t/a'], 'admin-missing')
        for row in (b'plan-root\t"x', b'plan-root', b'plan-root\t/a\t/b', b'env\tA\tB', b'env\tA\tB\tC\tD',
                    b'unknown\tx', b'', b'Candidate\t/x', b'node'):
            add(insert(row), 'row')
        add([b'config-origins-v2'] + rows[1:], 'old-name')
        add(rows[1:], 'old-name')
        add([b'config-origins-v3 '] + rows[1:], 'old-name')
        builder.add('O', 'parse_origins', {'raw': joined(rows)[:-1].hex()}, 'no-final-lf')
    # 大文字と小文字・末尾の `/`・引用の中身だけが違う名前は、別の candidate である(重なりは cfield の後の bytes で決まる)。
    distinct = o_base(rng, 0) + [b'candidate\t/r4d3-dup', b'candidate\t/R4D3-DUP', b'candidate\t/r4d3-dup/',
                                 b'candidate\t' + cquote(b'/r4d3-dup\t'), b'candidate\t"/r4d3-dup\\\\"']
    add(distinct, 'distinct-names')
    for raw, tag in ((b'', 'empty'), (b'config-origins-v3\n', 'admin-missing'), (b'config-origins-v3', 'no-final-lf'),
                     (b'\n', 'empty')):
        builder.add('O', 'parse_origins', {'raw': raw.hex()}, tag)


def add_a(builder):
    rng = builder.rng
    for depth in range(41):
        builder.add('A', 'ancestors', {'parents': list(range(-1, depth)), 'n': depth}, 'chain-40' if depth == 40 else
                    'chain')
    for _ in range(30):
        count = rng.randrange(1, 30)
        parents = [-1] + [rng.randrange(0, n) for n in range(1, count)]
        builder.add('A', 'ancestors', {'parents': parents, 'n': rng.randrange(0, count)}, 'branches')


def add_p(builder):
    rng = builder.rng
    literal = lambda raw, under, tag: builder.add('P', 'literal', {'raw': raw.hex(), 'under': under}, tag)  # noqa: E731
    revision = lambda value, tag: builder.add('P', 'revision', {'value': value}, tag)  # noqa: E731
    text = lambda value: {'text': [ord(c) for c in value]}  # noqa: E731
    for under in (False, True):
        literal(b'a' * 4096, under, 'literal-4096')
        literal(b'a/' * 2047 + b'bb', under, 'literal-4096')
        literal(b'a' * 4097, under, 'literal-4097')
        literal(b'.', under, 'under-dot')
        for raw in (b'', b'/a', b'/', b'a\0b', b'\0', b'..', b'a/./b', b'a//b', b'a/', b'./a', b'a/..', b'.a', b'a.',
                    b'...', b'a/b', b'a b/\xff', b'.git/x', b'a/.b/c..', b'x/./', b'\n', b'a\\b'):
            literal(raw, under, 'literal')
    for _ in range(40):
        raw = bytes(rng.choice(b'ab./\0') for _ in range(rng.randrange(0, 8)))
        literal(raw, rng.random() < 0.5, 'literal-random')
    for value in ('HEAD', 'head', ' HEAD', 'HEAD\n', 'HEAD^', 'refs/heads/main', 'refs/x', 'ref/x', 'refs/',
                  'refs//a', 'refs/a/', 'refs/a.', 'refs/a..b', 'refs/a@{b', 'refs/a@b', 'refs/a{b', 'refs/.a',
                  'refs/a.lock', 'refs/a.lockx', 'refs/a/.b', 'refs/é', 'main', ''):
        revision(text(value), 'revision')
    for width, tag in ((39, 'hex-39-41'), (40, 'hex-39-41'), (41, 'hex-39-41'), (63, 'hex-63-65'), (64, 'hex-63-65'),
                       (65, 'hex-63-65')):
        revision(text(hexs(rng, width)), tag)
        revision(text('a' * width), tag)
    revision(text('A' * 40), 'upper-hex')
    revision(text('aB' * 20), 'upper-hex')
    revision(text('G' * 64), 'upper-hex')
    for c in list(range(32)) + [127] + [ord(x) for x in ' ~^:?*[\\']:
        revision(text('refs/a' + chr(c) + 'b'), 'forbidden')
    revision(text('refs/' + 'a' * 1019), 'revision-1024')
    revision(text('refs/' + 'a' * 1020), 'revision-1025')
    revision(text('refs/' + 'é' * 509 + 'a'), 'revision-1024')
    revision(text('refs/' + 'é' * 510), 'revision-1025')
    for points in ([0xdc80], [0xd800], [0x72, 0x65, 0x66, 0x73, 0x2f, 0xdcff], [0xdc80] + [0x61] * 2000,
                   [0x48, 0x45, 0x41, 0x44, 0xdfff]):
        revision({'text': points}, 'surrogate')
    for name in NOT_TEXT:
        revision({'not_text': name}, 'not-text')
    for _ in range(30):
        value = rng.choice(('refs/', 'HEAD', '')) + ''.join(rng.choice('ab/.@{~^:é\x00H') for _ in range(rng.randrange(0, 9)))
        revision(text(value), 'revision-random')


def add_v(builder):
    add = lambda name, tag: builder.add('V', 'valid_ref', {'name': name.hex()}, tag)  # noqa: E731
    add(b'refs/' + b'a' * 1019, 'length-1024')
    add(b'refs/' + b'a' * 1020, 'length-1025')
    add(b'refs/' + b'a/' * 509 + b'b', 'length-1024')
    for c in list(range(32)) + [127] + list(b' ~^:?*[\\'):
        add(b'refs/a' + bytes([c]) + b'b', 'forbidden-byte')
    for name in (b'refs/heads/main', b'refs/', b'refs', b'Refs/a', b'refsx/a', b'/refs/a', b'refs/a/', b'refs/a.',
                 b'refs/a..b', b'refs/a@{b', b'refs/a@b', b'refs/a{', b'refs/@', b'refs/\x80\xff', b'refs/a.lockx',
                 b'heads/main', b'HEAD', b''):
        add(name, 'rules')
    for part in (b'', b'.x', b'x.lock', b'..', b'.', b'x.', b'x..y', b'.lock', b'a.lock.b', b'-x', b'x_y'):
        add(b'refs/a/' + part + b'/z', 'parts')
        add(b'refs/' + part, 'parts')
    rng = builder.rng
    for _ in range(30):
        add(rng.choice((b'refs/', b'refs/heads/', b'', b'ref/')) + bytes(rng.choice(b'ab/.@{~\x00\x7f\xff')
                                                                       for _ in range(rng.randrange(0, 9))), 'random')


# ---- K: packed-refs と 1 つの参照のファイル

def add_k(builder):
    rng = builder.rng

    def packed(raw, width, tag):
        number = builder.numbers.get('K', 0)
        folder = builder.folder('k', 'K-%04d' % number)
        (folder / 'packed-refs').write_bytes(raw)
        builder.add('K', 'file_refs', {'kind': 'packed', 'raw': raw.hex(), 'width': width,
                                       'dir': os.fsencode(folder).hex()}, tag)

    def loose(name, raw, width, tag):
        number = builder.numbers.get('K', 0)
        folder = builder.folder('k', 'K-%04d' % number)
        (folder / 'refs').mkdir()
        (folder / 'refs' / name).write_bytes(raw)
        builder.add('K', 'file_refs', {'kind': 'loose', 'name': (b'refs/' + os.fsencode(name)).hex(),
                                       'head': raw[:4097].hex(), 'width': width, 'dir': os.fsencode(folder).hex()},
                    tag)
    a, b = hexs(rng, 40).encode(), hexs(rng, 40).encode()
    long_a = hexs(rng, 64).encode()
    packed(b'# pack-refs with: peeled fully-peeled sorted \n' + a + b' refs/heads/a\n^' + b + b'\n', 40,
           'packed-comment-peeled')
    packed(b'#\n^\n#x\n^y\n', 40, 'packed-comment-peeled')
    packed(a + b' refs/heads/a\n\n' + b + b' refs/heads/b\n', 40, 'packed-empty-line')
    packed(b'\n', 40, 'packed-empty-line')
    packed(a + b' refs/heads/a\r\n' + b + b' refs/heads/b\r' + a + b' refs/heads/c', 40, 'packed-cr-crlf')
    packed(a + b' refs/heads/a\r\r\n', 40, 'packed-cr-crlf')
    for line in (a + b'  refs/x', b' ' + a + b' refs/x', a + b' refs/x ', a + b' refs/x y', a + b'\trefs/x', a,
                 a + b' ', a + b' refs/\x0bx', a + b' refs/\x0cx', a + b' refs/\x1cx'):
        packed(a + b' refs/heads/ok\n' + line + b'\n', 40, 'packed-spaces')
    packed(long_a + b' refs/heads/a\n', 40, 'packed-width')
    packed(a + b' refs/heads/a\n', 64, 'packed-width')
    packed(long_a + b' refs/heads/a\n', 64, 'packed-width')
    packed(a[:39] + b' refs/heads/a\n', 40, 'packed-width')
    packed(a.upper() + b' refs/heads/a\n', 40, 'packed-upper')
    packed(b'G' * 40 + b' refs/heads/a\n', 40, 'packed-upper')
    packed(a + b' refs/heads/a\n' + b + b' refs/heads/b\n' + b + b' refs/heads/a\n', 40, 'packed-duplicate')
    packed(b'', 40, 'packed-empty')
    packed(a + b' refs/heads/a', 40, 'packed-no-final-lf')
    for _ in range(50):
        lines = []
        for number in range(rng.randrange(1, 40)):
            choice = rng.random()
            if choice < 0.08:
                lines.append(b'^' + hexs(rng, 40).encode())
            elif choice < 0.12:
                lines.append(b'# c%d' % number)
            elif choice < 0.13:
                lines.append(rng.choice((b'', b'x', b'A' * 40 + b' refs/x')))
            else:
                lines.append(hexs(rng, 40).encode() + b' refs/heads/r%d' % rng.randrange(0, 30))
        separator = rng.choice((b'\n', b'\r\n', b'\r'))
        packed(separator.join(lines) + rng.choice((separator, b'')), 40, 'packed-random')
    for raw in (b'ref: refs/heads/main\n', b'ref: refs/heads/main', b'ref: refs/heads/main\n\n\n', b'ref: \n',
                b'ref: ', b'ref:refs/x\n', b'ref: refs/x\r\n', b'ref:  refs/x\n', b'Ref: refs/x\n', b'ref: a\nb\n'):
        loose('main', raw, 40, 'loose-ref')
    for raw in (a, a + b'\n', a + b'\n\n', a + b' ', b' ' + a, a + b'\r\n', a + b'\n' + b'x' * 10):
        loose('main', raw, 40, 'loose-lf-count')
    for raw in (b'ref: ' + b'a' * 4092, b'ref: ' + b'a' * 4092 + b'\n', b'ref: ' + b'a' * 5000 + b'\n',
                b'ref: refs/' + b'a' * 4087 + b'\n\n', a + b'\n' + b'\n' * 5000, b'ref: ' + b'\n' * 5000):
        loose('main', raw, 40, 'loose-4097')
    for raw, width in ((long_a, 40), (a, 64), (long_a + b'\n', 64), (a.upper(), 40), (b'', 40), (b'\n', 40)):
        loose('main', raw, width, 'loose-width')
    for name in ('x.lock', 'with space', '-dash', 'Upper'):
        loose(name, a + b'\n', 40, 'loose-name')
    for _ in range(20):
        raw = rng.choice((b'ref: refs/heads/r%d' % rng.randrange(0, 9) + b'\n' * rng.randrange(0, 3),
                          hexs(rng, 40).encode() + b'\n' * rng.randrange(0, 3)))
        loose('r%d' % rng.randrange(0, 1000), raw, 40, 'loose-random')


# ---- R: 参照の解決

def oids(rng, count, width=40):
    return [hexs(rng, width) for _ in range(count)]


def cat_response(data, corrupt=None, rng=None):
    """旧版を 1 回動かして cat-file に渡す名前を求め、正しい出力(か、わざと崩した出力)を作る。"""
    probe = dict(data, responses=[])
    recorder = []
    budget, _ = make_budget(['normal'])
    outcome(lambda: legacy_refs(probe, budget, recorder, None))
    if not recorder:
        return []
    ids = [line for line in unhex(recorder[0][2]).decode().split('\n') if line]
    types_ = ['blob', 'tree', 'commit', 'tag']
    rows = ['%s %s' % (oid, types_[index % 4]) for index, oid in enumerate(ids)]
    if corrupt == 'rc':
        return [['rc', 1, ('\n'.join(rows) + '\n').encode().hex()]]
    if corrupt == 'missing':
        rows = rows[:-1]
    elif corrupt == 'type':
        rows[-1] = rows[-1].rsplit(' ', 1)[0] + ' object'
    elif corrupt == 'order' and len(rows) > 1:
        rows[0], rows[1] = rows[1], rows[0]
    return [['rc', 0, ('\n'.join(rows) + '\n').encode().hex()]]


def add_r(builder):
    rng = builder.rng

    def add(common, local, tag, same_dir=True, backend='files', responses=None, corrupt=None):
        data = {'common': common, 'local': local, 'same_dir': same_dir, 'backend': backend, 'responses': []}
        data['responses'] = cat_response(data, corrupt) if responses is None else responses
        builder.add('R', 'resolve_refs', data, tag)
    o = oids(rng, 6)
    ref = lambda name, oid=None, target=None: [name.hex(), oid, None if target is None else target.hex()]  # noqa: E731
    direct = [ref(b'refs/heads/main', o[0]), ref(b'refs/heads/b', o[1]), ref(b'refs/tags/v1', o[0])]
    add(direct, [], 'direct')
    add(direct, [], 'reftable', backend='reftable')
    privates = direct + [ref(b'refs/worktree/w', o[2]), ref(b'refs/bisect/bad', o[3]), ref(b'refs/rewritten/r', o[4])]
    local = [ref(b'refs/worktree/w', o[5]), ref(b'refs/bisect/good', o[3]), ref(b'refs/heads/local-only', o[4]),
             ref(b'HEAD', None, b'refs/heads/b')]
    for same_dir in (True, False):
        for backend in ('files', 'reftable'):
            add(privates, local, 'private-names', same_dir, backend)
            add(privates + [ref(b'HEAD', o[0])], local, 'head', same_dir, backend)
            add(privates + [ref(b'HEAD', None, b'refs/heads/main')], [], 'head', same_dir, backend)
    add(privates, local, 'same-dir', True)
    add(privates, local, 'other-dir', False)
    add([ref(b'HEAD', o[1])] + direct, [ref(b'HEAD', o[2])], 'head', False)
    for name in (b'FETCH_HEAD', b'ORIG_HEAD', b'A', b'A1_', b'AB_9'):
        add(direct + [ref(name, o[3])], [], 'pseudo-ref')
    for name in (b'a', b'AB-C', b'Ab', b'_A', b'1A'):
        add(direct + [ref(name, o[3])], [], 'invalid-name')
    for name in (b'refs/heads/a..b', b'heads/x', b'refs/x.lock', b'refs/x/', b'refs/a b', b'refs/' + b'a' * 1020):
        add(direct + [ref(name, o[3])], [], 'invalid-name')
    for target in (b'HEAD', b'refs/x.lock', b'heads/x', b'refs/a..b', b''):
        add(direct + [ref(b'refs/heads/sym', None, target)], [], 'invalid-target')
    chain = [ref(b'refs/a', None, b'refs/b'), ref(b'refs/b', None, b'refs/c'), ref(b'refs/c', o[0])]
    add(chain, [], 'chain')
    add([ref(b'refs/a', None, b'refs/none'), ref(b'refs/b', None, b'refs/a')] + direct, [], 'dead-end')
    add([ref(b'refs/a', None, b'refs/none')], [], 'no-ids')
    add([ref(b'refs/a', None, b'refs/b'), ref(b'refs/b', None, b'refs/a')], [], 'cycle')
    add([ref(b'refs/a', None, b'refs/a')], [], 'cycle')
    add(direct + [ref(b'refs/x', None, b'refs/y'), ref(b'refs/y', None, b'refs/z'), ref(b'refs/z', None, b'refs/x')],
        [], 'cycle')
    add([ref(b'refs/c', o[0]), ref(b'refs/b', None, b'refs/c'), ref(b'refs/a', None, b'refs/b'),
         ref(b'refs/d', None, b'refs/a')], [], 'reaches-resolved')
    add([ref(b'refs/b', None, b'refs/none'), ref(b'refs/a', None, b'refs/b')], [], 'reaches-resolved')
    # 解決した順と名前の順が違う入力(N 回目の期限と循環のどちらになるかは、たどる順で決まる)。
    add([ref(b'refs/z', o[1]), ref(b'refs/m', None, b'refs/n'), ref(b'refs/n', None, b'refs/m'), ref(b'refs/a', o[2])],
        [], 'order-differs')
    add([ref(b'refs/z%d' % (9 - n), o[n % 6]) for n in range(10)] + [ref(b'refs/loop', None, b'refs/loop')], [],
        'order-differs')
    one = [ref(b'refs/a', o[0])]
    for rc in (1, 128, -9, 2):
        add(one, [], 'cat-file-rc', responses=[['rc', rc, (o[0] + ' commit\n').encode().hex()]])
    for out in ('', o[0] + ' commit\n' + o[0] + ' commit\n', o[1] + ' commit\n', o[0] + '\n', o[0] + ' commit x\n',
                o[0] + ' commit', o[0] + '\rcommit\n'):
        add(one, [], 'cat-file-rows', responses=[['rc', 0, out.encode().hex()]])
    for kind in ('object', 'Blob', 'commits', ''):
        add(one, [], 'cat-file-type', responses=[['rc', 0, (o[0] + ' ' + kind + '\n').encode().hex()]])
    for out in (o[0] + '\tcommit\n', o[0] + '  commit\n', ' ' + o[0] + ' commit \r\n', o[0] + '\x0bcommit\x0c\n',
                o[0] + '\x1ccommit\n', o[0] + ' commit\r'):
        add(one, [], 'cat-file-spacing', responses=[['rc', 0, out.encode().hex()]])
    add(one, [], 'cat-file-os', responses=[['os', 2]])
    two = [ref(b'refs/b', o[1]), ref(b'refs/a', o[0]), ref(b'refs/c', o[0])]
    for corrupt in (None, 'rc', 'missing', 'type', 'order'):
        add(two, [], 'cat-file-rows', corrupt=corrupt)
    width64 = oids(rng, 3, 64)
    add([ref(b'refs/a', width64[0]), ref(b'refs/b', None, b'refs/a'), ref(b'HEAD', width64[1])], [], 'reftable',
        backend='reftable')
    pool = [b'refs/heads/r%d' % n for n in range(12)] + [b'refs/worktree/w%d' % n for n in range(3)] + \
        [b'refs/bisect/b0', b'refs/rewritten/r0', b'HEAD', b'FETCH_HEAD', b'refs/tags/t0']
    for _ in range(150):
        values = []
        for side in range(2):
            items = []
            for name in rng.sample(pool, rng.randrange(0, len(pool))):
                if rng.random() < 0.55:
                    items.append(ref(name, rng.choice(o)))
                else:
                    items.append(ref(name, None, rng.choice(pool + [b'refs/heads/none'])))
            values.append(items)
        corrupt = rng.choice((None,) * 9 + ('missing',))
        add(values[0], values[1], 'random', rng.random() < 0.5, rng.choice(('files', 'reftable')), corrupt=corrupt)


# ---- H: HEAD

def add_h(builder):
    rng = builder.rng
    a, b = hexs(rng, 40), hexs(rng, 40)
    long_a = hexs(rng, 64)
    record = lambda name, oid, resolution=None: [name.hex(), oid, None, resolution or ('resolved' if oid else 'unresolved')]  # noqa: E731,E501
    records = [record(b'refs/heads/main', a), record(b'refs/heads/other', b), record(b'refs/heads/none', None),
               record(b'refs/heads/main', b)]
    good = {'records': records}
    failing = {'records': [], 'fail': ['failure', 'symbolic-ref-cycle', 23]}
    failing_os = {'records': [], 'fail': ['os', 13]}

    def files(raw, refs, tag, width=40):
        number = builder.numbers.get('H', 0)
        folder = builder.folder('h', 'H-%04d' % number)
        (folder / 'HEAD').write_bytes(raw)
        builder.add('H', 'head', {'backend': 'files', 'head': raw[:4097].hex(), 'width': width, 'refs': refs,
                                  'dir': os.fsencode(folder).hex()}, tag)

    def reftable(head, refs, tag, width=40):
        spec = dict(refs, head=head)
        builder.add('H', 'head', {'backend': 'reftable', 'width': width, 'refs': spec}, tag)
    for refs, tag in ((good, 'ref'), (failing, 'refs-failing'), (failing_os, 'refs-failing'), ({'records': []}, 'ref')):
        for raw in (b'ref: refs/heads/main\n', b'ref: refs/heads/main', b'ref: refs/heads/main\n\n',
                    b'ref: refs/heads/other\n', b'ref: refs/heads/none\n', b'ref: refs/heads/absent\n'):
            files(raw, refs, tag)
        for raw in (b'ref: heads/main\n', b'ref: refs/heads/a..b\n', b'ref: \n', b'ref: HEAD\n', b'ref: refs/\n',
                    b'ref: refs/heads/' + b'a' * 5000 + b'\n'):
            files(raw, refs, 'invalid-target')
        for raw in (a.encode() + b'\n', a.encode(), a.encode() + b'\n\n', a.upper().encode(), a.encode() + b' ', b'',
                    b'ref:refs/heads/main\n'):
            files(raw, refs, 'object-name')
        files(long_a.encode() + b'\n', refs, 'width', 64)
        files(a.encode() + b'\n', refs, 'width', 64)
        files(long_a.encode(), refs, 'width', 40)
    for refs, tag in ((good, 'reftable-direct'), (failing, 'refs-failing'), ({'records': []}, 'reftable-direct')):
        reftable(None, refs, 'reftable-missing')
        reftable([a, None], refs, tag)
        reftable([None, None], refs, tag)
        reftable(['not-hex', None], refs, tag)
        reftable([long_a, None], refs, tag)
        reftable([None, b'refs/heads/main'.hex()], refs, 'refs-calls')
        reftable([None, b'refs/heads/none'.hex()], refs, 'refs-calls')
        reftable([None, b'refs/heads/absent'.hex()], refs, 'refs-calls')
        reftable([None, b'HEAD'.hex()], refs, 'invalid-target')
        reftable([a, b'refs/heads/other'.hex()], refs, 'refs-calls')


# ---- Q: REV の object の名前

def add_q(builder):
    rng = builder.rng
    a, b = hexs(rng, 40), hexs(rng, 40)
    long_a = hexs(rng, 64)
    points = lambda value: [ord(c) for c in value]  # noqa: E731
    out = lambda text: text.encode('latin-1').hex() if isinstance(text, str) else text.hex()  # noqa: E731
    ok = lambda text: [['rc', 0, out(text)]]  # noqa: E731
    records = {'records': [['refs/heads/main'.encode().hex(), a, None, 'resolved'],
                           ['refs/heads/none'.encode().hex(), None, None, 'unresolved'],
                           ['refs/heads/x'.encode().hex(), 'HEAD', None, 'resolved'],
                           ['refs/heads/main'.encode().hex(), b, None, 'resolved']]}
    none = {'records': []}
    refs_failing = {'records': [], 'fail': ['failure', 'invalid-ref', 23]}
    head_a = {'oid': a, 'target': 'refs/heads/main'.encode().hex()}
    head_none = {'oid': None, 'target': 'refs/heads/main'.encode().hex()}
    head_word = {'oid': 'HEAD'}
    head_failing = {'fail': ['os', 5]}

    def add(value, tag, unborn=False, responses=None, refs=None, head=None, width=40):
        builder.add('Q', 'oid', {'value': value if isinstance(value, list) else points(value), 'unborn': unborn,
                                 'width': width, 'responses': ok(a + '\n') if responses is None else responses,
                                 'refs': records if refs is None else refs, 'head': head_a if head is None else head},
                    tag)
    for unborn in (False, True):
        add('HEAD', 'head', unborn)
        add('HEAD', 'unborn', unborn, head=head_none)
        add('HEAD', 'unborn', unborn, head=head_word, responses=[['rc', 1, '']])
        add('HEAD', 'unborn', unborn, responses=[['rc', 1, '']])
        add('HEAD', 'unborn', unborn, responses=[['rc', 128, '']], head=head_word)
        add('HEAD', 'head', unborn, head=head_failing)
        add('refs/heads/main', 'refs-found', unborn)
        add('refs/heads/x', 'refs-found', unborn, responses=[['rc', 1, '']], head=head_none)
        add('refs/heads/x', 'refs-found', unborn, responses=[['rc', 1, '']])
        add('refs/heads/none', 'refs-missing', unborn)
        add('refs/heads/absent', 'refs-missing', unborn)
        add('refs/heads/main', 'refs-missing', unborn, refs=none)
        add('refs/heads/main', 'refs-missing', unborn, refs=refs_failing)
        add(a, 'hex', unborn)
        add('main', 'hex', unborn)
        add('HEADS', 'hex', unborn)
        add('', 'hex', unborn)
    for rc, tag in ((0, 'rev-parse-rc'), (1, 'rev-parse-rc'), (128, 'rev-parse-rc'), (129, 'rev-parse-rc'),
                    (-1, 'rev-parse-rc'), (2, 'rev-parse-rc'), (127, 'rev-parse-rc')):
        add(a, tag, False, responses=[['rc', rc, out(a + '\n')]])
        add('HEAD', tag, True, responses=[['rc', rc, out(a + '\n')]], head=head_word)
    add(a, 'rev-parse-rc', responses=[['os', 2]])
    for text in ('  ' + a + '  \n', '\t' + a + '\r\n', a, '\x0b' + a + '\x0c', a + ' x\n', '', '\n', a + '\x1c'):
        add(a, 'output-spacing', responses=ok(text))
    for raw in (b'\xff' + a.encode(), a.encode() + b'\x80', 'é'.encode() + a.encode()):
        add(a, 'output-ascii', responses=ok(raw))
    for text, width in ((a.upper(), 40), (long_a, 40), (a, 64), (long_a + '\n', 64), (a[:39], 40)):
        add(a, 'output-width', responses=ok(text), width=width)
    lone = points('refs/x') + [0xd800]
    add(lone, 'refs-surrogate-0', refs=none)
    add(lone, 'refs-surrogate-1', refs={'records': [records['records'][0]]})
    add(lone, 'refs-surrogate-1', refs=records)
    add(lone, 'refs-surrogate-failing', refs=refs_failing)
    add(points('refs/') + [0xdc80], 'refs-surrogate-1', refs=records)
    for value in ([0xdc80], [0xdcff, 0x61], points('main') + [0xdc80]):
        add(value, 'rev-parse-dc80')
    for value in ([0xd800], [0xdc7f], points('main') + [0xdfff], [0xdc00]):
        add(value, 'rev-parse-d800')
    for _ in range(12):
        value = rng.choice(('HEAD', 'refs/heads/main', 'refs/heads/none', a, 'v1.0', 'refs/'))
        output = rng.choice((a, b, long_a)) + rng.choice(('', '\n', ' \n'))
        add(value, 'random', rng.random() < 0.5, responses=[['rc', rng.choice((0, 0, 0, 1, 128, 129)), out(output)]],
            head=rng.choice((head_a, head_none, head_word)))


# ---- T: 本物の take の記録

GIT_FIXED = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
    'GIT_AUTHOR_NAME': 'R4D', 'GIT_AUTHOR_EMAIL': 'r4d@example.invalid',
    'GIT_COMMITTER_NAME': 'R4D', 'GIT_COMMITTER_EMAIL': 'r4d@example.invalid',
    'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z',
    'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'GIT_PAGER': 'cat',
}


def checked_run(argv, cwd, env, timeout, label):
    result = run_command(argv, cwd=str(cwd), env=env, timeout=timeout)
    if result.timed_out:
        raise CheckError('%s が %d 秒の上限に当たった' % (label, timeout))
    if result.returncode != 0:
        raise CheckError('%s が失敗した(%d): %s' % (label, result.returncode,
                                                  result.stderr.decode('utf-8', 'replace')[-600:]))
    return result.stdout


def take_records(work, protected, kind):
    """1 つの repo を作り、本物の take(旧 implement-guard.sh)で保護記録を作る。scripts/diagnostic_test_support.py と同じ環境。"""
    home = work / ('home-' + kind)
    config = work / ('config-' + kind)
    home.mkdir()
    config.mkdir()
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C', 'HOME': str(home), 'XDG_CONFIG_HOME': str(config),
           'XDG_STATE_HOME': str(protected), 'TMPDIR': str(work), 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CEILING_DIRECTORIES': str(work),
           'GIT_PAGER': 'cat', 'PYTHONDONTWRITEBYTECODE': '1'}
    git_env = dict(GIT_FIXED, PATH=env['PATH'], HOME=str(home))
    repo = work / ('repo-' + kind)
    options = {'files': [], 'sha256': ['--object-format=sha256'], 'reftable': ['--ref-format=reftable'],
               'linked-worktree': []}[kind]

    def git(*args, cwd=repo):
        return checked_run(['git', '-c', 'init.defaultBranch=main', '-c', 'core.fsmonitor=', '-c',
                            'core.hooksPath=/dev/null', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', *args],
                           cwd, git_env, COMMAND_TIMEOUT, 'git ' + args[0])
    git('init', '-q', *options, str(repo), cwd=work)
    (repo / 'seed.txt').write_bytes(b'public seed\n')
    git('add', '--', 'seed.txt')
    git('commit', '-qm', 'seed')
    target = repo
    if kind == 'linked-worktree':
        target = work / 'linked'
        git('worktree', 'add', '-q', '-b', 'linked', str(target))
    (target / 'task').mkdir()
    (target / 'task/t.md').write_bytes('# タスク\n\n- [ ] 通常の試験\n'.encode())
    (target / 'link').symlink_to('seed.txt')
    stdout = checked_run(['bash', str(GUARD), 'take', '--cwd', str(target), '--task-md', 'task/t.md'], work, env,
                         TAKE_TIMEOUT, '本物の take(%s)' % kind)
    fields = dict(line.split('=', 1) for line in stdout.decode('utf-8', 'surrogateescape').splitlines())
    state = Path(fields['STATE_DIR'])
    if state.parent != protected / 'dev-workflow' or not state.name.startswith('guard-'):
        raise CheckError('take の記録の場所が想定と違う: %s' % state)
    files = {b'manifest.tsv': (state / 'manifest.tsv').read_bytes()}
    for entry in sorted((state / 'snapshot').iterdir()):
        files[b'snapshot/' + os.fsencode(entry.name)] = entry.read_bytes()
    return os.path.realpath(os.fsencode(target)), files


def add_t(builder):
    work = builder.folder('take')
    # take は /tmp・TMPDIR・repo の下の保護領域を拒む。実 HOME の下の新しい 0700 のフォルダだけを使い、最後に消す。
    with tempfile.TemporaryDirectory(prefix='r4d3-take-', dir=Path.home()) as protected:
        protected = Path(os.path.realpath(protected))
        os.chmod(protected, 0o700)
        for kind in TAKE_REPOS:
            started = time.monotonic()
            cwd, files = take_records(work, protected, kind)
            data = {'repo': kind, 'manifest': files[b'manifest.tsv'].hex(),
                    'meta': files[b'snapshot/4-meta.txt'].hex(), 'contexts': files[b'snapshot/config-contexts.txt'].hex(),
                    'origins': files[b'snapshot/config-origins.txt'].hex(), 'cwd': cwd.hex(), 'context_id': '0',
                    'state': os.path.join(os.fsencode(builder.root), b'guard-r4d3').hex()}
            builder.add('T', 'take', data, kind, {'files': files})
            print('R4D-3 生成: T(%s)の take と記録の読み取り %.1f 秒' % (kind, time.monotonic() - started),
                  file=sys.stderr)


def generate(destination, scope='full'):
    started = time.monotonic()
    check_prerequisites()
    destination = Path(os.path.realpath(destination))
    if not destination.is_dir() or any(destination.iterdir()):
        raise CheckError('--generate の出力先は空のフォルダでなければならない: %s' % destination)
    shortages = take_shortages() if scope == 'full' else []
    builder = Builder(destination)
    for add in (add_f, add_l, add_m, add_c, add_o, add_a, add_p, add_v, add_k, add_r, add_h, add_q):
        add(builder)
    legacy_seconds = time.monotonic() - started
    if scope == 'full':
        if shortages:
            builder.not_created.append('T(%s・%s)' % (sys.platform, '・'.join(shortages)))
        else:
            add_t(builder)
    cases = {'version': 1, 'scope': scope, 'platform': sys.platform, 'cases': builder.cases, 'tags': builder.tags,
             'not_created': builder.not_created, 'legacy_runs': builder.runs,
             'generation_seconds': time.monotonic() - started}
    print('R4D-3 生成: 入力 %d 件・旧版の実行 %d 回・旧版(T を除く)%.1f 秒・全体 %.1f 秒' % (
        len(builder.cases), builder.runs, legacy_seconds, cases['generation_seconds']), file=sys.stderr)
    path = destination / 'cases.json'
    path.write_text(json.dumps(cases, ensure_ascii=True, separators=(',', ':')))
    return path


# ---------------------------------------------------------------- 比較

def summarize(item):
    text = json.dumps(item, ensure_ascii=True)
    return text if len(text) <= 300 else text[:300] + '...(%d 文字)' % len(text)


def comparable(fn, output):
    """比べる形。parse_records と parse_origins は予算の確かめの回数を外す。take は 3 つの関数の結果を比べる。"""
    if not isinstance(output, dict):
        return ['invalid', output]
    if fn == 'take':
        result = output.get('result')
        if isinstance(result, list) and len(result) == 2 and result[0] == 'ok' and isinstance(result[1], dict):
            parts = {key: comparable(name, result[1].get(key)) for key, name in
                     (('manifest', 'check_manifest'), ('records', 'parse_records'), ('origins', 'parse_origins'))}
            return {'result': ['ok', parts], 'checks': output.get('checks'), 'calls': output.get('calls')}
    if fn in UNCOUNTED_FUNCTIONS:
        return {'result': output.get('result'), 'calls': output.get('calls')}
    return {'result': output.get('result'), 'checks': output.get('checks'), 'calls': output.get('calls')}


def compare_results(cases, rust):
    """全件の結果を比べ、不一致の説明の一覧・比べた結果の数・欠けた結果の数を返す。"""
    mismatches = []
    compared = 0
    missing = 0
    results = rust.get('results') if isinstance(rust, dict) else None
    results = results if isinstance(results, dict) else {}
    for case in cases['cases']:
        got = results.get(case['id'])
        if not isinstance(got, list) or len(got) != len(case['conds']):
            missing += len(case['conds'])
            mismatches.append('%s: Rust の結果が無いか、条件の数が違う' % case['id'])
            continue
        for cond, want, other in zip(case['conds'], case['legacy'], got):
            compared += 1
            if comparable(case['fn'], want) != comparable(case['fn'], other):
                mismatches.append('%s(%s)条件 %s: 旧版 %s / Rust %s' % (
                    case['id'], case['fn'], cond, summarize(comparable(case['fn'], want)),
                    summarize(comparable(case['fn'], other))))
    stray = set(results) - {case['id'] for case in cases['cases']}
    if stray:
        mismatches.append('cases.json に無い入力の Rust の結果がある: %s' % sorted(stray)[:3])
    return mismatches, compared, missing


def count_inputs(cases):
    counts = {group: 0 for group in GROUPS}
    for case in cases['cases']:
        counts[case['group']] = counts.get(case['group'], 0) + 1
    return counts


def required_counts(cases):
    required = dict(MINIMUM)
    if cases['scope'] == 'mini' or cases['not_created']:
        # 自己検査の小さい組は T を作らない。Mac で道具が足りないときは、T の 4 つをまとめて外す。
        required['T'] = 0
    return required


def complete_budget_conditions(case):
    """予算を使う組の入力で、通常の回数 k に対して N=1〜k+1 の全部と停止の印がそろっているか。"""
    if not case['legacy'] or case['conds'][:1] != [['normal']]:
        return False
    k = case['legacy'][0].get('checks')
    if not isinstance(k, int) or k < 0:
        return False
    return case['conds'] == [['normal']] + [['n', n] for n in range(1, k + 2)] + [['stop']]


def check_cases(cases):
    """件数・名前付きの入力・予算の条件・旧版の結果の形を確かめる。(問題の一覧, 条件のそろった入力の数) を返す。"""
    problems = []
    if cases.get('version') != 1:
        problems.append('cases.json の版が 1 でない')
    if cases['scope'] != 'full' and not os.environ.get(ALLOW_MINI_ENV):
        problems.append('cases.json の範囲が full でない(%s)' % cases['scope'])
    if cases['not_created'] and cases['platform'] != 'darwin':
        problems.append('Mac でないのに、作れなかった入力がある: %s' % cases['not_created'])
    counts = count_inputs(cases)
    for group, minimum in required_counts(cases).items():
        if counts.get(group, 0) < minimum:
            problems.append('件数が下限に足りない: %s=%d(下限 %d)' % (group, counts.get(group, 0), minimum))
    for group, tags in REQUIRED_TAGS.items():
        lacking = [tag for tag in tags if tag not in cases['tags'].get(group, [])]
        if lacking:
            problems.append('名前付きの入力が無い: %s %s' % (group, lacking))
    complete = {group: 0 for group in BUDGET_GROUPS}
    for case in cases['cases']:
        if case['group'] in BUDGET_GROUPS:
            if complete_budget_conditions(case):
                complete[case['group']] += 1
            else:
                problems.append('予算の条件がそろっていない: %s' % case['id'])
        elif case['conds'] != [['normal']]:
            problems.append('通常の条件だけを流す組に、ほかの条件がある: %s' % case['id'])
        if len(case['legacy']) != len(case['conds']):
            problems.append('旧版の結果の数が条件の数と違う: %s' % case['id'])
        for output in case['legacy']:
            result = output.get('result') if isinstance(output, dict) else None
            if not isinstance(result, list) or not result or result[0] not in RESULT_KINDS:
                problems.append('旧版の結果の種類が想定の外: %s %s' % (case['id'], summarize(result)))
        problems.extend(legacy_facts(case))
    for group in BUDGET_GROUPS:
        if complete[group] != counts.get(group, 0):
            problems.append('予算の条件のそろった入力の数が件数と違う: %s=%d(件数 %d)' % (
                group, complete[group], counts.get(group, 0)))
    return problems, complete


def origins_count_problem(case_id, output):
    """旧版の origins は、形式の検査の後に candidate ごとに予算を確かめる(:141)。回数が candidate の数と同じか。"""
    result = output.get('result')
    if result[0] == 'ok' and output.get('checks') != len(result[1]['groups']):
        return ['旧版の origins の確かめの回数が candidate の数と違う: %s(回数 %s・candidate %d)' % (
            case_id, output.get('checks'), len(result[1]['groups']))]
    return []


def legacy_facts(case):
    problems = []
    if case['fn'] == 'parse_origins':
        problems.extend(origins_count_problem(case['id'], case['legacy'][0]))
    if case['fn'] == 'take':
        result = case['legacy'][0]['result']
        parts = result[1] if result[0] == 'ok' and isinstance(result[1], dict) else {}
        for key in ('manifest', 'records', 'origins'):
            part = parts.get(key)
            if not isinstance(part, dict) or not isinstance(part.get('result'), list) or part['result'][:1] != ['ok']:
                problems.append('T の旧版の結果が成功でない: %s %s %s' % (case['id'], key, summarize(part)))
        if isinstance(parts.get('origins'), dict) and parts['origins'].get('result', [None])[:1] == ['ok']:
            problems.extend(origins_count_problem(case['id'], parts['origins']))
    return problems


def compare_main(cases_path, rust_path, expect_mismatch):
    started = time.monotonic()
    check_prerequisites()
    cases = json.loads(Path(cases_path).read_text())
    rust = json.loads(Path(rust_path).read_text())
    if expect_mismatch:
        mismatches, compared, missing = compare_results(cases, rust)
        total = sum(len(case['conds']) for case in cases['cases'])
        if missing or compared != total:
            raise CheckError('差し込みを入れた Rust の結果がそろっていない(%d / %d)' % (compared, total))
        if not mismatches:
            raise CheckError('差し込みを入れた Rust の結果が、旧版と全件一致した。比較が働いていない')
        print('R4D-3 比較(差し込み): 不一致 %d 件・期待どおり失敗' % len(mismatches), file=sys.stderr)
        return 0
    if os.environ.get(NO_SELFTEST_ENV):
        print('R4D-3 比較器の自己検査: 飛ばした(%s が設定されている)' % NO_SELFTEST_ENV, file=sys.stderr)
    else:
        run_selftest()
    mismatches, compared, _ = compare_results(cases, rust)
    problems, complete = check_cases(cases)
    counts = count_inputs(cases)
    for group in GROUPS:
        print('R4D-3 件数: %s=%d' % (group, counts.get(group, 0)), file=sys.stderr)
    print('R4D-3 予算の条件: %s' % '・'.join('%s=%d' % (group, complete[group]) for group in BUDGET_GROUPS),
          file=sys.stderr)
    for line in cases['not_created']:
        print('R4D-3 作れなかった入力: %s' % line, file=sys.stderr)
    print('R4D-3 比べた結果: %d' % compared, file=sys.stderr)
    seconds = cases['generation_seconds'] + (time.monotonic() - started)
    print('R4D-3 の比較: 入力 %d 件・不一致 %d 件・%.1f 秒' % (len(cases['cases']), len(mismatches), seconds),
          file=sys.stderr)
    for line in mismatches[:20]:
        print('不一致: ' + line, file=sys.stderr)
    for line in problems[:40]:
        print('失敗: ' + line, file=sys.stderr)
    return 0 if not mismatches and not problems else 1


def run_selftest():
    """比較器の自己検査。失敗なら CheckError(終了値 0 にしない)。自己検査の子は、印の環境変数で再帰しない。"""
    import importlib.util
    import unittest
    path = Path(__file__).resolve().with_name('test_diagnostic_state.py')
    # scripts/test_diagnostic_state.py(旧版の検査)と同じ名前で読み込まない。
    spec = importlib.util.spec_from_file_location('r4d3_checker_selftest', path)
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
    print('R4D-3 比較器の自己検査: %d 件合格' % result.testsRun, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--generate', metavar='DIR')
    parser.add_argument('--compare', nargs=2, metavar=('CASES', 'RUST_RESULTS'))
    parser.add_argument('--expect-mismatch', action='store_true')
    parser.add_argument('--scope', choices=('full', 'mini'), default='full', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
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
