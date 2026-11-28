#!/usr/bin/env python3
"""R4X-1 の比較器(check_implement_agent.py)の自己検査。旧版も Rust も起動せず、比べ方の誤りで比較器が不合格に
なることを確かめる(置き換えの規則・切り方・0 件・ケースの欠け・bash の行の戻し方〈行番号の 2 つの形〉・止め所の
ずれ・包みの有無による旧版の結果の変化・ジョブの知らせの行・core_pattern がファイル名の場合)。

    python3 -B -m unittest discover -s native/ci -p 'test_implement_agent.py'
"""
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('check_implement_agent', HERE / 'check_implement_agent.py')
check = importlib.util.module_from_spec(SPEC)
sys.modules['check_implement_agent'] = check
SPEC.loader.exec_module(check)

OLD_PATH = str(check.OLD).encode()
JA_BASH = {
    b'%s: line %d: ': '%s: %d 行: '.encode(),
    b'line %d: ': '%d 行: '.encode(),
    b'warning: ': '警告: '.encode(),
    b'%s: integer expression expected': '%s: 整数の式が予期されます'.encode(),
    b'(core dumped) ': '(コアダンプ) '.encode(),
    b'%s: error retrieving current directory: %s: %s\n':
        '%s: カレントディレクトリの取得時にエラーが発生しました : %s: %s\n'.encode(),
    b'getcwd: cannot access parent directories': 'getcwd: 親ディレクトリにアクセスできません'.encode(),
    b'setlocale: LC_ALL: cannot change locale (%s)': 'setlocale: LC_ALL: ロケールを変更できません (%s)'.encode(),
}
JA_LIBC = {
    b'File size limit exceeded': 'ファイルサイズ制限を超過しました'.encode(),
    b'No such file or directory': 'そのようなファイルやディレクトリはありません'.encode(),
    b'Quit': '終了'.encode(),
}


def side(returncode=0, stdout=b'', stderr=b'', records=None, marks=None, pid=100, tree=None):
    tree = tree if tree is not None else {'work': ('directory', 0o755, None)}
    return check.Side(returncode, stdout, stderr, tree, records or {}, marks or [], pid, False, dict(tree))


def case(**kw):
    values = dict(group='g', label='l', args=list(check.VALID))
    values.update(kw)
    return check.Case(**values)


