#!/usr/bin/env python3
"""R4R 比較器が、壊した結果・時間切れ・実行不能・0 件・入口の未起動・対応表の欠けを見逃さない検査。"""
import copy
import dataclasses
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
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
        spec = importlib.util.spec_from_file_location('r4r_subject', Path(__file__).with_name('check_r4r.py'))
        subject = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = subject
        spec.loader.exec_module(subject)
    return subject


# 旧 helper を流してから、1 つだけ壊して返す偽の検査入口。
# 旧 helper を流してから、1 つだけ壊して返す偽の検査入口。壊し方は、旧 helper の復元が成功しても
# 止まっても(Mac の fd-copy-unavailable など)、その欄だけが違う形にする。
FAKE = r'''#!{python} -IB
import os, shutil, subprocess, sys
MODE = {mode!r}
WRAPPER = {wrapper!r}
args = sys.argv[1:]
if WRAPPER and '--cp' in args:
    index = args.index('--cp') + 1
    with open(WRAPPER + '.target', 'w') as stream:
        stream.write(args[index])
    args[index] = WRAPPER
if MODE == 'timeout':
    import time
    time.sleep(30)
destination = args[args.index('--destination') + 1] if '--destination' in args else None
source = args[args.index('--source') + 1] if '--source' in args else None
if MODE == 'entity':
    keep = destination + '.keep'
    os.link(destination, keep)
done = subprocess.run([{python!r}, '-I', '-B', {helper!r}, *args], stdin=subprocess.DEVNULL, capture_output=True)
out, err, code = done.stdout, done.stderr, done.returncode
if MODE == 'line':
    out = out.replace(b'RESTORED=', b'RESTORED=x', 1)
elif MODE == 'order':
    lines = out.split(b'\n')
    lines[0], lines[1] = lines[1], lines[0]
    out = b'\n'.join(lines)
elif MODE == 'code':
    code += 1
elif MODE == 'stderr':
    err += b'extra\n'
elif MODE == 'mode':
    os.chmod(destination, (os.stat(destination).st_mode & 0o7777) ^ 0o001)
elif MODE == 'mtime':
    os.utime(destination, ns=(1, 1))
elif MODE == 'content':
    # 復元先は止まった時に元の実体のままで、その中身はハードリンクと共有するので、退避本文の側を変える。
    with open(source, 'ab') as stream:
        stream.write(b'x')
elif MODE == 'entity':
    # 中身・mode・mtime は変えず、実体だけを替える。復元が止まって元の実体のままなら、同じ中身の別の
    # 実体へ替える。復元されて新しい実体なら、元の実体へ戻して中身・mode・mtime を合わせる。
    if os.path.samefile(destination, keep):
        shutil.copy2(destination, destination + '.entity')
        os.replace(destination + '.entity', destination)
        os.unlink(keep)
    else:
        info = os.stat(destination)
        data = open(destination, 'rb').read()
        with open(keep, 'r+b') as stream:
            stream.truncate(0)
            stream.write(data)
        os.chmod(keep, info.st_mode & 0o7777)
        os.utime(keep, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.replace(keep, destination)
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(err)
sys.exit(code)
'''

# --cp を包み、子が受け取るものを 1 つだけ変える。包みは Python の os.execv にそろえる(シェルは
# PWD などを足すので使わない)。Python が起動時に変える SIGPIPE・SIGXFSZ は既定へ戻してから exec する。
# 旧 helper は --cp を os.path.realpath で解決し、その結果を実行するパスと argv[0] に使うので、包みも
# 控えた --cp を同じく解決してから exec する(Mac の /var のように、リンクを含むパスでも同じ argv[0])。
WRAPPER_HEAD = ('#!{python} -IB\nimport os, resource, signal, sys\n'
                'signal.signal(signal.SIGPIPE, signal.SIG_DFL)\nsignal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n'
                't = os.path.realpath(open(sys.argv[0] + ".target").read())\nargs = sys.argv[1:]\nzero = t\n')
