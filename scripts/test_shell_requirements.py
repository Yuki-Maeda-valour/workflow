"""起動前の OS/bash/GNU 道具診断。旧 bash は実体の明示パスだけを使う。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOOP = Path(os.environ.get('SHELL_REQUIREMENTS_LOOP', ROOT / 'plugins/dev-workflow/skills/ship-task/scripts/loop.sh'))
SELFTEST = Path(os.environ.get('SHELL_REQUIREMENTS_SELFTEST', ROOT / 'plugins/dev-workflow/skills/do-task/scripts/diff-snapshot-selftest.sh'))
BASH = shutil.which('bash')


class ShellRequirements(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.log = self.work / 'calls'
        self.env = dict(os.environ, PATH=str(self.bin), REQUIREMENTS_CALL_LOG=str(self.log))
        for directory in ('/usr/bin', '/bin'):
            for entry in Path(directory).iterdir():
                if entry.is_file() and os.access(entry, os.X_OK) and not (self.bin / entry.name).exists():
                    (self.bin / entry.name).symlink_to(entry)
        for name in ('date', 'git', 'claude', 'codex'):
            real = shutil.which(name) if name in ('date', 'git') else None
            self.stub(name, 'printf "%s\\n" ' + name + ' >> "$REQUIREMENTS_CALL_LOG"\n' + (f'exec {real} "$@"\n' if real else 'exit 91\n'))
        self.init = self.work / 'bash-env'
        self.init.write_text('shopt() { printf "shopt\\n" >> "$REQUIREMENTS_CALL_LOG"; builtin shopt "$@"; }\ntrap() { printf "trap\\n" >> "$REQUIREMENTS_CALL_LOG"; builtin trap "$@"; }\n')
        self.env['BASH_ENV'] = str(self.init)

    def stub(self, name, body):
        path = self.bin / name
        path.unlink(missing_ok=True)
        path.write_text('#!' + BASH + '\n' + body)
        path.chmod(0o755)

    def run_script(self, script, *args, bash=BASH, timeout=20):
        return subprocess.run([bash, str(script), *args], cwd=self.work, env=self.env, text=True, capture_output=True, timeout=timeout)

    def no_initialization(self):
        self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else '')
        self.assertFalse((self.work / '.claude').exists())

    def os_failure(self, bash=BASH, uname='printf "Darwin\\n"', argument='--help'):
        self.stub('uname', uname)
        result = self.run_script(LOOP, argument, bash=bash)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertIn('ERROR [os]', result.stderr)
        self.no_initialization()

    def test_r1_darwin_before_initialization(self):
        self.os_failure()

    def old_bash(self, version):
        path = os.environ.get('SHELL_REQUIREMENTS_BASH_' + version.replace('.', '_'))
        if not path or not Path(path).is_file():
            self.skipTest(f'Bash {version} 実体なし（互換モードで代用しない）')
        return path

    def check_old_bash(self, version):
        bash = self.old_bash(version)
        self.stub('uname', 'printf "Linux\\n"')
        result = self.run_script(LOOP, '--help', bash=bash)
        self.assertEqual(result.returncode, 20, result.stderr)
        self.assertIn('ERROR [bash-version]', result.stderr)
        self.assertIn('4.4', result.stderr)
        self.no_initialization()
        self.os_failure(bash=bash)

    def test_r2_bash_3_2(self):
        self.check_old_bash('3.2')

    def test_r2_bash_4_3(self):
        self.check_old_bash('4.3')

    def test_r3_missing_tools(self):
        for name in ('sha256sum', 'touch', 'find', 'head', 'grep', 'sort'):
            with self.subTest(tool=name):
                original = (self.bin / name).readlink()
                (self.bin / name).unlink()
                try:
                    self.assert_dependency_failure(name)
                finally:
                    (self.bin / name).symlink_to(original)

    def assert_dependency_failure(self, tool):
        result = self.run_script(SELFTEST)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(tool, result.stderr)
        self.assertIn('ERROR [requirements]', result.stderr)
        self.assertNotIn('PASS', result.stdout)
        calls = self.log.read_text() if self.log.exists() else ''
        self.assertNotIn('git', calls)
        self.assertNotIn('claude', calls)
        self.assertNotIn('codex', calls)

    def test_r3_incompatible_tools(self):
        for name in ('sha256sum', 'touch', 'find', 'head', 'grep', 'sort'):
            with self.subTest(tool=name):
                original = (self.bin / name).readlink()
                # GNU 版の表示だけでは足りない。使用オプションが拒否される実体を試す。
                self.stub(name, f'if [ "${{1:-}}" = --version ]; then exec {original} "$@"; fi\nexit 64\n')
                try:
                    self.assert_dependency_failure(name)
                finally:
                    (self.bin / name).unlink()
                    (self.bin / name).symlink_to(original)

    def test_r3_touch_failure_after_precheck(self):
        real_touch = shutil.which('touch')
        # 能力検査は通し、scratch リポジトリの時刻固定だけを失敗させる。
        self.stub('touch', f'case "$*" in *src/auth.ts*) exit 64 ;; esac\nexec {real_touch} "$@"\n')
        result = self.run_script(SELFTEST, timeout=180)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('touch -d の時刻固定に失敗', result.stderr)
        self.assertNotIn('隠蔽を再現できない環境のため飛ばす', result.stdout)

    def test_r3_old_selftest_bash(self):
        result = self.run_script(SELFTEST, bash=self.old_bash('3.2'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('4.0', result.stderr)
        self.assert_dependency_output(result)

    def assert_dependency_output(self, result):
        self.assertIn('ERROR [requirements]', result.stderr)
        self.assertNotIn('PASS', result.stdout)

    def check_supported(self, bash):
        self.stub('uname', 'printf "Linux\\n"')
        for arg, expected in (('--help', 0), ('--invalid-argument', 2)):
            result = self.run_script(LOOP, arg, bash=bash)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_r4_current_bash(self):
        self.check_supported(BASH)

    def test_r4_bash_4_4(self):
        self.check_supported(self.old_bash('4.4'))

    def test_r5_uname_failure(self):
        self.os_failure(uname='exit 64')

    def test_r5_darwin_invalid_argument(self):
        self.os_failure(argument='--invalid-argument')


class RestoreRequirements(unittest.TestCase):
    def test_public_restore_runtime_failure_preserves_body_and_state(self):
        # 実際の公開入口に、欠落helper・構文不正・起動不能Pythonを渡す。
        test = ROOT / 'plugins/dev-workflow/skills/do-task/scripts/implement-guard-selftest.sh'
        result = subprocess.run([BASH, str(test), '--only', 'J9'],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('同梱helper不在', result.stdout)
        self.assertIn('Python起動不能', result.stdout)
        self.assertNotIn('FAIL  ', result.stdout)


if __name__ == '__main__':
    unittest.main()
