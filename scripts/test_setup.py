"""入力と導入元を確かめる事前検査。使い捨ての配布元とホームで動かす。"""
import os
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from support_cli import run

SCRIPT = Path(__file__).resolve().parents[1] / 'setup.sh'
BASH = os.environ.get('SETUP_TEST_BASH', shutil.which('bash'))
PATH_MODES = ('--copy', '--link', '--agents-copy', '--agents')
MODES = PATH_MODES + ('--global', '--agents-global', '--list')
INSTALL_MODES = PATH_MODES + ('--global', '--agents-global')
BINARY_PATHS = ('linux-x86_64/dev-workflow', 'linux-arm64/dev-workflow',
                'macos-universal/dev-workflow')


def _identity(st):
    """置き直されていないかを比べる値。読んだ時刻(atime)は含めない。"""
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


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
        self.make_bundle()
        self.target = self.root / 'project'
        self.target.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home))

    @property
    def bundle(self):
        return self.repo / 'plugins/dev-workflow/bin/dev-workflow'

    def make_bundle(self):
        """実際の配布物が無い段階でも、導入の配置と権限を検査できる一組。"""
        self.bundle.mkdir(parents=True)
        launch = self.bundle / 'launch.sh'
        launch.write_text('#!/bin/sh\n'
                          'root=$(CDPATH= cd -P -- "$(dirname "$0")" && pwd) || exit 1\n'
                          'exec "$root/linux-x86_64/dev-workflow" "$@"\n')
        launch.chmod(0o755)
        sums = []
        for name in BINARY_PATHS:
            binary = self.bundle / name
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\nprintf "fixture\\n%s\\n" "$PWD"\n'
                              'printf "%s\\n" "$@"\n')
            binary.chmod(0o755)
            sums.append(hashlib.sha256(binary.read_bytes()).hexdigest() + '  ' + name + '\n')
        (self.bundle / 'SHA256SUMS').write_text(''.join(sums))
        (self.bundle / 'build-info.json').write_text('{"fixture": true}\n')
        for name in ('SHA256SUMS', 'build-info.json'):
            (self.bundle / name).chmod(0o644)

    def destination(self, mode='--copy', target=None):
        host = '.agents' if 'agents' in mode else '.claude'
        return (self.home if 'global' in mode else target or self.target) / host / 'skills'

    def assert_bundle(self, destination, linked):
        installed = destination.parent / 'bin/dev-workflow'
        self.assertEqual(linked, installed.is_symlink())
        if linked:
            self.assertTrue(installed.samefile(self.bundle))
        for path in self.bundle.rglob('*'):
            actual = installed / path.relative_to(self.bundle)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(actual.stat().st_mode))
            if path.is_file():
                self.assertEqual(path.read_bytes(), actual.read_bytes())

    def run_setup(self, mode='--copy', force=False, target=None):
        args = [mode]
        if mode not in ('--global', '--agents-global'):
            args.append(str(target or self.target))
        if force:
            args.append('--force')
        return self.run_args(args)

    def run_args(self, args):
        return run([BASH, str(self.repo / 'setup.sh'), *args], cwd=self.root,
                   env=self.env, timeout=15, stdin=None)

    def argument_error(self, args, *diagnostics):
        before = self.snapshot()
        result = self.run_args(args)
        self.assertEqual(before, self.snapshot(), result.stderr)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn('使い方:', result.stdout)
        for diagnostic in diagnostics:
            self.assertIn(diagnostic, result.stderr)

    def test_actual_distribution_resolves_after_all_six_install_modes(self):
        for mode in INSTALL_MODES:
            with self.subTest(mode=mode), self.fixture() as case:
                shutil.rmtree(case.bundle)
                real = SCRIPT.parent / 'plugins/dev-workflow'
                shutil.copytree(real / 'bin/dev-workflow', case.bundle)
                shutil.copytree(real / 'skills/create-task', case.source / 'create-task')
                installed = case.destination(mode).parent / 'bin/dev-workflow'
                result = case.run_setup(mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                linked = mode not in ('--copy', '--agents-copy')
                case.assert_bundle(case.destination(mode), linked)
                if not linked:
                    case.repo.rename(case.root / 'moved-distribution')
                else:
                    self.assertEqual(installed.resolve(), case.bundle.resolve())
                for engine in (None, 'legacy', 'rust'):
                    env = dict(case.env)
                    env.pop('DEV_WORKFLOW_ENGINE', None)
                    if engine is not None:
                        env['DEV_WORKFLOW_ENGINE'] = engine
                    before = sorted(case.target.rglob('*'))
                    result = subprocess.run([str(installed / 'launch.sh'), 'resolve-task-dir',
                                             '--project-root', str(case.target)], cwd='/', env=env,
                                            capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b'')
                    self.assertEqual(json.loads(result.stdout), {'task_dir': 'docs/tasks',
                                     'path': str(case.target / 'docs/tasks'), 'source': 'default'})
                    self.assertEqual(sorted(case.target.rglob('*')), before)

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
                self.assertIn('bin/dev-workflow/', result.stdout)
                self.assertNotIn('道具の版', result.stdout)
                self.assertEqual(before, self.snapshot())

    def tool_check(self, script=None):
        """配布元に、道具の版の確かめ(hooks/)と plugin.json を置く。script を渡せば、確かめのスクリプトを差し替える。"""
        plugin = SCRIPT.parent / 'plugins/dev-workflow'
        hooks = self.repo / 'plugins/dev-workflow/hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ('tool-versions.sh', 'tool-requirements.tsv'):
            shutil.copy2(plugin / 'hooks' / name, hooks / name)
        if script is not None:
            (hooks / 'tool-versions.sh').write_text(script)
        manifest = self.repo / 'plugins/dev-workflow/.claude-plugin'
        manifest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugin / '.claude-plugin/plugin.json', manifest / 'plugin.json')

    def test_tool_versions_table_at_the_end(self):
        self.tool_check()
        for mode in ('--copy', '--agents'):
            with self.subTest(mode=mode):
                result = self.run_setup(mode, force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('3 件導入', result.stdout)
                self.assertIn('が使う道具の版:', result.stdout)
                self.assertIn('| 道具 | 見つけた版 | 要る版 | 結果 | 使う所 |', result.stdout)
                self.assertLess(result.stdout.index('3 件導入'), result.stdout.index('が使う道具の版:'))
        self.assertFalse((self.home / '.local/state').exists())

    def test_tool_versions_failure_or_absence_keeps_exit_code(self):
        result = self.run_setup('--copy')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('NOTE: 道具の版を確かめるスクリプトが無いので、確かめませんでした:', result.stdout)
        self.tool_check(script='echo broken >&2\nexit 3\n')
        result = self.run_setup('--copy', force=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('NOTE: 道具の版を確かめられませんでした(終了コード 3)。', result.stdout)

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
                            self.assertIn('3 件導入', result.stdout)
                            destination = target / ('.agents' if 'agents' in mode else '.claude') / 'skills'
                            for skill in ('alpha', 'omega'):
                                self.assertEqual(skill + '\n', (destination / skill / 'SKILL.md').read_text())
                                self.assertEqual('copy' not in mode, (destination / skill).is_symlink())
                            case.assert_bundle(destination, linked='copy' not in mode)

    def test_nonexistent_project_remains_an_execution_error(self):
        for mode in PATH_MODES:
            with self.subTest(mode=mode):
                before = self.snapshot()
                result = self.run_args([mode, str(self.root / 'absent')])
                self.assertEqual(1, result.returncode)
                self.assertIn('存在しません', result.stderr)
                self.assertEqual(before, self.snapshot())

    def usage_text(self):
        result = self.run_args(['--help'])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stderr)
        self.assertTrue(result.stdout.startswith('使い方:\n'), result.stdout)
        self.assertTrue(result.stdout.endswith('  /plugin install dev-workflow@valour-workflow\n'),
                        result.stdout)
        return result.stdout

    def exact_error(self, args, stderr, returncode=2, with_usage=True):
        usage = self.usage_text() if with_usage else ''
        before = self.snapshot()
        result = self.run_args(args)
        self.assertEqual(before, self.snapshot(), result.stderr)
        self.assertEqual((returncode, stderr, usage),
                         (result.returncode, result.stderr, result.stdout))

    def test_error_messages_are_exact(self):
        path = str(self.target)
        cases = [
            (['--unknown'], '不明な引数: --unknown\n'),
            (['x  *'], '不明な引数: x  *\n'),
            (['--list', '--global'],
             'ERROR: モードは 1 つだけ指定してください: --list と --global\n'),
            (['--copy', path, '--copy', path],
             'ERROR: モードは 1 つだけ指定してください: --copy と --copy\n'),
            (['--agents-copy', path, '--list'],
             'ERROR: モードは 1 つだけ指定してください: --agents-copy と --list\n'),
            ([], ''),
            (['--force'], ''),
        ]
        for mode in PATH_MODES:
            cases.append(([mode], 'ERROR: %s の直後にプロジェクトパスが必要です\n' % mode))
        for args, stderr in cases:
            with self.subTest(args=args):
                self.exact_error(args, stderr)

    def test_overlapping_errors_report_the_first_one(self):
        path = str(self.target)
        missing = str(self.root / 'absent')
        duplicate = 'ERROR: モードは 1 つだけ指定してください: --list と --global\n'
        cases = [
            (['--list', '--copy'], 'ERROR: --copy の直後にプロジェクトパスが必要です\n'),
            (['--copy', path, '--copy'], 'ERROR: --copy の直後にプロジェクトパスが必要です\n'),
            (['--unknown', '--list', '--global'], '不明な引数: --unknown\n'),
            (['--list', '--global', '--unknown'], duplicate),
            (['--copy', missing, '--unknown'], '不明な引数: --unknown\n'),
        ]
        for args, stderr in cases:
            with self.subTest(args=args):
                self.exact_error(args, stderr)

    def test_missing_source_precedes_mode_check_only(self):
        shutil.rmtree(self.source)
        self.exact_error([], 'ERROR: %s が見つかりません\n' % self.source,
                         returncode=1, with_usage=False)
        result = self.run_args(['--unknown'])
        self.assertEqual((2, '不明な引数: --unknown\n'), (result.returncode, result.stderr))
        self.assertTrue(result.stdout.startswith('使い方:\n'), result.stdout)

    def test_reason_is_printed_before_usage(self):
        usage = self.usage_text()
        result = subprocess.run([BASH, str(self.repo / 'setup.sh'), '--unknown'], cwd=self.root,
                                env=self.env, text=True, timeout=15,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual((2, '不明な引数: --unknown\n' + usage), (result.returncode, result.stdout))

    def test_usage_output_failure_stops_with_status_1(self):
        for args, reason in ((['--unknown'], '不明な引数: --unknown\n'),
                             (['--list', '--global'],
                              'ERROR: モードは 1 つだけ指定してください: --list と --global\n'),
                             (['--copy'], 'ERROR: --copy の直後にプロジェクトパスが必要です\n')):
            with self.subTest(args=args):
                result = subprocess.run(
                    [BASH, '-c', 'exec >&-; exec "$@"', 'closed-stdout', BASH,
                     str(self.repo / 'setup.sh'), *args],
                    cwd=self.root, env=self.env, text=True, timeout=15,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertTrue(result.stderr.startswith(reason), result.stderr)

    def snapshot(self):
        result = {}
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                rel = str(path.relative_to(self.root))
                mode = stat.S_IMODE(path.lstat().st_mode)
                result[rel] = ('link', os.readlink(path)) if path.is_symlink() else (
                    ('dir', mode) if path.is_dir() else ('file', mode, path.read_bytes()))
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
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('ERROR', result.stderr)
        return result

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
        for mode in INSTALL_MODES:
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
        for mode in INSTALL_MODES:
            with self.subTest(mode=mode), self.fixture() as case:
                host = '.agents' if 'agents' in mode else '.claude'
                destination = (case.home if 'global' in mode else case.target) / host / 'skills'
                result = case.run_setup(mode=mode)
                self.assertEqual(0, result.returncode, result.stderr)
                for name in ('alpha', 'omega'):
                    self.assertEqual(name + '\n', (destination / name / 'SKILL.md').read_text())
                    self.assertEqual('copy' not in mode, (destination / name).is_symlink())
                case.assert_bundle(destination, linked='copy' not in mode)
                before = case.snapshot()
                result = case.run_setup(mode=mode)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('3 件スキップ', result.stdout)
                self.assertEqual(before, case.snapshot())
                result = case.run_setup(mode=mode, force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('3 件導入', result.stdout)
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
        self.assert_bundle(safe / 'skills', linked=False)

    def test_final_link_is_not_followed_even_if_dangling_or_points_to_source_parent(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling), self.fixture() as case:
                destination = case.target / '.claude/skills'
                destination.mkdir(parents=True)
                leaf = destination / 'alpha'
                leaf.symlink_to(case.root / 'missing' if dangling else case.repo, target_is_directory=True)
                result = case.rejected_unchanged()
                self.assertIn('--force', result.stderr)
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

    def test_bundle_absent_installs_only_skills_in_all_modes(self):
        for mode in INSTALL_MODES:
            with self.subTest(mode=mode), self.fixture() as case:
                shutil.rmtree(case.bundle.parent)
                destination = case.destination(mode)
                for force in (False, True):
                    result = case.run_setup(mode=mode, force=force)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('配布元に bin/dev-workflow/ がありません', result.stdout)
                    self.assertIn('skills だけを置きます', result.stdout)
                    self.assertIn('2 件導入', result.stdout)
                    self.assertFalse((destination.parent / 'bin').exists())
                    for name in ('alpha', 'omega'):
                        self.assertEqual(name + '\n', (destination / name / 'SKILL.md').read_text())
                        self.assertEqual('copy' not in mode, (destination / name).is_symlink())
                before = case.snapshot()
                result = case.run_args(['--list'])
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('配布元に bin/dev-workflow/ がありません', result.stdout)
                self.assertEqual(before, case.snapshot())

    def test_bundle_absent_does_not_touch_existing_bin(self):
        result = self.run_setup()
        self.assertEqual(0, result.returncode, result.stderr)
        installed = self.destination().parent / 'bin/dev-workflow'
        (installed / 'local-file').write_text('keep\n')
        shutil.rmtree(self.bundle)
        result = self.run_setup(force=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('keep\n', (installed / 'local-file').read_text())

    def test_copy_preserves_modes_under_umask_and_runs_without_checkout(self):
        for mode in ('--copy', '--agents-copy'):
            with self.subTest(mode=mode), self.fixture() as case:
                scripts = case.source / 'alpha/scripts'
                scripts.mkdir(mode=0o751)
                scripts.chmod(0o751)
                executable = scripts / 'run.sh'
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o751)
                result = run([BASH, '-c', 'umask 077; exec "$@"', 'setup', BASH,
                              str(case.repo / 'setup.sh'), mode, str(case.target)],
                             cwd=case.root, env=case.env, timeout=15, stdin=None)
                self.assertEqual(0, result.returncode, result.stderr)
                destination = case.destination(mode)
                case.assert_bundle(destination, linked=False)
                for path in (scripts, executable):
                    self.assertEqual(0o751, stat.S_IMODE(
                        (destination / path.relative_to(case.source)).stat().st_mode))
                # 権限の違いを検出する比較でも、直前に置いたコピーが同じと分かること。
                result = case.run_setup(mode=mode)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('3 件スキップ', result.stdout)
                shutil.rmtree(case.repo)
                launch = destination.parent / 'bin/dev-workflow/launch.sh'
                result = run([str(launch), 'probe', '日本語 空白', '', '*'], cwd=case.target,
                             env=case.env, timeout=15, stdin=None)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(['fixture', str(case.target), 'probe', '日本語 空白', '', '*'],
                                 result.stdout.splitlines())

    def test_matching_partial_install_can_be_completed_in_either_mode(self):
        for mode in ('--copy', '--link'):
            for existing_link in (False, True):
                for existing_name in ('omega', 'bin/dev-workflow'):
                    with self.subTest(mode=mode, link=existing_link, name=existing_name), self.fixture() as case:
                        source = case.bundle if existing_name.startswith('bin/') else case.source / existing_name
                        leaf = (case.destination().parent / existing_name if existing_name.startswith('bin/')
                                else case.destination() / existing_name)
                        leaf.parent.mkdir(parents=True)
                        if existing_link:
                            leaf.symlink_to(os.path.relpath(source, leaf.parent), target_is_directory=True)
                        else:
                            shutil.copytree(source, leaf)
                        # 読んだ時刻(atime)は、setup.sh が中身を読むと変わるので比べない。
                        before = _identity(leaf.lstat())
                        result = case.run_setup(mode=mode)
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertIn('2 件導入 / 1 件スキップ', result.stdout)
                        self.assertEqual(before, _identity(leaf.lstat()))
                        self.assertEqual('alpha\n', (case.destination() / 'alpha/SKILL.md').read_text())
                        self.assertTrue((case.destination().parent / 'bin/dev-workflow/launch.sh').is_file())

    def test_different_content_modes_and_entries_reject_partial_install(self):
        changes = ('content', 'file-mode', 'directory-mode', 'missing', 'extra-hidden', 'type', 'link')
        for name in ('omega', 'bin/dev-workflow'):
            for change in changes:
                with self.subTest(name=name, change=change), self.fixture() as case:
                    source = case.bundle if name.startswith('bin/') else case.source / name
                    leaf = (case.destination().parent / name if name.startswith('bin/')
                            else case.destination() / name)
                    shutil.copytree(source, leaf)
                    file = leaf / ('launch.sh' if name.startswith('bin/') else 'SKILL.md')
                    if change == 'content':
                        # 長さと時刻が同じでも本文を読む(時刻だけで同一と見なさない)。
                        before = file.stat()
                        file.write_bytes(b'x' * before.st_size)
                        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
                    elif change == 'file-mode':
                        file.chmod(stat.S_IMODE(file.stat().st_mode) ^ stat.S_IXUSR)
                    elif change == 'directory-mode':
                        leaf.chmod(stat.S_IMODE(leaf.stat().st_mode) ^ stat.S_IXGRP)
                    elif change == 'extra-hidden':
                        (leaf / '.local').write_text('extra\n')
                    else:
                        file.unlink()
                        if change == 'type':
                            file.mkdir()
                        elif change == 'link':
                            file.symlink_to(source / file.name)
                    result = case.rejected_unchanged()
                    self.assertIn('--force', result.stderr)
                    self.assertFalse((case.destination() / 'alpha').exists())

    def test_internal_symlinks_are_compared_without_following_them(self):
        source = self.source / 'omega'
        (source / 'alias').symlink_to('SKILL.md')
        (source / 'missing').symlink_to('absent')
        result = self.run_setup()
        self.assertEqual(0, result.returncode, result.stderr)
        result = self.run_setup()
        self.assertEqual(0, result.returncode, result.stderr)
        installed = self.destination() / 'omega/alias'
        installed.unlink()
        installed.symlink_to(source / 'SKILL.md')
        self.rejected_unchanged()

    def test_force_updates_skills_and_bundle_together_in_all_modes(self):
        for mode in INSTALL_MODES:
            with self.subTest(mode=mode), self.fixture() as case:
                destination = case.destination(mode)
                # 旧版のコピーを先に置き、導入方式も変更できることを確かめる。
                for name in ('alpha', 'omega'):
                    shutil.copytree(case.source / name, destination / name)
                shutil.copytree(case.bundle, destination.parent / 'bin/dev-workflow')
                (case.source / 'alpha/SKILL.md').write_text('new alpha\n')
                (case.bundle / 'build-info.json').write_text('{"fixture": "new"}\n')
                case.rejected_unchanged(mode=mode)
                result = case.run_setup(mode=mode, force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('3 件導入', result.stdout)
                self.assertEqual('new alpha\n', (destination / 'alpha/SKILL.md').read_text())
                case.assert_bundle(destination, linked='copy' not in mode)

    def test_broken_managed_leaves_require_force_in_all_modes(self):
        for mode in INSTALL_MODES:
            for name in ('omega', 'bin/dev-workflow'):
                with self.subTest(mode=mode, name=name), self.fixture() as case:
                    destination = case.destination(mode)
                    leaf = (destination.parent / name if name.startswith('bin/') else destination / name)
                    leaf.parent.mkdir(parents=True)
                    leaf.symlink_to(case.root / 'moved-checkout', target_is_directory=True)
                    result = case.rejected_unchanged(mode=mode)
                    self.assertIn('リンク切れ', result.stderr)
                    self.assertIn('--force', result.stderr)
                    result = case.run_setup(mode=mode, force=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual('omega\n', (destination / 'omega/SKILL.md').read_text())
                    case.assert_bundle(destination, linked='copy' not in mode)

    def test_broken_or_invalid_skill_and_bin_parents_never_allow_force(self):
        for name in ('skills', 'bin'):
            for kind in ('broken', 'cycle', 'file'):
                for force in (False, True):
                    with self.subTest(name=name, kind=kind, force=force), self.fixture() as case:
                        parent = case.destination().parent / name
                        parent.parent.mkdir()
                        if kind == 'file':
                            parent.write_text('keep\n')
                        else:
                            parent.symlink_to(case.root / 'missing' if kind == 'broken' else parent)
                        case.rejected_unchanged(force=force)

    def test_other_bin_entries_are_preserved_in_all_modes(self):
        for mode in INSTALL_MODES:
            with self.subTest(mode=mode), self.fixture() as case:
                parent = case.destination(mode).parent / 'bin'
                (parent / 'other-tool').mkdir(parents=True)
                (parent / 'other-tool/data').write_text('keep\n')
                (parent / 'another-link').symlink_to(case.root / 'absent')
                executable = parent / 'other-command'
                executable.write_text('keep executable\n')
                executable.chmod(0o751)
                for force in (False, True):
                    result = case.run_setup(mode=mode, force=force)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual('keep\n', (parent / 'other-tool/data').read_text())
                    self.assertEqual(str(case.root / 'absent'), os.readlink(parent / 'another-link'))
                    self.assertEqual('keep executable\n', executable.read_text())
                    self.assertEqual(0o751, stat.S_IMODE(executable.stat().st_mode))

    def test_bin_and_skill_source_overlaps_reject_before_all_writes(self):
        for mode in INSTALL_MODES:
            for force in (False, True):
                for parent_name, source_name in (('bin', 'bin'), ('bin', 'skill'), ('skills', 'bin')):
                    with self.subTest(mode=mode, force=force, parent=parent_name, source=source_name), self.fixture() as case:
                        parent = case.destination(mode).parent / parent_name
                        parent.parent.mkdir()
                        source = case.bundle if source_name == 'bin' else case.source / 'alpha'
                        parent.symlink_to(source, target_is_directory=True)
                        case.rejected_unchanged(mode=mode, force=force)

    def test_bin_destination_containing_checkout_rejects_before_skills_change(self):
        for force in (False, True):
            with self.subTest(force=force), self.fixture() as case:
                destination = case.destination()
                (destination / 'alpha').mkdir(parents=True)
                (destination / 'alpha/existing').write_text('keep\n')
                bin_dest = destination.parent / 'bin/dev-workflow'
                bin_dest.mkdir(parents=True)
                moved = bin_dest / 'checkout'
                shutil.move(str(case.repo), moved)
                case.repo = moved
                case.source = moved / 'plugins/dev-workflow/skills'
                case.rejected_unchanged(force=force)

    def test_parent_links_between_managed_destinations_are_rejected(self):
        for force in (False, True):
            with self.subTest(force=force), self.fixture() as case:
                destination = case.destination()
                (destination / 'omega').mkdir(parents=True)
                (destination.parent / 'bin').symlink_to(destination / 'omega', target_is_directory=True)
                case.rejected_unchanged(force=force)

    def test_same_managed_destination_is_rejected_even_when_not_yet_created(self):
        for force in (False, True):
            with self.subTest(force=force), self.fixture() as case:
                skill = case.source / 'dev-workflow'
                skill.mkdir()
                (skill / 'SKILL.md').write_text('dev-workflow\n')
                destination = case.destination()
                destination.mkdir(parents=True)
                (destination.parent / 'bin').symlink_to(destination, target_is_directory=True)
                case.rejected_unchanged(force=force)

    @unittest.skipIf(os.geteuid() == 0, 'root は権限不足を再現できない')
    def test_permission_errors_are_detected_before_any_install(self):
        for force in (False, True):
            for kind in ('bin-parent', 'bin-destination', 'source-file'):
                with self.subTest(force=force, kind=kind), self.fixture() as case:
                    if kind == 'bin-parent':
                        path = case.destination().parent / 'bin'
                        path.mkdir(parents=True)
                        permissions = 0o500
                    elif kind == 'bin-destination':
                        path = case.destination().parent / 'bin/dev-workflow'
                        shutil.copytree(case.bundle, path)
                        permissions = 0o500
                    else:
                        path = case.bundle / 'launch.sh'
                        permissions = 0o000
                    previous = stat.S_IMODE(path.stat().st_mode)
                    # 本文を読めないファイルもあるため、この例だけ手で変更なしを確認する。
                    path.chmod(permissions)
                    try:
                        result = case.run_setup(force=force)
                        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                        self.assertIn('ERROR', result.stderr)
                        self.assertFalse(case.destination().exists())
                        self.assertEqual(permissions, stat.S_IMODE(path.stat().st_mode))
                    finally:
                        path.chmod(previous)

    def test_broken_or_file_bundle_source_is_not_treated_as_absent(self):
        for kind in ('broken', 'file'):
            with self.subTest(kind=kind), self.fixture() as case:
                shutil.rmtree(case.bundle)
                if kind == 'file':
                    case.bundle.write_text('invalid\n')
                else:
                    case.bundle.symlink_to(case.root / 'absent', target_is_directory=True)
                case.rejected_unchanged()

    def test_wrong_source_link_requires_force_even_with_matching_content(self):
        for name in ('omega', 'bin/dev-workflow'):
            with self.subTest(name=name), self.fixture() as case:
                source = case.bundle if name.startswith('bin/') else case.source / name
                other = case.root / 'other-checkout'
                shutil.copytree(source, other)
                leaf = (case.destination().parent / name if name.startswith('bin/')
                        else case.destination() / name)
                leaf.parent.mkdir(parents=True)
                leaf.symlink_to(other, target_is_directory=True)
                case.rejected_unchanged()
                result = case.run_setup(force=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(leaf.is_symlink())
                self.assertTrue(other.is_dir())

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
