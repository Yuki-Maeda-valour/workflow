"""#260: 起動前計画の親子引継ぎと既存除外の単調な追加。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import unittest

from diagnostic_test_support import DiagnosticFixture, strict_json


class DiagnosticContexts(DiagnosticFixture):
    def make_child(self):
        child = self.repo / 'child'; child.mkdir()
        self.git('-C', str(child), 'init', '-q', '-b', 'main')
        (child / 'public.txt').write_text('child public')
        self.git('-C', str(child), 'add', 'public.txt')
        self.git('-C', str(child), 'commit', '-qm', 'child initial')
        oid = self.git('-C', str(child), 'rev-parse', 'HEAD').stdout.decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + oid + ',child')
        self.git('commit', '-qm', 'child gitlink')
        return child

    def child_plan(self, child):
        contexts = (self.state / 'snapshot/config-contexts.txt').read_text().splitlines()
        fields = next(line.split('\t') for line in contexts
                      if line.startswith('context\t') and line.split('\t')[5] == str(child))
        self.assertEqual(fields[4], '1')
        child_id = int(fields[1])
        previous, previous_hash = self.latest, self.latest_hash
        common = self.common.copy(); common[-1] = str(child_id)
        output = self.plan_dir / 'child-initial.json'
        response = self.success(self.cli('plan', *common, '--context-start',
            '--carry-plan', previous, previous_hash, '--out', output), 'plan-created')
        plan = strict_json(output.read_bytes())
        self.assertEqual(plan['parent_plan_sha256'], previous_hash)
        self.assertEqual({p['context_id'] for p in plan['policies']}, {0, child_id})
        child_policy = next(p for p in plan['policies'] if p['context_id'] == child_id)
        self.assertEqual(child_policy['history_origin'], 'context-start')
        self.assertIsNone(child_policy['start_kind'])
        self.assertEqual({s['role'] for s in child_policy['sources']}, {'current', 'held-head'})
        self.common, self.latest, self.latest_hash = common, output, response['plan_sha256']
        return child_id

    def test_parent_child_carry_retains_ancestor_exclusions_on_return_to_parent(self):
        child = self.make_child()
        self.profile(['child/parent-secret'])
        (child / 'parent-secret').write_text('parent-protected')
        self.begin()
        self.child_plan(child)
        result = self.observe('files', '--under', '.', '--kind', 'all')
        self.assertNotIn(b'parent-secret', self.records(result))
        self.assertIn(b'public.txt', self.records(result))
        self.profile(['child/late-secret'])
        (child / 'late-secret').write_text('later-protected')
        self.observe('refs')  # 子を準備するときにも祖先の追加を保持する。
        self.profile([])
        self.common[-1] = '0'
        self.observe('refs')
        plan = strict_json(self.latest.read_bytes())
        root = next(p for p in plan['policies'] if p['context_id'] == 0)
        self.assertIn('child/parent-secret', root['globs'])
        self.assertIn('child/late-secret', root['globs'])

    def test_child_context_start_requires_parent_carry(self):
        child = self.make_child()
        self.begin()
        contexts = (self.state / 'snapshot/config-contexts.txt').read_text().splitlines()
        fields = next(line.split('\t') for line in contexts
                      if line.startswith('context\t') and line.split('\t')[5] == str(child))
        common = self.common.copy(); common[-1] = fields[1]
        self.failure(self.cli('plan', *common, '--context-start',
            '--out', self.plan_dir / 'missing-parent.json'), 22)

    def test_foreign_state_carry_is_not_adopted(self):
        self.begin()
        (self.repo / 'seed.txt').write_text('new invocation content\n')
        self.commit('new invocation')
        second, fields = self.take()
        common = self.common.copy()
        common[common.index('--state')+1] = str(second)
        common[common.index('--manifest-sha256')+1] = fields['MANIFEST_SHA256']
        common[common.index('--snapshot-sha256')+1] = fields['SNAPSHOT_SHA256']
        # 別の起動時点で取得したstateのhashに旧計画を合わせ直さない。
        result = self.cli('plan', *common, '--base-ref', self.base_oid, '--start-ref', self.start_oid,
            '--start-kind', 'new-invocation', '--carry-plan', self.latest, self.latest_hash,
            '--out', self.plan_dir / 'foreign.json')
        self.failure(result, 22)

    def test_nested_repository_boundary_does_not_open_inner_files(self):
        child = self.make_child()
        self.begin()
        result = self.observe('files', '--path', 'child/public.txt', '--kind', 'all')
        self.assertNotIn(b'child/public.txt', self.records(result))
        self.assertTrue(result['data']['boundaries'])
        self.assertIn('nested-content-not-observed', result['warnings'])


if __name__ == '__main__':
    unittest.main()
