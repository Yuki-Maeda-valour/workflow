#!/usr/bin/env python3
"""R4S比較器の誤合格と、固定入力で検出する実装故障を検査する。"""
import copy
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_r4s as check
from support_native import RunResult, run_command


def run(value, **changes):
    fields = dict(returncode=0, stdout=check.encode(value), stderr=b'', timed_out=False)
    fields.update(changes)
    return RunResult(**fields)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'id': 'fixed', 'purpose': 'loop-profile', 'expected': {'accepted': True, 'patterns': [[65], [0xD800, 0xDC00]]}}
        self.good = [{'id': 'fixed', 'result': self.row['expected']}]

    def reject(self, value):
        with self.assertRaises((ValueError, TypeError, UnicodeError)):
            check.verify_pair(run(value), run(value), [self.row], {})

    def test_correct_and_surrogate_pair(self):
        check.verify_pair(run(self.good), run(self.good), [self.row], {})

    def test_process_failures_even_when_both_equal(self):
        for changes in ({'returncode': 1}, {'returncode': -9}, {'stderr': b'error'}, {'timed_out': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result = run(self.good, **changes)
                check.verify_pair(result, result, [self.row], {})

    def test_json_shape_and_execution_ids(self):
        for value in ([], {}, None, self.good * 2, [{'id': '', 'result': self.row['expected']}],
                      [{'id': 'other', 'result': self.row['expected']}], [{'id': 'fixed'}],
                      [{'id': 'fixed', 'result': self.row['expected'], 'extra': True}]):
            with self.subTest(value=value):
                self.reject(value)
        rows = [self.row, {**self.row, 'id': 'second'}]
        with self.assertRaises(ValueError):
            check.observation(run(self.good * 2), rows)

    def test_duplicate_key_and_invalid_json(self):
        samples = [b'', b'not-json', b'[]\n[]', b'[{"id":"fixed","id":"fixed","result":{}}]',
                   b'[{"id":"fixed","result":{"accepted":false,"accepted":false}}]',
                   b'[{"id":"fixed","result":{"accepted":NaN}}]', b'\xff']
        for raw in samples:
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                check.observation(run([], stdout=raw), [self.row])

    def test_strict_success_types(self):
        results = [{'accepted': 1, 'patterns': []}, {'accepted': True, 'patterns': 'A'},
                   {'accepted': True, 'patterns': [[True]]}, {'accepted': True, 'patterns': [[65.0]]},
                   {'accepted': True, 'patterns': [[-1]]}, {'accepted': True, 'patterns': [[0x110000]]},
                   {'accepted': True, 'patterns': [['65']]}, {'accepted': True},
                   {'accepted': True, 'patterns': [], 'reason': ''}]
        for result in results:
            with self.subTest(result=result):
                self.reject([{'id': 'fixed', 'result': result}])

    def test_equal_wrong_values_never_pass(self):
        results = [{'accepted': True, 'patterns': [[65], [0x10000]]},
                   {'accepted': True, 'patterns': [[0xD800, 0xDC00], [65]]},
                   {'accepted': True, 'patterns': []},
                   {'accepted': False, 'reason': 'secret_paths を安全に読めない'}]
        for result in results:
            with self.subTest(result=result):
                self.reject([{'id': 'fixed', 'result': result}])

    def test_wrong_and_unknown_reason(self):
        row = {'id': 'reject', 'purpose': 'canonical', 'expected': {'accepted': False, 'reason': 'secret_paths が文字列配列でない'}}
        for reason in ('secret_paths に不正な glob がある', '', 'unexpected exception'):
            value = [{'id': row['id'], 'result': {'accepted': False, 'reason': reason}}]
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                check.verify_pair(run(value), run(value), [row], {})

    def test_matcher_bool_not_integer(self):
        for value in (0, 1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check.validate_result({'matched': value}, 'matcher')

    def test_graph_shape_identity_and_order(self):
        good = {'accepted': True, 'nodes': [{'kind': 'sequence', 'tag': 'tag:yaml.org,2002:seq', 'value': [0]}]}
        check.validate_result(good, 'node-graph')
        for replacement in ([True], [-1], [1], '0'):
            bad = copy.deepcopy(good)
            bad['nodes'][0]['value'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                check.validate_result(bad, 'node-graph')
        row = {'id': 'graph', 'purpose': 'node-graph', 'expected': good}
        bad = [{'id': 'graph', 'result': {'accepted': True, 'nodes': []}}]
        with self.assertRaises(ValueError):
            check.verify_pair(run(bad), run(bad), [row], {})
        for value in ({'accepted': False, 'reason': ''}, {'accepted': 0}, {'accepted': True, 'nodes': {}}):
            with self.assertRaises(ValueError):
                check.validate_result(value, 'node-graph')

    def test_approved_difference_is_one_exact_pair(self):
        data, differences = check.load_data()
        row = next(row for row in data['cases'] if row['id'] == check.ALLOWED_ID)
        old = [{'id': row['id'], 'result': row['expected']}]
        new = [{'id': row['id'], 'result': differences[row['id']]['rust_expected']}]
        check.verify_pair(run(old), run(new), [row], differences)
        for wrong in (run(old), run([{'id': row['id'], 'result': {'accepted': True, 'patterns': []}}])):
            with self.assertRaises(ValueError):
                check.verify_pair(run(old), wrong, [row], differences)
        scalar = next(row for row in data['cases'] if row['id'] == 'constructor/self_str_value')
        bad = [{'id': scalar['id'], 'result': new[0]['result']}]
        with self.assertRaises(ValueError):
            check.verify_pair(run([{'id': scalar['id'], 'result': scalar['expected']}]), run(bad), [scalar], differences)

    def test_wrong_result_exits_nonzero(self):
        script = ("import sys;sys.path.insert(0,sys.argv[1]);import check_r4s as c;"
                  "from support_native import RunResult;v=c.decode(sys.stdin.buffer.read());"
                  "r=RunResult(0,c.encode(v['bad']),b'',False);c.verify_pair(r,r,v['rows'],{})")
        payload = {'bad': [{'id': 'fixed', 'result': {'accepted': True, 'patterns': []}}], 'rows': [self.row]}
        result = run_command([sys.executable, '-B', '-c', script, str(Path(check.__file__).parent)],
                             cwd=check.ROOT, input=check.encode(payload), timeout=10)
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'ValueError', result.stderr)


class InputAndCoverageTests(unittest.TestCase):
    def test_all_source_rows_and_fixed_hash(self):
        data, _ = check.load_data()
        coverage = check.decode((check.FIXTURES / 'coverage.json').read_bytes())
        rows = data['cases'] + data['graph_cases']
        for change in ('missing', 'duplicate', 'missing-id', 'missing-reason', 'changed-source'):
            value = copy.deepcopy(coverage)
            if change == 'missing':
                value['row_links'].pop()
            elif change == 'duplicate':
                value['row_links'][-1] = value['row_links'][0]
            elif change == 'missing-id':
                value['row_links'][0]['case_ids'] = ['absent']
            elif change == 'missing-reason':
                value['row_links'][0]['case_ids'] = []
                value['row_links'][0]['out_of_scope_reason'] = None
            else:
                value['row_links'][0]['source'] += ' changed'
            with self.subTest(change=change), self.assertRaises(ValueError):
                check.verify_coverage(value, rows)
        with mock.patch.object(Path, 'read_bytes', return_value=b'{}'), self.assertRaises(ValueError):
            check.load_data()

    def test_required_runtime_and_sources(self):
        check.require_environment()
        import yaml
        for target, name, value in ((sys, 'version_info', (3, 11)), (yaml, '__version__', '6.0.2'),
                                    (check.unicodedata, 'unidata_version', '14.0.0')):
            with mock.patch.object(target, name, value), self.assertRaises(RuntimeError):
                check.require_environment()
        with mock.patch.object(os, 'geteuid', return_value=0), self.assertRaises(RuntimeError):
            check.require_environment()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(check, 'ROOT', Path(temporary)):
            with self.assertRaises(FileNotFoundError):
                check.load_data()

    def test_bridge_absent_empty_and_not_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bridge'
            for candidate in (None, '', path):
                with self.subTest(candidate=candidate), self.assertRaises((ValueError, FileNotFoundError)):
                    check.require_bridge(candidate)
            path.write_bytes(b'no execution')
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                check.require_bridge(path)
            with self.assertRaises(ValueError):
                check.require_bridge(temporary)

    def test_actual_timeout_is_rejected(self):
        result = run_command([sys.executable, '-c', 'import time;time.sleep(30)'], cwd=check.ROOT, timeout=.05)
        self.assertTrue(result.timed_out)
        with self.assertRaises(ValueError):
            check.observation(result, [{'id': 'x', 'purpose': 'matcher'}])

    def test_unexpected_legacy_exception_is_not_a_rejection(self):
        loop, _ = check.legacy_modules()
        def fail():
            raise RuntimeError('unexpected')
        with self.assertRaises(RuntimeError):
            check.old_result(loop, fail)
        def wrong_stop():
            raise loop.Stop('unregistered failure')
        with self.assertRaises(RuntimeError):
            check.old_result(loop, wrong_stop)
        import yaml
        with mock.patch.object(yaml, 'compose', side_effect=RuntimeError('unknown')), self.assertRaises(RuntimeError):
            check.compose_graph(b'')
        with mock.patch.object(yaml, 'compose', side_effect=ValueError('unknown')), self.assertRaises(check.UnexpectedLegacyFailure):
            check.compose_graph(b'')

    def test_wrapped_internal_failures_exit_nonzero(self):
        # 旧実関数が同じ既知Stopへ包む故障も、固定拒否の合格にしない。
        script = textwrap.dedent('''
            import sys
            sys.path.insert(0, sys.argv[1])
            import check_r4s as c
            from pathlib import Path
            from unittest import mock
            from support_native import RunResult
            import yaml
            data, differences = c.load_data()
            row = next(r for r in data['cases'] if r['id'] == sys.argv[3])
            kind = sys.argv[5]
            fault = {'RuntimeError': RuntimeError('injected internal failure'),
                     'ValueError': ValueError('injected internal failure'),
                     'same-value': ValueError('duplicate secret_paths'),
                     'OSError': OSError(5, 'injected I/O failure')}[kind]
            with mock.patch.object(yaml, sys.argv[4], side_effect=fault):
                observed = c.legacy_driver(Path(sys.argv[2]), [c.request(row)])
            old = RunResult(0, c.encode(observed), b'', False)
            new = RunResult(0, c.encode([{'id': row['id'], 'result': row['expected']}]), b'', False)
            c.verify_pair(old, new, [row], differences)
        ''')
        data, _ = check.load_data()
        by_id = {row['id']: row for row in data['cases']}
        cases = [('yaml/unknown_tag_unused', 'safe_load', kind) for kind in ('RuntimeError', 'ValueError', 'OSError')]
        cases += [('yaml/malformed', 'compose', 'RuntimeError'), ('yaml/duplicate_top', 'compose', 'same-value')]
        with tempfile.TemporaryDirectory(prefix='r4s-hidden-fault-') as temporary:
            for index, (identifier, method, kind) in enumerate(cases):
                root = Path(temporary) / str(index)
                check.prepare_inputs(root, [by_id[identifier]])
                result = run_command([sys.executable, '-B', '-c', script, str(Path(check.__file__).parent),
                                      str(root), identifier, method, kind], cwd=root, timeout=10)
                with self.subTest(identifier=identifier, method=method, kind=kind):
                    self.assertFalse(result.timed_out)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'UnexpectedLegacyFailure', result.stderr)

    def test_input_bytes_mode_and_identity_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'input'
            path.write_bytes(b'old')
            before = check.tree_record(root)
            path.write_bytes(b'NEW')
            self.assertNotEqual(before, check.tree_record(root))
            before = check.tree_record(root)
            path.chmod(0o400)
            self.assertNotEqual(before, check.tree_record(root))
            before = check.file_record(path)
            held = root / 'held'
            path.rename(held)
            path.write_bytes(b'NEW')
            self.assertNotEqual(before, check.file_record(path))

    def test_comparison_rejects_input_mutation(self):
        row = {'id': 'yaml', 'purpose': 'loop-profile', 'input_hex': '7b7d', 'expected': {'accepted': True, 'patterns': []}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'input'
            def changed(*args, **kwargs):
                (root / '0/.claude/project-profile.yml').write_bytes(b'changed')
                return run([{'id': 'yaml', 'result': row['expected']}])
            with mock.patch.object(check, 'run_command', side_effect=changed), self.assertRaises(ValueError):
                check.compare_rows(Path('/unused'), [row], root, {})


class ImplementationMutationTests(unittest.TestCase):
    def test_all_ten_implementation_faults_are_detected(self):
        """観測値の加工だけでなく、旧実装の専用コピーを壊して実行する。"""
        import yaml
        data, _ = check.load_data()
        rejections = check.rejection_records()
        rows = {row['id']: row for row in data['cases']}
        fixed = check.decode((check.FIXTURES / 'mutations.json').read_bytes())
        self.assertEqual(len(fixed), 10)
        self.assertEqual({fault['mutant'] for fault in fixed}, {
            'canonical-elementwise-order', 'matcher-prevalidation', 'canonical-sorted',
            'matcher-ascii', 'duplicate-check-removed', 'merge-sequence-reversed',
            'yaml-pair-composed-by-json', 'star-treated-as-dotstar', 'glob-dotall', 'glob-strict-end',
        })
        loop, _ = check.legacy_modules()
        def altered(function, before, after):
            source = inspect.getsource(function)
            self.assertEqual(source.count(before), 1, '故障箇所を一意に変更できない')
            namespace = dict(loop.__dict__)
            exec(source.replace(before, after), namespace)
            return namespace[function.__name__]
        def elementwise(value):
            if isinstance(value, list):
                for item in value:
                    loop.canonical_secret_patterns([item])
            return loop.canonical_secret_patterns(value)
        def prevalidated(rel, patterns):
            try:
                loop.canonical_secret_patterns(patterns)
            except loop.Stop:
                return False
            return loop.secret_path(rel, patterns)
        wrong_duplicates = altered(loop.profile_patterns, '        duplicates(node)', '        pass  # intentional mutation')
        source = textwrap.dedent(inspect.getsource(yaml.constructor.SafeConstructor.flatten_mapping))
        self.assertEqual(source.count('submerge.reverse()'), 1)
        namespace = dict(vars(yaml.constructor))
        exec(source.replace('submerge.reverse()', 'pass  # intentional mutation'), namespace)
        class WrongMergeLoader(yaml.SafeLoader):
            flatten_mapping = namespace['flatten_mapping']
        matchers = {
            'matcher-prevalidation': prevalidated,
            'matcher-ascii': altered(loop.secret_path, 're.IGNORECASE', 're.IGNORECASE | re.ASCII'),
            'star-treated-as-dotstar': altered(loop.secret_path, 'out += "[^/]*"', 'out += ".*"'),
            'glob-dotall': altered(loop.secret_path, 're.IGNORECASE', 're.IGNORECASE | re.DOTALL'),
            'glob-strict-end': altered(loop.secret_path, 'suffix = r"(?:/.*)?$" if raw.endswith("/") else "$"',
                                       r'suffix = r"(?:/.*)?\Z" if raw.endswith("/") else r"\Z"'),
        }
        with tempfile.TemporaryDirectory(prefix='r4s-mutants-') as temporary:
            root = Path(temporary)
            profile = root / '.claude/project-profile.yml'
            profile.parent.mkdir()
            def profile_run(row, function):
                profile.write_bytes(bytes.fromhex(row['input_hex']))
                before = check.file_record(profile)
                value = function(str(root), loop.Budget(10000, 1073741824, 67108864, 30))
                self.assertEqual(check.file_record(profile), before)
                return value
            for fault in fixed:
                detected = []
                for identifier in fault['checked']:
                    row = rows[identifier]
                    name = fault['mutant']
                    def execute():
                        if name in matchers:
                            return matchers[name](check.text(row['input']['rel']), [check.text(v) for v in row['input']['patterns']])
                        if name == 'canonical-elementwise-order':
                            return elementwise(check.typed(row['input']))
                        if name == 'canonical-sorted':
                            return sorted(loop.canonical_secret_patterns(check.typed(row['input'])))
                        if name == 'duplicate-check-removed':
                            return profile_run(row, wrong_duplicates)
                        if name == 'merge-sequence-reversed':
                            with mock.patch.object(yaml, 'SafeLoader', WrongMergeLoader):
                                return profile_run(row, loop.profile_patterns)
                        if name == 'yaml-pair-composed-by-json':
                            return json.loads(json.dumps(profile_run(row, loop.profile_patterns)))
                        raise ValueError('未定義の故障')
                    try:
                        actual = check.old_result(loop, execute, rejections.get(identifier))
                    except check.UnexpectedLegacyFailure:
                        # 故障が拒否の種類や原因を変えても、正しい拒否には数えない。
                        detected.append(identifier)
                        continue
                    if actual != row['expected']:
                        detected.append(identifier)
                        with self.assertRaises(ValueError):
                            check.verify_pair(run([{'id': identifier, 'result': row['expected']}]),
                                              run([{'id': identifier, 'result': actual}]), [row], {})
                self.assertEqual(detected, fault['detected'], name)
                self.assertTrue(detected, name)


if __name__ == '__main__':
    unittest.main()
