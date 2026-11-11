#!/usr/bin/env python3
"""S2E 比較器(check_s2e.py)自身の検査。不具合を入れた結果・未起動・時間切れを不合格にすることを確かめる。"""
import dataclasses
import importlib.util
import io
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

subject = None


def get_subject():
    global subject
    if subject is None:
        spec = importlib.util.spec_from_file_location('s2e_subject', Path(__file__).with_name('check_s2e.py'))
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


TRACEBACK_OLD = (b'Traceback (most recent call last):\n  File "/old/environment-guard.py", line 9, in <module>\n    main()\n'
                 b'  File "/usr/lib/python3.10/argparse.py", line 5, in parse\n    raise\nKeyError: \'a\'\n\n'
                 b'During handling of the above exception, another exception occurred:\n\n'
                 b'Traceback (most recent call last):\n  File "/old/environment-guard.py", line 20, in main\n    x()\nIndexError: b\n')
TRACEBACK_NEW = (b"Traceback (most recent call last):\nKeyError: 'a'\n\nDuring handling of the above exception, another exception occurred:\n\n"
                 b'Traceback (most recent call last):\nIndexError: b\n')
FATAL_OLD = (b'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n\n'
             b'Current thread 0x00007f3a2c42a000 (most recent call first):\n  <no Python frame>\n')


