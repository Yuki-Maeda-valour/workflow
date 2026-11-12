#!/usr/bin/env python3
"""R4G比較器の自己検査。欠落・誤値・余計な出力・所在地の欄の取り違え・一覧の外の差を拒否させる。"""
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

subject = None


def get_subject():
    global subject
    if subject is None:
        spec = importlib.util.spec_from_file_location('r4g_subject', Path(__file__).with_name('check_r4g.py'))
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


class Comparator(unittest.TestCase):
    """外部の Rust のビルドも本家の Python も使わない検査。全回帰テストでも流す。"""

    def setUp(self):
        self.m = get_subject()
        # 自己検査では、どの決定にも 'self-test' のケースを足した一覧で当てる(本物の一覧は別の検査で見る)。
        patched = {name: dict(item, cases=list(item['cases']) + ['self-test'])
                   for name, item in self.m.DECISIONS.items()}
        patcher = mock.patch.object(self.m, 'DECISIONS', patched)
        patcher.start()
        self.addCleanup(patcher.stop)

    def result(self, code=0, out=b'', err=b'', timeout=False):
        return self.m.RunResult(code, out, err, timeout)

    # ---- 入口と旧Python

    def test_fixture_must_be_an_absolute_executable_binary_not_a_legacy_wrapper(self):
        for value in (None, '', 'relative', '/r4g-absent-binary'):
            with self.assertRaises(ValueError):
                self.m.fixture_path(value)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                self.m.fixture_path(temp)
            path = Path(temp) / 'fixture'
            path.write_bytes(b'\x7fELF' + b'\0' * 60)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                self.m.fixture_path(str(path))
            path.chmod(0o755)
            self.assertEqual(self.m.fixture_path(str(path)), path)
            # 旧版を包んだスクリプト(誤接続)は、実行できても拒む。
            path.write_bytes(b'#!/bin/sh\nexec python3 implementation-git.py "$@"\n')
            with self.assertRaisesRegex(ValueError, '誤接続'):
                self.m.fixture_path(str(path))

    def test_fixture_identity_rejects_anything_but_the_fixed_entry_usage(self):
        good = self.result(2, err=self.m.ENTRY_USAGE)
        self.m.verify_fixture_identity('/x', runner=lambda *a, **k: good)
        for bad in (self.result(0), self.result(2, err=b'usage: implementation-git.py'),
                    self.result(2, out=b'x', err=self.m.ENTRY_USAGE), dataclasses.replace(good, timed_out=True),
                    self.result(69, err=self.m.ENTRY_USAGE)):
            with self.assertRaisesRegex(ValueError, '誤接続'):
                self.m.verify_fixture_identity('/x', runner=lambda *a, b=bad, **k: b)

    def test_python_must_be_upstream_cpython_3_10_11_with_matching_stdlib(self):
        expected = self.m.stdlib_expectation()
        self.assertEqual(len(expected), 20)
        with tempfile.TemporaryDirectory() as temp:
            stdlib = Path(temp)
            for name in expected:
                (stdlib / name).parent.mkdir(parents=True, exist_ok=True)
                (stdlib / name).write_bytes(b'not upstream')
            with mock.patch.object(self.m, 'stdlib_expectation', return_value={
                    name: hashlib.sha256(b'not upstream').hexdigest() for name in expected}):
                good = dict(version=(3, 10, 11), implementation='cpython', euid=1000, stdlib_dir=temp)
                self.assertEqual(self.m.python_requirements(**good), temp)
                for change in (dict(version=(3, 10, 12)), dict(version=(3, 11, 0)),
                               dict(implementation='pypy'), dict(euid=0)):
                    with self.assertRaises(ValueError):
                        self.m.python_requirements(**dict(good, **change))
                (stdlib / 'argparse.py').write_bytes(b'patched by a distributor')
                (stdlib / 'json/encoder.py').unlink()
                with self.assertRaises(ValueError) as failure:
                    self.m.python_requirements(**good)
                # 不一致は名前だけを出す。
                self.assertEqual(str(failure.exception), '標準ライブラリが本家の3.10.11と違う: argparse.py, json/encoder.py')

    # ---- 決定済みの差

    def test_decision_list_is_exactly_the_design_list_with_times(self):
        real = self.m.load_decisions()
        self.assertEqual(set(real), {'location-fields', 'stdlib-line-numbers', 'partial-write', 'current-thread',
                                     'sigint-traceback', 'timeout-fraction', 'encoding-fixed',
                                     'installed-locale-lc-ctype', 'bash-own-lines-english', 'child-env-order',
                                     'bash-tools-baseline', 'write-failure-midway', 'per-run-values',
                                     'partial-write-fits', 'runtime-added-child-env', 'race-unordered-outcome',
                                     'time-valued-env', 'job-pid-sequence', 'rust-std-fd-reopen', 'strerror-table'})
        for name in ('rust-std-fd-reopen', 'strerror-table'):
            self.assertEqual(real[name]['decided_at'], '2026-11-12 08:55')
            self.assertIn('rust-decision-s3i-startup.txt', real[name]['source'])
        self.assertEqual(real['rust-std-fd-reopen']['cases'],
                         ['pending-io/closed-fd', 'reviews-dir/closed-fd', 'implementation-git/closed-fd'])
        for name in ('race-unordered-outcome', 'time-valued-env', 'job-pid-sequence'):
            self.assertEqual(real[name]['decided_at'], '2026-11-12 06:58')
            self.assertIn('【追加の決定 2】', real[name]['source'])
        self.assertEqual(real['race-unordered-outcome']['cases'], ['reviews-dir/hup-comsub-race',
                                                                   'reviews-dir/comsub-group-race'])
        self.assertEqual(real['time-valued-env']['cases'], ['reviews-dir/bash-special-vars'])
        self.assertEqual(real['job-pid-sequence']['cases'], ['reviews-dir/job-pid'])
        self.assertIn('implementation-git/installed-locale', real['runtime-added-child-env']['cases'])
        for item in real.values():
            self.assertRegex(item['decided_at'], r'^2026-11-1[12] \d\d:\d\d')
            self.assertNotIn('self-test', item['cases'])
        self.assertEqual(real['current-thread']['decided_at'], '2026-11-11 17:45')
        self.assertIn('rust-decision-startup-fatal-diagnostic.txt', real['current-thread']['source'])
        outside = self.m.load_outside_premises()
        self.assertEqual(set(outside), {'bash-startup-env', 'python-startup-env', 'self-referring-paths', 'broken-tools',
                                        'intermediate-write-failure', 'memory-exhaustion', 'python-startup-env-more',
                                        'resource-limits', 'bash-behavior-env'})
        self.assertEqual(outside['bash-behavior-env']['decided_at'], '2026-11-12 09:58')
        data = json.loads((self.m.FIXTURES / 'decided-differences.json').read_text())
        for broken in ({'decided_at': ''}, {'commands': ['probe']}, {'extra': 1}, {'cases': 'pending-io/traceback'},
                       {'cases': ['a', 'a']}, {'cases': ['']}):
            changed = json.loads(json.dumps(data))
            changed['decisions'][0].update(broken)
            with mock.patch.object(Path, 'read_text', return_value=json.dumps(changed)):
                with self.assertRaises(ValueError):
                    self.m.load_decisions()
        duplicate = json.loads(json.dumps(data))
        duplicate['decisions'].append(duplicate['decisions'][0])
        with mock.patch.object(Path, 'read_text', return_value=json.dumps(duplicate)):
            with self.assertRaises(ValueError):
                self.m.load_decisions()
        for broken in ({'decided_at': '2026-11-13 00:00'}, {'extra': 1}):
            changed = json.loads(json.dumps(data))
            changed['outside_premises'][0].update(broken)
            with mock.patch.object(Path, 'read_text', return_value=json.dumps(changed)):
                with self.assertRaises(ValueError):
                    self.m.load_outside_premises()

    def test_decisions_apply_only_to_listed_commands_and_cases(self):
        good = self.result(1, err=b'Fatal\nCurrent thread 0x0000000000000001 (most recent call first):\n')
        self.m.compare('pending-implementation', good, good, decisions=('current-thread',), case='self-test')
        for command, case in (('pending-implementation', 'other-case'), ('pending-implementation', None),
                              ('reviews-dir', 'self-test')):
            with self.assertRaises(self.m.Difference):
                self.m.compare(command, good, good, decisions=('current-thread',), case=case)
        # 専用の確かめも、コマンドとケースで照らす。
        old = self.result(-2, err=b'KeyboardInterrupt\n')
        with self.assertRaises(self.m.Difference):
            self.m.sigint_pair('reviews-dir', 'self-test', old, old)
        with self.assertRaises(self.m.Difference):
            self.m.sigint_pair('implementation-git', 'other-case', old, old)
        with self.assertRaises(self.m.Difference):
            self.m.same_env_set('implementation-git', 'self-test', ['A=1'], ['A=1'])
        with self.assertRaises(self.m.Difference):
            self.m.partial_write('reviews-dir', 'self-test', b'abc', 1)
        with self.assertRaises(self.m.Difference):
            self.m.bash_lines_english('implementation-git', 'self-test', b'', b'', b'', 'x.sh')
        calls = [['70', [], ['413d31', '423d32'], '2f']]
        reordered = [['70', [], ['423d32', '413d31'], '2f']]
        with self.assertRaises(self.m.Difference):
            self.m.same_calls('reviews-dir', calls, reordered)
        with self.assertRaises(self.m.Difference):
            self.m.same_calls('implementation-git', calls, reordered, case='self-test', env_as_set=True)
        self.m.same_calls('reviews-dir', calls, reordered, case='self-test', env_as_set=True)
        # 本物の一覧に無いケースへは当てられない。
        with mock.patch.object(self.m, 'DECISIONS', self.m.load_decisions()):
            with self.assertRaises(self.m.Difference):
                self.m.compare('pending-implementation', good, good, decisions=('current-thread',), case='self-test')

    def test_compare_rejects_unlisted_and_misapplied_decisions(self):
        good = self.result(2, err=b'x')
        self.m.compare('reviews-dir', good, good)
        for decisions, command in ((('unlisted',), 'reviews-dir'), (('timeout-fraction',), 'reviews-dir'),
                                   (('child-env-order',), 'implementation-git'), (('partial-write',), 'implementation-git'),
                                   (('write-failure-midway',), 'pending-implementation')):
            with self.assertRaises(self.m.Difference):
                self.m.compare(command, good, good, decisions=decisions, case='self-test', limit=1)
        for change in (dict(returncode=1), dict(stdout=b'extra'), dict(stderr=b'y'), dict(timed_out=True)):
            with self.assertRaises(self.m.Difference):
                self.m.compare('reviews-dir', good, dataclasses.replace(good, **change))
        # 両側が同じ誤値でも、固定期待は拒む。
        for wrong in (self.result(1), self.result(2, err=b'Traceback')):
            with self.assertRaises(self.m.Difference):
                self.m.fixed(wrong, 2, stderr=b'x')

    def location(self):
        return dict(script_path='/old/plugins/implementation-git.py', script_name='implementation-git.py',
                    stdlib_dir='/py/lib/python3.10', expected=(1, 1))

    def traceback(self, script, stdlib):
        return (b'Traceback (most recent call last):\n'
                b'  File "' + script + b'", line 380, in <module>\n    main(sys.argv[1:])\n'
                b'  File "' + stdlib + b'", line 505, in run\n    stdout, stderr = process.communicate(input, timeout=timeout)\n'
                b'KeyboardInterrupt\n')

    def test_location_fields_map_only_the_fields_and_reject_swaps_counts_and_strangers(self):
        old = self.result(1, err=self.traceback(b'/old/plugins/implementation-git.py', b'/py/lib/python3.10/subprocess.py'))
        new = self.result(1, err=self.traceback(b'implementation-git.py', b'subprocess.py'))
        self.m.compare('implementation-git', old, new, decisions=('location-fields',), case='self-test', location=self.location())
        cases = [
            # 所在地の欄の取り違え(旧スクリプトの位置に標準ライブラリの名前)。
            self.result(1, err=self.traceback(b'subprocess.py', b'implementation-git.py')),
            self.result(1, err=self.traceback(b'implementation-git.py', b'json/encoder.py')),
            # 行番号・本文の差は対応づけない。
            self.result(1, err=new.stderr.replace(b'line 505', b'line 506')),
            self.result(1, err=new.stderr.replace(b'KeyboardInterrupt', b'SystemExit')),
            self.result(1, err=new.stderr + b'extra\n'),
        ]
        for candidate in cases:
            with self.assertRaises(self.m.Difference):
                self.m.compare('implementation-git', old, candidate, decisions=('location-fields',), case='self-test', location=self.location())
        for params in (dict(expected=(1, 0)), dict(expected=(2, 1)), dict(script_name='other.py'),
                       dict(stdlib_dir='/elsewhere')):
            with self.assertRaises(self.m.Difference):
                self.m.compare('implementation-git', old, new, decisions=('location-fields',), case='self-test',
                               location=dict(self.location(), **params))
        # SHA-256で確かめた標準ライブラリの外の名前は、導入先の下でも対応づけない。
        unverified = self.result(1, err=old.stderr.replace(b'python3.10/subprocess.py', b'python3.10/unverified.py'))
        with self.assertRaises(self.m.Difference):
            self.m.compare('implementation-git', unverified, self.result(1, err=self.traceback(b'implementation-git.py', b'unverified.py')),
                           decisions=('location-fields',), case='self-test', location=self.location())
        stranger = self.result(1, err=old.stderr.replace(b'/py/lib/python3.10/subprocess.py', b'/tmp/subprocess.py'))
        with self.assertRaises(self.m.Difference):
            self.m.compare('implementation-git', stranger, new, decisions=('location-fields',), case='self-test', location=self.location())
        # < で始まる欄はそのまま比べる。
        string = self.result(1, err=b'  File "<string>", line 1, in <module>\n')
        self.m.compare('implementation-git', string, string, decisions=('location-fields',), case='self-test',
                       location=dict(self.location(), expected=(0, 0)))

    def test_current_thread_checks_only_position_and_form(self):
        def fatal(value):
            return (b'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\n'
                    b'Python runtime state: core initialized\n\nCurrent thread 0x' + value
                    + b' (most recent call first):\n  <no Python frame>\n')
        old = self.result(1, err=fatal(b'00007d260b2b9000'))
        new = self.result(1, err=fatal(b'00007ffd12345678'))
        self.m.compare('pending-implementation', old, new, decisions=('current-thread',), case='self-test')
        for value in (b'00007ffd1234567', b'00007FFD12345678', b'0x7ffd123456789a', b'00007ffd12345678 extra'):
            with self.assertRaises(self.m.Difference):
                self.m.compare('pending-implementation', old, self.result(1, err=fatal(value)), decisions=('current-thread',), case='self-test')
        for broken in (new.stderr.replace(b'<no Python frame>', b'<frame>'), new.stderr + new.stderr,
                       new.stderr.replace(b'core initialized', b'preinitialized')):
            with self.assertRaises(self.m.Difference):
                self.m.compare('pending-implementation', old, self.result(1, err=broken), decisions=('current-thread',), case='self-test')
        with self.assertRaises(self.m.Difference):
            self.m.compare('pending-implementation', old, new)

    def test_sigint_pair_ignores_only_the_middle_of_the_traceback(self):
        old = self.result(-2, out=b'partial', err=b'Traceback (most recent call last):\n  File "a"\nKeyboardInterrupt\n')
        new = self.result(-2, out=b'partial', err=b'Traceback (most recent call last):\n  File "b", line 9\n    x\nKeyboardInterrupt\n')
        self.m.sigint_pair('implementation-git', 'self-test', old, new)
        for change in (dict(returncode=130), dict(returncode=1), dict(stdout=b'other'), dict(timed_out=True),
                       dict(stderr=new.stderr + b'extra\n'), dict(stderr=b'Traceback (most recent call last):\nValueError\n'),
                       dict(stderr=b'garbage\nKeyboardInterrupt\n')):
            with self.assertRaises(self.m.Difference):
                self.m.sigint_pair('implementation-git', 'self-test', old, dataclasses.replace(new, **change))

    def test_timeout_fraction_checks_form_and_range_only(self):
        def message(value):
            return b"ERROR [git] Git \xe3\x82\x92 timed out after " + value + b" seconds\n"
        old = self.result(20, err=message(b'0.9995077729981858'))
        new = self.result(20, err=message(b'29.99'))
        self.m.compare('implementation-git', old, new, decisions=('timeout-fraction',), case='self-test', limit=30)
        for value in (b'31.0', b'0.0', b'-0.5', b'abc', b'1e3', b'0.5 0.5', b'0.10', b'1.50', b'1.5e-5', b'2.0e-05'):
            with self.assertRaises(self.m.Difference):
                self.m.compare('implementation-git', old, self.result(20, err=message(value)),
                               decisions=('timeout-fraction',), case='self-test', limit=30)
        self.m.compare('implementation-git', old, self.result(20, err=message(b'1.5e-05')),
                       decisions=('timeout-fraction',), case='self-test', limit=30)
        with self.assertRaises(self.m.Difference):
            self.m.compare('implementation-git', old, self.result(20, err=message(b'0.5') + b'x'),
                           decisions=('timeout-fraction',), case='self-test', limit=30)

    def test_bash_english_lines_and_child_env_set(self):
        script = 'reviews-dir.sh'
        localized = 'reviews-dir.sh: 159 行: printf: 書き込みエラー: デバイスに空き領域がありません\nERROR [internal] x\n'.encode()
        c_locale = b'reviews-dir.sh: line 159: printf: write error: No space left on device\nERROR [internal] x\n'
        rd = ('reviews-dir', 'self-test')
        self.m.bash_lines_english(*rd, localized, c_locale, c_locale, script)
        for new in (localized, c_locale + b'extra\n'):
            with self.assertRaises(self.m.Difference):
                self.m.bash_lines_english(*rd, localized, c_locale, new, script)
        with self.assertRaises(self.m.Difference):
            self.m.bash_lines_english(*rd, localized.replace(b'ERROR', b'error'), c_locale, c_locale, script)
        self.m.same_env_set(*rd, ['A=1', 'B=2', '_=x'], ['_=x', 'A=1', 'B=2'])
        for other in (['A=1', 'B=2'], ['A=1', 'B=3', '_=x'], ['A=1', 'B=2', '_=x', 'A=1']):
            with self.assertRaises(self.m.Difference):
                self.m.same_env_set(*rd, ['A=1', 'B=2', '_=x'], other)
        pi = ('pending-implementation', 'self-test')
        self.assertEqual(self.m.partial_write(*pi, b'abcdef', 3), b'abc')
        with self.assertRaises(self.m.Difference):
            self.m.partial_write(*pi, b'abc', -1)

    @staticmethod
    def exec_raw(path, argv, envp, cwd):
        """環境の並びと重複を決めて、fork と execve で起動する(Pythonのenvの辞書を通さない)。"""
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        argv_array = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
        env_array = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_end)
                os.dup2(write_end, 1)
                os.chdir(cwd)
                libc.execve(os.fsencode(path), argv_array, env_array)
            finally:
                os._exit(127)
        os.close(write_end)
        with os.fdopen(read_end, 'rb') as stream:
            out = stream.read()
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out

    def test_stub_records_the_raw_environment_path_and_passes_it_on(self):
        # Mac では代用品が sysctl の KERN_PROCARGS2 から読む(この経路は単位6の Mac の CI で確かめる)。
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'calls'
            stub = self.m.write_stub(temp, 'git', log, code=3)
            base = [b'PATH=' + os.fsencode(os.environ.get('PATH', '/usr/bin:/bin')), b'B=2', b'A=1']
            variants = {
                'plain': base,
                'reordered': [base[0], base[2], base[1]],
                'duplicated': base + [b'A=9'],
                'lc-ctype': base + [b'LC_CTYPE=C.UTF-8'],
            }
            for name, envp in variants.items():
                code, _ = self.exec_raw(stub, [b'git', b'-C', b'/r', b'\xff'], envp, temp)
                self.assertEqual(code, 3, name)
            calls = self.m.read_calls(log)
            self.assertEqual(len(calls), 4)
            for call, envp in zip(calls, variants.values()):
                # 実行したパスの全体・引数・生の環境(親が渡したものだけ。地域設定の強制で足されない)。
                self.assertEqual(call[0], os.fsencode(str(stub)).hex())
                self.assertEqual(call[1], [b'-C'.hex(), b'/r'.hex(), b'\xff'.hex()])
                self.assertEqual(call[2], [item.hex() for item in envp])
                self.assertEqual(call[3], os.fsencode(temp).hex())
            self.assertEqual(len({json.dumps(call[2]) for call in calls}), 4)
            self.m.same_calls('implementation-git', calls[:1], calls[:1])
            for other in calls[1:]:
                with self.assertRaises(self.m.Difference):
                    self.m.same_calls('implementation-git', calls[:1], [other])
            # 本物の道具へは、同じ生の環境(並び・重複)を渡す。
            real = self.m.write_stub(temp, 'env-real', log, real='/usr/bin/env')
            envp = variants['duplicated']
            code, out = self.exec_raw(real, [b'env-real'], envp, temp)
            self.assertEqual(code, 0)
            self.assertEqual(out.splitlines(), envp)
            for broken in (calls[:1], calls + calls[:1], [calls[1], calls[0], calls[2], calls[3]],
                           [[calls[0][0], calls[0][1][:2], calls[0][2], calls[0][3]], *calls[1:4]],
                           [[calls[0][0][:-2], calls[0][1], calls[0][2], calls[0][3]], *calls[1:4]]):
                with self.assertRaises(self.m.Difference):
                    self.m.same_calls('implementation-git', calls[:4], broken)

    def test_tool_versions_are_parsed_and_must_match_the_baseline(self):
        outputs = {'bash': b'GNU bash, version 5.2.21(1)-release (x86_64-pc-linux-gnu)\n',
                   'cat': b'cat (GNU coreutils) 9.4\nCopyright\n', 'find': b'find (GNU findutils) 4.9.0\n',
                   'sed': b'sed (GNU sed) 4.9\n'}
        runner = lambda argv, o=outputs: subprocess.CompletedProcess(argv, 0, o[argv[0]], b'')
        versions = self.m.tool_versions(runner)
        self.assertEqual(versions, self.m.TOOL_BASELINE)
        self.m.require_tool_baseline(versions)
        for name, text in (('bash', b'GNU bash, version 5.1.16(1)-release\n'), ('cat', b'cat (GNU coreutils) 8.32\n'),
                           ('sed', b'sed (BSD) 4.9\n'), ('find', b'')):
            with self.subTest(name=name):
                changed = dict(outputs, **{name: text})
                with self.assertRaisesRegex(self.m.Difference, '基準と違う'):
                    self.m.require_tool_baseline(self.m.tool_versions(lambda argv, o=changed: subprocess.CompletedProcess(argv, 0, o[argv[0]], b'')))
        def missing(argv):
            raise FileNotFoundError(argv[0])
        self.assertEqual(set(self.m.tool_versions(missing).values()), {None})
        limits = self.m.inherited_limits()
        self.assertIn('RLIMIT_FSIZE', limits)

    def test_pending_static_check_finds_children_and_writers_outside_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in self.m.PENDING_STATIC_FILES:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_bytes((self.m.ROOT / relative).read_bytes())
            self.assertEqual(self.m.pending_static_findings(root), [])
            scan = root / 'native/crates/cli/src/commands/pending_implementation/scan.rs'
            original = scan.read_text(encoding='utf-8')
            for line in ('fn a() { let _ = std::process::Command::new("git"); }',
                         'use dev_workflow_platform::r4g::process::fork_exec;',
                         'fn b() { let _ = OFlags::WRONLY; }', 'fn c() { std::fs::remove_file("x").ok(); }',
                         'fn d() { let _ = process::python_run; }'):
                with self.subTest(line=line):
                    scan.write_text(line + '\n' + original, encoding='utf-8')
                    self.assertTrue(self.m.pending_static_findings(root))
            # 文字列の中の語とテストのモジュールの中は数えない。
            scan.write_text('const X: &str = "exec std::process";\n' + original
                            + '\n#[cfg(test)]\nmod tests {\n    use std::process::Command;\n}\n', encoding='utf-8')
            self.assertEqual(self.m.pending_static_findings(root), [])
            fs = root / 'native/crates/platform/src/r4g/fs.rs'
            fs.write_text(fs.read_text(encoding='utf-8').replace('pub fn isatty', 'pub fn spawn_isatty'), encoding='utf-8')
            self.assertTrue(self.m.pending_static_findings(root))

    def test_per_run_values_hide_only_the_job_pid(self):
        def notice(pid):
            return (b'rd.sh: line 141: ' + pid + b' User defined signal 1   git -C x\n'
                    b'     ' + pid + b'                       | sed x\n')
        old = self.result(4, err=notice(b'   12'))
        self.m.compare('reviews-dir', old, self.result(4, err=notice(b' 3456')), decisions=('per-run-values',),
                       case='self-test')
        for broken in (notice(b'   12').replace(b'User defined signal 1', b'Hangup'),
                       notice(b'   12').replace(b'line 141', b'line 142'), notice(b'   12') + b'x\n'):
            with self.assertRaises(self.m.Difference):
                self.m.compare('reviews-dir', old, self.result(4, err=broken), decisions=('per-run-values',),
                               case='self-test')
        with self.assertRaises(self.m.Difference):
            self.m.compare('reviews-dir', old, self.result(4, err=notice(b' 3456')))

    def test_undecided_items_are_recorded_but_never_counted(self):
        # 06:58 の決定で、未決の項目は0件になった(本物の一覧は空)。仕組みは仮の項目で確かめる。
        self.assertEqual(self.m.UNDECIDED_ITEMS, {})
        records = []
        items = {'self/item': '仮の未決'}
        with mock.patch.object(self.m, 'UNDECIDED', records), mock.patch.object(self.m, 'UNDECIDED_ITEMS', items):
            self.m.undecided('self/item', 'a', lambda: None)

            def differ():
                raise self.m.Difference('x')
            self.m.undecided('self/item', 'b', differ)
            with self.assertRaises(ValueError):
                self.m.undecided('reviews-dir/job-pid', 'c', lambda: None)
            # Difference 以外の例外(比較器の誤り)は記録せずに通す。
            with self.assertRaises(KeyError):
                self.m.undecided('self/item', 'd', lambda: {}['x'])
            self.assertEqual(self.m.undecided_summary(records), 'self/item 2 件(一致 1・不一致 1)')
            # 最後の合格の前の切替え: 既定は、未決が残れば失敗。--allow-undecided のときだけ続ける。
            lines, failed = self.m.undecided_verdict(records, False)
            self.assertTrue(failed)
            self.assertEqual(lines[-1], 'R4G: 未決(管理担当の確認待ち。合格に数えない): self/item 2 件(一致 1・不一致 1)')
            self.assertFalse(self.m.undecided_verdict(records, True)[1])
        self.assertEqual([row[2] for row in records], ['一致', '不一致(x)'])
        self.assertEqual(self.m.undecided_verdict([], False), (['R4G: 未決: 0 件'], False))

    def test_closed_standard_fds_expect_the_rust_standard_reopening(self):
        # 2026-11-12 08:55: 閉じる指定だけを /dev/null(読み書き)へ置き換えた起動の指定が、旧版の側の期待。
        launch = {'redirect': {'0': None, '1': 'closed-pipe', '2': None}, 'fsize': 10}
        self.assertEqual(self.m.std_fd_reopened(launch),
                         {'redirect': {'0': 'null-rdwr', '1': 'closed-pipe', '2': 'null-rdwr'}, 'fsize': 10})
        self.assertEqual(launch['redirect']['0'], None)
        self.assertIsNone(self.m.std_fd_reopened(None))
        self.assertIn("target == 'null-rdwr'", self.m.LAUNCH)

    def test_script_variable_list_covers_every_assignment_in_reviews_dir(self):
        """2026-11-12 14:24 の決定 4 で比べる名前の一覧が、旧 reviews-dir.sh の全部の代入(関数の中・罠の文字列・
        コマンド置換の結果・local・for)と一致すること。旧版に代入を足すと、この検査が落ちて一覧の更新を促す。"""
        import re

        def collect(text):
            names, unset, exported, builtins = set(), set(), set(), []
            for line in text.splitlines():
                if line.lstrip().startswith('#'):
                    continue
                # 行の先頭・区切り(; & | ( { 空白・引用符)の後の NAME= と NAME=( 。$NAME と --x= は当たらない。
                names.update(re.findall(r"(?:^|[;&|({\s'\"])([A-Za-z_][A-Za-z0-9_]*)\+?=", line))
                for declared in re.findall(r'\blocal\s+([^;]+)', line):
                    names.update(re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)(?==|\s|$)', declared))
                names.update(re.findall(r'\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b', line))
                unset.update(re.findall(r'\bunset\s+([A-Za-z_][A-Za-z0-9_]*)', line))
                exported.update(re.findall(r'\bexport\s+([A-Za-z_][A-Za-z0-9_]*)', line))
                builtins += [name for name in ('read', 'declare', 'typeset', 'getopts', 'mapfile', 'readarray',
                                               'printf -v') if re.search(r'(?:^|[;&|\s])' + re.escape(name) + r'\s', line)]
            return names - exported, unset, exported, builtins
        text = self.m.REVIEWS_SCRIPT.read_text(encoding='utf-8')
        names, unset, exported, builtins = collect(text)
        self.assertEqual((exported, unset, builtins), ({'GIT_NO_LAZY_FETCH'}, {'CDPATH'}, []))
        self.assertEqual(names, set(self.m.REVIEWS_SCRIPT_NAMES))
        # 変異の対照: 関数の local・罠の文字列・for に名前を1つ足すと、一覧と合わなくなる。
        for old, new in (('  local d\n', '  local d extra_name\n'), ("'ec=$?;", "'ec=$? extra_name=1;"),
                         ('for d in', 'for extra_name in')):
            self.assertEqual(text.count(old), 1, old)
            self.assertIn('extra_name', collect(text.replace(old, new))[0])
        self.assertEqual(collect('x=1\nread y\n')[3], ['read'])

    def test_bash_behavior_variables_are_not_comparison_inputs(self):
        # 2026-11-12 09:58: 一覧の変数と、範囲の中の BASH_COMPAT は比較の環境から除く。範囲の外は警告を比べる。
        for name, value, expected in (('FUNCNEST', '1', True), ('TMOUT', '5', True), ('GLOBIGNORE', '*', True),
                                      ('BASH_COMPAT', '50', True), ('BASH_COMPAT', '4.2', True),
                                      ('BASH_COMPAT', '99', False), ('BASH_COMPAT', 'bogus', False),
                                      ('BASH_COMPAT', '3.0', False), ('BASH_XTRACEFD', '99', False),
                                      ('IFS', 'x', False), ('PATH', '/bin', False)):
            self.assertEqual(self.m.bash_behavior_input(name, value), expected, (name, value))
        with mock.patch.dict(os.environ, {'FUNCNEST': '1', 'BASH_COMPAT': '50', 'EXECIGNORE': '*'}):
            env = self.m.reviews_env()
        self.assertFalse({'FUNCNEST', 'BASH_COMPAT', 'EXECIGNORE'} & set(env))
        with mock.patch.dict(os.environ, {'BASH_COMPAT': '99'}):
            self.assertEqual(self.m.reviews_env()['BASH_COMPAT'], '99')

    def test_require_userns_turns_the_missing_namespace_into_a_failure(self):
        test = unittest.TestCase()
        records = []
        with mock.patch.object(self.m, 'NOT_RUN', records), mock.patch.object(self.m, 'REQUIRE_USERNS', False):
            self.m.userns_unavailable(test, 'unshare failed')
        self.assertEqual([(record[1], record[3]) for record in records], [(None, True)])
        with mock.patch.object(self.m, 'NOT_RUN', []) as kept, mock.patch.object(self.m, 'REQUIRE_USERNS', True):
            with self.assertRaises(self.m.Difference):
                self.m.userns_unavailable(test, 'unshare failed')
            self.assertEqual(kept, [])

    def test_not_run_cases_are_not_counted_as_passed(self):
        records = [('m.T.test_a', None, 'Linux だけ', True), ('m.T.test_b', '組 x', 'Linux だけ', True),
                   ('sec.test_c', None, 'Mac に基準の道具が無い', False), ('m.T.test_a', None, '重複', True)]
        lines = self.m.final_summary(10, records, 'strace の対照: 実施')
        # 流れた後に全体を未実施と記録したテスト(test_a)だけを合格から引く。一部の組と、流さなかった節は引かない。
        self.assertEqual(lines[-1], 'R4G: 合格(9 件・未実施 4 件・strace の対照: 実施)')
        self.assertEqual(lines[1], 'R4G: 未実施: m.T.test_b(組 x): Linux だけ')
        self.assertEqual(self.m.final_summary(3, [], 'c'), ['R4G: 合格(3 件・未実施 0 件・c)'])

    def test_job_pid_sequence_must_equal_the_recorded_children(self):
        def notice(*pids):
            return b''.join(b'rd.sh: line 141: ' + str(pid).rjust(5).encode() + b' Hangup                  git -C x\n'
                            for pid in pids)
        r = self.result
        check = self.m.job_pid_check
        self.assertEqual(check(r(4, err=notice(12)), r(4, err=notice(345)), {'git': 12}, {'git': 345},
                               case='self-test'), ['git'])
        # 2つの子の並び(役で照らす)。
        self.assertEqual(check(r(4, err=notice(12, 13)), r(4, err=notice(35, 34)), {'a': 12, 'b': 13},
                               {'a': 35, 'b': 34}, case='self-test'), ['a', 'b'])
        for old, new, old_children, new_children in (
                (notice(12), notice(345), {'git': 12}, {'git': 346}),        # Rust の番号が記録した子でない
                (notice(11), notice(345), {'git': 12}, {'git': 345}),        # 旧版の番号が記録した子でない
                (notice(12, 13), notice(34, 35), {'a': 12, 'b': 13}, {'a': 35, 'b': 34}),  # 並びが違う
                (notice(12), notice(345, 345), {'git': 12}, {'git': 345}),   # 行の数が違う(伏せた全文が違う)
                (notice(12), b'', {'git': 12}, {'git': 345})):
            with self.assertRaises(self.m.Difference):
                check(r(4, err=old), r(4, err=new), old_children, new_children, case='self-test')
        # 番号が出なければ素のまま比べる。期待した役の並びとも照らす。
        self.assertEqual(check(r(4, err=b'x\n'), r(4, err=b'x\n'), {'git': 1}, {'git': 2}, case='self-test'), [])
        with self.assertRaises(self.m.Difference):
            check(r(4, err=b'x\n'), r(4, err=b'y\n'), {'git': 1}, {'git': 2}, case='self-test')
        with self.assertRaises(self.m.Difference):
            check(r(4, err=b'x\n'), r(4, err=b'x\n'), {'git': 1}, {'git': 2}, expected_roles=['git'], case='self-test')

    def test_stub_shell_lines_are_removed_only_when_the_same(self):
        r = self.result
        stub = self.m.STUB_SHELL_PREFIX
        self.assertEqual(stub, b'/bin/bash: ')
        a = stub + b'BASH_XTRACEFD: 99: invalid value for trace file descriptor\n'
        b = stub + b'BASH_COMPAT: 1: compatibility value out of range\n'
        product = b'bash: BASH_XTRACEFD: 99: invalid value for trace file descriptor\nx.sh: line 3: y\n'
        # 包みの行は、混ざる順が違っても多重集合が同じなら両方から除く。製品の行は除かない。
        old, new = self.m.without_stub_shell_lines(r(0, err=product + a + b + a), r(0, err=a + product + a + b))
        self.assertEqual((old.stderr, new.stderr), (product, product))
        for broken in (a + product + b, a + a + product + b + b, product + a + a):
            with self.assertRaises(self.m.Difference):
                self.m.without_stub_shell_lines(r(0, err=product + a + b + a), r(0, err=broken))
        # 製品の行の違いは残り、後の比較で見つかる。
        old, new = self.m.without_stub_shell_lines(r(0, err=product + a), r(0, err=b'bash: other\n' + a))
        self.assertNotEqual(old.stderr, new.stderr)

    def test_time_valued_env_checks_the_form_and_the_run_time(self):
        def call(*env):
            return [b'/g'.hex(), [], [item.hex() for item in env], b'/'.hex()]
        names = (b'EPOCHSECONDS', b'EPOCHREALTIME')
        done = self.m.time_valued_env('reviews-dir', 'self-test',
                                      [call(b'A=1', b'EPOCHSECONDS=1000', b'EPOCHREALTIME=1000.250000')],
                                      names, 1000.2, 1000.3)
        self.assertEqual(done[0][2], [b'A=1'.hex(), b'EPOCHSECONDS=<time>'.hex(), b'EPOCHREALTIME=<time>'.hex()])
        for bad in (b'EPOCHSECONDS=999', b'EPOCHSECONDS=1001', b'EPOCHSECONDS=1e3', b'EPOCHREALTIME=1000.1',
                    b'EPOCHREALTIME=1000.100000', b'EPOCHREALTIME=1000.400000', b'EPOCHREALTIME=1000,250000',
                    b'EPOCHREALTIME=1'):
            with self.subTest(bad=bad), self.assertRaises(self.m.Difference):
                self.m.time_valued_env('reviews-dir', 'self-test', [call(bad)], names, 1000.2, 1000.3)
        # 名前を渡さない変数は、そのまま(呼出側が全部比べる)。
        kept = self.m.time_valued_env('reviews-dir', 'self-test', [call(b'EPOCHSECONDS=1')], (b'EPOCHREALTIME',),
                                      1000.2, 1000.3)
        self.assertEqual(kept[0][2], [b'EPOCHSECONDS=1'.hex()])

    def test_unordered_race_leftovers_stay_within_the_old_range(self):
        tmp = b'.claude/reviews/.base-untracked.r4gfix'
        base = {b'.claude': (1, 2, None), b'.claude/reviews/x.z': (3, 4, b'list')}

        def plus(state, key, value):
            return {**state, key: value}
        left = plus(base, tmp, (3, 0o600, b'a\0'))
        self.assertEqual(self.m.unordered_race_states(left, base), ([tmp], []))
        self.assertEqual(self.m.unordered_race_states(base, left), ([], [tmp]))
        self.assertEqual(self.m.unordered_race_states(left, left), ([tmp], [tmp]))
        for old, new in ((base, plus(base, b'.claude/reviews/.base-untracked.zz', (3, 0o600, b''))),
                         (base, plus(base, b'other/.base-untracked.r4gfix', (3, 0o600, b''))),
                         (left, plus(base, tmp, (3, 0o600, b'b\0'))),
                         (left, plus(left, b'.claude/reviews/.base-untracked.abcdef', (3, 0o600, b''))),
                         (base, plus(base, b'.claude/reviews/x.z', (3, 4, b'other')))):
            with self.assertRaises(self.m.Difference):
                self.m.unordered_race_states(old, new)
        # 置き場は渡した根からの相対で決める(根の下の別の .claude/reviews は範囲の外)。
        nested = b'repo/.claude/reviews/.base-untracked.r4gfix'
        self.assertEqual(self.m.unordered_race_states(plus(base, nested, (3, 0o600, b'')), base, b'repo/.claude/reviews'),
                         ([nested], []))
        with self.assertRaises(self.m.Difference):
            self.m.unordered_race_states(plus(base, nested, (3, 0o600, b'')), base)

    def test_group_signal_values_at_shell_end_are_only_range_checked_and_counted(self):
        counts = {'runs': 0, 'old_remained': 0, 'new_remained': 0, 'old_open': 0, 'new_open': 0, 'differ': 0}
        closed = {'remained': False, 'stderr_open': False}
        held = {'remained': True, 'stderr_open': True}
        with mock.patch.object(self.m, 'OBSERVATIONS', []) as observations:
            self.m.race_at_shell_end(closed, held, counts, 'x-group-1')
            self.m.race_at_shell_end(held, closed, counts, 'x-group-15')
            self.m.race_at_shell_end(closed, closed, counts, 'x-group-2')
            # シェルが3秒で終わらなかった側があれば数えない(呼出側が全部の一致を求める)。
            self.m.race_at_shell_end({'remained': None, 'stderr_open': None}, closed, counts, 'x-group-9')
        self.assertEqual(counts, {'runs': 3, 'old_remained': 1, 'new_remained': 1, 'old_open': 1, 'new_open': 1,
                                  'differ': 2})
        self.assertEqual(len(observations), 2)
        self.assertIn('x-group-1', observations[0])
        for broken in (None, {}, {'remained': False}, {'remained': 1, 'stderr_open': False},
                       {'remained': None, 'stderr_open': False}, {'remained': False, 'stderr_open': None, 'x': 1}):
            with self.subTest(raw=broken), self.assertRaises(self.m.Difference):
                self.m.race_at_shell_end(broken, closed, dict(counts), 'x')

    def test_settle_waits_for_the_stub_and_the_stderr_holder_without_releasing_them(self):
        def popen(script):
            # 新しい群で起こす(後片付けで、孫だけをこの群ごと止める)。
            return subprocess.Popen(['sh', '-c', script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, start_new_session=True)
        # 標準エラーを 0.3 秒持つ孫と、0.3 秒で終わる代用品: 待てば両方とも終わる。
        holder = popen('(sleep 0.3; echo late >&2) & exit 0')
        stub = subprocess.Popen(['sleep', '0.3'])
        holder.wait(timeout=10)
        stderr_open, early = self.m.stderr_still_open(holder)
        self.assertTrue(stderr_open)
        remained, stderr_open, early = self.m.settle_after_shell(holder, stub.pid, True, early, seconds=10)
        self.assertEqual((remained, stderr_open, early), (False, False, b'late\n'))
        stub.wait(timeout=10)
        holder.stderr.close()
        # 終わらない代用品と、閉じない標準エラーは、上限まで待って残ったままと返す(放さない)。
        holder = popen('sleep 30 & exit 0')
        stub = subprocess.Popen(['sleep', '30'])
        try:
            holder.wait(timeout=10)
            remained, stderr_open, _ = self.m.settle_after_shell(holder, stub.pid, True, b'', seconds=0.3)
            self.assertEqual((remained, stderr_open), (True, True))
        finally:
            stub.kill()
            stub.wait(timeout=10)
            try:
                os.killpg(holder.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            holder.stderr.close()

    @unittest.skipUnless(sys.platform.startswith('linux'), '/proc を読む(Linux だけ)')
    def test_group_settled_waits_for_every_member_of_the_group(self):
        # 群の頭(cat)は pipe を読んで眠り、隣の子(sleep)は待つ所の外で 0.4 秒眠る: 隣が終わるまで待つ。
        leader = subprocess.Popen(['sh', '-c', 'sleep 0.4 & exec cat'], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, start_new_session=True)
        stuck = subprocess.Popen(['sleep', '30'], start_new_session=True)
        try:
            started = time.monotonic()
            self.m.wait_group_settled(leader.pid, 'self-test', timeout=10)
            self.assertGreaterEqual(time.monotonic() - started, 0.2)
            # 待つ所の外で眠り続ける群は、上限で不合格(信号を送らない)。
            with self.assertRaises(self.m.Difference):
                self.m.wait_group_settled(stuck.pid, 'self-test', timeout=0.3)
        finally:
            for process in (leader, stuck):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            leader.stdin.close()
            for process in (leader, stuck):
                process.wait(timeout=10)

    def test_stub_blocked_accepts_the_inlined_fifo_open(self):
        seen = []
        with mock.patch.object(self.m, 'wait_sleeping_in', lambda pid, functions, what: seen.append(functions)):
            self.m.wait_stub_blocked(1)
        self.assertEqual(seen, [('wait_for_partner', 'fifo_open')])

    def test_installed_locale_strips_the_runtime_added_variable(self):
        # Mac の旧Pythonが子へ足す __CF_USER_TEXT_ENCODING は、02:41 の決定で外す(このケースも決定の cases にある)。
        def call(*env):
            return [b'/g'.hex(), [], [item.hex() for item in env], b'/'.hex(), {}, 'other']
        added = b'LC_CTYPE=C.UTF-8'
        with mock.patch.object(self.m, 'OBSERVATIONS', []) as observations:
            self.m.installed_locale_lc_ctype('implementation-git', 'self-test',
                                             [call(b'A=1', b'__CF_USER_TEXT_ENCODING=0x1F5:0x0:0x0', added)],
                                             [call(b'A=1')])
            self.assertTrue(any('__CF_USER_TEXT_ENCODING' in line for line in observations))
        self.assertIn('implementation-git/installed-locale',
                      self.m.load_decisions()['runtime-added-child-env']['cases'])

    def test_runtime_added_child_environment_is_stripped_only_by_the_decision(self):
        def call(*env):
            return [b'/g'.hex(), [], [item.hex() for item in env], b'/'.hex()]
        old = [call(b'A=1', b'__CF_USER_TEXT_ENCODING=0x1F5:0x0:0x0', b'B=2')]
        new = [call(b'A=1', b'B=2')]
        with mock.patch.object(self.m, 'OBSERVATIONS', []) as observations:
            self.m.same_calls('implementation-git', old, new, case='self-test')
            self.assertTrue(any('__CF_USER_TEXT_ENCODING' in line for line in observations))
        with self.assertRaises(self.m.Difference):
            self.m.same_calls('implementation-git', [call(b'A=1', b'C=3')], new, case='self-test')
        with self.assertRaises(self.m.Difference):
            self.m.same_calls('pending-implementation', old, new, case='self-test')

    def test_fixed_mktemp_fallback_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / 'calls'
            Path(str(log) + '.random').write_text('/x/.base-untracked.r4gfix\n')
            with self.assertRaises(self.m.Difference):
                self.m.read_calls(log)
            self.assertEqual(self.m.read_calls(log), [])

    def test_implementation_git_injection_maps_only_the_known_rewrites(self):
        original = self.m.IG_SCRIPT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / 'implementation-git.py'
            copy.write_text(original, encoding='utf-8')
            self.assertEqual(self.m.ig_injection(copy), [])
            copy.write_text(original.replace('PRECHECK_TIMEOUT = 120', 'PRECHECK_TIMEOUT = 0.05')
                            .replace('GIT_TIMEOUT = 30', 'GIT_TIMEOUT = 0.05'), encoding='utf-8')
            self.assertEqual(self.m.ig_injection(copy), ['--r4g-deadlines', '0.05,0.05'])
            copy.write_text(original.replace(*self.m.IG_MUTATION), encoding='utf-8')
            self.assertEqual(self.m.ig_injection(copy), ['--r4g-mutation', 'operation-env'])
            for broken in (original.replace('VERSION = 1', 'VERSION = 2'),
                           original.replace('GIT_TIMEOUT = 30', 'GIT_TIMEOUT = 1') + '#',
                           original.replace(*self.m.IG_MUTATION).replace('VERSION = 1', 'VERSION = 2')):
                copy.write_text(broken, encoding='utf-8')
                with self.assertRaises(self.m.Difference):
                    self.m.ig_injection(copy)

    def test_implementation_git_correspondence_is_one_to_one(self):
        data = self.m.ig_load_correspondence()
        methods = ['test_strict_input_stops_before_precheck', 'test_missing_sibling_precheck_script']
        self.assertEqual(sum(self.m.ig_check_correspondence(data, methods=methods).values()), 29)
        broken = json.loads(json.dumps(data))
        broken['entries'].pop()
        with self.assertRaises(AssertionError):
            self.m.ig_check_correspondence(broken, methods=methods)
        broken = json.loads(json.dumps(data))
        broken['entries'].append(dict(broken['entries'][0]))
        with self.assertRaises(AssertionError):
            self.m.ig_check_correspondence(broken, methods=methods)
        broken = json.loads(json.dumps(data))
        broken['entries'][0]['kind'] = 'legacy-only'
        with self.assertRaises(AssertionError):
            self.m.ig_check_correspondence(broken, methods=methods)
        with self.assertRaises(AssertionError):
            self.m.ig_check_correspondence(data, methods=methods[:1])

    def test_implementation_git_compare_maps_locations_and_rejects_swaps(self):
        stdlib = self.m.sysconfig.get_paths()['stdlib']
        script = str(self.m.IG_SCRIPT)
        def trace(first, second):
            return ('Traceback (most recent call last):\n  File "%s", line 380, in <module>\n    main()\n'
                    '  File "%s", line 195, in fullmatch\n    x\nTypeError: t\n' % (first, second)).encode()
        old = self.result(1, err=trace(script, stdlib + '/re.py'))
        new = self.result(1, err=trace('implementation-git.py', 're.py'))
        self.m.ig_compare(old, new, case='self-test')
        for bad in (trace('re.py', 'implementation-git.py'), trace('implementation-git.py', 'subprocess.py'),
                    trace('implementation-git.py', 're.py') + b'extra\n'):
            with self.assertRaises(self.m.Difference):
                self.m.ig_compare(old, self.result(1, err=bad), case='self-test')

    def test_installed_locale_difference_is_only_lc_ctype(self):
        def call(*env):
            return [b'/g'.hex(), [], [item.hex() for item in env], b'/'.hex()]
        self.m.installed_locale_lc_ctype('implementation-git', 'self-test', [call(b'A=1', b'LC_CTYPE=C.UTF-8')],
                                         [call(b'A=1')])
        for old, new in (([call(b'A=1')], [call(b'A=1')]), ([call(b'A=2', b'LC_CTYPE=C.UTF-8')], [call(b'A=1')]),
                         ([call(b'LC_CTYPE=C.UTF-8')], [call(b'LC_CTYPE=C.UTF-8')])):
            with self.assertRaises(self.m.Difference):
                self.m.installed_locale_lc_ctype('implementation-git', 'self-test', old, new)

    def test_fault_rule_hit_counts_are_compared_and_must_hit(self):
        self.m.check_rule_hits([[1, 2], [1, 2]])
        for broken in ([[1, 2], [1, 3]], [[0, 2], [0, 2]], [None, [1]], [[1], None], [[1, 2], [1]]):
            with self.assertRaises(self.m.Difference):
                self.m.check_rule_hits(broken)
        # 変異の対照: 照合を緩めた関数では、上の誤りを見逃す(この自己検査が照合の働きを見ている)。
        lenient = lambda counts: None  # noqa: E731
        with mock.patch.object(self.m, 'check_rule_hits', lenient):
            self.m.check_rule_hits([[1, 2], [1, 3]])

    def test_unknown_fault_ops_are_rejected_by_the_wrapper(self):
        wrapper = self.m.PENDING_WRAPPER
        spec = json.dumps({'rules': [{'op': 'bogus', 'errno': 2}]})
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'old.py'
            script.write_text('print("ran")\n')
            done = subprocess.run([sys.executable, '-I', str(wrapper), spec, str(script)], capture_output=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('知らない呼出しの名前'.encode(), done.stderr)
            self.assertNotIn(b'ran', done.stdout)
            # 変異の対照: 名前の一覧に足した包みは、同じ指定を通す(拒んでいたのは名前の確かめ)。
            mutant = Path(temp) / 'wrapper.py'
            text = wrapper.read_text(encoding='utf-8')
            self.assertEqual(text.count("'getcwd', 'clock'}"), 1)
            mutant.write_text(text.replace("'getcwd', 'clock'}", "'getcwd', 'clock', 'bogus'}"), encoding='utf-8')
            done = subprocess.run([sys.executable, '-I', str(mutant), spec, str(script)], capture_output=True)
            self.assertEqual((done.returncode, done.stdout), (0, b'ran\n'))

    def test_socket_path_limit_depends_on_the_platform(self):
        self.assertEqual(self.m.socket_path_limit('linux'), 107)
        self.assertEqual(self.m.socket_path_limit('darwin'), 103)

    def test_timeout_retries_only_for_a_different_stopped_child(self):
        r = self.result
        precheck = r(20, err="ERROR [precheck] 事前検査を実行できない: Command '['bash', '/x/d.sh']' timed out after 0.05 "
                             "seconds\n".encode())
        git = r(20, err="pre\nERROR [git] Git を実行できない: Command '['git', '-C', '/r', 'config']' timed out after "
                        "0.05 seconds\n".encode())
        self.assertIsNotNone(self.m.timeout_stage_difference(precheck, git))
        for broken in ((precheck, precheck), (precheck, r(20, b'x', git.stderr)), (precheck, r(2, err=git.stderr)),
                       (precheck, r(20, err=git.stderr + git.stderr)), (precheck, r(20, err=b'ERROR [git] other\n'))):
            self.assertIsNone(self.m.timeout_stage_difference(*broken))

    # 既存テストの記録の代用品と同じ形の、期限の来た子の結果と記録(0.05 秒の組)。
    PRECHECK_STOP = ("ERROR [precheck] 事前検査を実行できない: Command '['bash', '/c/diff-snapshot.sh', '--cwd', '/r', "
                     "'--precheck']' timed out after 0.05 seconds\n").encode()
    GIT_STOP = ("ERROR [git] Git を実行できない: Command '['git', '-C', '/r', '--no-pager', 'config', '--get', 'k']' "
                "timed out after 0.05 seconds\n").encode()
    BEFORE = json.dumps({'kind': 'precheck', 'args': ['precheck', '--cwd', '/r', '--precheck'], 'env': {}}) + '\n'
    PRECHECK_RECORD = json.dumps({'kind': 'precheck', 'args': ['precheck', '--cwd', '/r', '--precheck'],
                                  'env': {'GIT_CONFIG_NOSYSTEM': '1'}}) + '\n'
    GIT_RECORD = json.dumps({'kind': 'git', 'args': ['-C', '/r', '--no-pager', 'config', '--get', 'k'],
                             'env': {'GIT_CONFIG_COUNT': '0'}}) + '\n'

    def test_record_difference_is_only_the_stopped_childs_own_last_record(self):
        r, explain = self.result, self.m.timeout_record_difference
        precheck, git = r(20, err=self.PRECHECK_STOP), r(20, err=b'note\n' + self.GIT_STOP)
        for stopped, record in ((precheck, self.PRECHECK_RECORD), (git, self.GIT_RECORD)):
            for old_trace, new_trace, writer in ((record, '', '旧版'), ('', record, 'Rust'), (None, record, 'Rust'),
                                                 (self.BEFORE + record, self.BEFORE, '旧版')):
                with self.subTest(old=old_trace, new=new_trace):
                    explained = explain(stopped, stopped, old_trace, new_trace)
                    self.assertIsNotNone(explained)
                    self.assertIn(writer + ' の側だけ', explained)
        self.assertIn('git/git/config', explain(git, git, self.GIT_RECORD, ''))
        self.assertIn('precheck/bash', explain(precheck, precheck, '', self.PRECHECK_RECORD))
        other_env = self.PRECHECK_RECORD.replace('"1"', '"2"')
        accept = self.PRECHECK_RECORD.replace('"--precheck"]', '"--precheck", "--accept", "a"]')
        extra_key = self.PRECHECK_RECORD.replace('"env"', '"cwd": "/r", "env"')
        broken_traces = (
            (self.PRECHECK_RECORD, self.PRECHECK_RECORD),            # 違いが無い
            (self.GIT_RECORD, ''),                                   # 期限の来た子でない子(Git)の記録
            (accept, ''),                                            # 引数が時間切れの文の命令と違う
            (extra_key, ''),                                         # 記録の形が代用品と違う
            ('{"kind": "precheck"\n', ''),                           # 読めない行
            (self.PRECHECK_RECORD.rstrip('\n'), ''),                 # 欠けた行(改行で終わらない)
            (self.PRECHECK_RECORD * 2, ''),                          # 2行の違い
            (self.PRECHECK_RECORD + self.BEFORE, self.BEFORE),       # 足した行が末尾でない
            (self.BEFORE.replace('{}', '{"GIT_DIR": "x"}') + self.PRECHECK_RECORD, self.BEFORE),  # 途中の行の違い
            (other_env, self.PRECHECK_RECORD),                       # 両方に記録があり、中身が違う
        )
        for old_trace, new_trace in broken_traces:
            for pair in ((old_trace, new_trace), (new_trace, old_trace)):
                with self.subTest(traces=pair):
                    self.assertIsNone(explain(precheck, precheck, *pair))
        self.assertIsNone(explain(git, git, self.PRECHECK_RECORD, ''))
        broken_results = ((precheck, r(20, b'x', self.PRECHECK_STOP)), (r(2, err=self.PRECHECK_STOP),) * 2,
                          (r(20, err=self.PRECHECK_STOP * 2),) * 2, (r(20, err=b'ERROR [precheck] other\n'),) * 2,
                          (r(20, err=self.PRECHECK_STOP, timeout=True),) * 2, (precheck, git))
        for old, new in broken_results:
            with self.subTest(results=(old, new)):
                self.assertIsNone(explain(old, new, self.PRECHECK_RECORD, ''))

    def test_paired_call_reruns_only_for_an_explained_record_difference(self):
        stop = self.result(20, err=self.PRECHECK_STOP)
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / 'trace.jsonl'

            def run(plan, *, injection=('--r4g-deadlines', '0.05,0.05'), record_difference=None):
                """`plan` は1回の組ごとの(旧版が足す記録, Rust が足す記録)。起動の数と観測を返す。"""
                steps = [text for pair in plan for text in pair]
                started = []

                def fake_run(argv, **kwargs):
                    with trace.open('a', encoding='utf-8') as handle:
                        handle.write(steps[len(started)])
                    started.append(argv[0])
                    return stop
                trace.write_text(self.BEFORE, encoding='utf-8')
                patches = [mock.patch.object(self.m, 'run_command', fake_run),
                           mock.patch.object(self.m, 'ig_injection', lambda script: list(injection)),
                           mock.patch.object(self.m, 'ig_native', lambda *a: ['rust']),
                           mock.patch.object(self.m, 'OBSERVATIONS', [])]
                if record_difference is not None:
                    patches.append(mock.patch.object(self.m, 'timeout_record_difference', record_difference))
                for patch in patches:
                    patch.start()
                try:
                    try:
                        result = self.m.ig_paired_call([], {}, Path(temp) / 'implementation-git.py', timeout=12,
                                                       trace=trace)
                    except self.m.Difference as error:
                        result = error
                    return result, len(started), list(self.m.OBSERVATIONS), trace.read_text(encoding='utf-8')
                finally:
                    for patch in reversed(patches):
                        patch.stop()

            record = self.PRECHECK_RECORD
            # 1回目は旧版だけが記録を書けた → 流し直し、2回目で両方が同じ。
            result, started, notes, final = run([(record, ''), (record, record)])
            self.assertEqual((result.returncode, started, len(notes)), (20, 4, 1))
            self.assertIn('流し直し 1 回目', notes[0])
            self.assertIn('旧版 の側だけ', notes[0])
            self.assertEqual(final, self.BEFORE + record)
            # 説明できない違い(両方に記録があり環境が違う)は、流し直さずに直ちに不合格。
            result, started, notes, _ = run([(record, record.replace('"1"', '"2"'))] * 2)
            self.assertIsInstance(result, self.m.Difference)
            self.assertEqual((started, notes), (2, []))
            # 説明できる違いが続けば、上限の回数で不合格。
            limit = self.m.IG_DEADLINE_ATTEMPTS
            result, started, notes, _ = run([('', record)] * limit)
            self.assertIsInstance(result, self.m.Difference)
            self.assertEqual((started, len(notes)), (2 * limit, limit - 1))
            # 期限を書き換えない組は流し直さない。
            result, started, notes, _ = run([(record, ''), (record, record)], injection=())
            self.assertIsInstance(result, self.m.Difference)
            self.assertEqual((started, notes), (2, []))
            # 変異の対照: 記録の違いの説明を外すと、1回目で不合格になる(流し直すのは説明があるときだけ)。
            result, started, notes, _ = run([(record, ''), (record, record)], record_difference=lambda *a: None)
            self.assertIsInstance(result, self.m.Difference)
            self.assertEqual((started, notes), (2, []))

    def test_deadline_rewrites_must_be_python_literals(self):
        original = self.m.IG_SCRIPT.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / 'implementation-git.py'
            for literal, ok in (('0.05', True), ('1', True), ('1.0', True), ('0.050', False), ('007', False)):
                copy.write_text(original.replace('GIT_TIMEOUT = 30', 'GIT_TIMEOUT = ' + literal), encoding='utf-8')
                if ok:
                    self.assertEqual(self.m.ig_injection(copy), ['--r4g-deadlines', '120,' + literal])
                else:
                    with self.assertRaises(self.m.Difference):
                        self.m.ig_injection(copy)

    def test_installed_locale_children_of_python_keep_order(self):
        def call(parent, *env):
            return [b'/g'.hex(), [], [item.hex() for item in env], b'/'.hex(), {}, parent]
        added = b'LC_CTYPE=C.UTF-8'
        self.m.installed_locale_lc_ctype('implementation-git', 'self-test',
                                         [call('other', b'A=1', added, b'B=2')], [call('other', b'A=1', b'B=2')])
        with self.assertRaises(self.m.Difference):
            self.m.installed_locale_lc_ctype('implementation-git', 'self-test',
                                             [call('other', b'B=2', added, b'A=1')], [call('other', b'A=1', b'B=2')])
        # bash の孫は値の集合で比べる。
        self.m.installed_locale_lc_ctype('implementation-git', 'self-test',
                                         [call('shell', b'B=2', added, b'A=1')], [call('shell', b'A=1', b'B=2')])

    def test_pipeline_order_swaps_only_concurrent_pairs(self):
        def call(name, *args):
            return [os.fsencode('/s/' + name).hex(), [os.fsencode(a).hex() for a in args], [], '2f']
        ordered = [call('python3', '-I'), call('cut', '-f1'), call('sha256sum'), call('python3', '-c')]
        swapped = [ordered[0], ordered[2], ordered[1], ordered[3]]
        self.assertEqual(self.m.pipeline_order(ordered), self.m.pipeline_order(swapped))
        moved = [ordered[1], ordered[0], ordered[2], ordered[3]]
        self.assertNotEqual(self.m.pipeline_order(ordered), self.m.pipeline_order(moved))

    # ---- 既存テストと件数

    def test_existing_suite_requires_every_class_and_the_fixed_names(self):
        manifest = self.m.existing_manifest()
        self.assertEqual(manifest['total'], 76)
        self.assertEqual({file: sum(item['count'] for item in value['classes'].values())
                          for file, value in manifest['files'].items()},
                         {'test_pending_implementation.py': 47, 'test_implementation_git.py': 19,
                          'test_implementation_git_input.py': 10})
        wrappers = {(file, name): (lambda original, module: type('Wrapped', (original,), {}))
                    for file, item in manifest['files'].items() for name in item['classes']}
        suite = self.m.existing_suite(wrappers)
        self.assertEqual(suite.countTestCases(), 76)
        missing = dict(wrappers)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(AssertionError, '欠落か余分'):
            self.m.existing_suite(missing)
        extra = dict(wrappers)
        extra[('test_x.py', 'X')] = wrappers[next(iter(wrappers))]
        with self.assertRaisesRegex(AssertionError, '欠落か余分'):
            self.m.existing_suite(extra)
        hiding = dict(wrappers)
        key = ('test_implementation_git_input.py', 'InputCharacterization')
        hiding[key] = lambda original, module: type('Hiding', (original,), {'test_not_existing': lambda self: None})
        with self.assertRaisesRegex(AssertionError, '収集が欠落'):
            self.m.existing_suite(hiding)
        unrelated = dict(wrappers)
        unrelated[key] = lambda original, module: type('Unrelated', (unittest.TestCase,), {})
        with self.assertRaisesRegex(AssertionError, '収集が欠落'):
            self.m.existing_suite(unrelated)
        changed = json.loads(json.dumps(manifest))
        changed['files'][key[0]]['classes'][key[1]]['count'] += 1
        with self.assertRaisesRegex(AssertionError, '固定一覧'):
            self.m.existing_suite(wrappers, manifest=changed)
        moved = json.loads(json.dumps(manifest))
        moved['files'][key[0]]['sha256'] = '0' * 64
        with self.assertRaisesRegex(AssertionError, '固定一覧の時点'):
            self.m.existing_suite(wrappers, manifest=moved)

    def test_zero_failed_skipped_and_missing_cases_are_rejected(self):
        with self.assertRaises(AssertionError):
            self.m.run_suite(unittest.TestSuite())

        class Failure(unittest.TestCase):
            def test_broken(self):
                self.fail('deliberate')

        class Skipped(unittest.TestCase):
            def test_skip(self):
                self.skipTest('deliberate')

        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def test_expected(self):
                self.fail('deliberate')

        class Unexpected(unittest.TestCase):
            @unittest.expectedFailure
            def test_unexpected(self):
                pass

        with mock.patch('sys.stderr', io.StringIO()):
            for case in (Failure, Skipped, Expected, Unexpected):
                with self.assertRaises(AssertionError):
                    self.m.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case))

    # ---- strace

    def strace_files(self, temp, lines):
        path = Path(temp) / 'trace.4242'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return [path]

    @staticmethod
    def hex_string(raw):
        return ''.join('\\x%02x' % byte for byte in raw)

    def test_strace_judge_finds_absolute_relative_returned_and_named_opens(self):
        root = '/state/dev-workflow'.encode()
        candidates = [root + b'/guard-a', root + b'/guard-\xff', root + '/guard-é'.encode()]
        h = self.hex_string
        lines = [
            'openat(AT_FDCWD<%s>, "%s", O_RDONLY) = 3<%s>' % (h(b'/w'), h(root), h(root)),
            'newfstatat(3<%s>, "%s", {st_mode=S_IFDIR}, AT_SYMLINK_NOFOLLOW) = 0' % (h(root), h(b'guard-a')),
            'openat(3<%s>, "%s", O_RDONLY|O_NOFOLLOW) = 4<%s>' % (h(root), h(b'owner-a'), h(root + b'/owner-a')),
            '+++ exited with 0 +++',
        ]
        with tempfile.TemporaryDirectory() as temp:
            events = self.m.parse_strace(self.strace_files(temp, lines))
            self.assertEqual([event.call for event in events], ['openat', 'newfstatat', 'openat'])
            self.assertEqual(self.m.opened_candidates(events, candidates), [])
            bad_lines = [
                'openat(AT_FDCWD<%s>, "%s", O_RDONLY) = 5<%s>' % (h(b'/w'), h(candidates[0]), h(candidates[0])),
                'openat(3<%s>, "%s", O_RDONLY) = -1 ENOENT (No such file or directory)' % (h(root), h(b'guard-\xff')),
                'openat(3<%s>, "%s", O_RDONLY) = 6<%s>' % (h(root), h('guard-é/inner'.encode()), h(root + '/guard-é/inner'.encode())),
                'open("%s", O_RDONLY) = -1 ENOENT (No such file or directory)' % h(candidates[1] + b'/x'),
                'openat(AT_FDCWD<%s>, "%s", O_RDONLY) = 7<%s>' % (h(root), h(b'elsewhere'), h(candidates[0])),
            ]
            for line in bad_lines:
                with self.subTest(line=line[:40]):
                    events = self.m.parse_strace(self.strace_files(temp, [line]))
                    self.assertEqual(len(self.m.opened_candidates(events, candidates)), 1)
            # 相対の open は作業場所と、注釈の無い AT_FDCWD も作業場所と結ぶ。`..` は字面で畳む。
            relative = [
                ('open("%s", O_RDONLY) = -1 ENOENT (No such file or directory)' % h(b'guard-a'), '/state/dev-workflow'),
                ('open("%s", O_RDONLY) = 3' % h(b'dev-workflow/../dev-workflow/guard-a/x'), '/state'),
                ('openat(AT_FDCWD, "%s", O_RDONLY) = 3' % h(b'./guard-\xff'), '/state/dev-workflow'),
                ('openat(3<%s>, "%s", O_RDONLY) = 3' % (h(b'/elsewhere'), h(b'../state/dev-workflow/guard-a')), '/w'),
                ('openat(3<%s>, "%s", O_RDONLY) = 3' % (h(b'/other'), h(b'deep/guard-a/file')), '/w'),
            ]
            for line, cwd in relative:
                with self.subTest(line=line[:40]):
                    events = self.m.parse_strace(self.strace_files(temp, [line]))
                    self.assertEqual(len(self.m.opened_candidates(events, candidates, cwd=cwd)), 1)
            harmless = 'open("%s", O_RDONLY) = 3' % h(b'owner-a')
            events = self.m.parse_strace(self.strace_files(temp, [harmless]))
            self.assertEqual(self.m.opened_candidates(events, candidates, cwd='/state/dev-workflow'), [])
            with self.assertRaisesRegex(ValueError, '基準へ結べない'):
                self.m.opened_candidates(events, candidates)
            unresolved = 'openat(5, "%s", O_RDONLY) = 3' % h(b'owner-a')
            with self.assertRaisesRegex(ValueError, '基準へ結べない'):
                self.m.opened_candidates(self.m.parse_strace(self.strace_files(temp, [unresolved])), candidates, cwd='/w')
            execs = self.m.parse_strace(self.strace_files(temp, ['execve("%s", ["%s"], 0x7ffd /* 3 vars */) = 0' % (h(b'/usr/bin/git'), h(b'git'))]))
            self.assertEqual(self.m.executions(execs), [b'/usr/bin/git'])
            for broken in ('openat(AT_FDCWD, "\\q", O_RDONLY) = 3', 'not a syscall line', 'openat(3<\u00e9>, "x", 0) = 1'):
                with self.assertRaises(ValueError):
                    self.m.opened_candidates(self.m.parse_strace(self.strace_files(temp, [broken])), candidates)


class StraceControl(unittest.TestCase):
    """本物の strace の対照。候補を開く処理を判定が見逃さず、開かない処理を数えないこと。"""

    def setUp(self):
        self.m = get_subject()

    def trace(self, temp, code, root):
        prefix = Path(temp) / 'trace'
        env = dict(os.environ, LANG='ja_JP.UTF-8')
        argv, strace_env = self.m.strace_command([sys.executable, '-I', '-c', code, os.fsencode(root)], prefix, env)
        self.assertEqual(strace_env['LC_ALL'], 'C')
        result = subprocess.run(argv, env=strace_env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        return self.m.parse_strace(Path(temp).glob('trace.*'))

    def test_real_strace_records_round_trip_and_the_judge_detects_opens(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'state'
            root.mkdir()
            names = [b'guard-a', 'guard-é'.encode(), b'guard-\xff']
            for name in names:
                with open(os.path.join(os.fsencode(root), name), 'wb'):
                    pass
            candidates = [os.path.join(os.fsencode(root), name) for name in names]
            only_stat = ("import os, sys\nd = os.fsencode(sys.argv[1])\nfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)\n"
                         "for n in os.listdir(fd):\n    os.stat(n, dir_fd=fd, follow_symlinks=False)\n")
            events = self.trace(temp, only_stat, root)
            self.assertEqual(self.m.opened_candidates(events, candidates), [])
            self.assertTrue(any(self.m.annotation(event.args[0]) for event in events if event.call == 'openat'))
            for name in names:
                for mode in ('absolute', 'relative'):
                    for path in Path(temp).glob('trace.*'):
                        path.unlink()
                    if mode == 'absolute':
                        code = "import os, sys\nos.close(os.open(os.fsencode(sys.argv[1]) + b'/' + %r, os.O_RDONLY))\n" % name
                    else:
                        code = ("import os, sys\nfd = os.open(os.fsencode(sys.argv[1]), os.O_RDONLY)\n"
                                "os.close(os.open(%r, os.O_RDONLY, dir_fd=fd))\n" % name)
                    with self.subTest(name=name, mode=mode):
                        events = self.trace(temp, code, root)
                        found = self.m.opened_candidates(events, candidates)
                        self.assertEqual(len(found), 1)
                        self.assertIn(name, b''.join(self.m.string_arg(arg) for arg in found[0].args if arg.startswith('"')))


if __name__ == '__main__':
    unittest.main()
