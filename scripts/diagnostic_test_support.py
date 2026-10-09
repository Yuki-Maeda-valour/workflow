"""#260: 本物の保護記録を使う、診断公開CLIの契約試験。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
GUARD = SCRIPTS / 'implement-guard.sh'
DIAGNOSTIC = SCRIPTS / 'diagnostic-git.py'


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('JSONのkeyが重複: ' + key)
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError('JSONで認めない定数: ' + value)

    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=invalid_constant)


def inventory(root):
    """試験所有の小さい通常ツリーだけを、内容とmodeで照合する。"""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        key = os.fsencode(path.relative_to(root))
        if stat.S_ISREG(info.st_mode):
            result[key] = ('regular', mode, path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            result[key] = ('directory', mode)
        elif stat.S_ISLNK(info.st_mode):
            result[key] = ('symlink', mode, os.fsencode(os.readlink(path)))
        else:
            raise AssertionError('通常fixtureに特殊fileがある: ' + str(path))
    return result


class DiagnosticFixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='diagnostic-repo-')
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name).resolve()
        self.repo = self.work / 'repo'
        self.repo.mkdir()
        # takeは/tmp/TMPDIR/repo配下の保護領域を拒否する。
        # 既存guardを探索せず、実HOME直下の新規専用領域だけを所有する。
        protected = tempfile.TemporaryDirectory(prefix='diagnostic-test-', dir=Path.home())
        self.addCleanup(protected.cleanup)
        self.protected = Path(protected.name).resolve()
        self.assertEqual(stat.S_IMODE(self.protected.stat().st_mode), 0o700)
        self.assertFalse(self.protected.is_relative_to(Path('/tmp').resolve()))
        self.assertFalse(self.protected.is_relative_to(self.work))
        self.plan_dir = self.protected / 'plans'
        self.plan_dir.mkdir(mode=0o700)
        self.home = self.work / 'home'
        self.home.mkdir()
        xdg_config = self.work / 'config'
        xdg_config.mkdir()
        self.env = {
            'PATH': os.environ.get('PATH', os.defpath), 'LC_ALL': 'C',
            'HOME': str(self.home), 'XDG_CONFIG_HOME': str(xdg_config),
            'XDG_STATE_HOME': str(self.protected), 'TMPDIR': str(self.work),
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CEILING_DIRECTORIES': str(self.work),
            'GIT_PAGER': 'cat', 'PYTHONDONTWRITEBYTECODE': '1',
        }
        self.git_binary = shutil.which('git', path=self.env['PATH'])
        self.assertIsNotNone(self.git_binary, '通常fixtureにGitが必要')
        self.git('init', '-q')
        (self.repo / 'seed.txt').write_bytes(b'public seed\n')
        self.git('add', '--', 'seed.txt')
        self.git('commit', '-qm', 'seed')
        self.start_oid = self.git('rev-parse', 'HEAD').stdout.decode('ascii').strip()
        self.assertRegex(self.start_oid, r'^[0-9a-f]{40}$')
        self.base_oid = self.start_oid
        (self.repo / 'task').mkdir()
        (self.repo / 'task/t.md').write_text('# タスク\n\n- [ ] 通常の試験\n', encoding='utf-8')

    def git(self, *args):
        return subprocess.run(
            [self.git_binary, '-c', 'user.name=fixture', '-c',
             'user.email=fixture@example.invalid', '-c', 'init.defaultBranch=main',
             '-c', 'core.fsmonitor=', '-c', 'core.hooksPath=/dev/null',
             '-C', str(self.repo), *args], env=self.env, cwd=self.work,
            stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30)

    def take(self):
        before = inventory(self.repo)
        result = subprocess.run(
            ['bash', str(GUARD), 'take', '--cwd', str(self.repo),
             '--task-md', 'task/t.md'], cwd=self.work, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, '本物takeのfixture失敗: ' + repr(result.stderr))
        self.assertEqual(inventory(self.repo), before, 'takeで元repoのbytes/modeが変わった')
        fields = {}
        for line in result.stdout.decode('ascii').splitlines():
            key, value = line.split('=', 1)
            self.assertNotIn(key, fields)
            fields[key] = value
        self.assertEqual(set(fields), {'STATE_DIR', 'MANIFEST_SHA256',
                                      'SNAPSHOT_SHA256', 'TASKMD_REAL', 'BYTES',
                                      'ENTRIES', 'UNREADABLE'})
        for key in ('MANIFEST_SHA256', 'SNAPSHOT_SHA256'):
            self.assertRegex(fields[key], r'^[0-9a-f]{64}$')
        state = Path(fields['STATE_DIR'])
        self.assertEqual(state.parent, self.protected / 'dev-workflow')
        self.assertTrue(state.name.startswith('guard-'))
        self.assertFalse(state.is_symlink())
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(fields['TASKMD_REAL'], str(self.repo / 'task/t.md'))
        self.assertTrue((state / 'snapshot/config-contexts.txt').is_file())
        return state, fields

    def cli(self, *args, no_site=False, env=None):
        command = [sys.executable, '-I']
        if no_site:
            command.append('-S')
        return subprocess.run([*command, str(DIAGNOSTIC), *map(str, args)],
                              cwd=self.work, env=self.env if env is None else env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=330)

    def success(self, result, status):
        self.assertEqual(result.returncode, 0, repr(result.stderr))
        self.assertTrue(result.stdout.isascii())
        self.assertEqual(result.stdout.count(b'\n'), 1)
        self.assertTrue(result.stdout.endswith(b'\n'))
        value = strict_json(result.stdout)
        self.assertIs(type(value['version']), int)
        self.assertEqual(value['version'], 1)
        self.assertEqual(value['status'], status)
        return value

    def failure(self, result, rc, reason=None):
        self.assertEqual(result.returncode, rc, repr(result.stderr))
        self.assertEqual(result.stdout, b'')
        self.assertRegex(result.stderr, rb'^ERROR \[diagnostic:[a-z0-9-]+\]')
        self.assertNotIn(b'Traceback', result.stderr)
        if reason is not None:
            self.assertIn(('diagnostic:' + reason + ']').encode(), result.stderr)

    def begin(self, *extra, context=0, start=None, base=None):
        self.state, fields = self.take()
        self.binding = {'manifest_sha256': fields['MANIFEST_SHA256'],
                        'snapshot_sha256': fields['SNAPSHOT_SHA256']}
        self.common = ['--cwd', str(self.repo), '--state', str(self.state),
                       '--manifest-sha256', self.binding['manifest_sha256'],
                       '--snapshot-sha256', self.binding['snapshot_sha256'],
                       '--context-id', str(context)]
        self.plan_number = 0
        self.latest = self.plan_dir / 'plan-0.json'
        response = self.success(self.cli('plan', *self.common,
            '--base-ref', base or self.base_oid, '--start-ref', start or self.start_oid,
            '--start-kind', 'new-invocation', *extra, '--out', self.latest), 'plan-created')
        self.latest_hash = response['plan_sha256']
        self.assertEqual(self.latest_hash, hashlib.sha256(self.latest.read_bytes()).hexdigest())
        return response

    def extend(self, operation, *args):
        self.plan_number += 1
        output = self.plan_dir / ('plan-%d.json' % self.plan_number)
        old_path, old_hash = self.latest, self.latest_hash
        old_bytes = old_path.read_bytes()
        response = self.success(self.cli('extend-plan', *self.common, '--plan', old_path,
            '--plan-sha256', old_hash, '--out', output, operation, *args), 'plan-extended')
        self.assertEqual(set(response), {'version', 'status', 'plan_path_b64', 'plan_sha256',
            'context_id', 'state_binding', 'input_plan_sha256', 'effective_exclusions_sha256'})
        self.assertEqual(response['input_plan_sha256'], old_hash)
        self.assertEqual(response['state_binding'], self.binding)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(base64.b64decode(response['plan_path_b64'], validate=True), os.fsencode(output))
        self.latest, self.latest_hash = output, response['plan_sha256']
        self.assertEqual(self.latest_hash, hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        plan = strict_json(output.read_bytes())
        self.assertEqual(plan['parent_plan_sha256'], old_hash)
        self.assertEqual(plan['prepared_request']['operation'], operation)
        self.assertEqual(plan['context_id'], int(self.common[-1]))
        old_plan = strict_json(old_bytes)
        policies = {(x['context_id'], x['root_prefix_b64']): x for x in plan['policies']}
        for old in old_plan['policies']:
            new = policies[(old['context_id'], old['root_prefix_b64'])]
            for field in ('sources', 'globs', 'eres'):
                self.assertTrue(all(x in new[field] for x in old[field]), (field, old, new))
        effective = {'policies': [
            {key: policy[key] for key in ('context_id', 'root_kind', 'root_prefix_b64', 'globs', 'eres')}
            for policy in sorted(plan['policies'], key=lambda p: (
                p['context_id'], base64.b64decode(p['root_prefix_b64'], validate=True)))],
            'default_policy': 'snapshot-v1'}
        effective_raw = json.dumps(effective, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.assertEqual(response['effective_exclusions_sha256'],
                         hashlib.sha256(effective_raw).hexdigest())
        self.effective_hash = response['effective_exclusions_sha256']
        return response

    def observe(self, operation, *args):
        self.extend(operation, *args)
        result = self.cli('run', *self.common, '--plan', self.latest,
                          '--plan-sha256', self.latest_hash)
        response = self.success(result, 'observed')
        self.assertEqual(set(response), {'version', 'status', 'operation', 'context_id',
            'view_policy', 'scope', 'data', 'warnings', 'effective_exclusions_sha256',
            'plan_sha256', 'baseline_record_updated', 'resume_authorized'})
        self.assertEqual(response['operation'], operation)
        self.assertEqual(response['context_id'], int(self.common[-1]))
        self.assertEqual(response['view_policy'], 'raw-selected-v1')
        self.assertIs(response['baseline_record_updated'], False)
        self.assertIs(response['resume_authorized'], False)
        self.assertEqual(response['plan_sha256'], self.latest_hash)
        self.assertEqual(response['effective_exclusions_sha256'], self.effective_hash)
        self.assertIn('settings-not-applied', response['warnings'])
        self.assertEqual(response['warnings'], sorted(set(response['warnings'])))
        self.assertLessEqual(set(response['warnings']), {'settings-not-applied',
            'ignore-not-applied', 'nested-content-not-observed', 'sparse-not-materialized',
            'paths-excluded'})
        for scope in response['scope']:
            self.assertEqual(set(scope), {'kind', 'path_b64', 'redacted'})
            self.assertIn(scope['kind'], ('path', 'under'))
            self.assertIs(type(scope['redacted']), bool)
            if scope['redacted']:
                self.assertIsNone(scope['path_b64'])
            else:
                base64.b64decode(scope['path_b64'], validate=True)
        return response

    def records(self, response):
        return {base64.b64decode(x['path_b64'], validate=True): x
                for x in response['data']['records']}

    def profile(self, paths):
        path = self.repo / '.claude/project-profile.yml'
        path.parent.mkdir(exist_ok=True)
        path.write_text(('secret_paths:\n' + ''.join('  - ' + json.dumps(x) + '\n' for x in paths))
                        if paths else 'secret_paths: []\n')
        return path

    def commit(self, message='change'):
        self.git('add', '--all')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD').stdout.decode().strip()
