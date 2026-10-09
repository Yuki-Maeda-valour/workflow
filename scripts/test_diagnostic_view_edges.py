"""#260: 公開応答の入れ子型、正常な表示境界と実reftableの回帰。"""
import base64
import hashlib
import json
import socket
import stat
import unittest

from diagnostic_test_support import DiagnosticFixture, inventory


def decoded(value):
    return base64.b64decode(value, validate=True)


class DiagnosticViewEdges(DiagnosticFixture):
    def endpoint_schema(self, value, *, reads_body=True):
        self.assertIs(type(value), dict)
        self.assertEqual(set(value), {'kind', 'git_mode', 'os_mode', 'size', 'sha256', 'oid'})
        self.assertIn(value['kind'], {'absent', 'regular', 'symlink', 'gitlink', 'directory',
                                      'fifo', 'socket', 'device'})
        if value['git_mode'] is not None:
            self.assertIs(type(value['git_mode']), str)
            self.assertIn(value['git_mode'], {'100644', '100755', '120000', '160000'})
        for key in ('os_mode', 'size'):
            if value[key] is not None:
                self.assertIs(type(value[key]), int)
                self.assertGreaterEqual(value[key], 0)
        for key, width in (('sha256', 64), ('oid', 40)):
            if value[key] is not None:
                self.assertIs(type(value[key]), str)
                self.assertRegex(value[key], '^[0-9a-f]{%d}$' % width)
        if not reads_body:
            self.assertIsNone(value['sha256'])
        if value['kind'] == 'absent':
            self.assertEqual(value, dict(kind='absent', git_mode=None, os_mode=None,
                                         size=0, sha256=None, oid=None))

    def stages_schema(self, record):
        self.assertIs(type(record['index_flags']), list)
        self.assertLessEqual(set(record['index_flags']), {'skip-worktree', 'assume-unchanged'})
        self.assertIs(type(record['stages']), list)
        for stage in record['stages']:
            self.assertEqual(set(stage), {'stage', 'mode', 'oid'})
            self.assertIs(type(stage['stage']), int)
            self.assertIn(stage['stage'], (0, 1, 2, 3))
            self.assertRegex(stage['mode'], '^[0-7]{6}$')
            self.assertRegex(stage['oid'], '^[0-9a-f]{40}$')

    def common_data(self, data, variant, fields):
        self.assertEqual(set(data), {'variant', *fields})
        self.assertEqual(data['variant'], variant)
        if 'excluded_count' in fields:
            self.assertIs(type(data['excluded_count']), int)
            self.assertGreaterEqual(data['excluded_count'], 0)
        if 'records' in fields:
            self.assertIs(type(data['records']), list)
        if 'boundaries' in fields:
            self.assertIs(type(data['boundaries']), list)
            for entry in data['boundaries']:
                self.assertEqual(set(entry), {'path_b64', 'reason'})
                self.assertTrue(decoded(entry['path_b64']))
                self.assertIn(entry['reason'], ('nested-repository', 'submodule', 'special-file'))

    def test_all_seven_data_variants_have_exact_nested_schema_and_values(self):
        # 非空のstash・symref・通常stageを用意し、空配列だけの型検査にしない。
        (self.repo / 'seed.txt').write_bytes(b'stashed content\n')
        self.git('stash', 'push', '-qm', 'schema stash')
        self.git('symbolic-ref', 'refs/heads/alias', 'refs/heads/main')
        self.begin()
        (self.repo / 'seed.txt').write_bytes(b'changed content\n')
        before = inventory(self.repo)
        object_oid = self.git('rev-parse', 'HEAD:seed.txt').stdout.decode().strip()
        expected_object = dict(kind='regular', git_mode='100644', os_mode=None, size=12,
                               sha256=hashlib.sha256(b'public seed\n').hexdigest(), oid=object_oid)
        status = self.observe('status', '--path', 'seed.txt')['data']
        self.common_data(status, 'status-v1', ('records', 'excluded_count', 'boundaries'))
        self.assertEqual(len(status['records']), 1)
        record = status['records'][0]
        self.assertEqual(set(record), {'path_b64', 'head', 'index', 'worktree', 'index_flags',
                                      'stages', 'staged_comparison', 'worktree_comparison'})
        self.assertEqual(decoded(record['path_b64']), b'seed.txt')
        for endpoint in ('head', 'index', 'worktree'):
            self.endpoint_schema(record[endpoint])
        self.assertEqual(record['head'], expected_object)
        self.assertEqual(record['index'], expected_object)
        self.assertEqual(record['worktree']['size'], len(b'changed content\n'))
        self.assertEqual(record['worktree']['sha256'], hashlib.sha256(b'changed content\n').hexdigest())
        self.assertIsNone(record['worktree']['oid'])
        self.assertEqual(record['worktree']['os_mode'], stat.S_IMODE((self.repo / 'seed.txt').stat().st_mode))
        self.stages_schema(record)
        self.assertEqual(record['stages'], [{'stage': 0, 'mode': '100644', 'oid': object_oid}])
        self.assertEqual(record['index_flags'], [])
        self.assertEqual((record['staged_comparison'], record['worktree_comparison']), ('same', 'modified'))

        files = self.observe('files', '--path', 'seed.txt')['data']
        self.common_data(files, 'files-v1', ('records', 'excluded_count', 'boundaries'))
        record = files['records'][0]
        self.assertEqual(set(record), {'path_b64', 'tracked', 'endpoint', 'index_flags', 'stages'})
        self.assertIs(record['tracked'], True)
        self.endpoint_schema(record['endpoint'], reads_body=False)
        self.stages_schema(record)
        self.assertEqual(decoded(record['path_b64']), b'seed.txt')
        self.assertEqual(record['endpoint']['size'], len(b'changed content\n'))

        diff = self.observe('diff', '--path', 'seed.txt')['data']
        self.common_data(diff, 'diff-v1', ('left', 'right', 'records', 'excluded_count', 'boundaries'))
        self.assertEqual((diff['left'], diff['right']), ('index', 'worktree'))
        self.assertEqual(len(diff['records']), 1)
        record = diff['records'][0]
        self.assertEqual(set(record), {'path_b64', 'left', 'right', 'comparison', 'patch_b64'})
        self.assertEqual(decoded(record['path_b64']), b'seed.txt')
        self.endpoint_schema(record['left']); self.endpoint_schema(record['right'])
        self.assertEqual(record['left'], expected_object)
        self.assertEqual(record['comparison'], 'modified')
        self.assertIsNone(record['patch_b64'])

        blob = self.observe('show', '--path', 'seed.txt', '--format', 'blob')['data']
        self.common_data(blob, 'blob-v1', ('path_b64', 'endpoint', 'bytes_b64', 'excluded_count'))
        self.assertEqual(decoded(blob['path_b64']), b'seed.txt')
        self.assertEqual(decoded(blob['bytes_b64']), b'public seed\n')
        self.endpoint_schema(blob['endpoint']); self.assertEqual(blob['endpoint'], expected_object)
        history = self.observe('log')['data']
        self.common_data(history, 'history-v1', ('format', 'bytes_b64', 'limit', 'unborn', 'excluded_count'))
        self.assertEqual(history['format'], 'oneline'); self.assertIs(type(history['limit']), int)
        self.assertEqual(history['limit'], 20); self.assertIs(history['unborn'], False)
        self.assertIn(self.start_oid.encode(), decoded(history['bytes_b64']))
        refs = self.observe('refs')['data']
        self.common_data(refs, 'refs-v1', ('records',))
        names = {}
        for record in refs['records']:
            self.assertEqual(set(record), {'name_b64', 'oid', 'symbolic_target_b64', 'resolution'})
            name = decoded(record['name_b64']); names[name] = record
            self.assertRegex(record['oid'], '^[0-9a-f]{40}$')
            self.assertEqual(record['resolution'], 'resolved')
            if record['symbolic_target_b64'] is not None:
                self.assertTrue(decoded(record['symbolic_target_b64']).startswith(b'refs/'))
        self.assertEqual(decoded(names[b'refs/heads/alias']['symbolic_target_b64']), b'refs/heads/main')
        self.assertIsNone(names[b'refs/heads/main']['symbolic_target_b64'])
        self.assertEqual(names[b'refs/heads/alias']['oid'], self.start_oid)
        stashes = self.observe('stashes')['data']
        self.common_data(stashes, 'stashes-v1', ('bytes_b64', 'limit'))
        self.assertIs(type(stashes['limit']), int); self.assertEqual(stashes['limit'], 20)
        self.assertIn(b'schema stash', decoded(stashes['bytes_b64']))
        self.assertEqual(inventory(self.repo), before)

    def test_empty_binary_and_type_changes_keep_records_as_patch_authority(self):
        (self.repo / 'empty-delete').write_bytes(b'')
        (self.repo / 'binary').write_bytes(b'before\0bytes\xff')
        (self.repo / 'type').write_bytes(b'normal bytes\n')
        self.commit('edge source')
        self.begin()
        (self.repo / 'empty-add').write_bytes(b'')
        (self.repo / 'empty-delete').unlink()
        (self.repo / 'binary').write_bytes(b'after\0bytes\xfe')
        (self.repo / 'type').unlink(); (self.repo / 'type').symlink_to('missing-target')
        before = inventory(self.repo)
        args = [part for name in ('empty-add', 'empty-delete', 'binary', 'type') for part in ('--path', name)]
        result = self.observe('diff', *args, '--format', 'patch')
        records = self.records(result)
        self.assertEqual(set(records), {b'empty-add', b'empty-delete', b'binary', b'type'})
        for record in records.values():
            self.endpoint_schema(record['left']); self.endpoint_schema(record['right'])
            self.assertIs(type(record['patch_b64']), str)
            decoded(record['patch_b64'])
        for name, comparison, side in ((b'empty-add', 'added', 'right'), (b'empty-delete', 'deleted', 'left')):
            record = records[name]
            self.assertEqual(record['comparison'], comparison)
            self.assertEqual(record[side]['size'], 0)
            self.assertEqual(record[side]['sha256'], hashlib.sha256(b'').hexdigest())
            self.assertEqual(decoded(record['patch_b64']), b'')
        self.assertEqual(records[b'binary']['comparison'], 'modified')
        self.assertIn(b'Binary files ', decoded(records[b'binary']['patch_b64']))
        self.assertEqual(records[b'binary']['right']['sha256'], hashlib.sha256(b'after\0bytes\xfe').hexdigest())
        record = records[b'type']
        self.assertEqual(record['comparison'], 'type-changed')
        self.assertEqual((record['left']['kind'], record['right']['kind']), ('regular', 'symlink'))
        self.assertEqual(record['right']['git_mode'], '120000')
        self.assertEqual(record['right']['sha256'], hashlib.sha256(b'missing-target').hexdigest())
        self.assertIn(b'+missing-target', decoded(record['patch_b64']))
        self.assertEqual(inventory(self.repo), before)

    def test_patch_context_zero_and_hundred_are_valid_normal_boundaries(self):
        old = b'first\nsecond\nthird\nfourth\nfifth\nsixth\nseventh\n'
        (self.repo / 'seed.txt').write_bytes(old)
        self.commit('context source'); self.begin()
        (self.repo / 'seed.txt').write_bytes(old.replace(b'fourth', b'changed'))
        for context in (0, 100):
            with self.subTest(context=context):
                data = self.observe('diff', '--path', 'seed.txt', '--format', 'patch', '--context', str(context))
                patch = decoded(self.records(data)[b'seed.txt']['patch_b64'])
                self.assertIn(b'-fourth\n+changed\n', patch)
                if context == 0:
                    self.assertIn(b'@@ -4 +4 @@', patch)
                    self.assertNotIn(b' first\n', patch)
                else:
                    self.assertIn(b'@@ -1,7 +1,7 @@', patch)
                    self.assertIn(b' first\n', patch)
                    self.assertIn(b' seventh\n', patch)

    def test_fuller_and_limit_thousand_return_fixed_history_and_stash_forms(self):
        (self.repo / 'seed.txt').write_text('second\n'); second = self.commit('second message')
        (self.repo / 'seed.txt').write_text('stash bytes\n'); self.git('stash', 'push', '-qm', 'limit stash')
        self.begin()
        result = self.observe('log', '--format', 'fuller', '--limit', '1000')['data']
        self.assertEqual(result['format'], 'fuller'); self.assertEqual(result['limit'], 1000)
        raw = decoded(result['bytes_b64'])
        self.assertEqual(raw.count(b'commit '), 2)
        for value in (b'commit ' + second.encode(), b'Author:', b'AuthorDate:', b'Commit:', b'CommitDate:', b'second message'):
            self.assertIn(value, raw)
        self.assertNotIn(b'diff --git', raw); self.assertNotIn(b'\x1b', raw)
        result = self.observe('stashes', '--limit', '1000')['data']
        self.assertEqual(result['limit'], 1000)
        self.assertIn(b'limit stash', decoded(result['bytes_b64']))

    def test_linked_reftable_keeps_selected_private_refs_and_common_shared_refs(self):
        version = self.git('--version').stdout.decode().strip()
        if tuple(int(x) for x in version.split()[2].split('.')[:2]) < (2, 45):
            self.skipTest('実行Gitはreftable未対応: ' + version)
        self.git('refs', 'migrate', '--ref-format=reftable')
        self.assertEqual(self.git('rev-parse', '--show-ref-format').stdout.strip(), b'reftable')
        main = self.repo
        main_oid = self.start_oid
        self.git('update-ref', 'refs/worktree/choice', main_oid)
        self.git('update-ref', 'refs/worktree/main-only', main_oid)
        self.git('update-ref', 'refs/heads/shared', main_oid)
        linked = self.work / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'selected', str(linked))
        self.repo = linked
        (linked / 'seed.txt').write_bytes(b'selected worktree\n')
        selected_oid = self.commit('selected commit')
        self.git('update-ref', 'refs/worktree/choice', selected_oid)
        self.git('symbolic-ref', 'refs/worktree/alias', 'refs/worktree/choice')
        (linked / 'task').mkdir(); (linked / 'task/t.md').write_text('# fixture\n')
        self.start_oid = self.base_oid = selected_oid
        self.begin()
        before_main, before_linked = inventory(main), inventory(linked)
        result = self.observe('refs')
        refs = {decoded(r['name_b64']): r for r in result['data']['records']}
        self.assertEqual(refs[b'refs/worktree/choice']['oid'], selected_oid)
        self.assertEqual(refs[b'refs/worktree/alias']['oid'], selected_oid)
        self.assertEqual(decoded(refs[b'refs/worktree/alias']['symbolic_target_b64']), b'refs/worktree/choice')
        self.assertNotIn(b'refs/worktree/main-only', refs)
        self.assertEqual(refs[b'refs/heads/shared']['oid'], main_oid)
        self.assertEqual(refs[b'refs/heads/main']['oid'], main_oid)
        self.assertEqual(refs[b'refs/heads/selected']['oid'], selected_oid)
        self.assertEqual(inventory(main), before_main)
        self.assertEqual(inventory(linked), before_linked)

    def test_socket_metadata_is_normal_but_content_comparison_is_rejected(self):
        self.begin()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(self.repo / 'local.sock'))
        result = self.observe('files', '--path', 'local.sock', '--kind', 'all')
        record = self.records(result)[b'local.sock']
        self.endpoint_schema(record['endpoint'], reads_body=False)
        self.assertEqual(record['endpoint']['kind'], 'socket')
        self.assertIsNone(record['endpoint']['git_mode']); self.assertIsNone(record['endpoint']['oid'])
        self.assertIs(record['tracked'], False)
        self.assertEqual(result['data']['boundaries'], [{'path_b64': base64.b64encode(b'local.sock').decode(), 'reason': 'special-file'}])
        for operation in ('status', 'diff'):
            with self.subTest(operation=operation):
                self.extend(operation, '--path', 'local.sock')
                self.failure(self.cli('run', *self.common, '--plan', self.latest,
                                      '--plan-sha256', self.latest_hash), 23)
        normal = self.observe('files', '--path', 'seed.txt')
        self.assertTrue(self.records(normal)[b'seed.txt']['tracked'])
        self.assertTrue(stat.S_ISSOCK((self.repo / 'local.sock').lstat().st_mode))

    def test_secret_nested_boundary_does_not_publish_name_or_contents(self):
        child = self.repo / '.env.private'; child.mkdir()
        self.git('-C', str(child), 'init', '-q', '-b', 'main')
        (child / 'inside.txt').write_bytes(b'HIDDEN_NESTED_BODY')
        self.git('-C', str(child), 'add', 'inside.txt')
        self.git('-C', str(child), 'commit', '-qm', 'nested initial')
        self.begin()
        before = inventory(child)
        for operation in ('files', 'status'):
            with self.subTest(operation=operation):
                extra = ['--kind', 'all'] if operation == 'files' else []
                result = self.observe(operation, '--path', '.env.private/inside.txt', '--path', 'seed.txt', *extra)
                self.assertEqual(set(self.records(result)), {b'seed.txt'})
                self.assertEqual(result['data']['boundaries'], [])
                self.assertGreaterEqual(result['data']['excluded_count'], 1)
                self.assertIn('paths-excluded', result['warnings'])
                self.assertEqual(result['scope'][0], {'kind': 'path', 'path_b64': None, 'redacted': True})
                raw = json.dumps(result).encode()
                for value in (b'.env.private', b'inside.txt', b'HIDDEN_NESTED_BODY', base64.b64encode(b'.env.private'),
                              base64.b64encode(b'.env.private/inside.txt'), base64.b64encode(b'HIDDEN_NESTED_BODY')):
                    self.assertNotIn(value, raw)
        self.assertEqual(inventory(child), before)


if __name__ == '__main__':
    unittest.main()
