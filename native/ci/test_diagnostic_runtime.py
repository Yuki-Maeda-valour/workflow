#!/usr/bin/env python3
"""R4D-2 比較器(check_diagnostic_runtime.py)の自己検査。

比較器が、結果の 1 件の違い・並びの違い・理由と終了値の違い・失敗の種類の違い・予算の操作の列の 1 か所の違い・
件数の不足・R7 の 2 通りの外の結果を、それぞれ失敗にすることを確かめる。小さい組(scope=mini)の一覧と、
旧版の結果をそのまま写した「完全な Rust の結果」を使い、その結果へ 1 か所ずつ手を入れる。
J3 の旧版の呼出しを C の関数の呼出しの中から行うと、深さの測り方の確かめで失敗することも確かめる。
scripts/test_diagnostic_runtime.py(旧版の検査)とは別のファイルで、同じ Python のプロセスに同じ名前で読み込まない。
待つ条件は出来事(子の終わり)で決め、時間で待たない。
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
import check_diagnostic_runtime as check  # noqa: E402

CHECKER = HERE / 'check_diagnostic_runtime.py'
_state = {}


def child_env():
    return dict(os.environ, **{check.NO_SELFTEST_ENV: '1', check.ALLOW_MINI_ENV: '1', 'PYTHONDONTWRITEBYTECODE': '1'})


def run_child(argv, cwd):
    """子の比較器(か子の Python)を support_native.run_command で起動し、終わるまで待つ。"""
    result = check.run_command([sys.executable, '-B'] + argv, cwd=str(cwd), env=child_env(), timeout=300)
    if result.timed_out:
        raise AssertionError('子が上限時間に当たった: %r' % argv)
    return result


def setUpModule():
    """小さい組の一覧を 1 回だけ作る(自己検査の間は読み取りだけ)。

    J3 は呼出しの深さを測るので、unittest の呼出し(C の関数の呼出しを含む)の外の子のプロセスで作る。
    """
    # Mac の一時フォルダ(/var/folders)は、/private/var へのリンクを含む。
    # Reader は途中のリンクを辿らないので、実体のパスにしてから使う。
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix='r4d2-selftest-')))
    _state['root'] = root
    try:
        destination = root / 'gen'
        destination.mkdir()
        result = run_child([str(CHECKER), '--generate', str(destination), '--scope', 'mini'], root)
        if result.returncode != 0:
            raise AssertionError('小さい組を作れない: %s' % result.stderr.decode('utf-8', 'replace')[-2000:])
        legacy_path = destination / 'legacy.json'
        _state['legacy_path'] = legacy_path
        _state['legacy'] = json.loads(legacy_path.read_text())
    except BaseException:
        # setUpModule が失敗すると tearDownModule は呼ばれないので、ここで消す。
        shutil.rmtree(root, ignore_errors=True)
        raise


def tearDownModule():
    shutil.rmtree(_state['root'], ignore_errors=True)


def perfect_rust(data):
    """旧版の結果をそのまま写した、完全に一致する Rust の結果(R7 は 1 つ目の結果)。"""
    return {'results': copy.deepcopy(data['results'])}


def first(data, group):
    return data['groups'][group][0]


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(_state['legacy'])
        self.rust = perfect_rust(self.data)
        self.tmp = Path(tempfile.mkdtemp(prefix='r4d2-compare-', dir=_state['root']))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def compare(self, data=None, rust=None, expect_mismatch=False):
        """比較器の --compare を同じ Python のプロセスで流す。終了値と標準エラーを返す。"""
        legacy_path = self.tmp / 'legacy.json'
        rust_path = self.tmp / 'rust.json'
        legacy_path.write_text(json.dumps(self.data if data is None else data))
        rust_path.write_text(json.dumps(self.rust if rust is None else rust))
        argv = ['--compare', str(legacy_path), str(rust_path)] + (['--expect-mismatch'] if expect_mismatch else [])
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            code = check.main(argv)
        return code, stream.getvalue()

    def assert_fails(self, text_part, **changes):
        code, text = self.compare(**changes)
        self.assertNotEqual(code, 0, text)
        self.assertIn(text_part, text)

    def test_the_perfect_copy_passes_and_prints_the_summary(self):
        code, text = self.compare()
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-2 の比較: 入力 ', text)
        self.assertIn('不一致 0 件', text)
        for group in check.GROUPS:
            self.assertIn('R4D-2 件数: %s=' % group, text)
        self.assertIn('R4D-2 資源の上限: RLIMIT_NOFILE=', text)

    def test_one_changed_result_fails(self):
        item = first(self.data, 'j4-encode')
        self.rust['results'][item][0] = ['ok', '00']
        self.assert_fails(item)

    def test_changed_order_fails(self):
        item = next(i for i in self.data['groups']['j1-decode-corpus']
                    if self.data['results'][i][0] == 'ok' and len(self.data['results'][i][1]) >= 4)
        tokens = self.rust['results'][item][1]
        tokens[0], tokens[2] = tokens[2], tokens[0]
        self.assert_fails(item)
        self.rust = perfect_rust(self.data)
        item = next(i for i in self.data['groups']['r5-names'] if len(self.data['results'][i]['records']['dirs']) >= 2)
        dirs = self.rust['results'][item]['records']['dirs']
        dirs[0], dirs[1] = dirs[1], dirs[0]
        self.assert_fails(item)

    def test_changed_reason_and_exit_code_fail(self):
        item = next(i for g in ('r1-read-limit', 'r3-links-types') for i in self.data['groups'][g]
                    if self.data['results'][i]['ops'][-1][:2] == ['err', 'failure'])
        self.rust['results'][item]['ops'][-1][2] += 'x'
        self.assert_fails(item)
        self.rust = perfect_rust(self.data)
        self.rust['results'][item]['ops'][-1][3] = 99
        self.assert_fails(item)

    def test_changed_failure_kind_fails(self):
        item = next(i for i in self.data['groups']['j3-decode-depth'] if self.data['results'][i] == ['err', 'recursion'])
        self.rust['results'][item] = ['err', 'schema']
        self.assert_fails(item)
        self.rust = perfect_rust(self.data)
        item = first(self.data, 'r4-owner')
        self.rust['results'][item]['ops'][-1] = ['err', 'nul']
        self.assert_fails(item)

    def test_one_changed_budget_operation_fails(self):
        item = next(i for i in self.data['groups']['r1-read-limit'] if 'c' in self.data['results'][i]['budget'])
        log = self.rust['results'][item]['budget'].split(';')
        log[0] = 'management_copy=1'
        self.rust['results'][item]['budget'] = ';'.join(log)
        self.assert_fails(item)
        self.rust = perfect_rust(self.data)
        item = first(self.data, 'j5-response')
        self.rust['results'][item]['budget'] += ';c'
        self.assert_fails(item)

    def test_missing_and_stray_results_fail(self):
        item = first(self.data, 'j2-decode-random')
        del self.rust['results'][item]
        self.assert_fails(item)
        self.rust = perfect_rust(self.data)
        self.rust['results']['j1-decode-corpus/9999-none'] = ['ok', []]
        self.assert_fails('一覧に無い入力')

    def test_count_shortage_fails(self):
        for group in ('j7-keys', 'r10-cwd'):
            data = copy.deepcopy(self.data)
            data['groups'][group] = []
            with self.subTest(group=group):
                self.assert_fails('件数が下限に足りない: %s=0' % group, data=data)
        # 小さい組は、印が無ければ full として認めない。
        with mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: ''}):
            self.assert_fails('full でない')
        # full の範囲では、決まった下限と項目の名前を確かめる。
        data = copy.deepcopy(self.data)
        data['scope'] = 'full'
        counts = {group: len(ids) for group, ids in data['groups'].items()}
        problems = check.check_counts(data, counts)
        self.assertTrue(any('r1-read-limit' in p and '下限 53' in p for p in problems), problems)
        self.assertTrue(any('j3-decode-depth' in p and '下限 168' in p for p in problems), problems)
        self.assertTrue(any('項目の入力が無い: j1-decode-corpus' in p for p in problems), problems)

    def test_r7_result_outside_the_two_legacy_results_fails(self):
        item = first(self.data, 'r7-missing-order')
        alternatives = self.data['alternatives'][item]
        self.assertEqual(len(alternatives), 2)
        self.assertNotEqual(alternatives[0], alternatives[1])
        for alternative in alternatives:
            self.rust['results'][item] = copy.deepcopy(alternative)
            code, text = self.compare()
            self.assertEqual(code, 0, text)
        changed = copy.deepcopy(alternatives[0])
        changed['budget'] += ';c'
        self.rust['results'][item] = changed
        self.assert_fails('2 通りのどちらとも違う')

    def test_expect_mismatch_needs_every_injected_result_to_differ(self):
        bias = self.data['bias']
        injected = {key: copy.deepcopy(self.data['results'][bias[key]]) for key in ('j3', 'r')}
        rust = {'results': {bias['j3']: injected['j3'], bias['r']: injected['r']}}
        code, text = self.compare(rust=rust, expect_mismatch=True)
        self.assertNotEqual(code, 0, text)
        self.assertIn('比較が働いていない', text)
        rust['results'][bias['j3']] = ['err', 'recursion']
        code, text = self.compare(rust=rust, expect_mismatch=True)
        self.assertNotEqual(code, 0, text)
        rust['results'][bias['r']]['budget'] += ';c'
        code, text = self.compare(rust=rust, expect_mismatch=True)
        self.assertEqual(code, 0, text)
        self.assertIn('R4D-2 比較(差し込み): 不一致 2 件・期待どおり失敗', text)
        code, text = self.compare(rust={'results': {}}, expect_mismatch=True)
        self.assertNotEqual(code, 0, text)
        code, text = self.compare(rust={'results': {'j1-decode-corpus/9999-none': []}}, expect_mismatch=True)
        self.assertNotEqual(code, 0, text)

    def test_compare_in_a_child_process(self):
        # 比較器を子として起動する(自己検査の印を付け、support_native.run_command で起動する)。
        legacy_path = self.tmp / 'legacy.json'
        legacy_path.write_text(json.dumps(self.data))
        good = self.tmp / 'good.json'
        good.write_text(json.dumps(self.rust))
        bad_rust = perfect_rust(self.data)
        item = first(self.data, 'j6-base64')
        bad_rust['results'][item] = ['ok', 'ff']
        bad = self.tmp / 'bad.json'
        bad.write_text(json.dumps(bad_rust))
        for path, expected in ((good, 0), (bad, 1)):
            result = run_child([str(CHECKER), '--compare', str(legacy_path), str(path)], self.tmp)
            text = result.stderr.decode('utf-8', 'replace')
            self.assertEqual(result.returncode, expected, text)
            if expected:
                self.assertIn(item, text)


# 子の Python で J3 を作る(引数 sorted なら、sorted の key の中から旧版を呼ぶ)。
DEPTH_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[1])
import check_diagnostic_runtime as check

def inside_sorted(function):
    box = []
    sorted([0], key=lambda _: box.append(function()))
    return box[0]

builder = check.Builder(sys.argv[3], 'mini')
try:
    check.add_depths(builder, inside_sorted if sys.argv[2] == 'sorted' else check.direct)
except check.CheckError as error:
    print(error)
    raise SystemExit(3)
print('ok', len(builder.groups['j3-decode-depth']))
"""


