"""先行点検1の12件と部分出力を、旧版と並べて動かして比べる。"""
import dataclasses
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import closed_env
from .compare import Result, exact_ids, fixed, report_time, run, same_result, unchanged
from .diagnostics import bash_locations
from . import compare, launch_sites

STUB = r'''import json,os,sys,pathlib
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
for rule in json.loads(os.environ['S3D_STUB_RULES']):
    if rule['tool']!=name:continue
    seq=rule.get('args',[])
    same=lambda a,b:a==b or a.endswith('/'+b)
    if not any(all(same(a,b) for a,b in zip(args[i:i+len(seq)],seq)) for i in range(len(args)-len(seq)+1)):continue
    if rule.get('stderr'):os.write(2,rule['stderr'].encode())
    if rule.get('sleep'):
        import time;time.sleep(rule['sleep'])
    if rule.get('stdout')=='large':
        # 64 MiB を書く。読み手が先に閉じたかを記録する(head -c 8000 と同じなら途中で切れる)。
        state='complete'
        try:
            for _ in range(1024):os.write(1,b'x'*65536)
        except OSError:state='cut'
        fd=os.open(os.environ['S3D_STUB_RECORD'],os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600);os.write(fd,(state+'\n').encode());os.close(fd)
        sys.exit(0 if state=='complete' else 1)
    if rule.get('stdout_text'):os.write(1,rule['stdout_text'].encode())
    sys.exit(rule['code'])
os.execv(os.environ['S3D_STUB_REAL_'+name.upper()],[name,*args])
'''
INTERNAL = 'ERROR [internal] 予期しない失敗(終了コード {}・行 {})\n'
LONG = 'directory-name-that-keeps-each-record-long/'


def internal(code, line):
    return INTERNAL.format(code, line).encode()


def stubs(fixture, label, rules):
    """旧版とRustの両方へ同じ規則のスタブをPATHの先頭へ置く。"""
    if not rules:
        return dict(fixture.env)
    folder = fixture.root / ('stub-' + label)
    folder.mkdir()
    env = dict(fixture.env, S3D_STUB_RULES=json.dumps(rules))
    for tool in sorted({rule['tool'] for rule in rules}):
        real = shutil.which(tool, path=fixture.env['PATH'])
        if real is None:
            raise AssertionError(f'スタブの実体がありません: {tool}')
        env['S3D_STUB_REAL_' + tool.upper()] = real
        path = folder / tool
        path.write_text('#!' + sys.executable + '\n' + STUB)
        path.chmod(0o700)
    env['PATH'] = str(folder) + os.pathsep + fixture.env['PATH']
    return env


def outputs(root, ignored):
    """出力の親に増えた通常ファイルと中身。生成日時は区間検査の後で置換する。"""
    found = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if relative.parts[0] in ignored:
            continue
        if path.is_file() and not path.is_symlink():
            found[str(relative)] = path.read_bytes()
        elif path.is_dir():
            found[str(relative) + '/'] = None
    return found


def execute(fixture, command, label, args, *, env_update=None, rules=(), env_remove=(), timeout=120,
            collect=False):
    env = stubs(fixture, label, list(rules))
    for key in env_remove:
        env.pop(key, None)
    env.update(env_update or {})
    scratch = fixture.root / ('tmp-' + label)
    scratch.mkdir()
    env['TMPDIR'] = str(scratch)
    fixture.clean_outputs()
    ignored = {'repo', 'home', 'xdg', 'tmp-legacy', 'tmp-rust', 'stub-legacy', 'stub-rust'}
    before = fixture.input_state()
    listed = outputs(fixture.root, ignored)
    result = run([*command, *args], cwd=fixture.root, env=env, report=fixture.out, patch=fixture.patch,
                 timeout=timeout)
    unchanged(before, fixture.input_state())
    if os.listdir(scratch):
        raise AssertionError(f'{label}: 一時領域が残りました')
    shutil.rmtree(scratch)
    produced = None
    if collect:
        after = outputs(fixture.root, ignored)
        produced = {k: v for k, v in after.items() if k not in listed}
        for name in sorted(produced, key=len, reverse=True):
            path = fixture.root / name.rstrip('/')
            if name.endswith('/'):
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        produced = {k: report_time(v, result.started, result.ended) if v and v.startswith('# diff スナップショット'.encode()) else v
                    for k, v in produced.items()}
    return result, produced


