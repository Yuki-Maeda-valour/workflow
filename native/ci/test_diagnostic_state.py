#!/usr/bin/env python3
"""R4D-3 比較器(check_diagnostic_state.py)の自己検査。

比較器が、結果の 1 件の値・理由・終了値・失敗の種類・予算の確かめの回数・差し替えの部品の呼出し
(argv・input_bytes・output_limit・回数)の違い、件数の下限の不足、予算の条件の欠け、T の旧版の結果の失敗、
Python の版が 3.10 系でない、root、桁の上限が 4300 でない、を、それぞれ失敗にすることを確かめる。
小さい組(scope=mini。本物の take の組 T を作らない)の cases.json と、旧版の結果をそのまま写した
「完全な Rust の結果」を使い、その結果へ 1 か所ずつ手を入れる。
scripts/test_diagnostic_state.py(旧版の検査)と同じ Python のプロセスに同じ名前で読み込まない。
一時フォルダは自分で作った物だけを消し、作業ツリーにファイルを残さない。
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import check_diagnostic_state as check  # noqa: E402

CHECKER = HERE / 'check_diagnostic_state.py'
_state = {}


def setUpModule():
    """小さい組の cases.json を 1 回だけ作る(自己検査の間は読み取りだけ)。"""
    root = Path(tempfile.mkdtemp(prefix='r4d3-selftest-'))
    _state['root'] = root
    destination = root / 'gen'
    destination.mkdir()
    with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'}), \
            contextlib.redirect_stderr(io.StringIO()):
        path = check.generate(destination, 'mini')
    _state['path'] = path
    _state['text'] = path.read_text()
    _state['cases'] = json.loads(_state['text'])
    _state['perfect'] = json.dumps({'results': {case['id']: case['legacy'] for case in _state['cases']['cases']}})


def tearDownModule():
    shutil.rmtree(_state['root'], ignore_errors=True)


def perfect_rust(cases=None):
    """旧版の結果をそのまま写した、完全に一致する Rust の結果(JSON を経て写す。deepcopy より速い)。"""
    if cases is None:
        return json.loads(_state['perfect'])
    return json.loads(json.dumps({'results': {case['id']: case['legacy'] for case in cases['cases']}}))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(_state['text'])
        self.rust = perfect_rust()
        self.tmp = Path(tempfile.mkdtemp(prefix='r4d3-compare-', dir=_state['root']))
        patcher = mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def compare(self, cases=None, rust=None, expect_mismatch=False):
        """比較器の --compare を同じ Python のプロセスで流す。終了値と標準エラーを返す。"""
        # 手を入れていない cases.json は、作った物をそのまま使う(書き直す手間を省く)。
        cases_path = _state['path'] if cases is None else self.tmp / 'cases.json'
        rust_path = self.tmp / 'rust.json'
        if cases is not None:
            cases_path.write_text(json.dumps(cases))
        rust_path.write_text(json.dumps(self.rust if rust is None else rust))
        argv = ['--compare', str(cases_path), str(rust_path)] + (['--expect-mismatch'] if expect_mismatch else [])
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = check.main(argv)
        return code, stream.getvalue()

    def pick(self, predicate):
        for case in self.cases['cases']:
            for index, output in enumerate(case['legacy']):
                if predicate(case, output):
                    return case, self.rust['results'][case['id']][index]
        self.fail('条件に合う入力が無い')

    def assert_fails(self, text_part, **changes):
        code, text = self.compare(**changes)
        self.assertNotEqual(code, 0, text)
        self.assertIn(text_part, text)

    def test_the_perfect_copy_passes(self):
        code, text = self.compare()
        self.assertEqual(code, 0, text)
        self.assertIn('不一致 0 件', text)
        for group in check.GROUPS:
            self.assertIn('R4D-3 件数: %s=' % group, text)
        self.assertRegex(text, r'R4D-3 予算の条件: M=\d+・K=\d+・R=\d+')
        self.assertIn('R4D-3 比べた結果: ', text)

    def test_changed_value_fails(self):
        for fn in ('cfield', 'parse_records', 'parse_origins', 'file_refs', 'resolve_refs', 'head', 'oid', 'ancestors',
                   'valid_ref', 'integer'):
            with self.subTest(fn=fn):
                self.rust = perfect_rust()
                case, output = self.pick(lambda c, o, fn=fn: c['fn'] == fn and o['result'][0] == 'ok'
                                         and o['result'][1] not in (None, [], ''))
                value = output['result'][1]
                output['result'][1] = (not value) if isinstance(value, bool) else (
                    value + 1 if isinstance(value, int) else ['changed'])
                self.assert_fails(case['id'])

    def test_changed_reason_fails(self):
        case, output = self.pick(lambda c, o: o['result'][0] == 'failure')
        output['result'][1] += 'x'
        self.assert_fails(case['id'])

    def test_changed_exit_code_fails(self):
        case, output = self.pick(lambda c, o: o['result'][0] == 'failure' and o['result'][2] == 23)
        output['result'][2] = 22
        self.assert_fails(case['id'])

    def test_changed_failure_kind_fails(self):
        for kind, replacement in (('failure', ['value-error']), ('value-error', ['unicode-error']),
                                  ('unicode-error', ['value-error']), ('os-error', ['os-error', 99999]),
                                  ('ok', ['failure', 'state-schema', 22])):
            with self.subTest(kind=kind):
                self.rust = perfect_rust()
                case, output = self.pick(lambda c, o, kind=kind: o['result'][0] == kind)
                output['result'] = replacement
                self.assert_fails(case['id'])

    def test_changed_check_count_fails_except_for_records_and_origins(self):
        for fn in ('check_manifest', 'file_refs', 'resolve_refs', 'cfield'):
            with self.subTest(fn=fn):
                self.rust = perfect_rust()
                case, output = self.pick(lambda c, o, fn=fn: c['fn'] == fn)
                output['checks'] += 1
                self.assert_fails(case['id'])
        # parse_records と parse_origins は、旧版と Rust で予算を使う範囲が違うので、回数を比べない。
        self.rust = perfect_rust()
        for fn in check.UNCOUNTED_FUNCTIONS:
            _, output = self.pick(lambda c, o, fn=fn: c['fn'] == fn)
            output['checks'] += 7
        code, text = self.compare()
        self.assertEqual(code, 0, text)

    def test_changed_stub_calls_fail(self):
        def process(c, o):
            return any(event[0] == 'process' for event in o['calls'])
        changes = {
            'argv': lambda event: event[1].__setitem__(0, event[1][0] + '00'),
            'input_bytes': lambda event: event.__setitem__(2, event[2] + '0a'),
            'output_kind': lambda event: event.__setitem__(3, 'stderr'),
            'output_limit': lambda event: event.__setitem__(4, 4096 if event[4] is None else None),
        }
        for name, change in changes.items():
            for fn in ('resolve_refs', 'oid'):
                with self.subTest(change=name, fn=fn):
                    self.rust = perfect_rust()
                    case, output = self.pick(lambda c, o, fn=fn: c['fn'] == fn and process(c, o))
                    change(next(event for event in output['calls'] if event[0] == 'process'))
                    self.assert_fails(case['id'])
        for fn, kind in (('resolve_refs', 'process'), ('oid', 'head'), ('oid', 'refs'), ('head', 'refs')):
            with self.subTest(removed=kind, fn=fn):
                self.rust = perfect_rust()
                case, output = self.pick(lambda c, o, fn=fn, kind=kind: c['fn'] == fn
                                         and any(event[0] == kind for event in o['calls']))
                output['calls'] = [event for event in output['calls'] if event[0] != kind]
                self.assert_fails(case['id'])
            with self.subTest(added=kind, fn=fn):
                self.rust = perfect_rust()
                case, output = self.pick(lambda c, o, fn=fn, kind=kind: c['fn'] == fn
                                         and any(event[0] == kind for event in o['calls']))
                output['calls'] = output['calls'] + [copy.deepcopy(output['calls'][-1])]
                self.assert_fails(case['id'])

    def test_missing_or_extra_results_fail(self):
        case = self.cases['cases'][0]
        del self.rust['results'][case['id']]
        self.assert_fails(case['id'])
        self.rust = perfect_rust()
        case = next(c for c in self.cases['cases'] if len(c['conds']) > 1)
        self.rust['results'][case['id']].pop()
        self.assert_fails(case['id'])
        self.rust = perfect_rust()
        self.rust['results']['Z-9999'] = []
        self.assert_fails('Z-9999')

    def test_count_shortage_fails(self):
        cases = json.loads(_state['text'])
        drop = {c['id'] for c in [c for c in cases['cases'] if c['group'] == 'F'][check.MINIMUM['F'] - 1:]}
        cases['cases'] = [c for c in cases['cases'] if c['id'] not in drop]
        self.assertEqual(check.count_inputs(cases)['F'], check.MINIMUM['F'] - 1)
        self.assert_fails('F=%d' % (check.MINIMUM['F'] - 1), cases=cases, rust=perfect_rust(cases))
        # 名前付きの入力が一覧から消えても失敗にする。
        cases = json.loads(_state['text'])
        cases['tags']['L'] = [tag for tag in cases['tags']['L'] if tag != 'digits-4301']
        self.assert_fails('digits-4301', cases=cases)
        # full の範囲では、T の 4 つが要る(作れなかった入力の行が無ければ)。
        cases = json.loads(_state['text'])
        cases['scope'] = 'full'
        self.assert_fails('T=0', cases=cases)
        # Mac で道具が足りないときだけ、T の下限を 0 にする。その行を出す。
        cases['platform'] = 'darwin'
        cases['not_created'] = ['T(darwin・GNU の stat)']
        code, text = self.compare(cases=cases)
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-3 作れなかった入力: T(darwin・GNU の stat)', text)
        cases['platform'] = 'linux'
        self.assert_fails('Mac でない', cases=cases)
        with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: ''}):
            self.assert_fails('full でない')

    def test_missing_budget_conditions_fail(self):
        for drop in ('n-last', 'n-first', 'stop'):
            with self.subTest(drop=drop):
                cases = json.loads(_state['text'])
                case = next(c for c in cases['cases'] if c['group'] == 'M' and len(c['conds']) >= 4)
                index = {'n-last': len(case['conds']) - 2, 'n-first': 1, 'stop': len(case['conds']) - 1}[drop]
                for key in ('conds', 'legacy'):
                    del case[key][index]
                self.assert_fails(case['id'], cases=cases, rust=perfect_rust(cases))
        cases = json.loads(_state['text'])
        case = next(c for c in cases['cases'] if c['group'] == 'F')
        case['conds'].append(['stop'])
        case['legacy'].append(copy.deepcopy(case['legacy'][0]))
        self.assert_fails(case['id'], cases=cases, rust=perfect_rust(cases))

    def take_case(self, records_result):
        part = {'result': ['ok', None], 'checks': 1, 'calls': []}
        origins = {'result': ['ok', {'groups': [], 'admin': ['', '']}], 'checks': 0, 'calls': []}
        records = {'result': records_result, 'checks': 0, 'calls': []}
        output = {'result': ['ok', {'manifest': part, 'records': records, 'origins': origins}], 'checks': 0, 'calls': []}
        return {'id': 'T-0000', 'group': 'T', 'fn': 'take', 'input': {}, 'conds': [['normal']], 'legacy': [output]}

    def test_take_legacy_failure_fails(self):
        cases = json.loads(_state['text'])
        cases['cases'].append(self.take_case(['ok', {'cwd': None, 'toplevel': None, 'contexts': []}]))
        code, text = self.compare(cases=cases, rust=perfect_rust(cases))
        self.assertEqual(code, 0, text)
        cases['cases'][-1] = self.take_case(['failure', 'state-binding', 22])
        self.assert_fails('T の旧版の結果が成功でない', cases=cases, rust=perfect_rust(cases))

    def test_unexpected_legacy_kind_and_origin_count_fail(self):
        cases = json.loads(_state['text'])
        case = next(c for c in cases['cases'] if c['fn'] == 'cfield')
        case['legacy'][0]['result'] = ['other', 'IndexError', '']
        self.assert_fails('想定の外', cases=cases, rust=perfect_rust(cases))
        cases = json.loads(_state['text'])
        case = next(c for c in cases['cases'] if c['fn'] == 'parse_origins' and c['legacy'][0]['result'][0] == 'ok')
        case['legacy'][0]['checks'] += 1
        self.assert_fails('candidate の数と違う', cases=cases, rust=perfect_rust(cases))

    def test_expect_mismatch_needs_at_least_one_mismatch(self):
        code, text = self.compare(expect_mismatch=True)
        self.assertNotEqual(code, 0, text)
        self.assertIn('比較が働いていない', text)
        case, output = self.pick(lambda c, o: c['group'] == 'M' and o['result'][0] == 'failure')
        output['result'][1] += '-r4d3-mismatch'
        code, text = self.compare(expect_mismatch=True)
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-3 比較(差し込み): 不一致 1 件・期待どおり失敗', text)
        # 結果が欠けても、期待どおりとは扱わない。
        del self.rust['results'][self.cases['cases'][0]['id']]
        code, text = self.compare(expect_mismatch=True)
        self.assertNotEqual(code, 0, text)


class LegacyHarnessTests(unittest.TestCase):
    def test_outcome_separates_the_failure_kinds(self):
        R = check.legacy()['R']
        self.assertEqual(check.outcome(lambda: R.fail('x', 23)), ['failure', 'x', 23])
        self.assertEqual(check.outcome(lambda: '\ud800'.encode()), ['unicode-error'])
        self.assertEqual(check.outcome(lambda: int('1' * 4301)), ['value-error'])
        self.assertEqual(check.outcome(lambda: os.stat(b'/r4d3-none/x')), ['os-error', 2])
        self.assertEqual(check.outcome(lambda: [][1])[:2], ['other', 'IndexError'])
        with self.assertRaises(check.CheckError):
            check.outcome(lambda: (_ for _ in ()).throw(check.CheckError('x')))

    def test_nth_budget_expires_on_the_nth_check(self):
        R = check.legacy()['R']
        for n in (1, 2, 5):
            budget, clock = check.make_budget(['n', n])
            for _ in range(n - 1):
                budget.check()
            with self.assertRaises(R.Failure):
                budget.check()
            self.assertEqual(clock.calls - 1, n)
        budget, clock = check.make_budget(['stop'])
        with self.assertRaises(R.Failure):
            budget.check()
        self.assertEqual(clock.calls - 1, 0)

    def test_stub_runtime_encodes_before_recording(self):
        recorder = []
        runtime = check.StubRuntime(recorder, [['rc', 1, '0a']])
        head = [check.FAKE_GIT, *check.legacy()['G'].SAFE_GIT]
        with self.assertRaises(UnicodeEncodeError):
            runtime.process(head + ['\ud800'])
        self.assertEqual(recorder, [])
        self.assertEqual(runtime.process(head + ['\udc80', b'\xff'], output_limit=7), (1, b'\n'))
        self.assertEqual(recorder, [['process', ['80', 'ff'], '', 'git_output', 7]])
        with self.assertRaises(check.CheckError):
            runtime.process(['git', 'x'])


class PrerequisiteTests(unittest.TestCase):
    def test_wrong_python_fails(self):
        with mock.patch.object(check.sys, 'version_info', (3, 11, 0, 'final', 0)):
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        self.assertIn('3.10', str(raised.exception))

    def test_root_fails_before_writing(self):
        with mock.patch.object(check.os, 'geteuid', return_value=0):
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        self.assertIn('root', str(raised.exception))
        with tempfile.TemporaryDirectory(dir=_state['root']) as directory, \
                mock.patch.object(check.os, 'geteuid', return_value=0):
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                code = check.main(['--generate', directory])
            self.assertNotEqual(code, 0)
            self.assertIn('root', stream.getvalue())
            self.assertEqual(os.listdir(directory), [])

    def test_digit_limit_other_than_4300_fails(self):
        for value in (640, 0, None):
            with self.subTest(value=value):
                with mock.patch.object(check, 'int_max_str_digits', return_value=value):
                    with self.assertRaises(check.CheckError) as raised:
                        check.check_prerequisites()
                self.assertIn('4300', str(raised.exception))

    def test_take_shortages_fail_on_linux_and_are_listed_on_mac(self):
        def versions(argv, pattern):
            return ((3, 2), 'GNU bash, version 3.2.57') if argv[0] == 'bash' else ((2, 56), 'git version 2.56.1')
        with mock.patch.object(check, 'tool_version', side_effect=versions), \
                mock.patch.object(check.sys, 'platform', 'linux'):
            with self.assertRaises(check.CheckError) as raised:
                check.take_shortages()
        self.assertIn('bash 4.0', str(raised.exception))
        with mock.patch.object(check, 'tool_version', side_effect=versions), \
                mock.patch.object(check.sys, 'platform', 'darwin'):
            missing = check.take_shortages()
        self.assertTrue(any(item.startswith('bash 4.0') for item in missing), missing)

    def test_compare_runs_the_selftest_only_without_the_marker(self):
        # 印(環境変数)が付いた子は自己検査へ届かない。印が無ければ流す(--expect-mismatch は流さない)。
        self.assertEqual(check.NO_SELFTEST_ENV, 'R4D3_NO_SELFTEST')
        rust = Path(_state['root']) / 'perfect.json'
        rust.write_text(json.dumps(perfect_rust(_state['cases'])))
        with mock.patch.object(check, 'run_selftest', side_effect=AssertionError('流してはいけない')) as selftest, \
                mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check.compare_main(str(_state['path']), str(rust), False), 0)
            selftest.assert_not_called()
        environment = {k: v for k, v in os.environ.items() if k != check.NO_SELFTEST_ENV}
        with mock.patch.object(check, 'run_selftest') as selftest, \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1'}), \
                contextlib.redirect_stderr(io.StringIO()):
            check.compare_main(str(_state['path']), str(rust), False)
            selftest.assert_called_once()
        with mock.patch.object(check, 'run_selftest') as selftest, \
                mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(check.main(['--compare', str(_state['path']), str(rust), '--expect-mismatch']), 0)
            selftest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
