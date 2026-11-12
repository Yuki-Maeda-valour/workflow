#!/usr/bin/env python3
"""R4G専用比較。旧版(python3・bash)とRustの検査専用の入口を、同じ物理入力で比べる。

土台(単位2): 入口・本家のPythonの確かめ・決定済みの差の一覧・既存テストの包み・子の起動の記録・
strace の判定の部品と、その自己検査。
pending(単位4): 既存のCLIの24件の組・既存のmockの14件と同じ故障の組・時計の確認点と信号・
旧Pythonの入出力と起動・契約の追加のケース・pending-observation(strace・Gitの代用品・静的検査)。
reviews-dir(単位3): 道具の版・契約・信号・呼出側の GIT_CONFIG_*。
implementation-git(単位5): 既存の27件の組・対応表の29件・help と引数・契約・入出力と信号・
implementation-git-readonly・本物の期限。
"""
import argparse
import ast
import dataclasses
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from support_native import RunResult, run_command, snapshot_tree  # noqa: E402

FIXTURES = Path(__file__).with_name('r4g_fixtures')
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
OLD = {
    'pending-implementation': ('python', SCRIPTS / 'pending-implementation.py'),
    'reviews-dir': ('bash', SCRIPTS / 'reviews-dir.sh'),
    'implementation-git': ('python', SCRIPTS / 'implementation-git.py'),
}
FIXTURE = None
ENTRY_USAGE = ('r4g_contract: 検査対象のコマンドを指定してください'
               '(pending-implementation・reviews-dir・implementation-git)\n').encode()
BINARY_MAGICS = (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 検査入口と旧Python

def fixture_path(value):
    """検査入口は、実行できる通常ファイルの絶対パスで、ELFかMach-Oであること(旧版の包みを拒む)。"""
    if not value or not Path(value).is_absolute():
        raise ValueError('検査入口は絶対パスで指定してください')
    path = Path(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('検査入口は実行可能な通常ファイルである必要があります')
    with open(path, 'rb') as handle:
        head = handle.read(4)
    if head not in BINARY_MAGICS:
        raise ValueError('検査入口がRustの実行ファイルではない(旧版への誤接続)')
    return path


def verify_fixture_identity(fixture, runner=None):
    """引数なしの起動が入口の固定の文を返すこと。旧版のスクリプトやほかの実行ファイルを拒む。"""
    runner = runner or run_command
    result = runner([str(fixture)], cwd=ROOT, env=dict(os.environ), timeout=30)
    if result.timed_out or (result.returncode, result.stdout, result.stderr) != (2, b'', ENTRY_USAGE):
        raise ValueError('検査入口がR4Gの入口ではない(旧版への誤接続)')


def stdlib_expectation():
    data = json.loads((FIXTURES / 'cpython-3.10.11-stdlib.json').read_text(encoding='utf-8'))
    if data.get('version') != '3.10.11' or not data.get('files'):
        raise ValueError('標準ライブラリの固定値が不正')
    return data['files']


def stdlib_mismatches(stdlib_dir, expected):
    """名前だけを返す(中身や値は診断へ転記しない)。無いファイルも不一致に数える。"""
    bad = []
    for name, value in sorted(expected.items()):
        try:
            actual = hashlib.sha256((Path(stdlib_dir) / name).read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != value:
            bad.append(name)
    return bad


def python_requirements(*, version=None, implementation=None, euid=None, stdlib_dir=None):
    """旧版を動かすPython(この比較器を動かすPython)が本家の CPython 3.10.11 であること。"""
    version = tuple(sys.version_info[:3]) if version is None else tuple(version)
    implementation = sys.implementation.name if implementation is None else implementation
    euid = os.geteuid() if euid is None else euid
    stdlib_dir = sysconfig.get_paths()['stdlib'] if stdlib_dir is None else stdlib_dir
    if euid == 0:
        raise ValueError('R4G比較はroot以外で実行してください')
    if implementation != 'cpython' or version != (3, 10, 11):
        raise ValueError('R4G比較は本家のCPython 3.10.11で実行してください')
    bad = stdlib_mismatches(stdlib_dir, stdlib_expectation())
    if bad:
        raise ValueError('標準ライブラリが本家の3.10.11と違う: ' + ', '.join(bad))
    return stdlib_dir


# ---------------------------------------------------------------- 決定済みの差

class Difference(AssertionError):
    pass


DECISION_TIME = re.compile(r'2026-11-1[12] \d\d:\d\d')


def decision_data():
    return json.loads((FIXTURES / 'decided-differences.json').read_text(encoding='utf-8'))


def load_decisions():
    """決定済みの差。各項目は日時・出典・対象のコマンド・当ててよいケース(cases)を持つ。"""
    data = decision_data()
    decisions = {}
    for item in data['decisions']:
        if set(item) != {'id', 'decided_at', 'source', 'summary', 'commands', 'cases'}:
            raise ValueError('決定の項目が不正')
        if item['id'] in decisions or not DECISION_TIME.match(item['decided_at']):
            raise ValueError('決定の名前の重複か、日時の欠落')
        if not item['commands'] or not set(item['commands']) <= set(OLD):
            raise ValueError('決定の対象のコマンドが不正')
        cases = item['cases']
        if (not isinstance(cases, list) or len(set(cases)) != len(cases)
                or not all(isinstance(case, str) and case for case in cases)):
            raise ValueError('決定の対象のケースが不正')
        decisions[item['id']] = item
    return decisions


def load_outside_premises():
    """比較の前提の外にした条件(比べない)。決定の日時を持つ。"""
    data = decision_data()
    outside = {}
    for item in data['outside_premises']:
        if set(item) != {'id', 'decided_at', 'summary'} or item['id'] in outside:
            raise ValueError('前提の外の項目が不正')
        if not DECISION_TIME.match(item['decided_at']):
            raise ValueError('前提の外の日時の欠落')
        outside[item['id']] = item
    return outside


DECISIONS = load_decisions()
OUTSIDE_PREMISES = load_outside_premises()


def applied(command, name, case):
    """決定`name`を、コマンド`command`のケース`case`へ当ててよいこと。一覧・コマンド・ケースで照らす。"""
    if name not in DECISIONS:
        raise Difference('一覧の外の差: ' + name)
    if command not in DECISIONS[name]['commands']:
        raise Difference('この決定はこのコマンドに当たらない: ' + name)
    if case not in DECISIONS[name]['cases']:
        raise Difference('この決定はこのケースに当たらない: ' + name)


def same_result(old, new):
    if old.timed_out or new.timed_out:
        raise Difference('比較対象が時間切れになった')
    if (old.returncode, old.stdout, old.stderr) != (new.returncode, new.stdout, new.stderr):
        raise Difference('終了値または出力が一致しない')


def fixed(result, code, stdout=b'', stderr=b''):
    if result.timed_out or (result.returncode, result.stdout, result.stderr) != (code, stdout, stderr):
        raise Difference('固定期待と一致しない')


FILE_FIELD = re.compile(rb'^  File "([^"\n]*)", line \d+, in ', re.M)


def location_fields(text, *, script_path, script_name, stdlib_dir, expected):
    """呼出履歴の所在地の欄だけを固定名へ写す。`expected`は(旧スクリプト, 標準ライブラリ)の出現数。

    旧スクリプトの欄は`script_name`、標準ライブラリの欄は導入先からの相対パスへ写す。
    `<`で始まる欄はそのまま。ほかの所在地・出現数の違いは不合格。
    """
    if Path(script_path).name != script_name:
        raise Difference('旧スクリプトの呼出名が固定名と違う')
    prefix = os.fsencode(stdlib_dir).rstrip(b'/') + b'/'
    verified = set(stdlib_expectation())
    counts = [0, 0]

    def replace(match):
        path = match.group(1)
        if path == os.fsencode(script_path):
            counts[0] += 1
            new = script_name.encode()
        elif path.startswith(prefix):
            counts[1] += 1
            new = path[len(prefix):]
            if os.fsdecode(new) not in verified:
                raise Difference('SHA-256で確かめた標準ライブラリの外の所在地')
        elif path.startswith(b'<'):
            new = path
        else:
            raise Difference('対応づけない所在地の欄がある')
        return match.group(0).replace(b'"' + path + b'"', b'"' + new + b'"', 1)

    result = FILE_FIELD.sub(replace, text)
    if tuple(counts) != tuple(expected):
        raise Difference('所在地の欄の位置か出現数が違う')
    return result


THREAD = re.compile(rb'^Current thread 0x([0-9a-f]{16}) \(most recent call first\):$', re.M)


def current_thread(text):
    """Current thread の値を、1回だけ・決まった形で出ることを確かめてから伏せる。"""
    found = THREAD.findall(text)
    if len(found) != 1 or text.count(b'Current thread') != 1:
        raise Difference('Current thread の行の数か形が違う')
    return THREAD.sub(b'Current thread 0x<thread> (most recent call first):', text)


def sigint_pair(command, case, old, new):
    """SIGINT で終わり、標準出力が同じで、最後の行が KeyboardInterrupt。途中の呼出履歴は比べない。"""
    applied(command, 'sigint-traceback', case)
    for result in (old, new):
        if result.timed_out or result.returncode != -2:
            raise Difference('SIGINT で終わっていない')
        if not result.stderr.endswith(b'KeyboardInterrupt\n'):
            raise Difference('最後の行が KeyboardInterrupt でない')
        lines = result.stderr.splitlines()
        if lines[-1] != b'KeyboardInterrupt' or (len(lines) > 1 and lines[0] != b'Traceback (most recent call last):'):
            raise Difference('KeyboardInterrupt の形が違う')
    if old.stdout != new.stdout:
        raise Difference('SIGINT までの標準出力が違う')


FLOAT = rb'(?:[0-9]+\.[0-9]+(?:e-[0-9]+)?|[0-9](?:\.[0-9]+)?e-[0-9]+)'
REMAINING = re.compile(rb'timed out after (' + FLOAT + rb'|[0-9]+) seconds')


def timeout_fraction(text, limit):
    """時間切れの文の秒数が、Pythonの小数の形で 0 < 値 <= 期限 であることを確かめてから伏せる。"""
    found = REMAINING.findall(text)
    if len(found) != 1 or text.count(b'timed out after') != 1:
        raise Difference('時間切れの文の数が違う')
    value = found[0]
    if not re.fullmatch(FLOAT, value) or not 0 < float(value) <= limit:
        raise Difference('残り時間の小数が範囲か形の外')
    if repr(float(value)) != value.decode('ascii'):
        raise Difference('残り時間の小数がPythonの表記でない')
    return REMAINING.sub(b'timed out after <remaining> seconds', text)


def same_env_set(command, case, old_env, new_env):
    """子の環境の値の集合が同じこと(並びは比べない)。重複も数える。"""
    applied(command, 'child-env-order', case)
    if sorted(old_env) != sorted(new_env):
        raise Difference('子の環境の値の集合が違う')


def bash_lines_english(command, case, localized, c_locale, new, script_name):
    """翻訳のある言語の旧版と LC_ALL=C の旧版で違う行が、bash 自身の行だけであること。Rust は C の旧版と同じ。"""
    applied(command, 'bash-own-lines-english', case)
    left, right = localized.splitlines(keepends=True), c_locale.splitlines(keepends=True)
    if len(left) != len(right):
        raise Difference('翻訳で行の数が変わった')
    own = script_name.encode()
    for a, b in zip(left, right):
        if a != b and not (a.startswith(own + b': ') and b.startswith(own + b': ')):
            raise Difference('bash 自身の行ではない行が翻訳で違う')
    if new != c_locale:
        raise Difference('Rust の行が LC_ALL=C の旧版と違う')


def bash_lines_english_same_language(command, case, localized, c_locale, new, script_name):
    """翻訳のある言語の旧版と同じ言語の Rust: 旧版の行のうち bash 自身の行だけを、LC_ALL=C の旧版の同じ位置の
    行(英語)へ置き換えたものと全バイトで比べる。外部コマンドの翻訳された行はそのまま比べる。"""
    applied(command, 'bash-own-lines-english', case)
    left, right = localized.splitlines(keepends=True), c_locale.splitlines(keepends=True)
    if len(left) != len(right):
        raise Difference('翻訳で行の数が変わった')
    own = script_name.encode() + b': '
    expected = b''.join(english if line.startswith(own) else line for line, english in zip(left, right))
    if new != expected:
        raise Difference('Rust の行が、bash 自身の行だけを英語にした同じ言語の旧版と違う')


def partial_write(command, case, full, limit):
    """故障のない旧版の出力(固定名へ写した後)を、通ったバイト数で切った Rust の期待。"""
    applied(command, 'partial-write', case)
    if limit < 0:
        raise Difference('通ったバイト数が負')
    return full[:limit]


NORMALIZERS = {'location-fields', 'current-thread', 'timeout-fraction', 'per-run-values'}

JOB_PID = re.compile(rb'^(?P<head>.*?: line \d+: |     )(?P<pid> *\d+) ', re.M)


# 管理担当の確認待ちの項目(2026-11-12 単位5の指示)。決まるまでは「未決」として扱い、合格に数えない。
# 比べた結果(一致・不一致)は記録し、最後の行の前に表示する。未決が1件でも残れば、最後の合格を失敗にする
# (既定。--allow-undecided のときだけ、記録して合格の行を出す)。
# 2026-11-12 06:58 の決定(rust-decision-runtime-env.txt【追加の決定 2】)で、job-pid・bash-special-vars・
# hup-comsub-race の3件は決定済みの差(job-pid-sequence・time-valued-env・race-unordered-outcome)へ移した。
UNDECIDED_ITEMS = {}
UNDECIDED = []

# この機械では流せない組(未実施)。合格の件数に数えず、最後の行に別の件数で出す(pending-io-linux と同じ扱い)。
# 要素は (テストの名前, 一部の組の名前か None, 理由, 合格の件数に入った後か)。一部の組(None でない)は、テストの
# ほかの組が流れて合格に数えられているので、件数から引かない。テスト全体(None)は引く。
NOT_RUN = []


def not_run(test, part, reason):
    """テスト(全体か一部の組)を、この機械では未実施として記録する。テスト全体のときは、呼んだ後に return する。"""
    NOT_RUN.append((test.id(), part, reason, True))


# --require-userns(Linux の CI): /proc を隠す組(利用者とマウントの名前空間)を作れなければ、未実施ではなく失敗にする。
REQUIRE_USERNS = False


def userns_unavailable(test, reason):
    """名前空間を作れなかったとき: --require-userns なら失敗にし、そうでなければ未実施と記録する。"""
    if REQUIRE_USERNS:
        raise Difference(f'--require-userns を指定したが、名前空間を作れない: {reason}')
    not_run(test, None, f'この機械では名前空間を作れない: {reason}')


def not_run_section(name, case, reason):
    """節を流さないとき、その節のテストを1件ずつ未実施として記録する(合格の件数には入っていない)。"""
    for method in unittest.TestLoader().getTestCaseNames(case):
        NOT_RUN.append((f'{name}.{method}', None, reason, False))


def final_summary(count, records, control):
    """未実施の行と最後の行。合格の件数から、流れた後に未実施と記録したテスト全体の数を引く。"""
    whole = {record[0] for record in records if record[1] is None and record[3]}
    lines = [f'R4G: 未実施: {test_id}{"(" + part + ")" if part else ""}: {reason}'
             for test_id, part, reason, _ in records]
    lines.append(f'R4G: 合格({count - len(whole)} 件・未実施 {len(records)} 件・{control})')
    return lines


def undecided(item, label, check):
    """未決の項目を比べ、結果だけを記録する(不一致でも不合格にせず、一致しても合格に数えない)。"""
    if item not in UNDECIDED_ITEMS:
        raise ValueError('未決の項目の一覧に無い: ' + item)
    try:
        check()
    except Difference as error:
        UNDECIDED.append((item, label, f'不一致({error})'))
    else:
        UNDECIDED.append((item, label, '一致'))


def undecided_verdict(records, allow):
    """最後の合格の前の確かめ: 未決の記録が残っていれば、表示する行と、失敗にするかを返す。"""
    lines = [f'R4G: 未決の結果: {item} {label}: {outcome}' for item, label, outcome in records]
    if records:
        lines.append('R4G: 未決(管理担当の確認待ち。合格に数えない): ' + undecided_summary(records))
    else:
        lines.append('R4G: 未決: 0 件')
    return lines, bool(records) and not allow


def undecided_summary(records):
    """未決の項目ごとの件数(一致・不一致)の1行。"""
    parts = []
    for item in UNDECIDED_ITEMS:
        rows = [row for row in records if row[0] == item]
        if rows:
            same = sum(1 for row in rows if row[2] == '一致')
            parts.append(f'{item} {len(rows)} 件(一致 {same}・不一致 {len(rows) - same})')
    return '・'.join(parts)


def per_run_values(text):
    """旧bashが子の終わり方を知らせる行の番号(pid。実行ごとに変わる)だけを伏せる。行の形は全部比べる。"""
    return JOB_PID.sub(lambda match: match.group('head') + b'<pid>'.rjust(len(match.group('pid'))) + b' ', text)


def compare(command, old, new, *, decisions=(), case=None, **params):
    """一覧の決定だけを当ててから全バイトで比べる。一覧の外・対象のコマンドやケースの外の決定は不合格。"""
    for name in decisions:
        applied(command, name, case)
        if name not in NORMALIZERS:
            raise Difference('この決定は compare では当てない(専用の確かめを使う): ' + name)
    old_err, new_err = old.stderr, new.stderr
    if 'location-fields' in decisions:
        old_err = location_fields(old_err, **params['location'])
    if 'current-thread' in decisions:
        old_err, new_err = current_thread(old_err), current_thread(new_err)
    if 'per-run-values' in decisions:
        old_err, new_err = per_run_values(old_err), per_run_values(new_err)
    if 'timeout-fraction' in decisions:
        old_err = timeout_fraction(old_err, params['limit'])
        new_err = timeout_fraction(new_err, params['limit'])
    same_result(dataclasses.replace(old, stderr=old_err), dataclasses.replace(new, stderr=new_err))


# ---------------------------------------------------------------- 起動と入力の無変更

def legacy_argv(command, args):
    kind, script = OLD[command]
    return ([sys.executable, str(script)] if kind == 'python' else ['bash', str(script)]) + list(args)


def native_argv(command, args, entry=()):
    if FIXTURE is None:
        raise ValueError('検査入口が未設定')
    return [str(FIXTURE), *entry, command, *args]


def observation(path):
    """内容の記録に、atime を除く物理情報を足す。"""
    path = Path(path)
    result = snapshot_tree(path)
    for relative, value in result.items():
        st = (path / relative).lstat()
        value['physical'] = (st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino, st.st_size)
    return result


def unchanged(before, after):
    if before != after:
        raise Difference('比較中に入力が変わった')


def pair(command, args, env, roots=(), *, cwd=ROOT, entry=(), decisions=(), timeout=60, **params):
    before = [observation(root) for root in roots]
    old = run_command(legacy_argv(command, args), cwd=cwd, env=env, timeout=timeout)
    unchanged(before, [observation(root) for root in roots])
    new = run_command(native_argv(command, args, entry), cwd=cwd, env=env, timeout=timeout)
    unchanged(before, [observation(root) for root in roots])
    compare(command, old, new, decisions=decisions, **params)
    return old, new


def run_suite(suite):
    count = suite.countTestCases()
    if count == 0:
        raise AssertionError('検査ケースが0件')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if (not result.wasSuccessful() or result.testsRun != count or result.skipped
            or result.expectedFailures or result.unexpectedSuccesses):
        raise AssertionError('未実行・失敗・省略・失敗を期待したケースがある')
    return count


# ---------------------------------------------------------------- 既存テストの包み

def existing_manifest():
    return json.loads((FIXTURES / 'existing-tests.json').read_text(encoding='utf-8'))


def existing_suite(wrappers, *, manifest=None, root=ROOT, classes=None):
    """既存テストのクラスを継承し、CLIの起動だけを旧版とRustの組へ置き換えた包みを集める。

    `wrappers`は {(ファイル, クラス): 包みのクラスを作る関数(元のクラス, モジュール)}。
    `classes`を渡さなければ固定一覧の全クラスに包みが要る。渡せば、その組(固定一覧にあるもの)に
    ちょうど包みが要り、合計はその組の件数の和と比べる。名前・件数・欠落・余分を拒む。
    """
    manifest = manifest or existing_manifest()
    loader = unittest.TestLoader()
    suites = []
    total = 0
    every = {(file, name) for file, item in manifest['files'].items() for name in item['classes']}
    expected_keys = every if classes is None else set(classes)
    if not expected_keys <= every or set(wrappers) != expected_keys:
        raise AssertionError('既存テストの包みに欠落か余分がある')
    expected_total = manifest['total'] if classes is None else sum(
        manifest['files'][file]['classes'][name]['count'] for file, name in expected_keys)
    modules = {}
    for (file, name), make in sorted(wrappers.items()):
        if file not in modules:
            path = root / 'scripts' / file
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][file]['sha256']:
                raise AssertionError('既存テストのファイルが固定一覧の時点と違う')
            modules[file] = load(path, 'r4g_existing_' + file[:-3])
        original = getattr(modules[file], name)
        fixed_item = manifest['files'][file]['classes'][name]
        names = loader.getTestCaseNames(original)
        if (len(names), hashlib.sha256('\n'.join(names).encode()).hexdigest()) != (fixed_item['count'], fixed_item['sha256']):
            raise AssertionError('既存ケースの固定一覧と一致しない')
        wrapped = make(original, modules[file])
        if not issubclass(wrapped, original) or loader.getTestCaseNames(wrapped) != names:
            raise AssertionError('既存ケースの収集が欠落した')
        suite = loader.loadTestsFromTestCase(wrapped)
        if suite.countTestCases() != len(names):
            raise AssertionError('既存ケースの件数が違う')
        total += len(names)
        suites.append(suite)
    if total != expected_total:
        raise AssertionError('既存ケースの合計が固定一覧と違う')
    return unittest.TestSuite(suites)


# ---------------------------------------------------------------- 子の起動の記録

# R4G比較の代用品の本体(sh の包みから2回起動する)。
#   record: 包み(bash)のプロセスの状態を記録する。生の環境(並び・重複を含む。Linux は /proc/<pid>/environ、
#           Mac は sysctl の KERN_PROCARGS2)・実行したパス・引数・作業場所・継いだ信号の扱い(SigIgn・SigBlk)・
#           開いている fd。包みは信号の扱いも mask も fd も変えないので、親が渡したものが見える。
#   exec:   記録した生の環境で本物の道具を execve する。Python が起動で無視にした SIGPIPE・SIGXFSZ は、
#           包みが継いだ扱いへ戻してから渡す(代用品が子の信号の扱いを変えない)。
STUB_HELPER = r'''import ctypes, json, os, signal, sys


def proc_environ(pid):
    if sys.platform.startswith('linux'):
        with open('/proc/%d/environ' % pid, 'rb') as handle:
            data = handle.read()
        return data.split(b'\0')[:-1] if data else []
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'sysctl')
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'sysctl')
    data = buffer.raw[:size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    rest = rest[rest.index(b'\0'):].lstrip(b'\0')
    env = []
    for item in rest.split(b'\0')[argc:]:
        if not item:
            break
        env.append(item)
    return env


def status_fields(pid):
    fields = {}
    with open('/proc/%s/status' % pid) as handle:
        for line in handle:
            key, _, value = line.partition(':')
            fields[key] = value.strip()
    return fields


def proc_state(pid):
    # 継いだ信号の扱いと開いている fd。読めない機械(Mac の fd など)は None。
    # 包み(bash)は自分の SIGQUIT を無視するが、子には起動時の扱いへ戻して渡し、mask はそのまま渡す。
    # そこで、無視の集合と mask はこの子(記録の Python)のものを読み、Python が起動で無視にする
    # SIGPIPE・SIGXFSZ の2つだけは包みのものを読む。fd は包みのもの。
    state = {'SigIgn': None, 'SigBlk': None, 'fds': None}
    if sys.platform.startswith('linux'):
        own, wrapper = status_fields('self'), status_fields(pid)
        python_bits = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))
        ignored = (int(own['SigIgn'], 16) & ~python_bits) | (int(wrapper['SigIgn'], 16) & python_bits)
        state['SigIgn'] = '%016x' % ignored
        state['SigBlk'] = own['SigBlk']
        fds = []
        for name in sorted(os.listdir('/proc/%d/fd' % pid), key=int):
            target = os.readlink('/proc/%d/fd/%s' % (pid, name))
            kind = target.split(':', 1)[0] if target.startswith(('pipe:', 'socket:', 'anon_inode:')) else target
            fds.append([int(name), kind])
        state['fds'] = fds
    else:
        import subprocess
        try:
            out = subprocess.run(['ps', '-o', 'sigignore=', '-o', 'sigmask=', '-p', str(pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout.split()
            state['SigIgn'], state['SigBlk'] = out[0].decode(), out[1].decode()
        except (OSError, subprocess.CalledProcessError, IndexError):
            pass
    return state


def parent_kind(pid):
    # 呼び出したプロセスが shell(bash・sh)か(比べない。旧Pythonの直接の子と bash の孫を分けるため)。
    if not sys.platform.startswith('linux'):
        return None
    try:
        with open('/proc/%d/stat' % pid) as handle:
            ppid = int(handle.read().rsplit(')', 1)[1].split()[1])
        with open('/proc/%d/comm' % ppid) as handle:
            return 'shell' if handle.read().strip() in ('bash', 'sh', 'dash') else 'other'
    except (OSError, ValueError, IndexError):
        return None


def main():
    mode, log, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
    keep = '%s.keep.%d' % (log, pid)
    if mode == 'record':
        argv0, args = sys.argv[4], sys.argv[5:]
        environ = proc_environ(pid)
        state = proc_state(pid)
        entry = [os.fsencode(argv0).hex(), [os.fsencode(a).hex() for a in args],
                 [item.hex() for item in environ], os.fsencode(os.getcwd()).hex(), state, parent_kind(pid)]
        with open(log, 'a') as handle:
            handle.write(json.dumps(entry) + '\n')
        with open(keep, 'w') as handle:
            json.dump([[item.hex() for item in environ], state], handle)
        return 0
    real, code, fixed, args = sys.argv[4], int(sys.argv[5]), sys.argv[6] == '1', sys.argv[7:]
    with open(keep) as handle:
        hexed, state = json.load(handle)
    os.unlink(keep)
    environ = [bytes.fromhex(item) for item in hexed]
    directory = bool(args) and args[0] == '-d'
    rest = args[1:] if directory else args
    if fixed and len(rest) <= 1 and (rest or directory):
        # mktemp の代用品: 型板1つ(か、-d だけ)なら、乱数の代わりに決まった名前で作る。
        if rest:
            template = rest[0]
        else:
            tmpdir = next((item[7:] for item in environ if item.startswith(b'TMPDIR=')), b'') or b'/tmp'
            template = os.fsdecode(tmpdir).rstrip('/') + '/tmp.XXXXXXXXXX'
        stem = template.rstrip('X')
        count = len(template) - len(stem)
        if count >= 3:
            name = stem + ('r4gfix' * 2)[:count]
            try:
                if directory:
                    os.mkdir(name, 0o700)
                else:
                    os.close(os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
                sys.stdout.write(name + '\n')
                return 0
            except FileExistsError:
                # 決まった名前が既にある: 本物に任せると乱数の名前になるので、その印を残す(比較器が不合格にする)。
                with open(log + '.random', 'a') as handle:
                    handle.write(name + '\n')
            except OSError:
                pass
    if not real:
        return code
    ignored = int(state['SigIgn'], 16) if state.get('SigIgn') else 0
    for number in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(number, signal.SIG_IGN if ignored & (1 << (number - 1)) else signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    argv = [os.fsencode(real)] + [os.fsencode(a) for a in args]
    argv_array = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
    env_array = (ctypes.c_char_p * (len(environ) + 1))(*environ, None)
    libc.execve(os.fsencode(real), argv_array, env_array)
    return 126


sys.exit(main())
'''

STUB_WRAPPER = '''#!/bin/bash
{python} -I {helper} record {log} $$ "$0" "$@" || exit 125
exec {python} -I {helper} exec {log} $$ {real} {code} {fixed} "$@"
'''


def write_stub(directory, name, log, *, real=None, code=0, fixed_mktemp=False):
    """呼出(実行したパス・引数・生の環境・作業場所・継いだ信号の扱い・開いた fd)を記録し、本物の道具が
    あれば同じ引数と生の環境で exec する代用品(bash の包み。dash は起動時に mask を消すので使わない)。"""
    import shlex
    helper = Path(directory) / '.r4g-stub.py'
    if not helper.exists():
        helper.write_text(STUB_HELPER)
    path = Path(directory) / name
    path.write_text(STUB_WRAPPER.format(python=shlex.quote(sys.executable), helper=shlex.quote(str(helper)),
                                        log=shlex.quote(str(log)), real=shlex.quote(str(real) if real else ''),
                                        code=int(code), fixed='1' if fixed_mktemp else '0'))
    path.chmod(0o755)
    return path


def check_fixed_mktemp(log):
    """mktemp の代用品が決まった名前を作れず、本物(乱数の名前)に任せた印があれば不合格にする。"""
    marker = Path(str(log) + '.random')
    if marker.exists():
        names = marker.read_text(errors='replace').split()
        marker.unlink()
        raise Difference('mktemp の代用品が決まった名前を作れず、乱数の名前になった: ' + ', '.join(names[:3]))


def read_calls(log):
    check_fixed_mktemp(log)
    path = Path(log)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


RUNTIME_ADDED_ENV = (b'__CF_USER_TEXT_ENCODING',)


def runtime_added(command, case, old, new):
    """旧版の実行環境やOSの部品(Mac の旧Pythonの CoreFoundation など)が旧版の子の環境へ自分で足す変数を、
    子の環境の比較から外し、値と有無を記録する(2026-11-12 02:41 の決定)。ほかの変数は全部比べる。"""
    def strip(calls, side):
        out = []
        for call in calls:
            kept = []
            for item in call[2]:
                raw = bytes.fromhex(item)
                if raw.split(b'=', 1)[0] in RUNTIME_ADDED_ENV:
                    applied(command, 'runtime-added-child-env', case)
                    OBSERVATIONS.append(f'{command}: {side}の子の環境の {raw.decode(errors="backslashreplace")} '
                                        '(比較から外した。2026-11-12 02:41)')
                    continue
                kept.append(item)
            out.append([call[0], call[1], kept, call[3], *call[4:]])
        return out
    return strip(old, '旧版'), strip(new, 'Rust')


def same_calls(command, old, new, *, case=None, env_as_set=False):
    """子の起動の列(順・実行したパス・引数・作業場所・生の環境)が同じこと。

    環境の並びを比べないのは、決定 child-env-order をそのコマンドのケースへ当てたときだけ。
    旧版の実行環境が自分で足す変数(RUNTIME_ADDED_ENV)だけは、決定 runtime-added-child-env で外して記録する。
    """
    if len(old) != len(new):
        raise Difference('子の起動の数が違う')
    old, new = runtime_added(command, case, old, new)
    for left, right in zip(old, new):
        if left[0] != right[0] or left[1] != right[1] or left[3] != right[3]:
            raise Difference('子の起動のパス・引数・作業場所が違う')
        if left[4:5] != right[4:5]:
            raise Difference('子が継いだ信号の扱いか、開いている fd が違う')
        if env_as_set:
            same_env_set(command, case, left[2], right[2])
        elif left[2] != right[2]:
            raise Difference('子の環境が違う')


# ---------------------------------------------------------------- 道具の版と資源の上限

TOOL_BASELINE = {'bash': '5.2.21', 'coreutils': '9.4', 'findutils': '4.9.0', 'sed': '4.9'}
TOOL_PROBES = {'bash': (['bash', '--version'], rb'^GNU bash, version (\d+\.\d+\.\d+)\('),
               'coreutils': (['cat', '--version'], rb'^cat \(GNU coreutils\) (\d+(?:\.\d+)+)$'),
               'findutils': (['find', '--version'], rb'^find \(GNU findutils\) (\d+(?:\.\d+)+)$'),
               'sed': (['sed', '--version'], rb'^sed \(GNU sed\) (\d+(?:\.\d+)+)$')}


def tool_versions(runner=None):
    """PATHの道具の版。GNUでない・読めない道具は None。"""
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, env=dict(os.environ, LC_ALL='C')))
    versions = {}
    for name, (argv, pattern) in TOOL_PROBES.items():
        try:
            output = runner(argv).stdout
        except OSError:
            versions[name] = None
            continue
        match = re.match(pattern, output.split(b'\n', 1)[0])
        versions[name] = match.group(1).decode() if match else None
    return versions


# 道具の版の基準を要る節(2026-11-12 00:35 の決定の cases)。
REVIEWS_SECTIONS = ('reviews-tools', 'reviews-contract', 'reviews-signals', 'reviews-dir-filter-env')


def tool_baseline_matches(versions):
    return all(versions.get(name) == value for name, value in TOOL_BASELINE.items())


def require_tool_baseline(versions):
    """道具の版が基準(2026-11-12 00:35 の決定。CIのUbuntu 24.04)と同じこと。違えば比較を失敗にする。"""
    different = sorted(name for name, value in TOOL_BASELINE.items() if versions.get(name) != value)
    if different:
        raise Difference('道具の版が基準と違う: ' + ', '.join(
            f'{name}={versions.get(name)}(基準 {TOOL_BASELINE[name]})' for name in different))


RLIMIT_NAMES = ('RLIMIT_AS', 'RLIMIT_NOFILE', 'RLIMIT_CPU', 'RLIMIT_STACK', 'RLIMIT_DATA', 'RLIMIT_FSIZE')


def inherited_limits():
    """受け継いだ資源の上限(2026-11-12 00:42 の決定。比較器は設定せず、記録だけする)。"""
    import resource
    limits = {}
    for name in RLIMIT_NAMES:
        if hasattr(resource, name):
            soft, hard = resource.getrlimit(getattr(resource, name))
            limits[name] = tuple('unlimited' if value == resource.RLIM_INFINITY else value for value in (soft, hard))
    return limits


# ---------------------------------------------------------------- strace の判定

STRACE_CALLS = 'trace=execve,execveat,?open,openat,openat2'


def strace_command(argv, prefix, env):
    """strace 自身だけを LC_ALL=C にする(記録の strerror を英語の ASCII にする)。追跡される側の環境は
    `-E` で元のまま渡す。戻り値は (argv, strace の環境)。"""
    tracee = ['-E', 'LC_ALL=' + env['LC_ALL']] if 'LC_ALL' in env else ['-E', 'LC_ALL']
    return (['strace', '-ff', '-y', '-xx', '-qq', '-e', STRACE_CALLS, *tracee, '-o', str(prefix), *argv],
            dict(env, LC_ALL='C'))


ESCAPE = re.compile(r'\\x([0-9a-f]{2})|\\(.)')


def decode_strace(text):
    """-xx の文字列と注釈を元のバイト列へ戻す。16進でないエスケープは \\n などだけを受ける。"""
    simple = {'n': b'\n', 't': b'\t', 'r': b'\r', 'v': b'\v', 'f': b'\f', '"': b'"', '\\': b'\\'}
    out = bytearray()
    position = 0
    for match in ESCAPE.finditer(text):
        literal = text[position:match.start()]
        if any(ord(ch) > 127 for ch in literal):
            raise ValueError('strace の記録を戻せない')
        out += literal.encode('ascii')
        if match.group(1):
            out.append(int(match.group(1), 16))
        elif match.group(2) in simple:
            out += simple[match.group(2)]
        else:
            raise ValueError('strace の記録を戻せない')
        position = match.end()
    tail = text[position:]
    if any(ord(ch) > 127 for ch in tail):
        raise ValueError('strace の記録を戻せない')
    out += tail.encode('ascii')
    return bytes(out)


@dataclasses.dataclass(frozen=True)
class Event:
    pid: int
    call: str
    args: tuple
    result: str


def split_args(text):
    """引数を、引用符・<注釈>・[]・{} の中のカンマで切らずに分ける。"""
    parts, depth, quoted, current, index = [], 0, False, [], 0
    while index < len(text):
        ch = text[index]
        if quoted:
            current.append(ch)
            if ch == '\\':
                current.append(text[index + 1])
                index += 1
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
            current.append(ch)
        elif ch in '<[{':
            depth += 1
            current.append(ch)
        elif ch in '>]}':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
        index += 1
    if current or parts:
        parts.append(''.join(current).strip())
    return tuple(parts)


LINE = re.compile(r'^(?P<call>[a-z0-9_]+)\((?P<args>.*)\) += (?P<result>.*)$')