def pair(fixture, binary, legacy, args, *, code, rows=None, collect=False, **kwargs):
    values = []
    for label, command in (('legacy', ['bash', str(legacy)]), ('rust', [str(binary)])):
        values.append(execute(fixture, command, label, args, collect=collect, **kwargs))
    (old, old_files), (new, new_files) = values
    fixed(old, code)
    if rows is not None:
        old = dataclasses.replace(old, stderr=bash_locations(old.stderr, invocation=str(legacy), rows=rows))
    same_result(old, new)
    if old_files != new_files:
        raise AssertionError('公開したファイルの位置または中身が旧版と違います')
    return old, new


def many_tracked(fixture, count=1000, prefix=''):
    folder = fixture.repo / (prefix + LONG.rstrip('/'))
    folder.mkdir(parents=True)
    for i in range(count):
        (folder / f'record-{i:05d}-with-a-long-name.txt').write_bytes(b'x\n')
    fixture.git('add', '-A')


def sparse(fixture, count=1000):
    many_tracked(fixture, count, prefix='drop-')
    fixture.git('commit', '-q', '-m', 'sparse')
    fixture.base = fixture.git('rev-parse', 'HEAD').strip().decode()
    names = fixture.git('ls-files', '-z', '--', 'drop-' + LONG)
    fixture.git('update-index', '--skip-worktree', '-z', '--stdin', data=names)
    shutil.rmtree(fixture.repo / ('drop-' + LONG.rstrip('/')))
    fixture.git('config', 'core.sparseCheckout', 'true')
    (fixture.repo / '.git/info').mkdir(exist_ok=True)
    (fixture.repo / '.git/info/sparse-checkout').write_bytes(b'/seed.txt\n')


def normal(fixture, *extra):
    return [*fixture.normal(), *extra]