class DepthTests(unittest.TestCase):
    def run_depths(self, mode):
        return run_child(['-c', DEPTH_SCRIPT, str(HERE), mode, str(_state['root'])], _state['root'])

    def test_measurement_check_fails_inside_a_c_call(self):
        # sorted の key の中から呼ぶと、C の呼出しの分だけ深さの測り方がずれ、失敗にする。
        result = self.run_depths('sorted')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('深さの測り方の確かめが合わない', result.stdout.decode('utf-8'))
        # 普通の呼出しなら通る。
        result = self.run_depths('direct')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.decode('utf-8').startswith('ok '))

    def test_measurement_check_fails_inside_unittest_too(self):
        # この検査の中(unittest の呼出しは C の関数の呼出しを含む)からも、測り方の確かめは失敗にする。
        builder = check.Builder(_state['root'], 'mini')
        with self.assertRaises(check.CheckError) as raised:
            check.add_depths(builder)
        self.assertIn('深さの測り方の確かめが合わない', str(raised.exception))


class ScenarioRuleTests(unittest.TestCase):
    def scenario(self, several_missing):
        root = os.fsencode(_state['root'] / ('missing-%s' % several_missing))
        item = {'root': root.hex(), 'tree': [], 'cond': ['normal'], 'cwd': None, 'faults': None, 'child': None,
                'ops': [['lstat', (root + b'/a').hex(), True], ['lstat', (root + b'/b').hex(), True], ['verify']]}
        if several_missing:
            item['several_missing'] = True
        return item, root

    def test_two_missing_paths_at_verify_fail_outside_r7(self):
        # R7 のほかは、確かめ直しの時点の missing を 1 件までにする(2 件以上は旧版の順が揺れる)。
        item, root = self.scenario(False)
        with self.assertRaises(check.CheckError) as raised:
            check.run_scenario(item)
        self.assertIn('missing が 2 件', str(raised.exception))
        self.assertFalse(os.path.lexists(root))
        # R7 の印がある筋書きは通す。
        item, root = self.scenario(True)
        result = check.run_scenario(item)
        self.assertEqual(result['ops'][-1], ['ok', None])
        self.assertEqual(len(result['records']['missing']), 2)
        self.assertFalse(os.path.lexists(root))


