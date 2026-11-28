"""共通の補助 support_proc.py の、止め方・生死の確かめ・疑似端末の読み切りの関数を確かめる(#532)。

- 偽の時計と偽の killpg で、Mac(XNU)の EPERM の扱いと、ESRCH の厳しさを確かめる。
- support_native.py の写し(_signal_group)と、同じ場面の表で同じ動きをすることを確かめる。
- 本物の子と疑似端末で、状態の読み・状態の待ち・目印の読み切りを確かめる。
- 比較器の群へ送る信号が素の os.killpg に戻らないことと、移した所の数を、コードを動かさずに読んで確かめる。

関数が無い版でも ERROR にせず AssertionError の FAIL で落ちるように、新しい名前は getattr で読む
(修正の前に落ちることを確かめるため)。
"""
import ast
import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

try:
    import support_proc
except ModuleNotFoundError as error:
    if error.name != 'support_proc':
        raise
    from scripts import support_proc
try:
    import support_native
except ModuleNotFoundError as error:
    if error.name != 'support_native':
        raise
    from scripts import support_native

ROOT = Path(__file__).resolve().parents[1]
LINUX = sys.platform.startswith('linux')


def need(test, name):
    """support_proc の新しい名前を読む。無ければ FAIL にする(ERROR にしない)。"""
    value = getattr(support_proc, name, None)
    if value is None:
        test.fail(f'support_proc に {name} が無い')
    return value


class FakeClock:
    """差し替えた time.sleep の引数の分だけ進む時計。0 から始め、上限は無い(test_support_native.py と同じ形)。"""

    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeKillpg:
    """呼ばれた (pid, 信号, 時計) を控え、outcomes の順に答える killpg。最後の答えを繰り返す。

    答えは 'EPERM'・'ESRCH'・'EINVAL'・None(送れた)。送り直しが止まらない変異でもすぐ FAIL で止まるよう、
    1000 回を超えて呼ばれたら AssertionError を投げる。
    """

    LIMIT = 1000

    def __init__(self, clock, outcomes):
        self.clock = clock
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, pid, number):
        self.calls.append((pid, number, self.clock.now))
        if len(self.calls) > self.LIMIT:
            raise AssertionError(f'killpg が {self.LIMIT} 回を超えて呼ばれた(送り直しが止まらない)')
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if outcome == 'EPERM':
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        if outcome == 'ESRCH':
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))
        if outcome == 'EINVAL':
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))


class FakeProcess:
    """pid と、呼ばれた時計の時刻を控える poll だけを持つ偽の Popen。"""

    pid = 4242

    def __init__(self, clock):
        self.clock = clock
        self.polls = []

    def poll(self):
        self.polls.append(self.clock.now)


class FakeScene:
    """偽の時計・killpg・Popen をまとめ、os・sys・time のモジュールそのものを、呼ぶ間だけ差し替える。"""

    def __init__(self, outcomes, platform):
        self.clock = FakeClock()
        self.killpg = FakeKillpg(self.clock, outcomes)
        self.process = FakeProcess(self.clock)
        self.platform = platform

    def run(self, call):
        with mock.patch.object(os, 'killpg', self.killpg), mock.patch.object(sys, 'platform', self.platform), \
                mock.patch.object(time, 'monotonic', self.clock.monotonic), \
                mock.patch.object(time, 'sleep', self.clock.sleep):
            return call()

    def sent(self):
        return [(pid, number) for pid, number, _ in self.killpg.calls]

    def times(self):
        return [at for _, _, at in self.killpg.calls]


