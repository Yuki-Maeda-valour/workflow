"""#260: 公開CLIの正常操作・機密除外・保持値・生データの回帰。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import unittest

from diagnostic_test_support import DiagnosticFixture, inventory, strict_json


class DiagnosticMatrix(DiagnosticFixture):
    def test_all_seven_operations_preserve_sources_and_return_fixed_variants(self):
        self.begin()
        before_repo, before_state = inventory(self.repo), inventory(self.state)
        operations = [('status', ['--path', 'seed.txt'], 'status-v1'),
                      ('files', ['--path', 'seed.txt'], 'files-v1'),
                      ('diff', ['--path', 'seed.txt'], 'diff-v1'),
                      ('show', ['--path', 'seed.txt'], 'diff-v1'),
                      ('log', [], 'history-v1'), ('refs', [], 'refs-v1'),
                      ('stashes', [], 'stashes-v1')]
        for op, args, variant in operations:
            with self.subTest(operation=op):
                result = self.observe(op, *args)
                self.assertEqual(result['data']['variant'], variant)
        self.assertEqual(inventory(self.repo), before_repo)
        self.assertEqual(inventory(self.state), before_state)

    def test_initial_plan_cannot_run(self):
        self.begin()
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                              '--plan-sha256', self.latest_hash), 22)

    def test_wrong_held_hash_is_not_recovered_from_state(self):
        self.begin()
        args = self.common.copy()
        args[args.index('--manifest-sha256') + 1] = '0' * 64
        self.failure(self.cli('run', *args, '--plan', self.latest,
                              '--plan-sha256', self.latest_hash), 22)

    def test_wrong_plan_hash_and_changed_state_fail_before_output(self):
        self.begin()
        self.extend('refs')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                              '--plan-sha256', '0' * 64), 22)
        with (self.state / 'snapshot/4-meta.txt').open('ab') as stream:
            stream.write(b'changed\n')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                              '--plan-sha256', self.latest_hash), 22)

    def test_existing_out_is_preserved(self):
        self.begin()
        before = self.latest.read_bytes()
        self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, '--out', self.latest, 'refs'), 2)
        self.assertEqual(self.latest.read_bytes(), before)

    def test_unknown_duplicate_and_missing_cli_arguments(self):
        for args in [[], ['unknown'], ['plan', '--bogus', '1'],
                     ['plan', '--cwd', 'a', '--cwd', 'b'], ['run', '--cwd']]:
            with self.subTest(args=args):
                self.failure(self.cli(*args), 2)

    def test_operation_rejects_missing_scope_and_arbitrary_options(self):
        self.begin()
        cases = [('status',), ('files',), ('diff',), ('show',),
                 ('refs', '--limit', '2'), ('log', '--format', '%s'),
                 ('log', '--limit', '0'), ('log', '--limit', '1001'),
                 ('stashes', '--limit', '0'), ('status', '--path', 'seed.txt', '--exec', 'true'),
                 ('diff', '--path', 'seed.txt', '--context', '101'),
                 ('diff', '--path', 'seed.txt', '--left', 'index', '--right', 'index')]
        for n, args in enumerate(cases):
            with self.subTest(args=args):
                self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', self.plan_dir / ('bad%d' % n), *args), 2)

    def test_revision_operators_rejected_but_full_oid_supported(self):
        self.begin()
        for n, rev in enumerate(['HEAD~1', 'HEAD:seed.txt', '-HEAD', 'HEAD..HEAD', 'main']):
            with self.subTest(rev=rev):
                self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', self.plan_dir / ('rev%d' % n),
                    'show', '--rev', rev, '--path', 'seed.txt'), 2)
        result = self.observe('show', '--rev', self.start_oid, '--path', 'seed.txt', '--format', 'blob')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'public seed\n')

    def test_literal_glob_magic_dash_and_non_utf8_paths(self):
        names = [b'*.txt', b':(glob)*', b'-leading', b'bad-\xff', '日本語.txt'.encode()]
        for name in names:
            path = os.fsencode(self.repo) + b'/' + name
            with open(path, 'wb') as out:
                out.write(b'raw-' + name)
        self.commit()
        self.begin()
        for name in names:
            with self.subTest(path=name):
                result = self.observe('files', '--path', os.fsdecode(name))
                self.assertEqual(set(self.records(result)), {name})
                self.assertIsNone(self.records(result)[name]['endpoint']['sha256'])

    def test_illegal_paths_are_rejected(self):
        self.begin()
        for n, path in enumerate(['', '/absolute', 'a/../b', './seed.txt', 'a//b', 'a/.']):
            with self.subTest(path=path):
                self.failure(self.cli('extend-plan', *self.common, '--plan', self.latest,
                    '--plan-sha256', self.latest_hash, '--out', self.plan_dir / ('path%d' % n),
                    'files', '--path', path), 2)

    def test_changed_config_is_data_not_execution_configuration(self):
        self.begin()
        marker = self.work / 'should-not-run'
        with (self.repo / '.git/config').open('a') as out:
            out.write('\n[core]\n fsmonitor = touch ' + str(marker) + '\n')
            out.write('[diff]\n external = touch ' + str(marker) + '\n')
        (self.repo / 'seed.txt').write_bytes(b'changed raw\n')
        before = inventory(self.repo)
        result = self.observe('diff', '--path', 'seed.txt', '--format', 'patch')
        self.assertIn(b'changed raw', base64.b64decode(self.records(result)[b'seed.txt']['patch_b64']))
        self.assertFalse(marker.exists())
        self.assertEqual(inventory(self.repo), before)

    def test_current_profile_reduction_keeps_previous_exclusions(self):
        self.profile(['private.txt'])
        (self.repo / 'private.txt').write_bytes(b'SECRET_BODY_260')
        self.begin()
        self.profile(['later.txt'])
        (self.repo / 'later.txt').write_bytes(b'NEW_SECRET_BODY_260')
        first = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertNotIn(b'private.txt', self.records(first))
        self.assertNotIn(b'later.txt', self.records(first))
        self.profile([])
        second = self.observe('status', '--under', '.')
        self.assertNotIn(b'private.txt', self.records(second))
        self.assertNotIn(b'later.txt', self.records(second))
        self.assertIn('paths-excluded', second['warnings'])
        serialized = json.dumps(second).encode()
        self.assertNotIn(b'SECRET_BODY_260', serialized)

    def test_base_and_start_and_branch_only_exclusions_are_unioned(self):
        self.profile(['base-only'])
        base = self.commit('base profile')
        self.profile(['start-only'])
        start = self.commit('start profile')
        self.profile(['branch-only'])
        branch = self.commit('branch profile')
        self.profile(['current-only'])
        for name in ['base-only', 'start-only', 'branch-only', 'current-only', '.env', '.env.prod', '.dev.vars']:
            (self.repo / name).write_text('hidden')
        self.begin('--branch-ref', branch, base=base, start=start)
        result = self.observe('files', '--under', '.', '--kind', 'all')
        names = set(self.records(result))
        for name in ['base-only', 'start-only', 'branch-only', 'current-only', '.env', '.env.prod', '.dev.vars']:
            self.assertNotIn(name.encode(), names)
        self.assertIn(b'seed.txt', names)

    def test_profile_change_after_extend_requires_preparation_again(self):
        self.begin()
        self.extend('files', '--path', 'seed.txt')
        self.profile(['seed.txt'])
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash), 22, 'preparation-stale')
        result = self.observe('files', '--path', 'seed.txt')
        self.assertEqual(result['data']['records'], [])
        self.assertTrue(result['scope'][0]['redacted'])
        self.assertIsNone(result['scope'][0]['path_b64'])

    def test_invalid_current_profile_does_not_succeed_with_old_policy(self):
        self.begin()
        self.profile(['safe'])
        (self.repo / '.claude/project-profile.yml').write_bytes(b'secret_paths: [\xff]\n')
        result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                         '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'invalid', 'refs')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'\xff', result.stderr)

    def test_null_secret_paths_is_not_a_valid_empty_list(self):
        self.begin()
        self.profile([]).write_text('secret_paths: null\n')
        result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                         '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'null', 'refs')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')

    def test_all_secret_log_scope_is_empty_and_blob_selection_stops(self):
        self.profile(['seed.txt'])
        self.begin()
        result = self.observe('log', '--path', 'seed.txt')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'')
        self.assertGreater(result['data']['excluded_count'], 0)
        self.extend('show', '--path', 'seed.txt', '--format', 'blob')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                             '--plan-sha256', self.latest_hash), 23, 'excluded-selection')

    def test_raw_bytes_do_not_apply_attributes_or_line_endings(self):
        (self.repo / '.gitattributes').write_text('seed.txt text eol=lf\n')
        self.begin()
        content = b'public seed\r\n'
        (self.repo / 'seed.txt').write_bytes(content)
        result = self.observe('status', '--path', 'seed.txt')
        record = self.records(result)[b'seed.txt']
        self.assertEqual(record['worktree']['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(record['worktree_comparison'], 'modified')

    def test_ignore_is_not_applied_and_untracked_names_do_not_hash(self):
        (self.repo / '.gitignore').write_text('untracked.bin\n')
        (self.repo / 'untracked.bin').write_bytes(b'not hashed')
        self.begin()
        result = self.observe('status', '--under', '.', '--untracked', 'names')
        record = self.records(result)[b'untracked.bin']
        self.assertIsNone(record['worktree']['sha256'])
        self.assertEqual(record['worktree_comparison'], 'untracked')
        self.assertIn('ignore-not-applied', result['warnings'])

    def test_huge_untracked_file_metadata_is_allowed(self):
        self.begin()
        with (self.repo / 'huge.bin').open('wb') as out:
            out.truncate(2 * 1024**3)
        result = self.observe('files', '--path', 'huge.bin', '--kind', 'all')
        endpoint = self.records(result)[b'huge.bin']['endpoint']
        self.assertEqual(endpoint['size'], 2 * 1024**3)
        self.assertIsNone(endpoint['sha256'])
        self.assertEqual((self.repo / 'huge.bin').stat().st_size, 2 * 1024**3)

    def test_worktree_count_is_not_metadata_2000_limit(self):
        self.begin()
        folder = self.repo / 'many'
        folder.mkdir()
        for i in range(2001):
            (folder / str(i)).touch()
        result = self.observe('files', '--under', 'many', '--kind', 'all')
        self.assertEqual(len(result['data']['records']), 2001)

    def test_symlink_bytes_and_parent_boundary(self):
        (self.repo / 'link').symlink_to('missing-target')
        (self.repo / 'outside').symlink_to(self.work)
        self.begin()
        result = self.observe('files', '--path', 'link', '--kind', 'all')
        self.assertEqual(self.records(result)[b'link']['endpoint']['kind'], 'symlink')
        self.extend('diff', '--path', 'outside/secret')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                             '--plan-sha256', self.latest_hash), 23, 'symlink-parent')

    def test_fifo_metadata_allowed_but_comparison_stops(self):
        self.begin()
        os.mkfifo(self.repo / 'pipe')
        result = self.observe('files', '--path', 'pipe', '--kind', 'all')
        self.assertEqual(self.records(result)[b'pipe']['endpoint']['kind'], 'fifo')
        self.extend('diff', '--path', 'pipe')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                             '--plan-sha256', self.latest_hash), 23)

    def test_directory_requires_under_for_comparison(self):
        (self.repo / 'folder').mkdir()
        (self.repo / 'folder/file').write_text('value')
        self.begin()
        result = self.observe('files', '--path', 'folder', '--kind', 'all')
        self.assertEqual(self.records(result)[b'folder']['endpoint']['kind'], 'directory')
        self.extend('diff', '--path', 'folder')
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
                             '--plan-sha256', self.latest_hash), 23, 'directory-selection')
        result = self.observe('files', '--under', 'folder', '--kind', 'all')
        self.assertEqual(set(self.records(result)), {b'folder/file'})

    def test_absent_file_endpoint_and_normal_mode_change(self):
        self.begin()
        (self.repo / 'seed.txt').chmod(0o755)
        result = self.observe('diff', '--path', 'seed.txt')
        record = self.records(result)[b'seed.txt']
        self.assertEqual(record['right']['git_mode'], '100755')
        self.assertEqual(record['right']['os_mode'], 0o755)
        self.assertIsNone(record['patch_b64'])
        (self.repo / 'seed.txt').unlink()
        result = self.observe('files', '--path', 'seed.txt')
        self.assertEqual(self.records(result)[b'seed.txt']['endpoint'],
                         {'kind': 'absent', 'git_mode': None, 'os_mode': None,
                          'size': 0, 'sha256': None, 'oid': None})

    def test_skip_worktree_absence_and_materialized_changes(self):
        self.git('update-index', '--skip-worktree', 'seed.txt')
        (self.repo / 'seed.txt').unlink()
        self.begin()
        for op, args in [('status', []), ('diff', ['--format', 'patch']),
                         ('diff', ['--left', 'HEAD', '--right', 'worktree'])]:
            with self.subTest(op=op, args=args):
                result = self.observe(op, '--path', 'seed.txt', *args)
                record = self.records(result)[b'seed.txt']
                comparison = record['worktree_comparison'] if op == 'status' else record['comparison']
                self.assertEqual(comparison, 'not-materialized')
                self.assertIn('sparse-not-materialized', result['warnings'])
                if op == 'diff':
                    self.assertIsNone(record['patch_b64'])
        (self.repo / 'seed.txt').write_bytes(b'materialized change\n')
        result = self.observe('diff', '--path', 'seed.txt')
        self.assertEqual(self.records(result)[b'seed.txt']['comparison'], 'modified')
        self.assertNotIn('sparse-not-materialized', result['warnings'])
        result = self.observe('show', '--path', 'seed.txt', '--format', 'blob')
        self.assertEqual(base64.b64decode(result['data']['bytes_b64']), b'public seed\n')
        self.assertNotIn('sparse-not-materialized', result['warnings'])

    def test_assume_unchanged_is_not_content_equality(self):
        self.git('update-index', '--assume-unchanged', 'seed.txt')
        self.begin()
        (self.repo / 'seed.txt').write_text('changed\n')
        result = self.observe('status', '--path', 'seed.txt')
        record = self.records(result)[b'seed.txt']
        self.assertIn('assume-unchanged', record['index_flags'])
        self.assertEqual(record['worktree_comparison'], 'modified')

    def test_dangling_symbolic_ref_is_not_lost(self):
        self.git('symbolic-ref', 'refs/heads/dangling', 'refs/heads/missing')
        self.begin()
        result = self.observe('refs')
        refs = {base64.b64decode(r['name_b64']): r for r in result['data']['records']}
        self.assertIn(b'refs/heads/dangling', refs)
        self.assertEqual(refs[b'refs/heads/dangling'],
                         {'name_b64': base64.b64encode(b'refs/heads/dangling').decode(),
                          'oid': None, 'symbolic_target_b64': base64.b64encode(b'refs/heads/missing').decode(),
                          'resolution': 'unresolved'})

    def test_long_valid_symbolic_chain_is_not_classified_as_cycle(self):
        self.begin()
        for n in reversed(range(20)):
            target = 'refs/heads/main' if n == 19 else 'refs/heads/chain-%02d' % (n+1)
            self.git('symbolic-ref', 'refs/heads/chain-%02d' % n, target)
        result = self.observe('refs')
        refs = {base64.b64decode(r['name_b64']): r for r in result['data']['records']}
        self.assertEqual(refs[b'refs/heads/chain-00']['oid'], self.start_oid)
        self.assertEqual(refs[b'refs/heads/chain-00']['resolution'], 'resolved')

    def test_symbolic_cycle_is_a_failure_not_an_empty_ref_list(self):
        self.begin()
        self.git('symbolic-ref', 'refs/heads/cycle-a', 'refs/heads/cycle-b')
        self.git('symbolic-ref', 'refs/heads/cycle-b', 'refs/heads/cycle-a')
        result = self.cli('extend-plan', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'cyclic.json', 'refs')
        if result.returncode == 0:
            response = self.success(result, 'plan-extended')
            result = self.cli('run', *self.common, '--plan', self.plan_dir / 'cyclic.json',
                              '--plan-sha256', response['plan_sha256'])
        self.failure(result, 23)

    def test_stash_requires_history_and_preserves_existing_stash(self):
        (self.repo / 'seed.txt').write_text('stash data\n')
        self.git('stash', 'push', '-qm', 'normal stash')
        self.begin()
        result = self.observe('stashes', '--limit', '1')
        self.assertIn(b'normal stash', base64.b64decode(result['data']['bytes_b64']))
        (self.repo / '.git/logs/refs/stash').unlink()
        result = self.cli('extend-plan', *self.common, '--plan', self.latest,
                         '--plan-sha256', self.latest_hash, '--out', self.plan_dir / 'no-log', 'stashes')
        if result.returncode == 0:
            response = self.success(result, 'plan-extended')
            result = self.cli('run', *self.common, '--plan', self.plan_dir / 'no-log',
                              '--plan-sha256', response['plan_sha256'])
        self.failure(result, 23, 'incomplete-metadata')

    def test_full_json_plan_schema_rejects_unknown_and_duplicate_keys(self):
        self.begin()
        raw = self.latest.read_bytes()
        corruptions = [raw.rstrip()[:-1] + b',"unknown":0}',
                       raw.rstrip()[:-1] + b',"version":1}']
        for n, content in enumerate(corruptions):
            path = self.plan_dir / ('corrupt%d.json' % n)
            path.write_bytes(content)
            path.chmod(0o600)
            self.failure(self.cli('run', *self.common, '--plan', path,
                '--plan-sha256', hashlib.sha256(content).hexdigest()), 2)

    def test_source_environment_odb_override_is_explicitly_unsupported(self):
        self.begin()
        self.extend('refs')
        env = dict(self.env, GIT_OBJECT_DIRECTORY=str(self.repo / '.git/objects'))
        self.failure(self.cli('run', *self.common, '--plan', self.latest,
            '--plan-sha256', self.latest_hash, env=env), 23)


if __name__ == '__main__':
    unittest.main()
