"""bashが環境から読む変数のうち、旧版どおりに再現する部分(2026-11-12 09:58 追加の決定 3 の後の指示)。

- 起動時の警告: 範囲の外のBASH_COMPATと、整数でないか開いていない番号か読むだけの番号のBASH_XTRACEFDで、
  bash 5.2.21が出す英語の警告(先頭は固定名bash)。この変数だけをケースの許可一覧で明示して渡し、一覧の検査の
  例外にする。旧版の標準エラーの頭が固定の警告と同じことで、値が警告だけ(効果なし)の値であることを確かめる。
- 実行中に値を作る変数が子へ渡る値: 基準の容器で観測した旧版の値と比べる。RANDOM・SRANDOM・BASHPID・
  BASH_COMMAND・BASH_SUBSHELL・HISTCMD は子へ渡らない。SECONDS は受け継いだ文字列のまま渡る。
  EPOCHSECONDS・EPOCHREALTIME は06:58の決定のとおり、表記の形と実行の始めから終わりの間にあることを確かめ、
  ほかの全部を比べる。LINENO は旧版が子の環境を組み直した行の値(決まった値)になり、全バイトで比べる。
  組み直す時と行は流れで変わるので、通常・profileのほかに、組み直しの印や行が変わる流れ(LINENO_SETUPS)を
  LINENO だけを足して比べる。値は基準の bash 5.2.21 に合わせる(bash 5.1 は組み直す時が違う)。版に依る欄
  (「ID:LINENO」)として記録し、基準でない機械では欄を比べずに失敗として記録する。
"""
import hashlib
import os
from pathlib import Path
import re

from . import closed_env, review_cases
from .compare import run, same_result, unchanged

GROUP = 'startup-env'
DYNAMIC = ('SECONDS', 'RANDOM', 'SRANDOM', 'BASHPID', 'EPOCHSECONDS', 'EPOCHREALTIME', 'BASH_COMMAND',
           'BASH_SUBSHELL', 'HISTCMD', 'LINENO')
TIME_FORMS = {b'EPOCHSECONDS': re.compile(rb'[0-9]+'), b'EPOCHREALTIME': re.compile(rb'[0-9]+\.[0-9]{6}')}
COMPAT = b': compatibility value out of range\n'
INVALID = b': invalid value for trace file descriptor\n'


def _warning(name, value, tail):
    return 'bash: ' + name + ': ' + value + tail.decode()


def cases():
    items = []

    def add(name, variant, **values):
        items.append(dict(id='startup-env-' + name, group=GROUP, kind='startup-env', variant=variant, **values))
    # 基準の容器(bash 5.2.21)で観測した警告。空の値は警告も効果も無い。
    for name, value in (('99', '99'), ('5-3', '5.3'), ('30', '30'), ('3-0', '3.0'), ('abc', 'abc'), ('5-10', '5.10'),
                        ('051', '051'), ('53', '53'), ('space', ' 51'), ('empty', '')):
        add('compat-' + name, 'warning', env={'BASH_COMPAT': value},
            warning=_warning('BASH_COMPAT', value, COMPAT) if value else '')
    for name, value in (('abc', 'abc'), ('closed', '99'), ('space', ' 99'), ('plus', '+99'), ('wrap', '4294967395'),
                        ('negative', '-1'), ('overflow', '99999999999999999999'), ('suffix', '2x'), ('empty', '')):
        add('xtracefd-' + name, 'warning', env={'BASH_XTRACEFD': value},
            warning=_warning('BASH_XTRACEFD', value, INVALID) if value else '')
    # 番号は比較器が読むだけで開いた番号(<FD>を実行の時に替える)。
    add('xtracefd-readonly', 'readonly', env={'BASH_XTRACEFD': '<FD>'},
        warning='bash: BASH_XTRACEFD: <FD>: cannot open as FILE\n')
    add('order', 'warning', env={'SHLVL': '999', 'BASH_XTRACEFD': 'abc', 'BASH_COMPAT': '99', 'LC_ALL': 'xx_XX.UTF-8'},
        warning='bash: warning: shell level (1000) too high, resetting to 1\n'
                + _warning('BASH_XTRACEFD', 'abc', INVALID) + _warning('BASH_COMPAT', '99', COMPAT)
                + 'bash: warning: setlocale: LC_ALL: cannot change locale (xx_XX.UTF-8)\n')
    values = {'SECONDS': '100', 'RANDOM': '5', 'SRANDOM': '6', 'BASHPID': '8', 'EPOCHSECONDS': '9',
              'EPOCHREALTIME': '10.5', 'BASH_COMMAND': 'cmd', 'BASH_SUBSHELL': '3', 'HISTCMD': '4', 'LINENO': '7'}
    add('dynamic-normal', 'dynamic', env=values)
    add('dynamic-profile', 'dynamic', env=values, profile=True)
    add('dynamic-odd', 'dynamic', env={'SECONDS': ' 7x', 'RANDOM': '', 'SRANDOM': 'x', 'BASHPID': '',
                                       'EPOCHSECONDS': '', 'EPOCHREALTIME': 'x', 'BASH_COMMAND': '',
                                       'BASH_SUBSHELL': '-1', 'HISTCMD': 'x', 'LINENO': ''})
    for setup in LINENO_SETUPS:
        add('lineno-' + setup, 'lineno', env={'LINENO': '7'}, setup=setup)
    return items


