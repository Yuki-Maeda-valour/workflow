"""#260: 計画の保存は全対象の作業木・管理領域・保護記録の外だけ。"""
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest

from diagnostic_test_support import DIAGNOSTIC, DiagnosticFixture, inventory


def entry():
    spec = importlib.util.spec_from_file_location('diagnostic_publication_entry', DIAGNOSTIC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicationBoundaries(unittest.TestCase):
    def test_every_context_root_is_protected_before_file_creation(self):
        module = entry()
        with tempfile.TemporaryDirectory(prefix='diagnostic-publication-') as raw:
            root = Path(raw).resolve()
            state_path = root / 'guard-state'; state_path.mkdir(mode=0o700)
            contexts = []
            for number in range(3):
                context = {'active': number != 2}
                for key in ('wt', 'gd', 'common'):
                    path = root / ('context-%d-%s' % (number, key))
                    path.mkdir(mode=0o700)
                    context[key] = os.fsencode(path)
                contexts.append(context)
            # 未初期化の対象は管理領域を持たない。相対パスへ変換しない。
            contexts.append({'active': False, 'wt': os.fsencode(root / 'absent'),
                             'gd': b'-', 'common': b'-'})
            roots = [state_path] + [Path(os.fsdecode(c[k]))
                for c in contexts[:3] for k in ('wt', 'gd', 'common')]
            for number, protected in enumerate(roots):
                direct = protected / 'plans'; direct.mkdir(mode=0o700)
                alias = root / ('alias-%d' % number)
                alias.symlink_to(direct, target_is_directory=True)
            before = inventory(root)
            for number, protected in enumerate(roots):
                for parent in (protected / 'plans', root / ('alias-%d' % number)):
                    with self.subTest(root=protected.name, alias=parent.is_symlink()):
                        budget = module.R.Budget()
                        state = SimpleNamespace(path=os.fsencode(state_path), contexts=contexts,
                            budget=budget, reader=module.R.Reader(budget))
                        output = parent / 'new-plan.json'
                        with self.assertRaises(module.R.Failure) as caught:
                            module.P.publish({'fixture': True}, output, state)
                        self.assertEqual(caught.exception.code, 2)
                        self.assertEqual(caught.exception.reason, 'out-location')
                        self.assertFalse(output.exists())
                        self.assertEqual(inventory(root), before)

    def test_external_parent_keeps_normal_output_and_does_not_check_ancestor_owner(self):
        module = entry()
        with tempfile.TemporaryDirectory(prefix='diagnostic-publication-') as raw:
            root = Path(raw).resolve()
            protected = root / 'protected'; protected.mkdir(mode=0o700)
            output_parent = root / 'protected-sibling'; output_parent.mkdir(mode=0o700)
            budget = module.R.Budget(); reader = module.R.Reader(budget)
            state = SimpleNamespace(path=os.fsencode(protected), contexts=[{
                'wt': os.fsencode(protected), 'gd': b'-', 'common': b'-'}],
                budget=budget, reader=reader)
            output = output_parent / 'normal.json'
            path, digest = module.P.publish({'fixture': True}, output, state)
            self.assertEqual(path, os.fsencode(output))
            self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(set(reader.owners), {os.fsencode(output_parent)})
            self.assertEqual(inventory(protected), {b'.': ('directory', 0o700)})


class PublicationRealGuard(DiagnosticFixture):
    def check_outputs(self, protected_roots):
        parents = []
        for number, protected in enumerate(protected_roots):
            parent = protected / ('diagnostic-plans-%d' % number)
            parent.mkdir(mode=0o700)
            alias = self.work / ('output-alias-%d' % number)
            alias.symlink_to(parent, target_is_directory=True)
            parents.extend((parent, alias))
        self.begin()
        state_parent = self.state / 'diagnostic-plans'; state_parent.mkdir(mode=0o700)
        parents.append(state_parent)
        original = tuple(inventory(path) for path in [*protected_roots, self.state])
        old_plan = self.latest.read_bytes()
        for number, parent in enumerate(parents):
            for mode in ('plan', 'extend-plan'):
                with self.subTest(mode=mode, parent=str(parent)):
                    output = parent / ('attempt-%d-%s.json' % (number, mode))
                    if mode == 'plan':
                        args = ['--base-ref', self.base_oid, '--start-ref', self.start_oid,
                                '--start-kind', 'new-invocation', '--out', output]
                    else:
                        args = ['--plan', self.latest, '--plan-sha256', self.latest_hash,
                                '--out', output, 'refs']
                    self.failure(self.cli(mode, *self.common, *args), 2, 'out-location')
                    self.assertFalse(output.exists())
                    self.assertEqual(tuple(inventory(path) for path in
                        [*protected_roots, self.state]), original)
                    self.assertEqual(self.latest.read_bytes(), old_plan)
        self.observe('refs')
        self.assertEqual(tuple(inventory(path) for path in
            [*protected_roots, self.state]), original)

    def test_linked_worktree_initial_and_extended_plans_cannot_write_admin_or_common(self):
        original = self.repo
        linked = self.work / 'linked'
        self.git('worktree', 'add', '-qb', 'linked', str(linked))
        self.repo = linked
        (linked / 'task').mkdir(); (linked / 'task/t.md').write_text('# fixture\n')
        common = original / '.git'
        self.check_outputs([linked, common, common / 'worktrees/linked'])

    def test_separate_gitdir_initial_and_extended_plans_cannot_write_admin(self):
        admin = self.work / 'separate-admin'
        self.git('init', '-q', '--separate-git-dir', str(admin))
        self.assertTrue((self.repo / '.git').is_file())
        self.check_outputs([self.repo, admin])

    def test_parent_operation_protects_unselected_child_separate_admin(self):
        child = self.repo / 'child'; child.mkdir()
        admin = self.work / 'child-admin'
        self.git('-C', str(child), 'init', '-q', '--separate-git-dir', str(admin))
        (child / 'public.txt').write_text('public child\n')
        self.git('-C', str(child), 'add', 'public.txt')
        self.git('-C', str(child), 'commit', '-qm', 'child initial')
        oid = self.git('-C', str(child), 'rev-parse', 'HEAD').stdout.decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + oid + ',child')
        self.git('commit', '-qm', 'child gitlink')
        self.check_outputs([self.repo, child, admin])
        contexts = [line.split('\t') for line in
            (self.state / 'snapshot/config-contexts.txt').read_text().splitlines()
            if line.startswith('context\t')]
        self.assertTrue(any(row[5] == str(child) and row[6] == str(admin)
                            and row[4] == '1' for row in contexts))
        self.assertEqual(self.common[-1], '0')


if __name__ == '__main__':
    unittest.main()