class BackTest(unittest.TestCase):
    def setUp(self):
        check.BACK = check.Back(bash=JA_BASH, libc=JA_LIBC)
        self.addCleanup(setattr, check, 'BACK', None)

    def test_path_is_replaced_and_english_lines_stay(self):
        text = OLD_PATH + b': line 364: [: 9: integer expression expected\nERROR [usage] x\n'
        self.assertEqual(check.normalize_old(text),
                         b'implement-agent.sh: line 364: [: 9: integer expression expected\nERROR [usage] x\n')

    def test_both_line_number_forms_are_read(self):
        for local in ('364 行: ', '行 364: '):
            text = OLD_PATH + ': {}[: 9: 整数の式が予期されます\n'.format(local).encode()
            self.assertEqual(check.normalize_old(text),
                             b'implement-agent.sh: line 364: [: 9: integer expression expected\n', local)

    def test_unmapped_bash_line_fails(self):
        with self.assertRaises(check.Mismatch):
            check.normalize_old(OLD_PATH + ': 364 行: 訳の無い文\n'.encode())
        with self.assertRaises(check.Mismatch):
            check.normalize_old(OLD_PATH + b': nonumber\n')

    def test_warning_cwd_error_and_startup_lines(self):
        text = (OLD_PATH + ': 354 行: 警告: setlocale: LC_ALL: ロケールを変更できません (xx)\n'.encode() +
                'shell-init: カレントディレクトリの取得時にエラーが発生しました : getcwd: 親ディレクトリにアクセスできません:'
                ' そのようなファイルやディレクトリはありません\n'.encode())
        self.assertEqual(check.normalize_old(text),
                         b'implement-agent.sh: line 354: warning: setlocale: LC_ALL: cannot change locale (xx)\n'
                         b'shell-init: error retrieving current directory: getcwd: cannot access parent directories: '
                         b'No such file or directory\n')

    def test_job_notice_padding_is_recomputed_from_english(self):
        # ja_JP.UTF-8 では訳した信号の名前の後に空白 24 個と「(コアダンプ)」が続く。英語では詰めが無い。
        local = OLD_PATH + ': 354 行:    20 ファイルサイズ制限を超過しました'.encode() + b' ' * 24 + \
            '(コアダンプ) LC_ALL=C grep -q x\n'.encode()
        self.assertEqual(check.normalize_old(local),
                         b'implement-agent.sh: line 354:    20 File size limit exceeded(core dumped) LC_ALL=C grep -q x\n')
        short = OLD_PATH + ': 354 行:    20 終了'.encode() + b' ' * 18 + b'LC_ALL=C grep -q x\n'
        self.assertEqual(check.normalize_old(short),
                         b'implement-agent.sh: line 354:    20 Quit' + b' ' * 20 + b'LC_ALL=C grep -q x\n')
        # C の地域設定の旧版の英語の行は、そのまま残る。
        english = OLD_PATH + b': line 354:  5438 File size limit exceeded(core dumped) LC_ALL=C grep -q x\n'
        self.assertEqual(check.normalize_old(english),
                         b'implement-agent.sh: line 354:  5438 File size limit exceeded(core dumped) LC_ALL=C grep -q x\n')
        quit_line = OLD_PATH + b': line 354:    20 Quit' + b' ' * 20 + b'LC_ALL=C grep -q x\n'
        self.assertEqual(check.normalize_old(quit_line),
                         b'implement-agent.sh: line 354:    20 Quit' + b' ' * 20 + b'LC_ALL=C grep -q x\n')
        broken = OLD_PATH + ': 354 行:    20 知らない信号 LC_ALL=C\n'.encode()
        with self.assertRaises(check.Mismatch):
            check.normalize_old(broken)

    def test_grep_notice_expectation_needs_exactly_one_notice(self):
        line = b'implement-agent.sh: line 354:  5438 File size limit exceeded(core dumped) LC_ALL=C grep -q x\n'
        self.assertEqual(check.expect_grep_notice(side(stderr=line), None, False), [])
        local = OLD_PATH + ': 354 行:  5438 ファイルサイズ制限を超過しました'.encode() + b' ' * 24 + \
            '(コアダンプ) LC_ALL=C grep -q x\n'.encode()
        self.assertEqual(check.expect_grep_notice(side(stderr=local), None, True), [])
        # grep が死ななかった(知らせの行が無い)・2 つある・pid の字面が %5d の形でない、はどれも失敗。
        self.assertEqual(len(check.expect_grep_notice(side(stderr=b'ERROR [x] y\n'), None, False)), 2)
        self.assertTrue(check.expect_grep_notice(side(stderr=line * 2), None, False))
        odd = b'implement-agent.sh: line 354: 5438 File size limit exceeded LC_ALL=C grep -q x\n'
        self.assertEqual(len(check.expect_grep_notice(side(stderr=odd), None, False)), 1)

    def test_eucjp_lines_are_decoded_first(self):
        text = OLD_PATH + ': 364 行: [: 9: 整数の式が予期されます\n'.encode('euc_jp')
        self.assertEqual(check.normalize_old(text, 'EUC-JP'),
                         b'implement-agent.sh: line 364: [: 9: integer expression expected\n')

    def test_pid_mask_records_the_pids(self):
        masked, pids = check.mask_pids(b'implement-agent.sh: line 354:    42 Quit')
        self.assertEqual(pids, [42])
        self.assertIn(b'<pid>', masked)
        # `%5ld` の形でない pid は伏せない(差として残る)。
        masked, pids = check.mask_pids(b'implement-agent.sh: line 354: 42 Quit')
        self.assertEqual(pids, [])
        self.assertNotIn(b'<pid>', masked)
        masked, pids = check.mask_pids(b'implement-agent.sh: line 354: 123456 Quit')
        self.assertEqual(pids, [123456])


