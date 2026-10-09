"""#260: 内部の予算・読取り競合・出力監督の実プロセス試験。"""
import fcntl
import importlib.util
import json
import os
import pty
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
RUNTIME = SCRIPTS / 'diagnostic-runtime.py'
WRITER = SCRIPTS / 'diagnostic-output.py'


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 公開CLIへ試験用引数や環境を追加せず、内部Budgetを直接構成する。
DRIVER = '''
import importlib.util,json,os,pathlib,signal,sys
p,writer,report,seconds,size,code,phase=sys.argv[1:]
spec=importlib.util.spec_from_file_location('diagnostic_runtime',p)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
r=m.Runtime(m.Budget(seconds=float(seconds)))
original=signal.pthread_sigmask
if phase != 'none':
 def mask(how, signals):
  if how == signal.SIG_BLOCK:
   if phase == 'before': r.stop()
   if phase == 'block-error': raise OSError('masked fixture detail')
  value=original(how,signals)
  if how == signal.SIG_SETMASK:
   if phase == 'after': r.stop()
   if phase == 'restore-error': raise OSError('restore fixture detail')
  return value
 m.signal.pthread_sigmask=mask
raw=(b'{"data":"'+b'x'*int(size)+b'"}\\n') if phase=='json' else b'x'*int(size)+b'\\n'
rc=r.emit(raw,int(code),writer)
try:r.cleanup()
except BaseException:rc=20
pathlib.Path(report).write_text(json.dumps({'rc':rc,'children':[p.poll() for p in r.children],
 'unreaped':r.unreaped,'output_started':r.output_started}))
sys.exit(rc)
'''