class SignalGroupTest(unittest.TestCase):
    """signal_group が os.killpg と同じに送り、Mac の EPERM だけを期限つきで送り直すことを確かめる。"""

    def send(self, outcomes, platform, **kwargs):
        signal_group = need(self, 'signal_group')
        scene = FakeScene(outcomes, platform)
        if kwargs.pop('reap', True):
            kwargs['reap'] = scene.process.poll
        scene.run(lambda: signal_group(FakeProcess.pid, signal.SIGKILL, **kwargs))
        return scene

    def test_the_wait_is_the_same_as_the_copy(self):
        self.assertEqual(need(self, 'ZOMBIE_GROUP_WAIT'), 5)

    def test_mac_eperm_then_delivered_returns(self):
        try:
            scene = self.send(['EPERM', None], 'darwin')
        except PermissionError as error:
            self.fail(f'Mac の EPERM を送り直さずに投げた: {error!r}')
        self.assertEqual([(FakeProcess.pid, signal.SIGKILL)] * 2, scene.sent())

    def test_mac_eperm_then_esrch_returns(self):
        # 送り直しの ESRCH は、送れる生きたプロセスが無かったこと(Linux で終了済みだけの群へ送ると成功するのと同じ扱い)。
        try:
            scene = self.send(['EPERM', 'EPERM', 'ESRCH'], 'darwin')
        except OSError as error:
            self.fail(f'Mac の EPERM の後の ESRCH で投げた: {error!r}')
        self.assertEqual([(FakeProcess.pid, signal.SIGKILL)] * 3, scene.sent())

    def test_the_first_esrch_raises_on_every_platform(self):
        # 試す信号の厳しさ: 最初の ESRCH は os.killpg と同じに ProcessLookupError を投げる。
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform):
                scene = FakeScene(['ESRCH'], platform)
                signal_group = need(self, 'signal_group')
                with self.assertRaises(ProcessLookupError):
                    scene.run(lambda: signal_group(FakeProcess.pid, signal.SIGKILL, reap=scene.process.poll))
                self.assertEqual([(FakeProcess.pid, signal.SIGKILL)], scene.sent())
                self.assertEqual(([], []), (scene.process.polls, scene.clock.sleeps))

    def test_mac_eperm_that_lasts_past_the_deadline_raises(self):
        scene = FakeScene(['EPERM'], 'darwin')
        signal_group = need(self, 'signal_group')
        with self.assertRaises(PermissionError):
            scene.run(lambda: signal_group(FakeProcess.pid, signal.SIGKILL, reap=scene.process.poll))
        times = scene.times()
        self.assertGreaterEqual(len(times), 2, '期限まで送り直していない')
        self.assertEqual({(FakeProcess.pid, signal.SIGKILL)}, set(scene.sent()))
        # 境目(最初の EPERM から 5 秒)に初めて届いた時に投げる。
        self.assertGreaterEqual(scene.clock.now, 5, '期限の前に投げた')
        self.assertLess(times[-2], 5, '期限を過ぎても送り直した')
        self.assertEqual({.02}, set(scene.clock.sleeps))

    def test_other_platforms_raise_eperm_at_once(self):
        scene = FakeScene(['EPERM'], 'linux')
        signal_group = need(self, 'signal_group')
        with self.assertRaises(PermissionError):
            scene.run(lambda: signal_group(FakeProcess.pid, signal.SIGKILL, reap=scene.process.poll))
        self.assertEqual([(FakeProcess.pid, signal.SIGKILL)], scene.sent())
        self.assertEqual(([], []), (scene.process.polls, scene.clock.sleeps))

    def test_reap_is_called_once_before_each_retry(self):
        scene = self.send(['EPERM', 'EPERM', 'EPERM', None], 'darwin')
        times = scene.times()
        self.assertEqual(4, len(times))
        # 回収は送り直しの前に 1 回ずつ(各 EPERM の時刻に呼び、その後に 0.02 秒待って送り直す)。
        self.assertEqual(times[:-1], scene.process.polls)

    def test_retries_without_reap(self):
        try:
            scene = self.send(['EPERM', 'EPERM', None], 'darwin', reap=False)
        except PermissionError as error:
            self.fail(f'reap が無いと送り直さずに投げた: {error!r}')
        self.assertEqual([(FakeProcess.pid, signal.SIGKILL)] * 3, scene.sent())
        self.assertEqual([], scene.process.polls)