# LINENO の組み直しの印・行が通常の流れと変わる流れ(旧版の行)。基準の容器の旧版の値と全バイトで比べる。
LINENO_SETUPS = (
    'precheck',         # build_ere が無い: 最初の組み直しは 576
    'exclude-only',     # --exclude だけ: 519 の printf|grep
    'pre-untracked',    # 696 の read が印を付け、720 で組み直す
    'pager',            # 758 の $(sanitize …) で組み直し、784 からはその値
    'promisor',         # 767 の $(sanitize …) で組み直し、625 の git version もその値
    'info-attributes',  # 809 の read が印を付け、853 で組み直す
    'filter-unset',     # 932 の read が印を付け、950 で組み直す(改竄の疑いで 22)
    'sparse',           # 980・983・998・1249(子の一時の代入)・1250
    'accept',           # 1067 の export が印を付け、1068 で組み直す
    'subgit',           # 1551 の $(path_field …) で組み直し、1557 からはその値
    'gitignore',        # 1601 の $(path_field …) で組み直し、2 回目の 1593 はその値
)


def _lineno_args(setup, f, legacy):
    """LINENO_SETUPS の流れになるようにfixtureを整え、引数を返す。"""
    patch = ['--patch-out', str(f.patch)]
    if setup == 'precheck':
        return ['--cwd', str(f.repo), '--precheck']
    if setup == 'exclude-only':
        return ['--cwd', str(f.repo), '--base', f.base, '--out', str(f.out), '--exclude', r'(^|/)\.env($|/)', *patch]
    if setup == 'pre-untracked':
        listing = f.root / 'pre-untracked.z'
        listing.write_bytes(b'untracked.txt\0')
        digest = hashlib.sha256(listing.read_bytes()).hexdigest()
        return [*f.normal(), *patch, '--pre-untracked', str(listing), '--pre-untracked-sha256', digest]
    if setup == 'pager':
        f.git('config', 'core.pager', 'less')
    elif setup == 'promisor':
        f.git('config', 'remote.origin.promisor', 'true')
    elif setup == 'info-attributes':
        (f.repo / '.git/info').mkdir(exist_ok=True)
        (f.repo / '.git/info/attributes').write_bytes(b'*.txt text\n')
    elif setup == 'filter-unset':
        f.git('config', 'filter.unspecified.clean', 'cat')
    elif setup == 'sparse':
        review_cases.sparse(f, 3)
        (f.repo / 'seed.txt').write_bytes(b'environment probe change\n')
    elif setup == 'accept':
        f.git('config', 'filter.foo.clean', 'cat')
        first = run(['bash', str(legacy), '--cwd', str(f.repo), '--precheck'], cwd=f.root, env=f.env)
        digest = [line for line in first.stderr.decode().splitlines() if line.startswith('承認ダイジェスト: ')]
        if first.returncode != 22 or len(digest) != 1:
            raise AssertionError('承認ダイジェストを1回だけ得られません')
        return [*f.normal(), *patch, '--accept', digest[0].split(': ', 1)[1]]
    elif setup == 'subgit':
        (f.repo / 'module').mkdir()
        f.git('-C', 'module', 'init', '-q')
        f.git('-C', 'module', 'commit', '-q', '--allow-empty', '-m', 'module')
        oid = f.git('-C', 'module', 'rev-parse', 'HEAD').strip().decode()
        f.git('update-index', '--add', '--cacheinfo', f'160000,{oid},module')
        f.git('commit', '-q', '-m', 'gitlink')
        f.base = f.git('rev-parse', 'HEAD').strip().decode()
    elif setup == 'gitignore':
        (f.repo / '.gitignore').write_bytes(b'ignored.log\n')
        (f.repo / 'ignored.log').write_bytes(b'ignored\n')
    else:
        raise NotImplementedError(setup)
    return [*f.normal(), *patch]