class DiagnosticBudgetTests(unittest.TestCase):
    def setUp(self):
        self.module = load(RUNTIME)
        self.temp = tempfile.TemporaryDirectory(prefix='diagnostic-budget-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_each_quantity_allows_boundary_and_rejects_one_more(self):
        m = self.module
        expected = {'management_copy': 200*1024**2, 'management_output': 200*1024**2,
                    'management_entries': 2000, 'worktree_entries': 200000,
                    'body': 1024**3, 'patch_copy': 200*1024**2, 'git_output': 200*1024**2,
                    'stderr': 1024**2, 'json': 280*1024**2}
        self.assertEqual(m.LIMITS, expected)
        for kind, limit in expected.items():
            with self.subTest(kind=kind):
                budget = m.Budget()
                budget.charge(kind, limit)
                with self.assertRaises(m.Failure):
                    budget.charge(kind, 1)

    def test_budget_uses_original_deadline_and_stop_flag(self):
        now = [10.0]
        b = self.module.Budget(clock=lambda: now[0], seconds=1)
        now[0] = 10.9
        self.assertAlmostEqual(b.remaining(), .1)
        b.charge('body', 1)
        now[0] = 11.0
        with self.assertRaises(self.module.Failure): b.check()
        b = self.module.Budget(); b.stopped = True
        with self.assertRaises(self.module.Failure): b.check()

    def test_reader_limit_and_exact_contents(self):
        p = self.root / 'file'; p.write_bytes(b'abcd')
        r = self.module.Reader(self.module.Budget())
        self.assertEqual(r.read(p, limit=4), b'abcd')
        r.verify()
        with self.assertRaises(self.module.Failure): r.read(p, limit=3)

    def test_reader_rejects_replacement_mode_change_missing_creation_and_set_changes(self):
        for change in ('replace', 'mode', 'missing', 'add', 'delete'):
            with self.subTest(change=change):
                folder = self.root / change; folder.mkdir()
                p = folder / 'file'; p.write_bytes(b'old')
                r = self.module.Reader(self.module.Budget())
                r.read(p); r.names(folder)
                if change == 'replace':
                    replacement = folder / 'replacement'; replacement.write_bytes(b'old')
                    os.replace(replacement, p)
                elif change == 'mode': p.chmod(0o700)
                elif change == 'missing':
                    self.assertIsNone(r.read(folder / 'new', missing=True))
                    (folder / 'new').touch()
                elif change == 'add': (folder / 'new').touch()
                else: p.unlink()
                with self.assertRaises((self.module.Failure, OSError)): r.verify()

    def test_reader_refuses_final_and_parent_symlinks(self):
        p = self.root / 'file'; p.write_bytes(b'value')
        (self.root / 'link').symlink_to(p)
        (self.root / 'parent').symlink_to(self.root, target_is_directory=True)
        r = self.module.Reader(self.module.Budget())
        for path in (self.root / 'link', self.root / 'parent/file'):
            with self.subTest(path=path):
                with self.assertRaises((self.module.Failure, OSError)): r.read(path)

    def test_reader_catches_growth_during_read(self):
        p = self.root / 'file'; p.write_bytes(b'x'*65536)
        original = os.read; changed = [False]
        def grow(fd, count):
            data = original(fd, count)
            if data and not changed[0]:
                changed[0] = True
                with p.open('ab') as stream: stream.write(b'!')
            return data
        with mock.patch.object(self.module.os, 'read', side_effect=grow):
            with self.assertRaises(self.module.Failure):
                self.module.Reader(self.module.Budget()).read(p)

    def test_writer_short_writes_interrupts_and_eagain(self):
        module = load(WRITER); received = bytearray(); calls = [0]
        def write(fd, data):
            calls[0] += 1
            if calls[0] == 1: raise InterruptedError()
            if calls[0] == 2: raise BlockingIOError()
            part = bytes(data[:2]); received.extend(part); return len(part)
        with mock.patch.object(module.os, 'write', side_effect=write), \
             mock.patch.object(module.select, 'select', return_value=([], [1], [])):
            self.assertTrue(module.write_all(1, b'abcdef'))
        self.assertEqual(received, b'abcdef')
        with mock.patch.object(module.os, 'write', return_value=0):
            self.assertFalse(module.write_all(1, b'x'))

    def test_reader_rejects_short_read_and_growth_at_exact_limit(self):
        path = self.root / 'short'; path.write_bytes(b'abcd')
        reader = self.module.Reader(self.module.Budget())
        with mock.patch.object(self.module.os, 'read', return_value=b''):
            with self.assertRaises(self.module.Failure): reader.read(path, limit=4)

    def test_json_limit_stops_before_a_complete_response_is_returned(self):
        limits = dict(self.module.LIMITS); limits['json'] = 8
        runtime = self.module.Runtime(self.module.Budget(limits=limits))
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in runtime.handlers.items()])
        with self.assertRaises(self.module.Failure): runtime.response_bytes({'value': 'too long'})
        self.assertFalse(runtime.output_started)
        self.assertEqual(runtime.children, [])

    def test_process_output_and_stderr_budgets_stop_children(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                limits = dict(self.module.LIMITS); limits['git_output'] = 16; limits['stderr'] = 16
                r = self.module.Runtime(self.module.Budget(limits=limits))
                self.addCleanup(lambda r=r: [signal.signal(s, h) for s, h in r.handlers.items()])
                with self.assertRaises(self.module.Failure):
                    r.process([sys.executable, '-I', '-c',
                               'import sys;sys.' + stream + '.buffer.write(b"x"*32)'])
                self.assertTrue(all(p.poll() is not None for p in r.children))
                r.cleanup()

    def test_expired_global_budget_still_reaps_and_cleans(self):
        r = self.module.Runtime(self.module.Budget(seconds=.15))
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in r.handlers.items()])
        owned = self.root / 'owned'; owned.mkdir(); (owned / 'file').touch(); r.temp = os.fsencode(owned)
        with self.assertRaises(self.module.Failure):
            r.process([sys.executable, '-I', '-c', 'import time;time.sleep(60)'])
        self.assertTrue(all(p.poll() is not None for p in r.children))
        self.assertFalse(r.unreaped)
        r.cleanup()
        self.assertFalse(owned.exists())

    def test_term_ignoring_child_is_killed_and_reaped_after_deadline(self):
        runtime = self.module.Runtime(self.module.Budget(seconds=.2))
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in runtime.handlers.items()])
        started = time.monotonic()
        with self.assertRaises(self.module.Failure):
            runtime.process([sys.executable, '-I', '-c',
                'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
        self.assertLess(time.monotonic()-started, 11)
        self.assertEqual([p.poll() for p in runtime.children], [-signal.SIGKILL])
        self.assertFalse(runtime.unreaped)
        runtime.cleanup()

    def test_unreaped_child_retains_private_area_and_does_not_restart_stop_budget(self):
        now = [0.0]
        runtime = self.module.Runtime(self.module.Budget(clock=lambda: now[0], seconds=1))
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in runtime.handlers.items()])
        owned = self.root / 'unreaped'; owned.mkdir(); runtime.temp = os.fsencode(owned)
        child = mock.Mock(); child.poll.return_value = None
        runtime.children.append(child)
        with mock.patch.object(self.module.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0]+seconds)):
            with self.assertRaises(self.module.Failure): runtime.reap(child)
            first_end = now[0]
            with self.assertRaises(self.module.Failure): runtime.reap(child)
            self.assertLessEqual(now[0]-first_end, .05)
        self.assertTrue(runtime.unreaped)
        self.assertTrue(owned.is_dir())
        child.wait.assert_not_called()
        with self.assertRaises(self.module.Failure): runtime.cleanup()
        self.assertTrue(owned.is_dir())

    def test_cleanup_failure_keeps_area_and_does_not_restart_cleanup_budget(self):
        now = [0.0]
        runtime = self.module.Runtime(self.module.Budget(clock=lambda: now[0]))
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in runtime.handlers.items()])
        owned = self.root / 'cleanup'; owned.mkdir(); runtime.temp = os.fsencode(owned)
        with mock.patch.object(self.module.os, 'rmdir', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): runtime.cleanup()
        self.assertTrue(owned.is_dir())
        now[0] = 6
        with self.assertRaises(self.module.Failure): runtime.cleanup()
        self.assertTrue(owned.is_dir())



class DiagnosticOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='diagnostic-output-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / 'result.json'

    def args(self, *, seconds=3, size=100, code=0, phase='none'):
        return [sys.executable, '-I', '-B', '-c', DRIVER, str(RUNTIME), str(WRITER),
                str(self.report), str(seconds), str(size), str(code), phase]

    def result(self, expected):
        value = json.loads(self.report.read_text())
        self.assertEqual(value['rc'], expected)
        self.assertTrue(value['output_started'])
        self.assertFalse(value['unreaped'])
        self.assertTrue(all(code is not None for code in value['children']))
        self.assertEqual(len(value['children']), 1)
        return value

    def test_complete_large_output_and_failure_exit_are_preserved(self):
        result = subprocess.run(self.args(size=10*1024**2, seconds=10), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'x'*(10*1024**2)+b'\n')
        self.result(0)
        result = subprocess.run(self.args(code=22), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 22)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'x'*100+b'\n')
        self.result(22)

    def test_full_280_mib_json_is_allowed_within_original_deadline(self):
        output = self.root / 'near-limit.json'
        total = 280 * 1024**2
        with output.open('wb') as stream:
            result = subprocess.run(self.args(size=total-12, seconds=60, phase='json'),
                                    stdout=stream, stderr=subprocess.PIPE, timeout=75)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.stat().st_size, total)
        with output.open('rb') as stream:
            self.assertEqual(stream.read(9), b'{"data":"')
            remaining = total-12
            while remaining:
                block = stream.read(min(65536, remaining))
                self.assertTrue(block)
                self.assertEqual(block, b'x'*len(block))
                remaining -= len(block)
            self.assertEqual(stream.read(), b'"}\n')
        self.result(0)

    def test_already_nonblocking_sink_retries_without_changing_flags(self):
        readfd, writefd = os.pipe()
        os.set_blocking(writefd, False)
        original = fcntl.fcntl(writefd, fcntl.F_GETFL)
        process = subprocess.Popen(self.args(size=1024*1024, seconds=10), stdout=writefd,
                                   stderr=subprocess.PIPE)
        try:
            # 初めに読み手を止め、実際にEAGAINへ到達させる。
            time.sleep(.15)
            received = bytearray(); deadline = time.monotonic()+12
            while len(received) < 1024*1024+1:
                self.assertTrue(select.select([readfd], [], [], max(0, deadline-time.monotonic()))[0],
                                "非同期出力の受領期限")
                part = os.read(readfd, 65536)
                self.assertTrue(part)
                received.extend(part)
            self.assertEqual(process.wait(timeout=12), 0)
            self.assertEqual(received, b'x'*(1024*1024)+b'\n')
            self.assertEqual(fcntl.fcntl(writefd, fcntl.F_GETFL), original)
            self.assertEqual(process.stderr.read(), b'')
            self.result(0)
        finally:
            if process.poll() is None: process.kill(); process.wait()
            process.stderr.close(); os.close(readfd); os.close(writefd)

    def test_stdout_and_stderr_full_pipes_are_bounded_and_flags_unchanged(self):
        for stream, code in [('stdout', 0), ('stderr', 22)]:
            with self.subTest(stream=stream):
                readfd, writefd = os.pipe()
                try:
                    original = fcntl.fcntl(writefd, fcntl.F_GETFL)
                    os.set_blocking(writefd, False)
                    try:
                        while True: os.write(writefd, b'f'*4096)
                    except BlockingIOError: pass
                    fcntl.fcntl(writefd, fcntl.F_SETFL, original)
                    started = time.monotonic()
                    streams = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, stream: writefd}
                    result = subprocess.run(self.args(seconds=.15, code=code), timeout=12, **streams)
                    self.assertEqual(result.returncode, 20)
                    self.assertLess(time.monotonic()-started, 11)
                    self.assertEqual(fcntl.fcntl(writefd, fcntl.F_GETFL), original)
                    self.result(20)
                finally:
                    os.close(readfd); os.close(writefd)

    def test_partial_stdout_survives_epipe_without_second_writer(self):
        p = subprocess.Popen(self.args(size=4*1024**2), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            partial = p.stdout.read(4096)
            self.assertTrue(partial)
            p.stdout.close()
            self.assertEqual(p.wait(timeout=12), 20)
            self.assertEqual(p.stderr.read(), b'')
            self.result(20)
        finally:
            if p.poll() is None: p.kill(); p.wait()
            p.stderr.close()

    def test_actual_signals_stop_partial_send_and_reap_direct_child(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=sig):
                p = subprocess.Popen(self.args(size=4*1024**2), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
                try:
                    self.assertTrue(p.stdout.read(4096))
                    p.send_signal(sig)
                    self.assertEqual(p.wait(timeout=12), 20)
                    self.assertEqual(p.stderr.read(), b'')
                    self.result(20)
                finally:
                    if p.poll() is None: p.kill(); p.wait()
                    p.stdout.close(); p.stderr.close()

    def test_regular_file_and_tty_are_accepted(self):
        output = self.root / 'regular'
        with output.open('wb') as stream:
            result = subprocess.run(self.args(), stdout=stream, stderr=subprocess.PIPE, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), b'x'*100+b'\n')
        self.result(0)
        master, slave = pty.openpty()
        try:
            flags = fcntl.fcntl(slave, fcntl.F_GETFL)
            result = subprocess.run(self.args(), stdout=slave, stderr=subprocess.PIPE, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL), flags)
            self.assertTrue(os.read(master, 4096).startswith(b'x'*100))
            self.result(0)
        finally:
            os.close(master); os.close(slave)

    def test_already_expired_budget_does_not_spawn_output_child(self):
        result = subprocess.run(self.args(seconds=0), capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 20)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(self.report.read_text())['children'], [])

    def test_signal_and_mask_failures_respect_commit_boundary(self):
        for phase, rc in [('before', 20), ('block-error', 20), ('after', 0), ('restore-error', 0)]:
            with self.subTest(phase=phase):
                result = subprocess.run(self.args(phase=phase), capture_output=True, timeout=12)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertEqual(result.stdout, b'x'*100+b'\n')
                self.assertEqual(result.stderr, b'')
                self.result(rc)


if __name__ == '__main__':
    unittest.main()