class CompareTest(unittest.TestCase):
    def setUp(self):
        check.BACK = check.Back(bash={}, libc={})
        self.addCleanup(setattr, check, 'BACK', None)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root, ignore_errors=True))

    def compare(self, c, old, rust, reference=None):
        return check.compare_sides(c, self.root, old, rust, reference)

    def test_equal_sides_have_no_problem(self):
        self.assertEqual(self.compare(case(), side(2, stderr=b'x\n'), side(2, stderr=b'x\n')), [])

    def test_every_field_difference_is_found(self):
        self.assertTrue(self.compare(case(), side(2), side(3)))
        self.assertTrue(self.compare(case(), side(2, stderr=b'a'), side(2, stderr=b'b')))
        self.assertTrue(self.compare(case(), side(2, stdout=b'a'), side(2)))
        changed = side(2, tree={'work': ('directory', 0o700, None)})
        changed.initial = {'work': ('directory', 0o755, None)}
        self.assertTrue(self.compare(case(), side(2), changed))

    def test_stop_point_mismatch_is_found(self):
        c = case(stop=('grep', 'TERM'))
        self.assertTrue(self.compare(c, side(-15, marks=['stop-grep']), side(-15, marks=['stop-dirname'])))
        self.assertTrue(self.compare(c, side(-15, marks=[]), side(-15, marks=[])))
        self.assertEqual(self.compare(c, side(-15, marks=['stop-grep']), side(-15, marks=['stop-grep'])), [])

    def test_boundary_rules(self):
        cand = str(self.root).encode() + b'/home/.local/state/dev-workflow'
        old = side(-signal.SIGKILL, records={'mkdir': [{'argv': [b'mkdir', b'-p', cand], 'pid': b'1', 'ppid': b'100'}]})
        good = side(99, stderr=check.boundary_line(cand))
        self.assertEqual(self.compare(case(), old, good), [])
        self.assertTrue(self.compare(case(), old, side(99, stderr=check.boundary_line(cand + b'x'))))
        self.assertTrue(self.compare(case(), side(2), good), 'Rust だけが境界に届いた')
        trapped = case(stop=('boundary', 'TERM'))
        old_trapped = dataclasses.replace(old, marks=['stop-boundary'])
        self.assertTrue(self.compare(trapped, old_trapped, dataclasses.replace(good, marks=['stop-boundary'])))
        # 書き込みの故障で切れた境界の行は、行の頭の部分を許す(終了値は 99 のまま)。
        cut = case(fsize=10)
        self.assertEqual(self.compare(cut, old, side(99, stderr=check.boundary_line(cand)[:10])), [])
        self.assertTrue(self.compare(cut, old, side(99, stderr=b'XRROR [r4x')))

    def test_split_boundary_keeps_earlier_lines(self):
        want = b'ERROR [r4x-1-boundary] abc\n'
        self.assertEqual(check.split_boundary(b'x\nERROR [r4', want), (b'x\n', b'ERROR [r4'))
        self.assertEqual(check.split_boundary(b'xERROR [r4', want), (b'xERROR [r4', b''))

    def test_cut_output_rules(self):
        # (2) 置き換えた書き込みが切れない: 故障の無い旧版に合わせ、終了値の差を認める。
        full = OLD_PATH + b': line 364: [: 9: integer expression expected\nERROR [usage] y\n'
        fixed = b'implement-agent.sh: line 364: [: 9: integer expression expected\nERROR [usage] y\n'
        c = case(fsize=len(fixed) + 5)
        faulted = side(-25, stderr=full[:len(fixed) + 5])
        self.assertEqual(self.compare(c, faulted, side(2, stderr=fixed), reference=side(2, stderr=full)), [])
        self.assertTrue(self.compare(c, faulted, side(-25, stderr=fixed), reference=side(2, stderr=full)))
        # (3) 両方が切れる: 置き換えた出力を同じバイト数で切った物。
        c = case(fsize=30)
        faulted = side(-25, stderr=full[:30])
        self.assertEqual(self.compare(c, faulted, side(-25, stderr=fixed[:30]), reference=side(2, stderr=full)), [])
        self.assertTrue(self.compare(c, faulted, side(-25, stderr=fixed[:29]), reference=side(2, stderr=full)))

    def test_tool_launch_rules(self):
        grep = {'argv': [b'grep', b'-q', b'[^[:space:]]', b'-v'], 'env': [b'A=1', b'_=/x/grep'], 'ppid': b'100',
                'pid': b'7', 'fd0': b'file', 'fd1': b'file', 'fd2': b'char', 'ignored': b'0', 'blocked': b'0',
                'cwd': b'/w'}
        launched = case(args=['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'])
        old = side(2, records={'grep': [grep]})
        rust = side(2, records={'grep': [dict(grep, ppid=b'100')]})
        self.assertEqual(self.compare(launched, old, rust), [])
        rust_env = side(2, records={'grep': [dict(grep, env=[b'A=2', b'_=/x/grep'])]})
        self.assertTrue(self.compare(launched, old, rust_env))
        self.assertTrue(self.compare(launched, old, side(2)), 'Rust が起動しなかった')
        plain = case()
        old_plain = side(2, records={'grep': [dict(grep, argv=[b'grep', b'-q', b'[^[:space:]]',
                                                               str(self.root).encode() + b'/p.md'])]})
        self.assertEqual(self.compare(plain, old_plain, side(2)), [])
        self.assertTrue(self.compare(plain, old_plain, side(2, records={'grep': [grep]})), 'Rust が起動した')
        swap = dict(grep, env=[b'A=1', b'LOG_SWAP_PY=/py', b'_=/x/grep'])
        inherit = case(args=launched.args, env={'LOG_SWAP_PY': '/py'})
        self.assertEqual(self.compare(inherit, side(2, records={'grep': [swap]}), side(2, records={'grep': [swap]})), [])
        self.assertTrue(self.compare(inherit, side(2, records={'grep': [swap]}),
                                     side(2, records={'grep': [dict(swap, env=[b'A=1', b'LOG_SWAP_PY=/other',
                                                                                 b'_=/x/grep'])]})))

    def test_expectations_are_applied_to_both_sides(self):
        c = case(args=['--runner'], expect=check.expect_condition_5)
        usage = 'ERROR [usage] --runner に値が必要です\n'.encode() + check.USAGE_TXT.read_bytes()
        self.assertEqual(self.compare(c, side(2, stderr=usage), side(2, stderr=usage)), [])
        self.assertTrue(self.compare(c, side(2, stderr=usage[:-1]), side(2, stderr=usage[:-1])))