def _names(case):
    return [name for name in case['env'] if name not in ('SHLVL', 'LC_ALL')]


def _check_warning(case, old, fd=None):
    """旧版の標準エラーの頭の、bashの行が固定の警告と同じ(値が警告だけの値である)ことを確かめる。"""
    head = b''.join(line for line in old.stderr.splitlines(keepends=True)[:4] if line.startswith(b'bash: '))
    if head != case['warning'].replace('<FD>', str(fd)).encode():
        raise AssertionError(f'旧版の起動時の警告が固定の文と違います: {head!r}')


def _timed(calls, began, ended):
    """時刻で変わる値: 表記の形と、実行の始めから終わりの間にあることを確かめ、印へ替える。"""
    result = []
    for tool, call_args, environment in calls:
        environment = dict(environment)
        for name, form in TIME_FORMS.items():
            if name not in environment:
                continue
            value = environment[name]
            if not form.fullmatch(value):
                raise AssertionError(f'{name.decode()} の表記の形が違います: {value!r}')
            moment = float(value)
            if not began - 1e-6 <= moment <= ended + 1e-6 and not int(began) <= moment <= ended + 1:
                raise AssertionError(f'{name.decode()} が実行の始めから終わりの間にありません: {value!r}')
            environment[name] = b'<TIME>'
        result.append((tool, call_args, environment))
    return result


def run_case(case, f, binary, legacy):
    variant = case['variant']
    with closed_env.case_names(_names(case)):
        if variant == 'warning':
            old, new = review_cases.env_probe(case, f, binary, legacy)
            _check_warning(case, old)
            return old, new
        if variant == 'dynamic':
            return review_cases.env_probe(case, f, binary, legacy, varying=_timed)
        if variant == 'lineno':
            return review_cases.env_probe(case, f, binary, legacy,
                                          setup=lambda fixture: _lineno_args(case['setup'], fixture, legacy))
        if variant == 'readonly':
            return _readonly(case, f, binary, legacy)
    raise NotImplementedError(variant)


def _readonly(case, f, binary, legacy):
    """読むだけで開いた番号を渡す。fdopenの警告だけで、効果は無い。"""
    (f.repo / 'seed.txt').write_bytes(b'startup env change\n')
    args = [*f.normal(), '--patch-out', str(f.patch)]
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        env = dict(f.env, BASH_XTRACEFD=str(descriptor))
        results = []
        for command in (['bash', str(legacy)], [str(binary)]):
            f.clean_outputs()
            before = f.input_state()
            results.append(run([*command, *args], cwd=f.root, env=env, report=f.out, patch=f.patch,
                               pass_fds=(descriptor,)))
            unchanged(before, f.input_state())
    finally:
        os.close(descriptor)
    old, new = results
    _check_warning(case, old, descriptor)
    same_result(old, new)
    return old, new