class SignalGroupParityTest(unittest.TestCase):
    """support_proc.signal_group と、support_native.py の写し(_signal_group)が同じ動きをすることを確かめる。

    違うのは、写しの決まり(ESRCH はいつでも黙って戻る)の分だけ: 最初の答えが ESRCH の場面で、
    写しは戻り、signal_group は ProcessLookupError を投げる。
    """

    SCENES = (
        ('darwin', ('EPERM', 'EPERM', 'ESRCH')),
        ('darwin', ('EPERM', None)),
        ('darwin', ('EPERM',)),
        ('darwin', (None,)),
        ('linux', (None,)),
        ('linux', ('EPERM',)),
        ('darwin', ('ESRCH',)),
        ('linux', ('ESRCH',)),
    )

    def observe(self, platform, outcomes, call):
        scene = FakeScene(list(outcomes), platform)
        try:
            scene.run(lambda: call(scene.process))
            outcome = '戻る'
        except OSError as error:
            outcome = type(error).__name__
        return {'killpg': scene.killpg.calls, 'poll': scene.process.polls, 'sleep': scene.clock.sleeps,
                'outcome': outcome}

    def test_the_copy_in_support_native_behaves_the_same(self):
        signal_group = need(self, 'signal_group')
        for platform, outcomes in self.SCENES:
            with self.subTest(platform=platform, outcomes=outcomes):
                copy = self.observe(platform, outcomes,
                                    lambda process: support_native._signal_group(process, signal.SIGKILL))
                common = self.observe(platform, outcomes,
                                      lambda process: signal_group(process.pid, signal.SIGKILL, reap=process.poll))
                if outcomes[0] == 'ESRCH':
                    self.assertEqual(('戻る', 'ProcessLookupError'), (copy['outcome'], common['outcome']))
                    copy['outcome'] = common['outcome']
                self.assertEqual(copy, common)


class GroupGoneTest(unittest.TestCase):
    """group_gone が信号 0 で群の有無を読み、EPERM を不在と読み替えないことを確かめる。"""

    def look(self, outcomes, platform, **kwargs):
        group_gone = need(self, 'group_gone')
        scene = FakeScene(outcomes, platform)
        if kwargs.pop('reap', True):
            kwargs['reap'] = scene.process.poll
        result = scene.run(lambda: group_gone(FakeProcess.pid, **kwargs))
        # 送るのは信号 0 だけ。
        self.assertEqual({(FakeProcess.pid, 0)}, set(scene.sent()))
        return result, scene

    def test_esrch_is_gone(self):
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform):
                result, scene = self.look(['ESRCH'], platform)
                self.assertEqual((True, 'ESRCH'), result)
                self.assertEqual(1, len(scene.sent()))

    def test_a_group_that_takes_the_signal_is_not_gone(self):
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform):
                result, _ = self.look([None], platform)
                self.assertEqual((False, 'alive'), result)

    def test_mac_eperm_that_ends_in_esrch_is_gone(self):
        result, scene = self.look(['EPERM', 'EPERM', 'ESRCH'], 'darwin')
        self.assertEqual((True, 'EPERM 2 回の後 ESRCH'), result)
        self.assertEqual(3, len(scene.sent()))

    def test_mac_eperm_that_ends_in_a_living_group_is_not_gone(self):
        result, _ = self.look(['EPERM', None], 'darwin')
        self.assertEqual((False, 'alive'), result)

    def test_mac_eperm_that_lasts_is_not_gone(self):
        result, scene = self.look(['EPERM'], 'darwin')
        times = scene.times()
        self.assertEqual((False, f'EPERM {len(times)} 回'), result)
        self.assertGreaterEqual(len(times), 2, '期限まで読み直していない')
        self.assertGreaterEqual(scene.clock.now, 5, '期限の前に決めた')
        self.assertLess(times[-2], 5, '期限を過ぎても読み直した')
        self.assertEqual({.02}, set(scene.clock.sleeps))

    def test_eperm_on_other_platforms_is_read_once_and_not_gone(self):
        result, scene = self.look(['EPERM', 'ESRCH'], 'linux')
        self.assertEqual((False, 'EPERM'), result)
        self.assertEqual(1, len(scene.sent()))
        self.assertEqual(([], []), (scene.process.polls, scene.clock.sleeps))

    def test_no_wait_reads_once(self):
        result, scene = self.look(['EPERM', 'ESRCH'], 'darwin', wait=0)
        self.assertEqual((False, 'EPERM 1 回'), result)
        self.assertEqual(1, len(scene.sent()))
        self.assertEqual([], scene.process.polls)

    def test_reap_is_called_before_each_new_look(self):
        result, scene = self.look(['EPERM', 'EPERM', 'EPERM', 'ESRCH'], 'darwin')
        self.assertEqual((True, 'EPERM 3 回の後 ESRCH'), result)
        self.assertEqual(scene.times()[:-1], scene.process.polls)
        result, scene = self.look(['EPERM', 'ESRCH'], 'darwin', reap=False)
        self.assertEqual((True, 'EPERM 1 回の後 ESRCH'), result)

    def test_other_errors_are_raised(self):
        group_gone = need(self, 'group_gone')
        scene = FakeScene(['EINVAL'], 'darwin')
        with self.assertRaises(OSError) as raised:
            scene.run(lambda: group_gone(FakeProcess.pid))
        self.assertEqual(errno.EINVAL, raised.exception.errno)


