#!/usr/bin/env python3
"""R4D-1 比較器(check_diagnostic_reftable.py)の自己検査。

比較器が、結果の 1 件の違い・並びの違い・理由の字面の違い・終了値の違い・例外の種類の違い・件数の不足・
古い git・root を、それぞれ失敗にすることを確かめる。小さい組(scope=mini)の manifest と、
旧版の結果をそのまま写した「完全な Rust の結果」を使い、その結果へ 1 か所ずつ手を入れる。
scripts/test_diagnostic_reftable.py(旧版の検査)と同じ Python のプロセスに同じ名前で読み込まない。
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import check_diagnostic_reftable as check  # noqa: E402

CHECKER = HERE / 'check_diagnostic_reftable.py'
_state = {}


def setUpModule():
    """小さい組の manifest を 1 回だけ作る(自己検査の間は読み取りだけ)。"""
    root = Path(tempfile.mkdtemp(prefix='r4d1-selftest-'))
    _state['root'] = root
    destination = root / 'gen'
    destination.mkdir()
    base = root / 'base'
    base.mkdir()
    with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'}):
        manifest_path = check.generate(destination, base, 'mini')
    _state['manifest_path'] = manifest_path
    _state['manifest'] = json.loads(manifest_path.read_text())


def tearDownModule():
    shutil.rmtree(_state['root'], ignore_errors=True)


def perfect_rust(manifest):
    """旧版の結果をそのまま写した、完全に一致する Rust の結果。"""
    results = {}
    for item in manifest['inputs']:
        results[item['id']] = {str(i): copy.deepcopy(value) for i, value in enumerate(item['legacy'])}
    budget = {case['id']: copy.deepcopy(case['legacy']) for case in manifest['budget_cases']}
    return {'results': results, 'budget': budget, 't7_hex': manifest['t7_hex']}


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.manifest = copy.deepcopy(_state['manifest'])
        self.rust = perfect_rust(self.manifest)
        self.tmp = Path(tempfile.mkdtemp(prefix='r4d1-compare-', dir=_state['root']))
        patcher = mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def compare(self, manifest=None, rust=None, expect_mismatch=False):
        """比較器の --compare を同じ Python のプロセスで流す。終了値と標準エラーを返す。"""
        manifest_path = self.tmp / 'manifest.json'
        rust_path = self.tmp / 'rust.json'
        manifest_path.write_text(json.dumps(self.manifest if manifest is None else manifest))
        rust_path.write_text(json.dumps(self.rust if rust is None else rust))
        argv = ['--compare', str(manifest_path), str(rust_path)] + (['--expect-mismatch'] if expect_mismatch else [])
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = check.main(argv)
        return code, stream.getvalue()

    def pick(self, predicate):
        for item in self.manifest['inputs']:
            for index, value in enumerate(item['legacy']):
                if predicate(item, index, value):
                    return item, index
        self.fail('条件に合う入力が無い')

    def test_the_perfect_copy_passes(self):
        code, text = self.compare()
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-1 reftable の比較: 入力', text)
        self.assertIn('不一致 0 件', text)

    def assert_fails(self, text_part, **changes):
        code, text = self.compare(**changes)
        self.assertNotEqual(code, 0, text)
        self.assertIn(text_part, text)

    def test_one_changed_result_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'ok' and v[1] and v[1][0][1])
        self.rust['results'][item['id']][str(index)][1][0][1] = '00' * 20
        self.assert_fails(item['id'])

    def test_changed_order_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'ok' and len(v[1]) >= 2)
        rows = self.rust['results'][item['id']][str(index)][1]
        rows[0], rows[1] = rows[1], rows[0]
        self.assert_fails(item['id'])

    def test_changed_reason_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'err' and v[1] == 'reftable')
        self.rust['results'][item['id']][str(index)][2] += 'x'
        self.assert_fails(item['id'])

    def test_changed_exit_code_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'err' and v[1] == 'reftable')
        self.rust['results'][item['id']][str(index)][3] = 20 if item['legacy'][index][3] == 23 else 23
        self.assert_fails(item['id'])

    def test_changed_exception_kind_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'err' and v[1] == 'budget')
        self.rust['results'][item['id']][str(index)][1] = 'reftable'
        self.assert_fails(item['id'])
        item, index = self.pick(lambda i, n, v: v[0] == 'err' and v[1] == 'other')
        self.rust = perfect_rust(self.manifest)
        self.rust['results'][item['id']][str(index)][1] = 'reftable'
        self.assert_fails(item['id'])

    def test_success_turned_into_failure_and_back_fails(self):
        item, index = self.pick(lambda i, n, v: v[0] == 'ok')
        self.rust['results'][item['id']][str(index)] = ['err', 'reftable', 'invalid-reftable', 23]
        self.assert_fails(item['id'])
        item, index = self.pick(lambda i, n, v: v[0] == 'err')
        self.rust = perfect_rust(self.manifest)
        self.rust['results'][item['id']][str(index)] = ['ok', []]
        self.assert_fails(item['id'])

    def test_missing_rust_result_fails(self):
        item = self.manifest['inputs'][0]
        del self.rust['results'][item['id']]
        self.assert_fails(item['id'])
        self.rust = perfect_rust(self.manifest)
        del self.rust['results'][item['id']]['0']
        self.assert_fails(item['id'])

    def test_changed_budget_only_result_fails(self):
        case = self.manifest['budget_cases'][0]
        self.rust['budget'][case['id']][0] = ['fail', 'time-limit', 20]
        self.assert_fails(case['id'])
        self.rust = perfect_rust(self.manifest)
        remaining = next(c for c in self.manifest['budget_cases'] if c['id'] == 'g5-remaining')
        index = next(i for i, op in enumerate(remaining['ops']) if op[0] == 'remaining')
        self.rust['budget']['g5-remaining'][index] = ['ok', 0.5]
        self.assert_fails('g5-remaining')

    def test_count_shortage_fails(self):
        item = next(i for i in self.manifest['inputs'] if i['group'] == 'g1-truncate')
        manifest = copy.deepcopy(self.manifest)
        manifest['inputs'] = [i for i in manifest['inputs'] if i['id'] != item['id']]
        rust = perfect_rust(manifest)
        self.assert_fails('g1-truncate', manifest=manifest, rust=rust)
        # 名前付きの一覧から項目が消えても失敗にする。
        manifest = copy.deepcopy(self.manifest)
        manifest['named']['g3-boundary'] = [t for t in manifest['named']['g3-boundary'] if t != 'varint']
        self.assert_fails('g3-boundary', manifest=manifest, rust=perfect_rust(manifest))
        # git の入力が 1 件も無い manifest は、full の範囲として認めない。
        with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: ''}):
            self.assert_fails('full', manifest=self.manifest)

    def test_t7_constant_mismatch_fails(self):
        self.rust['t7_hex'] = '00' + self.rust['t7_hex'][2:]
        self.assert_fails('T7')

    def test_changed_input_directory_fails(self):
        # 実際のフォルダには触れず、manifest が覚えた状態の値を変える(共有の入力を壊さない)。
        item_id = next(iter(self.manifest['signatures']))
        self.manifest['signatures'][item_id][1] = '0' * 64
        self.assert_fails(item_id)

    def test_expect_mismatch_needs_at_least_one_quantity_mismatch(self):
        code, text = self.compare(expect_mismatch=True)
        self.assertNotEqual(code, 0, text)
        self.assertIn('比較が働いていない', text)
        biased = perfect_rust(self.manifest)
        changed = 0
        for item in self.manifest['inputs']:
            for index, cond in enumerate(item['conds']):
                if cond[0] == 'q' and item['legacy'][index][0] == 'ok' and changed < 3:
                    biased['results'][item['id']][str(index)] = ['err', 'budget', 'management_output-limit', 20]
                    changed += 1
        self.assertGreater(changed, 0)
        code, text = self.compare(rust=biased, expect_mismatch=True)
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-1 reftable の比較(差し込み): 不一致 %d 件・期待どおり失敗' % changed, text)
        # 「量」の条件の結果が欠けても、期待どおりとは扱わない。
        partial = perfect_rust(self.manifest)
        item = next(i for i in self.manifest['inputs'] if any(c[0] == 'q' for c in i['conds']))
        del partial['results'][item['id']]
        code, text = self.compare(rust=partial, expect_mismatch=True)
        self.assertNotEqual(code, 0, text)


class ConditionTests(unittest.TestCase):
    def test_nth_points_always_include_both_last_checks(self):
        for k in (1, 2, 7, 40):
            for plan in ('header', 'representative'):
                conds = check.conditions(plan, k, [100, 200])
                nths = [c[1] for c in conds if c[0] == 'n']
                self.assertIn(1, nths)
                self.assertIn(k, nths)
                self.assertIn(k + 1, nths)
            self.assertIn(-(-k // 2), [c[1] for c in check.conditions('header', k, []) if c[0] == 'n'])
            full = [c[1] for c in check.conditions('full', k, [10, 20]) if c[0] == 'n']
            self.assertEqual(full, list(range(1, k + 2)))

    def test_quantity_points_use_cumulative_charges(self):
        conds = check.conditions('full', 3, [40, 90])
        quantities = [c[1] for c in conds if c[0] == 'q']
        for value in (0, 1, 40, 39, 90, 89):
            self.assertIn(value, quantities)
        totals = [c[1] for c in check.conditions('totals', 3, [40, 90]) if c[0] == 'q']
        self.assertEqual(totals, [90, 89])
        self.assertEqual(check.conditions('normal', 3, [40]), [['normal']])
        guard = check.conditions('guard', 3, [])
        self.assertEqual([c[0] for c in guard], ['normal', 'stop', 'expired'])

    def test_legacy_results_are_classified_by_exception_type(self):
        rd, rt, _ = check.legacy()
        with tempfile.TemporaryDirectory() as directory:
            root = os.fsencode(directory)
            budget = check.make_budget(['normal'])
            self.assertEqual(check.result_of(lambda: rd.read_stack(root, 'sha1', budget))[:3], ['err', 'reftable', 'incomplete-metadata'])
            self.assertEqual(check.result_of(lambda: rd.read_stack(root + b'\0', 'sha1', check.make_budget(['normal']))),
                             ['err', 'other', 'ValueError', 0])
            self.assertEqual(check.result_of(lambda: rd.read_stack(root, 'sha1', check.make_budget(['stop']))),
                             ['err', 'budget', 'interrupted', 20])
            self.assertEqual(check.result_of(lambda: rd.read_stack(root, 'sha1', check.make_budget(['expired']))),
                             ['err', 'budget', 'time-limit', 20])
            self.assertEqual(check.result_of(lambda: rd.read_stack(root, 'SHA1', check.make_budget(['stop']))),
                             ['err', 'reftable', 'reftable-hash-mismatch', 23])

    def test_nth_check_budget_expires_on_the_nth_check(self):
        rd, rt, _ = check.legacy()
        for n in (1, 2, 3):
            budget = check.make_budget(['n', n])
            for _ in range(n - 1):
                budget.check()
            with self.assertRaises(rt.Failure):
                budget.check()


class PrerequisiteTests(unittest.TestCase):
    def test_old_git_fails_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / 'bin'
            shim.mkdir()
            real = shutil.which('git')
            script = shim / 'git'
            script.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "git version 2.44.0"; exit 0; fi\nexec %s "$@"\n' % real)
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            env = dict(os.environ, PATH='%s:%s' % (shim, os.environ['PATH']), **{check.NO_SELFTEST_ENV: '1'})
            out = Path(directory) / 'out'
            out.mkdir()
            result = subprocess.run([sys.executable, '-B', str(CHECKER), '--generate', str(out), '--relative-base', directory],
                                    capture_output=True, env=env, timeout=120)
            text = result.stderr.decode('utf-8', 'replace')
            self.assertNotEqual(result.returncode, 0, text)
            self.assertIn('2.44.0', text)
            self.assertIn('足りない', text)
            self.assertEqual(os.listdir(out), [])

    def test_root_fails(self):
        with mock.patch.object(check.os, 'geteuid', return_value=0):
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        self.assertIn('root', str(raised.exception))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(check.os, 'geteuid', return_value=0):
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                code = check.main(['--generate', directory, '--relative-base', directory])
            self.assertNotEqual(code, 0)
            self.assertIn('root', stream.getvalue())
            self.assertEqual(os.listdir(directory), [])

    def test_wrong_python_fails(self):
        with mock.patch.object(check.sys, 'version_info', (3, 11, 0, 'final', 0)):
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        self.assertIn('3.10', str(raised.exception))

    def test_compare_runs_the_selftest_only_without_the_marker(self):
        # 印(環境変数)が付いた子は自己検査へ届かない。印が無ければ流す(--expect-mismatch は流さない)。
        source = CHECKER.read_text()
        self.assertIn('NO_SELFTEST_ENV', source)
        self.assertEqual(check.NO_SELFTEST_ENV, 'R4D1_NO_SELFTEST')
        with mock.patch.object(check, 'run_selftest', side_effect=AssertionError('流してはいけない')) as selftest:
            manifest = _state['manifest_path']
            rust = Path(_state['root']) / 'perfect.json'
            rust.write_text(json.dumps(perfect_rust(_state['manifest'])))
            with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'}), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check.compare_main(str(manifest), str(rust), False), 0)
            selftest.assert_not_called()
        environment = {k: v for k, v in os.environ.items() if k != check.NO_SELFTEST_ENV}
        with mock.patch.object(check, 'run_selftest') as selftest, \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1'}), \
                contextlib.redirect_stderr(io.StringIO()):
            check.compare_main(str(_state['manifest_path']), str(Path(_state['root']) / 'perfect.json'), False)
            selftest.assert_called_once()


if __name__ == '__main__':
    unittest.main()
