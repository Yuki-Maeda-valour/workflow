"""品質コマンドの回収。実プロセスと、番号再利用・不在確認失敗の境界を検査する。"""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/ship-task/scripts/check-process.py'


def module():
    spec = importlib.util.spec_from_file_location('test_check_process_helper', SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux で実プロセスと POSIX 代替分岐を検査')
class CheckProcessTest(unittest.TestCase):
    def setUp(self):
        self.helper = module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.libc = ctypes.CDLL(None)
        old = ctypes.c_int()
        self.libc.prctl(37, ctypes.byref(old), 0, 0, 0)
        self.old_subreaper = old.value
        self.assertEqual(0, self.libc.prctl(36, 1, 0, 0, 0))
        self.pids = []
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), '試験の補助 thread が残った')
        for pid in self.pids:
            try:
                found, _ = os.waitpid(pid, os.WNOHANG)
                if not found:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        self.libc.prctl(36, self.old_subreaper, 0, 0, 0)
        self.tmp.cleanup()

    def wait_file(self, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return
            time.sleep(.01)
        raise AssertionError('fixture が起動しない')

    def thread(self, function):
        value = threading.Thread(target=function)
        value.start()
        self.threads.append(value)

    def run_code(self, code, timeout=2, method='linux', grace=.05):
        control = self.root / ('control-' + str(time.monotonic_ns()))
        control.mkdir()
        with self.helper.StopSignals() as stop:
            return self.helper.run_command([sys.executable, '-c', code], self.root, os.environ.copy(),
                                           timeout, control, stop, method=method, grace=grace)

    def remember(self, pidfile):
        if pidfile.exists():
            self.pids.append(int(pidfile.read_text()))
            return self.pids[-1]

    def test_normal_success_nonzero_output_and_launch_failure(self):
        import hashlib
        for method in ('linux', 'posix'):
            for rc in (0, 7):
                with self.subTest(method=method, rc=rc):
                    result = self.run_code(f"import sys; print('out'); print('err',file=sys.stderr); sys.exit({rc})", method=method)
                    self.assertEqual(rc, result['returncode'])
                    self.assertEqual(hashlib.sha256(b'out\n').hexdigest(), result['stdout_sha256'])
                    self.assertEqual(hashlib.sha256(b'err\n').hexdigest(), result['stderr_sha256'])
                    self.assertFalse(result['timed_out'])
            control = self.root / ('missing-' + method)
            control.mkdir()
            with self.helper.StopSignals() as stop:
                result = self.helper.run_command([str(self.root / 'absent')], self.root, os.environ.copy(),
                                                 1, control, stop, method=method, grace=.01)
            self.assertEqual(127, result['returncode'])
            self.assertTrue(result['clean'])

    def test_normal_parent_exit_cleans_pipe_holding_child(self):
        for rc in (0, 9):
            with self.subTest(rc=rc):
                pidfile = self.root / ('pid-' + str(rc))
                code = f'''import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)'])
Path({str(pidfile)!r}).write_text(str(p.pid))
raise SystemExit({rc})
'''
                started = time.monotonic()
                try:
                    result = self.run_code(code)
                finally:
                    pid = self.remember(pidfile)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(rc, result['returncode'])
                self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_timeout_term_ignore_doublefork_setsid_env_clear_and_unrelated(self):
        pidfile = self.root / 'pid'
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
        code = f'''import os,signal,time
if os.fork():
 time.sleep(4); os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
os.environ.clear()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(pidfile)!r},'w').write(str(os.getpid()))
time.sleep(4)
'''
        try:
            try:
                result = self.run_code(code, timeout=.3)
            finally:
                pid = self.remember(pidfile)
            self.assertTrue(result['timed_out'])
            self.assertFalse(Path(f'/proc/{pid}').exists())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_parent_term_int_hup_are_forwarded_and_reaped(self):
        for method, number in ((method, number) for method in ('linux', 'posix') for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)):
            with self.subTest(method=method, number=number):
                pidfile = self.root / (method + str(number))
                def interrupt():
                    self.wait_file(pidfile)
                    os.kill(os.getpid(), number)
                self.thread(interrupt)
                try:
                    with self.assertRaises(self.helper.Interrupted):
                        self.run_code(f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(4)", method=method)
                finally:
                    pid = self.remember(pidfile)
                self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_supervisor_death_never_proves_clean(self):
        pidfile = self.root / 'child'
        supervisor = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            supervisor.append(process.pid)
            return process
        def kill():
            self.wait_file(pidfile)
            os.kill(supervisor[0], signal.SIGKILL)
        self.thread(kill)
        try:
            with mock.patch.object(self.helper.subprocess, 'Popen', side_effect=launch), self.assertRaises(self.helper.RecoveryError):
                self.run_code(f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(2)")
        finally:
            self.remember(pidfile)

    def reap_fixture(self, pidfile):
        """試験の孤児だけを回収する。anchor や command の wait を横取りしない。"""
        self.wait_file(pidfile)
        pid = self.remember(pidfile)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                found, _ = os.waitpid(pid, os.WNOHANG)
                if found:
                    return
            except ChildProcessError:
                pass
            time.sleep(.01)
        raise AssertionError('fixture の孤児を回収できない')

    def test_posix_parent_exit_and_timeout_with_remaining_child(self):
        for timeout in (False, True):
            pidfile = self.root / str(timeout)
            self.thread(lambda: self.reap_fixture(pidfile))
            child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(3)"
            code = f'''import subprocess,sys,time
p=subprocess.Popen([sys.executable,'-c',{child!r}])
open({str(pidfile)!r},'w').write(str(p.pid))
time.sleep({3 if timeout else .1})
'''
            result = self.run_code(code, timeout=.3 if timeout else 2, method='posix')
            self.assertEqual(timeout, result['timed_out'])
            self.assertEqual('original-process-group-only', result['recovery_scope'])
            self.threads[-1].join(2)
            self.assertFalse(Path(f'/proc/{int(pidfile.read_text())}').exists())

    def test_posix_group_escape_is_explicitly_outside_guarantee(self):
        pidfile = self.root / 'escaped'
        marker = self.root / 'marker'
        child = f"import os,time; os.setsid(); open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(.8); open({str(marker)!r},'w').write('escaped')"
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(.15)"
        try:
            result = self.run_code(code, method='posix')
            pid = self.remember(pidfile)
            self.assertEqual('original-process-group-only', result['recovery_scope'])
            self.assertTrue(Path(f'/proc/{pid}').exists())
            os.waitpid(pid, 0)
            self.assertTrue(marker.exists())
        finally:
            self.remember(pidfile)

    def test_posix_anchor_death_is_failure_and_unrelated_is_untouched(self):
        pidfile = self.root / 'posix-orphan'
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
        anchor = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            anchor.append(process.pid)
            return process
        def kill():
            self.wait_file(pidfile)
            os.kill(anchor[0], signal.SIGKILL)
        self.thread(kill)
        self.thread(lambda: self.reap_fixture(pidfile))
        try:
            with mock.patch.object(self.helper.subprocess, 'Popen', side_effect=launch), self.assertRaises(self.helper.RecoveryError):
                self.run_code(f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(3)", method='posix')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_monitor_launch_failure_proves_no_command_started(self):
        for method in ('linux', 'posix'):
            with mock.patch.object(self.helper, 'launch_monitor', side_effect=self.helper.LaunchError):
                result = self.run_code('raise AssertionError("not started")', method=method)
            self.assertEqual(127, result['returncode'])
            self.assertEqual('no-command-started', result['recovery_scope'])


    def test_output_hash_has_a_fixed_size_when_a_writer_keeps_appending(self):
        import hashlib
        path = self.root / 'growing'
        with path.open('w+b') as stream:
            stream.write(b'first')
            stream.flush()
            original = os.fstat
            def before_read(fd):
                info = original(fd)
                with path.open('ab') as writer:
                    writer.write(b'later')
                return info
            with mock.patch.object(self.helper.os, 'fstat', side_effect=before_read):
                value = self.helper.digest_stream(stream)
        self.assertEqual(hashlib.sha256(b'first').hexdigest(), value)


    def test_result_missing_malformed_types_and_special_files(self):
        path = self.root / 'result'
        base = {'nonce': 'nonce', 'clean': True, 'returncode': 0, 'timed_out': False, 'signal': 0}
        with self.assertRaises(OSError):
            self.helper.read_result(path, 'nonce')
        cases = [b'', b'{', b'[]', b'x' * 4097]
        for key, value in [('nonce','other'), ('clean',1), ('returncode',True), ('timed_out',0), ('signal',True), ('signal',999)]:
            cases.append(json.dumps(dict(base, **{key:value})).encode())
        for raw in cases:
            path.write_bytes(raw)
            with self.assertRaises((self.helper.RecoveryError, ValueError)):
                self.helper.read_result(path, 'nonce')
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(self.helper.RecoveryError):
            self.helper.read_result(path, 'nonce')
        path.unlink()
        target = self.root / 'target'
        target.write_text(json.dumps(base))
        path.symlink_to(target)
        with self.assertRaises(OSError):
            self.helper.read_result(path, 'nonce')

    def test_linux_missing_mechanism_does_not_fall_back(self):
        with mock.patch.object(self.helper.os, 'pidfd_open', create=True) as pidfd:
            pidfd.side_effect = OSError('unavailable')
            with self.assertRaises(self.helper.RecoveryError):
                self.run_code('pass')


class GroupIdentityTest(unittest.TestCase):
    def test_only_signal_zero_after_reap_even_when_group_number_reappears(self):
        helper = module()
        events = []
        process = mock.Mock(pid=42)
        process.wait.side_effect = lambda **_: events.append('reap')
        calls = 0
        def send(pid, number):
            nonlocal calls
            events.append(number)
            if number == 0:
                calls += 1
                if calls == 2:
                    raise ProcessLookupError()
        with mock.patch.object(helper.os, 'killpg', side_effect=send), mock.patch.object(helper.time, 'sleep'):
            helper.stop_group(process, .01)
        self.assertEqual([signal.SIGTERM, signal.SIGKILL, 'reap', 0, 0], events)
        process.poll.assert_not_called()
        process.communicate.assert_not_called()

    def test_unknown_group_absence_is_failure(self):
        helper = module()
        process = mock.Mock(pid=42)
        with mock.patch.object(helper.os, 'killpg'), mock.patch.object(helper.time, 'sleep'), \
             mock.patch.object(helper.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaises(helper.RecoveryError):
                helper.stop_group(process, .01)


if __name__ == '__main__':
    unittest.main()