WRAPPERS = {
    'none': '',
    # --version の起動も壊す(Mac ではコピーの子が起動されないことがあるため)。観測器は 2 番目以降を見ない。
    'argv': ('if args == ["--version"]:\n    args = ["--version", "--r4r-extra"]\n'
             'elif args[:2] == ["-aL", "--reflink=never"]:\n    args = ["-aL", "--reflink=never"] + args[1:]\n'),
    'argv0': 'zero = t + "-other-name"\n',
    'env': 'os.environ["R4R_FAKE"] = "1"\n',
    'fsize': 'if args == ["--version"]:\n    resource.setrlimit(resource.RLIMIT_FSIZE, (512000, 512000))\n',
    'fds': 'os.dup2(os.open("/dev/null", os.O_RDONLY), 200)\n',
    'flags': 'os.dup2(os.open("/dev/null", os.O_RDONLY), 0)\n',
    'ignored': 'signal.signal(signal.SIGUSR2, signal.SIG_IGN)\n',
    'mask': 'signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGUSR1}})\n',
}
WRAPPER_TAIL = 'os.execv(t, [zero] + args)\n'
# 包みごとに、違うはずの欄。
WRAPPER_FIELDS = {'none': [], 'argv': ['cp.args'], 'argv0': ['cp.argv0'], 'env': ['cp.env'], 'fsize': ['cp.fsize'],
                  'fds': ['cp.fds'], 'flags': ['cp.fds'], 'ignored': ['cp.ignored'], 'mask': ['cp.blocked']}
FAKE_FIELDS = {'none': [], 'line': ['stdout'], 'order': ['stdout'], 'code': ['returncode'], 'stderr': ['stderr'],
               'mode': ['files.destination'], 'mtime': ['files.destination'], 'content': ['files.source'],
               'entity': ['relations']}