class ProcessStateTest(unittest.TestCase):
    """本物の子で、状態の 1 文字の読み(process_state)と、状態を待つ wait_state を確かめる。"""

    def child(self, code='import time; time.sleep(60)'):
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL)

        def stop():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
        self.addCleanup(stop)
        return process

    def wait(self, *args, **kwargs):
        """wait_state を呼ぶ。例外を投げる版でも ERROR にせず FAIL にする(wait_state は例外を投げない約束)。"""
        wait_state = need(self, 'wait_state')
        try:
            return wait_state(*args, **kwargs)
        except Exception as error:  # noqa: BLE001
            self.fail(f'wait_state が例外を投げた: {error!r}')

    def test_dead_states(self):
        self.assertEqual((None, 'Z', 'X'), need(self, 'DEAD_STATES'))

    def test_a_stopped_child_is_read_as_stopped(self):
        process_state = need(self, 'process_state')
        need(self, 'wait_state')
        child = self.child()
        os.kill(child.pid, signal.SIGSTOP)
        ok, state = self.wait(child.pid, lambda state: state in ('T', 't'), 10)
        self.assertTrue(ok, f'止めた子の状態が T にならない(状態 {state})')
        self.assertIn(state, ('T', 't'))
        # Linux の機械では、Linux 以外の読み方(ps)も sys.platform を差し替えて読む。
        forms = ['linux', 'darwin'] if LINUX else [sys.platform]
        for form in forms:
            with self.subTest(form=form), mock.patch.object(sys, 'platform', form):
                self.assertIn(process_state(child.pid), ('T', 't'))

    def test_a_reaped_child_has_no_state(self):
        process_state = need(self, 'process_state')
        need(self, 'wait_state')
        child = self.child('pass')
        child.wait(timeout=30)
        self.assertIsNone(process_state(child.pid))
        # accept が None(無いプロセス)を受け入れれば、消えた子で (True, None)。
        self.assertEqual((True, None), self.wait(child.pid, lambda state: state is None, 5))

    def test_the_wait_ends_at_the_limit_without_raising(self):
        need(self, 'wait_state')
        child = self.child()
        started = time.monotonic()
        ok, state = self.wait(child.pid, lambda state: False, 0.2)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertFalse(ok)
        # 最後に読んだ状態を返す(眠る子は S。起きた直後なら R)。
        self.assertIn(state, ('R', 'S', 'D'))

    def test_read_is_used_when_given_and_reads_every_5_milliseconds(self):
        need(self, 'wait_state')
        clock = FakeClock()
        reads = []

        def read(pid):
            reads.append(pid)
            return 'X' if len(reads) >= 3 else 'S'
        dead = need(self, 'DEAD_STATES')
        with mock.patch.object(time, 'monotonic', clock.monotonic), mock.patch.object(time, 'sleep', clock.sleep):
            result = self.wait(4242, lambda state: state in dead, 1, read=read)
            self.assertEqual((True, 'X'), result)
            self.assertEqual([4242] * 3, reads)
            self.assertEqual([.005, .005], clock.sleeps)
            # 期限まで満たさなければ、最後に読んだ状態を返す。
            reads.clear()
            self.assertEqual((False, 'S'), self.wait(4242, lambda state: state == 'Z', .02,
                                                     read=lambda pid: reads.append(pid) or 'S'))
            self.assertGreaterEqual(len(reads), 2)


