"""プロセス所有関係による回収の実プロセス回帰。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/dev-workflow/skills/ship-task/scripts/loop-supervisor.py'


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, code, timeout='3'):
        return subprocess.Popen([sys.executable, str(SCRIPT), '--result', str(self.root / 'result'),
                                 '--nonce', 'test-nonce', '--timeout', timeout, '--grace', '.1', '--',
                                 sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def result(self, process):
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (out, err))
        value = json.loads((self.root / 'result').read_text())
        self.assertTrue(value['clean'])
        self.assertEqual(value['nonce'], 'test-nonce')
        return value

    def wait_file(self, path):
        for _ in range(150):
            if path.exists() and path.stat().st_size:
                return
            time.sleep(.02)
        self.fail('子の起動を確認できない')

    def assert_gone(self, pid):
        for _ in range(100):
            if not Path(f'/proc/{pid}').exists():
                return
            time.sleep(.01)
        self.fail(f'子 {pid} が残った')

    def test_runtime_check_and_unavailable_pidfd_are_explicit(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), '--check'], capture_output=True, timeout=3)
        self.assertEqual(0, proc.returncode, proc.stderr)
        from unittest import mock
        from contextlib import redirect_stderr
        import io
        module = self.module(); output = io.StringIO()
        with mock.patch.object(module.sys, 'argv', [str(SCRIPT), '--check']), mock.patch.object(module, 'setup'), mock.patch.object(module.os, 'pidfd_open', side_effect=OSError()), redirect_stderr(output):
            self.assertEqual(97, module.main())
        self.assertIn('subreaper・pidfd', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_normal_exit(self):
        self.assertEqual(self.result(self.start('raise SystemExit(7)'))['returncode'], 7)

    def test_detached_double_fork_env_clear_nondumpable(self):
        pidfile = self.root / 'pid'
        code = f'''import os,time,ctypes
if os.fork():
 time.sleep(.3); raise SystemExit(0)
os.setsid()
if os.fork(): os._exit(0)
os.environ.clear()
ctypes.CDLL(None).prctl(4,0,0,0,0)
open({str(pidfile)!r},'w').write(str(os.getpid()))
time.sleep(60)
'''
        process = self.start(code)
        self.wait_file(pidfile)
        pid = int(pidfile.read_text())
        self.result(process)
        self.assert_gone(pid)

    def test_timeout_and_unrelated_process_untouched(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            value = self.result(self.start('import time; time.sleep(60)', '.2'))
            self.assertTrue(value['timed_out'])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait()

    def test_term_cleans_child(self):
        pidfile = self.root / 'pid'
        process = self.start(f'import os,time; open({str(pidfile)!r},"w").write(str(os.getpid())); time.sleep(60)')
        self.wait_file(pidfile)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(self.result(process)['signal'], signal.SIGTERM)
        self.assert_gone(int(pidfile.read_text()))

    def test_supervisor_kill_has_no_success_proof(self):
        pidfile = self.root / 'pid'
        process = self.start(f'import os,time; open({str(pidfile)!r},"w").write(str(os.getpid())); time.sleep(60)')
        self.wait_file(pidfile)
        process.kill()
        process.wait(timeout=3)
        os.kill(int(pidfile.read_text()), signal.SIGKILL)
        process.communicate(timeout=3)
        result = self.root / 'result'
        self.assertFalse(result.exists() and '"clean": true' in result.read_text())

    def test_parent_death_cleans_child(self):
        pidfile = self.root / 'pid'
        # このテスト自身が supervisor の親にならないよう、中間親だけを停止する。
        args = [sys.executable, str(SCRIPT), '--result', str(self.root / 'result'), '--nonce', 'test-nonce',
                '--timeout', '60', '--grace', '.1', '--', sys.executable, '-c',
                f'import os,time; open({str(pidfile)!r},"w").write(str(os.getpid())); time.sleep(60)']
        parent = subprocess.Popen([sys.executable, '-c', f'import subprocess,time; subprocess.Popen({args!r}); time.sleep(60)'])
        self.wait_file(pidfile)
        parent.kill()
        parent.wait()
        self.wait_file(self.root / 'result')
        self.assertTrue(json.loads((self.root / 'result').read_text())['clean'])
        self.assert_gone(int(pidfile.read_text()))

    def module(self):
        import importlib.util
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('supervisor_unit', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pid_reuse_does_not_signal(self):
        from unittest import mock
        module = self.module()
        with mock.patch.object(module, 'own_children', return_value=[12345]), \
             mock.patch.object(module, 'identity', side_effect=[(os.getpid(), 1), (os.getpid(), 2)]), \
             mock.patch.object(module.os, 'pidfd_open', return_value=123), \
             mock.patch.object(module.os, 'close'), \
             mock.patch.object(module.signal, 'pidfd_send_signal') as sent:
            with self.assertRaisesRegex(RuntimeError, 'starttime'):
                module.signal_owned(signal.SIGTERM)
            sent.assert_not_called()

    def test_unreadable_identity_is_unknown_not_empty(self):
        from unittest import mock
        module = self.module()
        with mock.patch.object(module, 'own_children', return_value=[12345]), \
             mock.patch.object(module, 'identity', side_effect=PermissionError('blocked')), \
             mock.patch.object(module.signal, 'pidfd_send_signal') as sent:
            with self.assertRaises(PermissionError):
                module.signal_owned(signal.SIGTERM)
            sent.assert_not_called()

    def test_int_cleans_child(self):
        pidfile = self.root / 'pid'
        process = self.start(f'import os,time; open({str(pidfile)!r},"w").write(str(os.getpid())); time.sleep(60)')
        self.wait_file(pidfile)
        process.send_signal(signal.SIGINT)
        self.assertEqual(self.result(process)['signal'], signal.SIGINT)
        self.assert_gone(int(pidfile.read_text()))

    def test_special_result_file_stops_before_child(self):
        os.mkfifo(self.root / 'result')
        marker = self.root / 'child-was-started'
        process = self.start(f'open({str(marker)!r}, "w").write("unexpected")')
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 97)
        self.assertFalse(marker.exists())


class ParentSupervisorControlTest(unittest.TestCase):
    """親の保持コードでも、整数 PID だけでは signal を送らない。"""
    @classmethod
    def setUpClass(cls):
        loop = SCRIPT.with_name('loop.sh').read_text()
        cls.helper = loop.split("read -r -d '' PY_HELPER <<'PY' || true\n", 1)[1].split('\nPY\n', 1)[0]

    def control(self, mode, pid, start='', parent=None):
        return subprocess.run([sys.executable, '-c', self.helper, 'supervisor-control', mode,
                               str(pid), str(os.getpid() if parent is None else parent), str(start)],
                              capture_output=True, timeout=3)

    def sleeper(self):
        return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])

    def test_actual_owned_process_capture_alive_and_term(self):
        child = self.sleeper()
        try:
            captured = self.control('capture', child.pid)
            self.assertEqual(0, captured.returncode, captured.stderr)
            start = captured.stdout.decode().strip()
            self.assertEqual(0, self.control('alive', child.pid, start).returncode)
            self.assertEqual(0, self.control('term', child.pid, start).returncode)
            self.assertEqual(-signal.SIGTERM, child.wait(timeout=3))
            self.assertEqual(3, self.control('alive', child.pid, start).returncode)
        finally:
            if child.poll() is None: child.kill(); child.wait()

    def test_retained_starttime_mismatch_and_other_parent_do_not_signal(self):
        child = self.sleeper()
        try:
            start = int(self.control('capture', child.pid).stdout)
            self.assertEqual(3, self.control('kill', child.pid, start + 1).returncode)
            self.assertIsNone(child.poll())
            self.assertEqual(3, self.control('term', child.pid, start, parent=os.getpid() + 1).returncode)
            self.assertIsNone(child.poll())
        finally:
            child.kill(); child.wait()

    def test_normal_fast_exit_is_distinct_from_unreadable_identity(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass']); child.wait(timeout=3)
        self.assertEqual(3, self.control('capture', child.pid).returncode)
        # EACCES is unknown, never the rc=3 disappearance used by normal fast exits.
        definition = self.helper.split('\nCOMMANDS = {', 1)[0]
        source = definition + '\nfrom unittest import mock\nwith mock.patch("os.open", side_effect=PermissionError()):\n cmd_supervisor_control("capture", "123", "456")\n'
        proc = subprocess.run([sys.executable, '-c', source], capture_output=True, timeout=3)
        self.assertNotEqual(0, proc.returncode); self.assertNotEqual(3, proc.returncode)

    def test_starttime_change_after_pidfd_open_does_not_signal(self):
        definition = self.helper.split('\nCOMMANDS = {', 1)[0]
        source = definition + '''
from unittest import mock
import signal
with mock.patch('__main__.supervisor_identity', side_effect=[(456, 1, b'S'), (456, 2, b'S')]), mock.patch('os.pidfd_open', return_value=88), mock.patch('os.close'), mock.patch('signal.pidfd_send_signal') as sent:
 try:
  cmd_supervisor_control('kill', '123', '456', '1')
 except SystemExit as exc:
  assert exc.code == 3
 else:
  raise AssertionError('changed identity accepted')
 sent.assert_not_called()
'''
        proc = subprocess.run([sys.executable, '-c', source], capture_output=True, timeout=3)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_same_uid_result_edit_is_a_documented_trust_limit(self):
        # 同じ UID が結果の保持領域まで書ける場合、nonce を秘密とは扱えない。
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / 'result'
            proc = subprocess.run([sys.executable, str(SCRIPT), '--result', str(result), '--nonce', 'held-nonce',
                                   '--timeout', '3', '--grace', '.1', '--', sys.executable, '-c', 'pass'],
                                  capture_output=True, timeout=5)
            self.assertEqual(0, proc.returncode, proc.stderr)
            original = json.loads(result.read_text()); self.assertEqual(0, original['returncode'])
            original['returncode'] = 7
            result.write_text(json.dumps(original))
            checked = subprocess.run([sys.executable, '-c', self.helper, 'supervisor-result', str(result), 'held-nonce'],
                                     capture_output=True, timeout=3)
            self.assertEqual(0, checked.returncode); self.assertEqual(b'7 0', checked.stdout.strip())
