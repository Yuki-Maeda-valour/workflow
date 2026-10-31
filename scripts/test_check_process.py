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
LINUX = sys.platform.startswith('linux')
DARWIN = sys.platform == 'darwin'


def module():
    spec = importlib.util.spec_from_file_location('test_check_process_helper', SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


# 不在の確かめ方と後始末。test_review_guard.py にも同じ関数を置く(試験ファイルはそれぞれ単独で読み込まれる)。
def probe(send, target):
    try:
        send(target, 0)
    except ProcessLookupError:
        return 'absent'
    except PermissionError:
        return 'denied'
    return 'alive'


def assert_absent_now(pid, send=os.kill):
    # 製品が「止めて回収した」と返した直後に使う。生きていれば待たずに失敗にする。
    # EPERM だけは macOS の回収直後の一過性の応答として、1 秒以内で 0.02 秒ごとに確かめ直す。
    deadline = time.monotonic() + 1
    while True:
        state = probe(send, pid)
        if state == 'absent':
            return
        if state == 'alive':
            raise AssertionError(f'{pid} が残っている')
        if time.monotonic() >= deadline:
            raise AssertionError(f'{pid} の不在を 1 秒以内に確かめられない(EPERM が続く)')
        time.sleep(.02)


def assert_group_absent_now(pgid):
    assert_absent_now(pgid, os.killpg)


def wait_absent(pid, timeout=5, send=os.kill):
    # 自分で終わるはずのプロセスと後始末だけに使う。期限まで待つ。
    deadline = time.monotonic() + timeout
    while probe(send, pid) != 'absent':
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
    return True


def record(path):
    # 子と孫が自分の pid と pgid を JSON で書く 1 文。書きかけを読ませないよう、置き換えで書く。
    part = str(path) + '.part'
    return (f"Path({part!r}).write_text(json.dumps({{'pid': os.getpid(), 'pgid': os.getpgrp()}})); "
            f"os.replace({part!r}, {str(path)!r})")


def read_record(path):
    try:
        value = json.loads(Path(path).read_text())
        return int(value['pid']), int(value['pgid'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def command_line(pid):
    found = subprocess.run(['ps', '-ww', '-o', 'command=', '-p', str(pid)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
    return found.stdout


def owned(root, pid, pgid):
    # 信号を送る直前に、今の pgid が記録と同じで、コマンド行に試験の領域を含むかを確かめる。
    try:
        if os.getpgid(pid) != pgid:
            return False
    except OSError:
        return False
    return str(root) in command_line(pid)


def cleanup(root, stop=(), children=(), groups=(), records=(), raw=()):
    # 試験が起動したものだけを止めて回収し、残ったものの番号を返す。
    # stop: 監督(run-checks)の Popen。children: 試験の直接の子の Popen(差し替えで捕まえた anchor・無関係なプロセス)。
    # groups: 元の群の pgid を書いた記録。records: JSON の記録。raw: 素の pid(既存の Linux の試験の形)。
    found = [value for value in map(read_record, records) if value]
    groups = [value[1] for value in map(read_record, groups) if value]
    pids = [pid for pid, _ in found] + list(raw)
    # 手順 1: 監督がまだ動いていれば、TERM で製品の停止の道を先に通す。
    for process in stop:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    # 手順 2: 元の群の先頭が生きていて、コマンド行に試験の領域を含むときだけ、群ごと止める。
    for pgid in groups:
        if owned(root, pgid, pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    # 手順 3: 記録した各プロセスを止める。素の pid は、まだ終わっていない自分の子だけ。
    for pid, pgid in found:
        if owned(root, pid, pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for pid in raw:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if not done:
            os.kill(pid, signal.SIGKILL)
    # 手順 4: 直接の子と、試験のプロセスが引き取った孤児(Linux の subreaper)を回収する。
    for process in children:
        if process.poll() is None:
            process.kill()
        process.wait()
    for pid in pids:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    # 手順 5: 記録したすべての pid と元の群が不在になるまで待つ。
    left = [pid for pid in pids if not wait_absent(pid)]
    return left + [pgid for pgid in groups if not wait_absent(pgid, send=os.killpg)]


@unittest.skipUnless(LINUX, 'Linux 専用: subreaper・pidfd・/proc で実プロセスと POSIX 方式を検査')
class CheckProcessLinuxTest(unittest.TestCase):
    def setUp(self):
        self.helper = module()
        self.tmp = tempfile.TemporaryDirectory(prefix='wf-proc-test-')
        self.root = Path(os.path.realpath(self.tmp.name))
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

    def test_posix_term_obeying_command_keeps_anchor_until_kill(self):
        # #289 の後始末の順を Linux でも確かめる。Linux は終了済みだけの群への KILL を拒まないので、#289 の症状は出ない。
        # 代わりに anchor の終了値で順を確かめる。検証の親が通知の読み口を群の停止まで開いていれば、anchor は
        # TERM で終わったコマンドの通知を書けて KILL で止まる。先に閉じる形では、通知を書けずに終了値 97 で終わる。
        # grace は、先に閉じる形の anchor が KILL より先に終わるのに十分な長さにする。
        # run_code はパスをコマンド行に入れないので、注釈に試験の領域のパスを入れる(ps の確認で見つかるように)。
        anchors = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            anchors.append(process)
            return process
        with mock.patch.object(self.helper.subprocess, 'Popen', side_effect=launch):
            result = self.run_code(f"import time; time.sleep(3)  # {self.root}", timeout=.3, method='posix', grace=.5)
        self.assertTrue(result['timed_out'])
        self.assertEqual('original-process-group-only', result['recovery_scope'])
        self.assertEqual(-signal.SIGKILL, anchors[0].returncode)

    def test_monitor_launch_failure_proves_no_command_started(self):
        for method in ('linux', 'posix'):
            with mock.patch.object(self.helper, 'launch_monitor', side_effect=self.helper.LaunchError):
                result = self.run_code('raise AssertionError("not started")', method=method)
            self.assertEqual(127, result['returncode'])
            self.assertEqual('no-command-started', result['recovery_scope'])


class CheckProcessCommonTest(unittest.TestCase):
    # OS に依存しない試験。subreaper は設定しない。
    def setUp(self):
        self.helper = module()
        self.tmp = tempfile.TemporaryDirectory(prefix='wf-proc-test-')
        self.root = Path(os.path.realpath(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_code(self, code, timeout=2, method='linux', grace=.05):
        control = self.root / ('control-' + str(time.monotonic_ns()))
        control.mkdir()
        with self.helper.StopSignals() as stop:
            return self.helper.run_command([sys.executable, '-c', code, str(self.root)], self.root, os.environ.copy(),
                                           timeout, control, stop, method=method, grace=grace)

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


def ignoring_child(path, natural, seconds=3):
    # TERM を無視し、記録を書いて眠り、自分で終わる子。自然に終わる直前に、自然終了の印を書く。
    return ("import json, os, signal, time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, signal.SIG_IGN); {record(path)}; {sleep_then_mark(seconds, natural)}")


def sleep_then_mark(seconds, natural):
    # 眠った後、自然に終わる直前に自然終了の印を書く文。製品に止められれば印は書かれない。
    return f"time.sleep({seconds}); Path({str(natural)!r}).write_text('natural')"


def send_self(number):
    # 試験のプロセス自身へ信号を送る action。
    def send():
        os.kill(os.getpid(), number)
        return True
    return send


def parent(child, wait, then=''):
    # 子を起動し、子の記録を見てから先へ進む親。3 秒で記録が来なければ終了 3 で止まる。
    names = [str(path) for path in wait]
    return f"""import json, os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {child!r}])
end = time.monotonic() + 3
while not all(Path(name).exists() for name in {names!r}):
    if time.monotonic() >= end:
        raise SystemExit(3)
    time.sleep(.01)
{then}
"""


@unittest.skipUnless(DARWIN, 'macOS 専用: POSIX 方式の監督を実プロセスで検査')
class CheckProcessDarwinTest(unittest.TestCase):
    def setUp(self):
        self.helper = module()
        self.tmp = tempfile.TemporaryDirectory(prefix='wf-proc-test-')
        self.root = Path(os.path.realpath(self.tmp.name))
        self.records = []
        self.naturals = []
        self.children = []
        self.threads = []
        self.lock = threading.Lock()
        self.active = False

    def tearDown(self):
        try:
            for thread in self.threads:
                thread.join(5)
            left = cleanup(self.root, children=self.children, groups=self.records, records=self.records)
        finally:
            self.tmp.cleanup()
        self.assertFalse([thread for thread in self.threads if thread.is_alive()], '試験の補助 thread が残った')
        self.assertEqual([], left, '試験のプロセスが残った')

    def path(self, name):
        # 記録ファイルの場所。後始末は、子の記録の pgid を元の群として扱う(群を抜けた子は自分の群)。
        value = self.root / name
        self.records.append(value)
        return value

    def pid_of(self, path):
        found = read_record(path)
        self.assertIsNotNone(found, f'{path.name} の記録が無い')
        return found[0]

    def natural(self, name):
        # 製品に止められるはずのプロセスが、自然に終わる直前に書く印の場所。
        value = self.root / (name + '-natural')
        self.naturals.append(value)
        return value

    def assert_not_natural(self, paths=None):
        # 製品が戻った直後に使う。製品が止めずに、自然に終わるのを待って戻ったなら、印がある。
        found = [path.name for path in (self.naturals if paths is None else paths) if path.exists()]
        self.assertEqual([], found, '止めずに自然に終わるのを待った')

    def thread(self, function):
        value = threading.Thread(target=function)
        value.start()
        self.threads.append(value)

    def when_recorded(self, path, action, wait=2):
        # 記録を見てから action を行う。監督が動いている間(StopSignals が受けている間)だけ行う。
        # 待ちは、品質コマンドが自分で終わるより短くする。action が実際に動いたら、返す Event を立てる。
        fired = threading.Event()
        def run():
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                with self.lock:
                    if path.exists() and self.active:
                        if action():
                            fired.set()
                        return
                time.sleep(.01)
        self.thread(run)
        return fired

    def unrelated(self):
        # 試験の直接の子として別の群で起動する無関係なプロセス。試験が必ず止めて回収する。
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(self.root)],
                                   start_new_session=True)
        self.children.append(process)
        return process

    def capture_anchor(self):
        # anchor の Popen を捕まえる。製品が回収しない経路でも、試験が最後に wait で回収する。
        original = subprocess.Popen
        anchors = []
        def launch(argv, *args, **kwargs):
            process = original(argv, *args, **kwargs)
            if '--anchor-fd' in argv:
                anchors.append(process)
                self.children.append(process)
            return process
        return anchors, mock.patch.object(self.helper.subprocess, 'Popen', side_effect=launch)

    def run_code(self, code, timeout=2, grace=.05):
        control = self.root / ('control-' + str(time.monotonic_ns()))
        control.mkdir()
        with self.helper.StopSignals() as stop:
            with self.lock:
                self.active = True
            try:
                return self.helper.run_command([sys.executable, '-c', code, str(self.root)], self.root,
                                               os.environ.copy(), timeout, control, stop, method='posix', grace=grace)
            finally:
                with self.lock:
                    self.active = False

    def test_normal_exit_codes_output_and_missing_command(self):
        import hashlib
        for rc in (0, 7):
            with self.subTest(rc=rc):
                result = self.run_code(f"import sys; print('out'); print('err', file=sys.stderr); sys.exit({rc})")
                self.assertEqual(rc, result['returncode'])
                self.assertEqual(hashlib.sha256(b'out\n').hexdigest(), result['stdout_sha256'])
                self.assertEqual(hashlib.sha256(b'err\n').hexdigest(), result['stderr_sha256'])
                self.assertFalse(result['timed_out'])
        control = self.root / 'missing'
        control.mkdir()
        with self.helper.StopSignals() as stop:
            result = self.helper.run_command([str(self.root / 'absent')], self.root, os.environ.copy(),
                                             1, control, stop, method='posix', grace=.01)
        self.assertEqual(127, result['returncode'])
        self.assertTrue(result['clean'])

    def test_parent_exit_stops_remaining_child(self):
        child = self.path('child')
        started = time.monotonic()
        result = self.run_code(parent(ignoring_child(child, self.natural('child')), [child]))
        self.assert_not_natural()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(0, result['returncode'])
        self.assertEqual('original-process-group-only', result['recovery_scope'])
        assert_absent_now(self.pid_of(child))

    def test_timeout_stops_remaining_child(self):
        child = self.path('child')
        code = parent(ignoring_child(child, self.natural('child')), [child], sleep_then_mark(3, self.natural('parent')))
        result = self.run_code(code, timeout=1)
        self.assert_not_natural()
        self.assertTrue(result['timed_out'])
        assert_absent_now(self.pid_of(child))

    def test_parent_term_int_hup_are_interrupted_and_child_stopped(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=number.name):
                child = self.path('child-' + number.name)
                naturals = [self.natural('child-' + number.name), self.natural('parent-' + number.name)]
                code = parent(ignoring_child(child, naturals[0]), [child], sleep_then_mark(3, naturals[1]))
                fired = self.when_recorded(child, send_self(number))
                with self.assertRaisesRegex(self.helper.Interrupted, f'終了通知 {int(number)} を受けた'):
                    self.run_code(code)
                self.assert_not_natural(naturals)
                assert_absent_now(self.pid_of(child))
                self.assertTrue(fired.is_set())

    def test_anchor_kill_is_failure_and_unrelated_is_untouched(self):
        child = self.path('child')
        unrelated = self.unrelated()
        try:
            anchors, launch = self.capture_anchor()
            def kill():
                # 製品がまだ回収していない anchor にだけ送る(poll は呼ばない。製品の wait を横取りしないため)。
                if anchors and anchors[0].returncode is None:
                    os.kill(anchors[0].pid, signal.SIGKILL)
                    return True
                return False
            fired = self.when_recorded(child, kill)
            code = parent(ignoring_child(child, self.natural('child')), [child], sleep_then_mark(3, self.natural('parent')))
            with launch, self.assertRaises(self.helper.RecoveryError) as caught:
                self.run_code(code, timeout=10)
            self.assert_not_natural()
            assert_absent_now(self.pid_of(child))
            self.assertTrue(fired.is_set(), 'anchor へ SIGKILL を送っていない')
            self.assertIn('anchor が', str(caught.exception))
            self.assertNotIn('Operation not permitted', str(caught.exception))
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()
        anchors[0].wait()

    def test_group_escape_is_explicitly_outside_guarantee(self):
        child = self.path('escaped')
        marker = self.root / 'marker'
        code = ("import json, os, time; from pathlib import Path; os.setsid(); "
                f"{record(child)}; time.sleep(.8); Path({str(marker)!r}).write_text('escaped')")
        result = self.run_code(parent(code, [child]))
        pid = self.pid_of(child)
        self.assertEqual('alive', probe(os.kill, pid))
        self.assertEqual((pid, pid), read_record(child), '子が群を抜けていない')
        self.assertEqual('original-process-group-only', result['recovery_scope'])
        self.assertTrue(wait_absent(pid), '群の外へ移った子が 5 秒以内に終わらない')
        self.assertTrue(marker.exists())

    def test_monitor_launch_failure_proves_no_command_started(self):
        with mock.patch.object(self.helper, 'launch_monitor', side_effect=self.helper.LaunchError):
            result = self.run_code('raise AssertionError("not started")')
        self.assertEqual(127, result['returncode'])
        self.assertEqual('no-command-started', result['recovery_scope'])

    # #289 の回帰の試験: 群の全員(品質コマンド)が TERM で終わっても、検証の親が通知の読み口を群の停止まで
    # 開いているので、anchor は通知を書けて KILL まで残る。群への KILL が EPERM にならず、時間切れは時間切れ、
    # 停止の信号は終了通知になる。読み口を先に閉じる形へ戻ると、anchor が通知を書けずに終わり、群が回収前の
    # 終了済みの anchor だけになる。macOS は KILL を EPERM で返し、RecoveryError(Operation not permitted)で落ちる。
    # 製品の既定の grace(.2 秒)で回す。
    def test_issue289_term_obeying_timeout_is_timed_out(self):
        command = self.path('command')
        anchors, launch = self.capture_anchor()
        code = f"import json, os, time; from pathlib import Path; {record(command)}; {sleep_then_mark(3, self.natural('command'))}"
        with launch:
            result = self.run_code(code, timeout=1, grace=.2)
        self.assert_not_natural()
        self.assertTrue(result['timed_out'])
        self.assertEqual(-signal.SIGKILL, result['returncode'])
        self.assertEqual('posix-process-group', result['recovery_method'])
        assert_absent_now(self.pid_of(command))
        # anchor は KILL まで生きていた(読み口を先に閉じる形では、通知を書けずに終了値 97 で終わる)。
        self.assertEqual(-signal.SIGKILL, anchors[0].returncode)

    def test_issue289_term_obeying_parent_signal_is_interrupted(self):
        # 期限は 10 秒にする(時間切れの経路で通らないように)。信号を実際に送ったことも確かめる。
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=number.name):
                command = self.path('command-' + number.name)
                natural = self.natural('command-' + number.name)
                anchors, launch = self.capture_anchor()
                fired = self.when_recorded(command, send_self(number))
                code = f"import json, os, time; from pathlib import Path; {record(command)}; {sleep_then_mark(2.5, natural)}"
                with launch, self.assertRaisesRegex(self.helper.Interrupted, f'終了通知 {int(number)} を受けた') as caught:
                    self.run_code(code, timeout=10, grace=.2)
                self.assert_not_natural([natural])
                self.assertTrue(fired.is_set(), f'{number.name} を送っていない')
                self.assertNotIn('Operation not permitted', str(caught.exception))
                assert_absent_now(self.pid_of(command))
                # anchor は KILL まで生きていた。
                self.assertEqual(-signal.SIGKILL, anchors[0].returncode)


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

    def test_permission_error_after_reap_retries_until_absence(self):
        helper = module()
        events = []
        process = mock.Mock(pid=42)
        process.wait.side_effect = lambda **_: events.append('reap')
        probes = iter([PermissionError(), None, ProcessLookupError()])
        def send(pid, number):
            events.append(number)
            if number == 0:
                error = next(probes)
                if error is not None:
                    raise error
        with mock.patch.object(helper.os, 'killpg', side_effect=send), \
             mock.patch.object(helper.time, 'sleep'), \
             mock.patch.object(helper.time, 'monotonic', side_effect=[0, 1, 2]):
            helper.stop_group(process, .01)
        self.assertEqual([signal.SIGTERM, signal.SIGKILL, 'reap', 0, 0, 0], events)
        process.wait.assert_called_once_with(timeout=2)
        process.poll.assert_not_called()
        process.communicate.assert_not_called()

    def test_persistent_permission_error_after_reap_is_failure(self):
        helper = module()
        process = mock.Mock(pid=42)
        def send(pid, number):
            if number == 0:
                raise PermissionError()
        with mock.patch.object(helper.os, 'killpg', side_effect=send) as killpg, \
             mock.patch.object(helper.time, 'sleep'), \
             mock.patch.object(helper.time, 'monotonic', side_effect=[0, 1, 5]):
            with self.assertRaisesRegex(helper.RecoveryError, 'プロセス群の不在を確認できない'):
                helper.stop_group(process, .01)
        self.assertEqual([mock.call(42, signal.SIGTERM), mock.call(42, signal.SIGKILL),
                          mock.call(42, 0), mock.call(42, 0)], killpg.call_args_list)
        process.wait.assert_called_once_with(timeout=2)

    def test_permission_error_before_reap_is_not_retried(self):
        for denied in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=denied):
                helper = module()
                process = mock.Mock(pid=42)
                def send(pid, number):
                    if number == denied:
                        raise PermissionError()
                with mock.patch.object(helper.os, 'killpg', side_effect=send) as killpg, \
                     mock.patch.object(helper.time, 'sleep'):
                    with self.assertRaises(PermissionError):
                        helper.stop_group(process, .01)
                expected = [mock.call(42, signal.SIGTERM)]
                if denied == signal.SIGKILL:
                    expected.append(mock.call(42, signal.SIGKILL))
                self.assertEqual(expected, killpg.call_args_list)
                process.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