class ReadUntilMarkerTest(unittest.TestCase):
    """本物の疑似端末で、目印まで読み切る read_until_marker の順番と、失敗の理由を確かめる(#519・#522 の形)。"""

    PREFIX = b'TEST-PTY-END-'

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)

    def read(self, finished, **kwargs):
        read_until_marker = need(self, 'read_until_marker')
        kwargs.setdefault('prefix', self.PREFIX)
        kwargs.setdefault('select_wait', 0.02)
        # 変異で止まらない形になっても、全体の期限で戻る(既定 10 秒)。
        kwargs.setdefault('deadline', time.monotonic() + 10)
        return read_until_marker(self.master, self.slave, finished, **kwargs)

    def assertRead(self, got, data, reason, number, written):
        self.assertEqual((data, reason, number, written), (got.data, got.reason, got.errno, got.written))

    def test_output_before_the_marker_is_read_whole_and_the_marker_is_removed(self):
        os.write(self.slave, b'first\nsecond')  # 改行を含まない末尾でも、目印の前に全部出る
        got = self.read(lambda: True)
        self.assertRead(got, b'first\r\nsecond', None, None, True)  # 端末が LF を CRLF にする
        self.assertNotIn(self.PREFIX, got.data)

    def test_output_written_after_an_empty_wait_is_read_before_the_marker(self):
        # 最初の select の呼び出しで子が最後の出力を書いて終わる順番を起こし、1 回に 4 バイトだけ読む。
        # 目印を書く時点に、端末の読み残しが無いこと(#522)。
        real_select, real_write, real_read = select.select, os.write, os.read
        calls = []
        ended = []
        leftovers = []

        def fake_select(rlist, wlist, xlist, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                real_write(self.slave, b'TAIL-DATA')
                ended.append(True)
                return [], [], []
            return real_select(rlist, wlist, xlist, timeout)

        def fake_write(fd, data):
            if data.startswith(self.PREFIX):
                leftovers.append(bool(real_select([self.master], [], [], 0)[0]))
            return real_write(fd, data)
        with mock.patch.object(select, 'select', fake_select), mock.patch.object(os, 'write', fake_write), \
                mock.patch.object(os, 'read', lambda fd, n: real_read(fd, min(n, 4))):
            got = self.read(lambda: bool(ended))
        self.assertRead(got, b'TAIL-DATA', None, None, True)
        self.assertEqual([False], leftovers)

    def test_marker_split_across_reads_is_found_and_removed(self):
        real_read = os.read
        os.write(self.slave, b'abc')
        with mock.patch.object(os, 'read', lambda fd, n: real_read(fd, 1)):
            got = self.read(lambda: True)
        self.assertRead(got, b'abc', None, None, True)

    def test_select_waits_as_told(self):
        real_select = select.select
        waits = []

        def recording(rlist, wlist, xlist, timeout=None):
            waits.append(timeout)
            return real_select(rlist, wlist, xlist, timeout)
        with mock.patch.object(select, 'select', recording):
            got = self.read(lambda: True, select_wait=0.07)
        self.assertIsNone(got.reason)
        self.assertTrue(waits)
        self.assertEqual({0.07}, set(waits))

    def test_the_deadline_before_the_marker_is_written(self):
        os.write(self.slave, b'partial')
        started = time.monotonic()
        got = self.read(lambda: False, deadline=started + 0.3)
        self.assertRead(got, b'partial', 'deadline', None, False)
        self.assertLess(time.monotonic() - started, 5)

    def test_the_deadline_after_the_marker_is_written(self):
        # 目印が端末に届かない(書いたことにして捨てる)。
        with mock.patch.object(os, 'write', lambda fd, data: len(data)):
            got = self.read(lambda: True, deadline=time.monotonic() + 0.3)
        self.assertRead(got, b'', 'deadline', None, True)

    def test_the_marker_time_limit(self):
        started = time.monotonic()
        with mock.patch.object(os, 'write', lambda fd, data: len(data)):
            got = self.read(lambda: True, marker_timeout=0.3, deadline=started + 3)
        self.assertRead(got, b'', 'marker-timeout', None, True)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_a_read_error_before_the_marker(self):
        failing = mock.Mock(side_effect=OSError(errno.EIO, os.strerror(errno.EIO)))
        with mock.patch.object(os, 'read', failing), \
                mock.patch.object(select, 'select', return_value=([self.master], [], [])):
            got = self.read(lambda: True)
        self.assertRead(got, b'', 'read-error', errno.EIO, False)

    def test_eof_before_the_marker(self):
        with mock.patch.object(os, 'read', return_value=b''), \
                mock.patch.object(select, 'select', return_value=([self.master], [], [])):
            got = self.read(lambda: True)
        self.assertRead(got, b'', 'eof', None, False)

    def test_a_marker_that_cannot_be_written(self):
        os.write(self.slave, b'seen')

        def failing(fd, data):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_read = os.read
        with mock.patch.object(os, 'read', lambda fd, n: real_read(fd, n)), mock.patch.object(os, 'write', failing):
            got = self.read(lambda: True)
        self.assertRead(got, b'seen', 'write-error', errno.EIO, False)


class XnuZombieGroupKillpg:
    """Linux の上で、Mac(XNU の killpg1)の規則をまねる killpg(test_support_native.py の #534 の形を写した)。

    /proc/<pid>/stat の最後の ')' の後の欄(状態・親・群)で群の全員を集める。群が空なら ESRCH、
    全員の状態が Z か X(回収前の終了済み)なら EPERM(数を数える)、ほかは本物の os.killpg に渡す。
    """

    def __init__(self):
        self.real = os.killpg
        self.eperm = 0
        self.last = None  # 最後に EPERM を返した時の群の (pid, 状態, 親)。

    @staticmethod
    def members(pgid):
        found = []
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                data = Path('/proc', entry, 'stat').read_bytes()
            except OSError:
                continue  # 読む間に消えたプロセス。
            state, parent, group = data[data.rindex(b')') + 2:].split()[:3]
            if int(group) == pgid:
                found.append((int(entry), state.decode(), int(parent)))
        return found

    def __call__(self, pgid, number):
        members = self.members(pgid)
        if not members:
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))
        if all(state in ('Z', 'X') for _, state, _ in members):
            self.eperm += 1
            self.last = members
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        return self.real(pgid, number)