class PrerequisiteTests(unittest.TestCase):
    def test_wrong_python_fails(self):
        with mock.patch.object(check.sys, 'version_info', (3, 11, 0, 'final', 0)):
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        self.assertIn('3.10', str(raised.exception))

    def test_root_fails_and_writes_nothing(self):
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

    def test_integer_digit_limit_other_than_4300_fails(self):
        previous = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(5000)
        try:
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        finally:
            sys.set_int_max_str_digits(previous)
        self.assertIn('4300', str(raised.exception))

    def test_recursion_limit_other_than_1000_fails(self):
        previous = sys.getrecursionlimit()
        sys.setrecursionlimit(1500)
        try:
            with self.assertRaises(check.CheckError) as raised:
                check.check_prerequisites()
        finally:
            sys.setrecursionlimit(previous)
        self.assertIn('1000', str(raised.exception))

    def test_compare_runs_the_selftest_only_without_the_marker(self):
        self.assertEqual(check.NO_SELFTEST_ENV, 'R4D2_NO_SELFTEST')
        rust = Path(_state['root']) / 'perfect.json'
        rust.write_text(json.dumps(perfect_rust(_state['legacy'])))
        with mock.patch.object(check, 'run_selftest', side_effect=AssertionError('流してはいけない')) as selftest, \
                mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1', check.NO_SELFTEST_ENV: '1'}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check.compare_main(str(_state['legacy_path']), str(rust), False), 0)
            selftest.assert_not_called()
        environment = {k: v for k, v in os.environ.items() if k != check.NO_SELFTEST_ENV}
        with mock.patch.object(check, 'run_selftest') as selftest, \
                mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.dict(os.environ, {check.ALLOW_MINI_ENV: '1'}), \
                contextlib.redirect_stderr(io.StringIO()):
            check.compare_main(str(_state['legacy_path']), str(rust), False)
            selftest.assert_called_once()


if __name__ == '__main__':
    unittest.main()
