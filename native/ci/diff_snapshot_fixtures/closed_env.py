"""旧版とRustへ渡す環境を、閉じた許可一覧から組み直す(設計「旧bashと旧Pythonの実行環境」)。

- 許可一覧は`PATH`・`HOME`・`TMPDIR`と、fixtureが固定した変数と、ケースが明示した変数だけ(environment.json)。
- `HOME`と`TMPDIR`はケースごとの専用フォルダ。`HOME`の中身(Gitの全体の設定の有無など)もfixtureに固定する。
- 比較器は起動の初めに自分の環境を許可一覧へ縮め、周囲の`PYTHON*`・`LC_*`・`LANG`・`GIT_*`・`BASH_*`・
  `COLUMNS`などを子へ渡さない。外した名前だけを記録する(値は記録しない)。
- 旧版とRustを起動するたびに、名前が閉じた一覧にあることと`HOME`の中身を確かめ、組み直した全変数を
  ケースのフォルダ名ごとに記録する。周囲の変数が漏れたら失敗にする。
- bashが環境から読み込んで動きを変える変数(bash_behavior_vars.json。2026-11-12 09:58 追加の決定 3)は、
  比較の入力に使わない。許可一覧がそれらを許していないことを読み込み時に確かめ、起動のたびに環境にあれば失敗にする。
- 例外: startup_env の群は、警告だけになる値のBASH_COMPAT・BASH_XTRACEFDと、実行中に値を作る変数を、ケースの間だけ
  case_names で明示して渡す(EXCEPTABLE に限る)。
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re

from . import bash_behavior_vars

SPEC = json.loads(Path(__file__).with_name('environment.json').read_text())
ALLOWED = tuple(SPEC['allowed'])
PATTERNS = tuple(re.compile(pattern) for pattern in SPEC['case_patterns'])
NAMES = frozenset(SPEC['case_names']) | frozenset(SPEC['fixed']) | frozenset(ALLOWED)
# 比較器の自分の環境に残す名前(子へは渡さない。TMPDIRは比較器の一時フォルダの置き場)。
OWN = ('PATH', 'TMPDIR')
# 周囲の変数の代わりの印。縮める直前に比較器の環境へ置き、縮めた後の子の環境に現れたら漏れとみなす。
CANARY = 's3d-ambient-canary-7c1f'
CANARY_NAMES = ('PYTHONPATH', 'PYTHONHASHSEED', 'PYTHONINTMAXSTRDIGITS', 'LC_ALL', 'LC_CTYPE', 'LANG', 'LANGUAGE',
                'GIT_DIR', 'GIT_TRACE', 'GIT_CONFIG_PARAMETERS', 'BASH_ENV', 'BASHOPTS', 'COLUMNS', 'SHLVL', 'USER')
# bashの動きを変える変数の印(一覧の各名前。BASH_FUNC_<名>%% は名前の例を1つ)。周囲にあっても子へ渡らないことを確かめる。
BEHAVIOR_SAMPLES = bash_behavior_vars.samples()
CANARY_NAMES += tuple(name for name in BEHAVIOR_SAMPLES if name not in CANARY_NAMES)
bash_behavior_vars.check_classification()
bash_behavior_vars.check_allowed(NAMES, PATTERNS)
_state = {'closed': False, 'removed': [], 'path': None}
# ケースが明示して渡す変数(startup_env)。一覧の変数は、警告だけになる値を持つもの(other_values)に限る。
_CASE = set()
EXCEPTABLE = frozenset({'BASH_COMPAT', 'BASH_XTRACEFD', 'SECONDS', 'RANDOM', 'SRANDOM', 'BASHPID', 'EPOCHSECONDS',
                        'EPOCHREALTIME', 'BASH_COMMAND', 'BASH_SUBSHELL', 'HISTCMD', 'LINENO'})
ENVIRONMENTS = {}
CASES = {}


def close_process():
    """比較器の環境を許可一覧へ縮める。外した名前を返す(値は持たない)。"""
    if 'PATH' not in os.environ:
        raise RuntimeError('比較器のPATHがありません')
    removed = sorted(name for name in os.environ if name not in OWN)
    for name in CANARY_NAMES:
        os.environ[name] = CANARY
    for name in tuple(os.environ):
        if name not in OWN:
            del os.environ[name]
    _state.update(closed=True, removed=removed, path=os.environ['PATH'])
    return removed


def base(root, temporary=None):
    """ケースのフォルダrootの専用HOME・TMPDIR(temporaryの指定が無ければroot/tmp)と、fixtureが固定した変数だけの環境。"""
    root = Path(root)
    home, temporary = root / 'home', Path(temporary) if temporary is not None else root / 'tmp'
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    env = {'PATH': os.environ['PATH'], 'HOME': str(home), 'TMPDIR': str(temporary)}
    for name, value in SPEC['fixed'].items():
        env[name] = value.replace('<ROOT>', str(root))
        if value.startswith('<ROOT>/'):
            Path(env[name]).mkdir(exist_ok=True)
    return env


def allowed_name(name):
    return name in NAMES or name in _CASE or any(pattern.fullmatch(name) for pattern in PATTERNS)


@contextlib.contextmanager
def case_names(names):
    """このケースの間だけ、明示した変数を許可一覧に足し、一覧の検査の例外にする(値が警告だけかは、ケースが
    旧版の固定の警告で確かめる)。足せるのは EXCEPTABLE だけ。"""
    names = set(names)
    if not names <= EXCEPTABLE:
        raise AssertionError(f'ケースの許可一覧に足せない変数です: {sorted(names - EXCEPTABLE)}')
    if _CASE:
        raise AssertionError('ケースの許可一覧を重ねて足しました')
    _CASE.update(names)
    try:
        yield
    finally:
        _CASE.clear()


def check(env):
    """名前が閉じた一覧にあり、HOMEの中身がfixtureの指定どおりであることを確かめる。"""
    if not _state['closed']:
        raise AssertionError('比較器の環境を許可一覧へ縮める前に起動しました')
    if any(CANARY in str(value) for value in env.values()):
        raise AssertionError('周囲の変数が子の環境へ漏れました')
    bash_behavior_vars.check({name: value for name, value in env.items() if name not in _CASE})
    leaked = sorted(name for name in env if not allowed_name(name))
    if leaked:
        raise AssertionError(f'許可一覧に無い環境変数を子へ渡しました: {leaked}')
    if 'PATH' not in env:
        raise AssertionError('子の環境にPATHがありません')
    home = Path(env.get('HOME', ''))
    if 'HOME' in env and home.name == 'home' and home.is_dir():
        found = sorted(entry.name for entry in home.iterdir())
        if found != sorted(SPEC['home']):
            raise AssertionError(f'HOMEの中身がfixtureの指定と違います: {found}')


def record(env, cwd):
    """組み直した全変数を、ケースのフォルダ名ごとに記録する(rootを<ROOT>へ対応づけて重複を除く)。"""
    root = os.fsencode(str(cwd))
    values = {}
    for name, value in env.items():
        raw = os.fsencode(value) if isinstance(value, str) else bytes(value)
        values[os.fsencode(name).hex()] = raw.replace(root, b'<ROOT>').hex()
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
    ENVIRONMENTS.setdefault(digest, values)
    CASES.setdefault(Path(cwd).name, []).append(digest)
    return digest


def check_recorded_difference(cwd, first, second, names):
    """元と参照の環境の差を、記録した全変数から確かめる(差はnamesの変数だけ)。"""
    digests = CASES.get(Path(cwd).name, [])
    try:
        one, two = ENVIRONMENTS[digests[first]], ENVIRONMENTS[digests[second]]
    except IndexError as error:
        raise AssertionError('元と参照の環境の記録がありません') from error
    changed = {bytes.fromhex(name).decode('utf-8', 'surrogateescape') for name in set(one) | set(two)
               if one.get(name) != two.get(name)}
    if not changed or not changed <= set(names):
        raise AssertionError(f'記録した元と参照の環境の差が、指定の変数のほかを含みます: {sorted(changed)}')


def guard(env, cwd):
    check(env)
    return record(env, cwd)


def self_check(root):
    """比較器の変異: 周囲の変数が漏れる環境を拒否できることを確かめる。"""
    if any(name in os.environ for name in CANARY_NAMES) or set(os.environ) - set(OWN):
        raise AssertionError('比較器の環境が許可一覧へ縮まっていません')
    Path(root).mkdir(parents=True, exist_ok=True)
    env = base(root)
    check(env)
    mutants = {'copy-ambient': dict(env, **{name: CANARY for name in CANARY_NAMES}),
               'unlisted-name': dict(env, USER='x')}
    mutants.update({'leak-' + name: dict(env, **{name: CANARY}) for name in CANARY_NAMES})
    home = Path(env['HOME']) / '.gitconfig'
    rejected = []
    for name, value in mutants.items():
        try:
            check(value)
        except AssertionError:
            rejected.append(name)
    home.write_bytes(b'[core]\n')
    try:
        check(env)
    except AssertionError:
        rejected.append('home-gitconfig')
    finally:
        home.unlink()
    if sorted(rejected) != sorted([*mutants, 'home-gitconfig']):
        raise AssertionError(f'周囲の変数が漏れる変異を拒否できません: {sorted(set(mutants) - set(rejected))}')
    # bashの動きを変える変数: 許可一覧の確かめより先に、一覧の確かめが止めることを名前ごとに確かめる。
    missed = []
    for name, value in BEHAVIOR_SAMPLES.items():
        try:
            check(dict(env, **{name: value}))
        except AssertionError as error:
            if '動きを変える変数' in str(error):
                rejected.append('behavior-' + name)
                continue
        missed.append(name)
    if missed:
        raise AssertionError(f'bashの動きを変える変数を入力から外せません: {missed}')
    return rejected


def report():
    return {'allowed': list(ALLOWED), 'fixed': SPEC['fixed'], 'home': SPEC['home'],
            'removed_from_comparator': _state['removed'], 'comparator_path': _state['path'],
            'environments': ENVIRONMENTS, 'cases': CASES, 'bash_behavior_vars': bash_behavior_vars.report()}
