"""#260: 最初の赤試験。期待条件は旧基準での確認から維持する。"""
import base64
import hashlib
import os
import stat
import subprocess
import sys
import unittest

from diagnostic_test_support import DiagnosticFixture, DIAGNOSTIC, inventory, strict_json


class DiagnosticGitTests(DiagnosticFixture):
    def test_plan_from_real_take_without_profiles(self):
        state, fields = self.take()
        repo_before, state_before = inventory(self.repo), inventory(state)
        self.assertFalse((self.repo / '.claude/project-profile.yml').exists())
        output = self.plan_dir / 'initial.json'
        binding = {'manifest_sha256': fields['MANIFEST_SHA256'],
                   'snapshot_sha256': fields['SNAPSHOT_SHA256']}
        result = subprocess.run(
            [sys.executable, '-I', '-S', str(DIAGNOSTIC), 'plan',
             '--cwd', str(self.repo), '--state', str(state),
             '--manifest-sha256', binding['manifest_sha256'],
             '--snapshot-sha256', binding['snapshot_sha256'], '--context-id', '0',
             '--base-ref', self.base_oid, '--start-ref', self.start_oid,
             '--start-kind', 'new-invocation', '--out', str(output)],
            env=self.env, cwd=self.work, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=330)
        self.assertEqual(inventory(self.repo), repo_before)
        self.assertEqual(inventory(state), state_before)
        self.assertEqual(result.returncode, 0,
                         '本物takeは成功。全profile不在のplanが失敗: ' + repr(result.stderr))
        self.assertTrue(result.stdout.isascii())
        self.assertTrue(result.stdout.endswith(b'\n'))
        self.assertEqual(result.stdout.count(b'\n'), 1)
        response = strict_json(result.stdout)
        self.assertEqual(set(response), {'version', 'status', 'plan_path_b64',
                                        'plan_sha256', 'context_id', 'state_binding'})
        self.assertIs(type(response['version']), int)
        self.assertIs(type(response['context_id']), int)
        self.assertEqual(response['version'], 1)
        self.assertEqual(response['status'], 'plan-created')
        self.assertEqual(response['context_id'], 0)
        self.assertEqual(response['state_binding'], binding)
        self.assertEqual(base64.b64decode(response['plan_path_b64'], validate=True),
                         os.fsencode(output))
        info = output.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.plan_dir.stat().st_mode), 0o700)
        self.assertFalse(output.is_relative_to(state))
        self.assertFalse(output.is_relative_to(self.repo))
        raw = output.read_bytes()
        self.assertEqual(response['plan_sha256'], hashlib.sha256(raw).hexdigest())
        plan = strict_json(raw)
        self.assertEqual(set(plan), {'version', 'state_binding', 'context_id',
                                    'management_binding', 'parent_plan_sha256',
                                    'prepared_request', 'source_checks', 'policies',
                                    'default_policy'})
        self.assertIs(type(plan['version']), int)
        self.assertIs(type(plan['context_id']), int)
        self.assertEqual(plan['version'], 1)
        self.assertEqual(plan['context_id'], 0)
        self.assertEqual(plan['state_binding'], binding)
        self.assertEqual(plan['management_binding'],
                         {'context_id': 0, 'relative_prefix_b64': '', 'identity_chain': []})
        self.assertIsNone(plan['parent_plan_sha256'])
        self.assertIsNone(plan['prepared_request'])
        self.assertEqual(plan['source_checks'], [])
        self.assertEqual(plan['default_policy'], 'snapshot-v1')
        self.assertEqual(len(plan['policies']), 1)
        policy = plan['policies'][0]
        self.assertEqual(set(policy), {'context_id', 'root_kind', 'root_prefix_b64',
                                      'history_origin', 'start_kind', 'branch_provenance',
                                      'sources', 'globs', 'eres'})
        self.assertIs(type(policy['context_id']), int)
        self.assertEqual(policy['context_id'], 0)
        self.assertEqual(policy['root_kind'], 'worktree')
        self.assertEqual(policy['root_prefix_b64'], '')
        self.assertEqual(policy['history_origin'], 'task')
        self.assertEqual(policy['start_kind'], 'new-invocation')
        self.assertEqual(policy['branch_provenance'], 'not-recorded')
        self.assertEqual(policy['globs'], ['.env', '.env.*', '.dev.vars'])
        self.assertEqual(policy['eres'], [])
        self.assertEqual(len(policy['sources']), 4)
        self.assertEqual({s['role'] for s in policy['sources']},
                         {'current', 'held-head', 'base', 'start'})
        for source in policy['sources']:
            self.assertEqual(set(source), {'role', 'commit_oid', 'profile_blob_oid',
                                           'profile_sha256', 'absent'})
            self.assertIs(source['absent'], True)
            self.assertIsNone(source['profile_blob_oid'])
            self.assertIsNone(source['profile_sha256'])
            self.assertEqual(source['commit_oid'],
                             None if source['role'] == 'current' else self.start_oid)


if __name__ == '__main__':
    unittest.main()