class Comparator(unittest.TestCase):
    def setUp(self):
        self.m = get_subject()
        self.m.use_real_tempdir()
        self.base = Path(tempfile.mkdtemp(prefix='r4r-self-'))
        self.addCleanup(shutil.rmtree, self.base, True)

    def result(self, code=0, out=b'', err=b'', timeout=False):
        return self.m.RunResult(code, out, err, timeout)

    def observed(self, **changes):
        good = self.m.Observed(
            self.result(0, b'RESTORED=yes\nTOUCHED=replaced\nTEMP=removed\n'),
            {'source': {'mode': 0o100640, 'sha256': 'a'}, 'destination': {'mode': 0o100640, 'sha256': 'a'}},
            [[]], {'same-entity': False, 'link-kept': True, 'fd-kept': True},
            [{'argv0': '/stub/cp-stub', 'args': ['--version'], 'fds': {'0': [32770, 0, '/dev/null']},
              'fsize': [-1, -1], 'blocked': 0, 'ignored': 0, 'caught': 0, 'env': '41'}], 0.1)
        return dataclasses.replace(good, **changes)

    def test_normal_control_then_every_field_mutation(self):
        good = self.observed()
        self.m.compare_observed(good, self.observed())
        self.assertEqual(self.m.differences(good, self.observed()), [])
        mutations = [
            (dict(result=self.result(0, b'RESTORED=yes\nTOUCHED=copied\nTEMP=removed\n')), ['stdout']),
            (dict(result=self.result(0, b'TOUCHED=replaced\nRESTORED=yes\nTEMP=removed\n')), ['stdout']),
            (dict(result=self.result(33, b'RESTORED=yes\nTOUCHED=replaced\nTEMP=removed\n')), ['returncode']),
            (dict(result=self.result(0, b'RESTORED=yes\nTOUCHED=replaced\nTEMP=removed\n', b'extra')), ['stderr']),
            (dict(result=self.result(0, b'RESTORED=yes\nTOUCHED=replaced\nTEMP=removed\n', timeout=True)),
             ['timeout']),
            (dict(files={'source': {'mode': 0o100640, 'sha256': 'a'},
                         'destination': {'mode': 0o100600, 'sha256': 'a'}}), ['files.destination']),
            (dict(temps=[[(b'.restore-taskmd-' + b'X' * 32, 0o40700, [])]]), ['temps']),
            (dict(relations={'same-entity': True, 'link-kept': True, 'fd-kept': True}), ['relations']),
            (dict(stub=[]), ['cp.count']),
        ]
        stub = good.stub[0]
        for key, value in (('argv0', '/other'), ('args', ['--version', 'x']), ('env', '42'), ('fsize', [9, 9]),
                           ('fds', {'0': [32770, 0, '/dev/null'], 'extra-7': [0, 0, '']}),
                           ('fds', {'0': [32768, 0, '/dev/null']}), ('fds', {'0': [32770, 0, 'pipe']}),
                           ('blocked', 0x200), ('ignored', 0x1000), ('caught', 0x4000)):
            mutations.append((dict(stub=[dict(stub, **{key: value})]), ['cp.' + key]))
        for change, fields in mutations:
            with self.subTest(change=change):
                self.assertEqual(self.m.differences(good, self.observed(**change)), fields)
                with self.assertRaises(self.m.ComparisonError):
                    self.m.compare_observed(good, self.observed(**change))

    def test_stdout_normalization_is_only_the_temp_hex(self):
        a = self.result(33, b'TEMP_PATH=/x/.restore-taskmd-' + b'0' * 32 + b'\n')
        b = self.result(33, b'TEMP_PATH=/x/.restore-taskmd-' + b'f' * 32 + b'\n')
        self.m.same_result(a, b)
        for other in (b'TEMP_PATH=/y/.restore-taskmd-' + b'f' * 32 + b'\n',
                      b'TEMP_PATH=/x/.restore-taskmd-' + b'f' * 31 + b'\n',
                      b'TEMP_PATH=/x/.restore-taskmd-' + b'F' * 32 + b'\n'):
            with self.assertRaises(self.m.ComparisonError):
                self.m.same_result(a, self.result(33, other))

    def test_fixed_expectations_reject_other_reason_touched_temp_and_code(self):
        good = self.result(33, b'RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=taskmd-body\n')
        self.m.fixed(good, 33, 'taskmd-body', 'none', 'none')
        for args in ((20, 'taskmd-body', 'none', 'none'), (33, 'verify-failed', 'none', 'none'),
                     (33, 'taskmd-body', 'replaced', 'none'), (33, 'taskmd-body', 'none', 'removed')):
            with self.assertRaises(AssertionError):
                self.m.fixed(good, *args)
        with self.assertRaises(AssertionError):
            self.m.fixed(self.result(0, b'RESTORED=yes\nTOUCHED=copied\nTEMP=removed\nREASON=x\n'), 0)
        with self.assertRaises(AssertionError):
            self.m.fixed(dataclasses.replace(good, timed_out=True), 33)

    def send(self, channel, record):
        import socket
        lines = ['argv0\t' + record['argv0'].encode().hex()]
        lines += ['arg\t' + arg.encode().hex() for arg in record['args']]
        lines += ['fsize\t-1 -1', 'signals\t0 0 0', 'pid\t%d' % record['pid']]
        lines += ['fd\t%d %d %d %s' % (fd, *values) for fd, values in record['fds'].items()]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
            sender.connect(str(channel.path))
            sender.sendall(('\n'.join(lines) + '\n').encode())

    def test_channel_uses_short_socket_path(self):
        # Mac の AF_UNIX のパスの上限(104 バイト)に当たらないよう、/tmp の下の短いパスに置く。
        channel = self.m.Channel()
        self.addCleanup(channel.close)
        self.assertLess(len(os.fsencode(channel.path)), 64)

    def test_unreaped_cp_record_is_rejected(self):
        channel = self.m.Channel()
        self.addCleanup(channel.close)
        sleeper = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        record = {'pid': sleeper.pid, 'argv0': '/stub', 'args': ['--version'], 'fds': {0: [2, 0, '/dev/null']}}
        self.send(channel, record)
        with self.assertRaises(self.m.ComparisonError):
            self.m.read_stub(channel)
        # 回収済みの子の記録は通る(正常な対照)。
        gone = subprocess.Popen(['true'])
        gone.wait()
        record['pid'] = gone.pid
        self.send(channel, record)
        self.assertEqual(len(self.m.read_stub(channel)), 1)

    def test_cp_alias_fd_number_must_be_inherited(self):
        channel = self.m.Channel()
        self.addCleanup(channel.close)
        gone = subprocess.Popen(['true'])
        gone.wait()
        record = {'pid': gone.pid, 'argv0': '/stub',
                  'args': ['-aL', '--reflink=never', '--', '/proc/self/fd/5', '/proc/self/fd/6'],
                  'fds': {0: [2, 0, '/dev/null'], 5: [0, 0, 'file'], 6: [2, 0, 'file']}}
        self.send(channel, record)
        [parsed] = self.m.read_stub(channel)
        self.assertEqual(parsed['args'][-2:], ['/proc/self/fd/<input>', '/proc/self/fd/<output>'])
        record['fds'] = {0: [2, 0, '/dev/null'], 5: [0, 0, 'file']}
        self.send(channel, record)
        with self.assertRaises(self.m.ComparisonError):
            self.m.read_stub(channel)

    def test_runner_stderr_keeps_other_lines_verbatim(self):
        notice = '/tmp/x/supervised: 1 行: 123 強制終了            "$supervisor" --signal=TERM\n'.encode()
        english = b'/tmp/y/supervised: line 1: 456 Killed                  "$supervisor"\n'
        self.assertEqual(self.m.runner_stderr(notice), self.m.runner_stderr(
            '/tmp/z/other: 1 行: 999 強制終了  x\n'.encode()))
        self.assertEqual(self.m.runner_stderr(english)[0], ('notice', b'Killed'))
        self.assertNotEqual(self.m.runner_stderr(notice + b'extra from helper\n'), self.m.runner_stderr(notice))
        self.assertNotEqual(self.m.runner_stderr(notice), self.m.runner_stderr(english))

    def test_mac_partition_covers_sixteen_without_overlap(self):
        self.m.check_partition()
        for apply, result, skip in ((self.m.MAC_APPLY[1:], self.m.MAC_RESULT_ONLY, self.m.MAC_SKIP),
                                    (self.m.MAC_APPLY, self.m.MAC_RESULT_ONLY + self.m.MAC_APPLY[:1], self.m.MAC_SKIP[1:])):
            with mock.patch.multiple(self.m, MAC_APPLY=apply, MAC_RESULT_ONLY=result, MAC_SKIP=skip):
                with self.assertRaises(AssertionError):
                    self.m.check_partition()

    def fake(self, mode, wrapper=None):
        wrapper_path = ''
        if wrapper:
            wrapper_path = str(self.base / ('wrapper-' + wrapper))
            Path(wrapper_path).write_text((WRAPPER_HEAD + WRAPPERS[wrapper] + WRAPPER_TAIL).format(python=sys.executable))
            os.chmod(wrapper_path, 0o755)
        path = self.base / ('fake-' + mode + ('-' + wrapper if wrapper else ''))
        path.write_text(FAKE.format(python=sys.executable, mode=mode, wrapper=wrapper_path, helper=str(self.m.HELPER)))
        path.chmod(0o755)
        return path

    def run_fake(self, fixture, *, stub=True, timeout=60):
        case = self.m.Contract('test_files')
        case.setUp()
        self.addCleanup(case.doCleanups)
        if stub:
            case.stub('gnu')
        with mock.patch.object(self.m, 'FIXTURE', fixture):
            return self.m.run_pair(case.scenario(timeout=timeout))

    def fake_differences(self, fixture, stub='gnu', alias=False):
        """run_pair と同じ手順で流し、旧と偽物で違った欄の一覧を返す。alias では --cp をリンクの
        フォルダ経由で渡す(Mac の /var のように、旧 helper の realpath で別のパスになる形)。"""
        case = self.m.Contract('test_files')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.stub(stub)
        if alias:
            link = case.base / 'cp-alias'
            link.symlink_to(Path(case.cp).parent, target_is_directory=True)
            case.cp = str(link / Path(case.cp).name)
        sc = case.scenario()
        with mock.patch.object(self.m, 'FIXTURE', fixture):
            self.m.snapshot(sc.root, sc.base / 'backup')
            old = self.m.run_side(sc, True)
            self.m.restore_layout(sc.root, sc.base / 'backup')
            new = self.m.run_side(sc, False)
        self.assertTrue(old.stub, '偽の cp の記録が無い')
        return self.m.differences(old, new)

    def broken_field_table(self, stub):
        # 正しい例(旧 helper を流すだけの偽物と、何も変えない包み)では、どの欄も違わない。
        self.assertEqual(self.fake_differences(self.fake('none'), stub), [])
        self.assertEqual(self.fake_differences(self.fake('none', 'none'), stub), [])
        # --cp がリンクを含むパスでも、何も変えない包みは違わず、argv[0] を変える包みだけが違う。
        self.assertEqual(self.fake_differences(self.fake('none', 'none'), stub, alias=True), [])
        self.assertEqual(self.fake_differences(self.fake('none', 'argv0'), stub, alias=True), ['cp.argv0'])
        for mode, fields in FAKE_FIELDS.items():
            if mode == 'none':
                continue
            with self.subTest(mode=mode, stub=stub):
                self.assertEqual(self.fake_differences(self.fake(mode), stub), fields)
        for wrapper, fields in WRAPPER_FIELDS.items():
            if wrapper == 'none':
                continue
            with self.subTest(wrapper=wrapper, stub=stub):
                self.assertEqual(self.fake_differences(self.fake('none', wrapper), stub), fields)

    def test_broken_fixtures_differ_only_in_the_broken_field(self):
        self.broken_field_table('gnu')
        with self.assertRaises(self.m.ComparisonError):
            self.run_fake(self.fake('code'))

    def test_broken_fixtures_when_restore_stops_simulating_mac_not_counted_as_mac_success(self):
        """復元が止まる配置(コピーに答えない cp。Mac の fd-copy-unavailable の止まり方を模擬する)でも、
        壊した欄だけが違う。Linux で模擬するので、Mac の成功には数えない。"""
        print('R4R: 復元が止まる配置で壊す表を流す(Mac を Linux で模擬。Mac の成功には数えない)')
        self.broken_field_table('version-only')

    def test_observer_missing_cc_failure_and_environment_rewrite_are_rejected(self):
        real_which = shutil.which

        def no_cc(name, *args, **kwargs):
            return None if name == 'cc' else real_which(name, *args, **kwargs)
        with mock.patch.object(shutil, 'which', no_cc):
            with self.assertRaisesRegex(AssertionError, 'cc が無い'):
                self.m.make_stub(self.base / 'no-cc')
        source = self.m.OBSERVER_C
        broken = {
            '組めない': source.replace('int main(int argc', 'int main(int argc, ;', 1),
            '失敗した': source.replace('    put_hex("argv0", argv[0]);', '    _exit(97);', 1),
            # Python の観測器のように、起動時に LC_CTYPE を足す。
            '環境を書き換えた': source.replace('    put_hex("argv0", argv[0]);',
                                        '    setenv("LC_CTYPE", "C.UTF-8", 1);\n    put_hex("argv0", argv[0]);', 1),
            # argv[0] を変えて記録する。
            'argv を変えて記録した': source.replace('    put_hex("argv0", argv[0]);',
                                             '    put_hex("argv0", "/other");', 1),
        }
        for message, text in broken.items():
            self.assertNotEqual(text, source, message)
            with self.subTest(message=message):
                with self.assertRaisesRegex(AssertionError, message):
                    self.m.make_stub(self.base / ('broken-' + str(len(message))), source_text=text)
        # 正しい観測器は通る(対照)。
        stub, channel = self.m.make_stub(self.base / 'good')
        channel.close()
        # 偽の cp を使う件で、観測器の記録が 1 件も無ければ失敗にする。
        case = self.m.Contract('test_files')
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.stub('gnu')
        case.log.close()
        case.log = self.m.Channel()
        self.addCleanup(case.log.close)
        with self.assertRaisesRegex(AssertionError, '偽の cp の記録が無い'):
            self.m.run_side(case.scenario(), True)

    def test_timeout_and_unrunnable_fixture_fail(self):
        with self.assertRaises(self.m.ComparisonError):
            self.run_fake(self.fake('timeout'), stub=False, timeout=1)
        for value in (None, '', 'relative', '/r4r-absent-binary', str(self.base)):
            with self.assertRaises(ValueError):
                self.m.fixture_path(value)
        plain = self.base / 'plain'
        plain.write_text('#!/bin/sh\n')
        plain.chmod(0o644)
        with self.assertRaises(ValueError):
            self.m.fixture_path(str(plain))
        with self.assertRaises(OSError):
            self.m.launch([str(plain)])

    def test_zero_failed_and_skipped_cases_are_rejected(self):
        with self.assertRaises(AssertionError):
            self.m.run_suite(unittest.TestSuite())

        class Failure(unittest.TestCase):
            def test_broken(self):
                self.fail('deliberate')

        class Skip(unittest.TestCase):
            def test_skipped(self):
                self.skipTest('deliberate')
        with mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(AssertionError):
                self.m.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Failure))
            with self.assertRaises(AssertionError):
                self.m.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Skip))

    def test_existing_manifest_launch_counts_and_acl_skip_are_checked(self):
        with mock.patch.object(unittest.TestLoader, 'getTestCaseNames', return_value=[]):
            with self.assertRaises(AssertionError):
                self.m.existing_suite('legacy')
        launches = self.base / 'launches'
        launches.write_text('rust\n')
        result = unittest.TestResult()
        name = self.m.SIXTEEN[0]
        records = {('R', name): [('pair', 0, b''), ('launches', 1)]}
        # ここから Linux の経路(ACL の省略の照合が無い)。Mac の実機でも Linux の経路を確かめるので、
        # OS を明示する(省略すると実行した OS になり、Mac では ACL の照合に当たる)。
        self.m.compare_existing(records, launches, [name], result, system='linux')
        # 入口が起動されなかった件・比べずに起動した回・記録の無い起動は失敗。
        for broken in ([('launches', 0)], [('pair', 0, b''), ('launches', 2)], [('launches', 1)],
                       [('pair', 0, b''), ('pair', 0, b''), ('launches', 1)]):
            with self.assertRaises(AssertionError):
                self.m.compare_existing({('R', name): broken}, launches, [name], result, system='linux')
        launches.write_text('')
        with self.assertRaises(AssertionError):
            self.m.compare_existing(records, launches, [name], result, system='linux')
        # 直接起動の件の回の関係が L と R で違えば失敗。
        launches.write_text('rust\n')
        direct = self.m.DIRECT[0]
        same = [('run', 33, b'x', b''), ('end', {'temp-dirs': 0})]
        records = {('L', direct): list(same), ('R', direct): list(same) + [('launches', 1)],
                   ('R', name): [('pair', 0, b''), ('launches', 1)]}
        self.m.compare_existing(records, launches, [name, direct], result, system='linux')
        for broken in ([('run', 20, b'x', b''), same[1]], [same[0], ('end', {'temp-dirs': 1})], same[:1]):
            records[('R', direct)] = broken + [('launches', 1)]
            with self.assertRaises(self.m.ComparisonError):
                self.m.compare_existing(records, launches, [name, direct], result, system='linux')
        # Mac の分岐: ACL の件が L と R の両方で、既存の理由で省かれていなければ失敗。
        records[('R', direct)] = list(same) + [('launches', 1)]

        acl = self.m.ACL
        for skipped, ok in (([('L', self.m.ACL_REASONS[0]), ('R', self.m.ACL_REASONS[0])], True),
                            ([('L', self.m.ACL_REASONS[0])], False),
                            ([('L', 'other'), ('R', 'other')], False), ([], False)):
            fake = unittest.TestResult()
            for side, reason in skipped:
                test = mock.Mock(_testMethodName=acl)
                type(test).__name__ = side
                fake.skipped.append((test, reason))
            if ok:
                self.m.compare_existing(records, launches, [name, direct], fake, system='darwin')
            else:
                with self.assertRaises(AssertionError):
                    self.m.compare_existing(records, launches, [name, direct], fake, system='darwin')

    def test_mac_branches_simulated_on_linux_not_counted_as_mac_success(self):
        """Mac の分岐(代用品の cp の受け手の保持・結果だけを比べる 8 件の包み・対照のコピー・ACL の
        省略の照合)を Linux で模擬する。Mac の成功には数えない。"""
        print('R4R: Mac の分岐を Linux で模擬する(Mac の成功には数えない)')
        # Mac の既定の一時フォルダ(/var/folders/…)は /var がリンク。比較器は配置をリンクを含まない実体の
        # パスに置く(旧 helper と Rust は親の各段をリンクを辿らずに開くので、置かないと退避本文を開けずに
        # taskmd-body で止まり、意図した経路に届かない)。
        real = self.base / 'real-tmp'
        real.mkdir()
        linked = self.base / 'linked-tmp'
        linked.symlink_to(real, target_is_directory=True)
        with mock.patch.object(tempfile, 'tempdir', str(linked)):
            self.assertEqual(self.m.use_real_tempdir(), os.path.realpath(real))
            self.assertEqual(tempfile.gettempdir(), os.path.realpath(real))
        target = 'rust' if self.m.FIXTURE else 'legacy'
        real_which = shutil.which

        def which(name, *args, **kwargs):
            return None if name in ('setfacl', 'getfacl') else real_which(name, *args, **kwargs)
        with mock.patch.object(shutil, 'which', which), mock.patch('sys.stderr', io.StringIO()):
            existing = self.m.existing_suite(target, system='darwin')
            try:
                self.assertEqual(len(existing.channels), 1)
                result = self.m.run_suite(existing.suite, allow_skips=(self.m.ACL,))
                self.m.compare_existing(existing.records, existing.launches, existing.rust_names, result,
                                        system='darwin')
            finally:
                existing.close()
        self.assertEqual(sorted(existing.rust_names), sorted(self.m.MAC_APPLY + self.m.MAC_RESULT_ONLY + (self.m.ACL,)))
        for name in self.m.MAC_RESULT_ONLY:
            self.assertTrue(any(item[0] == 'pair' for item in existing.records[('R', name)]), name)
        # 代用品ではコピーが止まるので、既存の検査は捨てられている(包みが働いた)。
        self.assertTrue(any(item[0] == 'ignored' for name in self.m.MAC_RESULT_ONLY
                            for item in existing.records[('R', name)]))
        # 包みは比較の失敗を捨てない。
        wrapped = self.m.result_only(lambda case: (_ for _ in ()).throw(self.m.ComparisonError('x')))
        case = mock.Mock(records=[], _testMethodName='x')
        with self.assertRaises(self.m.ComparisonError):
            wrapped(case)
        wrapped = self.m.result_only(lambda case: None)
        with self.assertRaises(AssertionError):
            wrapped(mock.Mock(records=[], _testMethodName='x'))

    def test_map_missing_unrun_excluded_ignored_and_os_mark_errors_are_rejected(self):
        data = json.loads(self.m.MAP.read_text())
        listed = ''.join(entry['rust'] + ': test\n' for entry in data['tests'].values())
        self.m.check_map(listed, '', 'linux', data)
        first = sorted(data['tests'])[0]
        broken = []
        missing = copy.deepcopy(data)
        del missing['tests'][first]
        broken.append((missing, listed, ''))
        broken.append((data, listed.replace(data['tests'][first]['rust'] + ': test\n', ''), ''))
        excluded = copy.deepcopy(data)
        excluded['tests'][first]['unreproducible'] = 'reason'
        broken.append((excluded, listed, ''))
        for mark in ([], ['darwin'], ['linux', 'windows']):
            wrong = copy.deepcopy(data)
            wrong['tests'][first]['os'] = mark
            broken.append((wrong, listed, ''))
        no_rust = copy.deepcopy(data)
        no_rust['tests'][first]['rust'] = ''
        broken.append((no_rust, listed, ''))
        broken.append((data, listed, data['tests'][first]['rust'] + ': test\n'))
        for value, list_text, ignored in broken:
            with self.assertRaises(AssertionError):
                self.m.check_map(list_text, ignored, 'linux', value)
        # Mac では linux だけの件を --list に求めない。
        mac_listed = ''.join(entry['rust'] + ': test\n' for entry in data['tests'].values() if 'darwin' in entry['os'])
        self.m.check_map(mac_listed, '', 'darwin', data)


if __name__ == '__main__':
    unittest.main()