class StageTest(unittest.TestCase):
    def test_final_line(self):
        self.assertEqual(check.final_line(0, 0, 0), (1, '不合格: 比べた件数が 0 件'))
        self.assertEqual(check.final_line(5, 1, 0)[0], 1)
        self.assertEqual(check.final_line(5, 0, 2, 'mac'), (0, '合格: 5 件(未実施 2 件)'))
        self.assertEqual(check.final_line(5, 0, 0), (0, '合格: 5 件(未実施 0 件)'))

    def test_full_scope_fails_on_unperformed(self):
        # 範囲 full(Linux の基準)では、未実施が 1 件でもあれば不合格にする。
        code, line = check.final_line(5, 0, 2)
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith('不合格: 範囲 full で未実施が 2 件ある'), line)
        self.assertEqual(check.final_line(5, 0, 2, 'full')[0], 1)

    def test_full_requirements_need_strace_and_ja_locale(self):
        locales = {b'c', b'c.utf8', b'posix', b'ja_jp.utf8', b'ja_jp.eucjp'}
        with mock.patch.object(check, 'strace_usable', return_value=True), \
                mock.patch.object(check, 'installed_locales', return_value=locales):
            self.assertEqual(check.full_requirements(), [])
        with mock.patch.object(check, 'strace_usable', return_value=False), \
                mock.patch.object(check, 'installed_locales', return_value=locales):
            missing = check.full_requirements()
        self.assertEqual(len(missing), 1)
        self.assertIn('strace', missing[0])
        with mock.patch.object(check, 'strace_usable', return_value=True), \
                mock.patch.object(check, 'installed_locales', return_value=locales - {b'ja_jp.utf8'}):
            missing = check.full_requirements()
        self.assertEqual(len(missing), 1)
        self.assertIn('ja_JP.utf8', missing[0])
        with mock.patch.object(check, 'strace_usable', return_value=False), \
                mock.patch.object(check, 'installed_locales', return_value=set()):
            missing = check.full_requirements()
        self.assertEqual(len(missing), 2)
        # 足りない要件は不一致として数え、最後の行を不合格にする(main と同じ数え方)。
        self.assertEqual(check.final_line(5, len(missing), 0), (1, '不合格: 2 件の不一致(5 件中)'))

    def test_missing_case_is_found(self):
        with mock.patch.object(check, 'GROUPS', check.GROUPS[:-1]):
            self.assertTrue(check.check_case_lists())
        self.assertEqual(check.check_case_lists(), [])

    def test_wrapper_presence_change_is_found(self):
        self.assertEqual(check.wrapper_differences(side(2, stderr=b'x'), side(2, stderr=b'x')), [])
        self.assertTrue(check.wrapper_differences(side(2, stderr=b'x'), side(2, stderr=b'y')))
        self.assertTrue(check.wrapper_differences(side(2), side(20)))

    def test_core_pattern_file_name_sets_core_zero(self):
        cases = [case(), case(label='m')]
        self.assertTrue(all(c.core0 for c in check.apply_core_limit(cases, 'core.%p')))
        self.assertFalse(any(c.core0 for c in check.apply_core_limit(cases, '|/usr/share/apport/apport %p')))
        self.assertFalse(check.core_file_pattern('(読めない)'))

    def test_execve_env_is_read_from_strace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace'
            path.write_text('12 execve("/usr/bin/grep", ["grep", "-q"], ["B=\\303\\251", "A=x\\ny", "_=/usr/bin/grep"])'
                            ' = 0\n', encoding='utf-8')
            self.assertEqual(check.execve_env(path, 'grep'), [b'A=x\ny', 'B=é'.encode()])
            self.assertIsNone(check.execve_env(Path(directory) / 'none', 'grep'))

    def test_excluded_env_inputs_fail(self):
        check.check_input_env([b'PATH=/x', b'LANG=C'], ())
        with self.assertRaises(AssertionError):
            check.check_input_env([b'BASH_ENV=/x'], ())
        with self.assertRaises(AssertionError):
            check.check_input_env([b'BASH_FUNC_x%%=() { :; }'], ())
        check.check_input_env([b'CDPATH=/x'], ('CDPATH',))

    def test_case_names_are_unique_and_lists_match(self):
        names = [c.name for c in check.all_cases('full')]
        self.assertEqual(len(names), len(set(names)))
        data = json.loads(check.MAC_UNPERFORMED.read_text(encoding='utf-8'))['cases']
        mac = {c.name for c in check.all_cases('mac')}
        self.assertFalse(mac & set(data), 'Mac の一覧のケースを Mac の範囲で流している')
        self.assertEqual(len(mac) + len(data), len(names))

    def test_case_lists_do_not_depend_on_the_machine(self):
        # Mac で作った一覧も、Linux で作った固定の一覧と同じ(機械で分かれるケースは、範囲 mac の外し方で扱う)。
        fixed = json.loads(check.CASE_LISTS.read_text(encoding='utf-8'))
        self.assertEqual(check.case_lists(), fixed)
        linux = {scope: [c.name for c in check.all_cases(scope)] for scope in ('full', 'mac')}
        with mock.patch.object(check, 'LINUX', False), mock.patch.object(check, 'SYSTEM', 'darwin'):
            self.assertEqual(check.case_lists(), fixed)
            self.assertEqual({scope: [c.name for c in check.all_cases(scope)] for scope in ('full', 'mac')}, linux)

    def test_mac_fixed_covers_the_mac_scope(self):
        # 範囲 mac のケースは、Mac の固定期待に入れるか、理由を付けて入れないかのどちらか一方。群ごとに 1 件以上入れる。
        data = json.loads(check.MAC_FIXED.read_text(encoding='utf-8'))
        mac = {c.name: c.group for c in check.all_cases('mac')}
        self.assertFalse(set(data['cases']) & set(data['unperformed']))
        self.assertEqual(set(data['cases']) | set(data['unperformed']), set(mac))
        for group in set(mac.values()):
            self.assertTrue(any(mac[name] == group for name in data['cases']), group)
        for reason in data['unperformed'].values():
            self.assertGreater(len(reason), 10)

    def test_mac_fixed_entry_round_trip_and_reasons(self):
        root = Path('/x/linux-root')
        cand = str(root).encode() + b'/work/home/.local/state/dev-workflow'
        records = {'grep': [{'argv': [b'grep', b'-q', b'[^[:space:]]', str(root).encode() + b'/p.md'], 'ppid': b'100'}],
                   'mkdir': [{'argv': [b'mkdir', b'-p', cand], 'ppid': b'7'}]}
        old = side(-signal.SIGKILL, stderr=OLD_PATH + b': x ' + str(root).encode() + b'/p.md\n', records=records,
                   marks=['stop-boundary'])
        c = case(args=list(check.VALID))
        entry = check.mac_fixed_entry(c, root, old)
        self.assertIsInstance(entry, dict)
        self.assertEqual(entry['returncode'], 'SIGKILL')
        self.assertNotIn(str(root), json.dumps(entry))
        mac_root = Path('/private/var/folders/mac-root-longer')
        rust = side(99, tree={'work': ('directory', 0o755, None), 'home': ('directory', 0o700, None)})
        back = check.side_from_fixed(entry, mac_root, rust)
        self.assertEqual(back.returncode, -signal.SIGKILL)
        self.assertEqual(back.stderr, OLD_PATH + b': x ' + str(mac_root).encode() + b'/p.md\n')
        self.assertEqual(back.records['mkdir'][0]['argv'][2], str(mac_root).encode() + b'/work/home/.local/state/dev-workflow')
        self.assertEqual(int(back.records['grep'][0]['ppid']), back.pid)
        self.assertEqual(back.tree, rust.initial)
        self.assertEqual(back.marks, ['stop-boundary'])
        # 入れないケース: Rust も grep を起動する形・`//` で始まる dirname の値・errno の文・ファイルを変えた。
        dash = case(args=['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'])
        self.assertIsInstance(check.mac_fixed_entry(dash, root, old), str)
        slash = side(20, records={'dirname': [{'argv': [b'dirname', b'//a'], 'ppid': b'1'}]})
        self.assertIn('dirname', check.mac_fixed_entry(c, root, slash))
        self.assertIn('errno', check.mac_fixed_entry(c, root, side(1, stderr=b'x: No such file or directory\n')))
        changed = side(0)
        changed.initial = {}
        self.assertIsInstance(check.mac_fixed_entry(c, root, changed), str)

    def test_stale_mac_fixed_is_found(self):
        # 範囲 full の旧版の結果から作り直した Mac の固定期待が、固定したファイルと違えば不合格。群に 1 件も無くても不合格。
        entry = {'returncode': 2, 'stdout': '', 'stderr': 'ERROR [usage] x\n', 'records': {}, 'marks': []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mac_fixed.json'
            path.write_text(json.dumps({'cases': {'g/a': entry}, 'unperformed': {'g/b': '理由の文(十分に長い)'}}))
            with mock.patch.object(check, 'MAC_FIXED', path), \
                    mock.patch.object(check, 'MAC_OLD', {'g/a': entry, 'g/b': '理由の文(十分に長い)'}):
                self.assertEqual(check.check_mac_fixed({'g/a', 'g/b'}, {'g/a': 'g', 'g/b': 'g'}), [])
                self.assertTrue(check.check_mac_fixed({'g/a', 'g/b', 'h/c'}, {'g/a': 'g', 'g/b': 'g', 'h/c': 'h'}))
            changed = dict(entry, stderr='ERROR [usage] y\n')
            with mock.patch.object(check, 'MAC_FIXED', path), \
                    mock.patch.object(check, 'MAC_OLD', {'g/a': changed, 'g/b': '理由の文(十分に長い)'}):
                self.assertTrue(check.check_mac_fixed({'g/a', 'g/b'}, {'g/a': 'g', 'g/b': 'g'}))

    @unittest.skipUnless(os.environ.get('R4X1_TEST_FIXTURE') and sys.platform.startswith('linux'),
                         'R4X1_TEST_FIXTURE(implement_agent_contract の絶対パス)を渡した Linux だけで流す'
                         '(CI の Linux の implement-agent は build.py が渡す)')
    def test_mac_simulation_compares_rust_with_the_mac_fixed(self):
        # bash 4.0 以上が無い Mac の流れの模擬: 終了値 3(合格に数えない)・不一致 0 件・比べた件数が 0 件でない。
        env = dict(os.environ, R4X1_SIMULATE_MAC='1')
        done = subprocess.run([sys.executable, '-B', str(HERE / 'check_implement_agent.py'),
                               '--fixture', os.environ['R4X1_TEST_FIXTURE']],
                              stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=900)
        text = done.stdout.decode('utf-8', 'replace')
        self.assertEqual(done.returncode, 3, text[-3000:])
        lines = text.splitlines()
        self.assertRegex(lines[-2], r'^合格: [1-9][0-9]* 件\(未実施 [0-9]+ 件\)$')
        self.assertTrue(lines[-1].startswith('R4X-1: 模擬の合格'), lines[-1])
        self.assertNotIn('R4X-1: 不一致:', text)
        compared = re.search(r'Mac の固定期待と比べた件数: ([0-9]+) 件', text)
        self.assertTrue(compared and int(compared.group(1)) > 0, text[-3000:])

    def test_static_mac_reasons(self):
        self.assertTrue(check.static_mac_reason(case(fsize=3)))
        self.assertTrue(check.static_mac_reason(case(stderr='devfull')))
        self.assertTrue(check.static_mac_reason(case(stop=('grep', 'TERM'))))
        self.assertIsNone(check.static_mac_reason(case(stop=('boundary', 'TERM'))))
        self.assertIsNone(check.static_mac_reason(case()))

    def test_last_value_follows_old_parser(self):
        args = ['--runner', 'a', '--prompt-file', 'x', '--prompt-file', '-v', '--dry-run', '--cwd', '.']
        self.assertEqual(check.last_value(args, b'--prompt-file'), b'-v')
        self.assertIsNone(check.last_value(['--runner'], b'--runner'))
        self.assertIsNone(check.last_value(['bogus', '--runner', 'a'], b'--runner'))


if __name__ == '__main__':
    unittest.main()