def run_case(case, fixture, binary, legacy):
    kind, f = case['kind'], fixture
    patch = ['--patch-out', str(f.patch)]
    ja = {'LANG': 'ja_JP.UTF-8'} if case.get('locale') == 'ja' else None
    remove = ('LC_ALL',) if case.get('locale') == 'ja' else ()
    if kind == 'pipeline-consumer-exits':
        many_tracked(f)
        return pair(f, binary, legacy, normal(f), code=20, timeout=60,
                    rules=[dict(tool='git', args=['--index-info'], code=1)])
    if kind == 'check-attr-exits':
        many_tracked(f)
        return pair(f, binary, legacy, normal(f), code=20, timeout=60,
                    rules=[dict(tool='git', args=['check-attr', '--stdin'], code=1)])
    if kind == 'check-rules-exits':
        sparse(f)
        return pair(f, binary, legacy, normal(f), code=22, timeout=60,
                    rules=[dict(tool='git', args=['check-rules'], code=129)])
    if kind == 'sparse-update-exits':
        sparse(f)
        return pair(f, binary, legacy, normal(f), code=20, timeout=60,
                    rules=[dict(tool='git', args=['--skip-worktree', '-z', '--stdin'], code=1, stderr='stub: update-index failed\n')])
    if kind == 'sparse-control':
        sparse(f, 40)
        return pair(f, binary, legacy, normal(f, *patch), code=0, timeout=60)
    if kind == 'config-key':
        f.git('config', case['key'], case['value'])
        rules = [dict(tool='git', args=['version'], code=0, stdout_text='git version 2.44.0\n')] if case.get('old_git') else []
        return pair(f, binary, legacy, ['--cwd', str(f.repo), '--precheck'], code=case['code'], rules=rules)
    if kind == 'filter-approval':
        f.git('config', case['key'], 'cat')
        first = run(['bash', str(legacy), '--cwd', str(f.repo), '--precheck'], cwd=f.root, env=f.env)
        fixed(first, 22)
        digest = [line for line in first.stderr.decode().splitlines() if line.startswith('承認ダイジェスト: ')]
        if len(digest) != 1:
            raise AssertionError('承認ダイジェストを1回だけ得られません')
        accept = ['--accept', digest[0].split(': ', 1)[1]]
        pair(f, binary, legacy, ['--cwd', str(f.repo), '--precheck', *accept], code=0)
        (f.repo / 'seed.txt').write_bytes(b'approved filter change\n')
        return pair(f, binary, legacy, normal(f, *patch, *accept), code=0)
    if kind == 'output-path':
        (f.repo / 'seed.txt').write_bytes(b'output path change\n')
        args = ['--cwd', str(f.repo), '--base', f.base, '--exclude-glob', '.env', '--out', str(f.root) + case['out']]
        if case.get('patch'):
            args += ['--patch-out', str(f.root) + case['patch']]
        return pair(f, binary, legacy, args, code=case['code'], collect=True)
    if kind == 'tmpdir-missing':
        missing = str(f.root / 'missing-tmp')
        old, new = None, None
        values = []
        for label, command in (('legacy', ['bash', str(legacy)]), ('rust', [str(binary)])):
            env = dict(f.env, TMPDIR=missing, **(ja or {}))
            for key in remove:
                env.pop(key, None)
            f.clean_outputs()
            values.append(run([*command, *normal(f)], cwd=f.root, env=env, report=f.out, patch=f.patch))
        old, new = values
        fixed(old, 20, stdout=b'', stderr=("mktemp: failed to create directory via template '" + missing
                                           + "/tmp.XXXXXXXXXX': No such file or directory\n").encode()
              + internal(1, 613) + internal(20, 613))
        same_result(old, new)
        return old, new
    if kind == 'unwritable':
        target = f.root / 'locked'
        target.mkdir()
        try:
            target.chmod(0o555)
            args = ['--cwd', str(f.repo), '--base', f.base, '--exclude-glob', '.env']
            if case['which'] == 'out':
                args += ['--out', str(target / 'out.md')]
            elif case['which'] == 'patch':
                args += ['--out', str(f.out), '--patch-out', str(target / 'out.patch')]
            else:
                args += ['--out', str(target / 'sub/out.md')]
            return pair(f, binary, legacy, args, code=20, env_update=ja, env_remove=remove)
        finally:
            target.chmod(0o755)
    if kind == 'info-attributes-unreadable':
        path = f.repo / '.git/info/attributes'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'* text\n')
        path.chmod(0)
        try:
            # 旧 801 行は git の実体のパスを使うので、TMPDIR がシンボリックリンクを通っても合うよう実体で書く。
            detail = str(path.resolve()).encode() + b': Permission denied'
            return pair(f, binary, legacy, normal(f), code=20,
                        rows=[{'position': 0, 'line': 279, 'detail': detail}])
        finally:
            path.chmod(0o644)
    if kind == 'stub-failure':
        (f.repo / 'seed.txt').write_bytes(b'stub failure change\n')
        return pair(f, binary, legacy, normal(f, *patch), code=20, rules=[case['rule']])
    if kind == 'profile-ref':
        env = {'COLUMNS': case['columns']} if case.get('columns') is not None else None
        refs = []
        for value in case['refs']:
            refs += ['--secret-profile-ref', f.base if value == 'BASE' else value]
        return pair(f, binary, legacy, normal(f, *refs), code=20, env_update=env)
    if kind == 'max-bytes':
        for i in range(3):
            (f.repo / f'untracked-{i}.txt').write_bytes(b'u\n')
        rows = None
        if case['diagnostic']:
            detail = b'[: ' + case['value'].encode() + b': integer expression expected'
            rows = [{'position': i, 'line': 1477, 'detail': detail} for i in range(3)]
        return pair(f, binary, legacy, normal(f, '--max-untracked-bytes', case['value']), code=0, rows=rows)
    if kind == 'info-file':
        path = f.repo / '.git/info' / case['file']
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(bytes.fromhex(case['data_hex']))
        (f.repo / 'seed.txt').write_bytes(b'info file change\n')
        return pair(f, binary, legacy, normal(f), code=case['code'])
    if kind == 'env-probe':
        return env_probe(case, f, binary, legacy)
    if kind == 'base-blob-head':
        (f.repo / 'seed.txt').write_bytes(b'blob head change\n')
        records = {label: f.root / ('blob-' + label) for label in ('legacy', 'rust')}
        values = []
        for label, command in (('legacy', ['bash', str(legacy)]), ('rust', [str(binary)])):
            values.append(execute(f, command, label, normal(f), timeout=60, env_update={'S3D_STUB_RECORD': str(records[label])},
                                  rules=[dict(tool='git', args=['cat-file', 'blob'], code=0, stdout='large')])[0])
        fixed(values[0], 0)
        same_result(*values)
        seen = [records[label].read_text() for label in ('legacy', 'rust')]
        if seen[0] != 'cut\n' or seen[0] != seen[1]:
            raise AssertionError('基準側blobの読取りを8000 byteで打ち切っていません')
        return values
    raise AssertionError('未知の点検ケースです')


