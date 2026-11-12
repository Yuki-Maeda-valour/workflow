"""部分出力の17件(書込みが途中のバイト数までしか通らない)。

標準出力・標準エラーを通常ファイルへ向け、RLIMIT_FSIZEで書けるバイト数を制限し、SIGXFSZを無視する。
旧版とRustの書込みが実際に切れたかをstraceで観測し、書けるバイト数と書込みの長さから求めた予測と照らす。
固定名へ置き換える欄(旧版の所在地欄・bashの起動名)を含む標準エラーは、22:38の決定の3つの場合に分ける。
  1: どちらも切れない。旧版の結果の欄を固定名に置き換えたもの。
  2: 旧版だけが切れる。故障の無かったときの旧版の結果(終了値・標準出力・欄を置き換えた標準エラー)。
  3: 両方が切れる。故障の無かった旧版の標準エラーの欄を置き換え、同じバイト数で切ったもの。
     終了値と標準出力は、同じ故障の条件の旧版。
置き換えた書込みだけが切れる場合(旧版の呼出名が固定名より短い)は決定の外なので、失敗にする。
`--out`と`--patch-out`は、すべてのファイルへ同じ上限を当てる(最初に切れる書込みを観測する)。
"""
import dataclasses
import os
from pathlib import Path
import re
import shutil
import tempfile

from .compare import exact_ids, fixed, same_result, unchanged
from .diagnostics import bash_locations
from . import compare, write_cuts

FIXED_SCRIPT = b'diff-snapshot.sh'
FIXED_BASH = b'bash'
PRINT_ERROR = b'printf: write error: File too large'
WARNING = b'bash: warning: setlocale: LC_ALL: cannot change locale (xx_YY.UTF-8)\n'
TMPD_NAME = re.compile(rb'/tmp\.[A-Za-z0-9]{10}/')
PUB_NAME = re.compile(rb'/\.diff-snapshot\.[A-Za-z0-9]{6}/')


def cases():
    result = []
    for size in (1, 5, 300):
        result.append(dict(id=f'partial-print-stdout-{size}', mode='print', size=size, stdout=True, stderr=False, plan=1))
    for size, plan in ((5, 3), (40, 3), (100, 3), (300, 1)):
        result.append(dict(id=f'partial-print-both-{size}', mode='print', size=size, stdout=True, stderr=True, plan=plan))
    for size, plan in ((0, 3), (10, 3), (40, 3), (300, 3)):
        result.append(dict(id=f'partial-usage-stderr-{size}', mode='usage', size=size, stdout=False, stderr=True, plan=plan))
    result.append(dict(id='partial-value-stderr-60', mode='value', size=60, stdout=False, stderr=True, plan=3))
    # 診断の標準エラーの残り3件: 旧版を絶対パスのbashで起動し、起動時の警告の先頭欄(起動名)を固定名bashへ
    # 置き換える。200ではどちらも切れない(1)。Rustの1行(69バイト)がちょうど入る上限では旧版だけが切れる(2)。
    # 30では両方が切れる(3)。
    for size, plan in ((200, 1), (len(WARNING), 2), (30, 3)):
        result.append(dict(id=f'partial-warning-stderr-{size}', mode='warning', size=size, stdout=False, stderr=True,
                           plan=plan))
    result.append(dict(id='partial-out-file', mode='out', size=None, stdout=False, stderr=False, plan='intermediate'))
    result.append(dict(id='partial-patch-out-file', mode='patch', size=None, stdout=False, stderr=False, plan='same-point'))
    assert len(result) == 17 and len({case['id'] for case in result}) == 17
    return result


def _arguments(mode):
    # 30要素のEREは約450 byteで、どの上限でもstdoutが途中で切れる。
    globs = [value for i in range(30) for value in ('--exclude-glob', f'secret-{i:02d}.key')]
    return {'print': ['--print-exclude-ere', *globs], 'usage': ['--unknown-s3d'], 'value': ['--cwd'],
            'warning': ['--print-exclude-ere', '--exclude-glob', '.env']}[mode]


def _run(command, case, f, env, *, stdout_limited, stderr_limited, size):
    with tempfile.TemporaryDirectory(prefix='s3d-partial-', dir=f.root) as temporary:
        out, err = Path(temporary) / 'stdout', Path(temporary) / 'stderr'
        result, cuts = write_cuts.run(command, cwd=f.root, env=env, size=size,
                                      stdout_file=out if stdout_limited else None,
                                      stderr_file=err if stderr_limited else None)
        cut = {name: any(Path(c['path']) == path for c in cuts) for name, path in (('stdout', out), ('stderr', err))}
    return result, cut


def _mapping(case, legacy, bash):
    """旧版の結果の、固定名へ置き換える欄だけを置き換える関数。"""
    if case['mode'] == 'print':
        rows = [{'position': 0, 'line': 567, 'detail': PRINT_ERROR}]
        return lambda raw: bash_locations(raw, invocation=str(legacy), rows=rows) if raw.strip() else raw
    if case['mode'] == 'warning':
        prefix = os.fsencode(bash) + b': warning: '

        def startup(raw):
            lines = raw.splitlines(keepends=True)
            if [i for i, line in enumerate(lines) if line.startswith(prefix)] != [0]:
                raise AssertionError('bashの起動名の欄の位置または出現数が違います')
            lines[0] = FIXED_BASH + lines[0][len(os.fsencode(bash)):]
            return b''.join(lines)
        return startup
    return lambda raw: raw


