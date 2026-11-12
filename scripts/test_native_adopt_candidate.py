"""R4C比較器の欠落・不一致の検出を、外部の Rust ビルドなしで全回帰テストへつなぐ。

比較器の実比較(旧版と Rust 実行物の比較)は CI の専用段で行う。ここでは次だけを確かめる:
- 比較器の自己検査(native/ci/test_adopt_candidate.py)が、Rust 実行物を使わずに全部通る
- 旧採用テストの全メソッドが観点の表へ 1 回ずつ載っている
- 実行物の指定が無い・相対・不在のとき、比較器が不合格で止まる(skip にしない)
- 引数の固定期待が本家 CPython 3.10.11 の argparse.py から採られている
"""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / 'native/ci'
CHECKER = CI / 'check_adopt_candidate.py'
SELF_TEST = CI / 'test_adopt_candidate.py'
ARGUMENT_CASES = CI / 'adopt_candidate_fixtures' / 'arg_cases.json'


def load(path, name):
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


class AdoptCandidateComparator(unittest.TestCase):
    def test_comparator_self_tests_pass_without_a_rust_build(self):
        module = load(SELF_TEST, 'r4c_selftest_loaded')
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        count = suite.countTestCases()
        self.assertGreaterEqual(count, 15)
        result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        self.assertEqual(result.testsRun, count)
        self.assertEqual([], result.skipped)
        self.assertTrue(result.wasSuccessful(), [text for _, text in result.failures + result.errors])

    def test_old_test_map_covers_all_26_old_methods(self):
        checker = load(CHECKER, 'r4c_checker_loaded')
        self.assertEqual(len(checker.old_test_methods()), 26)
        self.assertEqual(checker.verify_map(), (26, 20))

    def run_checker(self, *args):
        return subprocess.run([sys.executable, '-B', str(CHECKER), *args], capture_output=True, text=True,
                              cwd=ROOT, timeout=60)

    def test_checker_fails_without_a_usable_native_binary(self):
        with tempfile.TemporaryDirectory(prefix='r4c-selftest-') as directory:
            plain = Path(directory) / 'not-executable'
            plain.write_bytes(b'\x7fELF')
            for args in (['--fixture', ''], ['--fixture', 'relative/path'], ['--fixture', str(Path(directory) / 'absent')],
                         ['--fixture', str(plain)]):
                result = self.run_checker(*args)
                self.assertEqual(result.returncode, 1, args)
                self.assertIn('ADOPT: 不合格', result.stderr)
                self.assertEqual(result.stdout, '')
        self.assertEqual(self.run_checker().returncode, 2)

    def test_argument_expectations_come_from_upstream_3_10_11(self):
        document = json.loads(ARGUMENT_CASES.read_text(encoding='utf-8'))
        checker = load(CHECKER, 'r4c_checker_loaded_args')
        self.assertEqual(document['argparse_sha256'], checker.UPSTREAM_STDLIB_SHA256['argparse.py'])
        cases = document['cases']
        kinds = {'parsed': 0, 'help': 0, 'error': 0}
        for case in cases:
            kinds['parsed' if 'parsed' in case else 'help' if case['code'] == 0 else 'error'] += 1
        self.assertGreater(kinds['parsed'], 5)
        self.assertGreater(kinds['help'], 10)
        self.assertGreater(kinds['error'], 30)
        self.assertEqual(len({tuple(case['args']) + (case['columns'],) for case in cases}), len(cases))

    def test_rust_errno_table_equals_the_comparator_reference(self):
        import re
        source = (ROOT / 'native/crates/cli/src/commands/adopt_candidate/diagnostics.rs').read_text(encoding='utf-8')
        start = source.index('const GLIBC_STRERROR')
        rows = re.findall(r'\((\d+), "([^"]*)"\),', source[start:source.index('];', start)])
        checker = load(CHECKER, 'r4c_checker_loaded_errno')
        self.assertEqual({int(number): text for number, text in rows}, checker.GLIBC_ERRNO_REFERENCE)
        self.assertEqual(len(rows), 131)

    def test_rust_large_json_table_equals_the_comparator_table(self):
        import re
        source = (ROOT / 'native/crates/cli/src/commands/adopt_candidate/runtime.rs').read_text(encoding='utf-8')
        start = source.index('for (total, room, code, written, unraisable) in [')
        rows = re.findall(r'\((\d+), (\d+), (\d+), (\d+), (true|false)\),', source[start:source.index('] {', start)])
        checker = load(CHECKER, 'r4c_checker_loaded_large')
        rust = {}
        for total, room, code, written, unraisable in rows:
            kind = 'unraisable' if unraisable == 'true' else {'0': 'quiet', '2': 'stop'}[code]
            rust[(int(total), int(room))] = (int(code), int(written), kind)
        self.assertEqual(rust, checker.LARGE_JSON_EXPECTED)

    def test_fault_scenarios_cover_every_group_and_each_has_an_expectation(self):
        scenarios = json.loads((ROOT / 'native/ci/adopt_candidate_fixtures/fault_scenarios.json').read_text(encoding='utf-8'))['scenarios']
        identifiers = [item['id'] for item in scenarios]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertGreater(len(scenarios), 60)
        groups = {item['group'] for item in scenarios}
        for wanted in ('A11', 'A15', 'A16', 'A19', 'A20', 'A21', 'A22', 'A23', 'A24', 'A25', 'A26',
                       'E05', 'E07', 'E08', 'E09', 'E10', 'E11', 'E12'):
            self.assertIn(wanted, groups)
        for item in scenarios:
            self.assertIn(item.get('kind', 'run'), ('run', 'kill', 'interrupt', 'bypass'))
            self.assertEqual(set(item['expect']) - {'same_inode', 'both_names_at_window'}, {'code', 'stdout', 'stderr', 'tree'} | ({'index'} if item['prep'].get('git') else set()), item['id'])
        kill = next(item for item in scenarios if item['id'].startswith('A25-'))
        self.assertEqual((kill['expect']['code'], kill['expect']['same_inode'], kill['expect']['both_names_at_window']), (-9, True, True))

    def test_generator_definitions_equal_the_committed_scenarios(self):
        generator = load(CI / 'adopt_candidate_fixtures' / 'gen_fault_scenarios.py', 'r4c_generator_loaded')
        committed = json.loads((CI / 'adopt_candidate_fixtures' / 'fault_scenarios.json').read_text(encoding='utf-8'))['scenarios']
        definitions = [{key: value for key, value in item.items() if key != 'expect'} for item in committed]
        self.assertEqual(json.loads(json.dumps(generator.scenarios())), definitions)
        # os の欄が付く(Linux だけの改名の注入)。
        self.assertGreaterEqual(sum('os' in item for item in committed), 9)


if __name__ == '__main__':
    unittest.main()
