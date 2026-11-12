#!/usr/bin/env python3
"""S3Dの旧新版を比較する。未指定の実行物や未到達ケースを成功に数えない。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

from diff_snapshot_fixtures import closed_env
from diff_snapshot_fixtures import compare as compare_module
from diff_snapshot_fixtures import legacy_adapter, profile_cases, parser_cases, platform_cases, publication, profile_races, output_faults, fd_cases, raw_env, typed_audit, coverage, entry_signals, review_cases, launch_sites, path_reduction, fixed_extra
from diff_snapshot_fixtures.diagnostics import APPROVED_DIFFERENCES, typed_report
from diff_snapshot_fixtures.compare import binary_path, exact_ids, file_state, fixed, run, same_result, unchanged
from diff_snapshot_fixtures.vectors import PATHS, vectors
from diff_snapshot_fixtures.read_audit import traced_pair

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name('diff_snapshot_fixtures')
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
LEGACY = SCRIPTS / 'diff-snapshot.sh'
SELFTEST = SCRIPTS / 'diff-snapshot-selftest.sh'
SECRET = b'S3D_SYNTHETIC_SECRET_5d77e8fa'
DEFAULTS = b'.env\0.env.*\0.dev.vars\0'


def manifest():
    value = json.loads((FIXTURES / 'cases.json').read_text())
    if value.get('schema') != 1 or value.get('approved_differences') != APPROVED_DIFFERENCES:
        raise AssertionError('未承認の比較例外または未知の台帳形式です')
    if value['source_sha256'] != legacy_adapter.SOURCE_SHA256:
        raise AssertionError('台帳の旧自己テストSHAが違います')
    exact_ids([f'group-{i:02d}' for i in range(1, 72)], [item['id'] for item in value['groups']])
    if value['vectors'] != vectors():
        raise AssertionError('固定342入力が正式な入力集合と違います')
    source = legacy_adapter.verified_source(SELFTEST)
    if value['typed_assertions'] != legacy_adapter.moved_assertions(source):
        raise AssertionError('型テストへ移す旧assertionの台帳が違います')
    expected_extra = profile_cases.cases() + [dict(case, kind='parser') for case in parser_cases.cases()] + [dict(case,kind='raw-env') for case in raw_env.cases()]
    if value.get('additional_external') != expected_extra or value.get('typed_extra_required') != profile_races.ids()+typed_audit.ids()+list(launch_sites.TYPED_IDS)+list(launch_sites.READ_TYPED_IDS)+list(fixed_extra.TYPED_IDS):
        raise AssertionError('追加入力の台帳が正式な集合と違います')
    if value.get('output_faults')!=output_faults.cases() or value.get('initial_fds')!=fd_cases.cases():
        raise AssertionError('出力故障と起動時fdの入力集合が違います')
    if value.get('entry_signals')!=entry_signals.cases():
        raise AssertionError('入口の信号待機の入力集合が違います')
    fields = value.get('version_fields')
    if not isinstance(fields, dict) or set(fields) - set(SCOPES + ['full']) or any(
            not isinstance(kinds, list) or not kinds or set(kinds) - set(compare_module.VERSION_KINDS)
            for scope in fields.values() for kinds in scope.values()):
        raise AssertionError('版に依る欄の一覧(version_fields)の形が違います')
    if value.get('fixed_extra')!=fixed_extra.cases():
        raise AssertionError('固定入力の追加の群が設計の表と違います')
    fixed_extra.check_counts()
    if value.get('review_cases')!=review_cases.cases() or value.get('partial_outputs')!=review_cases.partial_cases():
        raise AssertionError('先行点検と部分出力の入力集合が違います')
    inventory=json.loads((FIXTURES/'legacy_ids.json').read_text())
    if inventory['source_sha256']!=legacy_adapter.SOURCE_SHA256 or len(inventory['ids'])!=926:
        raise AssertionError('旧assertionの厳密ID集合が違います')
    mapped=coverage.groups(inventory['ids'],[case['id'] for case in value['typed_assertions']])
    if inventory['groups']!=mapped or value.get('legacy_assertion_ids')!=inventory['ids']:
        raise AssertionError('旧71観点の到達記録と厳密ID集合が違います')
    if any(group.get('case_ids')!=mapped[group['id']] for group in value['groups']):
        raise AssertionError('旧71観点と各実行IDの対応が違います')
    return value


def environment(base):
    """閉じた許可一覧から組み直した環境(PATH・専用のHOMEとTMPDIR・fixtureが固定した変数)。"""
    return closed_env.base(base)


def inherited_limits():
    """2026-11-12 00:42の決定: 旧版の処理系の資源の上限で決まる失敗は比較の前提の外。比較器はこれらを
    設定せず、受け継いだ値を記録する(RLIMIT_FSIZEの書込みの故障はケースが自分で設定して比べる)。"""
    import resource
    names = ('RLIMIT_AS', 'RLIMIT_NOFILE', 'RLIMIT_CPU', 'RLIMIT_STACK', 'RLIMIT_DATA', 'RLIMIT_NPROC', 'RLIMIT_FSIZE')
    def show(value):
        return 'unlimited' if value == resource.RLIM_INFINITY else value
    return {name: [show(v) for v in resource.getrlimit(getattr(resource, name))]
            for name in names if hasattr(resource, name)}


def requirements():
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise RuntimeError('Python3.10系とroot以外の利用者が必要です')
    import yaml
    if yaml.__version__ != '6.0.3':
        raise RuntimeError('比較用PyYAMLは6.0.3が必要です')
    info = {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version(),
            'yaml': yaml.__version__, 'tools': {}, 'rlimits': inherited_limits()}
    for name, expected in (('bash', b'GNU bash'), ('grep', b'GNU grep'), ('sort', b'GNU coreutils'),
                           ('sha256sum', b'GNU coreutils'), ('find', b'GNU findutils'),
                           ('touch', b'GNU coreutils'), ('head', b'GNU coreutils')):
        path = shutil.which(name)
        if not path:
            raise RuntimeError(f'必要な道具がありません: {name}')
        result = subprocess.run([path, '--version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode or expected not in result.stdout:
            raise RuntimeError(f'GNU版を確認できません: {name}')
        info['tools'][name] = {'path': os.path.realpath(path), 'version': result.stdout.splitlines()[0].decode()}
    for argv, payload, codes, output in ((['grep', '-Eiqz', '--', '^a$'], b'A\0', (0,), b''),
                                          (['sort', '-z'], b'b\0a\0', (0,), b'a\0b\0')):
        done = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, LC_ALL='C'), check=False)
        if done.returncode not in codes or done.stdout != output:
            raise RuntimeError('GNU道具の必要なNUL機能を使えません')
    lfs = shutil.which('git-lfs')
    if lfs is None:
        raise RuntimeError('実Git LFSがありません。必須ケースを省略しません')
    lfs_version = subprocess.check_output([lfs, 'version']).strip()
    if not lfs_version.startswith(b'git-lfs/') or b'stub' in lfs_version:
        raise RuntimeError('実Git LFSの版を確認できません')
    info['tools']['git-lfs'] = {'path': os.path.realpath(lfs), 'version': lfs_version.decode()}
    git = shutil.which('git')
    if git is None:
        raise RuntimeError('Gitがありません')
    version = subprocess.check_output([git, 'version']).strip()
    info['tools']['git'] = {'path': os.path.realpath(git), 'version': version.decode()}
    # 旧自己テストのpromisorの観点(GIT_NO_LAZY_FETCH)はgit 2.45以上が要る。古いgitで比べずに止める。
    found = re.match(rb'git version (\d+)\.(\d+)', version)
    if not found or tuple(map(int, found.groups())) < (2, 45):
        raise RuntimeError(f'Gitは2.45以上が必要です: {version.decode(errors="replace")}')
    return info


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.out, self.patch = self.root / 'out.md', self.root / 'out.patch'
        self.env = environment(self.root)
        closed_env.check(self.env)
        self.git('init', '-q')
        (self.repo / 'seed.txt').write_bytes(b'seed\n')
        self.git('add', 'seed.txt')
        self.git('commit', '-q', '-m', 'seed')
        self.base = self.git('rev-parse', 'HEAD').strip().decode()

    def git(self, *args, data=None):
        argv = ['git', '--no-pager', '--no-replace-objects', '-c', 'core.fsmonitor=',
                '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
                '-c', 'user.name=S3D fixture', '-c', 'user.email=s3d@example.invalid',
                '-c', 'init.defaultBranch=main', '-C', str(self.repo), *args]
        done = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env, check=False)
        if done.returncode:
            raise AssertionError('scratch Gitの入力作成に失敗しました')
        return done.stdout

    def normal(self):
        return ['--cwd', str(self.repo), '--base', self.base, '--out', str(self.out), '--exclude-glob', '.env']

    def clean_outputs(self):
        for path in (self.out, self.patch):
            if path.is_symlink() or path.is_file():
                path.unlink()

    def input_state(self):
        paths = [self.repo / '.git/index', self.repo / '.git/config']
        for root, dirs, files in os.walk(self.repo, followlinks=False):
            dirs[:] = [name for name in dirs if name != '.git']
            paths.extend(Path(root) / name for name in files)
        return {os.fsencode(path): file_state(path) for path in paths}

    def pair(self, binary, args, *, code, markers=(), env=None):
        values = []
        for command in (['bash', str(LEGACY)], [str(binary)]):
            self.clean_outputs()
            before = self.input_state()
            result = run([*command, *args], cwd=self.root, env=env or self.env,
                         report=self.out, patch=self.patch)
            unchanged(before, self.input_state())
            fixed(result, code)
            values.append(result)
        same_result(*values, markers=markers)
        return values


def arguments(case, fixture):
    normal, kind = fixture.normal(), case['kind']
    if kind == 'normal':
        return normal
    if kind == 'precheck':
        return ['--cwd', str(fixture.repo), '--precheck']
    if kind == 'print':
        return ['--print-exclude-ere', '--exclude-glob', '.env']
    if kind == 'value-missing':
        return ['--' + case['option']]
    if kind == 'required-missing':
        index = normal.index('--' + case['option'])
        return normal[:index] + normal[index + 2:]
    if kind == 'unknown':
        return ['--unknown-s3d']
    if kind == 'duplicate':
        value = 'a' * 64
        return normal + ['--' + case['option'], value, '--' + case['option'], value]
    if kind == 'combination':
        return {'print-precheck': ['--print-exclude-ere', '--precheck'],
                'print-ref': ['--print-exclude-ere', '--secret-profile-ref', fixture.base],
                'precheck-base': ['--cwd', str(fixture.repo), '--precheck', '--base', fixture.base]}[case['combination']]
    if kind == 'patch-base-only':
        return normal + ['--patch-base', fixture.base]
    if kind == 'pair-only':
        return normal + ['--' + case['option'], 'a' * 64]
    if kind == 'union-only':
        return normal + ['--secret-profile-union-sha256', 'a' * 64]
    if kind == 'union-invalid':
        return normal + ['--secret-profile-ref', fixture.base, '--secret-profile-union-sha256', case['value']]
    if kind == 'limit-invalid':
        return normal + ['--max-untracked-bytes', case['value']]
    if kind == 'help':
        return [case['option']]
    raise AssertionError('未知の引数ケースです')


def load_review_guard():
    spec = importlib.util.spec_from_file_location('s3d_review_guard', ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/review-guard.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ere_case(case, fixture, binary, cache, guard):
    key = case['kind'], case['pattern']
    if key not in cache:
        args = ['--print-exclude-ere', '--exclude-glob' if key[0] == 'glob' else '--exclude', key[1]]
        cache[key] = fixture.pair(binary, args, code=case['expected'])
    if case['expected']:
        return
    old, new = cache[key]
    if not old.stdout.endswith(b'\n') or old.stdout.count(b'\n') != 1:
        raise AssertionError('printのEREが1行ではありません')
    path = bytes.fromhex(case['path_hex'])
    results = []
    for result in (old, new):
        done = subprocess.run([b'grep', b'-Eiqz', b'--', result.stdout[:-1]], input=path + b'\0',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=fixture.env, check=False)
        if done.returncode not in (0, 1) or done.stdout or done.stderr:
            raise AssertionError('GNU ERE照合の結果が不正です')
        results.append(done.returncode == 0)
    if results[0] != results[1]:
        raise AssertionError('GNU EREのpath判定が旧版と一致しません')
    all_paths = [os.fsdecode(value) for value in PATHS]
    classified = []
    for result in (old, new):
        try:
            classified.append(('paths', guard.excluded_paths(all_paths, [os.fsdecode(result.stdout[:-1])])))
        except guard.GuardError as error:
            classified.append(('error', str(error)))
    if classified[0] != classified[1]:
        raise AssertionError('review-guardの旧新版の集合/拒否が一致しません')
    # 既存review-guardは各式を括弧で包む。後方参照の番号が変わるため、
    # この既存入力だけは両側とも拒否する。呼出元の挙動を変更する検査ではない。
    if key == ('ere', r'^(a)\1$'):
        if classified[0] != ('error', '機密除外 ERE が不正'):
            raise AssertionError('review-guardの既存の後方参照拒否が変わりました')
    elif classified[0][0] != 'paths' or (os.fsdecode(path) in classified[0][1]) != results[1]:
        raise AssertionError('review-guardと除外の集合が一致しません')


def profile_case(case, fixture, binary):
    kind, origin = case['kind'], case['origin']
    profile_dir = fixture.repo / '.claude'
    profile_dir.mkdir()
    profile = profile_dir / 'project-profile.yml'
    body = b'secret_paths:\n  - secrets/\n'
    reference = fixture.base
    if origin == 'current':
        if kind == 'regular':
            profile.write_bytes(body)
        elif kind == 'empty':
            profile.write_bytes(b'')
        elif kind == 'leaf-link':
            target = fixture.root / 'profile-source'
            target.write_bytes(body)
            profile.symlink_to(target)
        elif kind == 'parent-link':
            profile_dir.rmdir()
            target = fixture.root / 'profile-parent'
            target.mkdir()
            (target / profile.name).write_bytes(body)
            profile_dir.symlink_to(target, target_is_directory=True)
        elif kind == 'fifo':
            os.mkfifo(profile)
        elif kind in ('limit', 'over-limit'):
            size = 1048576 + (kind == 'over-limit')
            profile.write_bytes(b'#' + b' ' * (size - 2) + b'\n')
    elif kind in ('100644', '100755', '120000'):
        if kind == '120000':
            profile.symlink_to('missing-profile')
        else:
            profile.write_bytes(body)
            profile.chmod(int(kind[-3:], 8))
        fixture.git('add', '--', '.claude/project-profile.yml')
        fixture.git('commit', '-q', '-m', 'profile')
        reference = fixture.git('rev-parse', 'HEAD').strip().decode()
        profile.unlink()
    elif kind == 'empty-tree':
        reference = fixture.git('hash-object', '-t', 'tree', '/dev/null').strip().decode()
    elif kind == 'unresolved':
        reference = 'f' * 40
    elif kind == 'blob':
        reference = fixture.git('hash-object', '-w', '--stdin', data=b'not a commit\n').strip().decode()
    elif kind == 'invalid':
        reference = 'HEAD'
    args = fixture.normal() + ['--secret-profile-ref', reference, '--patch-out', str(fixture.patch)]
    fixture.pair(binary, args, code=case['expected'])


def external_vectors(binary, cases):
    done, cache = [], {}
    guard = load_review_guard()
    with tempfile.TemporaryDirectory(prefix='s3d-vectors-') as temporary:
        root = Path(temporary)
        for case in cases:
            if case['executor'] != 'external':
                continue
            base = root / case['id']
            base.mkdir()
            fixture = Fixture(base)
            try:
                if case['category'] == 'args':
                    fixture.pair(binary, arguments(case, fixture), code=case['expected'])
                elif case['category'] == 'ere':
                    ere_case(case, fixture, binary, cache, guard)
                elif case['category'] == 'profile':
                    profile_case(case, fixture, binary)
                else:
                    raise AssertionError('未実装の外部ケースです')
            except Exception as error:
                raise AssertionError(f'{case["id"]}: {type(error).__name__}: {error}') from error
            done.append(case['id'])
    exact_ids([case['id'] for case in cases if case['executor'] == 'external'], done)
    return done


def selftest(binary):
    source = legacy_adapter.verified_source(SELFTEST)
    observed = []
    # 旧自己テストのH2は、一時フォルダのパスを含む表示が200文字に収まる前提で書かれている
    # ('sh ' + TMPDIR + '/tmp.XXXXXXXXXX/stubs/trace-clean.sh ' + 72文字)。専用のTMPDIRを短い名前で作る。
    with tempfile.TemporaryDirectory(prefix='s3d-selftest-') as temporary, \
            tempfile.TemporaryDirectory(prefix='s3d-') as short:
        if len(short) + 3 + 15 + 6 + 16 + 72 > 200:
            raise RuntimeError('比較器の一時フォルダのパスが長すぎます(旧自己テストのH2の表示が200文字を超える)')
        base = Path(temporary)
        for name, command in (('legacy', ['bash', str(LEGACY)]), ('rust', [str(binary)])):
            result_log, pid_log = base / (name + '.results'), base / (name + '.pids')
            script, wrapper = base / (name + '.selftest.sh'), base / (name + '.target.sh')
            script.write_text(legacy_adapter.adapt(source, result_log=result_log))
            legacy_adapter.wrapper(wrapper, command, pid_log=pid_log)
            env = dict(closed_env.base(base, short), DIFF_SNAPSHOT=str(wrapper))
            result = run(['bash', str(script)], cwd=base, env=env, timeout=1800)
            records = legacy_adapter.observations(result_log.read_bytes())
            if result.timed_out:
                raise AssertionError(f'{name}の自己テストが時間切れです')
            failed = [key for key, (state, _) in records.items() if state == 'fail']
            skipped = [key for key, (state, _) in records.items() if state == 'skipped']
            if failed or result.returncode:
                raise AssertionError(f'{name}の自己テストに失敗があります: {failed}')
            if skipped:
                raise AssertionError(f'{name}の必須観点が未実施です: {skipped}')
            pids = pid_log.read_bytes().split(b'\0')
            if pids[-1] or not pids[:-1] or any(not pid.isdigit() for pid in pids[:-1]):
                raise AssertionError('実行物へ到達したPIDの記録が不正です')
            observed.append(records)
    exact_ids(observed[0], observed[1])
    fixed_ids=json.loads((FIXTURES/'legacy_ids.json').read_text())['ids']
    exact_ids(fixed_ids,observed[0]);exact_ids(fixed_ids,observed[1])
    # descriptionにはscratchのパスを含む場合がある。IDと合否は完全一致で固定する。
    for case_id in observed[0]:
        if observed[0][case_id][0] != observed[1][case_id][0]:
            raise AssertionError(f'自己テストの合否が一致しません: {case_id}')
    return list(observed[1])


def read_audits(binary):
    if sys.platform != 'linux':
        # Macでは型監査が必須。Linux上のDarwinスタブで成功に置換しない。
        tracer=shutil.which('dtruss')
        if tracer is None:
            reason={'kind':'tool-unavailable','tool':'dtruss'}
        else:
            observed=run([tracer,'-f','/usr/bin/true'],cwd=ROOT,env=dict(os.environ),timeout=15)
            if observed.returncode==0 and not observed.timed_out:
                raise RuntimeError('dtrussを利用できますが本文を記録しない解析方法が未検証です。OS監査を省略しません')
            reason={'kind':'trace-denied','tool':os.path.realpath(tracer),'code':observed.returncode,
                    'timed_out':observed.timed_out,'stderr_hex':observed.stderr.hex()}
        return {'os_trace':'unavailable','reason':reason,'typed_audit':'required-separately','cases':[]}
    done = []
    with tempfile.TemporaryDirectory(prefix='s3d-read-audit-') as temporary:
        for readable in (True, False):
            for changed in (False, True):
                name = f'read-{int(readable)}-changed-{int(changed)}'
                root = Path(temporary) / name
                root.mkdir()
                fixture = Fixture(root)
                secret = fixture.repo / '.env'
                secret.write_bytes(SECRET + b'\n')
                fixture.git('add', '.env')
                fixture.git('commit', '-q', '-m', 'synthetic secret')
                fixture.base = fixture.git('rev-parse', 'HEAD').strip().decode()
                if changed:
                    secret.write_bytes(SECRET + b'-changed\n')
                if not readable:
                    secret.chmod(0)
                try:
                    counts = traced_pair(fixture, binary, legacy=LEGACY, marker=SECRET)
                except Exception as error:
                    raise AssertionError(f'{name}: {error}') from error
                finally:
                    secret.chmod(0o600)
                done.append(dict(id=name, **counts))
        mutations = []
        root = Path(temporary) / 'mutations'
        root.mkdir()
        fixture = Fixture(root)
        secret = fixture.repo / '.env'
        secret.write_bytes(SECRET + b'\n')
        fixture.git('add', '.env')
        fixture.git('commit', '-q', '-m', 'synthetic secret')
        fixture.base = fixture.git('rev-parse', 'HEAD').strip().decode()
        alias = fixture.repo / 'nested/.env'
        alias.parent.mkdir()
        os.link(secret, alias)
        for name in ('read', 'hash', 'extra-git', 'hardlink-read', 'hardlink-hash', 'dynamic-hardlink', 'fd-inherit', 'mmap'):
            executable = root / ('mutant-' + name)
            # 検査器の故障検出専用。実行物が不在のときの代替入口ではない。
            script = '#!' + sys.executable + '\nimport hashlib,mmap,os,subprocess,sys\n'
            script += 'status=subprocess.run([' + repr(str(LEGACY)) + ', *sys.argv[1:]], check=False).returncode\n'
            script += 'if status: sys.exit(status)\n'
            # 分類と公開を終えた後で余分な操作を加える。OS監査はこれも拒む。
            if name == 'read':
                script += 'open(' + repr(str(secret)) + ', "rb").read()\n'
            elif name == 'hash':
                script += 'hashlib.sha256(open(' + repr(str(secret)) + ', "rb").read()).digest()\n'
            elif name == 'hardlink-read':
                script += 'open(' + repr(str(alias)) + ', "rb").read()\n'
            elif name == 'hardlink-hash':
                script += 'hashlib.sha256(open(' + repr(str(alias)) + ', "rb").read()).digest()\n'
            elif name == 'dynamic-hardlink':
                script += 'alias='+repr(str(root/'dynamic-alias'))+'\nos.link('+repr(str(secret))+',alias)\nopen(alias,"rb").read()\nos.unlink(alias)\n'
            elif name == 'fd-inherit':
                script += 'fd=os.open(' + repr(str(secret)) + ',os.O_RDONLY)\npid=os.fork()\nif pid==0:\n copy=os.dup(fd)\n os.close(fd)\n os.read(copy,4096)\n os._exit(0)\nos.waitpid(pid,0)\nos.close(fd)\n'
            elif name == 'mmap':
                script += 'fd=os.open(' + repr(str(secret)) + ',os.O_RDONLY)\nview=mmap.mmap(fd,0,access=mmap.ACCESS_READ)\nview.close()\nos.close(fd)\n'
            else:
                script += 'subprocess.run(["git", "-C", ' + repr(str(fixture.repo)) + ', "diff", "--name-only", "HEAD", "--", ".env"], stdout=subprocess.DEVNULL, check=True)\n'
            executable.write_text(script)
            executable.chmod(0o700)
            try:
                traced_pair(fixture, executable, legacy=LEGACY, marker=SECRET,link_creation_mutant=name=='dynamic-hardlink')
            except AssertionError as error:
                expected = ('Git比較の呼出し・順序・引数が旧版と一致しません' if name == 'extra-git'
                            else 'Git比較以外から機密本文へアクセスしました')
                if str(error) != expected:
                    raise AssertionError(f'audit-mutant-{name}: 監査の検出ではなく、変異入力の実行が失敗しました: {error}') from error
            else:
                raise AssertionError('読取り監査が意図的な不具合を見逃しました')
            mutations.append('audit-mutant-' + name)
    exact_ids(['read-1-changed-0', 'read-1-changed-1', 'read-0-changed-0', 'read-0-changed-1'],
              [case['id'] for case in done])
    exact_ids(['audit-mutant-' + name for name in ('read', 'hash', 'extra-git', 'hardlink-read', 'hardlink-hash', 'dynamic-hardlink', 'fd-inherit', 'mmap')], mutations)
    return {'os_trace': 'strace', 'cases': done, 'mutants_rejected': mutations}


def launch_table(work):
    """起動場所の表: 旧版との対応、実測の文との一致、別の版の文を入れた表の拒否。"""
    table = launch_sites.load()
    shape = launch_sites.check_static(table, LEGACY.read_text())
    base, found = launch_sites.is_base(table)
    measured = launch_sites.measure(work)
    failures = launch_sites.check_measured(table, measured)
    # 変異: coreutils 8.32の文を入れた表を、実測との照合が拒否する。
    mutant = json.loads(json.dumps(table))
    for site in mutant['sites']:
        for diagnostic in site.get('diagnostics', ()):
            if diagnostic['recipe'] == 'cp-fsize':
                diagnostic['measured'] = "cp: error writing '<D>/big.tmp': File too large\n"
    rejected = bool(launch_sites.check_measured(mutant, measured))
    version_failures = []
    if base:
        if failures:
            raise AssertionError(f'起動場所の表の文が基準の版の実測と違います: {failures}')
        if not rejected:
            raise AssertionError('別の版の道具の文を入れた表を拒否できません')
    else:
        # 基準と違う機械では、版に依る欄を失敗として記録する(成功やskipにしない)。
        version_failures = ['launch-table-measured']
    return {'shape': shape, 'versions': found, 'base': base, 'differences': failures,
            'mutant_rejected': rejected, 'version_failures': version_failures}


SCOPES = ['vectors', 'selftest', 'read-audit', 'profile', 'parser', 'platform', 'publication', 'output-faults', 'fd',
          'raw-env', 'entry-signals', 'review', 'partial', 'launch-table', 'path-reduced', 'fixed-extra']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--only', choices=SCOPES)
    args = parser.parse_args(argv)
    began = time.monotonic()
    try:
        binary = binary_path(os.environ.get('DEV_WORKFLOW_TEST_DIFF_SNAPSHOT_BINARY'))
        typed_path = os.environ.get('DEV_WORKFLOW_S3D_TYPED_REPORT')
        # 周囲の変数を子へ渡さないよう、比較器の環境を許可一覧へ縮める(外した名前は記録する)。
        closed_env.close_process()
        before = file_state(binary)
        machine = requirements()
        data = manifest()
        result = dict(machine=machine, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                      approved_differences=APPROVED_DIFFERENCES, external=[], legacy=[],
                      typed_required=[case['id'] for case in data['vectors'] if case['executor'] == 'typed'],
                      typed_assertions_required=[case['id'] for case in data['typed_assertions']],
                      typed_extra_required=data['typed_extra_required'], additional=[], platform=[], publication=[], signals=[], profile_replacements=[], output_faults=[], fd=[], entry_signals=[], review=[], partial_outputs=[], version_failures=[], not_run=[])
        base, versions = launch_sites.is_base(launch_sites.load())
        darwin = platform.system() == 'Darwin'
        result['tool_versions'], result['tool_versions_base'] = versions, base
        if not base:
            # 基準でない機械: manifestの版に依る欄だけを比べずに記録し、ほかの欄は比べる。
            compare_module.VERSION_TOLERANT['names'] = {name.split(':')[0] for fields in data['version_fields'].values()
                                                        for name in fields}
        proof = None
        if args.only in (None, 'publication'):
            compare_module.CURRENT_SCOPE[0] = 'publication'
            from source_input_hash import source_input_hash
            proof = typed_report(typed_path,
                                 source_sha256=source_input_hash(ROOT),
                                 manifest_sha256=hashlib.sha256((FIXTURES/'cases.json').read_bytes()).hexdigest(),
                                 expected_ids=result['typed_required'] + result['typed_assertions_required'] + result['typed_extra_required'])
            result['typed_verified'] = proof['passed']
        with tempfile.TemporaryDirectory(prefix='s3d-env-') as work:
            result['environment_mutants_rejected'] = closed_env.self_check(work)
        if args.only in (None, 'launch-table'):
            compare_module.CURRENT_SCOPE[0] = 'launch-table'
            with tempfile.TemporaryDirectory(prefix='s3d-launch-') as work:
                result['launch_table'] = launch_table(work)
            result['version_failures'] += result['launch_table']['version_failures']
        if args.only in (None, 'vectors'):
            compare_module.CURRENT_SCOPE[0] = 'vectors'
            result['external'] = external_vectors(binary, data['vectors'])
        if args.only in (None, 'path-reduced'):
            compare_module.CURRENT_SCOPE[0] = 'path-reduced'
            with tempfile.TemporaryDirectory(prefix='s3d-path-') as work:
                result['path_reduced'] = path_reduction.run(binary, external_vectors=external_vectors, vectors=data['vectors'],
                                                            fixture_type=Fixture, legacy=LEGACY, root=work)
        if args.only in (None, 'selftest'):
            compare_module.CURRENT_SCOPE[0] = 'selftest'
            result['legacy'] = selftest(binary)
        if args.only in (None, 'read-audit'):
            compare_module.CURRENT_SCOPE[0] = 'read-audit'
            result['read_audit'] = read_audits(binary)
        if args.only in (None, 'profile'):
            compare_module.CURRENT_SCOPE[0] = 'profile'
            result['additional'] += profile_cases.run_cases(binary, fixture_type=Fixture, legacy=LEGACY)
        if args.only in (None, 'parser'):
            compare_module.CURRENT_SCOPE[0] = 'parser'
            result['additional'] += parser_cases.run_cases(binary, fixture_type=Fixture)
        if args.only in (None, 'platform'):
            compare_module.CURRENT_SCOPE[0] = 'platform'
            result['platform'] = platform_cases.run_cases(binary, fixture_type=Fixture, legacy=LEGACY)
        if args.only in (None, 'publication'):
            compare_module.CURRENT_SCOPE[0] = 'publication'
            result['publication'] = publication.run_cases(fixture_type=Fixture, legacy=LEGACY, typed_observations=proof.get('publication_observations', []))
            result['signals'] = publication.run_signals(fixture_type=Fixture, legacy=LEGACY, typed_observations=proof.get('signal_observations', []))
        if args.only in (None, 'output-faults'):
            compare_module.CURRENT_SCOPE[0] = 'output-faults'
            result['output_faults'] = output_faults.run_cases(binary, fixture_type=Fixture, legacy=LEGACY)
        if args.only in (None, 'fd'):
            compare_module.CURRENT_SCOPE[0] = 'fd'
            result['fd'] = fd_cases.run_cases(binary, fixture_type=Fixture, legacy=LEGACY)
        if args.only in (None,'raw-env'):
            compare_module.CURRENT_SCOPE[0] = 'raw-env'
            result['additional'] += raw_env.run_cases(binary,fixture_type=Fixture,legacy=LEGACY)
        if args.only in (None,'entry-signals'):
            compare_module.CURRENT_SCOPE[0] = 'entry-signals'
            result['entry_signals'] = entry_signals.run_cases(binary,fixture_type=Fixture,legacy=LEGACY)
            exact_ids([case['id'] for case in data['entry_signals']],[item['case_id'] for item in result['entry_signals']])
        if args.only in (None,'review'):
            compare_module.CURRENT_SCOPE[0] = 'review'
            result['review'] = review_cases.run_cases(binary,fixture_type=Fixture,legacy=LEGACY)
            exact_ids([case['id'] for case in data['review_cases']],[item['id'] for item in result['review']])
        if args.only in (None,'partial'):
            compare_module.CURRENT_SCOPE[0] = 'partial'
            try:
                result['partial_outputs'] = review_cases.run_partial(binary,fixture_type=Fixture,legacy=LEGACY)
                exact_ids([case['id'] for case in data['partial_outputs']],[item['id'] for item in result['partial_outputs']])
            except compare_module.NotRun as error:
                exact_ids([case['id'] for case in data['partial_outputs']], error.ids)
                result['not_run'] += [{'id': case_id, 'reason': error.reason} for case_id in error.ids]
        if args.only in (None, 'fixed-extra'):
            compare_module.CURRENT_SCOPE[0] = 'fixed-extra'
            finished = set(result['external']) | {item['id'] for item in result['review']} | set(result['additional'])
            typed_ids = set(proof['passed']) if proof else set()
            result['fixed_extra'] = fixed_extra.run_cases(binary, fixture_type=Fixture, legacy=LEGACY,
                                                          finished_ids=finished, typed_ids=typed_ids,
                                                          typed_observations=proof.get('timeout_observations', []) if proof else [])
            result['version_failures'] += result['fixed_extra']['version_failed']
            if darwin:
                # Macで作れない(straceが無い)書込みの故障は、未実施として記録する(成功に数えない)。
                moved = [case_id for case_id in result['fixed_extra']['pending'] if case_id.startswith('rust-file-write-')]
                result['not_run'] += [{'id': case_id, 'reason': 'strace'} for case_id in moved]
                # 別のプロセスの/proc/PID/memで開けないファイルを作る2件は、Linuxだけで作れる(型検査は全機種で流す)。
                proc = [case_id for case_id in result['fixed_extra']['pending']
                        if case_id in {'pr2-' + kind for kind in fixed_extra.prereview2.PROC_KINDS}]
                result['not_run'] += [{'id': case_id, 'reason': 'proc'} for case_id in proc]
                moved += proc
                result['fixed_extra']['pending'] = [i for i in result['fixed_extra']['pending'] if i not in moved]
            if args.only is None and result['fixed_extra']['pending']:
                raise AssertionError(f'固定入力の未到達 {len(result["fixed_extra"]["pending"])} 件: '
                                     + ', '.join(result['fixed_extra']['pending']))
        if args.only is None:
            compare_module.CURRENT_SCOPE[0] = 'full'
            result['profile_replacements'] = profile_races.run_cases(helper=LEGACY.with_name('secret-profiles.py'), typed_observations=proof.get('profile_observations', []))
            result['typed_audit'] = typed_audit.run_cases(fixture_type=Fixture,legacy=LEGACY,typed_observations=proof.get('audit_observations',[]))
            exact_ids([case['id'] for case in data['additional_external']],result['additional'])
            exact_ids(data['legacy_assertion_ids'],result['legacy'])
            result['coverage']=coverage.groups(result['legacy'],result['typed_assertions_required'])
            if any(result['coverage'][group['id']]!=group['case_ids'] for group in data['groups']):
                raise AssertionError('実行済みの71観点が固定した記録と一致しません')
        unchanged(before, file_state(binary))
        if args.only in (None, 'path-reduced'):
            result['not_run'] += [{'id': case_id, 'reason': 'strace'} for case_id in result['path_reduced']['not_run']]
        # 版に依る欄: 走らせた群の観測が、manifestの欄の一覧と一致すること(印の抜けと誤りを失敗にする)。
        scopes = (SCOPES + ['full']) if args.only is None else [args.only]
        try:
            compare_module.check_versions(data['version_fields'], scopes)
            version_error = None
        except AssertionError as error:
            version_error = str(error)
        hits = compare_module.VERSION_HITS
        result['version_fields'] = {scope: {name: sorted(kinds) for name, kinds in sorted(hits[scope].items())}
                                    for scope in sorted(hits)}
        result['version_dependent'] = sorted({name for scope in hits for name in hits[scope]})
        result['version_masked'] = sorted(compare_module.VERSION_MASKED)
        if not base:
            result['version_failures'] = sorted(set(result['version_failures']) | set(result['version_dependent']))
        if darwin and not base:
            # Macでは基準の版(Ubuntu 24.04のbash・coreutils・glibc)を用意できない。版に依る欄は未実施と記録する。
            result['not_run'] += [{'id': name, 'reason': 'version'} for name in result['version_failures']]
            result['version_failures'] = []
        result['environment'] = closed_env.report()
        result['mac_cf_user_text_encoding'] = review_cases.CF_RECORDS
        result['seconds'] = round(time.monotonic() - began, 3)
        result['scope'] = 'full-external' if args.only is None else args.only
        if args.report:
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        if version_error:
            raise AssertionError(version_error)
        print(f'外部比較: 成功 {len(result["external"])} / 追加 {len(result["additional"])} / 旧assertion {len(result["legacy"])} / 入口の信号 {len(result["entry_signals"])} / 点検 {len(result["review"])} / 部分出力 {len(result["partial_outputs"])} / {result["seconds"]} 秒')
        print(f'型検査は別実行: 入力 {len(result["typed_required"])} / 旧assertion {len(result["typed_assertions_required"])}')
        if result['not_run']:
            print(f'未実施(成功に数えない): {len(result["not_run"])} 件: '
                  + ', '.join(f'{item["id"]}({item["reason"]})' for item in result['not_run']))
        if result['version_failures']:
            print(f'S3D比較失敗: 基準の版でない機械のため、版に依る欄 {len(result["version_failures"])} 件を失敗にした: '
                  + ', '.join(result['version_failures']), file=sys.stderr)
            return 1
        return 0
    except (AssertionError, OSError, ValueError, RuntimeError) as error:
        print(f'S3D比較失敗: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