@unittest.skipUnless(LINUX, '群の全員の状態を /proc で読む(Linux だけ)')
class MacZombieGroupTest(unittest.TestCase):
    """本物の子の群が回収前の終了済み(ゾンビ)だけになったとき、Mac の規則をまねて確かめる(#526・#527)。"""

    def zombie(self):
        # 新しい群の子を起こし、終わるのを待つ(回収はしない。WNOWAIT)。群には終了済みの子だけが残る。
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'pass'], stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        self.addCleanup(child.wait, 30)
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        return child

    def test_signal_group_returns_after_reaping_the_direct_child(self):
        signal_group = need(self, 'signal_group')
        child = self.zombie()
        rule = XnuZombieGroupKillpg()
        with mock.patch.object(os, 'killpg', rule), mock.patch.object(sys, 'platform', 'darwin'):
            try:
                signal_group(child.pid, signal.SIGKILL, reap=child.poll)
            except PermissionError as error:
                self.fail(f'終了済みの子だけが残る群の EPERM で投げた: {error!r}'
                          f'(EPERM {rule.eperm} 回・最後に見た群: {rule.last})')
        self.assertGreaterEqual(rule.eperm, 1, 'EPERM の場面ができていない')
        self.assertIsNotNone(child.returncode, '直接の子を回収していない')
        support_proc.assert_group_absent_now(child.pid)

    def test_group_gone_is_true_after_reaping_the_direct_child(self):
        group_gone = need(self, 'group_gone')
        child = self.zombie()
        rule = XnuZombieGroupKillpg()
        with mock.patch.object(os, 'killpg', rule), mock.patch.object(sys, 'platform', 'darwin'):
            gone, seen = group_gone(child.pid, reap=child.poll)
        self.assertTrue(gone, f'回収した後も群が残ると読んだ: {seen}(最後に見た群: {rule.last})')
        self.assertRegex(seen, r'^EPERM [0-9]+ 回の後 ESRCH$')
        self.assertGreaterEqual(rule.eperm, 1, 'EPERM の場面ができていない')
        self.assertIsNotNone(child.returncode, '直接の子を回収していない')


# 比較器の群へ送る信号の所(#532 で signal_group へ移した所)。値は (signal_group の呼び出しの数, そのうち reap= を渡す数)。
# R4A の残りの kill_group・group_alive・後始末の killpg は、PermissionError を受け止める意図した形なので移さない
# (#532 の設計「R4A の EPERM の扱いは意図したもの」)。R4U と R4E の自己検査は、信号を送らない(読み切り・群の有無だけ)。
CALL_SITES = {
    'native/ci/check_r4a_rest.py': (0, 0),
    'native/ci/check_r4u.py': (0, 0),
    'native/ci/check_r4g.py': (11, 6),
    'native/ci/test_r4g.py': (2, 2),
    'native/ci/check_r4e.py': (3, 2),
    'native/ci/test_r4e.py': (0, 0),
    'native/ci/check_adopt_candidate.py': (13, 9),
    'native/ci/check_r4b.py': (1, 1),
}
# 素の os.killpg を置いてよい try が受け止める例外。
GUARDS = {'PermissionError', 'OSError'}
TRY_NODES = tuple(node for node in (ast.Try, getattr(ast, 'TryStar', None)) if node is not None)