def parse_strace(files):
    """strace -ff の各ファイル(名前の末尾が pid)を読み、対象の呼出しの並びを返す。読めない行は失敗。"""
    events = []
    for path in sorted(files):
        pid = int(str(path).rsplit('.', 1)[1])
        for line in Path(path).read_text(encoding='ascii').splitlines():
            if line.startswith(('---', '+++')) or not line.strip():
                continue
            match = LINE.match(line)
            if not match:
                raise ValueError('strace の行を読めない')
            events.append(Event(pid, match['call'], split_args(match['args']), match['result']))
    return events


def string_arg(text):
    if not (text.startswith('"') and text.endswith('"')):
        raise ValueError('strace の文字列の引数ではない')
    return decode_strace(text[1:-1])


ANNOTATION = re.compile(r'^(?:-?\d+|AT_FDCWD)<(.*)>$')


def annotation(text):
    match = ANNOTATION.match(text)
    return decode_strace(match.group(1)) if match else None


def under(path, candidate):
    return path == candidate or path.startswith(candidate.rstrip(b'/') + b'/')


def normalized(path):
    """`.`と`..`と重なった`/`を字面で畳む(リンクは辿らない)。絶対パスだけを受ける。"""
    if not path.startswith(b'/'):
        raise ValueError('strace の記録の相対パスを基準へ結べない')
    parts = []
    for part in path.split(b'/'):
        if part in (b'', b'.'):
            continue
        if part == b'..':
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return b'/' + b'/'.join(parts)


def opened_candidates(events, candidates, cwd=None):
    """候補とその下を開いた呼出しを数える。

    見る所: 絶対パス・基準のfdの注釈か作業場所(`cwd`。`open`と注釈の無い`AT_FDCWD`)と名前を結んだパス・
    返ったfdの注釈。パスは字面で`..`を畳む。どれかのパスが候補かその下に当たるか、どれかの要素が
    候補の名前と一致すれば違反。相対の名前を基準へ結べない記録は失敗にする。
    """
    violations = []
    names = {os.path.basename(candidate) for candidate in candidates}
    base_cwd = None if cwd is None else os.fsencode(cwd)
    for event in events:
        if event.call not in ('open', 'openat', 'openat2'):
            continue
        if event.call == 'open':
            base, name = base_cwd, string_arg(event.args[0])
        else:
            base = annotation(event.args[0])
            if base is None and event.args[0] == 'AT_FDCWD':
                base = base_cwd
            name = string_arg(event.args[1])
        if name.startswith(b'/'):
            paths = [normalized(name)]
        elif base is None:
            raise ValueError('strace の記録の相対パスを基準へ結べない')
        else:
            paths = [normalized(base.rstrip(b'/') + b'/' + name)]
        result = annotation(event.result.split(' ', 1)[0])
        if result is not None:
            paths.append(normalized(result))
        hit = any(under(path, candidate) for path in paths for candidate in candidates)
        named = any(part in names for path in paths + [name] for part in path.split(b'/'))
        if hit or named:
            violations.append(event)
    return violations


def executions(events):
    """execve と execveat の対象のパス(Gitを起動しないことの確かめに使う)。"""
    paths = []
    for event in events:
        if event.call == 'execve':
            paths.append(string_arg(event.args[0]))
        elif event.call == 'execveat':
            paths.append(string_arg(event.args[1]))
    return paths


def strace_usable():
    if not sys.platform.startswith('linux') or shutil.which('strace') is None:
        return False
    with tempfile.TemporaryDirectory() as temp:
        argv, env = strace_command(['/bin/true'], Path(temp) / 'probe', dict(os.environ))
        result = subprocess.run(argv, env=env, capture_output=True)
        return result.returncode == 0


# ---------------------------------------------------------------- pending の共通部品

