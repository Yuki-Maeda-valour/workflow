"""導入元を保護する事前検査。すべて使い捨ての配布元とホームで動かす。"""
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'setup.sh'
BASH = os.environ.get('SETUP_TEST_BASH', shutil.which('bash'))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'checkout'
        self.repo.mkdir()
        shutil.copy2(SCRIPT, self.repo / 'setup.sh')
        self.source = self.repo / 'plugins/dev-workflow/skills'
        for name in ('alpha', 'omega'):
            directory = self.source / name
            directory.mkdir(parents=True)
            (directory / 'SKILL.md').write_text(name + '\n')
        self.target = self.root / 'project'
        self.target.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home))

    def run_setup(self, mode='--copy', force=False, target=None):
        args = [BASH, str(self.repo / 'setup.sh'), mode]
        if mode not in ('--global', '--agents-global'):
            args.append(str(target or self.target))
        if force:
            args.append('--force')
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=15)

    def snapshot(self):
        result = {}
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                rel = str(path.relative_to(self.root))
                result[rel] = ('link', os.readlink(path)) if path.is_symlink() else (
                    ('dir',) if path.is_dir() else ('file', path.read_bytes()))
        return result

    def test_parent_link_to_source_is_rejected_without_changes(self):
        (self.target / '.claude').mkdir()
        (self.target / '.claude/skills').symlink_to(self.source, target_is_directory=True)
        before = self.snapshot()
        result = self.run_setup(force=True)
        self.assertEqual(before, self.snapshot(), result.stderr)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('ERROR', result.stderr)

    @contextmanager
    def fixture(self):
        case = SetupTests()
        case.setUp()
        try:
            yield case
        finally:
            case.doCleanups()

    def rejected_unchanged(self, **kwargs):
        before = self.snapshot()
        result = self.run_setup(**kwargs)
        self.assertEqual(before, self.snapshot(), result.stderr)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn('ERROR', result.stderr)

    def test_same_physical_source_and_internal_destination_without_force(self):
        for force in (False, True):
            with self.subTest(force=force), self.fixture() as case:
                destination = case.target / '.claude/skills'
                destination.parent.mkdir()
                shutil.move(str(case.source), destination)
                case.source.symlink_to(destination, target_is_directory=True)
                case.rejected_unchanged(force=force)
            with self.subTest(force=force, internal=True), self.fixture() as case:
                inside = case.source / 'alpha/project'
                inside.mkdir()
                case.rejected_unchanged(force=force, target=inside)

    def test_all_six_modes_reject_source_parent_links_with_or_without_force(self):
        for mode in ('--copy', '--link', '--agents-copy', '--agents', '--global', '--agents-global'):
            for force in (False, True):
                with self.subTest(mode=mode, force=force), self.fixture() as case:
                    host = '.agents' if 'agents' in mode else '.claude'
                    destination = (case.home if 'global' in mode else case.target) / host / 'skills'
                    destination.parent.mkdir()
                    destination.symlink_to(case.source, target_is_directory=True)
                    case.rejected_unchanged(mode=mode, force=force)

    def test_later_destination_contains_source_and_nothing_is_changed(self):
        for force in (False, True):
            with self.subTest(force=force), self.fixture() as case:
                destination = case.target / '.claude/skills'
                (destination / 'alpha').mkdir(parents=True)
                (destination / 'alpha/existing').write_text('keep\n')
                (destination / 'omega').mkdir()
                moved = destination / 'omega/checkout'
                shutil.move(str(case.repo), moved)
                case.repo = moved
                case.source = moved / 'plugins/dev-workflow/skills'
                case.rejected_unchanged(force=force)

    def test_all_six_modes_install_skip_and_force(self):
        for mode in ('--copy', '--link', '--agents-copy', '--agents', '--global', '--agents-global'):
            with self.subTest(mode=mode), self.fixture() as case:
                host = '.agents' if 'agents' in mode else '.claude'
                destination = (case.home if 'global' in mode else case.target) / host / 'skills'
                result = case.run_setup(mode=mode)
                self.assertEqual(0, result.returncode, result.stderr)
                for name in ('alpha', 'omega'):
                    self.assertEqual(name + '\n', (destination / name / 'SKILL.md').read_text())
                    self.assertEqual('copy' not in mode, (destination / name).is_symlink())
                before = case.snapshot()
                result = case.run_setup(mode=mode)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('2 件スキップ', result.stdout)
                self.assertEqual(before, case.snapshot())
                result = case.run_setup(mode=mode, force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('2 件導入', result.stdout)
                self.assertEqual(before, case.snapshot())

    def test_safe_parent_link_spaces_and_uncreated_parent(self):
        safe = self.root / 'safe directory'
        safe.mkdir()
        (self.target / '.claude').symlink_to(safe, target_is_directory=True)
        moved = self.target.with_name('project with spaces')
        self.target.rename(moved)
        self.target = moved
        result = self.run_setup()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('alpha\n', (safe / 'skills/alpha/SKILL.md').read_text())

    def test_final_link_is_not_followed_even_if_dangling_or_points_to_source_parent(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), self.fixture() as case:
                destination = case.target / '.claude/skills'
                destination.mkdir(parents=True)
                leaf = destination / 'alpha'
                leaf.symlink_to(case.root / 'missing' if dangling else case.repo, target_is_directory=True)
                result = case.run_setup()
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(leaf.is_symlink())
                self.assertEqual('alpha\n', (case.source / 'alpha/SKILL.md').read_text())
                result = case.run_setup(force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(leaf.is_symlink())
                self.assertEqual('alpha\n', (leaf / 'SKILL.md').read_text())
                self.assertEqual('alpha\n', (case.source / 'alpha/SKILL.md').read_text())

    def test_broken_cyclic_and_file_parents_are_rejected_before_writes(self):
        for kind in ('broken', 'cycle', 'file'):
            with self.subTest(kind=kind), self.fixture() as case:
                parent = case.target / '.claude'
                if kind == 'file':
                    parent.write_text('not a directory\n')
                else:
                    parent.symlink_to(case.root / 'absent' if kind == 'broken' else parent)
                case.rejected_unchanged(force=True)

    def test_similar_path_prefix_is_not_source_containment(self):
        target = self.source.with_name('skills-other')
        target.mkdir()
        result = self.run_setup(target=target)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('alpha\n', (target / '.claude/skills/alpha/SKILL.md').read_text())

    def case_alias(self, path):
        alias = self.root / str(path.relative_to(self.root)).upper()
        if not alias.exists() or not alias.samefile(path):
            self.skipTest('この一時領域のファイルシステムは大小文字を区別する')
        return alias

    def test_case_alias_source_is_rejected_without_changes(self):
        alias = self.case_alias(self.source)
        (self.target / '.claude').mkdir()
        (self.target / '.claude/skills').symlink_to(alias, target_is_directory=True)
        self.rejected_unchanged(force=True)

    def test_case_alias_source_internal_missing_parents_are_rejected(self):
        inside = self.source / 'alpha/project'
        inside.mkdir()
        alias = self.case_alias(inside)
        # コピーの自己再帰を試す必要はなく、リンクの作成も変更前に拒否する。
        self.rejected_unchanged(mode='--link', target=alias, force=True)

    def test_case_alias_later_source_ancestor_rejects_all_changes(self):
        alias = self.case_alias(self.target)
        destination = self.target / '.claude/skills'
        (destination / 'alpha').mkdir(parents=True)
        (destination / 'alpha/existing').write_text('keep\n')
        (destination / 'omega').mkdir()
        moved = destination / 'omega/checkout'
        shutil.move(str(self.repo), moved)
        self.repo = moved
        self.source = moved / 'plugins/dev-workflow/skills'
        self.rejected_unchanged(target=alias, force=True)


if __name__ == '__main__':
    unittest.main()