def caught_names(handler):
    if handler.type is None:
        return set()
    items = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {item.id if isinstance(item, ast.Name) else getattr(item, 'attr', None) for item in items}


def own_nodes(statement):
    """文の中の式の節(入れ子の文の中は含めない)。"""
    stack = list(ast.iter_child_nodes(statement))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.stmt):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def is_killpg(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == 'killpg'
    return (isinstance(func, ast.Attribute) and func.attr == 'killpg'
            and (isinstance(func.value, ast.Name) and func.value.id == 'os'
                 or isinstance(func.value, ast.Attribute) and func.value.attr == 'os'))


def is_signal_group(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == 'signal_group'
            or isinstance(func, ast.Attribute) and func.attr == 'signal_group')


def unguarded_killpg(source):
    """PermissionError か OSError を受け止める try の本体の直下(except・finally の中を除く)の外にある、
    素の os.killpg の呼び出しの行の並び。"""
    tree = ast.parse(source)
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, TRY_NODES) and any(caught_names(handler) & GUARDS for handler in node.handlers):
            for statement in node.body:
                guarded.update(id(inner) for inner in own_nodes(statement) if is_killpg(inner))
    return sorted(node.lineno for node in ast.walk(tree) if is_killpg(node) and id(node) not in guarded)


def signal_group_calls(source):
    calls = [node for node in ast.walk(ast.parse(source)) if is_signal_group(node)]
    return len(calls), sum(1 for call in calls if any(keyword.arg == 'reap' for keyword in call.keywords))


class KillpgCallSitesTest(unittest.TestCase):
    """比較器の群へ送る信号が、素の os.killpg に戻らないことと、移した所の数を、コードを読んで確かめる(#532)。

    確かめるのは、素の os.killpg が受け止めの無い所に無いことと、signal_group の呼び出しと reap= の数だけ。
    """

    # 読み方そのものの確かめに使う例: 受け止める try の本体の直下だけを通し、except・finally の中・
    # ProcessLookupError だけの try・入れ子の文の中・受け止めの無い所は通さない。
    SAMPLE = (
        'try:\n    os.killpg(1, 9)\nexcept (ProcessLookupError, PermissionError):\n    pass\n'   # 1 行目から
        'try:\n    os.killpg(1, 9)\nexcept OSError:\n    pass\n'                                # 5
        'try:\n    os.killpg(1, 0)\nexcept ProcessLookupError:\n    pass\n'                     # 9
        'try:\n    pass\nexcept TimeoutError:\n    os.killpg(1, 9)\n'                           # 13
        'try:\n    pass\nexcept OSError:\n    os.killpg(1, 9)\n'                                # 17
        'try:\n    pass\nfinally:\n    os.killpg(1, 9)\n'                                       # 21
        'try:\n    if x:\n        os.killpg(1, 9)\nexcept OSError:\n    pass\n'                 # 25
        'os.killpg(1, 9)\n'                                                                     # 30
        'self.m.os.killpg(1, 9)\n'                                                              # 31
        'signal_group(1, 9)\nself.m.signal_group(1, 9, reap=p.poll)\n')                         # 32

    def test_raw_killpg_is_only_inside_a_guarded_try(self):
        self.assertEqual([10, 16, 20, 24, 27, 30, 31], unguarded_killpg(self.SAMPLE))
        for name in CALL_SITES:
            with self.subTest(file=name):
                lines = unguarded_killpg((ROOT / name).read_text(encoding='utf-8'))
                if lines:
                    self.fail(f'{name}: 受け止めの無い素の os.killpg がある(signal_group へ移す): '
                              + '・'.join(f'{name}:{line}' for line in lines))

    def test_signal_group_calls_and_reap_per_file(self):
        self.assertEqual((2, 1), signal_group_calls(self.SAMPLE))
        # 合わせて 30 か所(うち reap= は 20)。
        self.assertEqual((30, 20), tuple(map(sum, zip(*CALL_SITES.values()))))
        for name, expected in CALL_SITES.items():
            with self.subTest(file=name):
                found = signal_group_calls((ROOT / name).read_text(encoding='utf-8'))
                self.assertEqual(expected, found, f'{name}: (signal_group の呼び出し, うち reap=) の数が違う')


if __name__ == '__main__':
    unittest.main()