def cases():
    result = []

    def add(case_id, kind, **values):
        result.append(dict(id=case_id, kind=kind, **values))
    add('review-pipeline-consumer-exits', 'pipeline-consumer-exits', item=1)
    add('review-check-attr-exits', 'check-attr-exits', item=2)
    add('review-check-rules-exits', 'check-rules-exits', item=2)
    add('review-sparse-update-exits', 'sparse-update-exits', item=2)
    add('review-sparse-control', 'sparse-control', item=2)
    for name, key, value, code, old in (
            ('diff-textconv', 'diff.textconv', 'x', 0, False),
            ('diff-command', 'diff.command', 'x', 0, False),
            ('remote-promisor', 'remote.promisor', 'true', 0, False),
            ('remote-promisor-old-git', 'remote.promisor', 'true', 0, True),
            ('diff-driver-textconv', 'diff.x.textconv', 'x', 22, False),
            ('diff-driver-command', 'diff.x.command', 'x', 22, False),
            ('remote-origin-promisor-old-git', 'remote.origin.promisor', 'true', 22, True)):
        add('review-config-' + name, 'config-key', item=4, key=key, value=value, code=code, old_git=old)
    add('review-filter-approval-no-dot', 'filter-approval', item=5, key='filter.foo')
    add('review-filter-approval-dot', 'filter-approval', item=5, key='filter.foo.clean')
    for name, out, patch, code in (('dot', '/x/.', None, 0), ('dotdot', '/n1/..', '/n2/..', 0),
                                   ('trailing-slash', '/out.md/', None, 0), ('double-slash', '//out.md', None, 0)):
        add('review-output-path-' + name, 'output-path', item=6, out=out, patch=patch, code=code)
    for locale in ('c', 'ja'):
        add('review-fail-tmpdir-missing-' + locale, 'tmpdir-missing', item=7, locale=locale)
        add('review-fail-mkdir-' + locale, 'unwritable', item=7, which='mkdir', locale=locale)
        add('review-fail-out-mktemp-' + locale, 'unwritable', item=7, which='out', locale=locale)
    add('review-fail-patch-mktemp', 'unwritable', item=7, which='patch', locale='c')
    add('review-fail-info-attributes-unreadable', 'info-attributes-unreadable', item=7)
    # cp・mvはRustが起動しない(23:19)。その失敗は型の差し替えの検査(N)で確かめる。
    for name, rule in (
            ('ls-files-flags', dict(tool='git', args=['ls-files', '-z', '-v'], code=1, stderr='stub: ls-files failed\n')),):
        add('review-fail-' + name, 'stub-failure', item=7, rule=rule)
    for name, refs, columns in (
            ('dash-x', ['-x'], None), ('dash-h', ['-h'], None), ('option-cwd', ['--cwd'], None),
            ('abbreviation', ['--re'], None), ('ambiguous', ['--=x'], None), ('separator', ['--'], None),
            ('separator-more', ['--', 'BASE', 'extra'], None), ('negative', ['-1'], None),
            ('space', ['- x'], None), ('after-valid', ['BASE', '-x'], None),
            ('columns-30', ['-x'], '30'), ('columns-60', ['-x'], '60'), ('columns-10', ['-x'], '10'),
            ('columns-underscore', ['-x'], '3_0'), ('columns-invalid', ['-x'], 'x')):
        add('review-profile-ref-' + name, 'profile-ref', item=11, refs=refs, columns=columns)
    for name, value, diagnostic in (('overflow', '99999999999999999999', True),
                                    ('intmax-plus-one', '9223372036854775808', True),
                                    ('intmax', '9223372036854775807', False),
                                    ('leading-zeroes', '00000000000000000000000000001', False)):
        add('review-max-bytes-' + name, 'max-bytes', item=12, value=value, diagnostic=diagnostic)
    for name, file, data, code in (
            ('exclude-vertical-tab', 'exclude', b'\x0b\n', 0), ('exclude-nul', 'exclude', b'#x\x00y\n', 0),
            ('exclude-form-feed-text', 'exclude', b'\x0ca\n', 0),
            ('attributes-vertical-tab', 'attributes', b'pat\x0bfilter=x\n', 22),
            ('attributes-nul', 'attributes', b'pat fil\x00ter=x\n', 22)):
        add('review-info-' + name, 'info-file', item=12, file=file, data_hex=data.hex(), code=code)
    add('review-base-blob-head', 'base-blob-head', item=12)
    add('review-env-probe-normal', 'env-probe', item=9)
    add('review-env-probe-profile', 'env-probe', item=9, profile=True)
    add('review-env-probe-no-pwd-shlvl', 'env-probe', item=9, env={'SHLVL': 'x', 'PWD': '/nonexistent'})
    return result


