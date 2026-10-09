"""#260: 正当なGit形式・管理座標・複数contextの公開回帰。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from diagnostic_test_support import DiagnosticFixture, GUARD, strict_json


class DiagnosticFormats(DiagnosticFixture):
    def recreate(self, *options, commit=True):
        shutil.rmtree(self.repo)
        self.repo.mkdir()
        self.git('init', '-q', *options)
        (self.repo / 'seed.txt').write_bytes(b'public seed\n')
        if commit:
            self.git('add', 'seed.txt')
            self.git('commit', '-qm', 'seed')
            self.start_oid = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        else:
            self.start_oid = self.git('hash-object', '-t', 'tree', '-w', '--stdin').stdout.decode().strip()
        self.base_oid = self.start_oid
        (self.repo / 'task').mkdir()
        (self.repo / 'task/t.md').write_text('# fixture\n- [ ] normal\n')

    def test_sha256_repository_keeps_oid_format(self):
        self.recreate('--object-format=sha256')
        self.assertEqual(len(self.start_oid), 64)
        self.begin()
        result = self.observe('refs')
        self.assertTrue(result['data']['records'])
        for record in result['data']['records']:
            if record['oid'] is not None:
                self.assertRegex(record['oid'], '^[0-9a-f]{64}$')
        result = self.observe('show', '--path', 'seed.txt', '--format', 'blob')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'public seed\n')

    def test_unborn_all_legal_empty_operations(self):
        self.recreate(commit=False)
        self.begin()
        result = self.observe('log')
        self.assertIs(result['data']['unborn'], True)
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'')
        result = self.observe('refs')
        self.assertEqual(result['data']['records'], [])
        result = self.observe('stashes')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'')
        result = self.observe('status', '--path', 'seed.txt')
        self.assertEqual(self.records(result)[b'seed.txt']['worktree_comparison'], 'untracked')

    def test_detached_head_remains_detached(self):
        self.git('checkout', '--detach', '-q')
        self.begin()
        result = self.observe('log')
        self.assertIn(self.start_oid.encode(), base64.b64decode(result['data']['bytes_b64']))
        self.assertEqual((self.repo / '.git/HEAD').read_text().strip(), self.start_oid)

    def test_split_index_shared_file_is_used_without_rewriting(self):
        self.git('update-index', '--split-index')
        shared = list((self.repo / '.git').glob('sharedindex.*'))
        self.assertTrue(shared)
        before = {p.name: p.read_bytes() for p in shared}
        self.begin()
        result = self.observe('files', '--path', 'seed.txt')
        self.assertTrue(self.records(result)[b'seed.txt']['tracked'])
        self.assertEqual({p.name: p.read_bytes() for p in shared}, before)

    def test_sparse_index_expands_logical_entries(self):
        for directory in ('in', 'out'):
            (self.repo / directory).mkdir()
            (self.repo / directory / 'file').write_text(directory)
        self.commit()
        self.git('sparse-checkout', 'init', '--cone', '--sparse-index')
        self.git('sparse-checkout', 'set', 'in')
        (self.repo / 'task').mkdir(exist_ok=True)
        (self.repo / 'task/t.md').write_text('# sparse fixture\n- [ ] normal\n')
        self.assertFalse((self.repo / 'out/file').exists())
        self.begin()
        result = self.observe('diff', '--under', 'out', '--format', 'patch')
        record = self.records(result)[b'out/file']
        self.assertEqual(record['comparison'], 'not-materialized')
        self.assertIsNone(record['patch_b64'])
        self.assertEqual(record['right']['kind'], 'absent')
        self.assertIn('sparse-not-materialized', result['warnings'])
        result = self.observe('show', '--path', 'out/file', '--format', 'blob')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'out')

    def test_unmerged_index_reports_stages_but_head_worktree_comparison_works(self):
        self.begin()
        oid = self.git('rev-parse', 'HEAD:seed.txt').stdout.strip()
        payload = b'0 ' + b'0' * 40 + b'\tseed.txt\n'
        payload += b''.join(b'100644 ' + oid + b' ' + str(n).encode() + b'\tseed.txt\n' for n in (1, 2, 3))
        command = [self.git_binary, '-C', str(self.repo), 'update-index', '--index-info']
        subprocess.run(command, input=payload, env=self.env, check=True, capture_output=True, timeout=30)
        result = self.observe('status', '--path', 'seed.txt')
        record = self.records(result)[b'seed.txt']
        self.assertIsNone(record['index'])
        self.assertEqual([r['stage'] for r in record['stages']], [1, 2, 3])
        self.extend('diff', '--path', 'seed.txt')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash), 23, 'unmerged-index')
        result = self.observe('diff', '--left', 'HEAD', '--right', 'worktree', '--path', 'seed.txt')
        self.assertEqual(result['data']['records'], [])

    def test_linked_worktree_uses_its_own_head_and_index(self):
        linked = self.work / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'linked', str(linked))
        self.repo = linked
        (self.repo / 'task').mkdir()
        (self.repo / 'task/t.md').write_text('# fixture\n')
        (self.repo / 'seed.txt').write_text('linked change\n')
        self.begin()
        result = self.observe('status', '--path', 'seed.txt')
        self.assertEqual(self.records(result)[b'seed.txt']['worktree_comparison'], 'modified')
        self.assertEqual((self.work / 'repo/seed.txt').read_bytes(), b'public seed\n')

    def test_management_profile_coordinates_are_not_top_level_coordinates(self):
        (self.repo / 'pkg[1]*').mkdir()
        (self.repo / 'pkg[1]*/.claude').mkdir()
        (self.repo / 'pkg[1]*/.claude/project-profile.yml').write_text('secret_paths:\n  - /secret.txt\n')
        (self.repo / 'pkg[1]*/secret.txt').write_text('HIDDEN_MANAGEMENT')
        (self.repo / 'secret.txt').write_text('PUBLIC_TOP')
        self.commit()
        # 管理cwdが異なるstateを使い回さず、そのcwdで本物takeを取り直す。
        taken = subprocess.run(['bash', str(GUARD), 'take',
            '--cwd', str(self.repo / 'pkg[1]*'), '--task-md', str(self.repo / 'task/t.md')],
            env=self.env, capture_output=True, timeout=120)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        fields = dict(line.split('=', 1) for line in taken.stdout.decode().splitlines())
        self.state = Path(fields['STATE_DIR'])
        self.binding = {'manifest_sha256': fields['MANIFEST_SHA256'], 'snapshot_sha256': fields['SNAPSHOT_SHA256']}
        self.common = ['--cwd', str(self.repo / 'pkg[1]*'), '--state', str(self.state),
                      '--manifest-sha256', fields['MANIFEST_SHA256'], '--snapshot-sha256', fields['SNAPSHOT_SHA256'], '--context-id', '0']
        self.latest = self.plan_dir / 'management.json'; self.plan_number = 0
        response = self.success(self.cli('plan', *self.common, '--base-ref', self.base_oid,
            '--start-ref', self.start_oid, '--start-kind', 'new-invocation', '--out', self.latest), 'plan-created')
        self.latest_hash = response['plan_sha256']
        plan = strict_json(self.latest.read_bytes())
        self.assertEqual(base64.b64decode(plan['management_binding']['relative_prefix_b64']), b'pkg[1]*')
        self.assertEqual({p['root_kind'] for p in plan['policies']}, {'worktree', 'management'})
        result = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertIn(b'secret.txt', self.records(result))
        self.assertNotIn(b'pkg[1]*/secret.txt', self.records(result))

    def test_reftable_unresolved_refs_and_history(self):
        version = self.git('--version').stdout.decode()
        parts = version.split()[2].split('.')
        if tuple(int(x) for x in parts[:2]) < (2, 45):
            self.skipTest('実行Gitにreftableなし: ' + version.strip())
        self.recreate('--ref-format=reftable')
        self.git('symbolic-ref', 'refs/heads/dangling', 'refs/heads/missing')
        (self.repo / 'seed.txt').write_text('stash ref format')
        self.git('stash', 'push', '-qm', 'reftable stash')
        self.begin()
        result = self.observe('refs')
        refs = {base64.b64decode(r['name_b64']): r for r in result['data']['records']}
        self.assertEqual(refs[b'refs/heads/dangling']['resolution'], 'unresolved')
        result = self.observe('stashes')
        self.assertIn(b'reftable stash', base64.b64decode(result['data']['bytes_b64']))

    def test_reftable_many_blocks_updates_and_deletion_from_actual_git(self):
        version = self.git('--version').stdout.decode()
        if tuple(int(x) for x in version.split()[2].split('.')[:2]) < (2, 45):
            self.skipTest('実行Gitにreftableなし: ' + version.strip())
        self.recreate('--ref-format=reftable')
        self.git('config', 'reftable.blockSize', '1024')
        self.begin()
        lines = ''.join('update refs/heads/r%04d %s\n' % (n, self.start_oid) for n in range(2001))
        subprocess.run([self.git_binary, '-C', str(self.repo), 'update-ref', '--stdin'],
                       input=lines.encode(), env=self.env, capture_output=True, check=True, timeout=30)
        self.git('update-ref', '-d', 'refs/heads/r0000')
        result = self.observe('refs')
        refs = {base64.b64decode(r['name_b64']): r for r in result['data']['records']}
        self.assertNotIn(b'refs/heads/r0000', refs)
        for n in range(1, 2001):
            self.assertEqual(refs[('refs/heads/r%04d' % n).encode()]['oid'], self.start_oid)


    def use_clone(self, args):
        original = self.repo
        clone = self.work / 'clone'
        self.git('clone', '-q', *args, str(clone))
        self.repo = clone
        (clone / 'task').mkdir(exist_ok=True)
        (clone / 'task/t.md').write_text('# clone fixture\n- [ ] normal\n')
        self.start_oid = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.base_oid = self.start_oid
        return original

    def test_shallow_boundary_is_preserved_without_fetching_older_history(self):
        (self.repo / 'seed.txt').write_text('second commit\n')
        self.commit('second')
        self.use_clone(['--depth', '1', self.repo.as_uri()])
        shallow = (self.repo / '.git/shallow').read_bytes()
        self.begin()
        result = self.observe('log', '--limit', '20')
        text = base64.b64decode(result['data']['bytes_b64'])
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn(b'second', text)
        self.assertEqual((self.repo / '.git/shallow').read_bytes(), shallow)

    def test_file_alternates_are_used_without_copying_or_modifying_source(self):
        from diagnostic_test_support import inventory
        original = self.use_clone(['--shared', str(self.repo)])
        before = inventory(original)
        self.assertTrue((self.repo / '.git/objects/info/alternates').is_file())
        self.begin()
        result = self.observe('show', '--path', 'seed.txt', '--format', 'blob')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'public seed\n')
        self.assertEqual(inventory(original), before)

    def test_linked_ref_priority_excludes_main_worktree_private_refs(self):
        self.git('update-ref', 'refs/worktree/selected', self.start_oid)
        self.git('update-ref', 'refs/worktree/main-only', self.start_oid)
        linked = self.work / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'linked', str(linked))
        self.repo = linked
        (self.repo / 'seed.txt').write_text('linked commit\n')
        linked_oid = self.commit('linked')
        self.git('update-ref', 'refs/worktree/selected', linked_oid)
        (self.repo / 'task').mkdir()
        (self.repo / 'task/t.md').write_text('# linked fixture\n')
        self.start_oid = self.base_oid = linked_oid
        self.begin()
        result = self.observe('refs')
        refs = {base64.b64decode(r['name_b64']): r for r in result['data']['records']}
        self.assertEqual(refs[b'refs/worktree/selected']['oid'], linked_oid)
        self.assertNotIn(b'refs/worktree/main-only', refs)
        self.assertIn(b'refs/heads/main', refs)



if __name__ == '__main__':
    unittest.main()
