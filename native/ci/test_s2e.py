#!/usr/bin/env python3
"""S2E 比較器(check_s2e.py)自身の検査。不具合を入れた結果・未起動・時間切れを不合格にすることを確かめる。"""
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import resource
import signal
from pathlib import Path
import stat
import subprocess
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
            if mode == 'full' and not self.m.has_dev_full():
                continue   # /dev/full は Linux だけ(Mac に無い。Mac の経路の模擬でも無い物として扱う)
            result = self.m.run_process(probe + [str(fd)], env=self.m.base_env(), cwd='/', redirects={fd: mode})
            self.assertEqual(result.returncode, code, (fd, mode, result))
        # fd 0 がディレクトリ(`/` を開いたもの)であること。Python は標準入力がディレクトリだと起動しないので sh で見る。
        # `/dev/fd/0` は Linux・Mac の両方にある(`/proc` は Linux だけ)。
        is_dir = ['/bin/sh', '-c', '[ -d /dev/fd/0 ]']
        self.assertEqual(self.m.run_process(is_dir, env=self.m.base_env(), cwd='/', redirects={0: 'dir'}).returncode, 0)
        self.assertEqual(self.m.run_process(is_dir, env=self.m.base_env(), cwd='/').returncode, 1)
        written = self.m.run_process([sys.executable, '-c', 'print("x")'], env=self.m.base_env(), cwd='/', redirects={1: 'pipe-closed'})
        self.assertEqual(written.stdout, b'')
        # 書く側が EPIPE を受けること(読み手が閉じたパイプであること)を終了コードで確かめる。/dev/null なら 0 になる。
        epipe = [sys.executable, '-c', 'import os,sys\ntry:\n os.write(int(sys.argv[1]), b"x")\nexcept BrokenPipeError:\n sys.exit(9)\nsys.exit(0)']
        for fd in (1, 2):
            result = self.m.run_process(epipe + [str(fd)], env=self.m.base_env(), cwd='/', redirects={fd: 'pipe-closed'})
            self.assertEqual(result.returncode, 9, (fd, result))
        self.assertEqual(self.m.run_process(epipe + ['1'], env=self.m.base_env(), cwd='/').returncode, 0)

    def test_sigpipe_ignore_mode_really_starts_the_child_with_sigpipe_ignored(self):
        # Python は自分で SIGPIPE を無視にするので、Python でなく sh で見る(Linux の /proc は Mac に無い)。SIGPIPE を自分へ送り、無視なら生き残って alive を出す。
        probe = ['/bin/sh', '-c', 'kill -s PIPE $$; echo alive']

        def ignored(redirects):
            result = self.m.run_process(probe, env=self.m.base_env(), cwd='/', redirects=redirects)
            return result.stdout == b'alive\n'
        self.assertFalse(ignored({}))
        self.assertTrue(ignored({'sigpipe': 'ignore'}))
        self.assertFalse(ignored({'sigpipe': 'default'}))
        # 呼び出し側の辞書は変えない。
        redirects = {'sigpipe': 'ignore'}
        self.m.run_process(probe, env=self.m.base_env(), cwd='/', redirects=redirects)
        self.assertEqual(redirects, {'sigpipe': 'ignore'})

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
        # 予定の枠は 0 件。0 件でなければ比較器は不合格にする。
        self.assertEqual(len(self.m.PENDING_2B), 0)
        self.m.check_pending_is_empty()
        for pending in (('read・exec: 生バイト',), ('hook', 'U7')):
            with self.assertRaisesRegex(AssertionError, r'予定の枠が 0 件でない.*: %d 件' % len(pending)):
                self.m.check_pending_is_empty(pending)

    def test_a_planned_mapping_row_is_rejected(self):
        rows = list(self.m.TEST_MAPPING)
        planned = list(rows[0])
        planned[2] = 'verify-pair/go(予定)'
        rows[0] = tuple(planned)
        with mock.patch.object(self.m, 'TEST_MAPPING', rows):
            with self.assertRaisesRegex(AssertionError, r'「予定」が残っている'):
                self.m.check_mapping()
        self.assertEqual(self.m.mapping_summary()['planned'], 0)


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
                head, _, last = err.rstrip(b'\\n').rpartition(b'\\n')   # 最後の行の例外名か本文を 1 文字変える(機種で最後の行が違う: OSError・BrokenPipeError)
                err = head + b'\\n' + last[:-1] + b'X\\n'
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
        path = self.base / f'fake-sh-{mode}'
        path.write_text(textwrap.dedent(f'''\
            #!/bin/sh
            if [ -d /dev/fd/0 ]; then fd=0; elif [ -d /dev/fd/1 ]; then fd=1; elif [ -d /dev/fd/2 ]; then fd=2; elif [ -n "${{PYTHONHASHSEED+x}}" ]; then fd=h; else fd=; fi
            if [ -z "$fd" ]; then exec {sys.executable} -B {str(self.script)} "$@"; fi
            case {mode} in
              sleep) exec sleep 120 ;;
              panic) echo "thread 'main' panicked at src/main.rs:1:1:" >&2; exit 101 ;;
              badshape)
                printf 'Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\\nPython runtime state: core initialized\\n\\nCurrent thread 0x00007f3a2c42a000 (most recent call first):\\n' >&2
                exit 1 ;;
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
        target = 'full' if self.m.has_dev_full() else 'pipe-closed'
        self.assertEqual([c.name for c in cases], [f'help-{target}-unbuffered'])
        tally = self.m.run_contract(self.fake('strip_middle'), cases, old_script=self.script, cwd=self.work)
        self.assertEqual((tally['matched'], tally['approved'], tally['u4']), (0, 1, 1))
        with self.assertRaises(AssertionError) as caught:
            self.m.run_contract(self.fake('traceback_last_line'), cases, old_script=self.script, cwd=self.work)
        self.assertTrue(str(caught.exception).startswith(f'output/help-{target}-unbuffered: U4:'), str(caught.exception))
        # 旧版と同じでも、途中の行だけを除いても通る件は、U4 の比べに入らず通常の一致か認めた差になる。
        self.assertEqual(self.m.run_contract(self.fake('control'), cases, old_script=self.script, cwd=self.work)['approved'], 0)

    def test_dev_full_is_never_used_where_it_does_not_exist(self):
        # Mac(と、その経路の模擬)には /dev/full が無い。出力の故障の件は pipe-closed だけにし、full の指定は黙って続けず止める。
        with mock.patch.dict(os.environ, {'S2E_SIMULATE_MAC': '1'}):
            self.assertFalse(self.m.has_dev_full())
            outputs = self.m.output_failure_cases()
            self.assertTrue(outputs)
            self.assertEqual({fd_mode for c in outputs for fd_mode in c.redirects.values()}, {'pipe-closed'})
            smoke = self.m.smoke_cases()
            self.assertEqual([c.name for c in smoke if c.group == 'output'], ['pipe-closed', 'help-pipe-closed-unbuffered'])
            for case in smoke:
                self.assertNotIn('full', case.redirects.values())
            with self.assertRaisesRegex(AssertionError, r'/dev/full が無い機種'):
                self.m.run_process(['true'], env=self.m.base_env(), cwd='/', redirects={1: 'full'})
        with mock.patch.object(self.m, 'has_dev_full', lambda: False):
            with self.assertRaisesRegex(AssertionError, r'/dev/full が無い機種'):
                self.m.run_process(['true'], env=self.m.base_env(), cwd='/', redirects={2: 'full'})

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
        names = {c.name for c in cases}
        # 起動時の Fatal は全件を再現する(固定の未実装の一覧は無い)。
        self.assertFalse(hasattr(self.m, 'UNIMPLEMENTED_FATAL'))
        self.assertTrue({'host-paths-fd0-directory', 'host-paths-fd1-directory', 'host-paths-fd2-directory', 'verify-pair-fd0-directory',
                         "host-paths-hashseed-'abc'", 'host-paths-hashseed-before-intmaxstrdigits'} <= names, names)
        return cases

    def run_fatal(self, mode, case, timeout=30):
        """比べる件が 0 件で落ちる(ほかの理由で落ちた)のと区別するため、通常の一致の件を 1 つ足して流す。"""
        return self.m.run_contract(self.fake_sh(mode), [case] + self.m.smoke_cases()[2:3], old_script=self.script, cwd=self.work, timeout=timeout)

    def assertFatalRejected(self, mode, case, timeout=30):
        with self.assertRaises(AssertionError) as caught:
            self.run_fatal(mode, case, timeout)
        self.assertTrue(str(caught.exception).startswith(f'fatal/{case.name}: '), str(caught.exception))

    def test_every_fatal_case_passes_with_the_old_result_and_is_never_unimplemented(self):
        for case in self.fatal_cases():
            with self.subTest(case=case.name):
                # 旧 guard と同じ形式・同じ本文なら、Current thread の値が違っても通り、「認めた差」に数える。
                # 旧版が診断を書けない件(標準エラーがディレクトリ)は、旧版と完全に一致するので通常の一致に数える。
                tally = self.run_fatal('control', case)
                # 設定の段階の診断(PYTHONHASHSEED・PYTHONINTMAXSTRDIGITS の誤り。Current thread の行が無い)も全バイトが一致するので通常の一致。
                # Current thread の行がある件(標準の fd がディレクトリ)だけが「認めた差」。
                config = any(key in case.env for key in ('PYTHONHASHSEED', 'PYTHONINTMAXSTRDIGITS'))
                expected = (2, 0) if case.kind == 'fatal-silent' or config else (1, 1)
                self.assertEqual((tally['matched'], tally['approved'], tally['unimplemented']), (*expected, []))

    def test_config_stage_fatal_is_matched_only_on_all_bytes_and_thread_fatal_is_approved(self):
        result = lambda code=0, out=b'', err=b'': self.m.RunResult(code, out, err, False)  # noqa: E731
        config_old = (b'Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be "random" or an integer in range [0; 4294967295]\n'
                      b'Python runtime state: preinitialized\n\n')
        config_cases = [c for c in self.fatal_cases() if any(k in c.env for k in ('PYTHONHASHSEED', 'PYTHONINTMAXSTRDIGITS'))]
        self.assertEqual(len(config_cases), 15)
        thread_cases = [c for c in self.fatal_cases() if c.kind == 'fatal' and c not in config_cases]
        self.assertTrue(thread_cases)

        class Stub:
            old_script = '/old/environment-guard.py'

            def __init__(self, old, new):
                self.old_result, self.new_result = old, new

            def old(self, case, redirects=None):
                return self.old_result

            def new(self, case):
                return self.new_result

        def run(case, old, new):
            tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
            self.m.run_case(Stub(old, new), case, tally)
            return tally['matched'], tally['approved']

        self.assertEqual(run(config_cases[0], result(1, b'', config_old), result(1, b'', config_old)), (1, 0))
        # Current thread の行を足した Rust の出力は、設定の段階では一致と認めない。
        with_thread = config_old + b'Current thread 0x00007f3a2c42a000 (most recent call first):\n  <no Python frame>\n'
        with self.assertRaises(AssertionError):
            run(config_cases[0], result(1, b'', config_old), result(1, b'', with_thread))
        # 本文の 1 文字の違いも落ちる(Current thread の値のような「許す差」は設定の段階には無い)。
        with self.assertRaises(AssertionError):
            run(config_cases[0], result(1, b'', config_old), result(1, b'', config_old.replace(b'hash_seed', b'hash_seeX')))
        # Current thread の行がある診断は、値の違いだけ許して「認めた差」に数える。
        self.assertEqual(run(thread_cases[0], result(1, b'', FATAL_OLD), result(1, b'', FATAL_OLD.replace(b'7f3a2c42a000', b'7ffe11112222'))), (0, 1))

    def test_fatal_cases_reject_timeout_panic_and_broken_fatal(self):
        for case in self.fatal_cases():
            modes = [('panic', 30), ('badshape', 30), ('sleep', 1)]
            if case.kind == 'fatal-silent':
                # 旧版が何も書かない(終了コード 1・出力なし)ので、形の崩れた Fatal は旧版と同じ見た目になりうる。
                modes.remove(('badshape', 30))
            for mode, timeout in modes:
                with self.subTest(case=case.name, mode=mode):
                    self.assertFatalRejected(mode, case, timeout)

    def test_startup_cases_compare_the_whole_result(self):
        ok = [c for c in self.m.all_cases() if c.group == 'startup-ok']
        self.assertGreaterEqual(len(ok), 10)
        tally = self.m.run_contract(self.fake('control'), ok, old_script=self.script, cwd=self.work)
        self.assertEqual((tally['matched'], tally['approved']), (len(ok), 0))
        # 結果の 1 バイトを変えた入口は落ちる。
        with self.assertRaises(AssertionError):
            self.m.run_contract(self.fake('stdout_char'), ok, old_script=self.script, cwd=self.work)

    def test_stderr_directory_case_fixes_the_old_result_and_requires_the_same_rust_result(self):
        result = lambda code=0, out=b'', err=b'': self.m.RunResult(code, out, err, False)  # noqa: E731
        case = next(c for c in self.fatal_cases() if c.kind == 'fatal-silent')
        self.assertEqual(case.redirects, {2: 'dir'})

        class Stub:
            old_script = '/old/environment-guard.py'

            def __init__(self, old, new):
                self.old_result, self.new_result = old, new

            def old(self, case, redirects=None):
                return self.old_result

            def new(self, case):
                return self.new_result

        # 旧版の固定期待(1・空・空)と同じ結果だけが通る。
        tally = {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}
        self.m.run_case(Stub(result(1), result(1)), case, tally)
        self.assertEqual((tally['matched'], tally['approved'], tally['unimplemented']), (1, 0, []))
        for new in (result(1, b'x'), result(1, b'', b'x'), result(0), result(2), dataclasses.replace(result(1), timed_out=True)):
            with self.assertRaises(AssertionError):
                self.m.run_case(Stub(result(1), new), case, {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []})
        # 旧版の側の結果が固定期待(1・空・空)から外れたら、新旧が同じでも落ちる。
        for old in (result(0), result(1, b'x'), result(1, b'', b'x'), dataclasses.replace(result(1), timed_out=True)):
            with self.assertRaises(AssertionError):
                self.m.run_case(Stub(old, old), case, {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []})


    def test_closed_stderr_cases_fix_the_old_result_and_require_the_same_rust_result(self):
        result = lambda code=0, out=b'', err=b'': self.m.RunResult(code, out, err, False)  # noqa: E731
        cases = [c for c in self.m.all_cases() if c.group == 'fatal-closed-stderr']
        kinds = {c.kind for c in cases}
        self.assertEqual(kinds, {'fatal-sigpipe', 'fatal-epipe'})
        self.assertTrue({'hashseed', 'intmaxstrdigits', 'fd0-directory'} <= {c.name for c in cases})
        # 親が SIGPIPE を無視にして起動する場合(旧版は信号でなく 1)が、設定の段階の Fatal ごとに入っている。
        ignored = [c for c in cases if c.redirects.get('sigpipe') == 'ignore']
        self.assertEqual({c.kind for c in ignored}, {'fatal-epipe'})
        self.assertTrue({'ignored-sigpipe-hashseed', 'ignored-sigpipe-intmaxstrdigits', 'ignored-sigpipe-hashseed-help'} <= {c.name for c in ignored})

        class Stub:
            old_script = '/old/environment-guard.py'

            def __init__(self, old, new):
                self.old_result, self.new_result = old, new

            def old(self, case, redirects=None):
                return self.old_result

            def new(self, case):
                return self.new_result

        fresh = lambda: {'matched': 0, 'approved': 0, 'u4': 0, 'unimplemented': []}  # noqa: E731
        for case in cases:
            code = -13 if case.kind == 'fatal-sigpipe' else 1
            tally = fresh()
            self.m.run_case(Stub(result(code), result(code)), case, tally)
            self.assertEqual((tally['matched'], tally['approved']), (1, 0))
            # Rust が違う終了値(信号でなく 1、または逆)・出力・時間切れなら落ちる。
            other = 1 if code == -13 else -13
            for new in (result(other), result(code, b'x'), result(code, b'', b'x'), dataclasses.replace(result(code), timed_out=True)):
                with self.subTest(case=case.name, new=new), self.assertRaises(AssertionError):
                    self.m.run_case(Stub(result(code), new), case, fresh())
            # 旧版の側の結果が固定期待から外れたら、新旧が同じでも落ちる。
            with self.subTest(case=case.name, old=other), self.assertRaises(AssertionError):
                self.m.run_case(Stub(result(other), result(other)), case, fresh())


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


# ---------------------------------------------------------------- 9 コマンドの旧新比較(s2e_commands.py)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s2e_commands  # noqa: E402

FAKE_COMMANDS = r'''#!{python}
import json, os, subprocess, sys, time
GUARD, MODE, READER, HOSTCHECK = {guard!r}, {mode!r}, {reader!r}, {hostcheck!r}
RUNNER, BASE = {runner!r}, {base!r}
def file_key(path):
    # 「この Rust が作った控え」の目印。inode 番号だけだと、消えたファイルの番号が別のファイルに再利用されて(負荷・順序で変わる)旧版の控えに当たるので、更新時刻(ns)も足す
    info = os.stat(path)
    return '%d:%d' % (info.st_ino, info.st_mtime_ns)
args = sys.argv[1:]
if MODE == 'sleep':
    time.sleep(120)
if args and args[0].startswith('reader-'):
    done = subprocess.run([sys.executable, '-B', READER, args[0], args[1], args[2], HOSTCHECK], capture_output=True)
    if MODE == 'reader-touch':
        os.utime(os.getcwd())   # 読取りだけの reader が、入力(流れの根のフォルダ自身)の時刻を変える
    lines = done.stdout.split(b'\n')
    pre = len(lines) == 4   # host-check の前処理で止まった入力は、3 行目に前処理を通さない呼出しの戻りが付く(Rust の reader はそれと同じ出力)
    out = (lines[2] if pre else lines[0]) + b'\n'   # 2 行目は host-check.py の戻りで、Rust の出力には無い
    if MODE == 'reader-pre-type' and pre:
        out = out.replace(b'"type":"', b'"type":"X', 1)
    if MODE == 'reader-pre-msg' and pre:
        out = out.replace(b'"msg":"', b'"msg":"X', 1)
    if MODE == 'json-type':
        out = out.replace(b'{{"t":"str"', b'{{"t":"bytes"', 1) if b'{{"t":"str"' in out else out.replace(b'{{"t":"list"', b'{{"t":"dict"')
    sys.stdout.buffer.write(out)
    sys.exit(done.returncode)
if MODE in ('budget', 'recheck', 'include', 'component-set', 'order', 'git-count-verify', 'git-count-bootstrap'):
    os.execv(sys.executable, [sys.executable, '-B', '-c', RUNNER, GUARD, MODE] + args)
done = subprocess.run([sys.executable, '-B', GUARD] + args, capture_output=True)
out, err = done.stdout, done.stderr
if MODE == 'error-name':
    err = err.replace(b'(OSError)', b'(IOError)')
if MODE == 'read-forbidden' and args and args[0] == 'verify' and done.returncode == 20:
    # 拒否するとき(読む前に止まるはずのとき)に、読んではならない先(FIFO・権限の無いファイル)を開く Rust。FIFO は開いて止まり、権限の無いファイルは別の理由(読めない)で止まる。
    import stat
    for folder, names, files in os.walk(os.getcwd()):
        for name in names + files:
            path = os.path.join(folder, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISFIFO(mode) or (stat.S_ISREG(mode) and mode & 0o444 == 0):
                try:
                    open(path, 'rb').read(1)
                except OSError as error:
                    sys.stderr.write('ERROR [environment-guard] 検査を完了できない (' + type(error).__name__ + ')\n')
                    sys.exit(20)
if args and args[0] == 'bootstrap' and done.returncode == 0:
    output = args[args.index('--output') + 1]
    if MODE == 'guard-copy':
        with open(output + '/environment-guard.py', 'ab') as handle:
            handle.write(b'\n')
    if MODE == 'tree-extra':
        open(output + '/extra.txt', 'w').close()
    if MODE == 'state-key':
        # 控えに鍵を 1 つ足す(照合は読み飛ばす)。受領の sha256 は変わるが、競合の流れの比較は受領の sha256 を除くので、控えの本文の比較だけが拾う
        value = json.load(open(output + '/environment.json'))
        value['zz'] = 1
        json.dump(value, open(output + '/environment.json', 'w'))
    if MODE == 'receipt-field':
        receipt = json.loads(out)
        receipt['guard_sha256'] = '1' * 64
        out = (json.dumps(receipt) + '\n').encode()
    if MODE == 'outside-write':
        open(os.getcwd() + '/stray.txt', 'w').close()   # 出力先の外の入力に、ファイルを足す
    if MODE == 'tree-mode':
        os.chmod(output, 0o750)
    if MODE == 'receipt-json':
        out = (json.dumps(json.loads(out), sort_keys=True) + '\n').encode()
    if MODE == 'receipt-sha':
        receipt = json.loads(out)
        receipt['sha256'] = '0' * 64
        out = (json.dumps(receipt) + '\n').encode()
    if MODE in ('cross', 'readonly'):
        with open(BASE + '/made.txt', 'a') as handle:
            handle.write(file_key(output + '/environment.json') + '\n')
if MODE == 'cross' and args and args[0] == 'verify':
    made = open(BASE + '/made.txt').read().split() if os.path.exists(BASE + '/made.txt') else []
    if file_key(args[args.index('--state') + 1]) not in made:
        sys.stderr.write('ERROR [environment-guard] この Rust が作っていない控え\n')
        sys.exit(20)
if MODE == 'readonly' and args and args[0] == 'verify' and done.returncode == 0:
    # この Rust が作った控えを照合するときだけ、入力の時刻を変える(旧版の控えを読む相互読取りでは変えない。読取りだけの前後確認だけが拾う)。
    made = open(BASE + '/made.txt').read().split() if os.path.exists(BASE + '/made.txt') else []
    state = args[args.index('--state') + 1]
    if file_key(state) in made:
        os.utime(json.load(open(state))['root'] + '/README.md')
if MODE == 'readonly-time' and args and args[0] == 'verify' and done.returncode == 0:
    os.utime(os.getcwd())   # 監視していない入力(流れの根のフォルダ自身)の時刻だけを変える
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(err)
sys.exit(done.returncode)
'''

# 旧版の側も同じ受領の細工をする旧 guard の包み(旧新の受領が同じで、控えの生バイトの SHA-256 とだけ合わない入力を作る)。
WRAP_OLD = r'''import json, subprocess, sys
done = subprocess.run([sys.executable, '-B', {guard!r}] + sys.argv[1:], capture_output=True)
out = done.stdout
if sys.argv[1] == 'bootstrap' and done.returncode == 0:
    receipt = json.loads(out)
    receipt['sha256'] = '0' * 64
    out = (json.dumps(receipt) + '\n').encode()
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(done.stderr)
sys.exit(done.returncode)
'''

# Rust が作った控えを読めない旧 guard の包み(旧版が Rust の控えを照合できない入力を作る。控えの本文は旧新で同じ)。
WRAP_OLD_REFUSES = r'''import os, subprocess, sys
BASE = {base!r}
def file_key(path):
    # 「この Rust が作った控え」の目印。inode 番号だけだと、消えたファイルの番号が別のファイルに再利用されて(負荷・順序で変わる)旧版の控えに当たるので、更新時刻(ns)も足す
    info = os.stat(path)
    return '%d:%d' % (info.st_ino, info.st_mtime_ns)
if sys.argv[1] == 'verify':
    made = open(BASE + '/made.txt').read().split() if os.path.exists(BASE + '/made.txt') else []
    if file_key(sys.argv[sys.argv.index('--state') + 1]) in made:
        sys.stderr.write('ERROR [environment-guard] この旧版が読めない控え\n')
        sys.exit(20)
done = subprocess.run([sys.executable, '-B', {guard!r}] + sys.argv[1:], capture_output=True)
sys.stdout.buffer.write(done.stdout)
sys.stderr.buffer.write(done.stderr)
sys.exit(done.returncode)
'''

PATCH_RUNNER = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("environment_guard_fake", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mode = sys.argv[2]
sys.argv = [sys.argv[1]] + sys.argv[3:]
command = sys.argv[1] if len(sys.argv) > 1 else ''
if mode == 'budget':
    module.MAX_ENTRIES += 1
elif mode == 'recheck':
    def verify(state, expected, current_inventory=None):
        value = module.load_verified(state, expected)
        if current_inventory is not None and module.plugin_inventory(module.read_regular(current_inventory)) != value['inventory']:
            raise module.Stop('有効 plugin の一覧が変わった')
        if module.snapshot(value['root'], value['specs'], value['inventory'], expected=value['entries']) != value['entries']:
            raise module.Stop('配布物または利用者環境が変わった')
        return value
    module.verify = verify
elif mode == 'order':
    def verify(state, expected, current_inventory=None):
        value = module.load_verified(state, expected)
        copied = {}
        module.tree(value['copy'], 'plugin', module.Budget(), copied)
        wanted = {k: v for k, v in value['entries'].items() if k == 'plugin' or k.startswith('plugin/')}
        if copied != wanted:
            raise module.Stop('信頼コピーが変わった')
        if module.snapshot(value['root'], value['specs'], value['inventory'], expected=value['entries']) != value['entries']:
            raise module.Stop('配布物または利用者環境が変わった')
        return value
    module.verify = verify
elif mode == 'include':
    module.includes = lambda path, raw, budget: []
elif mode == 'git-count-verify' and command == 'verify' or mode == 'git-count-bootstrap' and command == 'bootstrap':
    original = module.includes
    module.includes = lambda path, raw, budget: (original(path, raw, budget), original(path, raw, budget))[0]
elif mode == 'component-set' and command == 'verify':
    # 部品の集合から personal-agent を外した確認でも通してしまう(agents のフォルダが後から作られても見逃す)。verify 以降の差で、
    # Mac の経路の旧版の bootstrap でも効く。
    original = module.verify

    def verify(state, expected, current_inventory=None):
        try:
            return original(state, expected, current_inventory)
        except module.Stop:
            value = module.load_verified(state, expected)
            specs = dict(value['specs'])
            specs['components'] = [item for item in specs['components'] if item['kind'] != 'personal-agent']
            entries = {key: item for key, item in value['entries'].items() if not key.startswith('component:personal-agent:')}
            if module.snapshot(value['root'], specs, value['inventory'], expected=entries) != entries:
                raise
            return value
    module.verify = verify
raise SystemExit(module.main())
'''


FAKE_SIGINT = r"""#!{python}
import os, signal, subprocess, sys
MODE = {mode!r}
child = subprocess.Popen(['git', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def stop(number, frame):
    if MODE != 'leave-child':
        os.kill(child.pid, signal.SIGKILL)   # `child.wait()` は、主の流れが待っている最中に同じ鍵を取るので使わない
    sys.stderr.write('Traceback (most recent call last):\n  File "x", line 1, in <module>\nKeyboardInterrupt\n')
    if MODE == 'exit-130':
        sys.exit(130)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)


signal.signal(signal.SIGINT, stop)
child.wait()
"""


class CommandComparison(unittest.TestCase):
    """9 コマンドの旧新比較(`s2e_commands.py`)が、壊れた Rust を見つけること。偽の Rust は、旧 guard を包んで 1 つずつ壊した実行ファイルで、
    製品の example の外(テストの一時フォルダ)に作る。環境変数やテスト専用の組み立ての feature では作らない。
    壊し方ごとに、落ちた比較の文面を `assertRaisesRegex` で固定する(別の比較が拾っても通さない)。Rust の bootstrap を流す壊し方は、
    Mac の経路(旧版の bootstrap は patched・Rust の bootstrap は流さない)では意味を持たないので、Linux の経路だけで確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='s2e-cmd-test-')
        cls.base = Path(os.path.realpath(cls.folder.name))
        cls.reader = cls.base / 'old_reader.py'
        cls.reader.write_text(s2e_commands.OLD_READER)
        cls.wrapper = cls.base / 'wrap_old.py'
        cls.wrapper.write_text(WRAP_OLD.format(guard=str(s2e_commands.GUARD)))
        cls.work = cls.base / 'work'
        cls.work.mkdir()
        cls.fakes = {}
        cls.g = s2e_commands.gen()
        cls.flows = {case['name']: case for case in cls.g.cmd_cases()}
        cls.extra = {case['name']: case for _, _, case, _ in s2e_commands.extra_cases()}

    @classmethod
    def tearDownClass(cls):
        s2e_commands.remove_tree(cls.base)

    @property
    def rust_bootstrap(self):
        """Rust の bootstrap を流す経路か(Linux)。Mac の経路は旧版の patched の bootstrap で控えを作る。"""
        return not s2e_commands.MAC_PATH

    def fake(self, mode):
        if mode not in self.fakes:
            path = self.base / f'fake-{mode}'
            state = self.base / f'state-{mode}'
            state.mkdir()
            path.write_text(FAKE_COMMANDS.format(python=sys.executable, guard=str(s2e_commands.GUARD), mode=mode, reader=str(self.reader),
                                                 hostcheck=str(s2e_commands.HOST_CHECK), runner=PATCH_RUNNER, base=str(state)))
            path.chmod(0o755)
            self.fakes[mode] = path
        return self.fakes[mode]

    def run_case(self, mode, case, **kwargs):
        return s2e_commands.run_flow(case, self.fake(mode), self.work, **kwargs)

    def order_case(self):
        g, BS, CMD = self.g, self.g.BS, self.g.CMD
        both = {'ops_before': [{'op': 'write', 'path': 'plugin/README.md', 'body': 'HELLO'}, {'op': 'write', 'path': 'out/a/plugin/README.md', 'body': 'WORLD'}],
                'args': CMD('verify')}
        return {'name': 'order-both-changed', 'layout': g.cmd_base_layout(), 'runs': [{'args': BS('a')}, both], 'no_readers': True,
                'no_readers_reason': '順序の壊し方の検査用'}

    def test_control_passes_every_comparison_and_counts_them(self):
        for name in ('basic', 'include-chain', 'verify-copy-edit', 'verify-agents-dir-created', 'options-readers'):
            tally = self.run_case('control', self.flows[name])
            self.assertGreater(tally.runs, 0, name)
            self.assertGreater(tally.readonly, 0, name)
            self.assertGreater(tally.rust_runs, 0, name)
        tally = self.run_case('control', self.flows['basic'])
        self.assertGreater(tally.bootstrap_bytes, 0)
        self.assertGreater(tally.cross, 0)
        self.assertGreater(tally.readers, 0)
        self.run_case('control', self.order_case())

    # (壊し方, 流れ, 落ちるはずの比較の文面, Rust の bootstrap を流す壊し方か)
    def defects(self):
        f, x = self.flows, self.extra
        return [
            ('recheck', f['verify-copy-edit'], r'verify-copy-edit#\d+ verify: 旧新が一致しない', False),
            ('order', self.order_case(), r'order-both-changed#1 verify: 旧新が一致しない', False),
            ('include', f['include-chain'], r'include-chain#\d+ (bootstrap|verify): (旧新が一致しない|Rust が旧版の出力一式を照合できない)', False),
            ('component-set', f['verify-agents-dir-created'], r'verify-agents-dir-created#\d+ verify: (旧新が一致しない|Git の起動回数が違う)', False),
            ('git-count-verify', f['include-chain'], r'include-chain#\d+ verify: Git の起動回数が違う', False),
            ('git-count-bootstrap', f['include-chain'], r'include-chain#0 bootstrap: Git の起動回数が違う', True),
            ('json-type', f['options-readers'], r'options-readers reader-[a-z-]+ [a-z]+: 旧新が一致しない', False),
            ('readonly', f['basic'], r'basic#\d+ verify: 読取りだけのコマンドが入力を変えた', True),
            ('readonly-time', f['basic'], r'basic#0 bootstrap: .*照合したときに読取りだけのコマンドが入力を変えた', False),
            ('reader-touch', f['options-readers'], r'options-readers reader-[a-z-]+ [a-z]+: 読取りだけの reader が入力を変えた', False),
            ('outside-write', f['basic'], r'basic#0 bootstrap: bootstrap が出力先の外の入力を変えた', True),
            ('guard-copy', f['basic'], r'basic#0 bootstrap: 出力一式のバイト列が違う: environment-guard.py: 内容が違う', True),
            ('tree-extra', f['basic'], r'basic#0 bootstrap: 出力一式のバイト列が違う: extra.txt: 片方にしか無い', True),
            ('tree-mode', f['basic'], r'basic#0 bootstrap: 出力一式のバイト列が違う: \.: 種類か mode が違う', True),
            ('receipt-json', f['basic'], r'basic#0 bootstrap: 旧新が一致しない', True),
            ('cross', f['basic'], r'basic#0 bootstrap: Rust が旧版の控えを照合できない', True),
        ]

    def test_each_defect_is_rejected_by_the_comparison_it_belongs_to(self):
        for mode, case, pattern, needs_rust_bootstrap in self.defects():
            if needs_rust_bootstrap and not self.rust_bootstrap:
                continue   # Mac の経路は Rust の bootstrap を流さない(既定の監視先で失敗する)ので、bootstrap の出力の不具合は Linux の経路でだけ確かめる
            with self.subTest(defect=mode):
                with self.assertRaisesRegex(AssertionError, pattern):
                    self.run_case(mode, case)

    def test_the_receipt_sha256_check_is_the_only_one_that_sees_a_receipt_both_sides_got_wrong(self):
        if not self.rust_bootstrap:
            return   # Rust の bootstrap の受領を比べる入力(Linux の経路)
        wrapper = self.wrapper

        class Wrapped(s2e_commands.Flow):
            def setup(self):
                super().setup()
                self.old_script_override = wrapper
        with self.assertRaisesRegex(AssertionError, r'basic#0 bootstrap: 受領が控えの生バイトの SHA-256 と合わない'):
            Wrapped(self.flows['basic'], self.fake('receipt-sha'), self.work).go()

    def test_an_old_guard_that_cannot_read_the_rust_state_is_rejected(self):
        if not self.rust_bootstrap:
            return   # Rust の bootstrap が作った控えを旧版が読む相互読取り(Linux の経路)
        fake = self.fake('cross')
        wrapper = self.base / 'wrap_old_refuses.py'
        wrapper.write_text(WRAP_OLD_REFUSES.format(base=str(self.base / 'state-cross'), guard=str(s2e_commands.GUARD)))

        class Refusing(s2e_commands.Flow):
            def setup(self):
                super().setup()
                self.old_script_override = wrapper
        with self.assertRaisesRegex(AssertionError, r'basic#0 bootstrap: 旧版が Rust の控えを照合できない'):
            Refusing(self.flows['basic'], fake, self.work).go()

    def test_a_rust_that_reads_a_forbidden_target_is_rejected(self):
        # 読んではならない先を FIFO(開くと止まる)・権限の無いファイル(別の理由で止まる)にした流れで、本文を開く Rust は旧新が違う(止まる)。
        self.run_case('control', self.extra['x-preread-root-edit-0200'])
        for name in ('x-preread-root-edit-0200', 'x-preread-new-root-0200', 'x-preread-include-edit-0200', 'x-preread-absent-include-0200'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(AssertionError, name + r'#\d+ verify: 旧新が一致しない'):
                    self.run_case('read-forbidden', self.extra[name])
        for name in ('x-preread-root-edit-fifo', 'x-preread-new-root-fifo', 'x-preread-include-edit-fifo', 'x-preread-absent-include-fifo'):
            with self.subTest(name=name), mock.patch.object(s2e_commands, 'TOOL_TIMEOUT', 3):
                with self.assertRaisesRegex(AssertionError, name + r'#\d+ verify: 時間切れ'):
                    self.run_case('read-forbidden', self.extra[name])

    def test_the_budget_edge_just_after_the_limit_is_rejected(self):
        if not self.rust_bootstrap:
            return   # 上限の端は Linux だけで流す項目(Mac は Rust の bootstrap を流さない)。Mac では確かめない
        after = next(case for case in s2e_commands.boundary_item_cases(self.work) if case['name'] == 'x-limit-entries-after')
        with self.assertRaisesRegex(AssertionError, r'x-limit-entries-after#0 bootstrap: 旧新が一致しない'):
            self.run_case('budget', after)
        self.run_case('control', after)

    def test_timeout_and_unstarted_fixture_are_not_passes(self):
        with mock.patch.object(s2e_commands, 'TOOL_TIMEOUT', 2), self.assertRaisesRegex(AssertionError, r'時間切れ'):
            self.run_case('sleep', self.flows['basic'])
        with self.assertRaises(OSError):
            s2e_commands.run_flow(self.flows['basic'], self.base / 'nonexistent-fixture', self.work)

    def test_a_limit_that_cannot_be_set_is_a_failure_not_a_silent_pass(self):
        with mock.patch.object(s2e_commands.resource, 'setrlimit', side_effect=OSError(1, 'denied')):
            with self.assertRaisesRegex(AssertionError, r'起動前の上限の設定に失敗した'):
                s2e_commands.run_tool(['/bin/true'], env={}, cwd='/', fsize=100)

    def test_every_item_must_start_rust_at_least_once(self):
        empty = s2e_commands.Item('flow', 'no-rust', s2e_commands.ALL, lambda tmp: s2e_commands.Tally(runs=3))
        with mock.patch.object(s2e_commands, 'items', return_value=[empty]):
            with self.assertRaisesRegex(AssertionError, r'flow/no-rust: Rust を起動した比較が 0 件'):
                s2e_commands.run_items('/x', self.work, log=lambda text: None)
        # Rust の起動数は、控えが無い照合(失敗した bootstrap の後)を数えない。
        self.assertTrue(s2e_commands.Flow.is_meaningful_rust_run(['bootstrap', '--root', '/x']))
        self.assertFalse(s2e_commands.Flow.is_meaningful_rust_run(['verify', '--state', str(self.base / 'no-such-state'), '--expect-sha256', '0']))
        self.assertTrue(s2e_commands.Flow.is_meaningful_rust_run(['verify', '--state', str(self.reader), '--expect-sha256', '0']))

    def test_zero_totals_and_missing_names_are_rejected(self):
        m = get_subject()
        tally = s2e_commands.Tally(runs=1, bootstrap_bytes=1, cross=1, readers=1, readonly=1, approved=1)
        self.assertEqual(m.check_command_totals(tally, {('flow', 'all'): [3, 1.0]}), 3)
        for field in ('runs', 'bootstrap_bytes', 'cross', 'readers', 'readonly') + (('approved',) if not s2e_commands.MAC_PATH else ()):
            with self.assertRaises(AssertionError, msg=field):
                m.check_command_totals(dataclasses.replace(tally, **{field: 0}), {('flow', 'all'): [3, 1.0]})
        with self.assertRaises(AssertionError):
            m.check_command_totals(tally, {})
        saved = list(m.TEST_MAPPING)
        try:
            m.TEST_MAPPING[0] = (saved[0][0], saved[0][1], '比較[no-such-case]', '')
            with self.assertRaises(AssertionError):
                m.check_mapping_references()
            m.TEST_MAPPING[0] = (saved[0][0], saved[0][1], 'budget.rs::no_such_test', '')
            with self.assertRaises(AssertionError):
                m.check_mapping_references()
        finally:
            m.TEST_MAPPING[:] = saved
        self.assertGreater(m.check_mapping_references(), 100)

    def test_item_counts_are_fixed_per_machine_and_linux_only_items_have_reasons(self):
        m = get_subject()
        items = s2e_commands.items('/nonexistent-fixture')
        machine = 'mac' if s2e_commands.MAC_PATH else 'linux'
        per_group = {}
        skipped = []
        for item in items:
            if item.mark == s2e_commands.LINUX_ONLY and s2e_commands.MAC_PATH:
                skipped.append((item.name, item.reason))
                continue
            per_group.setdefault((item.group, item.mark), [0, 0.0])[0] += 1
        self.assertEqual(m.check_command_items(per_group, skipped, items), m.EXPECTED_ITEMS[machine]['total'])
        for change in (lambda: per_group.__setitem__(('flow', 'all'), [per_group[('flow', 'all')][0] + 1, 0.0]),
                       lambda: per_group.pop(('mac', 'all'))):
            with self.subTest():
                changed = {key: list(value) for key, value in per_group.items()}
                saved = dict(per_group)
                try:
                    change()
                    with self.assertRaises(AssertionError):
                        m.check_command_items(per_group, skipped, items)
                finally:
                    per_group.clear()
                    per_group.update(saved)
        with self.assertRaises(AssertionError):   # Linux だけの印の項目が増えたか減った
            moved = [dataclasses.replace(item, mark=s2e_commands.LINUX_ONLY) if item.name == 'x-sigint-verify' else item for item in items]
            m.check_command_items(per_group, skipped, moved)
        with self.assertRaises(AssertionError):   # 理由の無い Linux だけの印
            unreasoned = [dataclasses.replace(item, reason='') if item.mark == s2e_commands.LINUX_ONLY else item for item in items]
            m.check_command_items(per_group, skipped, unreasoned)

    def test_mapping_rows_cut_short_are_rejected(self):
        m = get_subject()
        saved = list(m.TEST_MAPPING)
        try:
            for cut in ('プ', '比較', '', 'x'):
                m.TEST_MAPPING[0] = (saved[0][0], saved[0][1], cut, '')
                with self.assertRaises(AssertionError, msg=cut):
                    m.check_mapping()
            m.TEST_MAPPING[0] = (saved[0][0], saved[0][1], '比較[basic]', '')
            m.check_mapping()
        finally:
            m.TEST_MAPPING[:] = saved
        for name, category, target, _ in m.TEST_MAPPING:   # 全行が行き先を示す語を持つ(切れた行が 1 つも無い)
            self.assertTrue(category == m.O or any(word in target for word in m.TARGET_WORDS), name)

    def test_snapshot_and_byte_comparison_see_content_mode_link_and_time_changes(self):
        root = self.base / 'snap'
        root.mkdir()
        (root / 'f').write_text('a')
        (root / 'l').symlink_to('f')
        before = s2e_commands.tree_snapshot(root)
        self.assertEqual(before, s2e_commands.tree_snapshot(root))
        for change in (lambda: (root / 'f').write_text('b'), lambda: os.utime(root / 'f', (1, 1)), lambda: os.chmod(root / 'f', 0o600),
                       lambda: ((root / 'l').unlink(), (root / 'l').symlink_to('g')), lambda: (root / 'n').write_text('x'), lambda: os.utime(root, (2, 2))):
            change()
            self.assertNotEqual(before, s2e_commands.tree_snapshot(root))
            before = s2e_commands.tree_snapshot(root)
        left, right = self.base / 'out-a', self.base / 'out-b'
        for side in (left, right):
            side.mkdir()
            (side / 'x').write_bytes(b'same')
        self.assertIsNone(s2e_commands.first_tree_difference(s2e_commands.tree_bytes(left), s2e_commands.tree_bytes(right)))
        (right / 'x').write_bytes(b'diff')
        self.assertIn('x', s2e_commands.first_tree_difference(s2e_commands.tree_bytes(left), s2e_commands.tree_bytes(right)))
        (right / 'x').write_bytes(b'same')
        os.chmod(right / 'x', 0o600)
        self.assertIn('mode', s2e_commands.first_tree_difference(s2e_commands.tree_bytes(left), s2e_commands.tree_bytes(right)))

    def test_a_one_bit_flip_changes_only_that_bit(self):
        flow = s2e_commands.Flow({'name': 'flip', 'layout': [], 'runs': []}, self.fake('control'), self.work)
        try:
            flow.root.mkdir()
            (flow.root / 'f').write_bytes(b'\x10abc')
            os.chmod(flow.root / 'f', 0o755)
            flow.apply_ops([{'op': 'flip', 'path': 'f'}, {'op': 'flip', 'path': 'f', 'offset': 2}])
            self.assertEqual((flow.root / 'f').read_bytes(), b'\x11a\x63c')
            self.assertEqual(os.stat(flow.root / 'f').st_mode & 0o777, 0o755)
        finally:
            flow.close()

    def test_the_state_digest_hides_only_device_inode_and_times(self):
        flow = s2e_commands.Flow(self.flows['basic'], self.fake('control'), self.work, tool='old')
        try:
            flow.setup()
            # Mac の経路は旧版の bootstrap を patched で流す(patched なしは既定の監視先 /etc のリンクで失敗する)
            result = flow.run_old(s2e_commands._bootstrap_args(flow.root, 'a'), patched=flow.mode == 'patched')
            self.assertEqual(result.returncode, 0, result.stderr)
            raw = (flow.root / 'out/a/environment.json').read_bytes()
            value = json.loads(raw)
            digest = s2e_commands.state_digest(raw, flow.root)
            masked = self.g.MASK_POSITIONS
            kind, key = next((entry[0], name) for name, entry in value['entries'].items()
                             if isinstance(entry, list) and entry and entry[0] in masked)
            changed = json.loads(raw)
            changed['entries'][key][masked[kind][0]] = 1234567
            self.assertEqual(s2e_commands.state_digest(json.dumps(changed).encode(), flow.root), digest)
            for edit in (lambda v: v.update(version=4), lambda v: v['entries'].pop(key),
                         lambda v: v['entries'][key].__setitem__(0, 'other-kind'), lambda v: v.update(extra=1)):
                changed = json.loads(raw)
                edit(changed)
                self.assertNotEqual(s2e_commands.state_digest(json.dumps(changed).encode(), flow.root), digest)
        finally:
            flow.close()

    def test_the_fake_git_race_hook_changes_the_input_only_at_the_named_call(self):
        case = {'name': 'race-hook', 'layout': self.g.cmd_base_layout(), 'no_readers': True,
                'runs': [{'args': self.g.BS('a')}, {'args': self.g.CMD('verify'), 'race': {'call': 99, 'ops': [{'op': 'write', 'path': 'plugin/README.md', 'body': 'x'}]}}]}
        flow = s2e_commands.Flow(case, self.fake('control'), self.work, tool='old', mode='patched')
        flow.go()
        self.assertEqual([record['code'] for record in flow.log], [0, 0])   # 99 回目の Git の起動は来ない。差し込まれない
        case['runs'][1]['race']['call'] = 1
        flow = s2e_commands.Flow(case, self.fake('control'), self.work, tool='old', mode='patched')
        flow.go()
        self.assertEqual(flow.log[1]['code'], 0)
        flow = s2e_commands.Flow(dict(case, runs=case['runs'] + [{'args': self.g.CMD('verify')}]), self.fake('control'), self.work, tool='old', mode='patched')
        flow.go()
        self.assertEqual(flow.log[2]['code'], 20)   # 差し込まれた変更を、次の照合が見つける

    def test_solo_comparison_sees_the_output_folder_the_state_body_and_the_receipt_of_a_bootstrap(self):
        if not self.rust_bootstrap:
            return   # 競合の流れの bootstrap を Rust で流すのは Linux の経路(Mac の経路は旧版の patched で作る)
        case = {'name': 'solo-bootstrap', 'layout': self.g.cmd_base_layout(), 'no_readers': True,
                'runs': [{'args': self.g.BS('a')}, {'args': self.g.CMD('verify')}]}
        self.assertGreater(s2e_commands.run_solo_pair(case, self.fake('control'), self.work, mode='cli').rust_runs, 0)
        for defect in ('tree-extra', 'state-key', 'receipt-field'):   # 出力フォルダの構成・控えの本文・受領の別の欄。どれも別の比較が拾わない
            with self.subTest(defect=defect):
                with self.assertRaisesRegex(AssertionError, r'solo-bootstrap#0 bootstrap: 旧新が一致しない'):
                    s2e_commands.run_solo_pair(case, self.fake(defect), self.work, mode='cli')

    def test_a_reader_that_differs_on_an_input_stopped_by_the_host_check_preprocessing_is_rejected(self):
        case = self.extra['x-forged-state']
        tally = self.run_case('control', case)
        self.assertGreater(tally.readers, 0)
        self.assertFalse([text for text in tally.excluded if 'reader を比べない(host-check の前処理が止め' in text])
        for defect, pattern in (('reader-pre-type', r'x-forged-state reader-[a-z-]+ [a-z]+: 旧新が一致しない'),
                                ('reader-pre-msg', r'x-forged-state reader-[a-z-]+ [a-z]+: 旧新が一致しない')):
            with self.subTest(defect=defect):
                with self.assertRaisesRegex(AssertionError, pattern):
                    self.run_case(defect, case)

    def test_a_forbidden_target_flow_must_stop_the_old_guard_for_the_fixed_reason(self):
        names = [name for name in self.extra if name.startswith('x-preread-') and 'nested' not in name]
        self.assertEqual(len(names), 8)
        for name in names:
            self.assertEqual(self.extra[name]['expect_old']['code'], 20, name)
            self.assertTrue(self.extra[name]['expect_old']['stderr'].startswith(b'ERROR [environment-guard] '), name)
        # 配置が変わって旧版が別の理由で止まるようになったら(期待を変えて模擬する)、旧新が一致していても合格にしない。
        case = dict(self.extra['x-preread-root-edit-0200'], expect_old=dict(self.extra['x-preread-root-edit-0200']['expect_old'],
                    stderr=s2e_commands.PREREAD_NEW_ROOT))
        with self.assertRaisesRegex(AssertionError, r'x-preread-root-edit-0200#2 verify: 旧版が決まった理由で止まらない'):
            self.run_case('control', case)
        case = dict(self.extra['x-preread-new-root-fifo'], expect_old=dict(self.extra['x-preread-new-root-fifo']['expect_old'], code=0))
        with self.assertRaisesRegex(AssertionError, r'x-preread-new-root-fifo#2 verify: 旧版が決まった理由で止まらない'):
            self.run_case('control', case)

    def test_a_flow_that_skips_the_step_of_its_fixed_old_expectation_is_rejected(self):
        # expect_old の手順の番号が、実際に流す手順に当たらない(手順を飛ばした・番号がずれた)流れは、旧新が一致していても落ちる。
        base = self.extra['x-preread-root-edit-0200']
        shifted = dict(base, expect_old=dict(base['expect_old'], index=len(base['runs']) + 3))
        with self.assertRaisesRegex(AssertionError, r'x-preread-root-edit-0200: 固定期待\(expect_old\)の手順 \d+ を旧版の結果と照らしていない'):
            self.run_case('control', shifted)
        # 当たる番号なら通る(統制)。
        self.run_case('control', base)

    def test_the_tilde_flows_record_that_nss_only_users_are_unconfirmed(self):
        tally = self.run_case('control', self.extra['x-tilde-forms'])
        self.assertTrue(any('x-tilde-forms' in text and 'NSS' in text and '未確認' in text for text in tally.excluded), tally.excluded)

    def test_the_simulated_mac_runs_an_unpatched_old_bootstrap_against_an_etc_link(self):
        if not s2e_commands.SIMULATED_MAC_ONLY:
            return   # Mac の経路の模擬(S2E_SIMULATE_MAC=1)のときだけ確かめる(check_s2e.py は省略を不合格にするので skip しない)
        flow = s2e_commands.Flow(self.flows['basic'], self.fake('control'), self.work, tool='old', mode='cli')
        try:
            flow.setup()
            args = s2e_commands._bootstrap_args(flow.root, 'a')
            result = flow.run_old(args)
            self.assertNotEqual(result.returncode, 0)   # Mac と同じく、既定の監視先が /etc へのリンクなので失敗する
            self.assertEqual(flow.run_old(args, patched=True).returncode, 0)
            flow.mac_link = False
            s2e_commands.remove_tree(flow.root / 'out/a')
            self.assertEqual(flow.run_old(args).returncode, 0)   # run_mac_default だけは、実際の既定の監視先の結果を見る
        finally:
            flow.close()

    def test_a_simulated_mac_pass_is_not_a_linux_pass(self):
        m = get_subject()
        for simulated, code, line in ((True, 3, m.SIMULATION_PASS), (False, 0, 'S2E: 合格')):
            with self.subTest(simulated=simulated):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(m.finish(simulated), code)
                self.assertEqual(out.getvalue(), line + '\n')
        self.assertNotEqual(m.SIMULATION_PASS, 'S2E: 合格')
        self.assertIn('Linux の合格ではありません', m.SIMULATION_NOTICE)
        with mock.patch.object(s2e_commands, 'SIMULATE_MAC', True), mock.patch.object(s2e_commands, 'IS_MAC', False):
            self.assertTrue(m.is_simulation())
        with mock.patch.object(s2e_commands, 'SIMULATE_MAC', False):
            self.assertFalse(m.is_simulation())

    def test_solo_races_compare_two_separate_runs_and_reject_a_difference(self):
        ops = [{'op': 'write', 'path': 'home/extra.cfg', 'body': '[user]\n\tname = swapped\n'}]
        case = {'name': 'solo-race', 'layout': self.g.cmd_base_layout(), 'no_readers': True,
                'runs': [{'args': self.g.BS('a')}, {'args': self.g.CMD('verify'), 'race': {'call': 1, 'ops': ops}}, {'args': self.g.CMD('verify')}]}
        # Mac の経路と同じ流し方(競合の無い bootstrap の控えは旧版の patched で作り、競合の回だけを旧新で流す)で、Linux でも確かめる。
        for mode in ('patched',) + (('cli',) if self.rust_bootstrap else ()):
            with self.subTest(mode=mode):
                self.assertGreater(s2e_commands.run_solo_pair(case, self.fake('control'), self.work, mode=mode).rust_runs, 0)
                for defect in ('include', 'git-count-verify'):
                    # Mac の経路(patched)の bootstrap は旧版なので、最初の差は競合の回(verify)。Linux の bootstrap(cli)は bootstrap の回から違いうる。
                    with self.assertRaisesRegex(AssertionError, r'solo-race#\d+ (bootstrap|verify): 旧新が一致しない' if mode == 'cli' else r'solo-race#1 verify: 旧新が一致しない'):
                        s2e_commands.run_solo_pair(case, self.fake(defect), self.work, mode=mode)

    def test_u2_expectations_reject_an_old_like_rust(self):
        # U2 の旧新それぞれの固定期待は Linux だけの項目(Rust の bootstrap が要る。Mac の経路は旧版の bootstrap が既定の監視先で失敗する)。
        if not self.rust_bootstrap:
            return
        # 旧版と同じ振る舞いの偽の Rust(配布物に guard が無くても成功する)は、U2 の固定期待で落ちる。
        with self.assertRaisesRegex(AssertionError, r'U2'):
            s2e_commands.run_u2(self.fake('control'), self.work)

    def test_u1_expectations_reject_a_strict_rust_when_the_locale_exists(self):
        probe = subprocess.run([sys.executable, '-B', '-c', 'import sys; print(sys.stdout.errors)'], env=dict(os.environ, LC_ALL='en_US.UTF-8'),
                               capture_output=True)
        if probe.stdout.strip() == b'strict':
            with self.assertRaises(AssertionError):
                s2e_commands.run_u1(self.fake('control'), self.work)
        else:
            tally = s2e_commands.run_u1(self.fake('control'), self.work)
            self.assertTrue(any('en_US.UTF-8' in text for text in tally.excluded))

    def test_the_inherited_nofile_is_recorded_and_never_set_by_the_runner(self):
        self.assertEqual(s2e_commands.inherited_nofile(), resource.getrlimit(resource.RLIMIT_NOFILE))
        probe = [sys.executable, '-B', '-c', 'import resource; print(*resource.getrlimit(resource.RLIMIT_NOFILE))']
        result = s2e_commands.run_tool(probe, env=dict(os.environ), cwd='/')
        self.assertEqual(result.stdout.decode().split(), [str(v) for v in resource.getrlimit(resource.RLIMIT_NOFILE)])

    def sigint_fake(self, mode):
        path = self.base / f'fake-sigint-{mode}'
        path.write_text(FAKE_SIGINT.format(python=sys.executable, mode=mode))
        path.chmod(0o755)
        return path

    def test_sigint_comparison_sees_the_exit_status_the_last_line_and_a_child_left_behind(self):
        # 偽の Rust: 'good' は旧版と同じ(子を止め、最後の行 KeyboardInterrupt で、信号 2 で終わる)。
        # 'exit-130' は終了値 130(信号で終わらない)、'leave-child' は止めた git の子を残す。
        for command in ('bootstrap', 'verify'):
            if command == 'bootstrap' and not self.rust_bootstrap:
                continue   # 中断された bootstrap は Rust の bootstrap が要る(Linux だけの項目)
            kwargs = {'mode': 'patched'} if command == 'verify' else {}
            with self.subTest(command=command, fake='good'):
                self.assertGreater(s2e_commands.run_sigint(command, self.sigint_fake('good'), self.work, **kwargs).runs, 0)
            for fake in ('exit-130', 'leave-child'):
                with self.subTest(command=command, fake=fake):
                    with self.assertRaisesRegex(AssertionError, r'sigint-' + command + r': 旧新が一致しない'):
                        s2e_commands.run_sigint(command, self.sigint_fake(fake), self.work, **kwargs)

    def test_the_fsize_comparison_rejects_a_different_failure(self):
        if not self.rust_bootstrap:
            return   # 書込みの失敗は Rust の bootstrap で比べる(Linux の経路)
        self.assertGreater(s2e_commands.run_fsize(self.fake('control'), self.work).runs, 0)
        with self.assertRaisesRegex(AssertionError, r'fsize 1: 旧新が一致しない'):
            s2e_commands.run_fsize(self.fake('error-name'), self.work)

    def test_each_item_has_a_mark_and_linux_only_items_are_listed(self):
        items = s2e_commands.items(self.base / 'unused')
        self.assertEqual(len({item.name for item in items}), len(items), '項目の名前が重複している')
        self.assertTrue({item.mark for item in items} <= {s2e_commands.ALL, s2e_commands.LINUX_ONLY})
        names = {item.name for item in items}
        self.assertTrue({'default-watch-bootstrap', 'patched-bootstrap-sample'} <= names)
        marks = {item.name: item.mark for item in items}
        self.assertEqual(marks['default-watch-bootstrap'], s2e_commands.ALL)     # Mac の必須の 2 項目
        self.assertEqual(marks['patched-bootstrap-sample'], s2e_commands.ALL)
        self.assertEqual(marks['u2-guard-origin'], s2e_commands.LINUX_ONLY)
        # bootstrap しか流さない流れは Linux だけ(Mac の経路では Rust を 1 回も起動しない)。読取りのコマンドがある流れは全機種。
        self.assertEqual(marks['x-special-settings-fifo'], s2e_commands.LINUX_ONLY)
        self.assertEqual(marks['x-preread-root-edit-fifo'], s2e_commands.ALL)
        self.assertEqual(marks['x-race-verify-settings-edit'], s2e_commands.ALL)
        self.assertEqual(marks['x-race-bootstrap-settings-edit'], s2e_commands.LINUX_ONLY)
        with mock.patch.object(s2e_commands, 'MAC_PATH', True):
            tally, per_group, skipped, _ = s2e_commands.run_items(self.base / 'unused', self.work, only=['x-limit', 'u2-guard'], log=lambda text: None)
        self.assertEqual(per_group, {})
        self.assertTrue(any(name.startswith('x-limit') for name, _ in skipped) and 'u2-guard-origin' in [name for name, _ in skipped])
        self.assertTrue(all('Mac では未確認' in reason or 'Mac' in reason for _, reason in skipped))
        self.assertEqual(tally.runs, 0)


# ---------------------------------------------------------------- read・exec・hook・verify-pair(2 段目 b-2b)

FAKE_COPY = r"""#!{python}
import os, re, signal, subprocess, sys, time
GUARD, MODE, RUNNER = {guard!r}, {mode!r}, {runner!r}
PY = sys.executable
args = sys.argv[1:]
cmd = args[0] if args else ''
if cmd == 'verify-pair' and '--state' in args:
    # verify-pair は、控えの隣の guard のコピーから起動する(旧版は自分の `__file__` を照らし直す)
    copy = os.path.join(os.path.dirname(args[args.index('--state') + 1]), 'environment-guard.py')
    if os.path.isfile(copy):
        GUARD = copy
R4P = 'ERROR [environment-guard] hook は R4P まで旧版の判定器で実行する\n'.encode()


def direct():
    if MODE == 'u4-full':
        os.execv(PY, [PY, '-B', GUARD] + args)
    os.execv(PY, [PY, '-B', '-c', RUNNER, GUARD, 'u4'] + args)   # 旧 guard の中で、捕まえない例外の traceback の途中の行を出さない(U4)


if MODE == 'direct':
    direct()
if MODE == 'plus-one':
    code = subprocess.run([PY, '-B', GUARD] + args).returncode
    sys.exit(code + 1 if code > 0 else code)
if MODE == 'sigint-130' and cmd in ('exec', 'verify-pair'):
    child, early = None, []

    def handler(number, frame):
        if child is None:
            early.append(1)
            return
        child.send_signal(signal.SIGINT)
        time.sleep(1.5)   # `child.wait()` は、主の流れが待っている最中に同じ鍵を取るので使わない
        sys.exit(130)
    signal.signal(signal.SIGINT, handler)   # 子を起動する前に入れる(合図の観測より先に SIGINT が届いても、既定の動きで終わらない)
    child = subprocess.Popen([PY, '-B', GUARD] + args)
    if early:
        handler(2, None)   # 代入の前に届いていたとき(負荷)は、ここで子へ渡す
    sys.exit(child.wait())
if MODE == 'sigint-leave' and cmd in ('exec', 'verify-pair'):
    # 中断されたときに、最後の行 KeyboardInterrupt を出し、信号 2 で終わるが、守るべき子(exec の子)を止めずに残す
    def handler(number, frame):
        sys.stderr.write('KeyboardInterrupt\n')
        sys.stderr.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
    signal.signal(signal.SIGINT, handler)   # 子を起動する前に入れる(負荷で合図の観測の方が先に進んでも、SIGINT を取りこぼさない)
    child = subprocess.Popen([PY, '-B', GUARD] + args)
    sys.exit(child.wait())
if MODE in ('sigint-leave-group', 'sigint-hold-pipes') and cmd == 'exec':
    # 別のプロセスグループに物を残す Rust。sigint-leave-group: 守るべき子(guard とその子)を別のグループで起動し、中断されても止めない。
    # sigint-hold-pipes: 子は止めるが、標準出力・標準エラーを持つ別のグループの物(sleep)を残す(その pid は {holder} に書く)。
    def handler(number, frame):
        sys.stderr.write('KeyboardInterrupt\n')
        sys.stderr.flush()
        if MODE == 'sigint-hold-pipes':
            child.kill()
            time.sleep(0.5)   # `child.wait()` は、主の流れが待っている最中に同じ鍵を取るので使わない
            holder = subprocess.Popen(['sleep', '300'], start_new_session=True)
            with open({holder!r}, 'w') as handle:
                handle.write(str(holder.pid))
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
    signal.signal(signal.SIGINT, handler)
    child = subprocess.Popen([PY, '-B', GUARD] + args, start_new_session=MODE == 'sigint-leave-group')
    sys.exit(child.wait())
if MODE == 'sigint-ignore' and cmd == 'verify-pair':
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # 中断で終わらない。`verified` を出す前に入れる(出した後に入れると、負荷で SIGINT が先に届いて終わってしまう)
    sys.stdout.write('verified\n')
    sys.stdout.flush()
    time.sleep(120)
if cmd == 'verify-pair' and MODE == 'pre-stderr':
    done = subprocess.run([PY, '-B', GUARD] + args, capture_output=True)
    out, err, code = done.stdout, done.stderr, done.returncode
    if code == 20 and b'verified' not in out:   # 1 回目の照合の前で止まる(引数の検査)ときだけ、標準エラーを変える
        err = err.replace(b'ERROR', b'ERRoR')
    sys.stdout.buffer.write(out)
    sys.stderr.buffer.write(err)
    sys.exit(code)
if cmd == 'verify-pair':
    if MODE.startswith('pair-'):
        os.execv(PY, [PY, '-B', '-c', RUNNER, GUARD, MODE] + args)
    direct()
if MODE == 'exec-env' and cmd == 'exec':
    os.environ['S2E_EXTRA'] = '1'
if MODE == 'no-lcctype' and cmd == 'exec':
    # PEP 538 の LC_CTYPE の足しを再現しない Rust(の代わり)。この偽の Rust を包む Python 自身が起動時に足した LC_CTYPE を環境から消し、
    # 旧 guard は PYTHONCOERCECLOCALE=0 で起動して足させない。PYTHONCOERCECLOCALE 自体は旧 guard の中で main の前に外す(子の環境に残さない)。
    # 旧 guard の子の環境は、LC_CTYPE の有無だけが旧版(旧 guard を包みなしで起動した値)と違う。
    env = dict(os.environ)
    env.pop('LC_CTYPE', None)
    env['PYTHONCOERCECLOCALE'] = '0'
    os.execve(PY, [PY, '-B', '-c', RUNNER, GUARD, 'drop-coerce'] + args, env)
done = subprocess.run([PY, '-B', GUARD] + args, capture_output=True)
out, err, code = done.stdout, done.stderr, done.returncode
if MODE == 'read-byte' and cmd == 'read':
    out = out[:-1]
if MODE == 'exec-code' and cmd == 'exec' and code > 0:
    code += 1
if MODE == 'copy-touch' and cmd == 'read':
    os.utime(os.getcwd())
if MODE == 'rewrite-prev-out' and cmd == 'bootstrap':
    current = args[args.index('--output') + 1]
    for name in os.listdir('out'):   # 今回の出力先のほかの出力一式(前の bootstrap の出力)を書き換える
        path = os.path.abspath(os.path.join('out', name))
        if path != current and os.path.exists(os.path.join(path, 'environment.json')):
            with open(os.path.join(path, 'environment.json'), 'ab') as handle:
                handle.write(b' ')
if cmd == 'hook' and code == 7 and MODE != 'u3-old':
    out, err, code = b'', R4P, 20   # U3: 判定器を実行せず、確認の後に 20
if MODE != 'u4-full' and b'Traceback (most recent call last):' in err:
    err = re.sub(rb'\n  [^\n]*', b'', err)   # U4: traceback の途中の行を出さない
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(err)
sys.exit(code)
"""

PAIR_RUNNER = r"""
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("environment_guard_fake", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
mode = sys.argv[2]
sys.argv = [sys.argv[1]] + sys.argv[3:]
if mode == 'drop-coerce':
    os.environ.pop('PYTHONCOERCECLOCALE', None)
if mode == 'pair-no-guard':
    original = module.run_pair

    def run_pair(a):
        module.sha = lambda raw: a.guard_sha256   # 本体の照らし直しを常に通す
        return original(a)
    module.run_pair = run_pair
elif mode == 'pair-no-inventory':
    original = module.verify
    module.verify = lambda state, expected, current_inventory=None: original(state, expected)   # 2 回目の照合が一覧を比べない
elif mode == 'pair-write':
    main = module.main

    def wrapped():
        code = main()
        open('stray.txt', 'w').close()   # 読取りだけのコマンドが入力へファイルを足す
        return code
    module.main = wrapped
if mode == 'u4':
    try:
        raise SystemExit(module.main())
    except Exception as caught:
        sys.stderr.write('Traceback (most recent call last):\n' + type(caught).__name__ + ': ' + str(caught) + '\n')
        sys.exit(1)
raise SystemExit(module.main())
"""


class CopyCommandComparison(unittest.TestCase):
    """read・exec・hook・verify-pair の旧新比較が、壊れた Rust を見つけること。偽の Rust は旧 guard を包んで 1 つずつ壊した実行ファイル。
    壊し方ごとに、落ちた比較の文面を `assertRaisesRegex` で固定する(solo = 入力が途中で変わる流れ・pre = 1 回目の照合の前に止まる流れの経路を含む)。
    統制(壊していない偽の Rust)は、U3(旧版は判定器を実行し Rust は確認の後に 20)と U4(traceback の途中の行を出さない)を旧版から作る。"""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix='s2e-copy-test-')
        cls.base = Path(os.path.realpath(cls.folder.name))
        cls.work = cls.base / 'work'
        cls.work.mkdir()
        cls.fakes = {}
        cls.cases = {case['name']: (kind, case) for kind, case in s2e_commands.copy_flow_cases()}

    @classmethod
    def tearDownClass(cls):
        s2e_commands.remove_tree(cls.base)

    def fake(self, mode):
        if mode not in self.fakes:
            path = self.base / f'fake-{mode}'
            path.write_text(FAKE_COPY.format(python=sys.executable, guard=str(s2e_commands.GUARD), mode=mode, runner=PAIR_RUNNER, holder=str(self.base / 'holder-pid')))
            path.chmod(0o755)
            self.fakes[mode] = path
        return self.fakes[mode]

    def small(self, name, keep, **changes):
        """流れの最初の手順(bootstrap)と、`keep` の手順だけを残した流れ。手順の番号が変わるので、旧版の固定期待の記録は外す。"""
        kind, case = self.cases[name]
        runs = [case['runs'][0]] + [case['runs'][index] for index in keep]
        return kind, {**case, 'runs': runs, 'recorded': None, **changes}

    def run_item(self, mode, name):
        kind, case = self.cases[name]
        return s2e_commands.run_copy_flow(case, kind, self.fake(mode), self.work)

    def run_small(self, mode, name, keep, **changes):
        kind, case = self.small(name, keep, **changes)
        return s2e_commands.run_copy_flow(case, kind, self.fake(mode), self.work)

    def steps(self, name, wanted):
        return [index for index, run in enumerate(self.cases[name][1]['runs']) if wanted(run)]

    def test_control_passes_every_kind_of_comparison_and_counts_them(self):
        for name in ('cp-read', 'cp-refusal-edit-copy', 'cp-hook', 'cp-pair-after-edit-guard-copy', 'cp-pair-after-edit-copy-and-restore'):
            with self.subTest(name=name):
                tally = self.run_item('control', name)
                self.assertGreater(tally.runs, 0)
                self.assertGreater(tally.readonly if self.cases[name][0] == 'both' else 1, 0)   # solo の流れは、入力が途中で変わるので前後確認を持たない
                self.assertGreater(tally.rust_runs, 0)
        hook = self.run_item('control', 'cp-hook')
        self.assertGreaterEqual(hook.approved, 2)   # U3 は通常の一致に数えず、認めた差に数える
        self.assertTrue(any('hook-after-verify' in text for text in hook.excluded))
        self.run_small('control', 'cp-exec', [1, 2])
        # 捕まえない例外(U4): 最後の行・因果の区切りは旧版どおり、途中の行だけ除く。
        uncaught = self.steps('cp-pair', lambda run: run.get('uncaught'))[:1]
        tally = self.run_small('control', 'cp-pair', uncaught)
        self.assertGreaterEqual(tally.approved, 1)

    def test_each_defect_is_rejected_by_the_comparison_it_belongs_to(self):
        exec_steps = self.steps('cp-exec', lambda run: run['args'][0] == 'exec')[:3]
        uncaught = self.steps('cp-pair', lambda run: run.get('uncaught'))[:1]
        inventory = self.steps('cp-pair', lambda run: any('inv-new.json' in str(arg) for arg in run['args']))[:1]
        bash_steps = self.steps('cp-exec', lambda run: 'scripts/probe.sh' in run['args'])[:1]
        defects = [
            ('read-byte', lambda mode: self.run_item(mode, 'cp-read'), r'cp-read#\d+ read: 旧新が一致しない'),
            # bash の子の環境の全体(余計な変数を足す Rust・LC_CTYPE を足さない Rust)
            ('exec-env', lambda mode: self.run_small(mode, 'cp-exec', bash_steps), r'cp-exec#\d+ exec: 子が書いた観測用のファイルが旧新で違う'),
            ('copy-touch', lambda mode: self.run_item(mode, 'cp-read'), r'cp-read#\d+ read: 読取りだけのコマンドが入力を変えた'),
            ('exec-env', lambda mode: self.run_small(mode, 'cp-exec', exec_steps), r'cp-exec#\d+ exec: 子が書いた観測用のファイルが旧新で違う'),
            ('exec-code', lambda mode: self.run_small(mode, 'cp-exec', exec_steps), r'cp-exec#\d+ exec: 旧新が一致しない'),
            ('u3-old', lambda mode: self.run_item(mode, 'cp-hook'), r'cp-hook#1 hook: U3 の Rust の固定期待と違う'),
            ('u4-full', lambda mode: self.run_small(mode, 'cp-pair', uncaught), r'cp-pair#1 verify-pair: U4: 標準エラーが旧版の最後の行・因果の区切りと一致しない'),
            # read の成功(1 回目の照合を通った後の出力)
            ('read-byte', lambda mode: self.run_small(mode, 'cp-read', [1]), r'cp-read#1 read: 旧新が一致しない'),
            # pre: 1 回目の照合の前で止まる流れ(cp-pair の 28 番目の手順: guard の SHA-256 が不正で、verified が出る前に終了コード 20)
            ('pre-stderr', lambda mode: self.run_small(mode, 'cp-pair', self.steps('cp-pair', lambda run: '--guard-sha256' in run['args'] and run['args'][run['args'].index('--guard-sha256') + 1] == 'xyz')[:1]),
             r"(?s)cp-pair#1 verify-pair: 旧新が一致しない\n旧: 20 b'' b'ERROR .*\n新: 20 b'' b'ERRoR "),
            # solo: 入力が途中で変わる流れ(旧版の回と Rust の回を別々に流す)
            ('pair-no-guard', lambda mode: self.run_item(mode, 'cp-pair-after-edit-guard-copy'), r'cp-pair-after-edit-guard-copy#1 verify-pair: 旧新が一致しない\((code, )?stderr'),
            ('pair-write', lambda mode: self.run_item(mode, 'cp-pair-after-rewrite-state'), r'cp-pair-after-rewrite-state#1 verify-pair: 旧新が一致しない\(tree'),
            ('pair-no-inventory', lambda mode: self.run_small(mode, 'cp-pair', inventory), r'cp-pair#1 verify-pair: 旧新が一致しない'),
        ]
        for mode, run, pattern in defects:
            with self.subTest(defect=mode, pattern=pattern):
                with self.assertRaisesRegex(AssertionError, pattern):
                    run(mode)

    def test_a_rust_that_does_not_add_lc_ctype_is_rejected_for_every_locale_step_and_both_kinds_of_child(self):
        # 旧 Python は PEP 538 で、LANG なし・LANG=C のとき子の環境へ LC_CTYPE=C.UTF-8 を足す。足さない Rust(LC_CTYPE の要素だけが違う)を、
        # LANG なし・LANG=C の 4 手順(Python の子と bash の子が 2 つずつ)のそれぞれで見つける。
        lang_steps = self.steps('cp-exec', lambda run: run.get('env', {}).get('LANG', 'C.UTF-8') in (None, 'C'))
        self.assertEqual(len(lang_steps), 4)
        for step in lang_steps:
            run = self.cases['cp-exec'][1]['runs'][step]
            with self.subTest(step=step, lang=run['env'].get('LANG'), child=run['args'][run['args'].index('--') + 1:][:1]):
                self.run_small('control', 'cp-exec', [step])
                with self.assertRaisesRegex(AssertionError, r'cp-exec#\d+ exec: 子が書いた観測用のファイルが旧新で違う'):
                    self.run_small('no-lcctype', 'cp-exec', [step])

    def test_an_old_result_that_no_longer_stops_for_the_recorded_reason_is_rejected(self):
        if s2e_commands.MAC_PATH:
            return   # 旧版の固定期待は Linux で作った値。Mac の経路では確かめない(Mac の旧版の値は未測定)
        import copy
        kind, case = self.cases['cp-refusal-edit-copy']
        tampered = copy.deepcopy(case)
        tampered['recorded'][1]['stderr'] = 'ERROR [environment-guard] 別の理由\n'
        with self.assertRaisesRegex(AssertionError, r'cp-refusal-edit-copy#1 read: 旧版の結果が固定期待\(copy_cases.json。決まった理由で止まる\)と違う'):
            s2e_commands.run_copy_flow(tampered, kind, self.fake('control'), self.work)

    def test_a_flow_must_use_every_recorded_old_result(self):
        # 旧版の記録(copy_cases.json)を持つ流れは、記録の数が手順の数と同じで、全手順を照らす。手順を 1 つ飛ばした流れは落ちる。
        if s2e_commands.MAC_PATH:
            return   # 旧版の記録の照合は Linux だけ
        kind, case = self.cases['cp-read']
        skipped = dict(case, runs=case['runs'][:-1])
        with self.assertRaisesRegex(AssertionError, r'cp-read: 旧版の記録\(copy_cases.json\)の数 \d+ が手順の数 \d+ と違う'):
            s2e_commands.run_copy_flow(skipped, kind, self.fake('control'), self.work)
        short = dict(case, recorded=case['recorded'][:-1])
        with self.assertRaisesRegex(AssertionError, r'cp-read#\d+ \w+: 旧版の記録\(copy_cases.json\)に、この手順\(\d+\)の結果が無い'):
            s2e_commands.run_copy_flow(short, kind, self.fake('control'), self.work)
        # 記録との照合を 1 手順だけ飛ばす実装(照らしたことの記録が欠ける)も、流れの終わりで見つける。
        original = s2e_commands.Flow.check_recorded_old

        def skip_one(flow, label, old):
            if flow.step_index != 1:
                original(flow, label, old)
        with mock.patch.object(s2e_commands.Flow, 'check_recorded_old', skip_one):
            with self.assertRaisesRegex(AssertionError, r'cp-read: 旧版の記録と照らしていない手順がある: \[1\]'):
                s2e_commands.run_copy_flow(case, kind, self.fake('control'), self.work)
        s2e_commands.run_copy_flow(case, kind, self.fake('control'), self.work)

    def test_a_second_bootstrap_that_rewrites_the_first_output_is_rejected(self):
        # 2 回目の bootstrap が、前の出力一式(out/a)を書き換えても、今回の出力先(out/b)だけを比べる実装は見つけない。
        if s2e_commands.MAC_PATH:
            return   # Rust の bootstrap を起動しない経路(Mac は旧版を置き換えて作る)では、偽の Rust を呼ばない
        kind, case = self.cases['cp-read']
        two = dict(case, name='cp-two-bootstraps', recorded=None, runs=[case['runs'][0], dict(case['runs'][0], args=[
            'bootstrap', '--root', '{r}/plugin', '--output', '{r}/out/b', '--inventory', '{r}/inv.json'])])
        s2e_commands.run_copy_flow(two, kind, self.fake('control'), self.work)
        with self.assertRaisesRegex(AssertionError, r"cp-two-bootstraps#1 bootstrap: bootstrap が今回の出力先のほかの出力一式\(out/ の下の \['a'\]\)を変えた"):
            s2e_commands.run_copy_flow(two, kind, self.fake('rewrite-prev-out'), self.work)

    def test_a_cf_user_text_encoding_variable_is_excluded_from_the_child_environment_and_recorded(self):
        flow = s2e_commands.Flow({'name': 'x', 'layout': [], 'runs': []}, self.fake('control'), self.work)
        try:
            raw = json.dumps({'argv': ['a'], 'env': [['A', '1'], ['__CF_USER_TEXT_ENCODING', '0x1F5:0x0:0x0'], ['B', '2']]}).encode()
            value = flow.normalize_probe(raw, 'old')
            self.assertEqual(value['env'], [['A', '1'], ['B', '2']])    # ほかの値と並びは変えない
            self.assertTrue(any('__CF_USER_TEXT_ENCODING' in note and '0x1F5' in note for note in flow.tally.notes))
            self.assertEqual(flow.normalize_probe(b'argv0=x\n', 'new'), b'argv0=x\n')
        finally:
            flow.close()

    def test_sigint_comparison_rejects_a_different_exit_and_accepts_the_same(self):
        for kind in ('exec', 'pair'):
            with self.subTest(kind=kind):
                self.assertGreater(s2e_commands.run_sigint_copy(kind, self.fake('direct'), self.work).runs, 0)
                with self.assertRaisesRegex(AssertionError, r'sigint-%s: 旧新が一致しない' % kind):
                    s2e_commands.run_sigint_copy(kind, self.fake('sigint-130'), self.work)

    def test_sigint_comparison_sees_a_child_left_behind_and_a_guard_that_does_not_stop(self):
        # 子を残す Rust(信号 2 で終わり、最後の行も同じだが、exec の子を止めない): 残った子がパイプを持つので、パイプの EOF を待つ実装は終わらない。
        # guard 自身の終了を待ち、子が生きているかを後始末の前に確かめるので、「旧新が一致しない」(left が True)で落ちる。
        with self.assertRaisesRegex(AssertionError, r"(?s)sigint-exec: 旧新が一致しない.*新: .*'left': True"):
            s2e_commands.run_sigint_copy('exec', self.fake('sigint-leave'), self.work)
        # 子の無い verify-pair は同じ偽の Rust でも旧版と同じ記録になる(統制)。
        self.assertGreater(s2e_commands.run_sigint_copy('pair', self.fake('sigint-leave'), self.work).runs, 0)
        # 子を別のプロセスグループに残す Rust: グループの kill が届かないので、残った子の pid へも送って止める。落ちた後にプロセスが残らない。
        with self.assertRaisesRegex(AssertionError, r"(?s)sigint-exec: 旧新が一致しない.*新: .*'left': True"):
            s2e_commands.run_sigint_copy('exec', self.fake('sigint-leave-group'), self.work)
        # 標準出力・標準エラーのパイプを別のグループの物が持ち続ける Rust: 生の TimeoutExpired でなく、残った物を止めたと分かる AssertionError で落ちる。
        holder = self.base / 'holder-pid'
        holder.unlink(missing_ok=True)
        try:
            with mock.patch.object(s2e_commands, 'PIPE_WAIT', 2):
                with self.assertRaisesRegex(AssertionError, r'SIGINT の後に子が残った・パイプが閉じない'):
                    s2e_commands.run_sigint_copy('exec', self.fake('sigint-hold-pipes'), self.work)
        finally:
            if holder.exists():
                try:
                    os.kill(int(holder.read_text()), signal.SIGKILL)   # この検査が作った sleep だけを止める(比較器が止められない物)
                except ProcessLookupError:
                    pass
        # 中断で終わらない Rust: 生の TimeoutExpired でなく、AssertionError で落ちる。
        with mock.patch.object(s2e_commands, 'SIGINT_WAIT', 1):
            with self.assertRaisesRegex(AssertionError, r'SIGINT を送っても 1 秒以内に終わらない'):
                s2e_commands.run_sigint_copy('pair', self.fake('sigint-ignore'), self.work)

    def test_u3_and_u7_check_the_fixed_expectation_of_the_old_side_by_themselves(self):
        # U3: 判定器の本文を変えた配置(旧版の判定器が別の終了コードで終わる)では、旧版の固定期待の文面で落ちる。旧版の側の照合を外した実装は、ここで通ってしまう。
        kind, case = self.small('cp-hook', self.steps('cp-hook', lambda run: run.get('u3'))[:1])
        layout = [dict(item, body=item['body'].replace('SystemExit(7)', 'SystemExit(9)')) if item.get('path', '').endswith('loop-permission.py') else item
                  for item in case['layout']]
        with self.assertRaisesRegex(AssertionError, r'cp-hook#1 hook: U3 の旧版の固定期待と違う: 9 '):
            s2e_commands.run_copy_flow(dict(case, layout=layout), kind, self.fake('control'), self.work)
        s2e_commands.run_copy_flow(case, kind, self.fake('control'), self.work)
        # U7: 旧版の側の結果が 1 件ずつ違うとき、その件の旧版の固定期待の文面で落ちる(旧版の固定期待は Rust の固定期待より先に、全件で確かめる)。
        real = s2e_commands.run_tool

        def tampered(match):
            def run(argv, **kwargs):
                result = real(argv, **kwargs)
                if argv[0] == sys.executable and match(argv, kwargs):
                    return dataclasses.replace(result, returncode=result.returncode + 3)
                return result
            return run
        for name, match in (('verify-pair-fd0-closed', lambda argv, kwargs: b'verify-pair' in argv and tuple(kwargs['close']) == (0,)),
                            ('verify-pair-fd1-closed', lambda argv, kwargs: b'verify-pair' in argv and tuple(kwargs['close']) == (1,)),
                            ('read-fd1-closed', lambda argv, kwargs: b'read' in argv),
                            ('exec-child-fds-closed', lambda argv, kwargs: b'exec' in argv)):
            with self.subTest(case=name), mock.patch.object(s2e_commands, 'run_tool', tampered(match)):
                with self.assertRaisesRegex(AssertionError, r'u7-%s 旧版: 固定期待と違う' % name):
                    s2e_commands.run_u7_copy(self.fake('direct'), self.work)
        with mock.patch.object(s2e_commands, 'FD_PROBE', "import sys\nopen(sys.argv[1], 'w').write('x')\n"):
            with self.assertRaisesRegex(AssertionError, r"u7-exec-child-fds-closed 旧版: 固定期待と違う.*'x'"):
                s2e_commands.run_u7_copy(self.fake('direct'), self.work)

    def test_u7_and_output_expectations_reject_an_old_like_rust(self):
        with self.assertRaisesRegex(AssertionError, r'u7-verify-pair-fd0-closed Rust: 固定期待と違う'):
            s2e_commands.run_u7_copy(self.fake('direct'), self.work)
        self.assertGreater(s2e_commands.run_output_env_copy(self.fake('direct'), self.work).runs, 0)
        with self.assertRaisesRegex(AssertionError, r'u45-read stdout=pipe-closed unbuffered=None: '):
            s2e_commands.run_output_env_copy(self.fake('plus-one'), self.work)

    def test_the_old_side_of_the_output_and_digit_comparison_must_reach_the_intended_path(self):
        # 旧版が狙った経路へ届かなくなった(旧版の側が違う結果になる)入力は、旧版の固定期待の文面で落ちる(Linux だけ。Mac の旧版の値は未測定)。
        if s2e_commands.MAC_PATH:
            return
        module = s2e_commands.check_module()
        real = module.run_process

        def tampered(change):
            def run(argv, **kwargs):
                result = real(argv, **kwargs)
                return change(argv, kwargs, result) or result
            return run

        def short_read(argv, kwargs, result):
            if argv[0] == sys.executable and b'read' in argv and not kwargs.get('redirects'):
                return dataclasses.replace(result, stdout=result.stdout[:-1])

        def other_overflow(argv, kwargs, result):
            if argv[0] == sys.executable and b'verify-pair' in argv and kwargs['env'].get('PYTHONINTMAXSTRDIGITS') == '1000':
                return dataclasses.replace(result, stderr=result.stderr.replace(b'OverflowError', b'ValueError'))

        def code_changed(argv, kwargs, result):
            if argv[0] == sys.executable and b'verify-pair' in argv and kwargs.get('redirects') == {1: 'pipe-closed'} and 'PYTHONUNBUFFERED' not in kwargs['env']:
                return dataclasses.replace(result, returncode=20)

        for change, pattern in ((short_read, r'u45-read stdout=pipe unbuffered=None: 旧版が決まった理由で止まらない'),
                                (other_overflow, r'u45-pair intmaxstrdigits=1000 go-timeout-digits=700: 旧版が決まった理由で止まらない'),
                                (code_changed, r'u45-pair stdout=pipe-closed unbuffered=None: 旧版が決まった理由で止まらない')):
            with self.subTest(pattern=pattern), mock.patch.object(module, 'run_process', tampered(change)):
                with self.assertRaisesRegex(AssertionError, pattern):
                    s2e_commands.run_output_env_copy(self.fake('direct'), self.work)

    def test_go_timeout_extremes_are_per_machine(self):
        def timeouts():
            return {run['args'][run['args'].index('--go-timeout') + 1] for _, case in s2e_commands.copy_flow_cases() for run in case['runs'] if '--go-timeout' in run['args']}
        with mock.patch.object(s2e_commands, 'MAC_PATH', False):
            self.assertTrue({'100000000', '100000001'} <= timeouts())
        with mock.patch.object(s2e_commands, 'MAC_PATH', True):
            mac = timeouts()
        self.assertIn('100000002', mac)
        self.assertNotIn('100000001', mac)
        self.assertIn('100000000', mac)

    def test_every_copy_flow_is_an_item_and_hook_after_verify_is_excluded_with_a_reason(self):
        names = {item.name: item for item in s2e_commands.items('/nonexistent-fixture') if item.group == 'copy'}
        self.assertEqual(len(names), 32)
        self.assertNotIn('cp-hook-after-verify', names)
        self.assertTrue(all(item.mark == s2e_commands.ALL for item in names.values()))
        self.assertEqual(self.cases['cp-pair-after-edit-copy-and-restore'][0], 'solo')   # 2 回の照合の間にコピーを書き戻す競合
        self.assertEqual(self.cases['cp-pair'][0], 'both')


if __name__ == '__main__':
    unittest.main()
