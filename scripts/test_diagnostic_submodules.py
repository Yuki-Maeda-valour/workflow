"""#260: 本物のsubmodule記録と未初期化gitlinkの非再帰を固定する。"""
import base64
import hashlib
import os
import subprocess
import unittest

from diagnostic_test_support import DiagnosticFixture, inventory


class DiagnosticSubmodules(DiagnosticFixture):
    def real_submodule(self):
        source = self.work / 'module-source'
        self.git('clone', '-q', str(self.repo), str(source))
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(source), 'child')
        self.commit('add actual submodule')
        return self.repo / 'child'

    def source(self, context=0):
        rows = (self.state / 'snapshot/config-contexts.txt').read_bytes().splitlines()
        return next(row.split(b'\t')[12] for row in rows if row.startswith(('context\t%d\t'%context).encode()))

    def check_source(self, expected, context=0):
        self.begin()
        self.assertEqual(self.source(context), expected)
        result = self.observe('files', '--path', 'seed.txt')
        self.assertIn(b'seed.txt', self.records(result))

    def test_actual_initialized_submodule_file_source(self):
        self.real_submodule()
        self.check_source(b'file')
        common = self.common.copy(); common[-1] = '1'
        output = self.plan_dir / 'child.json'
        response = self.success(self.cli('plan', *common, '--context-start',
            '--carry-plan', self.latest, self.latest_hash, '--out', output), 'plan-created')
        self.common, self.latest, self.latest_hash = common, output, response['plan_sha256']
        self.assertIn(b'seed.txt', self.records(self.observe('files', '--path', 'seed.txt')))
        # 正当な..入口の受理が、gitfile差替えの採用にはならない。
        gitfile = self.repo / 'child/.git'
        gitfile.write_text('gitdir: ../.git/modules/unrelated\n')
        before = inventory(self.repo), inventory(self.state)
        self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'changed-entry.json', 'refs'), 22)
        self.assertEqual((inventory(self.repo), inventory(self.state)), before)

    def test_actual_deinitialized_submodule_file_source(self):
        self.real_submodule()
        self.git('submodule', 'deinit', '-f', '--', 'child')
        self.check_source(b'file')

    def test_actual_submodule_index_blob_fallback(self):
        self.real_submodule()
        (self.repo / '.gitmodules').unlink()
        self.check_source(b'blob')

    def test_actual_submodule_head_blob_fallback(self):
        self.real_submodule()
        self.git('rm', '--', '.gitmodules')
        # takeは使い捨てindexのtreeを作る。fixture側で同じtreeを先に作る。
        self.git('write-tree')
        self.check_source(b'blob')

    def test_actual_submodule_unmerged_modules_source(self):
        self.real_submodule()
        # root index未解決は既存takeが停止する。正常rootの子で形式を生成する。
        child = self.repo / 'child'
        (child / '.gitmodules').write_text('')
        self.git('-C', str(child), 'add', '--', '.gitmodules')
        oid = self.git('-C', str(child), 'rev-parse', ':.gitmodules').stdout.strip()
        payload = b'0 ' + b'0' * 40 + b'\t.gitmodules\n'
        payload += b''.join(b'100644 ' + oid + b' ' + str(stage).encode() + b'\t.gitmodules\n'
                            for stage in (1, 2, 3))
        subprocess.run([self.git_binary, '-C', str(child), 'update-index', '--index-info'],
                       input=payload, env=self.env, check=True, capture_output=True, timeout=30)
        self.check_source(b'unmerged', context=1)

    def test_direct_symlink_admin_entry_keeps_identity_checks(self):
        admin = self.work / 'repo-admin'
        (self.repo / '.git').rename(admin)
        (self.repo / '.git').symlink_to(admin, target_is_directory=True)
        self.begin()
        self.assertIn(b'seed.txt', self.records(self.observe('files', '--path', 'seed.txt')))
        (self.repo / '.git').unlink()
        (self.repo / '.git').symlink_to(self.work / 'unrelated', target_is_directory=True)
        self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'changed-link.json', 'refs'), 22)

    def test_non_producer_module_sources_are_rejected_with_valid_outer_hash(self):
        self.state, fields = self.take()
        target = self.state / 'snapshot/config-contexts.txt'
        original = target.read_bytes()
        for index, source in enumerate((b'worktree', b'index', b'head', b'held', b'unknown')):
            rows = original.splitlines()
            for n, row in enumerate(rows):
                if row.startswith(b'context\t0\t'):
                    columns = row.split(b'\t'); columns[12] = source
                    rows[n] = b'\t'.join(columns)
            target.write_bytes(b'\n'.join(rows)+b'\n')
            # 不正fixtureの外側hashだけを合わせ、schemaの拒否を直接確認する。
            snapshot = self.state / 'snapshot'
            aggregate = b''.join(hashlib.sha256(p.read_bytes()).hexdigest().encode()+b'  '+os.fsencode(p.name)+b'\n'
                                 for p in sorted(snapshot.iterdir()) if not p.name.startswith('.'))
            result = self.cli('plan', '--cwd', self.repo, '--state', self.state,
                '--manifest-sha256', fields['MANIFEST_SHA256'], '--snapshot-sha256', hashlib.sha256(aggregate).hexdigest(),
                '--context-id', '0', '--base-ref', self.base_oid, '--start-ref', self.start_oid,
                '--start-kind', 'new-invocation', '--out', self.plan_dir / ('unknown-%d'%index))
            self.failure(result, 22, 'state-schema')
        target.write_bytes(original)

    def gitlink(self):
        self.git('update-index', '--add', '--cacheinfo', '160000', self.start_oid, 'child')
        self.git('commit', '-qm', 'parent gitlink')
        self.begin()

    def assert_boundary(self, response):
        boundaries = {base64.b64decode(x['path_b64']): x['reason'] for x in response['data']['boundaries']}
        self.assertEqual(boundaries.get(b'child'), 'submodule')
        self.assertIn('nested-content-not-observed', response['warnings'])
        self.assertFalse(any(p.startswith(b'child/') for p in self.records(response)))
        self.assertNotIn(base64.b64encode(b'CHILD_PRIVATE_BODY'), str(response).encode())
        for record in response['data']['records']:
            if record.get('patch_b64') is not None:
                self.assertNotIn(b'CHILD_PRIVATE_BODY', base64.b64decode(record['patch_b64']))

    def test_index_gitlink_without_git_marker_is_boundary_for_every_worktree_scope(self):
        self.gitlink()
        for shape in ('absent', 'empty', 'populated'):
            if shape == 'empty':
                (self.repo / 'child').mkdir()
            elif shape == 'populated':
                (self.repo / 'child/local.txt').write_bytes(b'CHILD_PRIVATE_BODY')
            before = inventory(self.repo), inventory(self.state)
            for op, extra in (('status', []), ('files', ['--kind', 'all']), ('diff', ['--format', 'patch'])):
                for scope in (['--path', 'child'], ['--path', 'child/local.txt'],
                              ['--under', 'child'], ['--under', '.']):
                    with self.subTest(shape=shape, operation=op, scope=scope):
                        result = self.observe(op, *scope, *extra)
                        self.assert_boundary(result)
                        if op == 'status':
                            self.assertEqual(self.records(result)[b'child']['worktree_comparison'], 'not-observed')
            self.assertEqual((inventory(self.repo), inventory(self.state)), before)
        # 兄弟の通常fileは引き続き本文比較できる。
        (self.repo / 'seed.txt').write_bytes(b'public changed\n')
        result = self.observe('diff', '--path', 'seed.txt', '--format', 'patch')
        self.assertIn(b'public changed', base64.b64decode(self.records(result)[b'seed.txt']['patch_b64']))
        # object間比較は子内部を観測せず、親gitlink追加をOIDで示す。
        result = self.observe('diff', '--left', self.base_oid, '--right', 'HEAD', '--path', 'child')
        self.assertEqual(self.records(result)[b'child']['right']['kind'], 'gitlink')
        self.assertEqual(self.records(result)[b'child']['right']['oid'], self.start_oid)
        self.assertEqual(result['data']['boundaries'], [])

    def test_unmerged_gitlink_boundary_uses_commit_stage_not_blob_stage(self):
        self.gitlink()
        (self.repo / 'child').mkdir()
        (self.repo / 'child/local.txt').write_bytes(b'CHILD_PRIVATE_BODY')
        blob = self.git('rev-parse', 'HEAD:seed.txt').stdout.strip()
        payload = b'0 ' + b'0' * 40 + b'\tchild\n'
        payload += b'100644 ' + blob + b' 1\tchild\n'
        payload += b''.join(b'160000 ' + self.start_oid.encode() + b' ' + str(n).encode() + b'\tchild\n'
                            for n in (2, 3))
        subprocess.run([self.git_binary, '-C', str(self.repo), 'update-index', '--index-info'],
                       input=payload, env=self.env, check=True, capture_output=True, timeout=30)
        result = self.observe('status', '--path', 'child/local.txt')
        self.assert_boundary(result)
        record = self.records(result)[b'child']
        self.assertIsNone(record['index'])
        self.assertEqual(record['staged_comparison'], 'unmerged')
        self.assertEqual(record['worktree_comparison'], 'not-observed')
        self.assertEqual(record['worktree']['oid'], self.start_oid)

    def test_excluded_gitlink_boundary_never_publishes_name(self):
        self.profile(['child'])
        self.gitlink()
        (self.repo / 'child').mkdir()
        (self.repo / 'child/local.txt').write_bytes(b'CHILD_PRIVATE_BODY')
        for op, extra in (('status', []), ('files', ['--kind', 'all']), ('diff', ['--format', 'patch'])):
            for scope in (['--path', 'child'], ['--path', 'child/local.txt'],
                          ['--under', 'child'], ['--under', '.']):
                with self.subTest(operation=op, scope=scope):
                    result = self.observe(op, *scope, *extra)
                    self.assertEqual(result['data']['boundaries'], [])
                    self.assertFalse(any(p == b'child' or p.startswith(b'child/') for p in self.records(result)))
                    self.assertGreater(result['data']['excluded_count'], 0)
                    if scope[1] != '.':
                        self.assertTrue(result['scope'][0]['redacted'])
        result = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertIn(b'seed.txt', self.records(result))


if __name__ == '__main__':
    unittest.main()