class Comparator(unittest.TestCase):
    def setUp(self):
        self.m = get_subject()

    def result(self, code=0, out=b'', err=b'', timeout=False):
        return self.m.RunResult(code, out, err, timeout)

    # ---- 通常の比較(加工しない)

    def test_every_field_mutation_is_rejected(self):
        good = self.result(out=b'fixed\n', err=b'diag\n')
        self.m.same_result(good, good)
        for change in [dict(returncode=1), dict(stdout=b'fixed\x00\n'), dict(stdout=b'fixee\n'), dict(stderr=b'diag!\n'), dict(stderr=b'dia\n'),
                       dict(stdout=b'fixed\n'[::-1]), dict(timed_out=True)]:
            with self.assertRaises(AssertionError, msg=str(change)):
                self.m.same_result(good, dataclasses.replace(good, **change))
        with self.assertRaises(AssertionError):
            self.m.same_result(dataclasses.replace(good, timed_out=True), good)

    def test_line_order_is_not_normalized(self):
        a = self.result(out=b'one\ntwo\n')
        b = self.result(out=b'two\none\n')
        with self.assertRaises(AssertionError):
            self.m.same_result(a, b)

    def test_fixed_expectations_reject_any_difference(self):
        self.m.fixed(self.result(2, b'o', b'e'), 2, b'o', b'e')
        for args in [(3, b'o', b'e'), (2, b'x', b'e'), (2, b'o', b'f'), (2, b'o', b'')]:
            with self.assertRaises(AssertionError):
                self.m.fixed(self.result(2, b'o', b'e'), *args)
        with self.assertRaises(AssertionError):
            self.m.fixed(self.result(0, timeout=True), 0)

    def test_approved_pair_checks_old_and_new_separately(self):
        old, new = self.result(1, b'a', b''), self.result(2, b'', b'')
        self.m.fixed_pair(old, new, (1, b'a', b''), (2, b'', b''))
        with self.assertRaises(AssertionError):
            self.m.fixed_pair(old, new, (2, b'', b''), (2, b'', b''))
        with self.assertRaises(AssertionError):
            self.m.fixed_pair(old, new, (1, b'a', b''), (1, b'a', b''))

    # ---- U4: 途中の行だけを除く

    def test_u4_removes_only_the_middle_lines(self):
        self.assertEqual(self.m.strip_traceback_middle(TRACEBACK_OLD), TRACEBACK_NEW)
        # 見出しの外の字下げした行は除かない。
        text = b'  keep before\nTraceback (most recent call last):\n  File x\nValueError: v\n  keep after\n'
        self.assertEqual(self.m.strip_traceback_middle(text),
                         b'  keep before\nTraceback (most recent call last):\nValueError: v\n  keep after\n')
        # 最後の行は、字下げが無いので必ず残る。
        self.assertTrue(self.m.strip_traceback_middle(b'Traceback (most recent call last):\n  a\n    b\nE: x\n').endswith(b'E: x\n'))

    def approved(self, new_err=TRACEBACK_NEW, **kw):
        old = self.result(1, b'partial', TRACEBACK_OLD)
        new = self.result(kw.get('code', 1), kw.get('out', b'partial'), new_err)
        self.m.approved_u4(old, new, script='/old/environment-guard.py', stdlib='/usr/lib/python3.10')

    def test_u4_control_passes_and_every_other_difference_fails(self):
        self.approved()
        for bad in [TRACEBACK_NEW.replace(b'IndexError: b', b'IndexError: c'),
                    TRACEBACK_NEW.replace(b'IndexError: b', b'IndexErro: b'),
                    TRACEBACK_NEW.replace(b"KeyError: 'a'", b"KeyError: 'b'"),
                    TRACEBACK_NEW.replace(b'During handling of the above exception, another exception occurred',
                                          b'The above exception was the direct cause of the following exception'),
                    TRACEBACK_NEW.replace(b'\n\nDuring handling of the above exception, another exception occurred:\n\n', b'\n'),
                    TRACEBACK_NEW.replace(b"KeyError: 'a'\n\nDuring", b"KeyError: 'a'\nDuring"),
                    TRACEBACK_NEW + b'x\n',
                    b'IndexError: b\n']:
            with self.assertRaises(AssertionError, msg=bad):
                self.approved(bad)
        with self.assertRaises(AssertionError):
            self.approved(code=2)
        with self.assertRaises(AssertionError):
            self.approved(out=b'parti')
        # Rust の側に途中の行が残っていても通さない(除くのは旧版の側だけ)。
        with self.assertRaises(AssertionError):
            self.approved(TRACEBACK_OLD)
        # 因果の順(ブロックの順)を入れ替えても通さない。
        first, second = TRACEBACK_NEW.split(b'\n\nDuring handling of the above exception, another exception occurred:\n\n')
        with self.assertRaises(AssertionError):
            self.approved(second + b'\n\nDuring handling of the above exception, another exception occurred:\n\n' + first)

    def test_old_environment_paths_are_replaced_but_user_paths_are_not(self):
        text = (b'x /old/dir/environment-guard.py y /usr/lib/python3.10/argparse.py z /usr/lib/python3.10/json/decoder.py '
                b'/work/user/environment-guard.py')
        replaced = self.m.fixed_old_paths(text, script='/old/dir/environment-guard.py', stdlib='/usr/lib/python3.10')
        self.assertEqual(replaced, b'x environment-guard.py y argparse.py z decoder.py /work/user/environment-guard.py')
        # 引用符の直後でも置き換える(traceback の File の行の形)。
        self.assertEqual(self.m.fixed_old_paths(b'File "/usr/lib/python3.10/argparse.py", line 3', script='/old/dir/environment-guard.py',
                                                stdlib='/usr/lib/python3.10'), b'File "argparse.py", line 3')

    def test_user_paths_containing_the_old_environment_paths_are_kept(self):
        # '-' を含まない利用者のパスと、旧版の実行環境のパスを途中に含む利用者のパスは、そのまま残す。
        kept = [b'/work/user/helper.py', b'/tmp/usr/lib/python3.10/argparse.py', b'/tmp/usr/lib/python3.10/json/decoder.py',
                b'/work/old/dir/environment-guard.py', b'/tmp/old/dir/environment-guard.py', b'a=/usr/lib/python3.10/argparse.py']
        for path in kept:
            with self.subTest(path=path):
                text = b'at ' + path + b' end'
                self.assertEqual(self.m.fixed_old_paths(text, script='/old/dir/environment-guard.py', stdlib='/usr/lib/python3.10'), text)
        mixed = b'/usr/lib/python3.10/argparse.py /tmp/usr/lib/python3.10/argparse.py /work/user/helper.py'
        self.assertEqual(self.m.fixed_old_paths(mixed, script='/old/dir/environment-guard.py', stdlib='/usr/lib/python3.10'),
                         b'argparse.py /tmp/usr/lib/python3.10/argparse.py /work/user/helper.py')

    # ---- Current thread の値だけ

    def fatal(self, new_err=FATAL_OLD.replace(b'7f3a2c42a000', b'7ffe11112222'), code=1, out=b''):
        self.m.approved_fatal(self.result(1, b'', FATAL_OLD), self.result(code, out, new_err))

    def test_current_thread_value_is_free_but_everything_else_is_checked(self):
        self.fatal()
        self.assertTrue(self.m.fatal_shape_ok(FATAL_OLD))
        for bad in [FATAL_OLD.replace(b'Fatal Python error', b'Fatal Python eror'),
                    FATAL_OLD.replace(b'core initialized', b'core initialised'),
                    FATAL_OLD.replace(b'  <no Python frame>\n', b''),
                    FATAL_OLD.replace(b'Current thread', b'Current threads'),
                    FATAL_OLD.replace(b'\n\nCurrent', b'\nCurrent'),
                    FATAL_OLD.replace(b'(most recent call first)', b'(most recent call last)'),
                    FATAL_OLD + b'extra\n',
                    FATAL_OLD.replace(b'0x00007f3a2c42a000', b'0x'),
                    b'Current thread 0x1 (most recent call first):\n  <no Python frame>\n' + FATAL_OLD]:
            with self.assertRaises(AssertionError, msg=bad):
                self.fatal(bad)
        with self.assertRaises(AssertionError):
            self.fatal(code=2)
        with self.assertRaises(AssertionError):
            self.fatal(out=b'x')
        # 診断の本文(Current thread の値の外)が 1 文字違えば落ちる。
        with self.assertRaises(AssertionError):
            self.fatal(FATAL_OLD.replace(b'<stdin>', b'<stdout>'))
        self.assertEqual(self.m.normalize_current_thread(FATAL_OLD).count(b'<THREAD>'), 1)

    # ---- 対応表

    def test_mapping_covers_every_existing_test_exactly_once(self):
        summary = self.m.check_mapping()
        self.assertEqual(summary['total'], 117)
        self.assertEqual(sum(summary['counts'].values()), 117)
        self.assertEqual(self.m.MAPPING_MD.read_text(encoding='utf-8'), self.m.mapping_markdown())
        saved = list(self.m.TEST_MAPPING)
        try:
            del self.m.TEST_MAPPING[3]
            with self.assertRaises(AssertionError):
                self.m.check_mapping()
            self.m.TEST_MAPPING[:] = saved + [saved[0]]
            with self.assertRaises(AssertionError):
                self.m.check_mapping()
            self.m.TEST_MAPPING[:] = saved
            self.m.TEST_MAPPING[5] = (saved[5][0], 'bogus', saved[5][2], '')
            with self.assertRaises(AssertionError):
                self.m.check_mapping()
        finally:
            self.m.TEST_MAPPING[:] = saved

    # ---- 実行(未起動・時間切れ・閉じた fd)

    def test_timeout_and_unstarted_are_not_passes(self):
        slow = self.m.run_process(['sleep', '5'], env=self.m.base_env(), cwd='/', timeout=0.3)
        self.assertTrue(slow.timed_out)
        with self.assertRaises(AssertionError):
            self.m.same_result(slow, slow)
        with self.assertRaises(OSError):
            self.m.run_process(['/nonexistent/s2e-fixture'], env=self.m.base_env(), cwd='/')

    def test_redirect_modes_really_close_or_redirect(self):
        probe = [sys.executable, '-c', 'import os,sys\ntry:\n os.fstat(int(sys.argv[1]))\nexcept OSError:\n sys.exit(7)\nsys.exit(0)']
        for fd, mode, code in [(1, 'close', 7), (2, 'close', 7), (0, 'close', 7), (1, 'full', 0)]:
            result = self.m.run_process(probe + [str(fd)], env=self.m.base_env(), cwd='/', redirects={fd: mode})
            self.assertEqual(result.returncode, code, (fd, mode, result))
        directory = self.m.run_process(['/bin/sh', '-c', 'readlink /proc/self/fd/0'], env=self.m.base_env(), cwd='/', redirects={0: 'dir'})
        self.assertEqual(directory.stdout, b'/\n')
        written = self.m.run_process([sys.executable, '-c', 'print("x")'], env=self.m.base_env(), cwd='/', redirects={1: 'pipe-closed'})
        self.assertEqual(written.stdout, b'')
        # 書く側が EPIPE を受けること(読み手が閉じたパイプであること)を終了コードで確かめる。/dev/null なら 0 になる。
        epipe = [sys.executable, '-c', 'import os,sys\ntry:\n os.write(int(sys.argv[1]), b"x")\nexcept BrokenPipeError:\n sys.exit(9)\nsys.exit(0)']
        for fd in (1, 2):
            result = self.m.run_process(epipe + [str(fd)], env=self.m.base_env(), cwd='/', redirects={fd: 'pipe-closed'})
            self.assertEqual(result.returncode, 9, (fd, result))
        self.assertEqual(self.m.run_process(epipe + ['1'], env=self.m.base_env(), cwd='/').returncode, 0)

    # ---- U7: 旧新それぞれの固定期待

    def test_u7_checks_old_and_new_expectations_separately(self):
        class Stub:
            def __init__(self, old, new):
                self.old_result, self.new_result, self.old_script = old, new, '/old/environment-guard.py'

            def old(self, case, redirects=None):
                return self.old_result

            def new(self, case):
                return self.new_result

        case = self.m.Case('u7', 'stub', ['x'], kind='u7', u7=lambda normal: ((1, b'old-out', b''), (2, b'', b'')))
        good_old, good_new = self.result(1, b'old-out', b''), self.result(2, b'', b'')

        def run(old, new):
            tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
            self.m.run_case(Stub(old, new), case, tally)
            return tally

        self.assertEqual(run(good_old, good_new)['approved'], 1)
        for old, new in [(self.result(1, b'old-outX', b''), good_new), (self.result(0, b'old-out', b''), good_new),
                         (self.result(1, b'old-out', b'e'), good_new), (dataclasses.replace(good_old, timed_out=True), good_new),
                         (good_old, self.result(2, b'x', b'')), (good_old, self.result(1, b'', b'')),
                         (good_old, dataclasses.replace(good_new, timed_out=True))]:
            with self.assertRaises(AssertionError):
                run(old, new)

    def test_mapping_summary_separates_planned_from_done(self):
        summary = self.m.check_mapping()
        self.assertEqual(summary['compared'] + summary['planned'], 117)
        self.assertLessEqual(summary['done'], summary['compared'])
        rows = {row[0]: row for row in self.m.TEST_MAPPING}
        self.assertEqual(summary['done'], sum(1 for row in rows.values() if '実装済み' in row[2] and '予定' not in row[2]))
        # U2 の件数は、旧新の両方で比較の行のうち bootstrap を使うものだけ。
        uses = {name: flag for _, name, flag in self.m.existing_test_names()}
        self.assertEqual(summary['rebuilt_for_u2'], sum(1 for name, row in rows.items() if row[1] == self.m.B and uses[name]))
        self.assertLessEqual(summary['rebuilt_for_u2'], summary['counts'][self.m.B])

    def test_catalog_has_the_required_groups_and_pending_frames(self):
        cases = self.m.all_cases()
        groups = {case.group for case in cases}
        self.assertTrue({'args', 'host-paths', 'output', 'u7', 'u7-same', 'fatal'} <= groups)
        commands = {case.name.split('/')[0] for case in cases if case.group == 'args'}
        self.assertTrue(set(self.m.COMMANDS) <= commands)
        self.assertEqual(len(set((c.group, c.name) for c in cases)), len(cases), 'ケース名が重複している')
        self.assertGreaterEqual(len(self.m.PENDING_2B), 8)