def expected_for(kind, old, clean, mapped_clean, size, mapping):
    """22:38の3つの場合の期待(終了値・標準出力・標準エラー)。"""
    if kind == 1:
        return old.returncode, old.stdout, mapping(old.stderr) if old.stderr else old.stderr
    if kind == 2:
        return clean.returncode, clean.stdout, mapped_clean
    return old.returncode, old.stdout, mapped_clean[:size]


# 比較器の変異: 3つの場合の取り違えと、切る位置のずれ。正しい期待と違う形になるとき、Rustの結果で拒否されること。
MUTANTS = {'as-case-1': lambda kind: 1, 'as-case-2': lambda kind: 2, 'as-case-3': lambda kind: 3}


def compare_stream_case(case, f, binary, legacy, rejected):
    size = case['size']
    bash = shutil.which('bash', path=f.env['PATH'])
    env = dict(f.env)
    old_command = ['bash', str(legacy)]
    if case['mode'] == 'warning':
        env['LC_ALL'] = 'xx_YY.UTF-8'
        # bash自身の文(起動時の警告)は版に依る欄。
        compare.note_field(case['id'], 'bash-startup-warning')
        old_command = [bash, str(legacy)]
        if len(os.fsencode(bash)) < len(FIXED_BASH):
            raise AssertionError('旧版のbashの起動名が固定名より短い(決定の外)')
    elif case['mode'] == 'print' and len(os.fsencode(str(legacy))) < len(FIXED_SCRIPT):
        raise AssertionError('旧版の呼出名が固定名より短い(決定の外)')
    args = _arguments(case['mode'])
    old, old_cut = _run([*old_command, *args], case, f, env, stdout_limited=case['stdout'],
                        stderr_limited=case['stderr'], size=size)
    new, new_cut = _run([str(binary), *args], case, f, env, stdout_limited=case['stdout'],
                        stderr_limited=case['stderr'], size=size)
    # 故障の無かったときの旧版: 同じstdoutの故障だけを残し、標準エラーは全文。
    clean, _ = _run([*old_command, *args], case, f, env, stdout_limited=case['stdout'], stderr_limited=False,
                    size=size)
    full, _ = _run([*old_command, *args], case, f, env, stdout_limited=False, stderr_limited=False, size=1 << 30)
    mapping = _mapping(case, legacy, bash)
    mapped_clean = mapping(clean.stderr) if clean.stderr else clean.stderr
    # 予測(書けるバイト数と書込みの長さ)と観測の照合。
    predicted = {'old': {'stdout': case['stdout'] and len(full.stdout) > size,
                         'stderr': case['stderr'] and len(clean.stderr) > size},
                 'new': {'stdout': case['stdout'] and len(full.stdout) > size,
                         'stderr': case['stderr'] and len(mapped_clean) > size}}
    if {'old': old_cut, 'new': new_cut} != predicted:
        raise AssertionError(f'切れた書込みの観測が予測と違います: 観測 {old_cut} {new_cut} 予測 {predicted}')
    if new_cut['stderr'] and not old_cut['stderr']:
        raise AssertionError('置き換えた書込みだけが切れました(決定の外)')
    kind = (3 if new_cut['stderr'] else 2) if old_cut['stderr'] else 1
    if kind != case['plan']:
        raise AssertionError(f'22:38の場合が予定と違います: {kind} 予定 {case["plan"]}')
    if old_cut['stderr'] and old.stderr != clean.stderr[:size]:
        raise AssertionError('故障ありの旧版の出力が、故障なしの出力の同じバイト数の先頭と一致しません')
    expected = expected_for(kind, old, clean, mapped_clean, size, mapping)
    actual = (new.returncode, new.stdout, new.stderr)
    if actual != expected:
        raise AssertionError(f'部分出力の終了値・stdout・切った診断が期待と違います(場合 {kind})')
    for name, choose in MUTANTS.items():
        try:
            wrong = expected_for(choose(kind), old, clean, mapped_clean, size, mapping)
        except AssertionError:
            rejected.add(name)
            continue
        if wrong != expected and wrong != actual:
            rejected.add(name)
    if kind == 3:
        for shift, name in ((1, 'cut-later'), (-1, 'cut-earlier')):
            wrong = (old.returncode, old.stdout, mapped_clean[:max(size + shift, 0)])
            if wrong != expected and wrong != actual:
                rejected.add(name)
    return {'id': case['id'], 'case': kind, 'old_cut': old_cut, 'new_cut': new_cut, 'code': new.returncode,
            'stdout_bytes': len(new.stdout), 'stderr_bytes': len(new.stderr)}


