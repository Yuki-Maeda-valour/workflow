"""純粋な host argv 検査。ホスト起動・認証・PyYAML に依存しない。"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/ship-task/scripts/host-argv.py'

class HostArgvTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('host_argv', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_normal(self):
        self.module.validate_argv(['host', '--model=model', '--effort=high', '--max-budget-usd=1.5', '--max-turns=4', '--name=normal', '--verbose'])
        self.module.validate_argv(['host'])

    def test_unsafe_unknown_alias_bundles(self):
        for arg in ('--continue', '-c', '--resume=x', '-rfoo', '-vc', '--remote', '--cloud=id',
                    '--plugin-url=x', '--bare', '--settings={}', '--no-hooks', '--mystery', '--verbose=false',
                    '--model', '--model=--continue', '--', 'prompt', '--max-turns=0', '--effort=unknown'):
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                self.module.validate_argv(['host', arg])

    def test_missing_executable_and_duplicate(self):
        for args in ([], [''], ['--verbose'], ['host','--verbose','--verbose'], ['host','--model=a','--model=b']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.module.validate_argv(args)

    def test_cli_without_site_packages_or_host_binary(self):
        normal = subprocess.run([sys.executable, '-S', str(SCRIPT), '--', 'nonexistent-host', '--model=normal'], capture_output=True, text=True)
        self.assertEqual(normal.returncode, 0, normal.stderr)
        bad = subprocess.run([sys.executable, '-S', str(SCRIPT), '--', 'nonexistent-host', '--resume=private-sentinel'], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 20)
        self.assertNotIn('private-sentinel', bad.stderr)

if __name__ == '__main__':
    unittest.main()