class MutatedFixture(unittest.TestCase):
    """旧 guard を包んだ偽の検査入口に、不具合を 1 つずつ入れて、比較器が落とすことを確かめる。"""

    def setUp(self):
        self.m = get_subject()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.script = self.m.stage_distribution(self.base)
        self.work = self.base / 'work'
        self.work.mkdir()

    def fake(self, mode):
        path = self.base / f'fake-{mode}'
        path.write_text(textwrap.dedent(f'''\
            #!{sys.executable}
            import os, subprocess, sys, time
            mode = {mode!r}
            guard = {str(self.script)!r}
            if mode == 'control':
                os.execv(sys.executable, [sys.executable, '-B', guard] + sys.argv[1:])
            if mode == 'sleep':
                time.sleep(120)
            capture_out = mode in ('stdout_char', 'order')
            done = subprocess.run([sys.executable, '-B', guard] + sys.argv[1:], stdout=subprocess.PIPE if capture_out else None,
                                  stderr=subprocess.PIPE)
            out, err, code = done.stdout or b'', done.stderr, done.returncode
            if mode == 'stdout_char':
                out = (out[:-2] + b'X' + out[-1:]) if len(out) > 1 else out + b'X'
            elif mode == 'stderr_char':
                err = (err[:-2] + b'X' + err[-1:]) if len(err) > 1 else err + b'X'
            elif mode == 'code':
                code += 1
            elif mode == 'order':
                out = b''.join(reversed(out.splitlines(True)))
                err = b''.join(reversed(err.splitlines(True)))
            elif mode == 'traceback_last_line':
                err = err.replace(b'OSError', b'OSErro')
            elif mode == 'strip_middle':
                kept, inside = [], False
                for line in err.split(b'\\n'):
                    if line.startswith(b'Traceback (most recent call last):'):
                        inside = True
                        kept.append(line)
                    elif inside and line.startswith(b'  '):
                        continue
                    else:
                        inside = False
                        kept.append(line)
                err = b'\\n'.join(kept)
            for fd, data in ((1, out if capture_out else b''), (2, err)):
                try:
                    os.write(fd, data)
                except OSError:
                    pass
            sys.exit(code)
            '''), encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def fake_sh(self, mode):
        """標準の fd がディレクトリのときだけ別の結果を返す偽の入口。Python で書くと、その Python が
        起動時に落ちるので、sh で書く。ほかのときは旧 guard をそのまま起動する。"""
        folder = self.base / f'sh-{mode}'
        folder.mkdir(exist_ok=True)
        for name, (code, out, err) in self.m.UNIMPLEMENTED_FATAL.items():
            command, fd = name.split('-fd')
            key = f'{command}-{fd[0]}'
            (folder / (key + '.out')).write_bytes(out)
            (folder / (key + '.err')).write_bytes(err)
            (folder / (key + '.code')).write_text(str(code))
        path = self.base / f'fake-sh-{mode}'
        path.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            if [ -d /proc/self/fd/0 ]; then fd=0; elif [ -d /proc/self/fd/1 ]; then fd=1; elif [ -d /proc/self/fd/2 ]; then fd=2; else fd=; fi
            if [ -z "$fd" ]; then exec {sys.executable} -B {str(self.script)} "$@"; fi
            case {mode} in
              sleep) exec sleep 120 ;;
              panic) echo "thread 'main' panicked at src/main.rs:1:1:" >&2; exit 101 ;;
              badshape)
                printf 'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\\nPython runtime state: core initialized\\n\\nCurrent thread 0x00007f3a2c42a000 (most recent call first):\\n' >&2
                exit 1 ;;
              unimplemented)
                key="$1-$fd"
                cat {folder}/$key.err >&2
                if [ "$fd" != 1 ]; then cat {folder}/$key.out; fi
                exit "$(cat {folder}/$key.code)" ;;
              *) exec {sys.executable} -B {str(self.script)} "$@" ;;
            esac
            '''), encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def contract(self, mode, timeout=30):
        return self.m.run_contract(self.fake(mode), self.m.smoke_cases(), old_script=self.script, cwd=self.work, timeout=timeout)

    def test_control_passes_and_counts_cases(self):
        tally = self.contract('control')
        self.assertEqual(tally['matched'], len(self.m.smoke_cases()))
        self.assertEqual((tally['approved'], tally['unimplemented']), (0, []))

    def test_u4_auto_cases_count_as_approved_only_when_the_old_side_has_a_traceback(self):
        tally = self.contract('strip_middle')
        self.assertEqual((tally['approved'], tally['u4']), (1, 1))
        self.assertEqual(tally['matched'], len(self.m.smoke_cases()) - 1)
        # 途中の行を除いていない(旧版と同じ)出力は、通常の一致になる。
        self.assertEqual(self.contract('control')['approved'], 0)

    def test_u4_last_line_is_checked_by_the_u4_comparison_itself(self):
        # u4auto の 1 件だけで流す。落ちた件の名前が u4auto の件であることまで確かめる(別の件で落ちたのを区別する)。
        cases = [c for c in self.m.smoke_cases() if c.kind == 'u4auto']
        self.assertEqual([c.name for c in cases], ['help-full-unbuffered'])
        tally = self.m.run_contract(self.fake('strip_middle'), cases, old_script=self.script, cwd=self.work)
        self.assertEqual((tally['matched'], tally['approved'], tally['u4']), (0, 1, 1))
        with self.assertRaises(AssertionError) as caught:
            self.m.run_contract(self.fake('traceback_last_line'), cases, old_script=self.script, cwd=self.work)
        self.assertTrue(str(caught.exception).startswith('output/help-full-unbuffered: U4:'), str(caught.exception))
        # 旧版と同じでも、途中の行だけを除いても通る件は、U4 の比べに入らず通常の一致か認めた差になる。
        self.assertEqual(self.m.run_contract(self.fake('control'), cases, old_script=self.script, cwd=self.work)['approved'], 0)

    def test_each_defect_is_rejected(self):
        for mode in ('stdout_char', 'stderr_char', 'code', 'order'):
            with self.subTest(mode=mode), self.assertRaises(AssertionError):
                self.contract(mode)

    def test_timeout_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.contract('sleep', timeout=0.5)

    def test_unstarted_fixture_is_rejected(self):
        with self.assertRaises(OSError):
            self.m.run_contract(self.base / 'does-not-exist', self.m.smoke_cases(), old_script=self.script, cwd=self.work)
        with self.assertRaises(ValueError):
            self.m.fixture_path(str(self.base / 'does-not-exist'))
        with self.assertRaises(ValueError):
            self.m.fixture_path('relative/path')

    def test_zero_cases_is_not_a_pass(self):
        with self.assertRaises(AssertionError):
            self.m.run_contract(self.fake('control'), [], old_script=self.script, cwd=self.work)

    def test_parse_only_assumption_is_enforced(self):
        # 解析だけで終わる前提のケースが、旧版で別の終了値になったら不合格(成功経路を紛れ込ませない)。
        case = self.m.Case('args', 'not-parse-only', ['host-paths'], self.m.base_env({'HOME': 'rel'}), kind='parse')
        with self.assertRaises(AssertionError):
            self.m.run_contract(self.fake('control'), [case], old_script=self.script, cwd=self.work)

    def test_u7_cases_use_separate_expectations(self):
        # U7: 旧 guard をそのまま包んだ物は、Rust の側の固定期待(開き直した /dev/null の結果)とは違うので落ちる。
        u7 = [c for c in self.m.all_cases() if c.group == 'u7']
        self.assertGreaterEqual(len(u7), 5)
        for case in u7:
            with self.subTest(case=case.name), self.assertRaises(AssertionError):
                self.m.run_contract(self.fake('control'), [case], old_script=self.script, cwd=self.work)

    def fatal_cases(self):
        cases = [c for c in self.m.all_cases() if c.group == 'fatal']
        self.assertEqual({c.name for c in cases}, set(self.m.UNIMPLEMENTED_FATAL))
        return cases

    def renamed(self, case, name='other-fd-directory'):
        return dataclasses.replace(case, name=name)

    def run_fatal(self, mode, case, timeout=30):
        """比べる件が 0 件で落ちる(ほかの理由で落ちた)のと区別するため、通常の一致の件を 1 つ足して流す。"""
        return self.m.run_contract(self.fake_sh(mode), [case] + self.m.smoke_cases()[2:3], old_script=self.script, cwd=self.work, timeout=timeout)

    def assertFatalRejected(self, mode, case, timeout=30):
        with self.assertRaises(AssertionError) as caught:
            self.run_fatal(mode, case, timeout)
        self.assertTrue(str(caught.exception).startswith(f'fatal/{case.name}: '), str(caught.exception))

    def test_fatal_cases_on_the_static_list_compare_the_current_rust_result(self):
        cases = self.fatal_cases()
        # 今の Rust と同じ結果を返す偽の入口は、未実装の印に数える(件数には入れない)。
        tally = self.m.run_contract(self.fake_sh('unimplemented'), cases + self.m.smoke_cases()[2:3], old_script=self.script, cwd=self.work)
        self.assertEqual((tally['matched'], tally['approved']), (1, 0))
        self.assertEqual(sorted(tally['unimplemented']), sorted('fatal/' + c.name for c in cases))
        # 一覧の件で、旧 guard と同じ Fatal を返す(実装済みに見える)入口は、固定期待と合わないので落ちる。
        for case in cases:
            with self.subTest(case=case.name, mode='control'):
                self.assertFatalRejected('control', case)

    def test_static_list_cases_reject_timeout_panic_and_broken_fatal(self):
        for mode, timeout in (('sleep', 1), ('panic', 30), ('badshape', 30)):
            for case in self.fatal_cases():
                with self.subTest(mode=mode, case=case.name):
                    self.assertFatalRejected(mode, case, timeout)

    def test_names_outside_the_static_list_never_become_unimplemented(self):
        for case in self.fatal_cases():
            other = self.renamed(case)
            self.assertNotIn(other.name, self.m.UNIMPLEMENTED_FATAL)
            # 旧 guard と同じ形式・同じ本文なら、Current thread の値が違っても通り、「認めた差」に数える。
            tally = self.run_fatal('control', other)
            # 標準エラーがディレクトリの件は、旧版と完全に一致するので通常の一致に数える。
            expected = (2, 0, []) if case.kind == 'fatal-silent' else (1, 1, [])
            self.assertEqual((tally['matched'], tally['approved'], tally['unimplemented']), expected)
            # 形が合わない結果(今の Rust の結果・panic・形の崩れた Fatal・時間切れ)は、未実装に回らずに落ちる。
            # 標準エラーがディレクトリの件は、旧版が何も書かない(終了コード 1・出力なし)ので、形の崩れた Fatal は旧版と同じ見た目になる。
            modes = [('unimplemented', 30), ('panic', 30), ('badshape', 30), ('sleep', 1)]
            if case.kind == 'fatal-silent':
                modes.remove(('badshape', 30))
            for mode, timeout in modes:
                with self.subTest(case=case.name, mode=mode):
                    self.assertFatalRejected(mode, other, timeout)

    def test_stderr_directory_case_fixes_the_old_result_and_the_current_rust_result(self):
        result = lambda code=0, out=b'', err=b'': self.m.RunResult(code, out, err, False)  # noqa: E731
        case = next(c for c in self.fatal_cases() if c.kind == 'fatal-silent')
        self.assertEqual(case.redirects, {2: 'dir'})
        # 旧版の固定期待(1・空・空)が崩れたら落ちる(旧版の側の結果をずらした Runner)。
        class Stub:
            old_script = '/old/environment-guard.py'

            def __init__(self, old, new):
                self.old_result, self.new_result = old, new

            def old(self, case, redirects=None):
                return self.old_result

            def new(self, case):
                return self.new_result

        rust = result(*self.m.UNIMPLEMENTED_FATAL[case.name])
        tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
        self.m.run_case(Stub(result(1), rust), case, tally)
        self.assertEqual(tally['unimplemented'], [f'fatal/{case.name}'])
        # 一覧の外(名前を変えた件)は、旧版と出力まで完全に一致したときだけ通る(通常の一致に数える)。
        other = dataclasses.replace(case, name='other-fd2-directory')
        tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
        self.m.run_case(Stub(result(1), result(1)), other, tally)
        self.assertEqual((tally['matched'], tally['approved'], tally['unimplemented']), (1, 0, []))
        for new in (result(1, b'x'), result(1, b'', b'x'), result(0), result(2), dataclasses.replace(result(1), timed_out=True), rust):
            with self.assertRaises(AssertionError):
                self.m.run_case(Stub(result(1), new), other, {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []})
        for old, new in [(result(0), rust), (result(1, b'x'), rust), (result(1, b'', b'x'), rust),
                         (dataclasses.replace(result(1), timed_out=True), rust), (result(1), result(1)),
                         (result(1), dataclasses.replace(rust, timed_out=True)), (result(1), result(0, rust.stdout, b'x'))]:
            with self.assertRaises(AssertionError):
                self.m.run_case(Stub(old, new), case, {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []})


class ComparatorFoundations(unittest.TestCase):
    """比較器の土台(件数の数え方・旧版の起動・配布物の確かめ)を外す変異を落とす。"""

    def setUp(self):
        self.m = get_subject()

    def test_run_suite_rejects_zero_failed_skipped_and_miscounted_cases(self):
        class Good(unittest.TestCase):
            def test_ok(self):
                pass

        class Failure(unittest.TestCase):
            def test_broken(self):
                self.fail('deliberate')

        class Skipped(unittest.TestCase):
            def test_skipped(self):
                self.skipTest('deliberate')

        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def test_expected(self):
                self.fail('deliberate')

        class Unexpected(unittest.TestCase):
            @unittest.expectedFailure
            def test_unexpected(self):
                pass

        class Miscount(unittest.TestCase):
            def countTestCases(self):
                return 2

            def test_one(self):
                pass

        load = unittest.defaultTestLoader.loadTestsFromTestCase
        with mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(self.m.run_suite(load(Good)), 1)
            for suite in (unittest.TestSuite(), load(Failure), load(Skipped), load(Expected), load(Unexpected), load(Miscount),
                          unittest.TestSuite([load(Good), load(Skipped)])):
                with self.assertRaises(AssertionError):
                    self.m.run_suite(suite)

    def test_u4_comparison_rejects_timeouts_on_either_side(self):
        kw = dict(script='/old/environment-guard.py', stdlib='/usr/lib/python3.10')
        old, new = self.m.RunResult(1, b'o', TRACEBACK_OLD, False), self.m.RunResult(1, b'o', TRACEBACK_NEW, False)
        self.m.approved_u4(old, new, **kw)
        for a, b in ((dataclasses.replace(old, timed_out=True), new), (old, dataclasses.replace(new, timed_out=True))):
            with self.assertRaises(AssertionError):
                self.m.approved_u4(a, b, **kw)

    def test_old_side_is_started_with_dash_b_from_the_staged_distribution(self):
        with tempfile.TemporaryDirectory() as folder:
            staged = self.m.stage_distribution(Path(folder))
            self.assertEqual(staged, Path(folder) / 'dist/skills/ship-task/scripts/environment-guard.py')
            seen = []

            def capture(argv, **kwargs):
                seen.append((argv, kwargs))
                return self.m.RunResult(0, b'', b'', False)

            runner = self.m.Runner(Path('/fixture'), staged, Path(folder), 7)
            case = self.m.Case('args', 'x', ['verify', '-h'], self.m.base_env({'HOME': '/h'}), {1: 'full'})
            with mock.patch.object(self.m, 'run_process', capture):
                runner.old(case)
                runner.new(case)
            old_argv, old_kwargs = seen[0]
            self.assertEqual(old_argv, [sys.executable, '-B', str(staged), b'verify', b'-h'])
            self.assertEqual((old_kwargs['env'], old_kwargs['redirects'], old_kwargs['timeout']), (case.env, case.redirects, 7))
            self.assertEqual(seen[1][0], ['/fixture', b'verify', b'-h'])

    def test_staging_stops_when_the_old_guard_sha256_differs(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            other = base / 'other-guard.py'
            other.write_bytes(self.m.GUARD.read_bytes() + b'# changed\n')
            with mock.patch.object(self.m, 'GUARD', other), self.assertRaises(AssertionError):
                self.m.stage_distribution(base / 'a')
            with mock.patch.object(self.m, 'GUARD_SHA256', '0' * 64), self.assertRaises(AssertionError):
                self.m.stage_distribution(base / 'b')
            self.assertFalse((base / 'a').exists() or (base / 'b').exists())
            staged = self.m.stage_distribution(base / 'c')
            self.assertEqual(staged.read_bytes(), self.m.GUARD.read_bytes())
            self.assertEqual(staged.stat().st_mode & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