ENV_STUB = r'''import json,os,sys,pathlib
name=pathlib.Path(sys.argv[0]).name
fd=os.open(os.environ['S3D_ENV_LOG'],os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
os.write(fd,(json.dumps({'tool':name,'args':[os.fsencode(a).hex() for a in sys.argv[1:]],
  'env':{os.fsencode(k).hex():os.fsencode(v).hex() for k,v in os.environ.items()}})+'\n').encode());os.close(fd)
real=os.environ['S3D_ENV_REAL_'+name.upper()]
os.execv(real,[real,*sys.argv[1:]])
'''
ENV_TOOLS = ('git', 'grep', 'mktemp', 'realpath', 'readlink', 'find', 'date', 'mkdir', 'cp', 'mv', 'rm', 'python3')


def env_calls(fixture, command, label, args, extra_env):
    folder = fixture.root / ('env-' + label)
    folder.mkdir()
    log = folder / 'log'
    env = dict(fixture.env, S3D_ENV_LOG=str(log), SHLVL='3', PWD=str(fixture.root), OLDPWD=str(fixture.repo))
    env.update(extra_env)
    for tool in ENV_TOOLS:
        real = shutil.which(tool, path=fixture.env['PATH'])
        if real is None:
            raise AssertionError(f'環境観測の実体がありません: {tool}')
        env['S3D_ENV_REAL_' + tool.upper()] = real
        (folder / tool).write_text('#!' + sys.executable + '\n' + ENV_STUB)
        (folder / tool).chmod(0o700)
    env['PATH'] = str(folder) + os.pathsep + fixture.env['PATH']
    scratch = fixture.root / ('envtmp-' + label)
    scratch.mkdir()
    env['TMPDIR'] = str(scratch)
    fixture.clean_outputs()
    result = run([*command, *args], cwd=fixture.root, env=env, report=fixture.out, patch=fixture.patch, timeout=120)
    replacements = [(str(folder).encode(), b'<STUB>'), (str(scratch).encode(), b'<TMPDIR>')]

    def normalize(raw):
        import re
        for old, new in replacements:
            raw = raw.replace(old, new)
        raw = re.sub(rb'<TMPDIR>/tmp\.[A-Za-z0-9]{10}', b'<TMPD>', raw)
        return re.sub(rb'\.diff-snapshot\.[A-Za-z0-9]{6}', b'.diff-snapshot.<X>', raw)
    calls = []
    for line in log.read_bytes().splitlines():
        value = json.loads(line)
        environment = {bytes.fromhex(k): normalize(bytes.fromhex(v)) for k, v in value['env'].items()
                       if not bytes.fromhex(k).startswith(b'S3D_')}
        calls.append((value['tool'], tuple(normalize(bytes.fromhex(a)) for a in value['args']), environment))
    return result, calls


