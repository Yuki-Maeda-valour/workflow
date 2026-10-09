"""入力と導入元を確かめる事前検査。使い捨ての配布元とホームで動かす。"""
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'setup.sh'
BASH = os.environ.get('SETUP_TEST_BASH', shutil.which('bash'))
PATH_MODES = ('--copy', '--link', '--agents-copy', '--agents')
MODES = PATH_MODES + ('--global', '--agents-global', '--list')


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
        args = [mode]
        if mode not in ('--global', '--agents-global'):
            args.append(str(target or self.target))
        if force:
            args.append('--force')
        return self.run_args(args)

    def run_args(self, args):
        return subprocess.run([BASH, str(self.repo / 'setup.sh'), *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def argument_error(self, args, *diagnostics):
        before = self.snapshot()
        result = self.run_args(args)
        self.assertEqual(before, self.snapshot(), result.stderr)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('使い方:', result.stdout)
        for diagnostic in diagnostics:
            self.assertIn(diagnostic, result.stderr)

    def test_required_paths_are_checked_before_consumption(self):
        operands = [[], ['']] + [[flag] for flag in MODES + ('--force', '--help', '-h', '--unknown', '-')]
        for mode in PATH_MODES:
            for operand in operands:
                with self.subTest(mode=mode, operand=operand):
                    self.argument_error([mode, *operand], mode, 'プロジェクトパス')

    def test_all_49_mode_pairs_are_rejected_before_any_install(self):
        for first in MODES:
            for second in MODES:
                with self.subTest(first=first, second=second), self.fixture() as case:
                    args = []
                    for mode in (first, second):
                        args.append(mode)
                        if mode in PATH_MODES:
                            args.append(str(case.target))
                    case.argument_error(args, first, second)

    def test_missing_path_precedes_duplicate_mode_diagnostic(self):
        for mode in PATH_MODES:
            with self.subTest(mode=mode):
                self.argument_error(['--list', mode], mode, 'プロジェクトパス')

    def test_unknown_extra_and_absent_arguments_do_not_write(self):
        for args, diagnostics in (([], ()), (['--force'], ()),
                                  (['--unknown'], ('--unknown',)),
                                  (['--copy', str(self.target), 'extra'], ('extra',))):
            with self.subTest(args=args):
                self.argument_error(args, *diagnostics)

    def test_help_stops_immediately_but_does_not_cancel_prior_errors(self):
        for help_flag in ('-h', '--help'):
            for prefix in ([], ['--force'], ['--list'], ['--copy', str(self.target)]):
                with self.subTest(help=help_flag, prefix=prefix):
                    before = self.snapshot()
                    result = self.run_args([*prefix, help_flag, '--unknown'])
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('使い方:', result.stdout)
                    self.assertEqual('', result.stderr)
                    self.assertEqual(before, self.snapshot())
            self.argument_error(['--unknown', help_flag], '--unknown')
            self.argument_error(['--copy', help_flag], '--copy', 'プロジェクトパス')
            self.argument_error(['--list', '--global', help_flag], '--list', '--global')

    def test_list_and_force_orders_only_list(self):
        for args in (['--list'], ['--force', '--list'], ['--list', '--force']):
            with self.subTest(args=args):
                before = self.snapshot()
                result = self.run_args(args)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('含まれる skills:', result.stdout)
                self.assertIn('alpha', result.stdout)
                self.assertIn('omega', result.stdout)
                self.assertEqual(before, self.snapshot())

    def test_project_paths_and_force_orders_remain_valid(self):
        for mode in PATH_MODES:
            for name in ('project with spaces', '日本語', '-project'):
                for relative in (False, True):
                    with self.subTest(mode=mode, name=name, relative=relative), self.fixture() as case:
                        target = case.root / name
                        target.mkdir()
                        operand = './' + name if relative else str(target)
                        for prefix, suffix in ((['--force'], []), ([], ['--force']),
                                               (['--force', '--force'], ['--force'])):
                            result = case.run_args([*prefix, mode, operand, *suffix])
                            self.assertEqual(0, result.returncode, result.stderr)
                            self.assertIn('2 件導入', result.stdout)
                            destination = target / ('.agents' if 'agents' in mode else '.claude') / 'skills'
                            for skill in ('alpha', 'omega'):
                                self.assertEqual(skill + '\n', (destination / skill / 'SKILL.md').read_text())
                                self.assertEqual('copy' not in mode, (destination / skill).is_symlink())

    def test_nonexistent_project_remains_an_execution_error(self):
        for mode in PATH_MODES:
            with self.subTest(mode=mode):
                before = self.snapshot()
                result = self.run_args([mode, str(self.root / 'absent')])
                self.assertEqual(1, result.returncode)
                self.assertIn('存在しません', result.stderr)
                self.assertEqual(before, self.snapshot())

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
