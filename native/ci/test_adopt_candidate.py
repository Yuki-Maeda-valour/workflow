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
import types
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
        self.assertEqual(self.m.verify_map(), (26, 26))
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

    def run_deep_root(self, base, total):
        """`make_deep_root` を、名前と親子の記録だけをする代役の `os` で流す。(root, 記録) を返す。"""
        fds, made, files, opened, written = {}, [], [], [], []

        def fake_open(name, flags, mode=0o777, dir_fd=None):
            fd = max(fds, default=2) + 1
            if dir_fd is None:
                self.assertEqual(opened, [], 'dir_fd 無しで開くのは最初の base だけ')
                fds[fd] = os.fsencode(name)
            else:
                self.assertIsInstance(name, (bytes, str))
                self.assertNotIn(b'/', os.fsencode(name))
                if flags & os.O_CREAT:
                    files.append((fds[dir_fd], name))
                    fds[fd] = None
                else:
                    self.assertIn((fds[dir_fd], name), made, '開く前に同じ親で mkdir していない')
                    fds[fd] = fds[dir_fd] + b'/' + name
            opened.append(fd)
            return fd

        def fake_mkdir(name, dir_fd=None):
            self.assertIsNotNone(dir_fd)
            self.assertNotIn(b'/', name)
            made.append((fds[dir_fd], name))

        def fake_close(fd):
            del fds[fd]

        fake_os = types.SimpleNamespace(
            O_RDONLY=os.O_RDONLY, O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, fsencode=os.fsencode,
            open=fake_open, mkdir=fake_mkdir, close=fake_close, write=lambda handle, data: written.append(data) or len(data))
        with mock.patch.object(self.m, 'os', fake_os):
            root = self.m.make_deep_root(base, total)
        self.assertEqual(fds, {}, '開いたままの fd が残った')
        return root, made, files, written

    def test_deep_roots_have_exactly_the_requested_json_length(self):
        # 実際の深いフォルダは作らない: 容器の AppArmor(docker-default)がパスの全体を約 8 KiB で拒否するため。
        # 実際に作る経路は、比較器の本体(E13b など)が CI で確かめる。ここでは名前と親子の記録だけで計算を確かめる。
        for total in (8192, 8193, 9808):
            for length in range(1, 261):
                base = '/' + 'b' * (length - 1)
                root, made, files, written = self.run_deep_root(base, total)
                path = root + '/進行中_例.md'.encode()
                self.assertEqual(len(b'{"path": "' + path + b'", "tracked": false, "sha256": "' + b'0' * 64 + b'", "warnings": []}\n'), total, (total, length))
                self.assertTrue(made and all(0 < len(name) <= 200 for _, name in made))
                self.assertEqual(root, os.fsencode(base) + b''.join(b'/' + name for _, name in made))
                self.assertEqual([parent for parent, _ in made], [root[:len(root) - sum(len(n) + 1 for _, n in made[i:])] for i in range(len(made))])
                self.assertEqual(files, [(root, '候補_例.md')])
                self.assertEqual(written, [self.m.CANDIDATE.encode()])

    def test_deep_root_refuses_lengths_it_cannot_make(self):
        fixed = len('{"path": "') + len('", "tracked": false, "sha256": "') + 64 + len('", "warnings": []}\n') + len('進行中_例.md'.encode()) + 1
        for total, need in ((fixed + 10 + 1, 1), (fixed + 10 - 1, -1)):
            with self.assertRaises(ValueError):
                self.run_deep_root('/' + 'b' * 9, total)

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

    # ---- 事前検査(S3D)への接続 ----

    def fake_child_mode(self, *, native_change=None, both_wrong=False):
        """子のモードの代役。基準(S3D の実行物)と Rust の実行物を、引数と作業場所の名前だけで決まる結果で返す。"""
        digest = 'a' * 64

        def run(binary, args, cwd, env, **conditions):
            name = Path(cwd).name
            native = str(binary) == 'native'
            if '--cwd' not in args:
                result = (2, b'', 'ERROR [usage] --cwd が必要です\n'.encode())
            elif '--bogus' in args:
                result = (2, b'', 'ERROR [usage] 不明な引数: --bogus\n'.encode())
            elif name == 'not-git':
                result = (2, b'', 'ERROR [usage] --cwd が git リポジトリの中でない\n'.encode())
            elif name == 'clean':
                result = (20, b'', 'ERROR [internal] 終了コード 153\n'.encode()) if conditions.get('fsize') is not None else (0, b'', b'')
            elif '--accept' in args and args[args.index('--accept') + 1] == digest:
                result = (0, b'', b'')
            elif '--accept' in args:
                result = (22, b'', '承認ダイジェストが一致しない\n'.encode())
            else:
                result = (22, b'', f'承認ダイジェスト: {digest}\n'.encode())
            if both_wrong and name == 'clean':
                result = (1, b'', b'x')
            if native and native_change:
                result = native_change(result, name, args, conditions) or result
            return self.m.RunResult(result[0], result[1], result[2], False)
        return run

    def run_child_mode(self, run):
        saved = (self.m.FIXTURE, self.m.DS_FIXTURE, self.m.TALLY.copy())
        self.m.FIXTURE, self.m.DS_FIXTURE = Path('native'), Path('reference')
        try:
            with mock.patch.object(self.m, 'run_named', run):
                case = self.m.Contract('test_e19_child_mode')
                getattr(case, 'test_e19_child_mode')()
        finally:
            self.m.FIXTURE, self.m.DS_FIXTURE, tally = saved
            self.m.TALLY.clear()
            self.m.TALLY.update(tally)

    def test_child_mode_check_passes_only_when_the_executable_equals_the_reference_in_every_case(self):
        self.run_child_mode(self.fake_child_mode())
        # Rust の実行物だけが違う(終了値・標準エラーのどれか 1 つ)なら失敗にする。
        for change in (lambda r, name, args, c: (r[0] + 1, r[1], r[2]) if name == 'needs' and '--accept' in args else None,
                       lambda r, name, args, c: (r[0], r[1], r[2] + b' ') if name == 'not-git' else None,
                       lambda r, name, args, c: (r[0], b'extra', r[2]) if c.get('ignore_sigint') else None,
                       lambda r, name, args, c: (0, b'', b'') if c.get('fsize') is not None else None):
            with self.assertRaises(AssertionError):
                self.run_child_mode(self.fake_child_mode(native_change=change))
        # 基準と Rust が同じ誤値でも、固定した期待(承認の要らない構成は終了 0)と違えば失敗にする。
        with self.assertRaises(AssertionError):
            self.run_child_mode(self.fake_child_mode(both_wrong=True))

    def test_child_mode_runs_the_executable_under_the_fixed_argv0_not_the_path(self):
        done = self.m.run_named('/bin/sh', ['-c', 'echo "$0"'], self.dir, self.m.environment())
        self.assertEqual((done.returncode, done.stdout), (0, b'diff-snapshot.sh\n'))
        ignored = self.m.run_named('/bin/sh', ['-c', 'kill -INT $$; echo survived'], self.dir, self.m.environment(), ignore_sigint=True)
        self.assertIn(b'survived', ignored.stdout)
        limited = self.m.run_named('/bin/sh', ['-c', 'ulimit -f'], self.dir, self.m.environment(), fsize=0)
        self.assertEqual(limited.stdout.strip(), b'0')

    def test_precheck_log_is_split_without_dropping_path(self):
        text = '<ROOT>\nFOO=bar\nPWD=/x\nSHLVL=2\n_=/usr/bin/env\nR4C_SHIM_MODE=log\nR4C_REAL_BASH=/bin/bash\nPATH=/a:/b\nARGV:/g/diff-snapshot.sh --cwd <ROOT> --precheck\nSTDIN:data\n'
        self.assertEqual(self.m.parse_precheck_log(text), {'cwd': '<ROOT>', 'env': ['FOO=bar', 'PATH=/a:/b'],
                                                          'argv': '/g/diff-snapshot.sh --cwd <ROOT> --precheck', 'stdin': 'data'})

    def test_legacy_prerequisites_fail_on_linux_and_are_not_run_on_mac(self):
        saved = list(self.m.NOT_RUN)
        try:
            with mock.patch.object(self.m, 'legacy_prerequisites', return_value=(False, 'bash が 4.0 未満(3)')):
                with mock.patch.object(self.m.sys, 'platform', 'linux'), self.assertRaises(AssertionError):
                    self.m.legacy_guard_ready('x')
                self.assertEqual(self.m.NOT_RUN, saved)
                with mock.patch.object(self.m.sys, 'platform', 'darwin'):
                    self.assertFalse(self.m.legacy_guard_ready('x'))
                self.assertEqual(self.m.NOT_RUN[len(saved):], ['x: 旧版の前提を用意できない(bash が 4.0 未満(3))'])
            with mock.patch.object(self.m, 'legacy_prerequisites', return_value=(True, '')):
                self.assertTrue(self.m.legacy_guard_ready('x'))
        finally:
            self.m.NOT_RUN[:] = saved

    def test_version_dependent_cases_fail_on_linux_and_are_not_run_on_mac_when_not_the_base(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from diff_snapshot_fixtures import launch_sites
        saved = list(self.m.NOT_RUN)
        try:
            with mock.patch.object(launch_sites, 'is_base', return_value=(False, {'bash': '5.1.16'})):
                with mock.patch.object(self.m.sys, 'platform', 'linux'), self.assertRaises(AssertionError):
                    self.m.version_ready('v')
                with mock.patch.object(self.m.sys, 'platform', 'darwin'):
                    self.assertFalse(self.m.version_ready('v'))
                self.assertEqual(self.m.NOT_RUN[len(saved):], ['v: 版に依る欄を基準の版と照らせない(version)'])
            with mock.patch.object(launch_sites, 'is_base', return_value=(True, {})):
                self.assertTrue(self.m.version_ready('v'))
        finally:
            self.m.NOT_RUN[:] = saved

    def test_prerequisites_detect_an_old_bash_and_a_missing_tool(self):
        def fake(argv, **kwargs):
            tool = argv[0]
            out = {'bash': '3\n', 'sed': 'sed (GNU sed) 4.9\n'}.get(tool, 'x\n')
            return mock.Mock(stdout=out)
        with mock.patch.object(self.m.subprocess, 'run', side_effect=fake):
            self.assertEqual(self.m.legacy_prerequisites(), (False, 'bash が 4.0 未満(3)'))
        def fake_gnu(argv, **kwargs):
            if argv[0] == 'bash':
                return mock.Mock(stdout='5\n')
            if argv[0] == 'grep':
                return mock.Mock(stdout='grep (BSD grep) 2.6\n')
            return mock.Mock(stdout=f'{argv[0]} (GNU tool) 1\n')
        with mock.patch.object(self.m.subprocess, 'run', side_effect=fake_gnu):
            self.assertEqual(self.m.legacy_prerequisites(), (False, 'grep が GNU の版でない'))

    def test_prerequisites_do_not_require_gnu_sed_and_record_the_versions(self):
        def fake(argv, **kwargs):
            tool = argv[0]
            if tool == 'bash':
                return mock.Mock(stdout='5\n')
            if tool == 'sed':
                return mock.Mock(stdout='sed: illegal option -- -\n')  # BSD の sed(Mac の既定)
            return mock.Mock(stdout=f'{tool} (GNU coreutils) 9.4\n')
        self.m.LEGACY_TOOL_VERSIONS.clear()
        with mock.patch.object(self.m.subprocess, 'run', side_effect=fake):
            self.assertEqual(self.m.legacy_prerequisites(), (True, ''))
        self.assertEqual(self.m.LEGACY_TOOL_VERSIONS['bash'], 5)
        self.assertNotIn('sed', self.m.LEGACY_TOOL_VERSIONS)
        self.assertIn('GNU', self.m.LEGACY_TOOL_VERSIONS['grep'])

    def test_the_bash_shim_keeps_argv0_and_only_the_observed_case_uses_it(self):
        self.assertIn('exec -a bash "$R4C_REAL_BASH"', self.m.BASH_SHIM)
        self.assertTrue(self.m.BASH_SHIM.startswith('#!/bin/bash'))
        import inspect
        self.assertFalse(inspect.signature(self.m.run_precheck).parameters['bash_shim'].default)

    def test_tools_missing_check_keeps_comparing_after_a_version_failure_and_counts_it_as_other(self):
        calls = []

        def fake_run(which, **kwargs):
            calls.append(sorted(kwargs))
            return {'code': 2, 'stdout': '', 'stderr': "候補の採用を停止: [Errno 2] No such file or directory: 'bash'\n",
                    'tree_after': {}, 'tree_before': {}}
        case = self.m.Contract('test_e21_precheck_tools_missing')
        result = unittest.TestResult()
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        try:
            with mock.patch.object(self.m, 'legacy_guard_ready', return_value=True), \
                    mock.patch.object(self.m, 'version_ready', side_effect=AssertionError('版が基準と違う')), \
                    mock.patch.object(self.m, 'run_precheck', side_effect=fake_run), \
                    mock.patch.object(self.m, 'legacy_tree', return_value=(Path('s'), Path('h'))):
                case.run(result)
            tally = {key: value for key, value in self.m.TALLY.items()}
        finally:
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)
        texts = [text for _, text in result.failures]
        # 版の失敗は、そのケースだけの失敗(未接続の文を含まない)。版に依らない bash-missing は比べ続けた。
        self.assertTrue(any('版が基準と違う' in text and '未接続' not in text for text in texts), texts)
        self.assertIn(['accept', 'bash_shim', 'path_dirs'], calls)
        # 承認済みの差は、通常の一致件数(old・native)に入れず、別の区分で数える。
        self.assertEqual((tally['old_match'], tally['native_match']), (saved['old_match'], saved['native_match']))
        self.assertGreaterEqual(tally['approved_old_match'] + len(tally['approved_native_mismatch']) + tally['approved_native_match'], 1)

    def test_reference_and_approved_results_are_counted_apart_from_the_old_and_native_ones(self):
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        try:
            self.m.judge_run('reference', 'r', lambda: None)
            self.m.judge_run('approved_old', 'a', lambda: None)
            with self.assertRaises(AssertionError):
                self.m.judge_run('reference', 'r2', lambda: self.fail('x'))
            with self.assertRaises(AssertionError):
                self.m.judge_run('approved_native', 'a2', lambda: self.fail('未接続です'))
            with self.assertRaises(AssertionError):
                self.m.judge_run('approved_native', 'a3', lambda: self.fail('違う'))
            counts = (self.m.TALLY['reference_match'] - saved['reference_match'], self.m.TALLY['reference_mismatch'][len(saved['reference_mismatch']):],
                      self.m.TALLY['approved_old_match'] - saved['approved_old_match'], self.m.TALLY['approved_native_unconnected'][len(saved['approved_native_unconnected']):],
                      self.m.TALLY['approved_native_mismatch'][len(saved['approved_native_mismatch']):])
            self.assertEqual(counts, (1, ['r2'], 1, ['a2'], ['a3']))
            self.assertEqual((self.m.TALLY['old_match'], self.m.TALLY['native_match']), (saved['old_match'], saved['native_match']))
            self.assertEqual((self.m.TALLY['old_mismatch'], self.m.TALLY['native_mismatch']), (saved['old_mismatch'], saved['native_mismatch']))
        finally:
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)

    def test_exact_argv0_check_rejects_a_fixture_that_enters_the_child_mode_on_a_near_name(self):
        saved = self.m.FIXTURE
        try:
            self.m.FIXTURE = Path('native')
            ok = self.m.RunResult(2, b'', b'usage: adopt-candidate.py [-h]\n', False)
            leak = self.m.RunResult(2, b'', b'ERROR [usage] --cwd', False)
            with mock.patch.object(self.m, 'run_named', return_value=ok) as run:
                case = self.m.Contract('test_e19d_child_mode_requires_exact_argv0')
                case.test_e19d_child_mode_requires_exact_argv0()
            self.assertEqual({call.kwargs['argv0'] for call in run.call_args_list}.__len__(), 5)
            self.assertTrue(any(call.kwargs['argv0'].endswith('/diff-snapshot.sh') and call.kwargs['argv0'] != 'diff-snapshot.sh' for call in run.call_args_list))
            with mock.patch.object(self.m, 'run_named', return_value=leak), self.assertRaises(AssertionError):
                self.m.Contract('test_e19d_child_mode_requires_exact_argv0').test_e19d_child_mode_requires_exact_argv0()
        finally:
            self.m.FIXTURE = saved

    def test_require_userns_turns_the_unavailable_proc_hiding_into_a_failure(self):
        saved = (self.m.REQUIRE_USERNS, list(self.m.NOT_RUN))
        try:
            with mock.patch.object(self.m, 'proc_hidden_prefix', return_value=(None, 'unshare が使えない')), \
                    mock.patch.object(self.m, 'legacy_guard_ready', return_value=True):
                for name in ('test_e19b_child_mode_without_proc', 'test_e20b_precheck_without_proc'):
                    self.m.REQUIRE_USERNS = False
                    getattr(self.m.Contract(name), name)()
                    self.assertTrue(self.m.NOT_RUN[-1].endswith('unshare が使えない'))
                    self.m.REQUIRE_USERNS = True
                    with self.assertRaises(AssertionError):
                        getattr(self.m.Contract(name), name)()
        finally:
            self.m.REQUIRE_USERNS = saved[0]
            self.m.NOT_RUN[:] = saved[1]

    def test_legacy_tree_copy_removes_or_breaks_only_the_copy_and_never_touches_plugins(self):
        before = (self.m.ROOT / 'plugins/dev-workflow/skills/create-task/scripts/candidate-keys.py').read_bytes()
        script, helper = self.m.legacy_tree(self.dir / 'a', guard='missing', remove=('task-digest.py',),
                                            replace={'candidate-keys.py': 'def broken(:\n'})
        scripts = script.parent
        self.assertEqual(script.name, 'adopt-candidate.py')
        self.assertFalse((scripts / 'task-digest.py').exists())
        self.assertEqual((scripts / 'candidate-keys.py').read_text(), 'def broken(:\n')
        self.assertFalse(helper.exists())
        self.assertTrue((scripts / 'resolve-task-dir.py').exists())
        self.assertEqual((self.m.ROOT / 'plugins/dev-workflow/skills/create-task/scripts/candidate-keys.py').read_bytes(), before)
        self.assertTrue((self.m.ROOT / 'plugins/dev-workflow/skills/create-task/scripts/task-digest.py').exists())

    def test_a_dependency_not_connected_message_from_the_rust_side_is_still_a_failure_and_counted_apart(self):
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        try:
            with self.assertRaises(AssertionError):
                self.m.judge_run('native', 'x', lambda: self.fail('候補の採用を停止: R4C の依存部品(#471 の候補解析)は未接続です'))
            self.assertEqual(self.m.TALLY['native_unconnected'][len(saved['native_unconnected']):], ['x'])
            self.assertEqual(self.m.TALLY['native_mismatch'], saved['native_mismatch'])
        finally:
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)

    def test_candidate_analysis_check_counts_a_malformed_output_as_a_side_mismatch_and_goes_on(self):
        # JSON でない・`warnings` が無い・要素に `code` が無い・要素が辞書でない・`warnings` が配列でない出力は、どれも側ごとの不一致。
        for bad_output in (b'not json', b'{}', b'{"warnings": [{}]}', b'{"warnings": [1]}', b'{"warnings": 5}', b'[]'):
            with self.subTest(output=bad_output):
                cases = [('first', b'x', 0, ['checkbox']), ('second', b'y', 0, [])]
                calls = []

                def fake_pair(body=None, **kwargs):
                    calls.append(body)
                    good = self.m.RunResult(0, json.dumps({'warnings': [{'code': 'checkbox'}] if body == b'x' else []}).encode(), b'', False)
                    bad = self.m.RunResult(0, bad_output, b'', False)
                    return (good, {}, {}), (bad if body == b'x' else good, {}, {})
                saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
                try:
                    with mock.patch.object(self.m, '_candidate_analysis_cases', return_value=cases), mock.patch.object(self.m, 'pair', fake_pair):
                        with self.assertRaises(AssertionError) as caught:
                            self.m.Contract('test_e04b_candidate_analysis').test_e04b_candidate_analysis()
                    tally = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
                finally:
                    self.m.TALLY.clear()
                    self.m.TALLY.update(saved)
                self.assertEqual(calls, [b'x', b'y'])  # 最初のケースの不一致の後も、残りのケースが流れる
                self.assertIn('標準出力が採用の JSON でない', str(caught.exception))
                self.assertEqual(tally['native_mismatch'][len(saved['native_mismatch']):], ['e04b:first'])
                self.assertEqual(tally['old_mismatch'], saved['old_mismatch'])

    # ---- 起動時に閉じた標準の fd(E24) ----

    TEXTS = {'usage': b'usage: adopt-candidate.py [-h]\nadopt-candidate.py: error: x\n', 'help': b'usage: adopt-candidate.py [-h]\nhelp\n',
             'tracked-stop': b'\xe5\x80\x99refused\n'}

    def test_closed_fd_expectations_follow_the_old_python_behaviour(self):
        t = self.TEXTS
        e = lambda path, closed, devnull=False, parity=None: self.m.closed_expectation(path, closed, devnull, parity, t)  # noqa: E731
        digest = __import__('hashlib').sha256(self.m.CANDIDATE.encode()).hexdigest()
        success = self.m._stdout_for(digest)
        stop = self.m.stop('候補の sha256 が保持値と違う')
        self.assertEqual(e('success', (0,)), (0, {1: success, 2: None}))
        self.assertEqual(e('success', (1,)), (0, {1: None, 2: None}))
        # 標準エラーを閉じたままの旧版は、停止の文を標準出力へ回す。`/dev/null` にした旧版・標準出力も閉じた旧版は、どこにも出ない。
        self.assertEqual(e('stop', (2,)), (2, {1: stop, 2: None}))
        self.assertEqual(e('stop', (2,), True), (2, {1: None, 2: None}))
        self.assertEqual(e('stop', (1, 2)), (2, {1: None, 2: None}))
        self.assertEqual(e('stop', (1,)), (2, {1: None, 2: stop}))
        # 使い方の表示は、標準エラーが無いと標準出力へ(使い方の部分だけ)出て終了値は 1。`/dev/null` にすると終了値は 2。
        self.assertEqual(e('usage', (0,)), (2, {1: None, 2: t['usage']}))
        self.assertEqual(e('usage', (2,)), (1, {1: b'usage: adopt-candidate.py [-h]\n', 2: None}))
        self.assertEqual(e('usage', (2,), True), (2, {1: None, 2: None}))
        self.assertEqual(e('usage', (1, 2)), (1, {1: None, 2: None}))
        # help は、標準出力が無いと標準エラーへ回り、どちらも無いと終了値 1。`/dev/null` にした旧版は終了 0。
        self.assertEqual(e('help', (1,)), (0, {1: None, 2: t['help']}))
        self.assertEqual(e('help', (2,)), (0, {1: t['help'], 2: None}))
        self.assertEqual(e('help', (1, 2)), (1, {1: None, 2: None}))
        self.assertEqual(e('help', (1, 2), True), (0, {1: None, 2: None}))

    def test_old_root_fd_takes_the_two_smallest_free_numbers_without_a_fixed_upper_bound(self):
        self.assertEqual([self.m.old_root_fd(c, p) for c in ((), (0,), (1, 2)) for p in (0, 1)], [3, 4, 0, 3, 1, 2])

    def test_closed_fd_expectations_for_the_tracked_path_depend_on_the_parity_of_the_root_depth(self):
        t = self.TEXTS
        e = lambda path, closed, parity, devnull=False: self.m.closed_expectation(path, closed, devnull, parity, t)  # noqa: E731
        # 保持する root の fd は、空いている小さい 2 つの番号(閉じた fd と 3 以上)の、偶数の階層なら小さい方・奇数なら大きい方。
        self.assertEqual([self.m.old_root_fd(c, 0) for c in ((0,), (1,), (2,), (1, 2), (0, 1, 2))], [0, 1, 2, 1, 0])
        self.assertEqual([self.m.old_root_fd(c, 1) for c in ((0,), (1,), (2,), (1, 2), (0, 1, 2))], [3, 3, 3, 2, 1])
        for closed, fails in (((0,), (False, False)), ((1,), (True, False)), ((2,), (True, False)), ((1, 2), (True, True)),
                              ((0, 1, 2), (False, True))):
            for parity in (0, 1):
                code, streams = e('tracked-success', closed, parity)
                if fails[parity]:
                    self.assertEqual(code, 2, (closed, parity))
                    shown = streams[2] if 2 not in closed else streams[1]
                    if (2 in closed and 1 in closed):
                        self.assertEqual(streams, {1: None, 2: None})
                    else:
                        self.assertEqual(shown, self.m.PREEXEC_FAILURE)
                else:
                    self.assertEqual(code, 0, (closed, parity))
                    if 1 not in closed:
                        self.assertIn(b'"tracked": true', streams[1])
                # `/dev/null` にした旧版は、階層に依らず通常どおり。
                self.assertEqual(e('tracked-success', closed, parity, True)[0], 0)
        # 拒否の文(事前検査)も、root の fd が 1 か 2 のときは、前処理の失敗の文に変わる。
        self.assertEqual(e('tracked-stop', (1,), 0), (2, {1: None, 2: self.m.PREEXEC_FAILURE}))
        self.assertEqual(e('tracked-stop', (2,), 1)[1][1], t['tracked-stop'])

    def test_closed_runner_really_closes_or_reopens_the_standard_streams(self):
        saved = self.m.FIXTURE
        self.m.FIXTURE = Path('/bin/sh')
        # 終了値で、fd 0・1・2 が開いているかを返す(開いていなければ 1・2・4 を足す)。確かめは、どの fd も付け替えずに行う。
        script = ['-c', 's=0; ( exec 9>&2 ) || s=$((s+4)); ( exec 9<&0 ) || s=$((s+1)); ( exec 9>&1 ) || s=$((s+2)); echo out; echo err >&2; exit $s']
        try:
            def run(closed, devnull):
                return self.m.closed_runner('native', closed, devnull)(script, self.m.environment(), self.dir)
            plain = run((), False)
            self.assertEqual((plain.returncode, plain.stdout, plain.stderr), (0, b'out\n', b'err\n'))
            for closed, code in (((0,), 1), ((1,), 2), ((2,), 4), ((1, 2), 6), ((0, 1, 2), 7)):
                self.assertEqual(run(closed, False).returncode, code, closed)  # 閉じたまま
                reopened = run(closed, True)
                self.assertEqual((reopened.returncode, reopened.stdout), (0, b'' if 1 in closed else b'out\n'), closed)  # /dev/null で開き直す(fd 1 を閉じていなければ出力は残る)
            self.assertEqual(run((1, 2), True).stderr, b'')
        finally:
            self.m.FIXTURE = saved

    def run_closed_check(self, perturb_old=False, perturb_native=False, extra_native_stderr=False):
        cases = (('stop', (2,), None), ('usage', (2,), None), ('help', (1, 2), None))
        texts = self.TEXTS

        def factory(which, closed, devnull):
            def runner(args, env, cwd, timeout=60):
                path = 'usage' if not args else ('help' if list(args) == ['-h'] else 'stop')
                code, streams = self.m.closed_expectation(path, closed, devnull or which == 'native', None, texts)
                out, err = streams[1] or b'', streams[2] or b''
                if (perturb_old and which == 'old' and closed and not devnull) or (perturb_native and which == 'native'):
                    code += 1
                if extra_native_stderr and which == 'native':
                    err += b'extra\n'  # 語は残し、標準エラーに 1 行足す
                return self.m.RunResult(code, out, err, False)
            return runner
        saved = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
        approved_before = len(self.m.APPROVED_DIFFERENCE_RUN)
        try:
            fixed = lambda args, columns=None: (texts['help'], b'') if list(args) == ['-h'] else (b'', texts['usage'])  # noqa: E731
            with mock.patch.object(self.m, 'CLOSED_CASES', cases), mock.patch.object(self.m, 'closed_runner', factory), \
                    mock.patch.object(self.m, 'fixed_text', fixed):
                result = unittest.TestResult()
                self.m.Contract('test_e24_closed_std_fds').run(result)
            tally = {key: (list(value) if isinstance(value, list) else value) for key, value in self.m.TALLY.items()}
            self.approved_added = len(self.m.APPROVED_DIFFERENCE_RUN) - approved_before
        finally:
            del self.m.APPROVED_DIFFERENCE_RUN[approved_before:]
            self.m.TALLY.clear()
            self.m.TALLY.update(saved)
        return result, tally, saved

    def test_closed_fd_check_passes_when_old_and_rust_follow_their_expectations(self):
        result, tally, saved = self.run_closed_check()
        self.assertEqual(([text for _, text in result.failures], result.errors), ([], []))
        self.assertEqual(tally['approved_old_match'] - saved['approved_old_match'], 9)
        self.assertEqual(tally['approved_native_match'] - saved['approved_native_match'], 3)
        self.assertEqual(self.approved_added, 3)  # 全部通ったケースだけが、承認済みの差の一覧に増える
        # 通常の一致件数には入らない。
        self.assertEqual((tally['old_match'], tally['native_match']), (saved['old_match'], saved['native_match']))

    def test_closed_fd_check_fails_when_the_old_expectation_is_broken(self):
        result, tally, saved = self.run_closed_check(perturb_old=True)
        self.assertTrue(result.failures)
        self.assertGreater(len(tally['approved_old_mismatch']), len(saved['approved_old_mismatch']))
        self.assertEqual(self.approved_added, 0)  # 変異を入れたら、一覧に増えない

    def test_closed_fd_check_fails_when_rust_differs_from_the_devnull_old(self):
        result, tally, saved = self.run_closed_check(perturb_native=True)
        self.assertTrue(result.failures)
        self.assertGreater(len(tally['approved_native_mismatch']), len(saved['approved_native_mismatch']))
        self.assertEqual(self.approved_added, 0)

    def test_closed_fd_check_fails_when_rust_adds_one_line_to_stderr_and_keeps_the_words(self):
        result, tally, saved = self.run_closed_check(extra_native_stderr=True)
        self.assertTrue(result.failures)
        self.assertGreater(len(tally['approved_native_mismatch']), len(saved['approved_native_mismatch']))
        self.assertEqual(tally['approved_old_mismatch'], saved['approved_old_mismatch'])
        self.assertEqual(self.approved_added, 0)

    def test_the_new_checks_are_in_the_observation_table(self):
        names = {name for name in dir(self.m.Contract) if name.startswith('test_')}
        for wanted in ('test_e19_child_mode', 'test_e19b_child_mode_without_proc', 'test_e19c_child_mode_sigint', 'test_e19d_child_mode_requires_exact_argv0', 'test_e20_precheck_observation',
                       'test_e20b_precheck_without_proc', 'test_e20c_precheck_bash_warning',
                       'test_e21_precheck_tools_missing', 'test_e22_precheck_deadline', 'test_e23_candidate_helpers_missing',
                       'test_e04b_candidate_analysis', 'test_e07b_precheck_accept', 'test_e24_closed_std_fds'):
            self.assertIn(wanted, names)
        with self.assertRaises(AssertionError):
            self.m.verify_map(extra=self.m.EXTRA_GROUPS + (('E99', 'x', 'test_e99_absent'),))


if __name__ == '__main__':
    unittest.main()