# 2026-11-12 02:41の決定: 旧版の処理系やOSの部品が子の環境へ自分で足す値は再現しない。Macの旧Pythonが
# profile用のgitの子の環境へ足す`__CF_USER_TEXT_ENCODING`だけを比較から外し、値と有無を記録する。
CF_NAME = b'__CF_USER_TEXT_ENCODING'
CF_RECORDS = []


def cf_text_encoding(case_id, legacy_calls, *, system=None):
    import platform
    if (system or platform.system()) != 'Darwin':
        return legacy_calls
    sites = launch_sites.load()
    result = []
    for tool, call_args, environment in legacy_calls:
        if tool == 'git' and 'site-30-git' in launch_sites.classify(sites, tool, call_args):
            CF_RECORDS.append({'case': case_id, 'args': [a.decode('utf-8', 'replace') for a in call_args],
                               'present': CF_NAME in environment,
                               'value': environment.get(CF_NAME, b'').decode('utf-8', 'replace')})
            environment = {k: v for k, v in environment.items() if k != CF_NAME}
        result.append((tool, call_args, environment))
    return result


def env_probe(case, fixture, binary, legacy, *, varying=None, setup=None):
    """旧版とRustの子(gitとgrep)の環境を比べる。setupはfixtureを整えて引数を返す(既定は通常の実行)。"""
    (fixture.repo / 'seed.txt').write_bytes(b'environment probe change\n')
    (fixture.repo / 'untracked.txt').write_bytes(b'untracked\n')
    args = [*fixture.normal(), '--patch-out', str(fixture.patch)]
    if setup is not None:
        args = setup(fixture)
    if case.get('profile'):
        profile = fixture.repo / '.claude/project-profile.yml'
        profile.parent.mkdir()
        profile.write_bytes(b'secret_paths: [secret.key]\n')
        args += ['--secret-profile-ref', fixture.base]
    began = time.time()
    old, legacy_calls = env_calls(fixture, ['bash', str(legacy)], 'legacy', args, case.get('env', {}))
    middle = time.time()
    new, rust_calls = env_calls(fixture, [str(binary)], 'rust', args, case.get('env', {}))
    ended = time.time()
    same_result(old, new)
    if varying is not None:
        # 時刻で変わる値(06:58の決定): 表記の形と、それぞれの実行の始めから終わりの間にあることを確かめて印へ替える。
        legacy_calls, rust_calls = varying(legacy_calls, began, middle), varying(rust_calls, middle, ended)
    launch_sites.check_launches(launch_sites.load(), rust_calls, legacy_calls)
    # SHLVLは基準のbash 5.2.21で観測した値に合わせる。版に依る欄として記録する。
    compare.note_field(case['id'] + ':SHLVL', 'SHLVL')
    varying_names = {b'SHLVL'}
    if 'LINENO' in case.get('env', {}):
        # LINENOも基準のbash 5.2.21が子の環境を組み直す行に合わせる(bash 5.1は組み直す時が違う)。版に依る欄。
        compare.note_field(case['id'] + ':LINENO', 'LINENO')
        varying_names.add(b'LINENO')
    base, _ = launch_sites.is_base(launch_sites.load())
    if not base:
        # 基準でない機械: SHLVL(とLINENO)の欄は失敗として記録済み。ほかの変数は比べる。
        legacy_calls = [(t, a, {k: v for k, v in e.items() if k not in varying_names}) for t, a, e in legacy_calls]
        rust_calls = [(t, a, {k: v for k, v in e.items() if k not in varying_names}) for t, a, e in rust_calls]
    legacy_calls = cf_text_encoding(case['id'], legacy_calls)
    pending = {}
    for tool, call_args, environment in legacy_calls:
        pending.setdefault((tool, call_args), []).append(environment)
    if not rust_calls:
        raise AssertionError('Rustの子の起動を観測できません')
    for tool, call_args, environment in rust_calls:
        candidates = pending.get((tool, call_args))
        if not candidates:
            raise AssertionError(f'旧版に無い子の起動です: {tool}')
        expected = candidates.pop(0)
        if expected != environment:
            names = sorted(set(expected) ^ set(environment)) + sorted(k for k in expected if k in environment and expected[k] != environment[k])
            raise AssertionError(f'子の環境が旧版と違います: {tool} {[n.decode("utf-8", "replace") for n in names]}')
    return old, new