PENDING = 'pending-implementation'
PENDING_SCRIPT = OLD[PENDING][1]
PENDING_WRAPPER = FIXTURES / 'pending_wrapper.py'
PENDING_TESTS = 'test_pending_implementation.py'
CORRESPONDENCE = FIXTURES / 'pending-correspondence.json'
FUNCTION_CASES = FIXTURES / 'pending-function-cases.json'
CORRESPONDENCE_KINDS = {'cli-pair': 24, 'injection-pair': 14, 'rust-unit': 4, 'legacy-only': 5}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def pending_env(**changes):
    """比較の環境: 今の環境から PYTHON* と今回の範囲を変える GIT_* を外し、`changes`を当てる(Noneは外す)。"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('PYTHON') and key not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR')}
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def tree_state(*paths):
    """入力の無変更の記録(atimeを除く番号・種類・権限・持ち主・大きさ・時刻)。読めないフォルダは葉にする。"""
    state = {}
    for base in paths:
        base = os.fsencode(base)
        if not os.path.lexists(base):
            state[base] = None
            continue
        stack = [base]
        while stack:
            path = stack.pop()
            info = os.lstat(path)
            state[path] = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns)
            if stat_module.S_ISDIR(info.st_mode):
                try:
                    names = os.listdir(path)
                except OSError:
                    continue
                stack.extend(os.path.join(path, name) for name in names)
    return state


def normalized_trace(lines):
    """呼出しの記録のfdの番号を、開いた順の名前へ写す。stat の結果は番号・種類・持ち主だけを残す
    (祖先のフォルダの時刻は、ほかのプロセスでも変わるため。候補の値はJSONの比較で見る)。"""
    labels = {}
    opened = 0
    out = []

    def name(fd):
        return None if fd is None else labels.get(fd, 'raw%d' % fd)

    def reduced(result):
        if result[0] == 'ok' and isinstance(result[1], list):
            dev, ino, mode, uid = result[1][:4]
            return ['ok', [dev, ino, mode & 0o170000, uid]]
        return result

    for line in lines:
        entry = json.loads(line)
        op = entry[0]
        if op == 'open':
            entry = [op, name(entry[1]), entry[2], entry[3], entry[4]]
            if entry[4][0] == 'ok':
                opened += 1
                labels[entry[4][1]] = 'fd%d' % opened
                entry[4] = ['ok', labels[entry[4][1]]]
        elif op == 'stat':
            entry = [op, name(entry[1]), entry[2], reduced(entry[3])]
        elif op == 'fstat':
            entry = [op, name(entry[1]), reduced(entry[2])]
        elif op in ('close', 'read', 'scandir'):
            entry = [op, name(entry[1]), *entry[2:]]
        out.append(entry)
    return out


def same_trace(old_lines, new_lines):
    old, new = normalized_trace(old_lines), normalized_trace(new_lines)
    if not old:
        raise Difference('呼出しの記録が空(旧版の包みが当たっていない)')
    if old != new:
        for index, (left, right) in enumerate(zip(old, new)):
            if left != right:
                raise Difference(f'OSの呼出しの順か回数が違う(#{index}: 旧版 {left} / Rust {right})')
        raise Difference(f'OSの呼出しの回数が違う(旧版 {len(old)} / Rust {len(new)})')
    return old


def read_lines(path):
    path = Path(path)
    return path.read_text(encoding='ascii').splitlines() if path.exists() else []


def pending_plain(args, env, cwd, watch=(), *, old_flags=(), decisions=(), case=None, timeout=60, **params):
    """旧版(`python3 <旧スクリプト>`)とRustを、同じ物理入力で順に流して全バイトで比べる。"""
    before = tree_state(*watch)
    old = run_command([sys.executable, *old_flags, str(PENDING_SCRIPT), *args], cwd=cwd, env=env, timeout=timeout)
    unchanged(before, tree_state(*watch))
    new = run_command(native_argv(PENDING, args), cwd=cwd, env=env, timeout=timeout)
    unchanged(before, tree_state(*watch))
    compare(PENDING, old, new, decisions=decisions, case=case, **params)
    return old, new


def clock_text(values):
    return ','.join(repr(float(value)) for value in values)


def pending_injected(args, env, cwd, watch=(), spec=None, *, clock=None, trace=True, signal_case=None,
                     timeout=60):
    """旧版の包みとRustの入口へ、同じ故障・時計を与えて比べる。記録の順と回数も比べる。"""
    spec = dict(spec or {})
    with tempfile.TemporaryDirectory(prefix='r4g-pending-trace-') as temp:
        paths = [Path(temp) / 'old.trace', Path(temp) / 'new.trace']
        old_spec, new_spec = dict(spec), dict(spec)
        if trace:
            old_spec['trace'], new_spec['trace'] = str(paths[0]), str(paths[1])
        hits = [Path(temp) / 'old.hits', Path(temp) / 'new.hits']
        if spec.get('rules'):
            old_spec['hits'], new_spec['hits'] = str(hits[0]), str(hits[1])
        if clock is not None:
            old_spec['clock'] = [float(value) for value in clock]
        entry = ['--r4g-pending-fault', json.dumps(new_spec)]
        if clock is not None:
            entry += ['--r4g-clock', clock_text(clock)]
        before = tree_state(*watch)
        old = run_command([sys.executable, str(PENDING_WRAPPER), json.dumps(old_spec), str(PENDING_SCRIPT), *args],
                          cwd=cwd, env=env, timeout=timeout)
        unchanged(before, tree_state(*watch))
        new = run_command(native_argv(PENDING, args, entry), cwd=cwd, env=env, timeout=timeout)
        unchanged(before, tree_state(*watch))
        if signal_case is not None:
            sigint_pair(PENDING, signal_case, old, new)
            return old, new, None
        compare(PENDING, old, new)
        calls = same_trace(read_lines(paths[0]), read_lines(paths[1])) if trace else None
        if spec.get('rules'):
            check_rule_hits([json.loads(path.read_text()) if path.exists() else None for path in hits])
        return old, new, calls


def check_rule_hits(counts):
    """故障の規則ごとの当たった回数: 旧版の包みと Rust で同じで、どの規則も1回以上当たる。"""
    if counts[0] is None or counts[1] is None or counts[0] != counts[1]:
        raise Difference(f'故障の規則の当たった回数が違う(旧版 {counts[0]} / Rust {counts[1]})')
    if 0 in counts[0]:
        raise Difference(f'一度も当たらない故障の規則がある({counts[0]})')


def result_of(run):
    if run.timed_out or not run.stdout.endswith(b'\n'):
        raise Difference('結果のJSONが無い')
    return json.loads(run.stdout)


class PendingWorld:
    """既存テストの PendingFixture と同じ形の使い捨ての入力(一時フォルダ・cwd・home・xdg・3つの置き場)。"""

    def __init__(self, test, parent=None):
        self.temp = tempfile.TemporaryDirectory(prefix='r4g-pending-', dir=parent)
        test.addCleanup(self.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.cwd, self.home, self.xdg = self.base / 'cwd', self.base / 'home', self.base / 'xdg'
        for path in (self.cwd, self.home, self.xdg):
            path.mkdir()
        self.roots = [self.xdg / 'dev-workflow', self.home / '.local/state/dev-workflow',
                      self.home / '.cache/dev-workflow']
        self.env = pending_env(HOME=str(self.home), XDG_STATE_HOME=str(self.xdg), PYTHONDONTWRITEBYTECODE='1')

    def cleanup(self):
        # 権限000のフォルダを戻してから消す(長いパスで辿れない所は、TemporaryDirectory の削除に任せる)。
        try:
            for path in [self.base, *self.base.rglob('*')]:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
        except OSError:
            pass
        self.temp.cleanup()

    def create_roots(self):
        for path in self.roots:
            path.mkdir(parents=True, exist_ok=True)

    def args(self, *extra, cwd=None):
        return ['scan', '--cwd', str(cwd or self.cwd), *extra]

    def make_top(self, top):
        top = Path(top)
        for sub in ('.git/objects', '.git/refs', 'task'):
            (top / sub).mkdir(parents=True, exist_ok=True)
        (top / '.git/HEAD').write_bytes(b'ref: refs/heads/main\n')
        return top

    def mark(self, guard, top, *, common=None, taskdir=None, tmpdir=None, override=None):
        """旧`implement-guard.sh`の形の印(13キー)。"""
        def ident(path, follow=True):
            info = os.stat(str(path), follow_symlinks=follow)
            return str(info.st_dev).encode(), str(info.st_ino).encode()
        values = {'version': b'1'}
        values['guard_dev'], values['guard_ino'] = ident(guard, follow=False)
        for key, path in (('top', top), ('common', common or Path(top) / '.git'), ('taskdir', taskdir or Path(top) / 'task')):
            values[key] = os.fsencode(str(path)).hex().encode()
            values[key + '_dev'], values[key + '_ino'] = ident(path)
        values['tmpdir'] = b'' if tmpdir is None else os.fsencode(str(tmpdir)).hex().encode()
        values.update(override or {})
        mark = Path(guard).parent / ('owner-' + Path(guard).name[len('guard-'):])
        mark.write_bytes(b''.join(key.encode() + b'\t' + value + b'\n' for key, value in values.items()))
        mark.chmod(0o600)
        return mark

    def candidate(self, name, kind='plain', root=0):
        """既存テストの make_kind と同じ種類(plain・overlap・other・unknown)の候補。"""
        guard = self.roots[root] / ('guard-' + name)
        guard.mkdir()
        if kind == 'plain':
            return guard
        if kind == 'overlap':
            top = self.make_top(self.cwd)
        else:
            top = self.make_top(self.base / ('top-' + name))
        tmpdir = None
        if kind == 'unknown':
            tmpdir = self.base / ('loop-' + name)
            tmpdir.symlink_to(tmpdir)
        self.mark(guard, top, tmpdir=tmpdir)
        return guard


def hexed(value):
    return os.fsencode(value).hex()

# ---------------------------------------------------------------- pending: 既存テストの包みと対応表

class _HelperRuns:
    """既存テストのモジュールの`subprocess`の代わり。旧スクリプトを起動する`run`だけをRustとの組にする。"""

    def __init__(self, real, helper):
        self._real = real
        self._helper = str(helper)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def run(self, argv, *args, **kwargs):
        result = self._real.run(argv, *args, **kwargs)
        if self._helper not in [str(item) for item in argv]:
            return result
        rest = list(argv)[[str(item) for item in argv].index(self._helper) + 1:]
        old = RunResult(result.returncode, result.stdout, result.stderr, False)
        new = run_command(native_argv(PENDING, rest), cwd=kwargs.get('cwd') or os.getcwd(),
                          env=kwargs.get('env'), timeout=kwargs.get('timeout') or 60)
        compare(PENDING, old, new)
        return result


def pending_cli_wrapper(original, module):
    """CLIの起動(`cli`と、`scan --help`の`subprocess.run`)だけを、旧版とRustの組へ置き換える。

    旧版は既存テストのとおり(`-I -B`)に起動し、その出力で既存の検査をすべて行う。続けて同じ引数・環境・
    作業場所でRustを起動し、終了値・標準出力・標準エラーを全バイトで比べ、入力の無変更を前後で確かめる。
    """
    class Paired(original):
        def setUp(self):
            super().setUp()
            patcher = mock.patch.object(module, 'subprocess', _HelperRuns(module.subprocess, module.HELPER))
            patcher.start()
            self.addCleanup(patcher.stop)

        def watched(self):
            return [self.base] + ([self.owned] if getattr(self, 'owned', None) else [])

        def cli(self, args=None, env=None, cwd=None):
            args = ['scan', '--cwd', str(cwd or self.cwd)] if args is None else args
            env = self.env if env is None else env
            before = tree_state(*self.watched())
            rc, result = super().cli(args, env=env, cwd=cwd)
            old = RunResult(rc, module.canonical(result) + b'\n', self.last_stderr, False)
            unchanged(before, tree_state(*self.watched()))
            new = run_command(native_argv(PENDING, args), cwd=str(self.base), env=env, timeout=12)
            unchanged(before, tree_state(*self.watched()))
            compare(PENDING, old, new)
            return rc, result

    Paired.__name__ = Paired.__qualname__ = 'Paired' + original.__name__
    return Paired


def pending_existing_suite():
    """既存のCLIの24件(PendingCLITests 14・PendingOwnerTests 10)を旧版とRustの組で流す。"""
    wrappers = {(PENDING_TESTS, name): pending_cli_wrapper for name in ('PendingCLITests', 'PendingOwnerTests')}
    return existing_suite(wrappers, classes=set(wrappers))


def load_correspondence():
    data = json.loads(CORRESPONDENCE.read_text(encoding='utf-8'))
    if set(data) != {'file', 'note', 'entries'} or data['file'] != PENDING_TESTS:
        raise ValueError('対応表の形が違う')
    return data


def check_correspondence(data, *, manifest=None, injection_methods=(), rust_sources=None):
    """既存47件の対応表: 名前の集合が既存テストと同じ・種類ごとの件数が固定値・行き先がある。"""
    manifest = manifest or existing_manifest()
    module = load(ROOT / 'scripts' / PENDING_TESTS, 'r4g_existing_names')
    loader = unittest.TestLoader()
    expected = {(name, method) for name in manifest['files'][PENDING_TESTS]['classes']
                for method in loader.getTestCaseNames(getattr(module, name))}
    entries = data['entries']
    seen = [(entry['class'], entry['name']) for entry in entries]
    if len(seen) != len(set(seen)) or set(seen) != expected:
        raise AssertionError('対応表の名前が既存テストと1対1でない')
    counts = {}
    for entry in entries:
        if set(entry) != {'class', 'name', 'kind', 'target', 'reason'} or entry['kind'] not in CORRESPONDENCE_KINDS:
            raise AssertionError('対応表の項目の形が違う')
        if not entry['reason']:
            raise AssertionError('対応表の理由が空')
        counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
        if entry['kind'] == 'cli-pair' and entry['target'] != 'pending-existing':
            raise AssertionError('CLIの組の行き先が違う')
        if entry['kind'] == 'injection-pair' and entry['target'] not in injection_methods:
            raise AssertionError('注入の組の行き先の検査が無い: ' + entry['target'])
        if entry['kind'] == 'rust-unit':
            sources = rust_sources if rust_sources is not None else pending_rust_sources()
            if not any(re.search(r'\bfn ' + re.escape(entry['target']) + r'\(', text) for text in sources):
                raise AssertionError('Rustの単体検査が無い: ' + entry['target'])
    if counts != CORRESPONDENCE_KINDS:
        raise AssertionError(f'対応表の種類ごとの件数が固定値と違う: {counts}')
    return counts


def pending_rust_sources():
    directory = ROOT / 'native/crates/cli/src/commands/pending_implementation'
    return [path.read_text(encoding='utf-8') for path in sorted(directory.glob('*.rs'))]


def pending_function_cases():
    """旧版の関数で、確認値・判定・種類の固定のケースを作る(Rustの単体検査が同じ表を読む)。"""
    old = load(PENDING_SCRIPT, 'r4g_old_pending_functions')
    tests = load(ROOT / 'scripts' / PENDING_TESTS, 'r4g_existing_fixed_result')
    base = tests.fixed_result()

    def copy():
        return json.loads(json.dumps(base))

    variants = [('fixed', copy())]
    shown = copy()
    shown.update(cwd='display changed', result='acknowledged', complete=False,
                 issues=[{'message': '表示だけ'}], list_sha256='unused', identity_sha256='unused')
    shown['roots'][0]['input_path'] = shown['roots'][0]['resolved_path'] = '表示だけ'
    shown['candidates'][0]['path'] = '表示だけ'
    variants.append(('display-only', shown))
    for key, value in (('mtime_ns', 101), ('ctime_ns', 201), ('size', 4097), ('mode', 16832 | 0o077),
                       ('ino', 13), ('dev', 8), ('uid', 43), ('kind', 'file'),
                       ('name_bytes_hex', '67756172642d79'), ('path_bytes_hex', '2f73746174652f67756172642d79')):
        changed = copy()
        changed['candidates'][0][key] = value
        variants.append(('candidate-' + key, changed))
    for key, value in (('mode', 16832 | 0o077), ('ino', 14), ('uid', 43)):
        changed = copy()
        targets = changed['roots'][:2] if key == 'mode' else changed['roots'][:1]
        for root in targets:
            root['identity'][key] = value
        variants.append(('root-identity-' + key, changed))
    changed = copy()
    changed['cwd_bytes_hex'] = '2f637765'
    variants.append(('cwd', changed))
    changed = copy()
    changed['candidates'] = []
    variants.append(('no-candidates', changed))
    changed = copy()
    changed['roots'][1]['input_path_bytes_hex'] = '2f616c69617332'
    variants.append(('alias-input', changed))
    changed = copy()
    changed['candidates'][0].update(owner={'top': '/a', 'top_bytes_hex': '2f61'}, unknown_later=1)
    variants.append(('extra-keys', changed))
    changed = copy()
    changed['candidates'][0]['reason'] = 'overlap'
    variants.append(('reason-overlap', changed))
    changed = copy()
    changed['candidates'][0].update(relation='other-project', reason=None)
    variants.append(('other-project', changed))
    digests = [{'label': label, 'result': result, 'list': old.list_digest(result),
                'identity': old.identity_digest(result)} for label, result in variants]
    decision_base = copy()
    overlap = dict(decision_base['candidates'][0], name_bytes_hex='67756172642d79', reason='overlap')
    other = dict(decision_base['candidates'][0], name_bytes_hex='67756172642d7a', relation='other-project',
                 reason=None)
    decision_base['candidates'] += [overlap, other]
    only_other = copy()
    only_other['candidates'] = [other]
    empty = copy()
    empty['candidates'] = []
    pins = {'list': 'a' * 64, 'identity': 'b' * 64}
    decisions = []
    for label, result in (('mixed', decision_base), ('other-only', only_other), ('empty', empty)):
        for acknowledged in ({}, {'identity': 'b' * 64}, {'identity': 'c' * 64}, {'list': 'a' * 64},
                             {'list': 'c' * 64}):
            code, outcome, notes = old.decide_outcome(result, acknowledged, pins)
            decisions.append({'label': label, 'result': result, 'acknowledged': acknowledged, 'digests': pins,
                              'expected': [code, outcome, notes]})
    kinds = [[mode, old.candidate_kind(mode)] for mode in
             (stat_module.S_IFBLK | 0o600, stat_module.S_IFCHR | 0o600, 0o600, stat_module.S_IFDIR | 0o700,
              stat_module.S_IFREG | 0o644, stat_module.S_IFLNK | 0o777, stat_module.S_IFIFO | 0o600,
              stat_module.S_IFSOCK | 0o755)]
    return {'digests': digests, 'decisions': decisions, 'kinds': kinds}


class PendingFunctionCases(unittest.TestCase):
    """Rustの単体検査が読む固定のケースが、今の旧版の関数の値と同じこと(表の陳腐化を拒む)。"""

    def test_fixed_function_cases_still_match_the_old_functions(self):
        recorded = json.loads(FUNCTION_CASES.read_text(encoding='utf-8'))
        self.assertEqual(recorded, json.loads(json.dumps(pending_function_cases())))
        self.assertEqual(recorded['digests'][0]['list'],
                         'c6846d4850a3703d5365dc73c53c70bb351d19a06aa118773f3816c1a929a990')

    def test_correspondence_table_covers_the_47_existing_tests(self):
        methods = unittest.TestLoader().getTestCaseNames(PendingInjectionPairs)
        counts = check_correspondence(load_correspondence(), injection_methods=methods)
        self.assertEqual(sum(counts.values()), 47)

# ---------------------------------------------------------------- pending: 既存のmockの検査と同じ故障の組

NOSUP = sorted({errno.ENOSYS, getattr(errno, 'ENOTSUP', errno.ENOSYS), getattr(errno, 'EOPNOTSUPP', errno.ENOSYS)})


class PendingInjectionPairs(unittest.TestCase):
    """既存の PendingBoundaryTests のうち、故障を与える14件を、旧版の包みとRustの注入へ同じ故障で与える。

    メソッドの名前は既存テストと同じ。旧版の`PermissionError('...')`・`NotImplementedError`のような
    エラー番号の無い例外はRustに無いので、同じ種類のエラー番号(EACCES・EPERM・ENOSYS・ENOTSUP・
    EOPNOTSUPP)で与える。比べるのは終了値・標準出力・標準エラーの全バイトと、OSの呼出しの順と回数。
    """

    maxDiff = None

    def world(self, parent=None):
        world = PendingWorld(self, parent)
        world.create_roots()
        return world

    def pair(self, world, spec=None, *, extra=(), clock=None, env=None, cwd_path=None):
        args = world.args(*extra, cwd=cwd_path)
        return pending_injected(args, env or world.env, str(world.base), [world.base], spec, clock=clock)

    def owner_world(self, name, *, git_cwd=False):
        world = self.world()
        if git_cwd:
            world.make_top(world.cwd)
        guard = world.roots[0] / ('guard-' + name)
        guard.mkdir()
        top = world.base / 'other'
        for sub in ('.git', 'task'):
            (top / sub).mkdir(parents=True, exist_ok=True)
        world.mark(guard, top)
        return world, top

    def acks(self, world):
        old, _, _ = self.pair(world)
        result = result_of(old)
        self.assertTrue(result['complete'])
        return [[], ['--acknowledged-list', result['list_sha256']],
                ['--acknowledged-identities', result['identity_sha256']]]

    def test_runtime_unavailable_denied_and_unknown_exception(self):
        world = self.world()
        for action, code in (({'errno': errno.EACCES}, 'unreadable'), ({'errno': errno.EPERM}, 'unreadable'),
                             ({'errno': errno.ENOSYS}, 'missing-feature'),
                             ({'unexpected': 'RuntimeError'}, 'internal-error')):
            with self.subTest(action=action):
                old, _, _ = self.pair(world, {'rules': [dict(op='scandir', **action)]})
                self.assertIn(code, [issue['code'] for issue in result_of(old)['issues']])

    def test_time_limit_uses_monotonic(self):
        world = self.world()
        old, _, _ = self.pair(world, clock=[0.0, 6.0])
        self.assertEqual([issue['code'] for issue in result_of(old)['issues']], ['time-limit'])
        # 単調時計の実行時の OSError は missing-feature(旧版の Budget.now)。最初と3回目で失敗させる。
        for calls in ([1], [3]):
            with self.subTest(clock_failure=calls):
                old, _, _ = self.pair(world, {'rules': [{'op': 'clock', 'calls': calls, 'errno': errno.EIO}]})
                self.assertEqual([issue['code'] for issue in result_of(old)['issues']], ['missing-feature'])

    def test_candidate_disappears_or_metadata_changes(self):
        world = self.world()
        (world.roots[0] / 'guard-race').mkdir()
        for rule in ({'errno': errno.ENOENT}, {'from': 2, 'change': {'ino': 1}}):
            with self.subTest(rule=rule):
                old, _, _ = self.pair(world, {'rules': [dict(op='stat', last_hex=hexed('guard-race'), dir=True,
                                                             **rule)]})
                self.assertIn('changed-during-scan', [issue['code'] for issue in result_of(old)['issues']])

    def test_candidate_internals_never_opened_or_scanned(self):
        world = self.world()
        guard = world.roots[0] / 'guard-private'
        guard.mkdir()
        (guard / 'manifest').write_bytes(b'nonsecret unread data')
        (guard / 'taskmd-body').write_bytes(b'not a task')
        (world.roots[0] / 'guard-link').symlink_to(guard)
        old, _, calls = self.pair(world)
        self.assertEqual(result_of(old)['result'], 'confirmation-required')
        opened = {}
        for entry in calls:
            if entry[0] == 'open':
                self.assertNotIn(b'guard-', bytes.fromhex(entry[2]))
                self.assertEqual(entry[3] & WRITE_FLAGS, 0)
                if entry[4][0] == 'ok':
                    opened[entry[4][1]] = bytes.fromhex(entry[2])
        scanned = [opened.get(entry[1]) for entry in calls if entry[0] == 'scandir']
        self.assertEqual(sorted(scanned), sorted(os.fsencode(str(root)) for root in world.roots))

    def test_mark_owner_and_changes_while_reading(self):
        world, _ = self.owner_world('b')
        for rule, expected in (({'change': {'uid': 1}}, 'mark-invalid'), ({'calls': [1], 'errno': errno.ENOENT}, 'no-mark'),
                               ({'calls': [2], 'errno': errno.ENOENT}, 'no-mark'),
                               ({'calls': [2], 'change': {'ino': 1}}, 'mark-invalid')):
            with self.subTest(rule=rule):
                old, _, _ = self.pair(world, {'rules': [dict(op='stat', path_hex=hexed('owner-b'), dir=True, **rule)]})
                self.assertEqual(result_of(old)['candidates'][0]['reason'], expected)

    def test_unsupported_features_in_mark_and_git_readers_stop_with_11(self):
        world, _ = self.owner_world('f')
        acks = self.acks(world)
        for number in NOSUP:
            readers = {'read': {'op': 'read'}, 'stat-mark': {'op': 'stat', 'path_hex': hexed('owner-f')},
                       'stat-dotgit': {'op': 'stat', 'path_hex': hexed('.git')},
                       'open-parent': {'op': 'open', 'path_hex': hexed('..')}}
            for label, rule in readers.items():
                for extra in acks:
                    with self.subTest(errno=number, reader=label, ack=extra[:1]):
                        old, _, _ = self.pair(world, {'rules': [dict(rule, errno=number)]}, extra=extra)
                        self.assertIn('missing-feature', [issue['code'] for issue in result_of(old)['issues']])

    def test_unsupported_features_in_each_new_opener_stop_with_11(self):
        world, top = self.owner_world('g', git_cwd=True)
        acks = self.acks(world)
        targets = {'mark-open': b'owner-g', 'top-open': os.fsencode(str(top)), 'tmp-open': b'/tmp',
                   'parent-open': b'..'}
        for number in NOSUP:
            for label, target in targets.items():
                for extra in acks:
                    with self.subTest(errno=number, opener=label, ack=extra[:1]):
                        old, _, _ = self.pair(world, {'rules': [{'op': 'open', 'path_hex': target.hex(),
                                                                  'errno': number}]}, extra=extra)
                        self.assertIn('missing-feature', [issue['code'] for issue in result_of(old)['issues']])

    def test_candidate_owner_change_during_scan_stops(self):
        world = self.world()
        (world.roots[0] / 'guard-owner').mkdir()
        old, _, _ = self.pair(world, {'rules': [{'op': 'stat', 'last_hex': hexed('guard-owner'), 'from': 2,
                                                  'change': {'uid': 1}}]})
        self.assertIn('changed-during-scan', [issue['code'] for issue in result_of(old)['issues']])

    def test_final_recheck_compares_numbers_kind_and_owner_only(self):
        world = self.world()
        (world.roots[0] / 'guard-t').mkdir()
        change = {'mtime_ns': 1, 'ctime_ns': 1, 'size': 1, 'mode_xor': 0o007}
        old, _, _ = self.pair(world, {'rules': [{'op': 'stat', 'last_hex': hexed('guard-t'), 'from': 2,
                                                  'change': change}]})
        self.assertEqual(result_of(old)['result'], 'confirmation-required')
        info = world.roots[0].stat()
        old, _, _ = self.pair(world, {'rules': [{'op': 'fstat', 'ident': [info.st_dev, info.st_ino], 'from': 2,
                                                  'change': change}]})
        self.assertEqual(result_of(old)['result'], 'confirmation-required')

    def test_root_alias_is_enumerated_once(self):
        world = self.world()
        env = dict(world.env, XDG_STATE_HOME=str(world.home / '.local/state'))
        old, _, calls = self.pair(world, env=env)
        self.assertEqual(result_of(old)['result'], 'none')
        self.assertEqual(len([entry for entry in calls if entry[0] == 'scandir']), 2)

    def test_root_changes_while_fd_is_held(self):
        world = self.world()
        info = world.roots[0].stat()
        old, _, _ = self.pair(world, {'rules': [{'op': 'fstat', 'ident': [info.st_dev, info.st_ino], 'from': 2,
                                                  'change': {'ino': 1}}]})
        self.assertIn('changed-during-scan', [issue['code'] for issue in result_of(old)['issues']])

    def no_temp(self):
        # 既存テストの TEMP_DIRS=() と同じ: /tmp と /private/tmp を開けない(無い)ことにする。
        return [{'op': 'open', 'path_hex': hexed(path), 'errno': errno.ENOENT} for path in ('/tmp', '/private/tmp')]

    def test_main_outcome_exit_code_and_stderr_are_pinned(self):
        cases = [('a', [], None), ('b', [('a', 'other'), ('b', 'other')], None),
                 ('c', [('a', 'other'), ('b', 'plain')], None), ('d', [('a', 'other'), ('b', 'overlap')], 'identity'),
                 ('e', [('a', 'other'), ('b', 'overlap')], 'list'), ('f', [('a', 'other'), ('b', 'plain')], 'identity'),
                 ('g', [('a', 'unknown')], 'identity'), ('h', [('a', 'plain')], 'zeros')]
        for label, candidates, ack in cases:
            with self.subTest(case=label):
                world = self.world()
                world.make_top(world.cwd)
                for name, kind in candidates:
                    world.candidate(name, kind)
                # 印のある候補が無いと /tmp を開かない(規則が当たらない)ので、そのときは規則を置かない。
                marked = any(kind != 'plain' for _, kind in candidates)
                spec = {'rules': self.no_temp()} if marked else {}
                old, _, _ = self.pair(world, spec)
                first = result_of(old)
                extra = {None: [], 'list': ['--acknowledged-list', first['list_sha256']],
                         'identity': ['--acknowledged-identities', first['identity_sha256']],
                         'zeros': ['--acknowledged-identities', '0' * 64]}[ack]
                self.pair(world, spec, extra=extra)

    def clock_calls(self, world, spec):
        _, _, calls = self.pair(world, spec)
        return len([entry for entry in calls if entry[0] == 'clock'])

    def test_final_time_check_sits_between_digests_and_decision(self):
        world = self.world()
        world.make_top(world.cwd)
        world.candidate('other', 'other')
        world.candidate('plain', 'plain')
        spec = {'rules': self.no_temp()}
        count = self.clock_calls(world, spec)
        old, _, _ = self.pair(world, spec, clock=[0.0] * (count - 1) + [6.0])
        result = result_of(old)
        self.assertEqual((old.returncode, result['result'], result['complete']), (11, 'scan-unavailable', False))
        self.assertNotIn('list_sha256', result)
        self.assertEqual([c['reason'] for c in result['candidates']], [None, 'no-mark'])
        self.assertEqual(old.stderr, 'pending-implementation: time-limit 走査全体の5秒の上限に達しました。\n'.encode())

    def test_unexpected_failure_in_decision_becomes_internal(self):
        world = self.world()
        world.make_top(world.cwd)
        world.candidate('other', 'other')
        world.candidate('overlap', 'overlap')
        old, _, _ = self.pair(world, {'rules': self.no_temp(), 'overlap_unexpected': 'RuntimeError'},
                              extra=['--acknowledged-identities', '0' * 64])
        self.assertEqual(old.returncode, 20)
        self.assertEqual(result_of(old)['issues'][0]['message'], '想定外の失敗が起きました: RuntimeError')


class PendingCheckpoints(unittest.TestCase):
    """時計の確認点の位置と回数: すべての確認点で上限に達する時刻を、旧版の包みとRustへ同じに与える。
    `SIGINT`・`SIGTERM`を決めた確認点で送る(2026-11-11 22:31 の決定の範囲で比べる)。"""

    def rich_world(self):
        world = PendingWorld(self)
        world.create_roots()
        world.make_top(world.cwd)
        world.candidate('other', 'other')
        world.candidate('overlap', 'overlap')
        world.candidate('plain', 'plain')
        world.candidate('unknown', 'unknown', root=1)
        (world.roots[2] / 'entry-file').write_bytes(b'x')
        return world

    def calls(self, world):
        _, _, calls = pending_injected(world.args(), world.env, str(world.base), [world.base])
        return len([entry for entry in calls if entry[0] == 'clock'])

    def test_every_checkpoint_reaches_the_limit_at_the_same_place(self):
        world = self.rich_world()
        count = self.calls(world)
        self.assertGreater(count, 30)
        seen = set()
        # 1回目は起点(Budget の作成)。2回目から最後までの各確認点で、上限に達する時刻を与える。
        for index in range(1, count):
            with self.subTest(checkpoint=index + 1):
                old, _, _ = pending_injected(world.args(), world.env, str(world.base), [world.base],
                                             clock=[0.0] * index + [5.0])
                issue = result_of(old)['issues'][0]
                self.assertEqual(issue['code'], 'time-limit')
                seen.add((issue['root_id'], issue['path_bytes_hex'], len(result_of(old)['candidates'])))
        # 確認点ごとに、置き場・パス・その時点の候補の数の違う time-limit になる(全部が同じ位置ではない)。
        self.assertGreater(len(seen), 5)

    def test_real_clock_limit_with_a_stopped_process(self):
        """本物の単調時計で5秒の上限が効くこと: 決めた確認点で自分に SIGSTOP を送らせ、止まったのを確かめて
        5秒を超えてから SIGCONT を送る。旧版の包みと Rust で、同じ確認点の time-limit になることを比べる。"""
        world = self.rich_world()
        count = self.calls(world)
        for call in (count // 2, count):
            with self.subTest(call=call):
                results = []
                started = time.monotonic()
                spec = json.dumps({'signal_at_clock': {'call': call, 'signal': signal.SIGSTOP}})
                for argv in ([sys.executable, str(PENDING_WRAPPER), spec, str(PENDING_SCRIPT), *world.args()],
                             native_argv(PENDING, world.args(), ['--r4g-pending-fault', spec])):
                    process = subprocess.Popen(argv, cwd=str(world.base), env=world.env, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                    # 直接の子なので、止まったことを waitpid(WUNTRACED) で受ける(/proc を使わない。Mac でも同じ)。
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        pid, status = os.waitpid(process.pid, os.WUNTRACED | os.WNOHANG)
                        if pid == process.pid:
                            if os.WIFSTOPPED(status):
                                break
                            raise Difference('決めた確認点で止まる前に終わった')
                        time.sleep(0.01)
                    else:
                        process.kill()
                        raise Difference('決めた確認点で止まらない')
                    time.sleep(5.2)
                    process.send_signal(signal.SIGCONT)
                    out, err = process.communicate(timeout=60)
                    results.append(RunResult(process.returncode, out, err, False))
                compare(PENDING, *results)
                issue = result_of(results[0])['issues'][0]
                self.assertEqual(issue['code'], 'time-limit')
                OBSERVATIONS.append(f'本物の時計の上限: 確認点 {call}/{count} で time-limit(2回で '
                                    f'{time.monotonic() - started:.1f} 秒)')

    def test_signals_at_checkpoints(self):
        world = self.rich_world()
        count = self.calls(world)
        for call in (1, count // 2, count):
            with self.subTest(signal='SIGINT', call=call):
                pending_injected(world.args(), world.env, str(world.base), [world.base],
                                 {'signal_at_clock': {'call': call, 'signal': signal.SIGINT}},
                                 trace=False, signal_case='pending-boundary/sigint')
            with self.subTest(signal='SIGTERM', call=call):
                old, new, _ = pending_injected(world.args(), world.env, str(world.base), [world.base],
                                               {'signal_at_clock': {'call': call, 'signal': signal.SIGTERM}},
                                               trace=False)
                self.assertEqual((old.returncode, old.stdout, old.stderr), (-signal.SIGTERM, b'', b''))

# ---------------------------------------------------------------- pending: 旧Pythonの入出力と起動

LAUNCH = r'''
import json, os, resource, signal, sys
spec = json.loads(sys.argv[1])
argv = sys.argv[2:]
for name in ('SIGPIPE', 'SIGXFSZ'):
    signal.signal(getattr(signal, name), signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_IGN if spec.get('ignore_sigint') else signal.SIG_DFL)
if spec.get('ignore_sigpipe'):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
for name in spec.get('ignore', []):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
if 'fsize' in spec:
    resource.setrlimit(resource.RLIMIT_FSIZE, (spec['fsize'], resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
for fd, target in sorted(spec.get('redirect', {}).items()):
    fd = int(fd)
    if target is None:
        os.close(fd)
        continue
    if target == 'directory':
        new = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    elif target == 'null-rdwr':
        new = os.open('/dev/null', os.O_RDWR)
    elif target == 'closed-pipe':
        read_end, new = os.pipe()
        os.close(read_end)
    else:
        new = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(new, fd)
    os.close(new)
for fd in spec.get('close', []):
    os.close(fd)
if 'keep_fd' in spec:
    null = os.open('/dev/null', os.O_RDONLY)
    os.dup2(null, spec['keep_fd'], inheritable=True)
    os.close(null)
if spec.get('block'):
    signal.pthread_sigmask(signal.SIG_BLOCK, [getattr(signal, name) for name in spec['block']])
if 'raw_env' in spec:
    # 生の環境(並び・重複・= の無い項目を含む)で exec する。argv[0] は絶対パス。
    import ctypes
    import shutil
    libc = ctypes.CDLL(None, use_errno=True)
    args = [os.fsencode(a) for a in argv]
    env = [bytes.fromhex(item) for item in spec['raw_env']]
    program = os.fsencode(shutil.which(argv[0]) or argv[0])
    libc.execve(program, (ctypes.c_char_p * (len(args) + 1))(*args, None),
                (ctypes.c_char_p * (len(env) + 1))(*env, None))
    sys.exit(127)
os.execvp(argv[0], argv)
'''


def launched(argv, launch):
    """起動の前に、fdの付け替え・資源の上限・SIGINT の無視をしてから exec する(比較器の Python の
    SIGPIPE・SIGXFSZ の無視は、子へ継がせない)。"""
    return [sys.executable, '-I', '-c', LAUNCH, json.dumps(launch), *argv] if launch is not None else list(argv)


def std_fd_reopened(launch):
    """2026-11-12 08:55 の決定(rust-std-fd-reopen): 起動時に閉じた標準の fd(0〜2)は、Rust の標準の実行時の
    初期化が `/dev/null`(読み書き)で開き直したまま使う。その期待は、閉じた fd を `/dev/null`(読み書き)にして
    流した旧版の結果(終了値・出力・子の起動の列・状態)である。閉じる指定(None)だけを置き換えた起動の指定を返す。"""
    if launch is None:
        return None
    redirect = {fd: ('null-rdwr' if target is None else target) for fd, target in launch.get('redirect', {}).items()}
    return dict(launch, redirect=redirect)


def pending_runs(args, env, cwd, *, old_launch=None, new_launch=None, watch=(), timeout=60, entry=()):
    before = tree_state(*watch)
    old = run_command(launched([sys.executable, str(PENDING_SCRIPT), *args], old_launch), cwd=cwd, env=env,
                      timeout=timeout)
    unchanged(before, tree_state(*watch))
    new = run_command(launched(native_argv(PENDING, args, entry), new_launch), cwd=cwd, env=env, timeout=timeout)
    unchanged(before, tree_state(*watch))
    return old, new


def pending_location():
    return dict(script_path=str(PENDING_SCRIPT), script_name='pending-implementation.py',
                stdlib_dir=sysconfig.get_paths()['stdlib'], expected=(2, 0))


def encoding_fixed(command, case, expected, new):
    """文字コードの決定(2026-11-11 22:24)による固定期待: UTF-8 の設定の旧版の結果と全バイトで比べる。"""
    applied(command, 'encoding-fixed', case)
    same_result(expected, new)


class PendingIO(unittest.TestCase):
    """旧Python 3.10の標準入出力・起動の失敗・環境変数・信号の扱いを、旧版とRustで比べる。"""

    def world(self, candidates=0, issue=False):
        world = PendingWorld(self)
        world.create_roots()
        for index in range(candidates):
            (world.roots[index % 3] / ('guard-%03d' % index)).mkdir()
        if issue:
            world.env = pending_env(HOME=None, XDG_STATE_HOME=None)
        return world

    def same(self, old, new, **params):
        compare(PENDING, old, new, **params)

    def test_closed_standard_streams(self):
        """起動時に閉じた標準の fd(2026-11-12 08:55 の決定 rust-std-fd-reopen): Rust は標準の初期化のとおり
        `/dev/null` で開き直したまま動く。期待は、その fd を `/dev/null` にして流した旧版の結果。"""
        world = self.world(candidates=1)
        issue = self.world(issue=True)
        for label, launch, args, case_world in (
                ('stdout', {'redirect': {'1': None}}, world.args(), world),
                ('stdout-help', {'redirect': {'1': None}}, ['scan', '--help'], world),
                ('stderr', {'redirect': {'2': None}}, world.args(), world),
                ('stdin', {'redirect': {'0': None}}, world.args(), world),
                ('all', {'redirect': {'0': None, '1': None, '2': None}}, world.args(), world),
                ('stderr-issue', {'redirect': {'2': None}}, issue.args(), issue)):
            with self.subTest(label=label):
                applied(PENDING, 'rust-std-fd-reopen', 'pending-io/closed-fd')
                old, new = pending_runs(args, case_world.env, str(case_world.base), old_launch=std_fd_reopened(launch),
                                        new_launch=launch, watch=[case_world.base])
                self.same(old, new)

    def test_closed_pipe(self):
        small, big = self.world(candidates=1), self.world(candidates=30)
        for label, world in (('small', small), ('big', big)):
            with self.subTest(label=label):
                launch = {'redirect': {'1': 'closed-pipe'}}
                old, new = pending_runs(world.args(), world.env, str(world.base), old_launch=launch,
                                        new_launch=launch, watch=[world.base])
                if b'Traceback' in old.stderr:
                    self.same(old, new, decisions=('location-fields',), case='pending-io/traceback',
                              location=pending_location())
                else:
                    self.same(old, new)

    def test_file_size_limit_on_stdout(self):
        for count in (1, 30):
            world = self.world(candidates=count)
            plain, _ = pending_plain(world.args(), world.env, str(world.base), [world.base])
            size = len(plain.stdout)
            for limit in sorted({0, 1, 100, 4095, 4096, 8191, 8192, size - 1, size}):
                if limit < 0 or limit > size:
                    continue
                with self.subTest(candidates=count, limit=limit), tempfile.TemporaryDirectory() as temp:
                    outputs = [Path(temp) / 'old.out', Path(temp) / 'new.out']
                    old, new = pending_runs(world.args(), world.env, str(world.base),
                                            old_launch={'fsize': limit, 'redirect': {'1': str(outputs[0])}},
                                            new_launch={'fsize': limit, 'redirect': {'1': str(outputs[1])}},
                                            watch=[world.base])
                    self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
                    self.assertEqual(outputs[0].read_bytes(), plain.stdout[:limit])
                    if b'Traceback' in old.stderr:
                        self.same(old, new, decisions=('location-fields',), case='pending-io/traceback',
                                  location=pending_location())
                    else:
                        self.same(old, new)

    def test_file_size_limit_on_stderr_traceback(self):
        # 呼出履歴は、閉じた pipe の標準出力へ大きな結果を書いた BrokenPipeError で出す(閉じた標準出力は、
        # 2026-11-12 08:55 の決定で Rust が /dev/null のまま使うので、呼出履歴の出し方に使わない)。
        world = self.world(candidates=30)
        launch = {'redirect': {'1': 'closed-pipe'}}
        full_old, _ = pending_runs(world.args(), world.env, str(world.base), old_launch=launch, new_launch=launch)
        self.assertIn(b'Traceback (most recent call last):', full_old.stderr)
        mapped = location_fields(full_old.stderr, **pending_location())
        for limit in sorted({0, 1, 37, 100, 200, len(mapped) - 1, len(mapped), len(mapped) + 1,
                             len(full_old.stderr) - 1}):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as temp:
                errors = [Path(temp) / 'old.err', Path(temp) / 'new.err']
                old, new = pending_runs(world.args(), world.env, str(world.base),
                                        old_launch={'fsize': limit, 'redirect': {'1': 'closed-pipe', '2': str(errors[0])}},
                                        new_launch={'fsize': limit, 'redirect': {'1': 'closed-pipe', '2': str(errors[1])}},
                                        watch=[world.base])
                written = errors[1].read_bytes()
                case = 'pending-io/partial-stderr'
                if len(mapped) <= limit:
                    # Rust の固定名の書込みは切れない: 故障の無かった旧版の結果(所在地を固定名にしたもの)と
                    # 一致することを確かめる(2026-11-11 22:38 の追加の決定 2。故障ありの旧版との終了値の差は認める)。
                    applied(PENDING, 'partial-write-fits', case)
                    self.assertEqual((written, new.returncode, new.stdout), (mapped, full_old.returncode, full_old.stdout))
                else:
                    self.assertEqual(written, partial_write(PENDING, case, mapped, limit))
                    self.assertEqual((new.returncode, new.stdout), (old.returncode, old.stdout))

    def test_startup_environment_variables(self):
        world = self.world(candidates=1)
        for changes in ({'PYTHONINTMAXSTRDIGITS': '1'}, {'PYTHONHASHSEED': 'abc'},
                        {'PYTHONHASHSEED': '-1', 'PYTHONINTMAXSTRDIGITS': 'x'}, {'PYTHONHASHSEED': '0'},
                        {'PYTHONHASHSEED': 'random'}, {'PYTHONOPTIMIZE': '2'}, {'PYTHONOPTIMIZE': 'x'},
                        {'PYTHONUNBUFFERED': '1'}, {'PYTHONINTMAXSTRDIGITS': '700'}):
            with self.subTest(changes=changes):
                pending_plain(world.args(), dict(world.env, **changes), str(world.base), [world.base])
        old, _ = pending_plain(world.args(), dict(world.env, PYTHONHASHSEED='abc'), str(world.base), [world.base])
        self.assertTrue(old.stderr.startswith(b'Fatal Python error: config_init_hash_seed'))

    def test_standard_streams_that_are_directories(self):
        world = self.world(candidates=1)
        for fd in ('0', '1', '2'):
            with self.subTest(fd=fd):
                launch = {'redirect': {fd: 'directory'}}
                old, new = pending_runs(world.args(), world.env, str(world.base), old_launch=launch,
                                        new_launch=launch, watch=[world.base])
                self.assertEqual(old.returncode, 1)
                if fd == '2':
                    self.same(old, new)
                else:
                    self.same(old, new, decisions=('current-thread',), case='pending-io/startup-directory')

    def test_locale_coercion_warnings(self):
        world = self.world(candidates=1)
        base = pending_env(HOME=str(world.home), XDG_STATE_HOME=str(world.xdg), LC_ALL=None, LC_CTYPE=None,
                           LANG=None)
        for changes in ({'PYTHONCOERCECLOCALE': 'warn', 'LANG': 'C'}, {'PYTHONCOERCECLOCALE': 'warn'},
                        {'PYTHONCOERCECLOCALE': 'warn', 'LC_ALL': 'C'}, {'LC_ALL': 'C'}, {'LANG': 'C'},
                        {'PYTHONCOERCECLOCALE': '0', 'LC_ALL': 'C.UTF-8'}):
            with self.subTest(changes=changes):
                old, _ = pending_plain(world.args(), dict(base, **changes), str(world.base), [world.base])
                if changes.get('PYTHONCOERCECLOCALE') == 'warn':
                    self.assertTrue(old.stderr.startswith(b'Python '), old.stderr[:80])

    def test_encoding_is_fixed_to_utf8(self):
        world = self.world()
        os.mkdir(os.fsencode(str(world.roots[0])) + b'/guard-' + '日本語'.encode() + b'-\xff')
        utf8 = dict(world.env, LC_ALL='C.UTF-8')
        expected, _ = pending_plain(world.args(), utf8, str(world.base), [world.base])
        for changes in ({'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'},
                        {'PYTHONIOENCODING': 'latin-1'}, {'PYTHONIOENCODING': 'ascii', 'LC_ALL': 'C.UTF-8'}):
            with self.subTest(changes=changes):
                env = dict(world.env, **changes)
                old, new = pending_runs(world.args(), env, str(world.base), watch=[world.base])
                encoding_fixed(PENDING, 'pending-io/encoding', expected, new)

    def test_sigint_ignored_at_startup_is_kept(self):
        world = PendingCheckpoints.rich_world(self)
        launch = {'ignore_sigint': True}
        spec = json.dumps({'signal_at_clock': {'call': 3, 'signal': signal.SIGINT}})
        old = run_command(launched([sys.executable, str(PENDING_WRAPPER), spec, str(PENDING_SCRIPT), *world.args()],
                                   launch),
                          cwd=str(world.base), env=world.env, timeout=60)
        new = run_command(launched(native_argv(PENDING, world.args(), ['--r4g-pending-fault', spec]), launch),
                          cwd=str(world.base), env=world.env, timeout=60)
        self.assertEqual(old.returncode, 10)
        compare(PENDING, old, new)

    def test_long_current_directory(self):
        world = self.world(candidates=1)
        parts = ['d' * 200] * 25
        deep = Path(os.fsdecode(os.fsencode(str(world.base)) + b'/' + b'/'.join(os.fsencode(p) for p in parts)))
        current = os.open(str(world.base), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                os.mkdir(part, dir_fd=current)
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                os.close(current)
                current = following
        finally:
            os.close(current)
        self.assertGreater(len(os.fsencode(str(deep))), 4096)
        script = 'cd "$1" || exit 99; shift; for p in $(seq 25); do cd ' + parts[0] + ' || exit 98; done; exec "$@"'
        for argv in (['scan', '--cwd', '.'], ['scan', '--cwd', parts[0]]):
            with self.subTest(argv=argv):
                old = run_command(['bash', '-c', script, 'r4g', str(world.base), sys.executable, str(PENDING_SCRIPT),
                                   *argv], cwd=str(world.base), env=world.env, timeout=60)
                new = run_command(['bash', '-c', script, 'r4g', str(world.base), *native_argv(PENDING, argv)],
                                  cwd=str(world.base), env=world.env, timeout=60)
                compare(PENDING, old, new)


class PendingIOLinux(PendingIO):
    """Linux だけで作れる出力先(`/dev/full`・pipe の大きさの指定)の比較。Mac では未実施として記録する
    (Mac の /dev/full の代わりは作れないため。設計の「比較の前提」)。"""

    def test_full_device(self):
        small, issue = self.world(candidates=1), self.world(issue=True)
        cases = [('full-stdout', small, {'redirect': {'1': '/dev/full'}}, small.env, small.args(), False),
                 ('full-stdout-issue', issue, {'redirect': {'1': '/dev/full'}}, issue.env, issue.args(), False),
                 ('full-stderr-issue', issue, {'redirect': {'2': '/dev/full'}}, issue.env, issue.args(), False),
                 ('unbuffered-full', small, {'redirect': {'1': '/dev/full'}}, dict(small.env, PYTHONUNBUFFERED='1'),
                  small.args(), True),
                 ('unbuffered-help', small, {'redirect': {'1': '/dev/full'}}, dict(small.env, PYTHONUNBUFFERED='1'),
                  ['scan', '--help'], True)]
        for label, world, launch, env, args, traceback in cases:
            with self.subTest(label=label):
                old, new = pending_runs(args, env, str(world.base), old_launch=launch, new_launch=launch,
                                        watch=[world.base])
                if traceback:
                    self.assertIn(b'Traceback (most recent call last):', old.stderr)
                    self.same(old, new, decisions=('location-fields',), case='pending-io/traceback',
                              location=pending_location())
                else:
                    self.same(old, new)

    def test_sigint_while_stdout_pipe_is_blocked(self):
        world = self.world(candidates=20)
        results, blocked = [], []
        for argv in ([sys.executable, str(PENDING_SCRIPT), *world.args()], native_argv(PENDING, world.args())):
            result, first = blocked_pipe_sigint(argv, world.env, str(world.base))
            results.append(result)
            blocked.append(first)
        sigint_pair(PENDING, 'pending-io/sigint-blocked-pipe', *results)
        # 同じ長さの書込みで止まり、pipe に入った分(部分書込み)だけを書いたところで終わった(同じ経路)。
        self.assertEqual(blocked[0], blocked[1])
        self.assertTrue(0 < len(results[0].stdout) < blocked[0])



def blocked_pipe_sigint(argv, env, cwd):
    """標準出力のpipeを4096バイトにして読まずに起動し、/proc/<pid>/syscall で標準出力への write で止まった
    のを確かめてから SIGINT を送り、その後に読む。戻り値は結果と、止まった write の長さ。"""
    import fcntl
    read_end, write_end = os.pipe()
    fcntl.fcntl(write_end, 1031, 4096)  # F_SETPIPE_SZ
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=write_end,
                               stderr=subprocess.PIPE, start_new_session=True)
    os.close(write_end)
    def blocked_write():
        # /proc/<pid>/syscall の番号が write(x86_64 は 1・aarch64 は 64)で、fd 1 へ書いて止まっていること。
        number = {'x86_64': '1', 'aarch64': '64'}.get(os.uname().machine)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with open('/proc/%d/syscall' % process.pid) as handle:
                    fields = handle.read().split()
            except OSError:
                fields = []
            if fields[:2] == [number, '0x1']:
                return fields
            time.sleep(0.01)
        raise Difference('標準出力の書込みで止まらない')

    # 書込みで止まったのを確かめてから1回だけ送る。部分書込みで戻った後、旧Pythonは次に止まる前に信号を
    # 処理する(BufferedWriter の PyErr_CheckSignals)。Rust も同じ所で確かめる。
    blocked = blocked_write()
    first = int(blocked[3], 16)
    process.send_signal(signal.SIGINT)
    # 止まっていた write から戻ったのを確かめてから読む。先に読み始めると pipe に空きができ、信号で起きた
    # write が信号を見る前にもう1ページ書く(カーネルの pipe_write は、空きがあれば信号の確かめより先に書く)。
    # その競り合いは旧版どうしでも結果を変えるので、比べる前に除く。
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process_state(process.pid) in (None, 'Z'):
            break
        try:
            with open('/proc/%d/syscall' % process.pid) as handle:
                fields = handle.read().split()
        except OSError:
            break
        if fields and fields[0] != 'running' and fields[:4] != blocked[:4]:
            break
        time.sleep(0.005)
    else:
        raise Difference('信号の後も、止まっていた標準出力の書込みから戻らない')
    chunks = []
    while True:
        part = os.read(read_end, 65536)
        if not part:
            break
        chunks.append(part)
    os.close(read_end)
    stderr = process.stderr.read()
    process.wait(timeout=30)
    OBSERVATIONS.append(f'詰まった pipe: write({first} バイト)で止まった所へ SIGINT({os.path.basename(argv[0])})')
    return RunResult(process.returncode, b''.join(chunks), stderr, False), first

# ---------------------------------------------------------------- pending: 契約の追加のケース

class PendingContract(unittest.TestCase):
    """既存テストに無い契約の分岐(引数の`=`の形・印の番号の境界・リンクの40個・特殊な候補・置き場の組合せ)。"""

    def world(self):
        world = PendingWorld(self)
        world.create_roots()
        return world

    def same(self, world, *extra, env=None, cwd=None):
        old, _ = pending_plain(world.args(*extra, cwd=cwd), env or world.env, str(world.base), [world.base])
        return old

    def test_argument_branches(self):
        world = self.world()
        h = 'a' * 64
        for args in ([], ['--help'], ['scan'], ['scan', '--help', 'x'], ['--help', 'scan'], ['scan', '--cwd'],
                     ['scan', '--cwd=--x'], ['scan', '--cwd='], ['scan', '--cwd', ''],
                     ['scan', '--cwd=' + str(world.cwd), '--acknowledged-list='],
                     ['scan', '--cwd=' + str(world.cwd), '--acknowledged-list=--' + h[2:]],
                     ['scan', '--cwd', str(world.cwd), '--acknowledged-identities', h.upper()],
                     ['scan', '--cwd', str(world.cwd), '--acknowledged-list', h, '--acknowledged-identities=' + h],
                     ['scan', '--cwd', str(world.cwd), '--cwd', str(world.cwd)], ['scan', '--cwd', str(world.cwd), '--'],
                     ['scan', '--cwd', str(world.cwd), 'extra'], ['scan', '--cwd', '\udcff'],
                     ['scan', '--cwd', str(world.cwd), '--acknowledged-list', 'ａ' * 64]):
            with self.subTest(args=args):
                pending_plain(args, world.env, str(world.base), [world.base])

    def test_mark_numbers_at_the_boundaries(self):
        world = self.world()
        top = world.make_top(world.base / 'other')
        guard = world.roots[0] / 'guard-n'
        guard.mkdir()
        for value in (b'18446744073709551615', b'18446744073709551616', b'99999999999999999999',
                      b'100000000000000000000', b'0', b'01'):
            for key in ('guard_dev', 'guard_ino', 'top_ino', 'common_dev', 'taskdir_ino'):
                with self.subTest(key=key, value=value):
                    world.mark(guard, top, override={key: value})
                    self.same(world)
        world.mark(guard, top)
        self.same(world)

    def test_links_up_to_forty_and_loops(self):
        world = self.world()
        real = world.base / 'real-home'
        (real / '.local/state/dev-workflow').mkdir(parents=True)
        for count in (40, 41):
            previous = real
            for index in range(count):
                link = world.base / ('link-%d-%d' % (count, index))
                link.symlink_to(previous)
                previous = link
            with self.subTest(links=count):
                self.same(world, env=dict(world.env, HOME=str(previous)))
        loop = world.base / 'loop'
        loop.symlink_to(loop)
        self.same(world, env=dict(world.env, HOME=str(loop)))
        broken = world.base / 'broken'
        broken.symlink_to(world.base / 'missing')
        self.same(world, env=dict(world.env, HOME=str(broken)))
        self.same(world, env=dict(world.env, HOME='/'))
        self.same(world, env=dict(world.env, HOME=str(world.home) + '/', XDG_STATE_HOME=str(world.home) + '/.local/state/'))

    def test_special_candidates_and_cwd_forms(self):
        world = self.world()
        root = world.roots[0]
        os.mkfifo(str(root / 'guard-fifo'))
        locked = root / 'guard-locked'
        locked.mkdir()
        (locked / 'inner').mkdir()
        locked.chmod(0)
        (root / 'guard-file').write_bytes(b'x')
        (root / 'guard-link').symlink_to(world.base / 'missing')
        (root / 'owner-fifo').write_bytes(b'not a mark\n')
        os.mkfifo(str(root / 'owner-file'))
        self.same(world)
        link = world.base / 'cwd-link'
        link.symlink_to(world.cwd)
        for cwd in (link, world.cwd / '..', world.cwd / '.', Path('cwd'), Path('./cwd/../cwd')):
            with self.subTest(cwd=str(cwd)):
                self.same(world, cwd=cwd)
        (world.roots[1] / 'guard-x').mkdir()
        for env in (dict(world.env, XDG_STATE_HOME='relative'), dict(world.env, XDG_STATE_HOME=''),
                    pending_env(HOME=str(world.home)), pending_env(XDG_STATE_HOME=str(world.xdg)),
                    dict(world.env, HOME=str(world.xdg), XDG_STATE_HOME=str(world.home / '.local/state'))):
            with self.subTest(env={k: env.get(k) for k in ('HOME', 'XDG_STATE_HOME')}):
                self.same(world, env=env)

# ---------------------------------------------------------------- pending-observation(完了条件4)

PENDING_STATIC_FILES = (
    'native/crates/cli/src/commands/pending_implementation/mod.rs',
    'native/crates/cli/src/commands/pending_implementation/lines.rs',
    'native/crates/cli/src/commands/pending_implementation/mark.rs',
    'native/crates/cli/src/commands/pending_implementation/report.rs',
    'native/crates/cli/src/commands/pending_implementation/scan.rs',
    'native/crates/cli/src/commands/pending_implementation/system.rs',
    'native/crates/cli/src/commands/r4g_support/mod.rs',
    'native/crates/cli/src/commands/r4g_support/json.rs',
    'native/crates/cli/src/commands/r4g_support/python_io.rs',
    'native/crates/cli/src/commands/r4g_support/python_startup.rs',
    'native/crates/cli/src/commands/r4g_support/strerror.rs',
    'native/crates/cli/src/commands/r4g_support/text.rs',
    'native/crates/cli/src/commands/r4g_support/traceback.rs',
    'native/crates/cli/src/commands/r4b_support/bytes.rs',
    'native/crates/cli/src/commands/r4b_support/unicode.rs',
    'native/crates/platform/src/r4g/mod.rs',
    'native/crates/platform/src/r4g/fs.rs',
    'native/crates/platform/src/r4g/startup.rs',
)
# 子の起動の関数(std::process・platform の子の実行)。platform の process は信号の扱いの関数だけを許す。
PENDING_FORBIDDEN = re.compile(
    r'std::process|\bCommand\b|\bfork\w*|\bexec\w*|posix_spawn|\bspawn\w*|\bpython_run\b|\bbash_spawn\b|'
    r'\bfork_exec\b|\bPythonCommand\b|\bBashCommand\b|\bChild\b|process::(?!set_disposition\b)\w+')
# pending の本体だけ: 書込みの道具(旧版の AST 検査の write・mkdir・rename・unlink などに当たる)。
PENDING_WRITES = re.compile(
    r'std::fs|\bfs::write\b|create_dir|remove_|\brename\b|\bunlink\b|OFlags::(?:WRONLY|RDWR|CREATE|TRUNC|APPEND)|'
    r'set_permissions|\bchmod\b|\bchown\b|\butime')


def remove_test_items(text):
    """`#[cfg(test)]` の項目を、波括弧の釣り合いで取り除く(`mod tests` もほかの関数も)。行のコメントも除く。"""
    out = []
    lines = text.split('\n')
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == '#[cfg(test)]':
            depth = 0
            started = False
            index += 1
            while index < len(lines):
                current = lines[index].split('//', 1)[0]
                depth += current.count('{') - current.count('}')
                started = started or '{' in current
                index += 1
                if started and depth <= 0:
                    break
                if not started and current.rstrip().endswith(';'):
                    break
            continue
        out.append(line.split('//', 1)[0])
        index += 1
    return '\n'.join(out)


RAW_STRING = re.compile(r'(?<![A-Za-z0-9_])b?r(#*)"(?:.|\n)*?"\1')
PLAIN_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"')
CHAR_LITERAL = re.compile(r"b?'(?:\\.|[^'\\\n])'")


def code_only(text):
    """文字列と文字の字面を空にする(文の中の語を、コードの参照と取り違えないため)。"""
    text = RAW_STRING.sub('""', text)
    text = CHAR_LITERAL.sub("' '", text)
    return PLAIN_STRING.sub('""', text)


def pending_static_findings(root=ROOT):
    """pending と、それが使う部品のソース(テストを除く)が、子の起動と書込みの道具を参照しないこと。"""
    findings = []
    for relative in PENDING_STATIC_FILES:
        text = remove_test_items(code_only((Path(root) / relative).read_text(encoding='utf-8')))
        for number, line in enumerate(text.split('\n'), 1):
            if PENDING_FORBIDDEN.search(line):
                findings.append((relative, number, line.strip()))
            if '/pending_implementation/' in relative and PENDING_WRITES.search(line):
                findings.append((relative, number, line.strip()))
    return findings


def observation_world(test):
    """候補(ASCII・非ASCIIのUTF-8・非UTF-8の名前、ディレクトリ・ファイル・書き手の無いFIFO・権限000)、
    印、Gitの置き場(今回の作業は .git のフォルダ、もう1つは gitdir: と commondir の worktree)を持つ入力。"""
    world = PendingWorld(test)
    world.create_roots()
    world.make_top(world.cwd)
    root = os.fsencode(str(world.roots[0]))
    names = [b'guard-ascii', 'guard-日本語'.encode(), b'guard-\xff\xfe', b'guard-fifo', b'guard-locked',
             b'guard-file']
    for name in names[:3]:
        os.mkdir(root + b'/' + name)
    os.mkfifo(root + b'/guard-fifo')
    os.mkdir(root + b'/guard-locked')
    with open(root + b'/guard-file', 'wb') as handle:
        handle.write(b'private')
    os.chmod(root + b'/guard-locked', 0)
    other = world.make_top(world.base / 'other')
    world.mark(Path(os.fsdecode(root + b'/guard-ascii')), other)
    world.mark(Path(os.fsdecode(root + '/guard-日本語'.encode())), world.cwd)
    admin = world.cwd / '.git/worktrees/wt'
    admin.mkdir(parents=True)
    (admin / 'HEAD').write_bytes(b'ref: refs/heads/main\n')
    (admin / 'commondir').write_bytes(b'../..\n')
    worktree = world.base / 'wt'
    worktree.mkdir()
    (worktree / '.git').write_bytes(b'gitdir: ' + os.fsencode(str(admin)) + b'\n')
    candidates = [root + b'/' + name for name in names]
    return world, worktree, candidates


class PendingObservation(unittest.TestCase):
    """候補を開かないこと・Gitを起動しないことを、strace の記録と Git の代用品と静的検査で確かめる。"""

    def test_sources_do_not_reference_child_processes_or_writers(self):
        self.assertEqual(pending_static_findings(), [])

    def test_git_is_never_started(self):
        world, worktree, _ = observation_world(self)
        with tempfile.TemporaryDirectory(prefix='r4g-git-stub-') as temp:
            log = Path(temp) / 'calls'
            write_stub(temp, 'git', log, code=97)
            env = dict(world.env, PATH=temp + os.pathsep + world.env.get('PATH', ''))
            for cwd in (world.cwd, worktree):
                with self.subTest(cwd=str(cwd)):
                    old, _ = pending_plain(world.args(cwd=cwd), env, str(world.base), [world.base])
                    self.assertIn(result_of(old)['result'], ('confirmation-required', 'other-projects-only'))
            self.assertEqual(read_calls(log), [])

    def traced(self, argv, env, cwd, temp, label):
        prefix = Path(temp) / label
        command, strace_env = strace_command(argv, prefix, env)
        result = run_command(command, cwd=cwd, env=strace_env, timeout=120)
        files = sorted(Path(temp).glob(label + '.*'))
        if result.timed_out or not files:
            raise Difference('strace の記録を取れない')
        return result, parse_strace(files)

    def test_strace_records_no_candidate_opens_and_no_git(self):
        if not strace_usable():
            self.skipTest('strace を使えない(--require-strace の機械では失敗にする)')
        world, worktree, candidates = observation_world(self)
        with tempfile.TemporaryDirectory(prefix='r4g-strace-') as temp:
            stub = Path(temp) / 'stub'
            stub.mkdir()
            log = stub / 'calls'
            write_stub(stub, 'git', log, code=97)
            env = dict(world.env, PATH=str(stub) + os.pathsep + world.env.get('PATH', ''))
            records = []
            for cwd in (world.cwd, worktree):
                old_argv = [sys.executable, str(PENDING_SCRIPT), *world.args(cwd=cwd)]
                new_argv = native_argv(PENDING, world.args(cwd=cwd))
                old, old_events = self.traced(old_argv, env, str(world.base), temp, 'old-%d' % len(records))
                new, new_events = self.traced(new_argv, env, str(world.base), temp, 'new-%d' % len(records))
                compare(PENDING, old, new)
                for side, events, argv in (('old', old_events, old_argv), ('new', new_events, new_argv)):
                    found = opened_candidates(events, candidates, cwd=str(world.base))
                    self.assertEqual(found, [], side)
                    started = executions(events)
                    self.assertEqual(started, [os.fsencode(argv[0])], side)
                    opens = len([event for event in events if event.call in ('open', 'openat', 'openat2')])
                    records.append(f'{side} cwd={cwd.name}: open系 {opens} 件・候補 0 件・execve {len(started)} 件')
            self.assertEqual(read_calls(log), [])
            OBSERVATIONS.extend(records)

    def test_strace_judge_detects_deliberate_candidate_opens(self):
        """対照: 旧版の包み(runpy)とRustの型による注入が候補を開くと、判定が見つけて失敗にする。"""
        if not strace_usable():
            self.skipTest('strace を使えない(--require-strace の機械では失敗にする)')
        world, _, candidates = observation_world(self)
        names = [os.path.basename(candidate) for candidate in candidates]
        with tempfile.TemporaryDirectory(prefix='r4g-strace-control-') as temp:
            for mode in ('relative', 'absolute'):
                spec = json.dumps({'open_candidates': mode})
                for side, argv in (('old', [sys.executable, str(PENDING_WRAPPER), spec, str(PENDING_SCRIPT),
                                            *world.args()]),
                                   ('new', native_argv(PENDING, world.args(), ['--r4g-pending-fault', spec]))):
                    with self.subTest(mode=mode, side=side):
                        _, events = self.traced(argv, world.env, str(world.base), temp, f'{side}-{mode}')
                        found = opened_candidates(events, candidates, cwd=str(world.base))
                        hit_names = set()
                        for event in found:
                            for arg in event.args[:2]:
                                if arg.startswith('"'):
                                    hit_names.add(os.path.basename(string_arg(arg)))
                        self.assertTrue(set(names) <= hit_names, (side, mode, hit_names))
                        OBSERVATIONS.append(f'対照 {side} {mode}: 候補を開く呼出し {len(found)} 件を検出')


OBSERVATIONS = []


# ---------------------------------------------------------------- reviews-dir(単位3)

REVIEWS = 'reviews-dir'
REVIEWS_SCRIPT = OLD[REVIEWS][1]
REVIEWS_TOOLS = ('sed', 'mkdir', 'mktemp', 'git', 'sha256sum', 'shasum', 'openssl', 'cut', 'python3', 'mv', 'rm')
# 2026-11-12 14:24 の決定 4: 旧 reviews-dir.sh が代入する名前の全部(関数の local〈die の code・reason、
# ensure_dirs の d〉・ERR の罠の ec・コマンド置換の結果の代入・配列の GIT_PRE を含む。export する
# GIT_NO_LAZY_FETCH と、unset する CDPATH は test_environment_passed_to_children が見る)。スクリプト全体の
# 代入・local・for・read・unset・export・declare を数えて作った(read・declare・getopts・mapfile・printf -v は無い)。
REVIEWS_SCRIPT_NAMES = ('GIT_PRE', 'TMP', 'ec', 'code', 'reason', 'd', 'SHA_CMD', 'SUB', 'ROOT', 'TOP', 'LIST',
                        'REVIEWS', 'NAME', 'PUBLISH', 'rc', 'SHA', 'NL')
BASH_STARTUP_ENV = ('BASH_ENV', 'SHELLOPTS', 'BASHOPTS', 'POSIXLY_CORRECT')
# 2026-11-12 09:58 の決定(rust-decision-runtime-env.txt【追加の決定 3】): bash 5.2.21 の variables.c の初期化
# (initialize_shell_variables)と代入の処理(special_vars の sv_*)・実行中に読む値から作った、非対話のスクリプトの
# 動きを変える変数。効果を再現せず、比較の入力に使わない(比較の環境から除く)。BASH_COMPAT は範囲の中の値だけ
# (範囲の外は起動時の警告を再現する)。一覧に入れないもの: IFS・OPTIND・OPTERR(起動時に既定へ戻す)・LANG・LC_*・
# TEXTDOMAIN・TEXTDOMAINDIR(地域設定の決定)・BASH_XTRACEFD(起動時の警告を再現。効果は xtrace のときだけ)・
# PATH・HOME・CDPATH(子の入力か、スクリプトが消す)・ENV・PS1・PS2・HIST*・MAIL*・IGNOREEOF・TERM*・
# COMP_WORDBREAKS・HOSTFILE・histchars・PROMPT_*・INPUTRC・TABSIZE・COLUMNS・LINES・EMACS・INSIDE_EMACS
# (非対話の bash では効かない。子の環境へ渡る値は再現する)・動的な変数(RANDOM・SECONDS・BASH_ARGV0 など)。
BASH_BEHAVIOR_ENV = ('BASH_LOADABLES_PATH', 'CHILD_MAX', 'EXECIGNORE', 'FUNCNEST', 'GLOBIGNORE', 'POSIX_PEDANTIC',
                     'PS4', 'TIMEFORMAT', 'TMOUT', 'TZ')


def bash_compat_in_range(value):
    """旧bashの sv_shcompat が受けて動きを変える値(`D.D` か `DD` で 31〜52)。"""
    if len(value) == 3 and value[0].isdigit() and value[1] == '.' and value[2].isdigit():
        level = int(value[0]) * 10 + int(value[2])
    elif len(value) == 2 and value.isdigit():
        level = int(value)
    else:
        return False
    return 31 <= level <= 52


def bash_behavior_input(name, value):
    """比較の入力に使わない変数か(2026-11-12 09:58 の決定)。"""
    return name in BASH_BEHAVIOR_ENV or (name == 'BASH_COMPAT' and bash_compat_in_range(value))


def reviews_env(**changes):
    """比較の環境: bash の起動を変える変数(前提の外。2026-11-11 22:31 の決定の一覧)だけを外す。
    ENV は非対話の bash が読まないので外さない。PYTHON* は子へそのまま渡り、旧版と同じに比べる。"""
    env = {key: value for key, value in os.environ.items()
           if key not in BASH_STARTUP_ENV and not key.startswith('BASH_FUNC_') and not bash_behavior_input(key, value)}
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def reviews_argv(args, launch=None):
    return launched(['bash', str(REVIEWS_SCRIPT), *args], launch)


def reviews_native(args, launch=None):
    return launched(native_argv(REVIEWS, args, ['--r4g-script-name', str(REVIEWS_SCRIPT)]), launch)


def cp_tree(source, target):
    subprocess.run(['cp', '-a', '--', str(source), str(target)], check=True)


def tree_files(base):
    """入力の後の状態(名前・種類・中身)。一時ファイルの名前も伏せない(mktemp の代用品が決まった名前にする)。"""
    state = {}
    base = os.fsencode(str(base))
    for directory, names, files in os.walk(base, onerror=lambda error: None):
        for name in names + files:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            content = None
            if stat_module.S_ISREG(info.st_mode):
                try:
                    with open(path, 'rb') as handle:
                        content = handle.read()
                except OSError:
                    content = b'<unreadable>'
            elif stat_module.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            # 一時ファイルの名前は mktemp の代用品で決まった値にしたので、伏せずに比べる(2026-11-12 単位5の指示)。
            key = os.path.relpath(path, base)
            state[key] = (stat_module.S_IFMT(info.st_mode), stat_module.S_IMODE(info.st_mode), content)
    return state


class ReviewsWorld:
    """reviews-dir の使い捨ての入力: Git の作業場所(未追跡のファイル)・外のフォルダ・道具の記録の代用品。"""

    def __init__(self, test):
        self.temp = tempfile.TemporaryDirectory(prefix='r4g-rd-')
        test.addCleanup(self.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / 'w'
        self.repo = self.work / 'repo'
        self.outside = self.work / 'outside'
        self.outside.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, env=reviews_env())
        (self.repo / 'untracked.txt').write_bytes(b'u\n')
        self.stubs = self.base / 'stubs'
        self.stubs.mkdir()
        self.log = self.base / 'calls'
        self.real_path = os.environ.get('PATH', '/usr/bin:/bin')
        for tool in REVIEWS_TOOLS:
            real = shutil.which(tool, path=self.real_path)
            if real:
                write_stub(self.stubs, tool, self.log, real=real, fixed_mktemp=tool == 'mktemp')
        self.pristine = self.base / 'pristine'

    def cleanup(self):
        try:
            for path in [self.base, *self.base.rglob('*')]:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
        except OSError:
            pass
        self.temp.cleanup()

    @property
    def reviews(self):
        return self.repo / '.claude/reviews'

    def env(self, path=None, **changes):
        settings = {'PATH': path or (str(self.stubs) + os.pathsep + self.real_path), 'LC_ALL': 'C'}
        settings.update(changes)
        return reviews_env(**settings)

    def save(self, name='x.z', *, top=None, root=None, extra=()):
        top = top or self.repo
        root = root or self.repo
        return ['save-untracked', '--top', str(top), '--root', str(root), '--list',
                str(Path(root) / '.claude/reviews' / name), *extra]

    def bin_with(self, *tools, extra=None):
        """道具を選んだ PATH(記録の代用品を並べる)。`extra` は {名前: 中身のパス}。フォルダの名前は、道具と
        `extra` の名前と中身のパスで決める(中身の違う組が、前に作った PATH を使い回さない)。"""
        target = hashlib.sha256(repr(sorted((extra or {}).items())).encode()).hexdigest()[:12]
        directory = self.base / ('bin-' + '-'.join(tools + tuple(extra or ())) + ('-' + target if extra else ''))
        if not directory.exists():
            directory.mkdir()
            for tool in ('bash', 'cat') + tools:
                real = shutil.which(tool, path=self.real_path)
                if real is None:
                    continue
                if tool in REVIEWS_TOOLS:
                    write_stub(directory, tool, self.log, real=real, fixed_mktemp=tool == 'mktemp')
                else:
                    os.symlink(real, directory / tool)
            for name, target in (extra or {}).items():
                os.symlink(target, directory / name)
        return str(directory)


PIPELINE_PAIRS = ({b'sed'}, {b'sha256sum', b'cut'}, {b'shasum', b'cut'}, {b'openssl', b'sed'})


def pipeline_order(calls):
    """同時に動くパイプラインの2つの子(使い方の sed の組・sha256 の道具と cut か sed)は、記録の順が
    実行ごとに入れ替わるので、隣り合う組の中だけを決まった順に並べる。ほかの順は比べる。"""
    out = list(calls)
    index = 0
    while index + 1 < len(out):
        names = {os.path.basename(bytes.fromhex(out[index][0])), os.path.basename(bytes.fromhex(out[index + 1][0]))}
        if names in PIPELINE_PAIRS:
            out[index:index + 2] = sorted(out[index:index + 2], key=lambda call: json.dumps(call[:2]))
            index += 2
            continue
        index += 1
    return out


def reviews_pair(test, world, args, *, env=None, launch=None, case=None, decisions=(), reset=True,
                 calls=True, compare_state=True, outputs=None, time_vars=(), wrap=(), old_launch=None, **params):
    """旧版(bash 5.2.21)とRustを、同じ入力(旧版の後に元へ戻す)で順に流して比べる。
    比べるもの: 終了値・標準出力・標準エラー・子の起動の列(実行したパス・引数・作業場所・生の環境の値の集合)・
    走らせた後の入力の状態。`outputs`を渡せば、出力の比べ方をそれに任せる(知らせの番号の確かめなど)。
    `time_vars`の名前は、子の環境のその値の欄だけを時刻の決定(time-valued-env)で確かめて伏せる。
    `wrap`は、両方の起動の前に置く同じ命令(/proc を隠す名前空間など)。`old_launch` を渡せば、旧版だけを
    その起動の指定で流す(閉じた標準の fd の期待など)。"""
    env = env or world.env()
    if world.pristine.exists():
        shutil.rmtree(world.pristine)
    cp_tree(world.work, world.pristine)
    results = []
    for index, argv in enumerate(([*wrap, *reviews_argv(args, old_launch or launch)],
                                  [*wrap, *reviews_native(args, launch)])):
        if index == 1 and reset:
            shutil.rmtree(world.work)
            cp_tree(world.pristine, world.work)
        if world.log.exists():
            world.log.unlink()
        started = time.time()
        result = run_command(argv, cwd=str(world.work), env=env, timeout=120)
        ended = time.time()
        recorded = read_calls(world.log) if calls else []
        if time_vars:
            recorded = time_valued_env(REVIEWS, 'reviews-dir/bash-special-vars', recorded, time_vars, started, ended)
        results.append((result, recorded, tree_files(world.work) if compare_state else None))
    (old, old_calls, old_state), (new, new_calls, new_state) = results
    if calls:
        old, new = without_stub_shell_lines(old, new)
    if outputs is not None:
        outputs(old, new)
    else:
        compare(REVIEWS, old, new, decisions=decisions, case=case, **params)
    if calls:
        same_calls(REVIEWS, pipeline_order(old_calls), pipeline_order(new_calls), case='reviews-dir/calls',
                   env_as_set=True)
    if compare_state and old_state != new_state:
        raise Difference('走らせた後の入力の状態が違う: ' + ', '.join(
            sorted(os.fsdecode(key) for key in set(old_state) ^ set(new_state)
                   or [key for key in old_state if old_state[key] != new_state.get(key)])[:5]))
    return old, new, old_calls


STUB_SHELL_PREFIX = os.fsencode(STUB_WRAPPER.split('\n', 1)[0][2:]) + b': '


def without_stub_shell_lines(old, new):
    """観測の道具(記録の代用品の包み。STUB_WRAPPER の #! の bash)の行だけを除く。包みは、起動の環境変数
    (BASH_XTRACEFD・BASH_COMPAT・LC_ALL・SHLVL)で bash 自身の行を出すが、製品の出力ではない(本物の道具は
    出さない)。並んで動く子(パイプラインの2つ)の行は混ざる順が実行ごとに変わるので、旧版と Rust で行の
    多重集合が同じことを確かめてから、両方から除く(違えば不合格)。製品の bash の行は `bash: ` か
    `<スクリプト>: line N: ` で始まり、この形にならないので除かない。記録の代用品を使う比較(calls=True)の
    全部に当てる。"""
    def split(result):
        lines = result.stderr.splitlines(keepends=True)
        stub = sorted(line for line in lines if line.startswith(STUB_SHELL_PREFIX))
        rest = b''.join(line for line in lines if not line.startswith(STUB_SHELL_PREFIX))
        return stub, dataclasses.replace(result, stderr=rest)
    (old_stub, old), (new_stub, new) = split(old), split(new)
    if old_stub != new_stub:
        raise Difference('記録の代用品の包みの bash の行(多重集合)が違う')
    return old, new


def job_pid_check(old, new, old_children, new_children, *, expected_roles=None, case='reviews-dir/job-pid'):
    """2026-11-12 06:58 の決定(job-pid-sequence): 旧bashが子の信号での終わりを知らせる行の番号を伏せて全文を
    比べ(per-run-values)、伏せた番号の並びが、記録した子の番号の並びと等しいことを確かめる。
    `old_children`・`new_children` は {役: 記録した番号}。旧版の知らせの番号を役へ戻し、Rust の並びがその役の
    番号の並びと等しいこと。`expected_roles` を渡せば、役の並びもそれと等しいこと。番号が出なければ素のまま比べる。"""
    old_seq, new_seq = notice_pids(old.stderr), notice_pids(new.stderr)
    if not old_seq and not new_seq:
        compare(REVIEWS, old, new)
        roles = []
    else:
        applied(REVIEWS, 'job-pid-sequence', case)
        compare(REVIEWS, old, new, decisions=('per-run-values',), case=case)
        by_pid = {pid: role for role, pid in old_children.items()}
        if any(pid not in by_pid for pid in old_seq):
            raise Difference(f'旧版の知らせの番号が、記録した子の番号でない({old_seq}・記録 {old_children})')
        roles = [by_pid[pid] for pid in old_seq]
        expected = [new_children.get(role) for role in roles]
        if new_seq != expected:
            raise Difference(f'Rust の知らせの番号の並びが、記録した子の番号の並びと違う({new_seq}・期待 {expected})')
    if expected_roles is not None and roles != list(expected_roles):
        raise Difference(f'知らせた子の並びが期待と違う({roles}・期待 {list(expected_roles)})')
    return roles


EPOCH_FORMS = {b'EPOCHSECONDS': re.compile(rb'\A[0-9]+\Z'), b'EPOCHREALTIME': re.compile(rb'\A[0-9]+\.[0-9]{6}\Z')}


def time_valued_env(command, case, calls, names, started, ended):
    """2026-11-12 06:58 の決定(time-valued-env): 子の環境の、実行の時刻で値が変わる変数(旧bashの EPOCHSECONDS・
    EPOCHREALTIME)の値の欄だけ、bash の表記の形と、実行の始めから終わりの時刻の間にあることを確かめ、
    `<time>` に置き換える。ほかの欄はそのまま(呼出側が全部比べる)。"""
    start_us, end_us = int(started * 1_000_000), int(ended * 1_000_000) + 1
    out = []
    for call in calls:
        kept = []
        for item in call[2]:
            raw = bytes.fromhex(item)
            name, _, value = raw.partition(b'=')
            if name in names:
                applied(command, 'time-valued-env', case)
                if not EPOCH_FORMS[name].match(value):
                    raise Difference(f'{name.decode()} の値が bash の表記の形でない: {value!r}')
                if name == b'EPOCHSECONDS':
                    within = int(started) <= int(value) <= int(ended)
                else:
                    whole, fraction = value.split(b'.')
                    within = start_us <= int(whole) * 1_000_000 + int(fraction) <= end_us
                if not within:
                    raise Difference(f'{name.decode()} の値が実行の時刻の間にない: {value!r}({started}〜{ended})')
                raw = name + b'=<time>'
            kept.append(raw.hex())
        out.append([call[0], call[1], kept, *call[3:]])
    return out


class ReviewsTools(unittest.TestCase):
    """道具の版の基準(2026-11-12 00:35): 基準と違う機械では、reviews-dir の比較を失敗にする。"""

    def test_tool_versions_are_the_baseline(self):
        require_tool_baseline(tool_versions())


class ReviewsContract(unittest.TestCase):
    """loop-selftest の reviews-dir の節の全観点と、引数・道具・競合・出力の故障・言語・環境。"""

    maxDiff = None

    def setUp(self):
        require_tool_baseline(tool_versions())
        self.world = ReviewsWorld(self)

    def pair(self, args, **kwargs):
        return reviews_pair(self, self.world, args, **kwargs)

    def test_arguments_and_usage(self):
        w = self.world
        root, repo = str(w.repo), str(w.repo)
        cases = [[], ['-h'], ['--help', 'x'], ['x'], ['ensure'], ['ensure', '--root'], ['ensure', '--root='],
                 ['ensure', '--root', ''], ['ensure', '--root', str(w.base / 'missing')],
                 ['ensure', '--root', str(w.repo / 'untracked.txt')], ['ensure', '--root', root, '--top', repo],
                 ['ensure', '--root', root, '--top', ''], ['ensure', '--root', root, '--list', ''],
                 ['ensure', '--root', 'x', '--root', root], ['save-untracked', '--root', root],
                 ['save-untracked', '--root', root, '--top', repo],
                 ['save-untracked', '--root', root, '--top', str(w.base / 'missing'), '--list', 'x'],
                 ['save-untracked', '--root', root, '--top', repo, '--list', str(w.base / 'elsewhere.z')],
                 ['save-untracked', '--root', root, '--top', repo, '--list', root + '/.claude/reviews/'],
                 ['save-untracked', '--root', root, '--top', repo, '--list', root + '/.claude/reviews/a/b'],
                 ['save-untracked', '--root', root, '--top', repo, '--list', root + '/.claude/reviews/.'],
                 ['save-untracked', '--root', root, '--top', repo, '--list', root + '/.claude/reviews/..'],
                 ['save-untracked', '--root', root, '--top', repo, '--list', root + '/.claude/reviewsX/a'],
                 ['save-untracked', '--root', root, '--bogus', 'x'], ['ensure', '--root', '\udcff']]
        for args in cases:
            with self.subTest(args=args):
                self.pair(args)

    def test_ensure_checks_each_level(self):
        w = self.world
        cases = {'ok': lambda d: None,
                 'claude-symlink': lambda d: (d / '.claude').symlink_to(w.outside),
                 'claude-file': lambda d: (d / '.claude').write_bytes(b'x'),
                 'reviews-symlink': lambda d: ((d / '.claude').mkdir(), (d / '.claude/reviews').symlink_to(w.outside)),
                 'reviews-file': lambda d: ((d / '.claude').mkdir(), (d / '.claude/reviews').write_bytes(b'')),
                 'claude-dangling': lambda d: (d / '.claude').symlink_to(d / 'nowhere'),
                 'unwritable': lambda d: d.chmod(0o500)}
        for label, prepare in cases.items():
            with self.subTest(case=label):
                root = w.work / ('root-' + label)
                root.mkdir()
                prepare(root)
                self.pair(['ensure', '--root', str(root)])

    def test_save_untracked_like_the_loop_selftest(self):
        w = self.world
        marker = w.base / 'fsmonitor.marker'
        hook = w.base / 'fsmonitor.sh'
        hook.write_text('#!/bin/sh\necho ran >>"%s"\nexit 1\n' % marker)
        hook.chmod(0o755)
        subprocess.run(['git', '-C', str(w.repo), 'config', 'core.fsmonitor', str(hook)], check=True)
        (w.repo / 'excluded.txt').write_bytes(b'e\n')
        excludes = w.base / 'excludes'
        excludes.write_bytes(b'excluded.txt\n')
        env = w.env(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.excludesFile', GIT_CONFIG_VALUE_0=str(excludes))
        old, _, _ = self.pair(w.save(), env=env)
        self.assertEqual(old.returncode, 0)
        listed = (w.reviews / 'x.z').read_bytes()
        self.assertEqual(old.stdout, hashlib.sha256(listed).hexdigest().encode() + b'\n')
        self.assertIn(b'untracked.txt', listed.split(b'\0'))
        self.assertNotIn(b'excluded.txt', listed.split(b'\0'))
        self.assertFalse(marker.exists())
        self.pair(w.save(), env=env)  # 既存の通常ファイルは置き換える
        for label, prepare in (('symlink', lambda p: p.symlink_to(w.outside / 'target')),
                               ('fifo', lambda p: os.mkfifo(str(p))), ('directory', lambda p: p.mkdir())):
            with self.subTest(list=label):
                target = w.reviews / ('list-' + label)
                prepare(target)
                old, _, _ = self.pair(w.save('list-' + label))
                self.assertEqual(old.returncode, 3)
        self.assertFalse((w.outside / 'target').exists())
        notrepo = w.work / 'notrepo'
        notrepo.mkdir()
        old, _, _ = self.pair(w.save(top=notrepo, root=notrepo))
        self.assertEqual(old.returncode, 4)
        self.assertEqual(os.listdir(notrepo / '.claude/reviews'), [])

    def test_portability_tools_and_races(self):
        w = self.world
        mv_bsd = w.base / 'mv-bsd'
        mv_bsd.write_text('#!/bin/sh\nfor a in "$@"; do\n  case "$a" in\n    --) break ;;\n    -f|-i|-n|-v|-h) : ;;\n'
                          '    -*) echo "mv: illegal option -- ${a#-}" >&2; echo "usage: mv [-f | -i | -n] [-hv] source target"'
                          ' >&2; exit 64 ;;\n  esac\ndone\nexec %s "$@"\n' % shutil.which('mv', path=w.real_path))
        mv_bsd.chmod(0o755)
        real_git = shutil.which('git', path=w.real_path)
        race = w.base / 'git-race'
        race.write_text('#!/bin/sh\n"%s" "$@"\nrc=$?\nfor a in "$@"; do\n  if [ "$a" = ls-files ]; then\n'
                        '    case "${RDP_RACE:-}" in\n      dir) mkdir -- "$RDP_RACE_LIST" ;;\n'
                        '      symlink) ln -s -- "$RDP_RACE_TARGET" "$RDP_RACE_LIST" ;;\n    esac\n  fi\ndone\nexit $rc\n'
                        % real_git)
        race.chmod(0o755)
        cases = [('bsd-mv', w.bin_with('python3', 'sha256sum', 'git', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                       extra={'mv': str(mv_bsd)}), {}),
                 ('nopy-gnu-mv', w.bin_with('mv', 'sha256sum', 'git', 'sed', 'mktemp', 'rm', 'mkdir', 'cut'), {}),
                 ('nopy-bsd-mv', w.bin_with('sha256sum', 'git', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                            extra={'mv': str(mv_bsd)}), {}),
                 ('shasum', w.bin_with('python3', 'mv', 'shasum', 'git', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                       extra={'perl': shutil.which('perl', path=w.real_path)}), {}),
                 ('openssl', w.bin_with('python3', 'mv', 'openssl', 'git', 'sed', 'mktemp', 'rm', 'mkdir'), {}),
                 ('no-sha', w.bin_with('python3', 'mv', 'git', 'sed', 'mktemp', 'rm', 'mkdir'), {}),
                 ('no-sed', w.bin_with('python3', 'mv', 'sha256sum', 'git', 'mktemp', 'rm', 'mkdir', 'cut'), {}),
                 ('no-mktemp', w.bin_with('python3', 'mv', 'sha256sum', 'git', 'sed', 'rm', 'mkdir', 'cut'), {}),
                 ('no-cut', w.bin_with('python3', 'mv', 'sha256sum', 'git', 'sed', 'mktemp', 'rm', 'mkdir'), {}),
                 ('no-git', w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut'), {}),
                 ('no-rm-git-fail', w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'mkdir', 'cut',
                                               extra={'git': '/bin/false'}), {}),
                 ('no-mv-no-py', w.bin_with('sha256sum', 'git', 'sed', 'mktemp', 'rm', 'mkdir', 'cut'), {}),
                 ('race-dir', w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                         extra={'git': str(race)}),
                  {'RDP_RACE': 'dir', 'RDP_RACE_LIST': str(w.reviews / 'x.z')}),
                 ('race-symlink', w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                             extra={'git': str(race)}),
                  {'RDP_RACE': 'symlink', 'RDP_RACE_LIST': str(w.reviews / 'x.z'),
                   'RDP_RACE_TARGET': str(w.outside)})]
        clean = w.base / 'clean'
        (w.repo / '.claude/reviews').mkdir(parents=True)
        cp_tree(w.work, clean)
        for label, path, extra in cases:
            with self.subTest(case=label):
                # ケースごとに同じ入力から始める(前のケースの一覧や残った一時ファイルを持ち越さない)。
                shutil.rmtree(w.work)
                cp_tree(clean, w.work)
                args = w.save() if label != 'no-sed' else ['bogus']
                self.pair(args, env=w.env(path=path, **extra))
                self.assertEqual(os.listdir(w.outside), [])

    def test_output_faults_and_inherited_sigpipe(self):
        w = self.world
        cases = [('stdout-full', w.save(), {'redirect': {'1': '/dev/full'}}),
                 ('stdout-closed', w.save(), {'redirect': {'1': None}}),
                 ('stdout-pipe-default', w.save(), {'redirect': {'1': 'closed-pipe'}}),
                 ('stdout-pipe-ignored', w.save(), {'redirect': {'1': 'closed-pipe'}, 'ignore_sigpipe': True}),
                 ('stderr-full-usage', [], {'redirect': {'2': '/dev/full'}}),
                 ('stderr-closed-usage', [], {'redirect': {'2': None}}),
                 ('stderr-pipe-usage', [], {'redirect': {'2': 'closed-pipe'}}),
                 ('stderr-pipe-usage-ignored', [], {'redirect': {'2': 'closed-pipe'}, 'ignore_sigpipe': True}),
                 ('stderr-full-git-fail', w.save(top=w.outside, root=w.outside), {'redirect': {'2': '/dev/full'}}),
                 ('stdin-closed', w.save(), {'redirect': {'0': None}})]
        for label, args, launch in cases:
            with self.subTest(case=label):
                old_launch = None
                if None in launch.get('redirect', {}).values():
                    # 2026-11-12 08:55 の決定(rust-std-fd-reopen): 閉じた fd を /dev/null にした旧版が期待。
                    applied(REVIEWS, 'rust-std-fd-reopen', 'reviews-dir/closed-fd')
                    old_launch = std_fd_reopened(launch)
                self.pair(args, launch=launch, old_launch=old_launch)
        # 標準エラーが書けず、PATH に rm が無い組。旧bashは ERR の罠の echo の失敗(set -e)で罠を抜けると、罠を
        # 「実行中」(SIG_INPROGRESS)のまま残すので、EXIT の罠の rm の失敗で ERR の罠は動かない(終了値は rm の 127)。
        no_rm = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'mkdir', 'cut', 'git')
        for label, args in (('stderr-full-git-fail-no-rm', w.save(top=w.outside, root=w.outside)),
                            ('stderr-full-save-ok-no-rm', w.save()), ('stderr-full-usage-no-rm', []),
                            ('stderr-full-mkdir-fail-no-rm', w.save(root=str(w.base / 'missing' / 'deeper')))):
            with self.subTest(case=label):
                old, _, _ = self.pair(args, launch={'redirect': {'2': '/dev/full'}}, env=w.env(path=no_rm))
                OBSERVATIONS.append(f'reviews-dir {label}: 終了値 {old.returncode}(旧版と Rust で同じ)')
        for limit in (0, 10, 60, 200):
            with self.subTest(fsize=limit), tempfile.TemporaryDirectory(dir=str(w.base)) as temp:
                # 書込みの上限は子にも効くので、記録の代用品を通さない(本物の道具だけの PATH)。
                launch = {'fsize': limit, 'redirect': {'2': str(Path(temp) / 'err')}}
                self.pair(['x'], launch=launch, calls=False, env=w.env(path=w.real_path))

    def test_environment_passed_to_children(self):
        w = self.world
        for label, changes in (('plain', {}), ('cdpath', {'CDPATH': str(w.outside)}),
                               ('lazy', {'GIT_NO_LAZY_FETCH': '0'}), ('shlvl', {'SHLVL': '7'}),
                               ('shlvl-bad', {'SHLVL': 'abc'}), ('shlvl-999', {'SHLVL': '999'}),
                               ('shlvl-neg', {'SHLVL': '-3'}), ('ifs', {'IFS': 'x'}),
                               ('oldpwd', {'OLDPWD': '/'}), ('oldpwd-bad', {'OLDPWD': '/nonexistent'}),
                               ('pwd-wrong', {'PWD': '/'}), ('invalid-name', {'A-B': '1', '1X': '2'}),
                               ('lineno', {'LINENO': 'x'}), ('optind', {'OPTIND': '5'}),
                               ('locale-warning', {'LC_ALL': 'xx_YY.UTF-8'}), ('ps', {'PS1': 'a', 'PS2': 'b'}),
                               ('xtracefd-99', {'BASH_XTRACEFD': '99'}), ('xtracefd-x', {'BASH_XTRACEFD': 'x'}),
                               ('xtracefd-2', {'BASH_XTRACEFD': '2'}), ('xtracefd-0', {'BASH_XTRACEFD': ' 0'}),
                               ('xtracefd-big', {'BASH_XTRACEFD': '4294967298'}), ('xtracefd-empty', {'BASH_XTRACEFD': ''}),
                               ('compat-bogus', {'BASH_COMPAT': 'bogus'}), ('compat-3.0', {'BASH_COMPAT': '3.0'}),
                               ('compat-99', {'BASH_COMPAT': '99'}), ('argv0', {'BASH_ARGV0': 'x'}),
                               ('seconds', {'SECONDS': '100'}), ('random', {'RANDOM': '7', 'SRANDOM': '9'}),
                               ('bashpid', {'BASHPID': '1', 'BASH_SUBSHELL': '3'}),
                               ('env', {'ENV': str(w.outside / 'env.sh')}),
                               ('python-vars', {'PYTHONPATH': str(w.outside), 'PYTHONINSPECT': '1'}),
                               ('startup-order', {'SHLVL': '999', 'BASH_XTRACEFD': '99', 'BASH_COMPAT': '1',
                                                  'LC_ALL': 'xx_YY'})):
            # 2026-11-12 09:58 の決定: bash の動きを変える変数(範囲の中の BASH_COMPAT を含む)は入力に使わない。
            assert not any(bash_behavior_input(name, value) for name, value in changes.items()), label
            with self.subTest(case=label):
                self.pair(w.save(), env=w.env(**changes))

    def test_setlocale_warning_follows_glibc(self):
        """LC_ALL の setlocale の警告(00:35 の決定)。glibc の探し方・名前の形・LOCPATH は環境の最初の値・
        旧bashの LC_ALL は最後の値・2回目の失敗は errno を立てない(文の末尾の strerror の有無)。"""
        w = self.world
        loc = w.base / 'locpath'
        loc.mkdir()
        base = [f'{key}={value}'.encode() for key, value in w.env(LC_ALL=None).items()]
        cases = {'plain-bad': [b'LC_ALL=xx_YY.UTF-8'], 'modifier': [b'LC_ALL=C.UTF-8@x'],
                 'modifier-euro': [b'LC_ALL=C.utf8@euro'], 'path': [b'LC_ALL=/usr/lib/locale/C.utf8'],
                 'path-bad': [b'LC_ALL=/nonexistent/x'], 'dotdot': [b'LC_ALL=ja_JP.utf8/../C'],
                 'slash-rel': [b'LC_ALL=a/b'], 'dup-bad-good': [b'LC_ALL=xx_YY', b'LC_ALL=C.UTF-8'],
                 'dup-good-bad': [b'LC_ALL=C.UTF-8', b'LC_ALL=xx_YY'], 'dup-bad-bad': [b'LC_ALL=aa_BB', b'LC_ALL=xx_YY'],
                 'locpath-after': [b'LC_ALL=zz_ZZ', b'LOCPATH=' + os.fsencode(str(loc))],
                 'locpath-before': [b'LOCPATH=' + os.fsencode(str(loc)), b'LC_ALL=zz_ZZ'],
                 'locpath-installed': [b'LC_ALL=C.UTF-8', b'LOCPATH=' + os.fsencode(str(loc))],
                 'locpath-archive-only': [b'LC_ALL=' + REVIEWS_ARCHIVE_LOCALE, b'LOCPATH=' + os.fsencode(str(loc))],
                 'archive': [b'LC_ALL=' + REVIEWS_ARCHIVE_LOCALE], 'space': [b'LC_ALL= '],
                 'C': [b'LC_ALL=C'], 'POSIX': [b'LC_ALL=POSIX'], 'long': [b'LC_ALL=' + b'x' * 300],
                 'alias': [b'LC_ALL=japanese'], 'lang-only': [b'LANG=xx_YY']}
        for label, extra in cases.items():
            with self.subTest(case=label):
                launch = {'raw_env': [item.hex() for item in base + extra]}
                self.pair(w.save(), launch=launch)

    def test_bash_own_lines_are_english(self):
        w = self.world
        bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'rm', 'mkdir', 'cut', 'git')
        # 翻訳の .mo は、/usr/share/locale と、Ubuntu の言語の包み(/usr/share/locale-langpack)の両方を見る。
        catalogs = [f'{root}/ja/LC_MESSAGES/{name}.mo' for root in ('/usr/share/locale', '/usr/share/locale-langpack')
                    for name in ('bash', 'coreutils', 'sed', 'git', 'libc')
                    if (Path(root) / 'ja/LC_MESSAGES' / (name + '.mo')).exists()]
        OBSERVATIONS.append('reviews-dir: 日本語の翻訳の .mo: ' + ('・'.join(catalogs) if catalogs else 'なし'))
        locked = w.base / 'locked-root'
        locked.mkdir()
        locked.chmod(0o555)
        # 記録の代用品を通すと、本物の道具の argv[0] は代用品のパスになる(#! の起動で元の名前が消える)。
        # 本物の道具だけの PATH の組も流し、bash が渡す名前のままの診断(翻訳を含む)を比べる。
        cases = (('mktemp-missing', w.save(), bindir), ('usage', ['x'], bindir),
                 ('unwritable-root', ['ensure', '--root', str(locked)], bindir),
                 ('unwritable-root-real-tools', ['ensure', '--root', str(locked)], w.real_path))
        for label, args, path in cases:
            with self.subTest(case=label):
                c_old = run_command(reviews_argv(args), cwd=str(w.work), env=w.env(path=path), timeout=60)
                localized_env = w.env(path=path, LC_ALL=None, LANG='ja_JP.UTF-8', LANGUAGE='ja')
                localized = run_command(reviews_argv(args), cwd=str(w.work), env=localized_env, timeout=60)
                new = run_command(reviews_native(args), cwd=str(w.work), env=localized_env, timeout=60)
                # 同じ言語の旧版と比べる: bash 自身の行だけを英語(LC_ALL=C の旧版の行)へ置き換えた旧版。
                bash_lines_english_same_language(REVIEWS, 'reviews-dir/language', localized.stderr, c_old.stderr,
                                                 new.stderr, str(REVIEWS_SCRIPT))
                self.assertEqual((new.returncode, new.stdout), (localized.returncode, localized.stdout))
                if label.startswith('unwritable-root'):
                    # 翻訳が効いたか(mkdir の診断の行に ASCII の外の文字があるか)を記録する。
                    tool_lines = [line for line in localized.stderr.splitlines() if b'mkdir: ' in line]
                    OBSERVATIONS.append(f'reviews-dir: 日本語の環境の mkdir の診断({label}): ' + (
                        '翻訳された' if any(max(line) > 0x7f for line in tool_lines) else '英語のまま')
                        + f'({len(tool_lines)} 行。先頭 {tool_lines[0].split(b": ")[0].decode() if tool_lines else "-"}。'
                        '旧版と Rust で同じ行)')
                if not label.startswith('unwritable-root'):
                    bash_lines_english(REVIEWS, 'reviews-dir/language', localized.stderr, c_old.stderr, new.stderr,
                                       str(REVIEWS_SCRIPT))

    def test_exec_failures_follow_bash_5_2_21(self):
        """起動の失敗は bash 5.2.21 の規則で比べる(実行権の無いファイル・解釈系の無い #!・2 進・空・ディレクトリ・
        実行権の無いファイルの後ろの本物)。shebang の無い文字のファイルは前提の外(broken-tools)なので比べない。"""
        w = self.world
        real_git = shutil.which('git', path=w.real_path)
        files = {'noexec': (b'#!/bin/sh\nexit 0\n', 0o644), 'badinterp': (b'#!/nonexistent/interp\n', 0o755),
                 'binary': (b'\x7fELF\x02\x01\x01\x00' + b'\x00' * 120, 0o755), 'empty': (b'', 0o755),
                 'crlf': (b'#!/bin/sh\r\nexit 0\r\n', 0o755)}
        for label, (data, mode) in files.items():
            target = w.base / ('git-' + label)
            target.write_bytes(data)
            target.chmod(mode)
            with self.subTest(case=label):
                bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                    extra={'git': str(target)})
                self.pair(w.save(), env=w.env(path=bindir))
        directory = w.base / 'dir-bin'
        (directory / 'git').mkdir(parents=True)
        with self.subTest(case='directory'):
            bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut')
            self.pair(w.save(), env=w.env(path=str(directory) + os.pathsep + bindir))
        with self.subTest(case='noexec-before-real'):
            shadow = w.base / 'shadow-bin'
            shadow.mkdir()
            (shadow / 'git').write_bytes(b'#!/bin/sh\nexit 9\n')
            self.pair(w.save(), env=w.env(path=str(shadow) + os.pathsep + str(w.stubs) + os.pathsep + w.real_path))
        del real_git

    def test_children_killed_by_signals_are_reported(self):
        w = self.world
        pids = w.base / 'killed-pids'
        for signal_name in ('USR1', 'TERM', 'INT', 'PIPE', 'HUP'):
            stub = w.base / ('git-kill-' + signal_name)
            stub.write_text('#!/bin/sh\necho $$ >>"%s"\nkill -%s $$\n' % (pids, signal_name))
            stub.chmod(0o755)
            bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                extra={'git': str(stub)})
            with self.subTest(signal=signal_name):
                if pids.exists():
                    pids.unlink()

                def outputs(old, new):
                    # 代用品が記録した自分の $$(旧版・Rust の順)。旧bashは INT と PIPE の終わりを知らせない。
                    recorded = [int(line) for line in pids.read_text().split()]
                    if len(recorded) != 2:
                        raise Difference(f'代用品の番号の記録が2つでない: {recorded}')
                    # bash 5.2.21 は DONT_REPORT_SIGPIPE・DONT_REPORT_SIGTERM で作られ(config-top.h)、INT も
                    # 知らせない。知らせるのは USR1 と HUP。
                    job_pid_check(old, new, {'git': recorded[0]}, {'git': recorded[1]},
                                  expected_roles=['git'] if signal_name in ('USR1', 'HUP') else [])
                self.pair(w.save(), env=w.env(path=bindir), outputs=outputs)

    def test_exit_trap_rm_failure_runs_the_err_trap_on_signals(self):
        """信号の経路の EXIT の罠で rm が無いとき、残っている ERR の罠の行も出す(終わり方は信号のまま)。
        標準エラーが書けない(/dev/full)組では、ERR の罠の echo も失敗する(EXIT の罠はその終了値を受ける)。"""
        w = self.world
        real_git = shutil.which('git', path=w.real_path)
        stub = w.base / 'git-hold-rm'
        stub.write_text('#!/bin/sh\necho $$ >"$R4G_NOTIFY"\nread x <"$R4G_RELEASE"\nexec "%s" "$@"\n' % real_git)
        stub.chmod(0o755)
        bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'mkdir', 'cut', extra={'git': str(stub)})
        env = w.env(path=bindir)
        keep_origin(w)
        # /dev/full は Linux だけ(Mac では標準エラーが書ける組だけ。/dev/full の組は未実施と記録する)。
        launches = (None, {'redirect': {'2': '/dev/full'}}) if sys.platform.startswith('linux') else (None,)
        if not sys.platform.startswith('linux'):
            not_run(self, '標準エラーが /dev/full の組', 'Linux だけ(/dev/full)')
        # 群への TERM・HUP は、git の代用品の終わりとシェルの EXIT の罠が競う(知らせの行〈HUP は番号つき・TERM は
        # 語だけ〉が出るかが変わる)ので、順序を決めて流す: git-first(代用品が終わったのを確かめてからシェルが信号を
        # 処理する)・shell-first(シェルが終わってから代用品が終わる)。
        cases = [(target, number, launch) for launch in launches
                 for target in ('process', 'group', 'git-first', 'shell-first')
                 for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
                 if target == 'process' or (target in ('git-first', 'shell-first')) == (number in (signal.SIGTERM, signal.SIGHUP))]
        for target, number, launch in cases:
            with self.subTest(target=target, signal=number, launch=launch):
                results = []
                for index, argv in enumerate((reviews_argv(w.save()), reviews_native(w.save()))):
                    reset_world(w, index)
                    if target in ('process', 'group'):
                        result, remained, stub_pid, _ = signal_run(w, argv, env, target, number, launch=launch)
                    else:
                        result, remained, stub_pid = ordered_git_signal_run(w, argv, env, target, number, launch)
                    check_fixed_mktemp(w.log)
                    results.append((result, remained, tree_files(w.work), stub_pid))
                (old, old_remained, old_state, old_pid), (new, new_remained, new_state, new_pid) = results
                # 標準エラーが書けない組(/dev/full)では、知らせの行は見えない。
                expected = {'git-first': ['git'] if launch is None and number == signal.SIGHUP else [],
                            'shell-first': []}.get(target)
                job_pid_check(old, new, {'git': old_pid}, {'git': new_pid}, expected_roles=expected)
                self.assertEqual((old.returncode, old_remained, old_state), (new.returncode, new_remained, new_state))

    def test_hash_remembers_found_tools(self):
        """旧bashの hash: 最上位で見つけた python3・mkdir は、2回目に探し直さない(前の PATH に別の道具が
        現れても・覚えた道具が消えても)。サブシェル(コマンド置換)は親が覚えた場所を使う。"""
        w = self.world
        real_python = shutil.which('python3', path=w.real_path)
        real_mkdir = shutil.which('mkdir', path=w.real_path)
        keep_origin(w)
        cases = {
            'python-shadowed': ('python3', real_python, 'printf "#!/bin/sh\\nexit 9\\n" >"$R4G_EARLY/python3"; '
                                                         'chmod +x "$R4G_EARLY/python3"'),
            'python-removed': ('python3', real_python, 'rm -f "$0"; ln -s "%s" "$R4G_LATE/python3"' % real_python),
            'python-noexec': ('python3', real_python, 'rm -f "$0"; printf "x" >"$0"'),
            'mkdir-shadowed': ('mkdir', real_mkdir, 'printf "#!/bin/sh\\nexit 7\\n" >"$R4G_EARLY/mkdir"; '
                                                    'chmod +x "$R4G_EARLY/mkdir"'),
        }
        for label, (tool, real, action) in cases.items():
            with self.subTest(case=label):
                results = []
                for index, argv in enumerate((reviews_argv(w.save()), reviews_native(w.save()))):
                    reset_world(w, index)
                    early, middle, late = (w.base / f'hash-{label}-{name}' for name in ('early', 'mid', 'late'))
                    for directory in (early, middle, late):
                        if directory.exists():
                            shutil.rmtree(directory)
                        directory.mkdir()
                    stub = middle / tool
                    # 1回目の起動で前の PATH を変えてから本物へ渡す(覚えた場所が使われるか)。
                    stub.write_text('#!/bin/sh\nif [ ! -e "$R4G_DONE" ]; then : >"$R4G_DONE"; %s; fi\nexec "%s" "$@"\n'
                                    % (action, real))
                    stub.chmod(0o755)
                    done = w.base / f'hash-{label}-done'
                    if done.exists():
                        done.unlink()
                    bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut', 'git')
                    path = os.pathsep.join([str(early), str(middle), bindir, str(late)])
                    env = w.env(path=path, R4G_EARLY=str(early), R4G_LATE=str(late), R4G_DONE=str(done))
                    if w.log.exists():
                        w.log.unlink()
                    result = run_command(argv, cwd=str(w.work), env=env, timeout=120)
                    results.append((result, read_calls(w.log), tree_files(w.work)))
                (old, old_calls, old_state), (new, new_calls, new_state) = results
                compare(REVIEWS, old, new)
                same_calls(REVIEWS, pipeline_order(old_calls), pipeline_order(new_calls), case='reviews-dir/calls',
                           env_as_set=True)
                self.assertEqual(old_state, new_state)

    def test_publish_output_with_nul_and_python_failure_lines(self):
        """公開の python3 のコマンド置換(148〜153 行)の中の診断と NUL の警告は、旧bashでは 153 行。"""
        w = self.world
        real_python = shutil.which('python3', path=w.real_path)
        nul = w.base / 'python-nul'
        nul.write_text('#!/bin/sh\ncase "$*" in *os.replace*) "%s" "$@"; rc=$?; printf "a\\000b\\n"; exit $rc;; esac\n'
                       'exec "%s" "$@"\n' % (real_python, real_python))
        nul.chmod(0o755)
        fail = w.base / 'python-fail'
        fail.write_text('#!/bin/sh\ncase "$*" in *os.replace*) printf "boom\\000\\nsecond\\n" >&2; exit 3;; esac\n'
                        'exec "%s" "$@"\n' % real_python)
        fail.chmod(0o755)
        for label, target in (('nul', nul), ('fail-nul', fail)):
            with self.subTest(case=label):
                bindir = w.bin_with('mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut', 'git',
                                    extra={'python3': str(target)})
                self.pair(w.save(), env=w.env(path=bindir), calls=False)

    def test_stub_observes_inherited_signals_and_fds(self):
        """対照: 記録の代用品が、子が継いだ信号の扱い(無視・mask)と開いた fd を見分けられる(旧版どうしで、
        起動の扱いだけを変えた組を不合格にする)。"""
        w = self.world
        if not sys.platform.startswith('linux'):
            not_run(self, None, 'Linux だけ(子の信号の扱いと fd を /proc で観測する)')
            return
        runs = {}
        keep_origin(w)
        for index, (label, launch) in enumerate((('plain', None), ('sigpipe-ignored', {'ignore_sigpipe': True}),
                                                 ('extra-fd', {'keep_fd': 5}), ('blocked', {'block': ['SIGUSR1']}))):
            reset_world(w, 0 if index == 0 else 1)
            if w.log.exists():
                w.log.unlink()
            run_command(launched(reviews_argv(w.save()), launch), cwd=str(w.work), env=w.env(), timeout=120)
            runs[label] = read_calls(w.log)
        self.assertTrue(runs['plain'])
        self.assertEqual(runs['plain'][0][4]['SigIgn'], '0000000000000000')
        self.assertEqual(runs['blocked'][0][4]['SigBlk'], '%016x' % (1 << (signal.SIGUSR1 - 1)))
        for label in ('sigpipe-ignored', 'extra-fd', 'blocked'):
            with self.subTest(variant=label), self.assertRaises(Difference):
                same_calls(REVIEWS, pipeline_order(runs['plain']), pipeline_order(runs[label]),
                           case='reviews-dir/calls', env_as_set=True)


    def test_script_variables_exported_by_the_caller(self):
        """2026-11-12 14:24 の決定 4: 呼出元が旧スクリプトの変数と同じ名前を export すると、旧bashは子の起動の
        時点の値を渡す(関数の local・罠の中の代入・コマンド置換の中の子・EXIT の罠の子を含む。配列にする
        GIT_PRE は子の環境から消える)。値はどれも Rust で作れるので再現し、比較から外さない。成功・使い方・
        ensure・各失敗の経路で、子の環境の集合を全部比べる。LINENO も export した組は、代入で子の環境を
        作り直す所(array_needs_making)も比べる。"""
        w = self.world
        exported = {name: 'caller-' + name for name in REVIEWS_SCRIPT_NAMES}
        real_git = shutil.which('git', path=w.real_path)
        cut_fail = w.base / 'cut-fail'
        cut_fail.write_text('#!/bin/sh\nexit 3\n')
        mv_no_t = w.base / 'mv-no-t'
        mv_no_t.write_text('#!/bin/sh\necho "mv: illegal option -- T" >&2\nexit 64\n')
        race = w.base / 'git-dir-race'
        race.write_text('#!/bin/sh\n"%s" "$@"\nrc=$?\nmkdir -- "$RDP_RACE_LIST"\nexit $rc\n' % real_git)
        for path in (cut_fail, mv_no_t, race):
            path.chmod(0o755)
        tools = ('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut', 'git')

        def without(*names, extra=None):
            return w.bin_with(*[tool for tool in tools if tool not in names and tool not in (extra or {})],
                              extra=extra)
        fresh = w.work / 'fresh'
        fresh.mkdir()
        cases = (
            ('save', w.save(), None),
            ('ensure', ['ensure', '--root', str(fresh)], None),
            ('help', ['-h'], None),
            ('unknown-argument', ['save-untracked', '--bogus'], None),
            ('missing-value', ['save-untracked', '--root'], None),
            ('git-fail', w.save(top=w.outside, root=w.repo), None),
            ('sha-fail', w.save(), without(extra={'cut': str(cut_fail)})),
            ('publish-python-fail', w.save('raced'), without(extra={'git': str(race)})),
            ('publish-mv-fail', w.save(), without('python3', extra={'mv': str(mv_no_t)})),
            ('no-sha', w.save(), without('sha256sum')),
        )
        keep_origin(w)
        for with_lineno in (False, True):
            for label, args, path in cases:
                changes = dict(exported, RDP_RACE_LIST=str(w.reviews / 'raced'))
                if with_lineno:
                    changes['LINENO'] = 'caller-LINENO'
                with self.subTest(case=label, lineno=with_lineno):
                    reset_world(w, 0)
                    old, _, calls = self.pair(args, env=w.env(path=path, **changes))
                    seen = {}
                    for call in calls:
                        tool = os.path.basename(os.fsdecode(bytes.fromhex(call[0])))
                        env = {bytes.fromhex(item).partition(b'=')[0]: bytes.fromhex(item).partition(b'=')[2]
                               for item in call[2]}
                        seen.setdefault(tool, []).append(env)
                        # 配列にした GIT_PRE は、どの子にも渡らない。
                        self.assertNotIn(b'GIT_PRE', env)
                    if not with_lineno:
                        self.script_variable_controls(label, old, seen)

    def script_variable_controls(self, label, old, seen):
        """陽性の対照: 旧版の子の環境に、その経路で代入した値が実際に出ていること(比べた組が空でないこと)。"""
        def values(tool, name):
            return [env.get(name.encode()) for env in seen.get(tool, [])]
        w = self.world
        if label == 'save':
            self.assertEqual(old.returncode, 0)
            self.assertEqual(values('git', 'rc'), [b'0'])
            self.assertEqual(values('git', 'PUBLISH'), [b'os.replace'])
            self.assertEqual(values('git', 'SHA_CMD'), [b'sha256sum'])
            self.assertEqual(values('mktemp', 'TMP'), [b''])
            self.assertEqual(values('sha256sum', 'SHA'), [b'caller-SHA'])
            self.assertEqual(values('python3', 'NL')[-1:], [b'\n'])
            self.assertEqual(values('python3', 'reason')[-1:], [b'caller-reason'])
            self.assertEqual(values('python3', 'NAME')[-1:], [b'x.z'])
            self.assertEqual(values('python3', 'SUB')[-1:], [b'save-untracked'])
            self.assertEqual(values('mkdir', 'd'), [os.fsencode(str(w.repo / '.claude')),
                                                     os.fsencode(str(w.repo / '.claude/reviews'))])
            self.assertEqual(values('git', 'd'), [b'caller-d'])  # 関数から戻ると元の値
        elif label == 'ensure':
            self.assertEqual(values('mkdir', 'd'), [os.fsencode(str(w.work / 'fresh/.claude')),
                                                     os.fsencode(str(w.work / 'fresh/.claude/reviews'))])
        elif label == 'help':
            self.assertEqual(values('sed', 'SUB'), [b'-h', b'-h'])
            self.assertEqual(values('sed', 'ROOT'), [b'', b''])
        elif label == 'git-fail':
            self.assertEqual(old.returncode, 4)
            self.assertEqual([(env.get(b'code'), env.get(b'reason'), env.get(b'rc')) for env in seen['rm']],
                             [(b'4', b'git', b'128')])
        elif label == 'sha-fail':
            self.assertEqual(old.returncode, 20)
            # サブシェルの ERR の罠が 20 で終え、親の ERR の罠が ec=20 を置く。
            self.assertEqual(values('rm', 'ec'), [b'20'])
            self.assertEqual(values('rm', 'SHA'), [b''])
        elif label in ('publish-python-fail', 'publish-mv-fail'):
            self.assertEqual(old.returncode, 20)
            self.assertEqual([(env.get(b'code'), env.get(b'reason')) for env in seen['rm']], [(b'20', b'internal')])
            self.assertEqual(values('rm', 'PUBLISH'), [b'os.replace' if label == 'publish-python-fail' else b'mv'])
        elif label == 'no-sha':
            self.assertEqual(old.returncode, 20)
            self.assertEqual(values('python3', 'PUBLISH'), [b'mv'])

    def test_bash_epoch_variables_in_the_parent_environment(self):
        """親の環境の EPOCHSECONDS・EPOCHREALTIME(2026-11-12 06:58 の決定 time-valued-env): 旧bashは子の環境へ
        書き出した時点の時刻を渡す。子の環境のその値の欄だけ、表記の形と実行の時刻の間にあることを確かめ、
        ほかは全部比べる。"""
        w = self.world
        for name, value in (('EPOCHSECONDS', '1'), ('EPOCHREALTIME', '1.5'), ('both', None)):
            env = w.env(EPOCHSECONDS='1', EPOCHREALTIME='1.5') if value is None else w.env(**{name: value})
            names = (b'EPOCHSECONDS', b'EPOCHREALTIME') if value is None else (name.encode(),)
            with self.subTest(case=name):
                _, _, calls = self.pair(w.save(), env=env, time_vars=names)
                for wanted in names:
                    passed = sum(1 for call in calls if (wanted + b'=<time>').hex() in call[2])
                    # 確かめが空にならないこと: 子(git・sed など)へ渡った回が1回以上ある。
                    self.assertGreater(passed, 0, wanted)
                    OBSERVATIONS.append(f'reviews-dir: 親の環境の {wanted.decode()}: 子 {passed} 回へ実行の時刻の値を'
                                        '渡した(旧版・Rust。形と時刻の間を確かめた)')

    def test_hidden_proc(self):
        """/proc の無い環境(新しい利用者とマウントの名前空間で、/proc を空の tmpfs で隠す)。旧 reviews-dir は
        /proc に頼らない。Rust も同じく動くこと(コマンド置換のサブシェルの fork を含む)。記録の代用品は /proc を
        読むので、本物の道具だけの PATH で流す。Linux だけ。名前空間を作れない機械は未実施と記録する。"""
        w = self.world
        if not sys.platform.startswith('linux'):
            not_run(self, None, 'Linux だけ(/proc を隠す名前空間)')
            return
        unshare = shutil.which('unshare', path=w.real_path)
        hide = [unshare, '-rm', 'sh', '-c', 'mount -t tmpfs r4g-no-proc /proc && test ! -e /proc/self && exec "$@"',
                'r4g-hide-proc'] if unshare else []
        probe = subprocess.run([*hide, 'true'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) if hide else None
        if probe is None or probe.returncode != 0:
            reason = probe.stderr.decode(errors='replace').strip() if probe is not None else 'unshare が無い'
            userns_unavailable(self, reason)
            return
        keep_origin(w)
        # 一覧に一時ファイル自身の名前が入るので、mktemp は決まった名前で作る小さな代用品にする(/proc を読まない)。
        fixed = w.base / 'noproc-bin'
        fixed.mkdir(exist_ok=True)
        (fixed / 'mktemp').write_text('#!/bin/sh\n[ "$#" -eq 1 ] || exit 1\nn="${1%XXXXXX}r4gfix"\n'
                                      '(umask 077; set -C; : >"$n") || exit 1\nprintf "%s\\n" "$n"\n')
        (fixed / 'mktemp').chmod(0o755)
        path = str(fixed) + os.pathsep + w.real_path
        for label, args in (('save', w.save()), ('ensure', ['ensure', '--root', str(w.repo)]), ('usage', ['x'])):
            with self.subTest(case=label):
                reset_world(w, 0)
                old, _, _ = self.pair(args, env=w.env(path=path), calls=False, wrap=hide)
                OBSERVATIONS.append(f'reviews-dir: /proc を隠す組 {label}: 終了値 {old.returncode}(旧版と Rust で同じ)')


class ReviewsFilterEnv(unittest.TestCase):
    """完了条件6(reviews-dir-filter-env): 呼出側の GIT_CONFIG_* が Git に効き、core.fsmonitor のコマンドが
    起動しない。旧版と Rust の両方で、起動の列も比べる。"""

    def test_caller_git_config_is_kept_and_fsmonitor_is_not_started(self):
        require_tool_baseline(tool_versions())
        w = ReviewsWorld(self)
        marker = w.base / 'fsmonitor.marker'
        hook = w.base / 'fsmonitor.sh'
        hook.write_text('#!/bin/sh\necho ran >>"%s"\nexit 1\n' % marker)
        hook.chmod(0o755)
        subprocess.run(['git', '-C', str(w.repo), 'config', 'core.fsmonitor', str(hook)], check=True)
        (w.repo / 'excluded.txt').write_bytes(b'e\n')
        excludes = w.base / 'excludes'
        excludes.write_bytes(b'excluded.txt\n')
        env = w.env(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.excludesFile', GIT_CONFIG_VALUE_0=str(excludes))
        # 陽性の対照: 前置きの無い git ls-files -o は、この設定で fsmonitor のコマンドを起動する。
        control = run_command([shutil.which('git', path=w.real_path), '-C', str(w.repo), 'ls-files', '-o',
                               '--exclude-standard', '-z'], cwd=str(w.work), env=env, timeout=60)
        self.assertEqual(control.returncode, 0, control.stderr)
        if not marker.exists():
            raise Difference('対照: 前置きの無い git ls-files -o が fsmonitor のコマンドを起動しない(検査が働かない)')
        marker.unlink()
        OBSERVATIONS.append('reviews-dir-filter-env 対照: 前置きの無い git ls-files -o は fsmonitor を起動した')
        for side, argv in (('old', reviews_argv(w.save())), ('new', reviews_native(w.save()))):
            if (w.reviews / 'x.z').exists():
                (w.reviews / 'x.z').unlink()
            if w.log.exists():
                w.log.unlink()
            result = run_command(argv, cwd=str(w.work), env=env, timeout=60)
            self.assertEqual(result.returncode, 0, (side, result.stderr))
            names = (w.reviews / 'x.z').read_bytes().split(b'\0')
            self.assertIn(b'untracked.txt', names, side)
            self.assertNotIn(b'excluded.txt', names, side)
            self.assertFalse(marker.exists(), side)
            git_calls = [call for call in read_calls(w.log) if os.path.basename(bytes.fromhex(call[0])) == b'git']
            self.assertEqual(len(git_calls), 1, side)
            env_names = {bytes.fromhex(item).split(b'=', 1)[0] for item in git_calls[0][2]}
            self.assertTrue({b'GIT_CONFIG_COUNT', b'GIT_CONFIG_KEY_0', b'GIT_CONFIG_VALUE_0'} <= env_names, side)
            self.assertIn(b'GIT_NO_LAZY_FETCH=1'.hex(), git_calls[0][2], side)
            OBSERVATIONS.append(f'reviews-dir-filter-env {side}: 一覧に excluded.txt なし・fsmonitor の起動なし')


def process_state(pid):
    """プロセスの状態の1文字(R・S・T・Z など)。無ければ None。Linux は /proc、ほかの機種は ps で読む。"""
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/%d/stat' % pid) as handle:
                return handle.read().split(') ', 1)[1][0]
        except OSError:
            return None
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    text = result.stdout.strip()
    return text[:1].decode('ascii') if result.returncode == 0 and text else None


def pid_alive(pid):
    return process_state(pid) not in (None, 'Z')


def process_parent(pid):
    """プロセスの親の番号。無ければ None。Linux は /proc、ほかの機種は ps で読む。"""
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/%d/stat' % pid) as handle:
                return int(handle.read().split(') ', 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None
    result = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def group_report(pgid):
    """失敗の調べのため、群のプロセスの番号・親・状態・コマンドと、Linux では待つ所と信号の印を並べる。"""
    snapshot = subprocess.run(['ps', '-e', '-o', 'pid=,ppid=,pgid=,stat=,args='], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout.decode(errors='replace')
    lines = []
    for line in snapshot.splitlines():
        fields = line.split()
        if fields[2:3] != [str(pgid)]:
            continue
        extra = ''
        if sys.platform.startswith('linux'):
            try:
                with open('/proc/%s/wchan' % fields[0]) as handle:
                    extra += ' wchan=' + handle.read().strip()
                with open('/proc/%s/status' % fields[0]) as handle:
                    extra += ' ' + ' '.join(item.replace('\t', '') for item in handle.read().splitlines()
                                            if item.startswith(('SigPnd', 'ShdPnd', 'SigBlk', 'SigIgn', 'SigCgt')))
            except OSError:
                pass
        lines.append(line[:160] + extra)
    return lines


def wait_sleeping_in(pid, functions, what, timeout=15):
    """プロセスが、カーネルの待つ関数(`pipe_read`・`do_wait` など)の中で眠るのを待つ。旧bashは、信号を
    確かめてから read・waitpid に入るまでの間に信号を受けると、その呼出しで待ち続ける(信号の処理が後になる)。
    信号を送る前に、待つ呼出しの中にいることを確かめて、この隙間に当たらないようにする。Linux は
    /proc/<pid>/wchan で読む。ほかの機種は読めないので、0.5 秒待つだけにする(揺れうる。Mac では道具の版の
    基準が無いので reviews-dir の節は未実施になるが、implementation-git の信号の組〈ig_signal_run〉が
    wait_stub_blocked を通して使い、0.5 秒待つだけになる)。

    旧 bash の性質(事実): SIGCHLD の関数(SA_RESTART)の中で受けた終わらせる信号の処理は、読んでいる pipe が
    閉じるまで遅れる。Rust には SIGCHLD の関数が無いので、これを再現しない。順序を決めた比較では、この前提
    (処理していない SIGCHLD が無いこと)で避ける。順序を決めない流し方で出力や終了値に差が出たら不合格になる
    (一時ファイルが残るかどうかの差は 2026-11-12 06:58 の決定 race-unordered-outcome の範囲)。"""
    if not sys.platform.startswith('linux'):
        time.sleep(0.5)
        return
    deadline = time.monotonic() + timeout
    seen = None
    while time.monotonic() < deadline:
        asleep, seen = sleeping_in(pid, functions)
        if asleep:
            return
        time.sleep(0.005)
    raise Difference(f'{what}: 待つ呼出しの中で眠らない(wchan {seen}・状態 {process_state(pid)})')


def sleeping_in(pid, functions):
    """Linux: プロセスが、`functions` のどれかの中で眠り(状態 S)、届いたまま処理していない SIGCHLD が無いか。
    戻り値は (真偽, 読んだ wchan)。"""
    chld = 1 << (signal.SIGCHLD - 1)
    try:
        with open('/proc/%d/wchan' % pid) as handle:
            seen = handle.read().strip()
        with open('/proc/%d/status' % pid) as handle:
            status = {line.split(':')[0]: int(line.split()[1], 16) for line in handle.read().splitlines()
                      if line.startswith(('SigPnd:', 'ShdPnd:', 'SigBlk:'))}
    except (OSError, IndexError, ValueError):
        seen, status = None, {}
    # 届いたまま処理していない SIGCHLD(止めていないもの)があれば、まだ待つ(旧bashの SIGCHLD の関数は
    # SA_RESTART なので、その関数の中で受けた信号では、読む呼出しが中断されずに続き、信号の処理が後になる)。
    unhandled = (status.get('SigPnd', 0) | status.get('ShdPnd', 0)) & ~status.get('SigBlk', 0) & chld
    return seen in functions and process_state(pid) == 'S' and not unhandled, seen


# 信号を送る前に、群の全員が眠っていてよい待つ所(コマンド置換の pipe を読む・子を待つ・FIFO を開いて待つ)。
GROUP_WAITS = ('pipe_read', 'do_wait', 'wait_for_partner', 'fifo_open')


def group_members(pgid):
    """Linux: 群 `pgid` に属するプロセスの番号(/proc/<pid>/stat の群の欄で選ぶ)。"""
    members = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % name) as handle:
                fields = handle.read().rsplit(')', 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[2] == str(pgid):
            members.append(int(name))
    return members


def wait_group_settled(pgid, what, functions=GROUP_WAITS, timeout=15):
    """群の全員が、`functions` のどれかで眠るか、終わった(ゾンビ)か、止まっているまで待つ。待たせた代用品と
    シェルのほかに、パイプラインの隣の子(sha256sum | cut の sha256sum と、その記録の代用品の Python)が
    まだ動いていれば、信号がその起動の途中(Python の初期化など)に当たるかが負荷で変わり、出力が揺れる。
    旧版と Rust の差ではなく比較器の同期の不足なので、送る前にここまで待つ。Linux 以外は 0.5 秒待つだけ。"""
    if not sys.platform.startswith('linux'):
        time.sleep(0.5)
        return
    deadline = time.monotonic() + timeout
    busy = []
    while time.monotonic() < deadline:
        busy = []
        for pid in group_members(pgid):
            if process_state(pid) in (None, 'Z', 'T', 't'):
                continue
            asleep, seen = sleeping_in(pid, functions)
            if not asleep:
                busy.append((pid, seen, process_state(pid)))
        if not busy:
            return
        time.sleep(0.005)
    raise Difference(f'{what}: 群の子が待つ所で眠らない({busy}・群: {group_report(pgid)})')


def wait_stub_blocked(pid, what='代用品が FIFO を開いて待つ'):
    """待たせる代用品(`echo $$ >知らせ; read x <放す FIFO`)が、知らせを書いた後に放す FIFO を開いて待つ所
    (`wait_for_partner`)まで進んだのを待つ。知らせは開く前に届くので、その間に信号を送ると、代用品の dash が
    開く前に信号を受け(開く呼出しが中断されない)、中断されたときと違う出力になる。旧版と Rust の差ではなく、
    比較器の同期の不足なので、送る前にここまで待つ。確かめたカーネル(6.8.0 x86_64)では `wait_for_partner`。
    この関数が呼出し元へ埋め込まれたカーネルでは呼出し元の `fifo_open` になるので、両方を受け付ける。"""
    wait_sleeping_in(pid, ('wait_for_partner', 'fifo_open'), what)


def wait_state(pid, accept, what, timeout=15):
    """プロセスの状態が accept を満たすまで待つ。満たさなければ失敗。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if accept(process_state(pid)):
            return
        time.sleep(0.005)
    raise Difference(f'{what}: 待った状態にならない(状態 {process_state(pid)})')


def read_fifo(path, timeout=15):
    """FIFO の知らせを、時間を限って読む(書き手が来なければ、自分で書き手として開いて解き、失敗にする)。"""
    import threading
    box = []

    def reader():
        with open(path, 'rb') as handle:
            box.append(handle.read())
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        try:
            os.close(os.open(str(path), os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        thread.join(5)
        raise Difference(f'知らせが来ない: {path.name}')
    return box[0]


def release_fifo(path):
    """FIFO を書き手として開いて閉じ、読んで待つ代用品を放す(読む側が無ければ何もしない)。"""
    try:
        os.close(os.open(str(path), os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def stderr_still_open(process):
    """シェルが終わった時点で、標準エラーの終わり(EOF)がまだ来ないか(ほかのプロセスが持っているか)。
    読めた分を返す。"""
    import fcntl
    early = b''
    fd = process.stderr.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    still_open = True
    while True:
        try:
            part = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not part:
            still_open = False
            break
        early += part
    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    return still_open, early


def race_at_shell_end(old_raw, new_raw, counts, label):
    """2026-11-12 06:58 の決定(race-unordered-outcome。順序を決めない群への信号): シェルが終わった時点で
    代用品が残っていたか・標準エラーが開いていたかの一致は求めず、範囲だけを確かめて `counts` へ数える。
    範囲: 2つの値がどちらも真偽値であること(どちらの値もありうる)。放さずに待った後の値と終了値・出力・
    状態は、呼出側が旧版との一致を求める。どちらかの側でシェルが3秒で終わらなければ(値が None)、シェルの
    終わりとの競りが無いので数えず、呼出側が全部の一致を求める。"""
    applied(REVIEWS, 'race-unordered-outcome', 'reviews-dir/comsub-group-race')
    for side, raw in (('旧版', old_raw), ('Rust', new_raw)):
        if not isinstance(raw, dict) or set(raw) != {'remained', 'stderr_open'}:
            raise Difference(f'{label}: {side} のシェルが終わった時点の値が無い')
        if not all(raw[key] is None or isinstance(raw[key], bool) for key in raw) or \
                (raw['remained'] is None) != (raw['stderr_open'] is None):
            raise Difference(f'{label}: {side} のシェルが終わった時点の値が範囲の外: {raw}')
    if old_raw['stderr_open'] is None or new_raw['stderr_open'] is None:
        return
    counts['runs'] += 1
    counts['old_remained'] += old_raw['remained']
    counts['new_remained'] += new_raw['remained']
    counts['old_open'] += old_raw['stderr_open']
    counts['new_open'] += new_raw['stderr_open']
    if old_raw != new_raw:
        counts['differ'] += 1
        OBSERVATIONS.append(f'reviews-dir 順序を決めない群への信号 {label}: シェルが終わった時点で '
                            f'代用品 旧版={"残る" if old_raw["remained"] else "無い"}・'
                            f'Rust={"残る" if new_raw["remained"] else "無い"}、標準エラー '
                            f'旧版={"開く" if old_raw["stderr_open"] else "閉じる"}・'
                            f'Rust={"開く" if new_raw["stderr_open"] else "閉じる"}')


def unordered_race_states(old_state, new_state, place=b'.claude/reviews'):
    """2026-11-12 06:58 の決定(race-unordered-outcome): 順序を決めない群への HUP では、一時ファイルが残るかの
    一致は求めない。残った物は、旧版の残しうる物(置き場 `place`〈状態の根からの相対〉の直下の
    .base-untracked.<6文字>。mktemp の代用品の決まった名前で、両方で同じ1つの名前)の範囲に収まり、両方に
    残った物は中身まで同じで、ほかの状態は全部同じこと。戻り値は (旧版に残った物, Rust に残った物)。"""
    applied(REVIEWS, 'race-unordered-outcome', 'reviews-dir/hup-comsub-race')
    pattern = re.compile(rb'\A' + re.escape(place) + rb'/\.base-untracked\.[A-Za-z0-9]{6}\Z')

    def split(state):
        left = {key: value for key, value in state.items() if pattern.match(key)}
        return left, {key: value for key, value in state.items() if key not in left}
    (old_left, old_rest), (new_left, new_rest) = split(old_state), split(new_state)
    if old_rest != new_rest:
        keys = sorted(key for key in set(old_rest) | set(new_rest) if old_rest.get(key) != new_rest.get(key))
        raise Difference('一時ファイルのほかの状態が違う: ' + ', '.join(os.fsdecode(key) for key in keys[:5]))
    if len(set(old_left) | set(new_left)) > 1:
        raise Difference('残った一時ファイルの名前が1つでない')
    if any(old_left[key] != new_left[key] for key in set(old_left) & set(new_left)):
        raise Difference('両方に残った一時ファイルの中身か種類が違う')
    return sorted(old_left), sorted(new_left)


# 群への HUP の順序(サブシェルが仕事の群へ送り直す HUP と、親の EXIT の罠の rm の前後)。止める相手と、
# 続ける時点を決めて、競合を作らずに流す。
HUP_ORDERS = ('subshell-first', 'during-rm', 'after-parent')


def ordered_hup_run(world, argv, env, order):
    """コマンド置換のサブシェルで子を待つ間の群への HUP を、順序を決めて流す。
    'subshell-first': 親を止めてから群へ送り、サブシェルが終わってから親を続ける(親が終わらせる信号を
      処理し始める前に、2つ目の HUP が着く)。
    'during-rm': サブシェルを止めてから群へ送り、親の EXIT の罠の rm が着いてから(代用品が待つ間)
      サブシェルを続ける(処理の途中に2つ目の HUP が着く)。
    'after-parent': サブシェルを止めてから群へ送り、親が終わってからサブシェルを続ける。
    代用品(群への HUP で終わる)の終わりと、親の終わりも競わせない: どの順でも、代用品が終わったのを確かめて
    から、止めた親かサブシェルを続ける('after-parent' は親も止めて送り、代用品が終わってから親を続ける)。
    代用品の知らせは「番号 親の番号」(親の番号はサブシェル)。戻り値は (結果, 代用品が残ったか,
    番号の役, 標準エラーが開いていたか)。'during-rm' の標準エラーは、サブシェルと親の終わりが競うので測らない。"""
    fifos = {name: world.base / name for name in ('notify', 'release', 'rm-notify', 'rm-release')}
    for fifo in fifos.values():
        if fifo.exists():
            fifo.unlink()
        os.mkfifo(str(fifo))
    env = dict(env, R4G_NOTIFY=str(fifos['notify']), R4G_RELEASE=str(fifos['release']),
               R4G_RM_NOTIFY=str(fifos['rm-notify']), R4G_RM_RELEASE=str(fifos['rm-release']))
    process = subprocess.Popen(argv, cwd=str(world.work), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stub_pid, subshell = (int(value) for value in read_fifo(fifos['notify']).split()[:2])
        roles = {stub_pid: 'stub', subshell: 'subshell'}
        # 知らせの親の番号が、本当にシェルの子(サブシェル)であること(止める相手を取り違えない)。
        if subshell == process.pid or process_parent(subshell) != process.pid:
            raise Difference(f'代用品の親がシェルの子でない(代用品 {stub_pid}・親 {subshell}・シェル {process.pid})')
        # 代用品は放す FIFO を開いて待ち、親はコマンド置換の pipe を読む呼出しの中、サブシェルは子を待つ呼出しの
        # 中にいること。
        wait_stub_blocked(stub_pid)
        wait_sleeping_in(process.pid, ('pipe_read',), f'{order}: 親が pipe を読む')
        wait_sleeping_in(subshell, ('do_wait',), f'{order}: サブシェルが子を待つ')
        wait_group_settled(process.pid, f'{order}: シェルの群が待つ')
        stopped = process.pid if order == 'subshell-first' else subshell
        os.kill(stopped, signal.SIGSTOP)
        wait_state(stopped, lambda state: state in ('T', 't'), f'{order}: 止める')
        if stopped != process.pid:
            # サブシェルが止まると、親へ SIGCHLD が届く。旧bashは、その関数(SA_RESTART)を終えて読む呼出しへ
            # 戻るまで待ってから送る(関数の中で受けた HUP では、読む呼出しが中断されない)。
            wait_sleeping_in(process.pid, ('pipe_read',), f'{order}: 親が pipe を読む(止めた後)')
        if order == 'after-parent':
            os.kill(process.pid, signal.SIGSTOP)
            wait_state(process.pid, lambda state: state in ('T', 't'), f'{order}: 親を止める')
        os.killpg(process.pid, signal.SIGHUP)
        ended = lambda state: state in (None, 'Z')  # noqa: E731
        if order == 'subshell-first':
            wait_state(subshell, ended, f'{order}: サブシェルの終わり')
            wait_state(stub_pid, ended, f'{order}: 代用品の終わり')
            os.kill(process.pid, signal.SIGCONT)
            process.wait(timeout=15)
        elif order == 'during-rm':
            roles[int(read_fifo(fifos['rm-notify']).split()[0])] = 'rm'
            wait_state(stub_pid, ended, f'{order}: 代用品の終わり')
            os.kill(subshell, signal.SIGCONT)
            wait_state(subshell, lambda state: state in (None, 'Z'), f'{order}: サブシェルの終わり')
            # 2つ目の HUP で rm が終わらなかったとき(HUP を無視する対照の変種)は、ここで放す。
            release_fifo(fifos['rm-release'])
            process.wait(timeout=15)
        else:
            wait_state(stub_pid, ended, f'{order}: 代用品の終わり')
            os.kill(process.pid, signal.SIGCONT)
            process.wait(timeout=15)
        stderr_open, early = stderr_still_open(process)
        if order == 'during-rm':
            stderr_open = None
        remained = pid_alive(stub_pid)
        if order == 'after-parent':
            os.kill(subshell, signal.SIGCONT)
    except (Difference, subprocess.TimeoutExpired) as error:
        # 失敗の調べのため、群のプロセスの状態を添える。
        group = group_report(process.pid)
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=60)
        raise Difference(f'{order}: {error}(群: {group}・標準エラー: {err[-300:]!r})') from error
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=60)
        raise
    for name in ('release', 'rm-release'):
        release_fifo(fifos[name])
    out, err = process.communicate(timeout=60)
    deadline = time.monotonic() + 30
    while any(pid_alive(pid) for pid in roles) and time.monotonic() < deadline:
        time.sleep(0.02)
    return RunResult(process.returncode, out, early + err, False), remained, roles, stderr_open


# 順序を決めない群への信号で、シェルが終わった後に、信号で終わるはずの代用品と標準エラーの持ち手(サブ
# シェル)が、放さなくても自分で終わるのを待つ上限(秒)。
SETTLE_SECONDS = 10


def settle_after_shell(process, stub_pid, stderr_open, early, seconds=SETTLE_SECONDS):
    """シェルが終わった後、代用品が終わり、標準エラーの終わり(EOF)が来るのを、放さずに待つ(上限 `seconds`)。
    戻り値は (待った後も代用品が残るか, 待った後も標準エラーが開いているか, 読めた分を足した early)。"""
    deadline = time.monotonic() + seconds
    while True:
        if stderr_open:
            stderr_open, more = stderr_still_open(process)
            early += more
        remained = pid_alive(stub_pid)
        if (not remained and not stderr_open) or time.monotonic() >= deadline:
            return remained, stderr_open, early
        time.sleep(0.02)


def signal_run(world, argv, env, target, signal_number, *, launch=None, raw=None):
    """代用品が着いたこと(FIFO の知らせ)を待ってから、信号を送る。送る相手はプロセスか群。
    シェルが終わった時点で、代用品が残っているかと、標準エラーがまだ開いているか(サブシェルが持つ)を
    記録し、その後に放して片付ける。戻り値は (結果, 代用品が残ったか, 代用品の番号, 標準エラーが開いていたか)。

    `raw`(辞書)を渡すと、順序を決めない群への信号の扱いにする: シェルが終わった時点の2つの値を `raw` へ
    入れ、放さずに `settle_after_shell` で待った後の値を返す(2026-11-12 06:58 の決定 race-unordered-outcome)。
    シェルが3秒で終わらなければ待たない(`raw` の値は None)。"""
    notify, release = world.base / 'notify', world.base / 'release'
    for fifo in (notify, release):
        if fifo.exists():
            fifo.unlink()
        os.mkfifo(str(fifo))
    env = dict(env, R4G_NOTIFY=str(notify), R4G_RELEASE=str(release))
    process = subprocess.Popen(launched(argv, launch), cwd=str(world.work), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    with open(notify, 'rb') as handle:
        stub_pid = int(handle.read().split()[0])
    wait_stub_blocked(stub_pid)
    # シェルは、子を待つか、コマンド置換の pipe を読む呼出しの中にいること(信号を確かめてから呼出しに入る
    # までの隙間に送らない)。
    wait_sleeping_in(process.pid, ('pipe_read', 'do_wait'), 'シェルが待つ')
    wait_group_settled(process.pid, 'シェルの群が待つ')
    if target == 'process':
        os.kill(process.pid, signal_number)
    else:
        os.killpg(process.pid, signal_number)
    early = b''
    stderr_open = None
    try:
        process.wait(timeout=3)
        # シェルは終わった。標準エラーの終わり(EOF)がまだ来ないなら、ほかのプロセスが持っている。
        stderr_open, early = stderr_still_open(process)
    except subprocess.TimeoutExpired:
        pass
    remained = pid_alive(stub_pid)
    if raw is not None:
        raw.update(remained=remained if stderr_open is not None else None, stderr_open=stderr_open)
        if stderr_open is not None:
            remained, stderr_open, early = settle_after_shell(process, stub_pid, stderr_open, early)
    # 放す(書き手として開いて閉じる)。代用品が既に終わっていれば、読む側が無いので開けない。
    if remained:
        try:
            os.close(os.open(str(release), os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
    out, err = process.communicate(timeout=60)
    deadline = time.monotonic() + 30
    while pid_alive(stub_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return RunResult(process.returncode, out, early + err, False), remained, stub_pid, stderr_open


def ordered_git_signal_run(world, argv, env, order, signal_number, launch=None):
    """git の代用品(FIFO で待つ)を待つ間の群への信号を、代用品とシェルの終わりの順を決めて流す。
    'git-first': シェルを止め、代用品へ送って終わった(ゾンビになった)のを確かめてから、シェルへ送って続ける
      (シェルは信号を処理する前に代用品が終わっている。EXIT の罠の子を待つ間に回収される)。
    'shell-first': 代用品を止めてから群へ送り、シェルが終わってから代用品を続ける(代用品はその後に終わる)。
    戻り値は (結果, シェルが終わった時点で代用品が残っていたか, 代用品の番号)。"""
    notify, release = world.base / 'notify', world.base / 'release'
    for fifo in (notify, release):
        if fifo.exists():
            fifo.unlink()
        os.mkfifo(str(fifo))
    env = dict(env, R4G_NOTIFY=str(notify), R4G_RELEASE=str(release))
    process = subprocess.Popen(launched(argv, launch), cwd=str(world.work), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stub_pid = int(read_fifo(notify).split()[0])
        wait_stub_blocked(stub_pid)
        # シェルは git を待つ呼出しの中にいること。
        wait_sleeping_in(process.pid, ('do_wait',), f'{order}: シェルが git を待つ')
        wait_group_settled(process.pid, f'{order}: シェルの群が待つ')
        if order == 'git-first':
            os.kill(process.pid, signal.SIGSTOP)
            wait_state(process.pid, lambda state: state in ('T', 't'), f'{order}: シェルを止める')
            os.kill(stub_pid, signal_number)
            wait_state(stub_pid, lambda state: state == 'Z', f'{order}: 代用品の終わり')
            os.kill(process.pid, signal_number)
            os.kill(process.pid, signal.SIGCONT)
            process.wait(timeout=15)
        else:
            os.kill(stub_pid, signal.SIGSTOP)
            wait_state(stub_pid, lambda state: state in ('T', 't'), f'{order}: 代用品を止める')
            os.killpg(process.pid, signal_number)
            process.wait(timeout=15)
        remained = pid_alive(stub_pid)
        if order == 'shell-first':
            os.kill(stub_pid, signal.SIGCONT)
    except (Difference, subprocess.TimeoutExpired) as error:
        group = group_report(process.pid)
        os.killpg(process.pid, signal.SIGKILL)
        out, err = process.communicate(timeout=60)
        raise Difference(f'{order}: {error}(群: {group}・標準エラー: {err[-300:]!r})') from error
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=60)
        raise
    release_fifo(release)
    out, err = process.communicate(timeout=60)
    deadline = time.monotonic() + 30
    while pid_alive(stub_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return RunResult(process.returncode, out, err, False), remained, stub_pid


def reset_world(world, index):
    """旧版(0)の前に入力の写しを取り、Rust(1)の前に写しへ戻す。テストの始めに取った元(`origin`)が
    あれば、旧版の前にも元へ戻す(前のケースが残した一時ファイルを持ち越さない)。"""
    origin = getattr(world, 'origin', None)
    if index == 0:
        if origin is not None:
            shutil.rmtree(world.work)
            cp_tree(origin, world.work)
        if world.pristine.exists():
            shutil.rmtree(world.pristine)
        cp_tree(world.work, world.pristine)
    else:
        shutil.rmtree(world.work)
        cp_tree(world.pristine, world.work)


def keep_origin(world):
    """テストの始めの入力を控える(以後の reset_world は、旧版の前に毎回ここへ戻す)。"""
    world.origin = world.base / 'origin'
    if world.origin.exists():
        shutil.rmtree(world.origin)
    cp_tree(world.work, world.origin)


def notice_pids(text):
    """子の終わり方の知らせの番号(伏せる前の値)。"""
    return [int(match.group('pid')) for match in JOB_PID.finditer(text)]


def archive_only_locale():
    """locale-archive にだけある地域設定の名前(/usr/lib/locale にディレクトリの無いもの)。無ければ C.utf8。"""
    try:
        names = subprocess.run(['locale', '-a'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return b'C.utf8'
    for name in names:
        if name not in (b'C', b'POSIX') and not (Path('/usr/lib/locale') / os.fsdecode(name)).exists():
            return name
    return b'C.utf8'


REVIEWS_ARCHIVE_LOCALE = archive_only_locale()

# コマンド置換の中で子を待つ位置(待たせる道具・合わせる引数・python3 を置くか)。
REVIEWS_COMSUB_POSITIONS = (('mktemp', '', True), ('sha256sum', '', True), ('cut', '', True),
                            ('python3', 'os.replace', True), ('mv', '', False))


def wait_signal_taken(pid, number, timeout=15):
    """Linux: 送った信号 `number` を、プロセスが受け取る(スレッドと共有の保留の印が消える)か、プロセスが
    終わるまで待つ。受け取らなければ失敗。"""
    bit = 1 << (number - 1)
    deadline = time.monotonic() + timeout
    pending = None
    while time.monotonic() < deadline:
        if process_state(pid) in (None, 'Z'):
            return
        try:
            with open('/proc/%d/status' % pid) as handle:
                fields = {line.split(':')[0]: int(line.split()[1], 16) for line in handle.read().splitlines()
                          if line.startswith(('SigPnd:', 'ShdPnd:'))}
        except (OSError, IndexError, ValueError):
            return
        pending = (fields.get('SigPnd', 0) | fields.get('ShdPnd', 0)) & bit
        if not pending:
            return
        time.sleep(0.002)
    raise Difference(f'送った信号 {number} を受け取らない(状態 {process_state(pid)})')


def blocked_stderr_signal(world, argv, env, signal_number):
    """子を待たない間の信号: 標準エラーの pipe を 4096 バイトにして先に満たし、シェルの最初の標準エラーへの
    write で止まったのを /proc/<pid>/syscall で確かめてから、プロセスへ信号を送る。"""
    import fcntl
    read_end, write_end = os.pipe()
    fcntl.fcntl(write_end, 1031, 4096)  # F_SETPIPE_SZ
    os.write(write_end, b'#' * 4096)
    process = subprocess.Popen(argv, cwd=str(world.work), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=write_end, start_new_session=True)
    os.close(write_end)
    number = {'x86_64': '1', 'aarch64': '64'}.get(os.uname().machine)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with open('/proc/%d/syscall' % process.pid) as handle:
                fields = handle.read().split()
        except OSError:
            fields = []
        # 旧bashは `echo ... >&2` を、fd 1 を fd 2 の写しにしてから書く(fd 1 か 2 への write)。
        if fields[:1] == [number] and fields[1:2] in (['0x1'], ['0x2']):
            break
        time.sleep(0.01)
    else:
        raise Difference('標準エラーの書込みで止まらない')
    process.send_signal(signal_number)
    # 送った信号を受け取る(保留の印が消える)まで、pipe を読まない。読むと、止まった書込みが進むのと信号で
    # 中断されるのとが競い、行が出るかが負荷で変わる(旧版と Rust の差ではない)。
    wait_signal_taken(process.pid, signal_number)
    chunks = []
    while True:
        part = os.read(read_end, 65536)
        if not part:
            break
        chunks.append(part)
    os.close(read_end)
    out = process.stdout.read()
    process.wait(timeout=30)
    err = b''.join(chunks)
    if not err.startswith(b'#' * 4096):
        raise Difference('先に満たした分が無い')
    return RunResult(process.returncode, out, err[4096:], False)


class ReviewsSignals(unittest.TestCase):
    """子を待つ間の INT・TERM・HUP を、送る相手(プロセス・群)と子の振る舞いの組ごとに比べる。git(最上位)と、
    コマンド置換のサブシェルの中で待つ位置(mktemp・sha256sum・cut・公開の python3・mv)と、子を待たない間。
    期待は旧版の結果(終了値・出力・一時ファイル・子の残り方・シェルの後も標準エラーが開いているか)。
    「子を残さない」は当てない(設計)。"""

    def run_pair(self, w, env, target, number, launch=None, label='', settle=False):
        """`target` が 'git-first'・'shell-first' なら、git の代用品とシェルの終わりの順を決めて流す
        (`ordered_git_signal_run`。標準エラーが開いているかは測らない)。`settle` なら、順序を決めない群への
        信号として流す(`signal_run` の `raw`)。各側は (結果, 代用品が残ったか, 状態, 標準エラーが開いていたか,
        代用品の番号, シェルが終わった時点の2つの値〈settle でなければ None〉)。"""
        results = []
        for index, argv in enumerate((reviews_argv(w.save()), reviews_native(w.save()))):
            reset_world(w, index)
            raw = {} if settle else None
            if target in ('git-first', 'shell-first'):
                result, remained, stub_pid = ordered_git_signal_run(w, argv, env, target, number, launch)
                stderr_open = None
            else:
                result, remained, stub_pid, stderr_open = signal_run(w, argv, env, target, number, launch=launch,
                                                                     raw=raw)
            check_fixed_mktemp(w.log)
            results.append((result, remained, tree_files(w.work), stderr_open, stub_pid, raw))
        return results

    def test_signals_while_waiting_for_git(self):
        require_tool_baseline(tool_versions())
        w = ReviewsWorld(self)
        real_git = shutil.which('git', path=w.real_path)
        variants = {
            'default': '#!/bin/sh\necho $$ >"$R4G_NOTIFY"\nread x <"$R4G_RELEASE"\nexec "%s" "$@"\n' % real_git,
            'traps-int': ('#!/bin/sh\ntrap \'exit 3\' INT\necho $$ >"$R4G_NOTIFY"\nread x <"$R4G_RELEASE"\n'
                          'exec "%s" "$@"\n' % real_git),
        }
        for name, body in variants.items():
            stub = w.base / ('git-' + name)
            stub.write_text(body)
            stub.chmod(0o755)
        keep_origin(w)
        # 群への TERM・HUP は、git の代用品の終わりと、シェルの EXIT の罠の rm を待つ間の回収が競う(知らせの行
        # 〈HUP は番号つき・TERM は語だけ〉が出るかが変わる)ので、順序を決めて流す(git-first・shell-first)。
        ordered = ('git-first', 'shell-first')
        cases = [(variant, target, number, launch)
                 for variant in variants for target in ('process', 'group', *ordered)
                 for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
                 for launch in (None,)
                 if target == 'process' or (target in ordered) == (number in (signal.SIGTERM, signal.SIGHUP))]
        cases.append(('default', 'process', signal.SIGTERM, {'ignore': ['SIGTERM']}))
        cases.append(('default', 'group', signal.SIGINT, {'ignore_sigint': True}))
        for variant, target, number, launch in cases:
            with self.subTest(variant=variant, target=target, signal=number, launch=launch):
                bindir = w.bin_with('python3', 'mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut',
                                    extra={'git': str(w.base / ('git-' + variant))})
                label = f'{variant}-{target}-{int(number)}-{launch}'
                (old, old_remained, old_state, old_open, old_pid, _), \
                    (new, new_remained, new_state, new_open, new_pid, _) = \
                    self.run_pair(w, w.env(path=bindir), target, number, launch, label)
                # git-first の HUP は、EXIT の罠の rm を待つ間に回収した git を番号つきで知らせる。
                expected = None
                if target in ordered:
                    expected = ['git'] if (target, number) == ('git-first', signal.SIGHUP) else []
                job_pid_check(old, new, {'git': old_pid}, {'git': new_pid}, expected_roles=expected)
                self.assertEqual((old.returncode, old_remained, old_open), (new.returncode, new_remained, new_open))
                self.assertEqual(old_state, new_state)

    def comsub_hold(self, w):
        """コマンド置換の各位置で子を待たせる PATH の組: (道具, 位置の代用品だけの PATH, EXIT の罠の rm も
        待たせる PATH)。待たせる代用品は「番号 親の番号」を知らせる(親の番号はサブシェル)。"""
        fixed = w.base / 'fixed-mktemp'
        if not fixed.exists():
            fixed.mkdir()
            write_stub(fixed, 'mktemp', w.log, real=shutil.which('mktemp', path=w.real_path), fixed_mktemp=True)
        hold_rm = w.base / 'hold-rm'
        hold_rm.write_text('#!/bin/sh\ncase "$*" in *.base-untracked.*) echo $$ >"$R4G_RM_NOTIFY"; '
                           'read x <"$R4G_RM_RELEASE";; esac\nexec "%s" "$@"\n' % shutil.which('rm', path=w.real_path))
        hold_rm.chmod(0o755)
        positions = []
        for tool, match, python in REVIEWS_COMSUB_POSITIONS:
            # mktemp は、待たせた後に決まった名前の代用品へ渡す(一時ファイルの名前を比べられるように)。
            real = str(fixed / 'mktemp') if tool == 'mktemp' else shutil.which(tool, path=w.real_path)
            stub = w.base / ('hold-' + tool)
            stub.write_text('#!/bin/sh\ncase "$*" in *%s*) echo $$ $PPID >"$R4G_NOTIFY"; read x <"$R4G_RELEASE";; '
                            'esac\nexec "%s" "$@"\n' % (match, real))
            stub.chmod(0o755)
            tools = ['mv', 'sha256sum', 'sed', 'mktemp', 'rm', 'mkdir', 'cut', 'git'] + (['python3'] if python else [])
            tools = [name for name in tools if name != tool]
            plain = w.bin_with(*tools, extra={tool: str(stub)})
            held = w.bin_with(*[name for name in tools if name != 'rm'], extra={tool: str(stub), 'rm': str(hold_rm)})
            positions.append((tool, plain, held))
        return positions

    def test_signals_inside_command_substitutions(self):
        """コマンド置換のサブシェルの中で子を待つ間。サブシェルは親と別のプロセスで、親が先に終わっても続く
        (ERR の罠を受け継ぎ、後から行を書く・標準エラーを持ち続ける)。群への HUP は、サブシェルが仕事の群へ
        HUP を送り直すので、親の EXIT の罠の rm と競う(一時ファイルが残るかは実行ごとに変わる)。ここでは
        2026-11-12 06:58 の決定(race-unordered-outcome)で比べ、残った回数を記録する。競合の順序ごとの結果は
        test_group_hup_in_command_substitutions_in_each_order が決めて全部比べる。

        群への信号(INT・TERM・HUP)では、代用品とサブシェルも信号で終わり、シェルの終わりと競う。シェルが
        終わった時点で代用品が残っていたか・標準エラーが開いていたかは実行ごとに変わるので、同じ決定で、
        その2つの一致は求めず、取りうる範囲だけを確かめて回数を記録する(2026-11-12 管理担当の指示。
        reviewer-final-a 2回目の指摘1)。範囲: シェルが3秒で終わり、その時点の2つの値はどちらもありうる。
        放さずに待った後(SETTLE_SECONDS まで)の値(代用品が残るか・標準エラーが開いているか)と、終了値・
        出力・状態は、旧版と同じこと。プロセスだけへの信号は、代用品とサブシェルが信号を受けないので競わず、
        全部を比べる。"""
        require_tool_baseline(tool_versions())
        w = ReviewsWorld(self)
        keep_origin(w)
        counts = {'old': 0, 'new': 0, 'differ': 0, 'runs': 0}
        group = {'runs': 0, 'old_remained': 0, 'new_remained': 0, 'old_open': 0, 'new_open': 0, 'differ': 0}
        for tool, bindir, _ in self.comsub_hold(w):
            for target in ('process', 'group'):
                for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                    label = f'{tool}-{target}-{int(number)}'
                    with self.subTest(case=label):
                        (old, old_remained, old_state, old_open, old_pid, old_raw), \
                            (new, new_remained, new_state, new_open, new_pid, new_raw) = \
                            self.run_pair(w, w.env(path=bindir), target, number, label=label,
                                          settle=target == 'group')
                        job_pid_check(old, new, {'stub': old_pid}, {'stub': new_pid})
                        if target == 'group':
                            race_at_shell_end(old_raw, new_raw, group, label)
                        self.assertEqual((old.returncode, old_remained, old_open), (new.returncode, new_remained, new_open))
                        if target == 'group' and number == signal.SIGHUP:
                            place = os.fsencode(os.path.relpath(w.repo / '.claude/reviews', w.work))
                            old_left, new_left = unordered_race_states(old_state, new_state, place)
                            counts['runs'] += 1
                            counts['old'] += bool(old_left)
                            counts['new'] += bool(new_left)
                            counts['differ'] += bool(old_left) != bool(new_left)
                            OBSERVATIONS.append(f'reviews-dir 順序を決めない群への HUP {label}: 一時ファイル '
                                                f'旧版={"残る" if old_left else "消える"}・Rust={"残る" if new_left else "消える"}')
                            continue
                        self.assertEqual(old_state, new_state)
        OBSERVATIONS.append(f'reviews-dir 順序を決めない群への HUP(race-unordered-outcome): {counts["runs"]} 回のうち、'
                            f'一時ファイルが残った回 旧版 {counts["old"]}・Rust {counts["new"]}、'
                            f'残るかが違った回(不一致〈一時ファイル〉){counts["differ"]}')
        OBSERVATIONS.append(f'reviews-dir 順序を決めない群への信号(race-unordered-outcome): {group["runs"]} 回のうち、'
                            f'シェルが終わった時点で代用品が残っていた回 旧版 {group["old_remained"]}・'
                            f'Rust {group["new_remained"]}、標準エラーが開いていた回 旧版 {group["old_open"]}・'
                            f'Rust {group["new_open"]}、どちらかが違った回 {group["differ"]}')

    def test_group_hup_in_command_substitutions_in_each_order(self):
        """群への HUP の競合(サブシェルが仕事の群へ送り直す HUP と、親の EXIT の罠の rm)を、順序を決めて
        流す(HUP_ORDERS。止める相手と続ける時点を決める)。順序ごとに、終了値・出力・一時ファイル・子の
        残り方・標準エラーが旧版と同じかを比べる。mktemp の位置は一時ファイルの名前がまだ無いので、rm の
        途中の順序は無い。"""
        require_tool_baseline(tool_versions())
        w = ReviewsWorld(self)
        keep_origin(w)
        for tool, plain, held in self.comsub_hold(w):
            for order in HUP_ORDERS:
                if order == 'during-rm' and tool == 'mktemp':
                    continue
                label = f'{tool}-{order}'
                with self.subTest(case=label):
                    results = []
                    for index, argv in enumerate((reviews_argv(w.save()), reviews_native(w.save()))):
                        reset_world(w, index)
                        env = w.env(path=held if order == 'during-rm' else plain)
                        result, remained, roles, stderr_open = ordered_hup_run(w, argv, env, order)
                        check_fixed_mktemp(w.log)
                        # 役(代用品・サブシェル・rm)ごとの記録した番号。
                        children = {role: pid for pid, role in roles.items()}
                        results.append((result, remained, tree_files(w.work), stderr_open, children))
                    (old, old_remained, old_state, old_open, old_children), \
                        (new, new_remained, new_state, new_open, new_children) = results
                    job_pid_check(old, new, old_children, new_children)
                    self.assertEqual((old.returncode, old_remained, old_open), (new.returncode, new_remained, new_open))
                    self.assertEqual(old_state, new_state)
                    OBSERVATIONS.append(f'reviews-dir 群への HUP の順序 {label}: 一時ファイル '
                                        f'{"残る" if any(b".base-untracked." in key for key in old_state) else "消える"}'
                                        f'・終了値 {old.returncode}(旧版と Rust で同じ)')
        # 対照: EXIT の罠の始めで HUP を無視にする変種(旧版の写しの1行だけを変える)は、rm の途中の順序で
        # 2つ目の HUP に終わらされず、一時ファイルを消す。順序を決めた比較が、この違いを見分けることを確かめる。
        line = 'cleanup() { if [ -n "$TMP" ]; then rm -f -- "$TMP"; fi; }'
        text = REVIEWS_SCRIPT.read_text(encoding='utf-8')
        if text.count(line) != 1:
            raise Difference('対照: cleanup の行が見つからない')
        mutant = w.base / 'reviews-dir-ignore-hup.sh'
        mutant.write_text(text.replace(line, line.replace('{ if', "{ trap '' HUP; if")), encoding='utf-8')
        _, _, held = self.comsub_hold(w)[1]
        states = []
        for index, argv in enumerate((reviews_argv(w.save()), ['bash', str(mutant), *w.save()])):
            reset_world(w, index)
            ordered_hup_run(w, argv, w.env(path=held), 'during-rm')
            states.append(tree_files(w.work))
        if states[0] == states[1]:
            raise Difference('対照: EXIT の罠で HUP を無視にする変種との違いを見分けない')
        OBSERVATIONS.append('reviews-dir 群への HUP の順序 対照: EXIT の罠で HUP を無視にする変種は、rm の途中の順序で'
                            '一時ファイルを消した(旧版は残す)')

    def test_group_sigint_with_children_that_handle_it_in_the_sha_pipeline(self):
        """コマンド置換のサブシェルの sha256 のパイプライン(sha256sum | cut)を待つ間の群への INT。旧bashの
        waitchld は、最後に回収した子で child_caught_sigint を決める(INT を受けて普通に終わる子が最後なら、
        サブシェルは続けて ERR の罠の行を出す)。INT を受けて 0.5 秒後に終了値3で終わる役(handles)と、INT で
        直ちに終わる役(dies)の組で、回収の順を決めて比べる。"""
        require_tool_baseline(tool_versions())
        w = ReviewsWorld(self)
        keep_origin(w)
        # 代用品の PATH には道具を選んだフォルダしか無いので、sleep は本物のパスで呼ぶ。
        sleep = shutil.which('sleep', path=w.real_path)
        roles = {'handles': ('#!/bin/sh\ntrap "' + sleep + ' 0.5; exit 3" INT\necho $$ >"%s"\n'
                             'while :; do ' + sleep + ' 0.05; done\n'),
                 'dies': '#!/bin/sh\necho $$ >"%s"\nexec ' + sleep + ' 30\n'}
        variants = {'late-sha': ('handles', 'dies'), 'late-cut': ('dies', 'handles'),
                    'both-handle': ('handles', 'handles'), 'both-die': ('dies', 'dies')}
        for variant, (sha_role, cut_role) in variants.items():
            with self.subTest(case=variant):
                fifos = {'sha': w.base / f'int-{variant}-sha', 'cut': w.base / f'int-{variant}-cut'}
                stubs = {}
                for name, role in (('sha', sha_role), ('cut', cut_role)):
                    stub = w.base / f'int-{variant}-{name}-stub'
                    stub.write_text(roles[role] % fifos[name])
                    stub.chmod(0o755)
                    stubs[name] = str(stub)
                bindir = w.bin_with('python3', 'mv', 'sed', 'mktemp', 'rm', 'mkdir', 'git',
                                    extra={'sha256sum': stubs['sha'], 'cut': stubs['cut']})
                results = []
                for index, argv in enumerate((reviews_argv(w.save()), reviews_native(w.save()))):
                    reset_world(w, index)
                    for fifo in fifos.values():
                        if fifo.exists():
                            fifo.unlink()
                        os.mkfifo(str(fifo))
                    process = subprocess.Popen(argv, cwd=str(w.work), env=w.env(path=bindir), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                    try:
                        children = {name: int(read_fifo(fifo).split()[0]) for name, fifo in fifos.items()}
                        # 代用品が待つ所(直ちに終わる役は sleep の中・受けて終わる役は sleep の子を待つ所)まで
                        # 進んでから送る(知らせの後、exec の前に受けると、dash が受けたまま sleep へ進む)。
                        for name, role in (('sha', sha_role), ('cut', cut_role)):
                            wait_sleeping_in(children[name],
                                             ('hrtimer_nanosleep',) if role == 'dies' else ('do_wait',),
                                             f'{variant}: {name} の代用品が待つ')
                        os.killpg(process.pid, signal.SIGINT)
                        out, err = process.communicate(timeout=30)
                    except BaseException:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=30)
                        raise
                    deadline = time.monotonic() + 10
                    while any(pid_alive(pid) for pid in children.values()) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    check_fixed_mktemp(w.log)
                    results.append((RunResult(process.returncode, out, err, False), tree_files(w.work), children))
                (old, old_state, old_children), (new, new_state, new_children) = results
                job_pid_check(old, new, old_children, new_children)
                self.assertEqual(old.returncode, new.returncode)
                self.assertEqual(old_state, new_state)
                trap_line = re.search(rb'ERROR \[internal\] [^\n]*', old.stderr)
                OBSERVATIONS.append(f'reviews-dir 群への INT・sha256 のパイプライン {variant}: 終了値 {old.returncode}・'
                                    + (trap_line.group(0).decode() if trap_line else 'ERR の罠の行なし')
                                    + '(旧版と Rust で同じ)')

    def test_signals_while_not_waiting_for_a_child(self):
        """子を待たない間(標準エラーへの書込みで止まった間)の TERM・HUP・INT。"""
        require_tool_baseline(tool_versions())
        if not sys.platform.startswith('linux'):
            not_run(self, None, 'Linux だけ(書込みで止まった所を /proc で確かめる)')
            return
        w = ReviewsWorld(self)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=number):
                results = [blocked_stderr_signal(w, argv, w.env(), number)
                           for argv in (reviews_argv(['x']), reviews_native(['x']))]
                compare(REVIEWS, *results)


# ---------------------------------------------------------------- implementation-git(単位5)

IG = 'implementation-git'
IG_SCRIPT = OLD[IG][1]
IG_PRECHECK = SCRIPTS / 'diff-snapshot.sh'
IG_INPUT_TESTS = 'test_implementation_git_input.py'
IG_GIT_TESTS = 'test_implementation_git.py'
IG_CORRESPONDENCE = FIXTURES / 'implementation-git-correspondence.json'
IG_KINDS = {'cli-pair': 24, 'cli-pair-injected': 2, 'cli-pair-mock': 1, 'split': 1, 'legacy-only': 1}
IG_EXISTING = 'implementation-git-existing'
IG_HASH = 'a' * 64
IG_OPS = ('status', 'diff', 'log', 'show', 'ls-files')
IG_MUTATION = ('result = run_git(cwd, env, *command)', 'result = run_git(cwd, clean_env(), *command)')
IG_FILTER = '通常 名'


def ig_native(args, script=IG_SCRIPT, entry=()):
    """Rust の起動。旧版の`__file__`(事前検査はその兄弟の diff-snapshot.sh)は入口の指定で渡す。"""
    return native_argv(IG, args, ['--r4g-script-file', str(script), *entry])


def ig_frame_counts(text):
    """呼出履歴の所在地の欄の数(旧スクリプト・標準ライブラリ)。Rust の固定名の出力から数える。"""
    paths = FILE_FIELD.findall(text)
    script = sum(1 for path in paths if path == b'implementation-git.py')
    stdlib = sum(1 for path in paths if path != b'implementation-git.py' and not path.startswith(b'<'))
    return script, stdlib


def ig_compare(old, new, *, case, script=IG_SCRIPT, decisions=(), **params):
    """旧版に呼出履歴があれば所在地の欄の決定を当ててから、全バイトで比べる。"""
    decisions = tuple(decisions)
    if b'Traceback (most recent call last):' in old.stderr and 'location-fields' not in decisions:
        decisions += ('location-fields',)
    if 'location-fields' in decisions:
        params['location'] = dict(script_path=str(script), script_name='implementation-git.py',
                                  stdlib_dir=sysconfig.get_paths()['stdlib'], expected=ig_frame_counts(new.stderr))
    compare(IG, old, new, decisions=decisions, case=case, **params)


def ig_injection(script):
    """旧版のコピーの書き換えを、Rust の注入点の指定へ対応づける。対応づけない書き換えは不合格。"""
    text = Path(script).read_text(encoding='utf-8')
    original = IG_SCRIPT.read_text(encoding='utf-8')
    if text == original:
        return []
    pre = re.search(r'^PRECHECK_TIMEOUT = ([0-9]+(?:\.[0-9]+)?)$', text, re.M)
    git = re.search(r'^GIT_TIMEOUT = ([0-9]+(?:\.[0-9]+)?)$', text, re.M)
    if pre and git:
        for literal in (pre.group(1), git.group(1)):
            # Rust の入口が受けるのは、Python の str(数) と同じ表記だけ(0.050 や 007 は対応づけない)。
            value = float(literal)
            if literal != (str(int(value)) if '.' not in literal else repr(value)):
                raise Difference('期限の書き換えの字面が Python の表記でない: ' + literal)
        rebuilt = original.replace('PRECHECK_TIMEOUT = 120', 'PRECHECK_TIMEOUT = ' + pre.group(1), 1)
        rebuilt = rebuilt.replace('GIT_TIMEOUT = 30', 'GIT_TIMEOUT = ' + git.group(1), 1)
        if rebuilt == text:
            return ['--r4g-deadlines', pre.group(1) + ',' + git.group(1)]
    if original.count(IG_MUTATION[0]) == 1 and text == original.replace(*IG_MUTATION):
        return ['--r4g-mutation', 'operation-env']
    raise Difference('対応づけない旧版のコピーの書き換え')


def ig_rewrite(script, precheck=None, git=None):
    """旧版のコピーの期限の定数を書き換える(既存テストと同じ方法)。Rust へは --r4g-deadlines で同じ値を渡す。"""
    text = IG_SCRIPT.read_text(encoding='utf-8')
    if precheck is not None:
        text = text.replace('PRECHECK_TIMEOUT = 120', 'PRECHECK_TIMEOUT = ' + precheck, 1)
    if git is not None:
        text = text.replace('GIT_TIMEOUT = 30', 'GIT_TIMEOUT = ' + git, 1)
    Path(script).write_text(text, encoding='utf-8')
    return ig_injection(script)


def ig_text_result(argv, result, timeout):
    """`subprocess.run(text=True)`と同じ形の結果(UTF-8の厳密な復号と改行の読み替え)。"""
    if result.timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)

    def text(data):
        return data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    return subprocess.CompletedProcess(argv, result.returncode, text(result.stdout), text(result.stderr))


def ig_read_trace(trace):
    return trace.read_text(encoding='utf-8') if trace is not None and trace.exists() else None


def ig_set_trace(trace, text):
    if trace is None:
        return
    if text is None:
        if trace.exists():
            trace.unlink()
    else:
        trace.write_text(text, encoding='utf-8')


def ig_paired_call(args, env, script, *, timeout, trace=None):
    """既存テストの1回の起動を、旧版とRustの組へ置き換え、旧版の結果を既存テストへ返す。

    比べるもの: 終了値・標準出力・標準エラー(全バイト)と、記録の代用品が残す子の起動の列(引数と GIT_ の
    環境の並び)。期限を0.05秒へ書き換えた組は、どの子で期限が来るかと、期限の来た子が止められる前に記録を
    書けたかが機械の負荷で変わる。その2つだけで説明できる違いのときに限り、両方が同じ組が取れるまで最大
    IG_DEADLINE_ATTEMPTS 回まで両方を流し直し、回数と要点を観測に残す(取れなければ不合格)。説明できない
    違いは直ちに不合格。期限の来る子を FIFO で決めた確かな組は IgContract.test_precheck_deadlines と
    test_git_failures_and_deadlines が比べる。
    """
    script = Path(script)
    entry = ig_injection(script)
    prefix = ig_read_trace(trace)
    argv = [sys.executable, '-B', str(script), *args]
    attempts = IG_DEADLINE_ATTEMPTS if '--r4g-deadlines' in entry else 1
    for attempt in range(attempts):
        ig_set_trace(trace, prefix)
        old = run_command(argv, cwd=os.getcwd(), env=env, timeout=timeout)
        old_trace = ig_read_trace(trace)
        ig_set_trace(trace, prefix)
        new = run_command(ig_native(args, script, entry), cwd=os.getcwd(), env=env, timeout=timeout)
        new_trace = ig_read_trace(trace)
        try:
            ig_compare(old, new, case='implementation-git/existing', script=script)
        except Difference as error:
            explained, failure = timeout_stage_difference(old, new), error
        else:
            if old_trace == new_trace:
                break
            failure = Difference('子の起動の記録(引数と GIT_ の環境の並び)が違う')
            explained = timeout_record_difference(old, new, old_trace, new_trace)
        if attempt + 1 == attempts or explained is None:
            raise failure
        OBSERVATIONS.append(f'implementation-git 既存の 0.05 秒の組の流し直し {attempt + 1} 回目: {explained}'
                            f'(不一致: {failure})')
    ig_set_trace(trace, old_trace)
    return ig_text_result(argv, old, timeout)


# 0.05 秒の組の流し直しの上限。記録の有無は旧版と Rust で独立に揺れ、記録だけでも1回の組が合わない見込みは
# 最大で半分。CPU を2つに絞り空回り3本を並べた容器の30回(2026-11-12)では、90回の呼出しのうち1回が9回目で
# やっと合った(5回の上限では落ちた)。半分の見込みで20回続けて合わない見込みは約100万分の1。
IG_DEADLINE_ATTEMPTS = 20


TIMEOUT_LINE = re.compile(rb"^ERROR \[(precheck|git)\] [^\n]*: Command '\['([a-z]+)'([^\n]*)\]' timed out after "
                          rb"[0-9.e-]+ seconds\n\Z", re.M)
TIMEOUT_OPS = (b'config', b'rev-parse', b'cat-file', b'status', b'diff', b'log', b'show', b'ls-files')


def timeout_stage_difference(old, new):
    """0.05 秒の組の不一致が「期限の来た子の違い」だけで説明できるときは、その要点。ほかの差は None。
    説明できる: 両方が終了値20・同じ標準出力で、標準エラーの最後の行が1つの時間切れの文。"""
    if (old.returncode, new.returncode) != (20, 20) or old.stdout != new.stdout or old.timed_out or new.timed_out:
        return None
    stages = []
    for result in (old, new):
        if result.stderr.count(b'timed out after') != 1:
            return None
        found = TIMEOUT_LINE.search(result.stderr)
        if found is None:
            return None
        ops = [op for op in TIMEOUT_OPS if b"'" + op + b"'" in found.group(3)]
        stages.append(b'/'.join([found.group(1), found.group(2), *ops[:1]]).decode())
    if stages[0] == stages[1]:
        return None
    return f'旧版は {stages[0]}・Rust は {stages[1]} で期限が来た'


def timeout_record_difference(old, new, old_trace, new_trace):
    """結果が同じ0.05秒の組で、子の起動の記録の違いが「期限の来た子が止められる前に記録を書けたか」だけで
    説明できるときは、その要点。ほかの差は None。

    既存テストの記録の代用品は、起動の直後に記録を書いてから眠る。期限より前に書けたかは負荷で変わる。
    説明できる: 両方が終了値20・同じ標準出力と標準エラーで、標準エラーの最後の行が1つの時間切れの文。
    記録の列は、一方が他方の末尾にちょうど1行を足したもので、その行が時間切れの文の命令の子の記録
    (事前検査なら kind が precheck で引数が `precheck` と命令の3つ目から後、Git なら kind が git で引数が
    命令の2つ目から後)。途中の行の違い・2行以上の違い・欠けた行・ほかの子の記録は説明できない。
    """
    if (old.returncode, new.returncode) != (20, 20) or old.timed_out or new.timed_out:
        return None
    if (old.stdout, old.stderr) != (new.stdout, new.stderr) or old.stderr.count(b'timed out after') != 1:
        return None
    found = TIMEOUT_LINE.search(old.stderr)
    if found is None:
        return None
    try:
        command = ast.literal_eval((b"['" + found.group(2) + b"'" + found.group(3) + b']').decode('utf-8'))
    except (ValueError, SyntaxError, UnicodeDecodeError):
        return None
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return None
    if found.group(1) == b'precheck':
        expected = {'kind': 'precheck', 'args': ['precheck', *command[2:]]}
    else:
        expected = {'kind': 'git', 'args': command[1:]}
    lines = []
    for trace in (old_trace or '', new_trace or ''):
        if trace and not trace.endswith('\n'):
            return None
        lines.append(trace.splitlines(keepends=True))
    if len(lines[0]) < len(lines[1]):
        shorter, longer, writer = lines[0], lines[1], 'Rust'
    else:
        shorter, longer, writer = lines[1], lines[0], '旧版'
    if len(longer) != len(shorter) + 1 or longer[:-1] != shorter:
        return None
    try:
        record = json.loads(longer[-1])
    except ValueError:
        return None
    if (not isinstance(record, dict) or set(record) != {'kind', 'args', 'env'} or not isinstance(record['env'], dict)
            or {key: record[key] for key in ('kind', 'args')} != expected):
        return None
    ops = [op.decode() for op in TIMEOUT_OPS if op.decode() in command[1:]]
    stage = '/'.join([found.group(1).decode(), command[0], *ops[:1]])
    return f'{stage} で期限が来た子の記録を、{writer} の側だけが止められる前に書けた'


def ig_input_wrapper(original, module):
    """InputCharacterization の起動(`call`)を、旧版とRustの組へ置き換える。"""
    class Paired(original):
        def call(self, *args):
            return ig_paired_call(list(args), dict(os.environ, COLUMNS='80'), module.SCRIPT, timeout=60)

    Paired.__name__ = Paired.__qualname__ = 'Paired' + original.__name__
    return Paired


def ig_harness_wrapper(original, module):
    """Harness の起動(`call`)を、旧版とRustの組へ置き換える。記録の代用品の列も比べる。"""
    class Paired(original):
        def call(self, *args, env=None, script=None):
            child = dict(self.env)
            child.update(env or {})
            return ig_paired_call(list(args), child, script or self.script, timeout=12, trace=self.trace)

    Paired.__name__ = Paired.__qualname__ = 'Paired' + original.__name__
    return Paired


def ig_existing_suite():
    """既存のCLIの27件(InputCharacterization 10・StubContractTests 11・RealRepositoryTests 6)を組で流す。"""
    wrappers = {(IG_INPUT_TESTS, 'InputCharacterization'): ig_input_wrapper,
                (IG_GIT_TESTS, 'StubContractTests'): ig_harness_wrapper,
                (IG_GIT_TESTS, 'RealRepositoryTests'): ig_harness_wrapper}
    return existing_suite(wrappers, classes=set(wrappers))


def ig_load_correspondence():
    data = json.loads(IG_CORRESPONDENCE.read_text(encoding='utf-8'))
    if set(data) != {'files', 'note', 'entries'} or data['files'] != [IG_INPUT_TESTS, IG_GIT_TESTS]:
        raise ValueError('対応表の形が違う')
    return data


def ig_check_correspondence(data, *, manifest=None, methods=()):
    """既存29件の対応表: 名前の集合が既存テストと1対1・種類ごとの件数が固定値・行き先がある。"""
    manifest = manifest or existing_manifest()
    loader = unittest.TestLoader()
    expected = set()
    for file in data['files']:
        module = load(ROOT / 'scripts' / file, 'r4g_existing_names_' + file[:-3])
        for name in manifest['files'][file]['classes']:
            expected |= {(file, name, method) for method in loader.getTestCaseNames(getattr(module, name))}
    entries = data['entries']
    seen = [(entry['file'], entry['class'], entry['name']) for entry in entries]
    if len(seen) != len(set(seen)) or set(seen) != expected:
        raise AssertionError('対応表の名前が既存テストと1対1でない')
    counts = {}
    for entry in entries:
        if set(entry) != {'file', 'class', 'name', 'kind', 'target', 'reason'} or entry['kind'] not in IG_KINDS:
            raise AssertionError('対応表の項目の形が違う')
        if not entry['reason']:
            raise AssertionError('対応表の理由が空')
        counts[entry['kind']] = counts.get(entry['kind'], 0) + 1
        if entry['kind'] in ('cli-pair', 'cli-pair-injected') and entry['target'] != IG_EXISTING:
            raise AssertionError('CLIの組の行き先が違う')
        if entry['kind'] in ('cli-pair-mock', 'split') and entry['target'] not in methods:
            raise AssertionError('注入の組の行き先の検査が無い: ' + entry['target'])
        if entry['kind'] == 'legacy-only' and entry['target']:
            raise AssertionError('旧版だけの項目に行き先がある')
    if counts != IG_KINDS:
        raise AssertionError(f'対応表の種類ごとの件数が固定値と違う: {counts}')
    return counts


def ig_state(repo):
    """読取りの前後で変わってはいけないもの: 作業ツリーと .git の全ファイルの中身と、atime を除く物理情報。"""
    return observation(repo)


class IgWorld:
    """implementation-git の使い捨ての入力: filter を設定した実Gitのリポジトリ・旧版のコピーと兄弟の事前検査・
    子の起動の記録の代用品(git と bash)。"""

    NAMES = ('normal.txt', 'space name.txt', '日本語 [a].txt', 'Case.txt')

    def __init__(self, test, *, precheck=None, repository=True):
        self.temp = tempfile.TemporaryDirectory(prefix='r4g-ig-')
        test.addCleanup(self.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'repo'
        self.home = self.base / 'home'
        self.home.mkdir()
        self.copy = self.base / 'copy'
        self.copy.mkdir()
        self.script = self.copy / 'implementation-git.py'
        shutil.copy2(IG_SCRIPT, self.script)
        self.precheck = self.copy / 'diff-snapshot.sh'
        if precheck is None:
            shutil.copy2(IG_PRECHECK, self.precheck)
        else:
            self.precheck.write_text(precheck, encoding='utf-8')
            self.precheck.chmod(0o755)
        self.real_path = os.environ.get('PATH', '/usr/bin:/bin')
        self.real_git = shutil.which('git', path=self.real_path)
        self.stubs = self.base / 'stubs'
        self.stubs.mkdir()
        self.log = self.base / 'calls'
        for tool in ('git', 'bash', 'mktemp'):
            # 事前検査(diff-snapshot.sh)の中の mktemp -d も決まった名前にする(開いた fd の名前を比べる)。
            write_stub(self.stubs, tool, self.log, real=shutil.which(tool, path=self.real_path),
                       fixed_mktemp=tool == 'mktemp')
        if repository:
            self.make_repository()

    def cleanup(self):
        try:
            for path in [self.base, *self.base.rglob('*')]:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
        except OSError:
            pass
        self.temp.cleanup()

    def setup_env(self):
        return {'PATH': self.real_path, 'HOME': str(self.home), 'LC_ALL': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': os.devnull}

    def git(self, *args):
        return subprocess.run([self.real_git, '-C', str(self.repo), *args], env=self.setup_env(), check=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def make_repository(self):
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        for name in self.NAMES:
            (self.repo / name).write_text('first\n')
        (self.repo / 'dir').mkdir()
        (self.repo / 'dir/file').write_text('nested\n')
        (self.repo / 'link').symlink_to('normal.txt')
        self.git('add', '--all')
        self.git('commit', '-qm', 'initial')
        (self.repo / 'normal.txt').write_text('second\n')
        self.git('add', 'normal.txt')
        self.git('commit', '-qm', 'second')
        for item, value in (('clean', 'cat'), ('smudge', 'cat'), ('required', 'true')):
            self.git('config', f'filter.{IG_FILTER}.{item}', value)
        (self.repo / '.gitattributes').write_text(f'*.txt "filter={IG_FILTER}"\n')
        (self.repo / 'normal.txt').write_text('working\n')

    def env(self, path=None, **changes):
        env = {'PATH': path or (str(self.stubs) + os.pathsep + self.real_path), 'HOME': str(self.home),
               'LC_ALL': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
               'TMPDIR': tempfile.gettempdir()}
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, argv, env=None, launch=None, timeout=120):
        if self.log.exists():
            self.log.unlink()
        result = run_command(launched(argv, launch), cwd=str(self.base), env=self.env() if env is None else env,
                             timeout=timeout)
        return result, read_calls(self.log)

    def approval(self):
        """実の事前検査が出す承認ダイジェスト(prepare は承認を求めて22で止まる)。"""
        result, _ = self.run([sys.executable, str(self.script), 'prepare', '--cwd', str(self.repo)])
        values = re.findall('承認ダイジェスト:\\s*([a-f0-9]{64})'.encode(), result.stderr)
        if result.returncode != 22 or not values:
            raise Difference('事前検査の承認ダイジェストが取れない')
        return values[-1].decode()

    def prepared(self, accept=None):
        """旧版の prepare の照合値(run に渡す)。"""
        args = ['prepare', '--cwd', str(self.repo)] + (['--accept', accept] if accept else [])
        result, _ = self.run([sys.executable, str(self.script), *args])
        if result.returncode != 0:
            raise Difference('旧版の prepare が失敗した: ' + result.stderr.decode(errors='replace')[-300:])
        return json.loads(result.stdout)['context_sha256']

    def run_args(self, digest, *rest, accept=None, cwd=None):
        args = ['run', '--cwd', str(cwd or self.repo), '--expect-context-sha256', digest]
        if accept:
            args += ['--accept', accept]
        return args + list(rest)

    def pair(self, args, *, env=None, launch=None, case='implementation-git/contract', decisions=(), entry=(),
             calls=True, timeout=120, watch=True, old_launch=None, **params):
        """旧版と Rust を同じ入力で順に流し、終了値・出力・子の起動の列を比べる。入力の無変更も前後で確かめる。
        `old_launch` を渡せば、旧版だけをその起動の指定で流す(閉じた標準の fd の期待など)。"""
        env = self.env() if env is None else env
        before = ig_state(self.repo) if watch and self.repo.exists() else None
        results = []
        for argv, side_launch in (([sys.executable, str(self.script), *args], old_launch or launch),
                                  (ig_native(args, self.script, entry), launch)):
            results.append(self.run(argv, env, side_launch, timeout))
            if before is not None:
                unchanged(before, ig_state(self.repo))
        (old, old_calls), (new, new_calls) = results
        ig_compare(old, new, case=case, script=self.script, decisions=decisions, **params)
        if calls:
            same_calls(IG, old_calls, new_calls, case='implementation-git/calls')
        return old, new, old_calls


def ig_held_call(calls):
    """子の起動の列の最後(期限の来た子)の、実行した名前と引数。列が空なら不合格。"""
    if not calls:
        raise Difference('期限の来た子の起動の記録が無い')
    path, args = calls[-1][0], calls[-1][1]
    return (os.path.basename(os.fsdecode(bytes.fromhex(path))),
            *(os.fsdecode(bytes.fromhex(item)) for item in args))


def ig_precheck_stub(raw=b'', code=0, stderr=b'', block=None, close_outputs=False, sleep=0):
    """事前検査の代用品(bash で動く)。決めた出力と終了値を返す。`block` は放されるまで待つ FIFO。"""
    lines = ['#!/bin/bash', 'printf %s "$*" >"${R4G_PRECHECK_ARGS:-/dev/null}"']
    if close_outputs:
        lines.append('exec >&- 2>&-')
    if block:
        lines.append(f'echo $$ >{shlex_quote(block + ".notify")}; read x <{shlex_quote(block)}')
    if sleep:
        lines.append(f'sleep {sleep}')
    if raw:
        lines.append("printf '" + ''.join('\\%03o' % byte for byte in raw) + "'")
    if stderr:
        lines.append("printf '" + ''.join('\\%03o' % byte for byte in stderr) + "' >&2")
    lines.append(f'exit {code}')
    return '\n'.join(lines) + '\n'


def shlex_quote(text):
    return "'" + str(text).replace("'", "'\\''") + "'"


def ig_tokens(mapping, reverse=False):
    entries = list(mapping.items())
    if reverse:
        entries.reverse()
    result = ['GIT_CONFIG_COUNT=%d' % len(entries)]
    for index, (key, value) in enumerate(entries):
        result += ['GIT_CONFIG_KEY_%d=%s' % (index, key), 'GIT_CONFIG_VALUE_%d=%s' % (index, value)]
    if reverse:
        result.reverse()
    return ('\0'.join(result) + '\0').encode('utf-8')


def ig_filters(*names):
    return {'filter.%s.%s' % (name, item): ('false' if item == 'required' else '')
            for name in names for item in ('clean', 'smudge', 'process', 'required')}


class IgSplit(unittest.TestCase):
    """対応表で mock や文書の検査から分けた部分(Rust でも比べる部分)。"""

    def test_strict_input_stops_before_precheck(self):
        """`:370` の mock の部分: UTF-8 でない引数は事前検査の前に終了2で止まる(子を起動しない)。"""
        world = IgWorld(self, precheck=ig_precheck_stub())
        for value in (b'\xff', b'\xed\xa0\x80', b'HEAD\xc3', b'\xc0\xaf'):
            for args in ([b'run', b'--cwd', os.fsencode(str(world.repo)), b'--expect-context-sha256',
                          IG_HASH.encode(), b'show', b'--rev', value],
                         [b'prepare', b'--cwd', value], [value]):
                with self.subTest(value=value, args=args[:1]):
                    old, new, calls = world.pair(args)
                    self.assertEqual((old.returncode, calls), (2, []))
                    self.assertEqual(old.stderr, 'ERROR [input] 引数 は厳密な UTF-8 ではない\n'.encode())

    def test_missing_sibling_precheck_script(self):
        """`:602` のうち Rust でも比べる部分: 兄弟の diff-snapshot.sh が無い・読めない・ディレクトリなら終了20。"""
        world = IgWorld(self)
        digest = IG_HASH
        for label in ('missing', 'unreadable', 'directory', 'dangling'):
            with self.subTest(label=label):
                if world.precheck.is_symlink() or world.precheck.is_file():
                    world.precheck.unlink()
                elif world.precheck.is_dir():
                    world.precheck.rmdir()
                if label == 'unreadable':
                    shutil.copy2(IG_PRECHECK, world.precheck)
                    world.precheck.chmod(0)
                elif label == 'directory':
                    world.precheck.mkdir()
                elif label == 'dangling':
                    world.precheck.symlink_to(world.base / 'nowhere')
                for args in (['prepare', '--cwd', str(world.repo)], world.run_args(digest, 'status')):
                    old, _, calls = world.pair(args)
                    self.assertEqual((old.returncode, calls), (20, []))
                    self.assertEqual(old.stderr, 'ERROR [requirements] diff-snapshot.sh を読み込めない\n'.encode())


class IgCorrespondence(unittest.TestCase):
    def test_correspondence_is_one_to_one_and_fixed(self):
        methods = [name for name in unittest.TestLoader().getTestCaseNames(IgSplit)]
        counts = ig_check_correspondence(ig_load_correspondence(), methods=methods)
        self.assertEqual(sum(counts.values()), 29)


def pty_run(argv, env, cwd, columns):
    """標準出力を端末(幅 `columns`)にして起動し、終了値・端末への出力・標準エラーを返す。"""
    import fcntl
    import struct
    import termios
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, columns, 0, 0))
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=slave,
                                   stderr=subprocess.PIPE, start_new_session=True)
        os.close(slave)
        slave = None
        chunks = []
        while True:
            try:
                part = os.read(master, 65536)
            except OSError:
                break
            if not part:
                break
            chunks.append(part)
        stderr = process.stderr.read()
        process.wait(timeout=60)
        return RunResult(process.returncode, b''.join(chunks), stderr, False)
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)


class IgArguments(unittest.TestCase):
    """全階層の help と幅・argparse の誤り・`int()`・`--x=--` の値(空のリスト)の扱い。"""

    LEVELS = ([], ['prepare'], ['run'], *(['run', op] for op in IG_OPS))

    def setUp(self):
        self.world = IgWorld(self)

    def test_help_for_every_level_and_columns(self):
        widths = (None, '', '0', '-5', '1', '11', '20', '33', '40', '57', '60', '79', '80', '81', '100', '200', 'x',
                  ' 90 ', '1_00', '٨٠', '9' * 5000, '+70', '7' * 30)
        for level in self.LEVELS:
            for width in widths:
                with self.subTest(level=level, width=width):
                    self.world.pair(level + ['--help'], env=self.world.env(COLUMNS=width), calls=False)
        for changes in ({'LINES': '5', 'COLUMNS': None}, {'COLUMNS': '0', 'LINES': '0'},
                        {'PYTHONINTMAXSTRDIGITS': '640', 'COLUMNS': '9' * 700}, {'COLUMNS': '50', 'LINES': 'x'}):
            with self.subTest(changes=changes):
                self.world.pair(['run', 'diff', '-h'], env=self.world.env(**changes), calls=False)

    def test_help_on_a_terminal(self):
        for columns, width in ((50, None), (120, None), (50, '70'), (0, None)):
            with self.subTest(columns=columns, width=width):
                env = self.world.env(COLUMNS=width)
                old = pty_run([sys.executable, str(self.world.script), 'run', 'show', '--help'], env,
                              str(self.world.base), columns)
                new = pty_run(ig_native(['run', 'show', '--help'], self.world.script), env, str(self.world.base),
                              columns)
                ig_compare(old, new, case='implementation-git/contract')

    def test_argument_errors(self):
        repo = str(self.world.repo)
        run = lambda *rest: self.world.run_args(IG_HASH, *rest)  # noqa: E731
        cases = [[], ['bogus'], ['--', 'run'], ['-'], ['-x'], ['--bogus', 'run'], ['-h=x'], ['-hx'], ['-hh'],
                 ['--help=x'], ['--help='], ['-h', 'bogus'], ['bogus', '-h'], ['-- ', 'run'], ['run'],
                 ['prepare'], ['prepare', '--cwd'], ['prepare', '--cwd', '--accept', 'x'], ['prepare', '--cw', 'x'],
                 ['prepare', '--cwd=' + repo, '--cwd', repo], ['prepare', '--cwd', repo, 'extra'],
                 ['prepare', '--cwd', repo, '--', 'x'], ['prepare', '--cwd', '-1'], ['prepare', '--cwd', '-١'],
                 ['prepare', '--cwd', '-1.5\n'], ['prepare', '--cwd', '-x y'], ['prepare', '--cwd='],
                 ['prepare', '--cwd', repo, '--accept'], ['prepare', '--cwd', repo, '-h=--'],
                 ['run', '--cwd', repo, 'status'], ['run', 'status'], ['run', '--expect-context-sha256', 'x', 'bogus'],
                 run('status', '--format', 'bad'), run('status', '--format=bad', '--format=short'),
                 run('status', '-h=x'), run('diff', '--cached=x'), run('diff', '--cached='), run('diff', '--cached=--'),
                 run('diff', '--context', '--'), run('diff', '--context', '-1'), run('diff', '--context=٣'),
                 run('diff', '--context= 5 '), run('diff', '--context=1_0'), run('diff', '--context=0x10'),
                 run('diff', '--context=+7'), run('diff', '--context=-0'), run('diff', '--context=' + '9' * 5000),
                 run('diff', '--context=1__0'), run('diff', '--context=　 7　'), run('log', '--limit', ''),
                 run('log', '--limit=--'), run('diff', '--context=--'), run('status', '--path', 'a', '--', 'b'),
                 run('status', '--path=--'), run('show', '--file=--'), run('show', '--rev=--'),
                 run('diff', '--base=--'), run('log', '--to=--'), ['run', '--cwd', repo, '--expect-context-sha256=--',
                                                                  'status'],
                 ['prepare', '--cwd=--'], run('show', '--file=a', '--format=--'), run('status', '--untracked', '--x'),
                 run('ls-files', '--mode=bad'), run('status', 'extra', '--bogus')]
        for args in cases:
            with self.subTest(args=args):
                old, _, _ = self.world.pair(args)
                # 正しい int の値(`1_0`・`+7`・全角の空白など)は事前検査まで進む。起動の列は pair が比べる。
                self.assertIn(old.returncode, (0, 1, 2, 20, 22))
        old, _, _ = self.world.pair(['run', '--cwd', repo, '--expect-context-sha256', IG_HASH, 'diff',
                                     '--context=' + '9' * 5000], env=self.world.env(PYTHONINTMAXSTRDIGITS='0'))
        self.assertEqual(old.returncode, 2)

    def test_list_values_after_the_precheck(self):
        """`--x=--` で空のリストが入った値のうち、事前検査と実効値の確認の後に効くもの。"""
        world = IgWorld(self, precheck=ig_precheck_stub())
        digest = world.prepared()
        for rest in (('status', '--format=--'), ('status', '--untracked=--'), ('diff', '--format=--'),
                     ('log', '--format=--'), ('show', '--format=--'), ('ls-files', '--mode=--')):
            with self.subTest(rest=rest):
                world.pair(world.run_args(digest, *rest))
        world.pair(['prepare', '--cwd', str(world.repo), '--accept=--'])


class IgContract(unittest.TestCase):
    """既存テストに無い契約の分岐(cwd・6変数と子の環境・事前検査・filter・context・Git の失敗と期限・5操作)。"""

    def test_cwd_resolution(self):
        world = IgWorld(self, precheck=ig_precheck_stub())
        (world.base / 'link').symlink_to(world.repo, target_is_directory=True)
        (world.base / 'loop1').symlink_to(world.base / 'loop2')
        (world.base / 'loop2').symlink_to(world.base / 'loop1')
        locked = world.base / 'locked'
        (locked / 'inner').mkdir(parents=True)
        odd = os.fsencode(str(world.base)) + b'/odd-\xff'
        os.mkdir(odd)
        os.symlink(odd, str(world.base / 'odd-link'))
        (world.base / 'plain').write_text('x')
        cases = ['repo', str(world.repo), str(world.base / 'link'), str(world.repo) + '/', '//' + str(world.repo),
                 str(world.repo) + '/../repo', str(world.base / 'link') + '/../repo', 'loop1', 'loop1/x', 'missing',
                 'plain', 'plain/', str(locked / 'inner'), str(world.base / 'odd-link'), '.', 'repo/./dir/..']
        locked.chmod(0)
        try:
            for cwd in cases:
                with self.subTest(cwd=cwd):
                    world.pair(['prepare', '--cwd', cwd])
        finally:
            locked.chmod(0o700)

    def test_environment_variables_and_child_environment(self):
        world = IgWorld(self)
        accept = world.approval()
        digest = world.prepared(accept)
        for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                    'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
            for value in ('elsewhere', ''):
                with self.subTest(key=key, value=value):
                    old, _, calls = world.pair(world.run_args(digest, 'status', accept=accept),
                                               env=world.env(**{key: value}))
                    self.assertEqual(old.returncode, 2 if value else 0)
        inherited = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'filter.old.clean', 'GIT_CONFIG_VALUE_0': 'old',
                     'GIT_CONFIG_PARAMETERS': "'x.y=1'", 'GIT_LITERAL_PATHSPECS': '1', 'GIT_GLOB_PATHSPECS': '1',
                     'GIT_NOGLOB_PATHSPECS': '1', 'GIT_ICASE_PATHSPECS': '1', 'GIT_NO_LAZY_FETCH': '0',
                     'GIT_OPTIONAL_LOCKS': '1', 'GIT_CONFIG_KEY_9': 'x'}
        for label, changes in (('inherited', inherited), ('lazy-first', {'GIT_TERMINAL_PROMPT': '1'}),
                               ('pwd', {'PWD': '/', 'OLDPWD': '/'}), ('columns', {'COLUMNS': '20'})):
            with self.subTest(label=label):
                env = dict(changes)
                env.update(world.env())
                world.pair(world.run_args(digest, 'show', accept=accept), env=env)

    def test_raw_environment_order_duplicates_and_odd_entries(self):
        world = IgWorld(self)
        accept = world.approval()
        digest = world.prepared(accept)
        base = [f'{key}={value}'.encode() for key, value in world.env().items()]
        for label, entries in (('duplicate-path', [b'PATH=/nonexistent'] + base + [b'PATH=/also']),
                               ('duplicate-git', base + [b'GIT_DIR=', b'GIT_DIR=elsewhere']),
                               ('odd', [b'=x', b'noequals', b'A=B=C'] + base),
                               ('non-utf8', base + [b'R4G_X=\xff', b'R4G_\xfe=1'])):
            with self.subTest(label=label):
                launch = {'raw_env': [entry.hex() for entry in entries]}
                world.pair(world.run_args(digest, 'status', accept=accept), launch=launch)

    def test_locale_coercion_in_the_child_environment(self):
        world = IgWorld(self)
        accept = world.approval()
        digest = world.prepared(accept)
        for label, changes in (('coerce', {'LC_ALL': None}), ('coerce-ctype-c', {'LC_ALL': None, 'LC_CTYPE': 'C'}),
                               ('coerce-lang-c', {'LC_ALL': None, 'LANG': 'C'}),
                               ('no-coerce', {'LC_ALL': None, 'PYTHONCOERCECLOCALE': '0'}),
                               ('warn', {'LC_ALL': None, 'PYTHONCOERCECLOCALE': 'warn'}),
                               ('installed', {'LC_ALL': None, 'LANG': 'C.UTF-8'}),
                               ('c-all', {'LC_ALL': 'C'})):
            with self.subTest(label=label):
                env = world.env(**changes)
                if label == 'coerce-ctype-c':
                    env = dict([('LC_CTYPE', 'C')] + [item for item in env.items() if item[0] != 'LC_CTYPE'])
                world.pair(world.run_args(digest, 'status', accept=accept), env=env)
        with self.subTest(label='not-installed'):
            # 事前検査の bash は地域設定で警告を変えうるので、出力の無い代用品の事前検査で比べる。
            world = IgWorld(self, precheck=ig_precheck_stub())
            digest, accept = world.prepared(), None
            env = world.env(LC_ALL=None, LANG='xx_YY.UTF-8')
            args = world.run_args(digest, 'status', accept=accept)
            old, old_calls = world.run([sys.executable, str(world.script), *args], env)
            new, new_calls = world.run(ig_native(args, world.script), env)
            ig_compare(old, new, case='implementation-git/installed-locale')
            installed_locale_lc_ctype(IG, 'implementation-git/installed-locale', old_calls, new_calls)

    def test_precheck_outputs_codes_and_relay(self):
        good = ig_tokens(ig_filters('a'))
        cases = [
            ('empty', ig_precheck_stub()), ('good', ig_precheck_stub(good)),
            ('reversed', ig_precheck_stub(ig_tokens(ig_filters('a', 'b=c 日本'), reverse=True))),
            ('nul-missing', ig_precheck_stub(good[:-1])), ('not-utf8', ig_precheck_stub(b'\xff\0')),
            ('surrogate', ig_precheck_stub(b'GIT_CONFIG_COUNT=1\0GIT_CONFIG_KEY_0=filter.\xed\xa0\x80.clean\0'
                                           b'GIT_CONFIG_VALUE_0=\0')),
            ('dup-count', ig_precheck_stub(b'GIT_CONFIG_COUNT=4\0GIT_CONFIG_COUNT=4\0')),
            ('count-x', ig_precheck_stub(good.replace(b'COUNT=4', b'COUNT=x'))),
            ('count-5', ig_precheck_stub(good.replace(b'COUNT=4', b'COUNT=5'))),
            ('count-0004', ig_precheck_stub(good.replace(b'COUNT=4', b'COUNT=0004'))),
            ('count-big', ig_precheck_stub(good.replace(b'COUNT=4', b'COUNT=' + b'9' * 400))),
            ('other-name', ig_precheck_stub(good + b'OTHER=1\0')), ('no-equals', ig_precheck_stub(b'GIT_CONFIG_COUNT\0')),
            ('index-gap', ig_precheck_stub(good.replace(b'GIT_CONFIG_KEY_0', b'GIT_CONFIG_KEY_9'))),
            ('index-01', ig_precheck_stub(good.replace(b'GIT_CONFIG_KEY_0', b'GIT_CONFIG_KEY_00'))),
            ('missing-required', ig_precheck_stub(ig_tokens({k: v for k, v in ig_filters('a').items()
                                                             if not k.endswith('.required')}))),
            ('bad-value', ig_precheck_stub(good.replace(b'VALUE_0=\0', b'VALUE_0=cat\0'))),
            ('required-true', ig_precheck_stub(good.replace(b'VALUE_3=false', b'VALUE_3=true'))),
            ('semantic-dup', ig_precheck_stub(good.replace(b'.smudge', b'.clean'))),
            ('not-filter', ig_precheck_stub(good.replace(b'filter.', b'other.', 1))),
            ('unknown-item', ig_precheck_stub(good.replace(b'.clean', b'.unknown', 1))),
            ('empty-name', ig_precheck_stub(ig_tokens(ig_filters('')))),
            ('newline-name', ig_precheck_stub(ig_tokens(ig_filters('a\nb')))),
            ('dotted-name', ig_precheck_stub(ig_tokens(ig_filters('a.clean')))),
            ('only-nul', ig_precheck_stub(b'\0')), ('junk', ig_precheck_stub(b'GIT_CONFIG_COUNT=0\0junk\0')),
            ('rc2', ig_precheck_stub(code=2, stderr=b'note\n')), ('rc20', ig_precheck_stub(code=20)),
            ('rc22', ig_precheck_stub(code=22, stderr='承認ダイジェスト: x\n'.encode())),
            ('rc9', ig_precheck_stub(code=9)), ('rc1', ig_precheck_stub(code=1)),
            ('rc126', ig_precheck_stub(code=126)),
            ('killed', '#!/bin/bash\nkill -TERM $$\n'),
            ('stderr-big', ig_precheck_stub(stderr=bytes(range(256)) * 300)),
            ('stdout-big-bad', ig_precheck_stub(b'x' * 70000)),
        ]
        for label, body in cases:
            with self.subTest(label=label):
                world = IgWorld(self, precheck=body)
                old, _, _ = world.pair(['prepare', '--cwd', str(world.repo), '--accept', 'ok'])
                world.pair(world.run_args(IG_HASH, 'status', accept='ok'))
                if label == 'good':
                    self.assertEqual(old.returncode, 0)
                    digest = json.loads(old.stdout)['context_sha256']
                    world.pair(world.run_args(digest, 'status', accept='ok'))
                    world.pair(world.run_args(digest, 'status', accept='other'))

    def test_precheck_spawn_failures(self):
        world = IgWorld(self, precheck=ig_precheck_stub())
        only_git = world.base / 'only-git'
        only_git.mkdir()
        os.symlink(world.real_git, only_git / 'git')
        noexec = world.base / 'noexec'
        noexec.mkdir()
        (noexec / 'bash').write_text('#!/bin/sh\nexit 0\n')
        (noexec / 'git').write_text('#!/bin/sh\nexit 0\n')
        for label, env in (('no-bash', world.env(path=str(only_git))), ('no-path', world.env(PATH=None)),
                           ('empty-path', world.env(path=':')),
                           ('noexec-first', world.env(path=str(noexec) + os.pathsep + str(world.stubs) + os.pathsep +
                                                      world.real_path)),
                           ('noexec-only', world.env(path=str(noexec)))):
            with self.subTest(label=label):
                world.pair(['prepare', '--cwd', str(world.repo)], env=env)

    def test_precheck_deadlines(self):
        for label, kwargs in (('reading', {}), ('waiting', {'close_outputs': True})):
            with self.subTest(label=label):
                world = IgWorld(self)
                fifo = world.base / 'hold'
                os.mkfifo(str(fifo))
                world.precheck.write_text(ig_precheck_stub(block=str(fifo), **kwargs))
                entry = ig_rewrite(world.script, precheck='2')
                decisions = ('timeout-fraction',) if label == 'waiting' else ()
                case = 'implementation-git/timeout-waiting' if label == 'waiting' else 'implementation-git/contract'
                old, _, old_calls = world.pair(['prepare', '--cwd', str(world.repo)], entry=entry,
                                               decisions=decisions, case=case, limit=2)
                self.assertIn(b'timed out after', old.stderr)
                # 期限の来た子(FIFO で止めた事前検査の bash)の起動の記録も、列の最後で比べた。
                self.assertEqual(ig_held_call(old_calls)[:2], ('bash', str(world.precheck)))

    def test_git_failures_and_deadlines(self):
        world = IgWorld(self, precheck=ig_precheck_stub(ig_tokens(ig_filters('a'))))
        digest = world.prepared()
        bin_dir = world.base / 'fake-bin'
        bin_dir.mkdir()
        fake = bin_dir / 'git'
        fifo = world.base / 'git-hold'
        os.mkfifo(str(fifo))
        real = world.real_git
        behaviours = {
            'config-wrong': 'case "$*" in *config*) echo unexpected; exit 0;; esac; exec %s "$@"' % real,
            'config-rc1': 'case "$*" in *config*) exit 1;; esac; exec %s "$@"' % real,
            'config-rc7-stderr': 'case "$*" in *config*) echo bad >&2; exit 7;; esac; exec %s "$@"' % real,
            'toplevel-other': 'case "$*" in *show-toplevel*) echo /; exit 0;; esac; exec %s "$@"' % real,
            'toplevel-relative': 'case "$*" in *show-toplevel*) echo repo; exit 0;; esac; exec %s "$@"' % real,
            'toplevel-no-newline': 'case "$*" in *show-toplevel*) printf %%s "$PWD"; exit 0;; esac; exec %s "$@"' % real,
            'toplevel-not-utf8': "case \"$*\" in *show-toplevel*) printf '/\\377\\n'; exit 0;; esac; exec %s \"$@\"" % real,
            'toplevel-missing': 'case "$*" in *show-toplevel*) echo /nonexistent/x; exit 0;; esac; exec %s "$@"' % real,
            'read-rc7': 'case "$*" in *" status "*) printf partial; echo err >&2; exit 7;; esac; exec %s "$@"' % real,
            'read-killed': 'case "$*" in *" status "*) printf partial; kill -KILL $$;; esac; exec %s "$@"' % real,
            'read-big': 'case "$*" in *" status "*) head -c 300000 /dev/zero | tr "\\0" x; head -c 70000 /dev/zero '
                        '| tr "\\0" y >&2; exit 0;; esac; exec %s "$@"' % real,
            'rev-bad': 'case "$*" in *"rev-parse --verify"*) echo zz; exit 0;; esac; exec %s "$@"' % real,
            'rev-rc': 'case "$*" in *"rev-parse --verify"*) exit 128;; esac; exec %s "$@"' % real,
            'cat-tree': 'case "$*" in *cat-file*) echo tree; exit 0;; esac; exec %s "$@"' % real,
            'cat-rc': 'case "$*" in *cat-file*) exit 1;; esac; exec %s "$@"' % real,
        }
        ops = {'read-rc7': ('status',), 'read-killed': ('status',), 'read-big': ('status',),
               'rev-bad': ('show',), 'rev-rc': ('log', '--from', 'HEAD~1'), 'cat-tree': ('show', '--file', 'dir'),
               'cat-rc': ('show', '--file', 'normal.txt')}
        for label, body in behaviours.items():
            with self.subTest(label=label):
                fake.write_text('#!/bin/bash\n' + body + '\n')
                fake.chmod(0o755)
                env = world.env(path=str(bin_dir) + os.pathsep + str(world.stubs) + os.pathsep + world.real_path)
                world.pair(world.run_args(digest, *ops.get(label, ('status',))), env=env)
        recorded = world.base / 'recorded-bin'
        recorded.mkdir()
        write_stub(recorded, 'git', world.log, real=fake)
        for label, body, stage in (
                ('config-reading', 'case "$*" in *config*) echo $$ >"%s.notify"; read x <"%s";; esac; exec %s "$@"',
                 'reading'),
                ('read-waiting', 'case "$*" in *" status "*) exec >&- 2>&-; echo $$ >"%s.notify"; read x <"%s";; '
                 'esac; exec %s "$@"', 'waiting')):
            with self.subTest(label=label):
                fake.write_text('#!/bin/bash\n' + body % (fifo, fifo, real) + '\n')
                fake.chmod(0o755)
                entry = ig_rewrite(world.script, git='2')
                # 記録の代用品を偽の Git の前に置き、期限の来た子(FIFO で止めた Git)の起動の記録も比べる。
                env = world.env(path=os.pathsep.join([str(recorded), str(world.stubs), world.real_path]))
                decisions = ('timeout-fraction',) if stage == 'waiting' else ()
                case = 'implementation-git/timeout-waiting' if stage == 'waiting' else 'implementation-git/contract'
                old, _, old_calls = world.pair(world.run_args(digest, 'status'), env=env, entry=entry,
                                               decisions=decisions, case=case, limit=2)
                self.assertIn(b'timed out after', old.stderr)
                held = ig_held_call(old_calls)
                self.assertEqual(held[:2], ('git', '-C'))
                self.assertIn('config' if stage == 'reading' else 'status', held)
        ig_rewrite(world.script)
        only_bash = world.base / 'only-bash'
        only_bash.mkdir()
        os.symlink(shutil.which('bash', path=world.real_path), only_bash / 'bash')
        with self.subTest(label='no-git'):
            world.pair(world.run_args(digest, 'status'), env=world.env(path=str(only_bash)))

    def test_five_operations_argv_and_paths(self):
        world = IgWorld(self)
        accept = world.approval()
        digest = world.prepared(accept)
        initial = world.git('rev-list', '--max-parents=0', 'HEAD').stdout.decode().strip()
        cases = [('status',), ('status', '--format', 'porcelain', '--untracked', 'all'), ('status', '--untracked=no'),
                 ('diff',), ('diff', '--cached'), ('diff', '--base', initial), ('diff', '--base', initial, '--target',
                                                                                 'HEAD'),
                 ('diff', '--format', 'name-status', '--context', '0'), ('diff', '--format=stat', '--context=100'),
                 ('log',), ('log', '--limit', '1', '--format', 'fuller'), ('log', '--from', initial),
                 ('log', '--from', initial, '--to', 'HEAD'), ('show',), ('show', '--rev', initial),
                 ('show', '--format', 'name-only'), ('show', '--file', 'normal.txt'), ('show', '--file', 'link'),
                 ('show', '--file', 'dir'), ('show', '--file', 'missing'), ('show', '--rev', 'nope'),
                 ('ls-files',), ('ls-files', '--mode', 'untracked'),
                 ('status', '--path', 'normal.txt', '--path', '日本語 [a].txt'),
                 ('ls-files', '--path', 'space name.txt'), ('diff', '--path', '*.txt'), ('log', '--path', 'Case.txt')]
        for rest in cases:
            with self.subTest(rest=rest):
                world.pair(world.run_args(digest, *rest, accept=accept))


def installed_locale_lc_ctype(command, case, old_calls, new_calls):
    """入っていない地域設定の名前で、旧Pythonだけが子の環境へ LC_CTYPE=C.UTF-8 を足す差(2026-11-11 22:31 の回答)。
    旧版の子の環境が、Rust の子の環境の末尾にその1項目を足したものと同じこと。ほかは全部比べる。"""
    applied(command, 'installed-locale-lc-ctype', case)
    if len(old_calls) != len(new_calls) or not old_calls:
        raise Difference('子の起動の数が違う')
    # 旧版の実行環境が自分で足す変数(Mac の __CF_USER_TEXT_ENCODING)は、ほかの比較と同じく外して記録する
    # (2026-11-12 02:41 の決定。このケースも決定の cases にある)。
    old_calls, new_calls = runtime_added(command, case, old_calls, new_calls)
    added = b'LC_CTYPE=C.UTF-8'.hex()
    for left, right in zip(old_calls, new_calls):
        if left[0] != right[0] or left[1] != right[1] or left[3] != right[3]:
            raise Difference('子の起動のパス・引数・作業場所が違う')
        if left[4:5] != right[4:5]:
            raise Difference('子が継いだ信号の扱いか、開いている fd が違う')
        rest = list(left[2])
        if added not in rest or added in right[2]:
            raise Difference('旧版だけが LC_CTYPE=C.UTF-8 を渡す形でない')
        rest.remove(added)
        if left[5:6] == ['shell']:
            # bash の孫(事前検査の中の Git)は bash が環境を並べ直すので、値の集合で比べる。
            if sorted(rest) != sorted(right[2]):
                raise Difference('入っていない地域設定の差が LC_CTYPE の1項目だけでない')
        elif rest != right[2]:
            # 旧Pythonの直接の子(事前検査の bash・Git)は、1項目を除いた並びまで同じ。
            raise Difference('入っていない地域設定の差が LC_CTYPE の1項目だけでない(並びを含む)')


def ig_signal_run(world, argv, env, target, signal_number, launch=None):
    """git の代用品が着いた(FIFO の知らせ)のを待ってから信号を送る。終わった時点で代用品が残っているかを記録する。"""
    notify = Path(str(world.hold) + '.notify')
    for path in (world.hold, notify):
        if path.exists():
            path.unlink()
    os.mkfifo(str(world.hold))
    os.mkfifo(str(notify))
    process = subprocess.Popen(launched(argv, launch), cwd=str(world.base), env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    with open(notify, 'rb') as handle:
        stub_pid = int(handle.read().split()[0])
    wait_stub_blocked(stub_pid)
    if target == 'process':
        os.kill(process.pid, signal_number)
    else:
        os.killpg(process.pid, signal_number)
    finished = False
    try:
        out, err = process.communicate(timeout=3)
        finished = True
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + 2
    while pid_alive(stub_pid) and time.monotonic() < deadline and finished:
        time.sleep(0.02)
    remained = pid_alive(stub_pid)
    if remained:
        try:
            fd = os.open(str(world.hold), os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
    if not finished:
        out, err = process.communicate(timeout=60)
    deadline = time.monotonic() + 30
    while pid_alive(stub_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return RunResult(process.returncode, out, err, False), remained


class IgIO(unittest.TestCase):
    """旧Python 3.10の標準入出力・起動・信号・子の状態を、implementation-git の旧版とRustで比べる。"""

    def setUp(self):
        self.world = IgWorld(self, precheck=ig_precheck_stub(ig_tokens(ig_filters('a'))))
        self.digest = self.world.prepared()

    def big(self):
        """8KiB を超える読取りの出力(show の patch)。"""
        path = self.world.repo / 'big.dat'
        path.write_text('line\n' * 4000)
        self.world.git('add', 'big.dat')
        self.world.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'big')
        return self.world.run_args(self.digest, 'show')

    def test_closed_and_full_outputs(self):
        world = self.world
        big = self.big()
        small = world.run_args(self.digest, 'log')
        prepare = ['prepare', '--cwd', str(world.repo)]
        failure = ['prepare', '--cwd', '']
        cases = [('pipe-small', small, {'redirect': {'1': 'closed-pipe'}}, {}),
                 ('pipe-big', big, {'redirect': {'1': 'closed-pipe'}}, {}),
                 ('pipe-prepare', prepare, {'redirect': {'1': 'closed-pipe'}}, {}),
                 ('pipe-prepare-unbuffered', prepare, {'redirect': {'1': 'closed-pipe'}}, {'PYTHONUNBUFFERED': '1'}),
                 ('pipe-help', ['--help'], {'redirect': {'1': 'closed-pipe'}}, {}),
                 ('pipe-help-unbuffered', ['--help'], {'redirect': {'1': 'closed-pipe'}}, {'PYTHONUNBUFFERED': '1'}),
                 ('pipe-stderr-failure', failure, {'redirect': {'2': 'closed-pipe'}}, {}),
                 ('closed-stdout-run', small, {'redirect': {'1': None}}, {}),
                 ('closed-stdout-prepare', prepare, {'redirect': {'1': None}}, {}),
                 ('closed-stdout-help', ['run', '--help'], {'redirect': {'1': None}}, {}),
                 ('closed-stderr-failure', failure, {'redirect': {'2': None}}, {}),
                 ('closed-stderr-run', small, {'redirect': {'2': None}}, {}),
                 ('closed-both-help', ['--help'], {'redirect': {'1': None, '2': None}}, {}),
                 ('closed-stdin', small, {'redirect': {'0': None}}, {}),
                 ('stdin-directory', small, {'redirect': {'0': 'directory'}}, {}),
                 ('stdout-directory', small, {'redirect': {'1': 'directory'}}, {}),
                 ('stderr-directory', small, {'redirect': {'2': 'directory'}}, {})]
        if sys.platform.startswith('linux'):
            cases += [('full-big', big, {'redirect': {'1': '/dev/full'}}, {}),
                      ('full-small', small, {'redirect': {'1': '/dev/full'}}, {}),
                      ('full-prepare-unbuffered', prepare, {'redirect': {'1': '/dev/full'}}, {'PYTHONUNBUFFERED': '1'}),
                      ('full-stderr-failure', failure, {'redirect': {'2': '/dev/full'}}, {})]
        else:
            not_run(self, 'full-big・full-small・full-prepare-unbuffered・full-stderr-failure', 'Linux だけ(/dev/full)')
        for label, args, launch, changes in cases:
            with self.subTest(label=label):
                decisions, case, old_launch = (), 'implementation-git/io', None
                if label in ('stdin-directory', 'stdout-directory'):
                    decisions, case = ('current-thread',), 'implementation-git/startup-directory'
                if label.startswith('closed-'):
                    # 2026-11-12 08:55 の決定(rust-std-fd-reopen): 閉じた fd を /dev/null にした旧版が期待。
                    applied(IG, 'rust-std-fd-reopen', 'implementation-git/closed-fd')
                    old_launch = std_fd_reopened(launch)
                world.pair(args, env=world.env(**changes), launch=launch, decisions=decisions, case=case,
                           old_launch=old_launch)

    def test_file_size_limit_on_stdout_and_stderr(self):
        world = self.world
        args = self.big()
        plain, _, _ = world.pair(args)
        size = len(plain.stdout)
        small = world.run_args(self.digest, 'log')
        small_size = len(world.pair(small)[0].stdout)
        # 8KiB を超える出力は書込みの途中で EFBIG(ERROR [runtime] と終了20)、小さい出力は終了時の flush で
        # EFBIG(Exception ignored と終了120)になる。
        limits = [(args, limit) for limit in sorted({0, 1, 4096, 8191, 8192, 8193, size - 1, size})]
        limits += [(small, limit) for limit in (0, 10, small_size - 1, small_size)]
        for args, limit in limits:
            with self.subTest(stream='stdout', limit=limit, op=args[-1]), \
                    tempfile.TemporaryDirectory(dir=str(world.base)) as temp:
                outputs = [Path(temp) / 'old.out', Path(temp) / 'new.out']
                results = []
                for index, argv in enumerate(([sys.executable, str(world.script), *args],
                                              ig_native(args, world.script))):
                    launch = {'fsize': limit, 'redirect': {'1': str(outputs[index])}}
                    results.append(world.run(argv, launch=launch, env=world.env(path=world.real_path))[0])
                ig_compare(*results, case='implementation-git/io')
                self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
                if args is small and limit < small_size:
                    self.assertEqual(results[0].returncode, 120)
        failure = ['prepare', '--cwd', str(world.base / 'missing')]
        full, _, _ = world.pair(failure)
        for limit in (0, 10, len(full.stderr) - 1, len(full.stderr)):
            with self.subTest(stream='stderr', limit=limit), tempfile.TemporaryDirectory(dir=str(world.base)) as temp:
                errors = [Path(temp) / 'old.err', Path(temp) / 'new.err']
                results = []
                for index, argv in enumerate(([sys.executable, str(world.script), *failure],
                                              ig_native(failure, world.script))):
                    launch = {'fsize': limit, 'redirect': {'2': str(errors[index])}}
                    results.append(world.run(argv, launch=launch, env=world.env(path=world.real_path))[0])
                ig_compare(*results, case='implementation-git/io')
                self.assertEqual(errors[0].read_bytes(), errors[1].read_bytes())

    def test_startup_environment_and_encoding(self):
        world = self.world
        args = world.run_args(self.digest, 'status')
        for changes in ({'PYTHONINTMAXSTRDIGITS': '1'}, {'PYTHONHASHSEED': 'abc'}, {'PYTHONHASHSEED': '0'},
                        {'PYTHONUNBUFFERED': '1'}, {'PYTHONINTMAXSTRDIGITS': '700'}, {'PYTHONOPTIMIZE': '2'},
                        {'PYTHONCOERCECLOCALE': 'warn', 'LC_ALL': 'C'},
                        {'PYTHONCOERCECLOCALE': 'warn', 'LC_ALL': None}):
            with self.subTest(changes=changes):
                world.pair(args, env=world.env(**changes))
        expected, _, _ = world.pair(['--help'])
        for changes in ({'PYTHONIOENCODING': 'ascii'}, {'LC_ALL': 'C', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'},
                        {'PYTHONIOENCODING': 'latin-1'}):
            with self.subTest(encoding=changes):
                env = world.env(**changes)
                new, _ = world.run(ig_native(['--help'], world.script), env)
                encoding_fixed(IG, 'implementation-git/encoding', expected, new)

    def test_signals_while_waiting_for_git(self):
        world = self.world
        world.hold = world.base / 'hold'
        bin_dir = world.base / 'sig-bin'
        bin_dir.mkdir()
        (bin_dir / 'git').write_text('#!/bin/bash\ncase "$*" in *" status "*) echo $$ >"%s.notify"; read x <"%s"; '
                                     'exit 0;; esac\nexec %s "$@"\n' % (world.hold, world.hold, world.real_git))
        (bin_dir / 'git').chmod(0o755)
        env = world.env(path=str(bin_dir) + os.pathsep + world.real_path)
        args = world.run_args(self.digest, 'status')
        for target, number, launch in (('process', signal.SIGINT, None), ('group', signal.SIGINT, None),
                                       ('process', signal.SIGTERM, None), ('group', signal.SIGTERM, None),
                                       ('process', signal.SIGINT, {'ignore_sigint': True}),
                                       ('process', signal.SIGHUP, None)):
            with self.subTest(target=target, signal=number, launch=launch):
                results = [ig_signal_run(world, argv, env, target, number, launch)
                           for argv in ([sys.executable, str(world.script), *args], ig_native(args, world.script))]
                (old, old_remained), (new, new_remained) = results
                if number == signal.SIGINT and launch is None:
                    sigint_pair(IG, 'implementation-git/sigint', old, new)
                    # 旧Pythonの KeyboardInterrupt は子を待ってから止めて回収する(子を残さない)。
                    self.assertEqual((old_remained, new_remained), (False, False))
                else:
                    ig_compare(old, new, case='implementation-git/io')
                    self.assertEqual(old_remained, new_remained)

    def test_child_state(self):
        """子の fd(継いだ fd を閉じる)・信号の扱い(SIGPIPE・SIGXFSZ を既定へ)・mask(継ぐ)を、子の報告で比べる。"""
        if not sys.platform.startswith('linux'):
            not_run(self, None, 'Linux だけ(子の状態を /proc で報告する)')
            return
        world = self.world
        bin_dir = world.base / 'state-bin'
        bin_dir.mkdir()
        # fd の一覧は、パイプラインの前にファイルへ書く(`ls | sort` の形では、ls が読む間に代用品の bash が次の
        # pipe の端を持つかが負荷で変わり、旧版と Rust の差でない揺れになる)。
        listing = shlex_quote(str(world.base / 'state-fds'))
        (bin_dir / 'git').write_text(
            '#!/bin/bash\ncase "$*" in *" status "*) ls /proc/$$/fd >%s; sort -n %s | tr "\\n" " "; echo; '
            'grep -E "^Sig(Blk|Ign)" /proc/$$/status; exit 0;; esac\nexec %s "$@"\n' % (listing, listing, world.real_git))
        (bin_dir / 'git').chmod(0o755)
        env = world.env(path=str(bin_dir) + os.pathsep + world.real_path)
        args = world.run_args(self.digest, 'status')
        for launch in ({'keep_fd': 5}, {'block': ['SIGUSR1']}, {'ignore': ['SIGUSR2', 'SIGTERM']}, None):
            with self.subTest(launch=launch):
                old, _, _ = world.pair(args, env=env, launch=launch)
                self.assertIn(b'Sig', old.stdout)

    def test_long_current_directory(self):
        world = self.world
        parts = ['d' * 200] * 25
        current = os.open(str(world.base), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                os.mkdir(part, dir_fd=current)
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                os.close(current)
                current = following
        finally:
            os.close(current)
        script = 'cd "$1" || exit 99; shift; for p in $(seq 25); do cd ' + parts[0] + ' || exit 98; done; exec "$@"'
        for argv in (['prepare', '--cwd', '.'], ['prepare', '--cwd', '..']):
            with self.subTest(argv=argv):
                env = world.env()
                old = run_command(['bash', '-c', script, 'r4g', str(world.base), sys.executable, str(world.script),
                                   *argv], cwd=str(world.base), env=env, timeout=60)
                new = run_command(['bash', '-c', script, 'r4g', str(world.base), *ig_native(argv, world.script)],
                                  cwd=str(world.base), env=env, timeout=60)
                ig_compare(old, new, case='implementation-git/io')


def ig_git_state(repo):
    """`.git/index` と参照(HEAD・refs・packed-refs)の内容と時刻(mtime・ctime)と番号。"""
    git_dir = Path(repo) / '.git'
    selected = [git_dir / 'index', git_dir / 'HEAD', git_dir / 'packed-refs']
    selected += sorted(path for path in (git_dir / 'refs').rglob('*'))
    state = {}
    for path in selected:
        if not path.exists() or path.is_dir():
            continue
        info = path.lstat()
        state[str(path)] = (path.read_bytes(), info.st_mode, info.st_ino, info.st_dev, info.st_size,
                            info.st_mtime_ns, info.st_ctime_ns)
    if str(git_dir / 'index') not in state:
        raise Difference('.git/index が無い')
    return state


class IgReadonly(unittest.TestCase):
    """完了条件5(implementation-git-readonly): run の5操作を実Gitで流し、前後で .git/index と参照の内容・時刻が
    変わらない(旧版と Rust の両方)。出力と子の起動の列も旧版と比べる。"""

    def world(self):
        world = IgWorld(self)
        # index より古い時刻にそろえてから index を取り直し、racy な項目(index と同じ秒の変更)を作らない。
        past = time.time() - 100
        for path in world.repo.rglob('*'):
            if '.git' not in path.relative_to(world.repo).parts and not path.is_symlink():
                os.utime(path, (past, past))
        subprocess.run([world.real_git, '-C', str(world.repo), 'update-index', '-q', '--refresh'],
                       env=world.setup_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return world

    def ops(self, world):
        initial = world.git('rev-list', '--max-parents=0', 'HEAD').stdout.decode().strip()
        return [('status',), ('diff',), ('diff', '--base', initial), ('diff', '--format', 'stat'),
                ('diff', '--cached'), ('log',), ('show',), ('ls-files',)]

    def runs(self, world, op, accept, digest, reset=None):
        args = world.run_args(digest, *op, accept=accept)
        results = []
        for argv in ([sys.executable, str(world.script), *args], ig_native(args, world.script)):
            if reset is not None:
                reset()
            before = ig_git_state(world.repo)
            result, calls = world.run(argv)
            results.append((result, calls, ig_git_state(world.repo) != before))
        ig_compare(results[0][0], results[1][0], case='implementation-git/contract')
        same_calls(IG, results[0][1], results[1][1], case='implementation-git/calls')
        return results

    def test_five_operations_do_not_touch_index_or_refs(self):
        world = self.world()
        accept = world.approval()
        digest = world.prepared(accept)
        for op in self.ops(world):
            with self.subTest(op=op):
                results = self.runs(world, op, accept, digest)
                self.assertEqual([result.returncode for result, _, _ in results], [0, 0])
                self.assertEqual([changed for _, _, changed in results], [False, False])
                OBSERVATIONS.append(f'implementation-git-readonly: {" ".join(op)} の前後で index と参照が同じ'
                                    '(旧版・Rust)')

    def test_stat_only_changes_behave_like_the_old_version(self):
        """中身が同じで stat だけが違うファイルがあるとき。status は index を書かない(GIT_OPTIONAL_LOCKS=0)。
        diff は旧版でも Git の自動の取り直し(diff.autorefreshindex)で index を書く。Rust も同じであることを確かめ、
        旧版の振る舞いとして記録する。"""
        world = self.world()
        accept = world.approval()
        digest = world.prepared(accept)
        future = time.time() + 1000
        index = (world.repo / '.git/index').read_bytes()

        def reset():
            # 旧版と Rust の前に同じ状態(取り直す前の index と、stat だけが違うファイル)へ戻す。
            (world.repo / '.git/index').write_bytes(index)
            for name in IgWorld.NAMES[1:]:
                os.utime(world.repo / name, (future, future))
        for op in self.ops(world):
            with self.subTest(op=op):
                results = self.runs(world, op, accept, digest, reset)
                changed = [changed for _, _, changed in results]
                self.assertEqual(changed[0], changed[1])
                if op[0] != 'diff':
                    self.assertEqual(changed, [False, False])
                OBSERVATIONS.append(f'implementation-git-readonly: stat だけが違うとき {" ".join(op)} が index を'
                                    f'書いた: 旧版 {changed[0]}・Rust {changed[1]}')


class IgRealDeadlines(unittest.TestCase):
    """本物の期限(Git 30秒・事前検査 120秒)を、機械ごとに少なくとも1回測る。旧版と Rust を並べて流す。"""

    def run_both(self, world, args, env):
        import threading
        results = [None, None]

        def run(index, argv):
            started = time.monotonic()
            result = run_command(argv, cwd=str(world.base), env=env, timeout=400)
            results[index] = (result, time.monotonic() - started)
        threads = [threading.Thread(target=run, args=(index, argv)) for index, argv in
                   enumerate(([sys.executable, str(world.script), *args], ig_native(args, world.script)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_git_30_and_precheck_120_seconds(self):
        for label, limit in (('git', 30), ('precheck', 120)):
            with self.subTest(label=label):
                world = IgWorld(self, precheck=ig_precheck_stub(ig_tokens(ig_filters('a'))))
                digest = world.prepared()
                hold = world.base / 'never'
                os.mkfifo(str(hold))
                if label == 'precheck':
                    world.precheck.write_text('#!/bin/bash\nread x <%s\n' % shlex_quote(hold))
                    args = ['prepare', '--cwd', str(world.repo)]
                    env = world.env()
                else:
                    bin_dir = world.base / 'slow-bin'
                    bin_dir.mkdir()
                    (bin_dir / 'git').write_text('#!/bin/bash\ncase "$*" in *" status "*) read x <%s;; esac\n'
                                                 'exec %s "$@"\n' % (shlex_quote(hold), world.real_git))
                    (bin_dir / 'git').chmod(0o755)
                    args = world.run_args(digest, 'status')
                    env = world.env(path=str(bin_dir) + os.pathsep + world.real_path)
                (old, old_time), (new, new_time) = self.run_both(world, args, env)
                ig_compare(old, new, case='implementation-git/contract')
                self.assertIn(f'timed out after {limit} seconds'.encode(), old.stderr)
                for elapsed in (old_time, new_time):
                    self.assertGreaterEqual(elapsed, limit)
                    self.assertLess(elapsed, limit + 20)
                OBSERVATIONS.append(f'implementation-git: 本物の期限 {label} {limit} 秒: 旧版 {old_time:.1f} 秒・'
                                    f'Rust {new_time:.1f} 秒')


# ---------------------------------------------------------------- 入口の節(旧版の無い固定期待)

class EntryContract(unittest.TestCase):
    """検査入口の枠。旧版は無いので、固定期待だけで確かめる。"""

    def run_entry(self, *args):
        return run_command([str(FIXTURE), *args], cwd=ROOT, env=dict(os.environ), timeout=30)

    def test_missing_unknown_and_bad_entry_options_are_fixed_failures(self):
        fixed(self.run_entry(), 2, stderr=ENTRY_USAGE)
        fixed(self.run_entry('probe'), 2, stderr=ENTRY_USAGE)
        fixed(self.run_entry('--r4g-unknown', 'x', 'reviews-dir'), 2,
              stderr='r4g_contract: 入口の指定が不正: --r4g-unknown\n'.encode())
        fixed(self.run_entry('--r4g-clock', '1', 'reviews-dir'), 2,
              stderr='r4g_contract: --r4g-clock はこのコマンドに使えません\n'.encode())

    def test_entry_values_are_required(self):
        fixed(self.run_entry('reviews-dir'), 2, stderr='r4g_contract: reviews-dir には --r4g-script-name が要ります\n'.encode())
        fixed(self.run_entry('implementation-git'), 2,
              stderr='r4g_contract: implementation-git には --r4g-script-file が要ります\n'.encode())
        fixed(self.run_entry('--r4g-deadlines', '1,1', 'reviews-dir'), 2,
              stderr='r4g_contract: --r4g-deadlines はこのコマンドに使えません\n'.encode())
        fixed(self.run_entry('--r4g-mutation', 'other', IG), 2,
              stderr='r4g_contract: 入口の指定が不正: --r4g-mutation\n'.encode())
        # 知らない呼出しの名前の故障の規則は、入口で拒む(旧版の包みも拒む。test_r4g.py の対照)。
        fixed(self.run_entry('--r4g-pending-fault', '{"rules":[{"op":"bogus","errno":2}]}', PENDING), 2,
              stderr='r4g_contract: 入口の指定が不正: --r4g-pending-fault\n'.encode())
        fixed(self.run_entry('--r4g-pending-fault', '{"x":1}', PENDING), 2,
              stderr='r4g_contract: 入口の指定が不正: --r4g-pending-fault\n'.encode())


SECTIONS = (('entry', EntryContract), ('pending-function-cases', PendingFunctionCases),
            ('pending-injection', PendingInjectionPairs), ('pending-checkpoints', PendingCheckpoints),
            ('pending-io', PendingIO), ('pending-io-linux', PendingIOLinux), ('pending-contract', PendingContract),
            ('pending-observation', PendingObservation), ('reviews-tools', ReviewsTools),
            ('reviews-contract', ReviewsContract), ('reviews-signals', ReviewsSignals),
            ('reviews-dir-filter-env', ReviewsFilterEnv), ('implementation-git-correspondence', IgCorrespondence),
            ('implementation-git-split', IgSplit), ('implementation-git-arguments', IgArguments),
            ('implementation-git-contract', IgContract), ('implementation-git-io', IgIO),
            ('implementation-git-readonly', IgReadonly), ('implementation-git-deadlines-real', IgRealDeadlines))

# 未決の項目だけを比べる節。流して結果を記録するが、合格の件数に数えない(今は無い)。
UNDECIDED_SECTIONS = ()


# 前提の外の旧Pythonの起動の環境変数(python-startup-env・python-startup-env-more の一覧と、利用者の site)。
PYTHON_OUTSIDE_PREMISES = frozenset((
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONPROFILEIMPORTTIME', 'PYTHONVERBOSE', 'PYTHONINSPECT', 'PYTHONWARNINGS',
    'PYTHONMALLOC', 'PYTHONDEVMODE', 'PYTHONFAULTHANDLER', 'PYTHONTRACEMALLOC', 'PYTHONMALLOCSTATS',
    'PYTHONPLATLIBDIR', 'PYTHONWARNDEFAULTENCODING', 'PYTHONPYCACHEPREFIX', 'PYTHONEXECUTABLE',
    'PYTHONUSERBASE', 'PYTHONNOUSERSITE'))


def socket_path_limit(platform=None):
    """AF_UNIX のソケットのパスの長さの上限(sun_path の大きさ - 1)。Linux は 107、Mac は 103。"""
    platform = sys.platform if platform is None else platform
    return 103 if platform == 'darwin' else 107


def main(argv=None):
    global FIXTURE, REQUIRE_USERNS
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--require-strace', action='store_true',
                        help='strace が使えなければ失敗にする(hp-pc など strace の記録を必須にする機械)')
    parser.add_argument('--require-userns', action='store_true',
                        help='/proc を隠す組の名前空間を作れなければ失敗にする(Linux の CI。手元の作れない容器では付けない)')
    parser.add_argument('--allow-undecided', action='store_true',
                        help='未決の項目が残っても、記録して合格の行を出す(既定は、残れば失敗にする)')
    args = parser.parse_args(argv)
    FIXTURE = fixture_path(args.fixture)
    REQUIRE_USERNS = args.require_userns
    python_requirements()
    # 既存テストは一時フォルダの下に AF_UNIX のソケットを作る(パスは 107 バイトまで)。
    socket_path = len(os.fsencode(tempfile.gettempdir())) + len('/pending-contract-xxxxxxxx/xdg/dev-workflow/guard-socket')
    if socket_path > socket_path_limit():
        raise ValueError(f'一時フォルダのパスが長い(ソケットのパスが {socket_path} バイト。{socket_path_limit()} まで)。'
                         'TMPDIR を短い名前のフォルダにしてから流す')
    # 旧Pythonの起動を変える環境変数のうち、決定(2026-11-11 22:31・2026-11-12 00:28)で前提の外とした名前だけを、
    # 比較の環境から外す(設計: 比較では旧版の環境から除く)。ほかの PYTHON*(再現する PYTHONUNBUFFERED などや、
    # 文字コードの決定の対象)は外さずに比べる。
    removed = sorted(key for key in os.environ if key in PYTHON_OUTSIDE_PREMISES)
    for key in removed:
        del os.environ[key]
    if removed:
        print('R4G: 比較の環境から外した変数: ' + ', '.join(removed))
    verify_fixture_identity(FIXTURE)
    module = load(Path(__file__).with_name('test_r4g.py'), 'r4g_comparator_tests')
    module.subject = sys.modules[__name__]
    count = run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(module.Comparator))
    print('R4G: 受け継いだ資源の上限(設定しない): ' + ' '.join(
        f'{name}={soft}/{hard}' for name, (soft, hard) in inherited_limits().items()))
    control = 'strace の対照: 未実施(この機械では使えない)'
    usable = strace_usable()
    if args.require_strace and not usable:
        raise ValueError('--require-strace を指定したが、strace を使えない')
    if usable:
        count += run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(module.StraceControl))
        control = 'strace の対照: 実施'
    count += run_suite(pending_existing_suite())
    count += run_suite(ig_existing_suite())
    for name, case in SECTIONS:
        if name == 'pending-io-linux':
            if not sys.platform.startswith('linux'):
                for method in unittest.TestLoader().getTestCaseNames(case):
                    if method in vars(case):
                        NOT_RUN.append((f'{name}.{method}', None, '/dev/full と pipe の大きさの指定は Linux だけ', False))
                continue
            own = [method for method in unittest.TestLoader().getTestCaseNames(case) if method in vars(case)]
            count += run_suite(unittest.TestSuite(case(method) for method in own))
            continue
        if name == 'pending-observation' and not usable:
            # strace の観測は、使える機械だけで数える(--require-strace の機械では上で止まる)。
            suite = unittest.TestSuite(case(method) for method in
                                       ('test_sources_do_not_reference_child_processes_or_writers',
                                        'test_git_is_never_started'))
            count += run_suite(suite)
            continue
        if name in REVIEWS_SECTIONS and sys.platform == 'darwin' and not tool_baseline_matches(tool_versions()):
            # 2026-11-12 00:35 の決定: Mac で基準の版の道具(bash 5.2.21 など)を用意できなければ未実施と記録する。
            applied(REVIEWS, 'bash-tools-baseline', name)
            not_run_section(name, case, 'Mac に基準の版の道具が無い(' + ', '.join(
                f'{tool} {version}' for tool, version in sorted(tool_versions().items())) + ')')
            continue
        count += run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    for name, case in UNDECIDED_SECTIONS:
        run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    for line in OBSERVATIONS:
        print('R4G: 観測: ' + line)
    lines, failed = undecided_verdict(UNDECIDED, args.allow_undecided)
    for line in lines:
        print(line)
    if failed:
        raise AssertionError(f'未決が {len(UNDECIDED)} 件残っている(--allow-undecided を付けたときだけ記録して続ける)')
    for line in final_summary(count, NOT_RUN, control):
        print(line)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ValueError, OSError) as error:
        print(f'R4G: 不合格: {error}', file=sys.stderr)
        sys.exit(1)
