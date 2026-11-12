#!/usr/bin/env python3
"""R4C比較器(check_adopt_candidate.py)が、欠落・重複・0件・skip・時間切れ・両側同じ誤値・余計な出力・
他者の実体の削除・旧版への誤接続を拒否する自己検査。Rust の実行物は使わない(代役は小さなスクリプト)。"""
import copy
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest import mock

subject = None


def get_subject():
    global subject
    if subject is None:
        spec = importlib.util.spec_from_file_location('r4c_subject', Path(__file__).with_name('check_adopt_candidate.py'))
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


def write_executable(path, text, mode=0o755):
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    path.chmod(mode)
    return path


class Comparator(unittest.TestCase):
    def setUp(self):
        self.m = get_subject()
        self.tmp = tempfile.TemporaryDirectory(prefix='r4c-selftest-')
        self.dir = Path(self.tmp.name)
        self.saved = (self.m.FIXTURE, self.m.OLD_PYTHON, self.m.ARG_CASES, self.m.native)
        self.m.OLD_PYTHON = Path(sys.executable)

    def tearDown(self):
        self.m.FIXTURE, self.m.OLD_PYTHON, self.m.ARG_CASES, self.m.native = self.saved
        self.tmp.cleanup()

    def result(self, code=0, out=b'', err=b'', timeout=False):
        return self.m.RunResult(code, out, err, timeout)

    # ---- 実行物の指定 ----

    def test_fixture_path_accepts_only_an_absolute_executable_native_file(self):
        good = write_executable(self.dir / 'native', b'\x7fELF' + b'\0' * 60)
        self.assertEqual(self.m.fixture_path(str(good)), good)
        script = write_executable(self.dir / 'script', '#!/bin/sh\nexit 0\n')
        plain = write_executable(self.dir / 'plain', b'\x7fELF', mode=0o644)
        wrapper = write_executable(self.dir / 'python-wrapper', f'#!{sys.executable}\nprint(1)\n')
        linked = self.dir / 'legacy-link'
        linked.symlink_to(self.m.LEGACY_SCRIPT)
        for value in (None, '', 'relative/native', str(self.dir / 'absent'), str(self.dir), str(plain),
                      str(script), str(wrapper), str(linked), str(self.m.LEGACY_SCRIPT)):
            with self.assertRaises(ValueError, msg=value):
                self.m.fixture_path(value)

    def test_native_binary_inside_the_distributed_tree_is_a_wrong_connection(self):
        repo = self.dir / 'repo'
        (repo / 'plugins').mkdir(parents=True)
        inside = write_executable(repo / 'plugins/tool', b'\x7fELF')
        with self.assertRaises(ValueError):
            self.m.fixture_path(str(inside), repo=repo)
        outside = write_executable(repo / 'tool', b'\x7fELF')
        self.assertEqual(self.m.fixture_path(str(outside), repo=repo), outside)

    # ---- 旧 Python ----

    def test_old_python_standard_library_must_match_the_recorded_hashes(self):
        import argparse
        import hashlib
        actual = {'argparse.py': hashlib.sha256(Path(argparse.__file__).read_bytes()).hexdigest()}
        self.assertTrue(self.m.verify_old_python(sys.executable, actual, version=None).startswith('('))
        with self.assertRaises(AssertionError):
            self.m.verify_old_python(sys.executable, actual, version='(2, 7, 18)')
        for wrong in ({'argparse.py': '0' * 64}, {'argparse.py': actual['argparse.py'].upper()},
                      {**actual, 'argparse.pyc': actual['argparse.py']}):
            with self.assertRaises((AssertionError, KeyError, OSError)):
                self.m.verify_old_python(sys.executable, wrong, version=None)
        with self.assertRaises(AssertionError):
            self.m.verify_old_python(sys.executable, {'no_such_stdlib_file.py': '0' * 64}, version=None)

    # ---- 結果の比較 ----

    def test_same_result_rejects_every_field_and_timeouts_and_extra_output(self):
        good = self.result(out=b'fixed\n')
        self.m.same_result(good, good)
        for change in (dict(returncode=1), dict(stdout=b'other'), dict(stderr=b'extra'), dict(stdout=b'fixed\nextra\n'),
                       dict(timed_out=True)):
            with self.assertRaises(AssertionError, msg=change):
                self.m.same_result(good, dataclasses.replace(good, **change))
            with self.assertRaises(AssertionError, msg=change):
                self.m.same_result(dataclasses.replace(good, **change), good)

    def test_fixed_rejects_equal_wrong_results_on_both_sides(self):
        expected = (2, b'', '候補の採用を停止: x\n'.encode())
        right = self.result(*expected)
        self.m.fixed(right, *expected)
        for wrong in (self.result(0, b'', expected[2]), self.result(2, b'out', expected[2]), self.result(2, b'', b''),
                      self.result(2, b'', expected[2] + b'extra'), self.result(2, b'', expected[2], True)):
            with self.assertRaises(AssertionError):
                self.m.fixed(wrong, *expected)
            # 両側が同じ誤値でも、同じ結果なので same_result は通る。固定期待だけが拒む。
            self.m.same_result(wrong, dataclasses.replace(wrong)) if not wrong.timed_out else None
        case = self.m.Contract('test_a02_loop_marker')
        wrong = self.result(2, b'', b'wrong\n')
        with self.assertRaises(AssertionError):
            case.expect(((wrong, {}, {}), (wrong, {}, {})), 2, expected[2])

    def test_tree_records_are_compared_without_inode_numbers(self):
        old = {'.': {'kind': 'directory', 'mode': 0o700, 'sha256': None},
               '進行中_例.md': {'kind': 'file', 'mode': 0o644, 'sha256': 'a' * 64}}
        self.m.same_tree(old, copy.deepcopy(old))
        for mutate in (lambda t: t.pop('進行中_例.md'), lambda t: t.update(extra={'kind': 'file', 'mode': 0o644, 'sha256': 'b'}),
                       lambda t: t['進行中_例.md'].update(mode=0o600), lambda t: t['進行中_例.md'].update(sha256='c' * 64)):
            changed = copy.deepcopy(old)
            mutate(changed)
            with self.assertRaises(AssertionError):
                self.m.same_tree(old, changed)

    def test_third_party_entries_must_survive_unchanged(self):
        entry = {'kind': 'file', 'mode': 0o644, 'sha256': 'a' * 64}
        before = {'.': {'kind': 'directory', 'mode': 0o700, 'sha256': None}, '完了_例.md': entry}
        self.m.third_party_intact(before, copy.deepcopy(before), ['完了_例.md'])
        for after in ({'.': before['.']}, {**before, '完了_例.md': {**entry, 'sha256': 'b' * 64}},
                      {**before, '完了_例.md': {**entry, 'kind': 'symlink'}}, {**before, '完了_例.md': {**entry, 'mode': 0o600}}):
            with self.assertRaises(AssertionError):
                self.m.third_party_intact(before, after, ['完了_例.md'])
        with self.assertRaises(AssertionError):
            self.m.third_party_intact({}, {}, ['完了_例.md'])

    # ---- 検査の件数 ----

    def test_run_suite_rejects_zero_skipped_failed_and_missing_cases(self):
        class Good(unittest.TestCase):
            def test_one(self):
                pass

        class Skipped(unittest.TestCase):
            @unittest.skip('skip')
            def test_one(self):
                pass

        class Failing(unittest.TestCase):
            def test_one(self):
                self.fail('x')

        load = unittest.defaultTestLoader.loadTestsFromTestCase
        self.assertEqual(self.m.run_suite(load(Good)), 1)
        for case in (Skipped, Failing):
            with self.assertRaises(AssertionError):
                self.m.run_suite(load(case))
        with self.assertRaises(AssertionError):
            self.m.run_suite(unittest.TestSuite())

    # ---- 観点の表 ----

    def test_map_covers_every_old_method_exactly_once(self):
        methods = self.m.old_test_methods()
        self.assertEqual(len(methods), 26)
        self.assertEqual(self.m.verify_map(), (26, 20))
        table = list(self.m.OLD_TEST_MAP)
        broken = {
            'missing': (table[1:], methods),
            'extra_old_method': (table, methods + ['new_old_test']),
            'duplicate_id': (table + [table[0]], methods),
            'duplicate_name': (table[:-1] + [(table[-1][0], table[0][1], '', None)], methods),
            'unknown_check': ([(table[0][0], table[0][1], '', 'test_absent')] + table[1:], methods),
        }
        for name, (rows, names) in broken.items():
            with self.assertRaises(AssertionError, msg=name):
                self.m.verify_map(methods=names, table=rows)
        with self.assertRaises(AssertionError):
            self.m.verify_map(extra=self.m.EXTRA_GROUPS + (self.m.EXTRA_GROUPS[0],))

    # ---- 実行物の代役による検出 ----

    def stub(self, body):
        script = write_executable(self.dir / 'stub', '#!/bin/sh\n' + body)
        self.m.FIXTURE = script
        return script

    def run_case(self, name):
        suite = unittest.TestSuite([self.m.Contract(name)])
        with open(os.devnull, 'w') as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        self.last_problems = [text for _, text in result.failures + result.errors]
        return result.wasSuccessful()

    def test_stub_connected_to_the_old_script_is_not_a_native_binary(self):
        # 旧版へ素通しする代役は、出力が旧版と同じになるが、入口の確認が先に拒む。
        wrapper = write_executable(self.dir / 'wrapper', f'#!/bin/sh\nexec {sys.executable} -B {self.m.LEGACY_SCRIPT} "$@"\n')
        with self.assertRaises(ValueError):
            self.m.fixture_path(str(wrapper))
        self.m.FIXTURE = wrapper
        self.assertTrue(self.run_case('test_a03_unattended'), self.last_problems)  # 出力だけでは見分けられないので、入口で拒む。

    def test_stub_with_correct_output_passes_and_each_corruption_fails(self):
        message = '候補の採用を停止: 無人実行では候補を採用できない'
        good = f'printf "%s\\n" "{message}" >&2\nexit 2\n'
        self.stub(good)
        self.assertTrue(self.run_case('test_a03_unattended'))
        for body in (good.replace('exit 2', 'exit 1'),
                     good.replace('exit 2', 'echo extra\nexit 2'),
                     good.replace('exit 2', 'echo extra >&2\nexit 2'),
                     'exit 2\n', f'printf "%s" "{message}" >&2\nexit 2\n'):
            self.stub(body)
            self.assertFalse(self.run_case('test_a03_unattended'), body)

    def test_stub_that_times_out_or_deletes_a_third_party_entry_fails(self):
        self.stub('sleep 30\n')
        timed = self.m.run_command([str(self.m.FIXTURE)], cwd=self.dir, env={'PATH': os.environ['PATH']}, timeout=1)
        self.assertTrue(timed.timed_out)
        with self.assertRaises(AssertionError):
            self.m.fixed(timed, 2, b'', b'')
        with self.assertRaises(AssertionError):
            self.m.same_result(self.result(), timed)
        message = '候補の採用を停止: 同じ名の状態ファイルがある'
        self.stub(f'for a; do last=$a; done\nrm -f "$(dirname "$last")/完了_例.md"\nprintf "%s\\n" "{message}" >&2\nexit 2\n')
        self.assertFalse(self.run_case('test_a08_collision_all_states'))

    # ---- 引数の固定期待 ----

    def document(self, count=6):
        cases = json.loads(self.m.ARG_CASES.read_text(encoding='utf-8'))
        usable = [case for case in cases['cases'] if 'parsed' not in case][:count]
        return {'argparse_sha256': cases['argparse_sha256'], 'cases': usable}

    def test_argument_cases_reject_wrong_provenance_and_tampered_expectations(self):
        document = self.document()
        calls = []

        def echo(args, env, cwd, timeout=50):
            case = document['cases'][len(calls)]
            calls.append(case)
            return self.result(case['code'], case['stdout'].encode(), case['stderr'].encode())

        # 旧版は実際に起動する。Ubuntu など本家と違う標準ライブラリの Python では、固定期待の行番号が違い得るため、
        # 比較器の入口(verify_old_python)が先に拒む。ここでは出力の同じ最初のケースだけで検出の動きを確かめる。
        self.m.native = echo
        wrong_origin = dict(document, argparse_sha256='0' * 64)
        with self.assertRaises(AssertionError):
            self.m.argument_cases(wrong_origin)
        tampered = copy.deepcopy(document)
        tampered['cases'][0]['stderr'] += 'x'
        tampered['cases'][0]['stdout'] += 'x'
        calls.clear()
        with self.assertRaises(AssertionError):
            self.m.argument_cases(tampered)
        empty = dict(document, cases=[])
        self.assertEqual(self.m.argument_cases(empty), (0, 0))
        path = self.dir / 'empty.json'
        path.write_text(json.dumps(empty))
        self.m.ARG_CASES = path
        self.assertFalse(self.run_case('test_e01_argument_cases'))

    def test_diagnostic_normalization_maps_only_known_paths_and_one_well_formed_thread_value(self):
        script, stdlib = '/x/y/adopt-candidate.py', '/py/lib/python3.10'
        text = (f'File "{script}", line 1\nFile "{stdlib}/argparse.py", line 2\nFile "/other/argparse.py", line 3\n'
                'Current thread 0x00007f0000001234 (most recent call first):\n').encode()
        out = self.m.normalize_diagnostic(text, script=script, stdlib=stdlib)
        self.assertIn(b'File "adopt-candidate.py", line 1', out)
        self.assertIn(b'File "argparse.py", line 2', out)
        self.assertIn(b'File "/other/argparse.py", line 3', out)
        self.assertIn(b'Current thread 0x<THREAD> (most recent call first):', out)
        bad = b'Current thread 0x1234 (most recent call first):\n'
        self.assertEqual(self.m.normalize_diagnostic(bad), bad)
        twice = (b'Current thread 0x00007f0000001234 (most recent call first):\n') * 2
        with self.assertRaises(AssertionError):
            self.m.normalize_diagnostic(twice)

    def test_faulty_runs_apply_the_size_limit_and_keep_the_partial_output(self):
        code = 'import os,sys;print(os.write(1,b"x"*100),file=sys.stderr);print(os.write(1,b"y"),file=sys.stderr)'
        result = self.m.faulty([sys.executable, '-B', '-c', code], {'PATH': os.environ['PATH']}, stdout=('file', 10))
        self.assertEqual(result[1], b'x' * 10)
        self.assertIn(b'10', result[2])
        full = self.m.faulty([sys.executable, '-B', '-c', 'import os;os.write(1,b"x")'], {'PATH': os.environ['PATH']}, stdout='full')
        self.assertNotEqual(full[0], 0)
        directory = self.m.faulty([sys.executable, '-B', '-c', 'print(1)'], {'PATH': os.environ['PATH']}, stdin='dir')
        self.assertIn(b'<stdin> is a directory', directory[2])

    def test_errno_baseline_rejects_a_machine_that_differs_from_the_reference(self):
        self.assertEqual(self.m.verify_errno_baseline(sys.executable, reference={2: 'No such file or directory', 13: 'Permission denied'}), 2)
        for wrong in ({2: 'No such file or directory!'}, {13: 'Permission denied', 2: ''}, {99999: 'x'}):
            with self.assertRaises(AssertionError):
                self.m.verify_errno_baseline(sys.executable, reference=wrong)
        # 既定の基準は、この機種の OS の表(Linux は glibc、Mac は macOS)。基準の機械なら通る。
        if sys.platform.startswith('linux') and sys.version_info[:3] == (3, 10, 11):
            self.m.verify_errno_baseline(sys.executable)

    def test_unavailable_inputs_are_counted_as_not_run_not_as_success(self):
        before = len(self.m.NOT_RUN)
        with mock.patch.object(os.path, 'exists', return_value=False):
            self.assertTrue(self.m._unavailable({'stdout': 'full'}, 'help・/dev/full'))
            self.assertFalse(self.m._unavailable({'stdout': 'pipe'}, 'help・pipe'))
        self.assertEqual(self.m.NOT_RUN[before:], ['help・/dev/full'])
        del self.m.NOT_RUN[before:]

    def test_evidence_records_three_runs_per_start_up_failure_and_the_machine_facts(self):
        stub = write_executable(self.dir / 'native-stub', '#!/bin/sh\nexit 0\n')
        evidence = self.m.collect_evidence(sys.executable, stub)
        for key in ('platform', 'machine', 'python', 'stdlib_sha256', 'errno', 'fixture'):
            self.assertIn(key, evidence)
        for name in ('stdin-directory', 'stdout-directory', 'stderr-directory'):
            self.assertEqual((len(evidence['runs'][name]['old']), len(evidence['runs'][name]['native'])), (3, 3), name)
        self.assertEqual(len(evidence['runs']['control-help']['old']), 1)
        self.assertIn('help-output-failure-buffered', evidence['runs'])
        self.assertEqual(set(evidence['stdlib_sha256']), set(self.m.UPSTREAM_STDLIB_SHA256))

    def test_fault_scenarios_must_exist_and_old_behaviour_must_match_the_fixed_expectation(self):
        scenarios = self.m.fault_scenarios()
        self.assertGreater(len(scenarios), 60)
        empty = self.dir / 'empty.json'
        empty.write_text('{"scenarios": []}')
        self.m.FAULT_SCENARIOS = empty
        try:
            with self.assertRaises(AssertionError):
                self.m.fault_scenarios()
        finally:
            self.m.FAULT_SCENARIOS = Path(self.m.FIXTURES / 'fault_scenarios.json')
        scenario = next(item for item in scenarios if item['id'] == 'A20-forced-fallback')
        self.m.check_fault_scenario(scenario, native_too=False)
        tampered = copy.deepcopy(scenario)
        tampered['expect']['stderr'] += 'x'
        with self.assertRaises(AssertionError):
            self.m.check_fault_scenario(tampered, native_too=False)
        tampered = copy.deepcopy(scenario)
        tampered['expect']['tree'].pop('進行中_例.md')
        with self.assertRaises(AssertionError):
            self.m.check_fault_scenario(tampered, native_too=False)

    def test_shim_log_is_split_into_cwd_environment_argv_and_stdin(self):
        text = 'ROOT\nFOO=bar\nGIT_NO_LAZY_FETCH=1\nPWD=/x\nSHLVL=1\n_=/usr/bin/env\nR4C_SHIM_MODE=log\nPATH=/a\n__CF_USER_TEXT_ENCODING=0x1F5:0:0\nARGV:ls-files --error-unmatch\nSTDIN:line1\nline2\n'
        parsed = self.m.parse_shim_log(text)
        self.assertEqual(parsed, {'cwd': 'ROOT', 'env': ['FOO=bar', 'GIT_NO_LAZY_FETCH=1', 'PATH=/a'], 'argv': 'ls-files --error-unmatch', 'stdin': 'line1\nline2'})

    def test_deep_roots_have_exactly_the_requested_json_length(self):
        for total in (8192, 8193, 9808):
            with tempfile.TemporaryDirectory(prefix='r4c-deep-') as directory:
                root = self.m.make_deep_root(str(Path(directory).resolve()), total)
                path = root + '/進行中_例.md'.encode()
                self.assertEqual(len(b'{"path": "' + path + b'", "tracked": false, "sha256": "' + b'0' * 64 + b'", "warnings": []}\n'), total)

    def test_unimplemented_items_have_reasons_and_linux_only_scenarios_are_marked(self):
        self.assertTrue(self.m.UNIMPLEMENTED)
        for name, reason in self.m.UNIMPLEMENTED.items():
            self.assertTrue(name and len(reason) > 10)
        for scenario in self.m.fault_scenarios():
            if scenario['group'] == 'E11':
                self.assertEqual(scenario.get('os'), ['linux'], scenario['id'])

    def test_interrupt_scenario_keeps_only_the_last_line_of_a_signalled_traceback(self):
        scenarios = {item['id']: item for item in self.m.fault_scenarios()}
        for identifier in ('A25c-sigint-before-link', 'A25b-sigint-between-link-and-unlink', 'A25-kill-between-link-and-unlink'):
            self.assertIn(identifier, scenarios)
        self.assertEqual(scenarios['A25c-sigint-before-link']['expect']['code'], -signal.SIGINT)
        self.assertEqual(scenarios['A25c-sigint-before-link']['expect']['stderr'], 'KeyboardInterrupt\n')
        self.assertEqual(scenarios['A25b-sigint-between-link-and-unlink']['expect']['code'], 2)

    # ---- 旧版の側と Rust の側の別集計・権限・umask ----

    def test_judge_counts_the_old_side_and_the_rust_side_separately(self):
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        try:
            self.m.judge('old', 'a', (0, b''), (0, b''))
            self.m.judge('native', 'a', (0, b''), (0, b''))
            self.assertEqual((self.m.TALLY['old_match'] - saved['old_match'], self.m.TALLY['native_match'] - saved['native_match']), (1, 1))
            with self.assertRaises(AssertionError):
                self.m.judge('old', 'b', (1, b''), (0, b''))
            with self.assertRaises(AssertionError):
                self.m.judge('native', 'c', (1, 'R4C の依存部品(#471 の候補解析)は未接続です'.encode()), (0, b''))
            with self.assertRaises(AssertionError):
                self.m.judge('native', 'd', (1, b'other'), (0, b''))
            self.assertEqual(self.m.TALLY['old_mismatch'][len(saved['old_mismatch']):], ['b'])
            # 依存部品が未接続で止まったものは、それ以外の不一致とは別に数える(成功には数えない)。
            self.assertEqual(self.m.TALLY['native_unconnected'][len(saved['native_unconnected']):], ['c'])
            self.assertEqual(self.m.TALLY['native_mismatch'][len(saved['native_mismatch']):], ['d'])
        finally:
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)

    def test_judge_sides_checks_the_rust_side_even_when_the_old_side_is_wrong(self):
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        try:
            with self.assertRaises(AssertionError):
                self.m.judge_sides('x', (1, b''), (0, b''), (0, b''))
            self.assertEqual(self.m.TALLY['old_mismatch'][len(saved['old_mismatch']):], ['x'])
            self.assertEqual(self.m.TALLY['native_match'] - saved['native_match'], 1)
            # 側ごとの期待(各側の root の絶対パスを含む出力)。
            self.m.judge_sides('y', (0, b'/a'), (0, b'/b'), {'old': (0, b'/a'), 'native': (0, b'/b')})
            with self.assertRaises(AssertionError):
                self.m.judge_sides('z', (0, b'/a'), (0, b'/a'), {'old': (0, b'/a'), 'native': (0, b'/b')})
        finally:
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)

    def test_fixed_umask_restores_the_previous_value(self):
        before = os.umask(0o077)
        try:
            with self.m.fixed_umask(0o022):
                self.assertEqual(os.umask(0o022), 0o022)
            self.assertEqual(os.umask(0o077), 0o077)
        finally:
            os.umask(before)

    def test_snapshot_records_the_whole_permission_not_only_the_executable_bit(self):
        root = self.dir / 'snap'
        root.mkdir()
        (root / 'a').write_text('x')
        (root / 'a').chmod(0o600)
        (root / 'sub').mkdir()
        (root / 'sub').chmod(0o750)
        (root / 'link').symlink_to('a')
        first = self.m._snapshot(root)
        self.assertEqual((first['a']['mode'], first['sub']['mode']), (0o600, 0o750))
        self.assertNotIn('mode', first['link'])
        self.assertNotIn('exec', first['a'])
        (root / 'a').chmod(0o644)
        second = self.m._snapshot(root)
        with self.assertRaises(AssertionError):
            self.m.same_tree(first, second)
        (root / 'a').chmod(0o666)
        third = self.m._snapshot(root)
        self.assertNotEqual(second['a'], third['a'])  # 実行権限の有無は同じでも、権限の違いを見分ける。

    def test_large_json_expectations_follow_the_measured_flush_io_rule(self):
        table = self.m.LARGE_JSON_EXPECTED
        # スクリプトの実行の直後の flush_io が失敗を捨てる区間: 4096 を超え 8192 未満・上限 0 は、終了 0 で何も出ない。
        for total in (4097, 5000, 8191):
            self.assertEqual(table[(total, 0)], (0, 0, 'quiet'))
        self.assertEqual(table[(4096, 0)][2], 'unraisable')
        # 8192・8193 バイトで上限 5000 は、print の中ではなく終了時の flush の失敗(終了 120)。
        for total in (8192, 8193):
            self.assertEqual(table[(total, 5000)], (120, 5000, 'unraisable'))
        self.assertEqual(sorted({kind for _, _, kind in table.values()}), ['quiet', 'stop', 'unraisable'])

    def test_every_shim_mode_used_by_the_comparator_is_defined_in_the_shim(self):
        for mode in ('log', 'trap-fast', 'trap-slow', 'slow'):
            self.assertIn(mode + ')', self.m.GIT_SHIM)

    def test_long_stderr_table_is_complete_and_the_race_runs_are_recorded(self):
        table = self.m.LONG_STDERR_EXPECTED
        self.assertEqual(len(table), 30)
        self.assertEqual({key[1] for key in table}, {'file0', 'file100', 'file5000', 'full', 'pipe'})
        self.assertEqual(table[(5000, 'file5000', False)], (120, 5000))
        self.assertEqual(table[(5000, 'file5000', True)], (2, 5000))
        self.assertGreaterEqual(self.m.RACE_RUNS, 20)

    def test_shim_writes_the_pid_after_installing_the_trap(self):
        for mode in ('trap-fast', 'trap-slow'):
            line = next(row for row in self.m.GIT_SHIM.splitlines() if row.startswith(mode + ')'))
            self.assertLess(line.index("trap '"), line.index('shim.pid'), mode)

    def test_run_suite_prints_the_breakdown_only_when_asked(self):
        import contextlib
        import io

        class Failing(unittest.TestCase):
            def test_one(self):
                self.fail('x')
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        for breakdown in (False, True):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), self.assertRaises(AssertionError):
                self.m.run_suite(load(Failing), breakdown=breakdown)
            self.assertEqual('依存部品' in stream.getvalue(), breakdown)

    def test_race_reference_set_comes_from_ordered_cases_and_does_not_depend_on_samples(self):
        kinds = self.m.race_reference_kinds()
        src, dest = '候補_例.md', '進行中_例.md'
        rollback = '候補の採用を停止: 非原子的な採用を完了できない(自身が作った宛先を戻した)'
        self.assertIn((2, (src,), False, rollback), kinds)       # A25b: link と unlink の間
        self.assertIn((2, (), False, rollback), kinds)           # A25e: unlink の直後(どちらの名前も残らない)
        self.assertIn((-2, (dest,), False, 'KeyboardInterrupt'), kinds)       # A25d: try の外
        self.assertIn((-2, (src, dest), False, 'KeyboardInterrupt'), kinds)   # A25f: os.stat(dest) の間(両方の名前が残る)
        self.assertIn((-2, (dest,), True, 'KeyboardInterrupt'), kinds)  # 出力の後・終了の前
        self.assertIn((0, (dest,), True, ''), kinds)             # 信号が届く前に完了
        self.assertEqual(kinds, self.m.race_reference_kinds())   # 何度作っても同じ(時間に頼らない)

    def test_comparison_environment_is_explicit(self):
        env = self.m.environment(columns='40', marker='run-1')
        self.assertEqual((env['PYTHONUTF8'], env['LC_ALL'], env['COLUMNS'], env['DEV_WORKFLOW_LOOP_ITER']),
                         ('1', 'C.UTF-8', '40', 'run-1'))
        self.assertNotIn('COLUMNS', self.m.environment())
        self.assertEqual(self.m.environment()['DEV_WORKFLOW_LOOP_ITER'], '')

    def test_git_ancestor_of_the_scratch_folder_is_refused(self):
        self.m.check_no_git_ancestor(self.dir)
        (self.dir / '.git').write_text('gitdir: /elsewhere')
        with self.assertRaises(AssertionError):
            self.m.check_no_git_ancestor(self.dir / 'child')


if __name__ == '__main__':
    unittest.main()