def run_cases(binary, *, fixture_type, legacy):
    done = []
    with tempfile.TemporaryDirectory(prefix='s3d-review-') as temporary:
        for case in cases():
            root = Path(temporary) / case['id']
            root.mkdir()
            began = time.monotonic()
            try:
                old, _ = run_case(case, fixture_type(root), binary, legacy)
            except Exception as error:
                raise AssertionError(f'{case["id"]}: {error}') from error
            done.append(dict(id=case['id'], item=case['item'], code=old.returncode,
                             seconds=round(time.monotonic() - began, 3)))
    exact_ids([case['id'] for case in cases()], [value['id'] for value in done])
    return done


# ── 部分出力(書込みが途中のバイト数までしか通らない)──
def limited(size):
    def prepare():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    return prepare


def partial_run(command, args, fixture, size, *, stdout_file, stderr_file):
    with tempfile.TemporaryDirectory(prefix='s3d-partial-', dir=fixture.root) as temporary:
        out_path, err_path = Path(temporary) / 'stdout', Path(temporary) / 'stderr'
        with open(out_path, 'wb') as out, open(err_path, 'wb') as err:
            started = time.time()
            closed_env.guard(fixture.env, fixture.root)
            done = subprocess.run([*command, *args], cwd=fixture.root, env=fixture.env, stdin=subprocess.DEVNULL,
                                  stdout=out if stdout_file else subprocess.PIPE,
                                  stderr=err if stderr_file else subprocess.PIPE,
                                  preexec_fn=limited(size) if stdout_file or stderr_file else None,
                                  start_new_session=True, timeout=60, check=False)
        stdout = out_path.read_bytes() if stdout_file else done.stdout
        stderr = err_path.read_bytes() if stderr_file else done.stderr
    compare.note_versions([*command, *args], fixture.root, stderr)
    return Result(done.returncode, stdout, stderr, None, None, started, time.time())


def cut_location(clean, *, invocation, rows, size):
    """故障なしの旧出力の所在地欄を固定名へ置き換えてから、同じbyte数で切る。"""
    mapped = bash_locations(clean, invocation=invocation, rows=rows) if rows else clean
    return mapped[:size]


def partial_cases():
    from . import partial_outputs
    return partial_outputs.cases()


def run_partial(binary, *, fixture_type, legacy):
    from . import partial_outputs
    # PATHを縮めた実行などが partial_cases を絞るので、ここで一覧を取って渡す。
    return partial_outputs.run_partial(binary, fixture_type=fixture_type, legacy=legacy, selected=partial_cases())