def _limited_pair(f, binary, legacy, args, size):
    scratch = f.root / 'tmp-shared'
    scratch.mkdir(exist_ok=True)
    env = dict(f.env, TMPDIR=str(scratch))
    values = []
    for command in (['bash', str(legacy)], [str(binary)]):
        f.clean_outputs()
        before = f.input_state()
        listed = sorted(os.listdir(f.root))
        result, cuts = write_cuts.run([*command, *args], cwd=f.root, env=env, size=size)
        unchanged(before, f.input_state())
        if os.listdir(scratch) or sorted(os.listdir(f.root)) != listed:
            raise AssertionError('一時領域を残したか、ほかのファイルを作りました')
        values.append((result, cuts))
    return values


def compare_file_case(case, f, binary, legacy):
    (f.repo / 'seed.txt').write_bytes(b''.join(b'line %05d of a long tracked change\n' % i for i in range(400)))
    args = [*f.normal()]
    if case['mode'] == 'patch':
        # patch.rawが最初に切れるよう、patchの基点を大きな変更の前に置く(本文の差分は小さい)。
        older = f.base
        f.git('add', 'seed.txt')
        f.git('commit', '-q', '-m', 'large')
        f.base = f.git('rev-parse', 'HEAD').strip().decode()
        (f.repo / 'seed.txt').write_bytes((f.repo / 'seed.txt').read_bytes() + b'tail\n')
        args = [*f.normal(), '--patch-out', str(f.patch), '--patch-base', older]
    # 故障なしの旧版で、公開する本文の大きさを測る。
    f.clean_outputs()
    whole = write_cuts.run(['bash', str(legacy), *args], cwd=f.root, env=dict(f.env, TMPDIR=str(f.root)),
                           size=1 << 30)[0]
    fixed(whole, 0)
    size = len((f.patch if case['mode'] == 'patch' else f.out).read_bytes()) - 1
    f.clean_outputs()
    (old, old_cuts), (new, new_cuts) = _limited_pair(f, binary, legacy, args, size)
    first = [Path(c['path']).name for c in (old_cuts[:1] + new_cuts[:1])]
    if case['mode'] == 'patch':
        if first != ['patch.raw', 'patch.raw']:
            raise AssertionError(f'最初に切れた書込みがpatch.rawではありません: {first}')
        same_result(*(dataclasses.replace(r, stderr=TMPD_NAME.sub(b'/tmp.X/', r.stderr)) for r in (old, new)),
                    generated_report=False)
        fixed(old, 20, stdout=b'')
        return {'id': case['id'], 'case': 'same-point', 'first': first, 'code': new.returncode}
    # 旧版は認められた中間の書込み(out.raw)で先に止まる。Rustは自分の書くout.tmpで失敗し、表の文(1187行のcp)で止まる。
    if first != ['out.raw', 'out.tmp']:
        raise AssertionError(f'最初に切れた書込みが予定と違います: {first}')
    fixed(old, 20, stdout=b'')
    fixed(new, 20, stdout=b'')
    from .launch_sites import load
    template = next(d['template'] for s in load()['sites'] if s['id'] == 'site-50-cp' for d in s['diagnostics']
                    if d['name'] == 'write')
    shown = PUB_NAME.sub(b'/.diff-snapshot.X/', TMPD_NAME.sub(b'/tmp.X/', new.stderr))
    source = "'" + str(f.root / 'tmp-shared') + "/tmp.X/out.raw'"
    dest = "'" + str(f.root) + "/.diff-snapshot.X/out.tmp'"
    expected = (template.replace('{source}', source).replace('{dest}', dest).encode()
                + 'ERROR [internal] 予期しない失敗(終了コード 1・行 1187)\n'.encode())
    if shown != expected:
        raise AssertionError('Rustの診断が起動場所の表の形ではありません')
    return {'id': case['id'], 'case': 'intermediate', 'first': first, 'code': new.returncode}


def run_partial(binary, *, fixture_type, legacy, selected=None):
    """selectedが全17件のときだけ、3つの場合の網羅と比較器の変異の拒否を求める。"""
    selected = cases() if selected is None else selected
    if not write_cuts.available():
        import sys
        if sys.platform == 'darwin':
            raise compare.NotRun([case['id'] for case in selected], 'strace')
        raise AssertionError('部分出力の観測にstraceが要ります(Linux)')
    done, rejected = [], set()
    with tempfile.TemporaryDirectory(prefix='s3d-partial-cases-') as temporary:
        for case in selected:
            root = Path(temporary) / case['id']
            root.mkdir()
            try:
                if case['mode'] in ('out', 'patch'):
                    done.append(compare_file_case(case, fixture_type(root), binary, legacy))
                else:
                    done.append(compare_stream_case(case, fixture_type(root), binary, legacy, rejected))
            except Exception as error:
                raise AssertionError(f'{case["id"]}: {error}') from error
    exact_ids([case['id'] for case in selected], [value['id'] for value in done])
    if selected != cases():
        return done
    if {case['case'] for case in done if isinstance(case['case'], int)} != {1, 2, 3}:
        raise AssertionError('22:38の3つの場合をすべて通っていません')
    missing = set(MUTANTS) | {'cut-later', 'cut-earlier'}
    if missing - rejected:
        raise AssertionError(f'比較器の変異を拒否できません: {sorted(missing - rejected)}')
    return done
