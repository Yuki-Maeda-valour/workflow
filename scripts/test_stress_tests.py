"""stress-tests.py(負荷の下で何度も流す道具)の自己検査(#532)。

一時フォルダに小さなテストを置き、道具を別のプロセスで流して、回数・終了コード・落ちた回の出力・負荷の子の片付け・
1 回の上限(--timeout)を確かめる。
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest

try:
    import support_proc
except ModuleNotFoundError as error:
    if error.name != 'support_proc':
        raise
    from scripts import support_proc

TOOL = Path(__file__).resolve().with_name('stress-tests.py')

# 流すたびに、数えるファイルへ 1 文字を足す(同時に流しても数え違えないよう、ファイルを錠で守る)。
# N 回目(数えた後の文字数が N)だけ落ちる。N が 0 なら落ちない。ERROR が真なら、落ちる代わりに例外を投げる。
COUNTING_TEST = '''\
import fcntl
from pathlib import Path
import unittest

COUNTER = Path(__file__).with_name('count')
FAIL_AT = {fail_at}
ERROR = {error}


class Counted(unittest.TestCase):
    def test_counted(self):
        with open(COUNTER, 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            number = len(handle.read()) + 1
            handle.write('x')
        if number == FAIL_AT:
            if ERROR:
                raise RuntimeError(f'COUNTED-ERROR-{{number}}')
            self.fail(f'COUNTED-FAIL-{{number}}')
'''

# 読み込んだときに自分の群の番号を書き(1 回の上限より十分前に書くため)、テストでは同じ群に孫(長く眠る)を起こし、
# 自分も長く眠る(1 回の上限に当たる)。
SLEEPING_TEST = '''\
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
(HERE / 'group.part').write_text(str(os.getpgrp()))
os.replace(HERE / 'group.part', HERE / 'group')


class Sleeping(unittest.TestCase):
    def test_sleeping(self):
        subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL)
        time.sleep(60)
'''


class StressTestsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wf-stress-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def counting(self, fail_at=0, error=False):
        (self.root / 'test_counted.py').write_text(COUNTING_TEST.format(fail_at=fail_at, error=error),
                                                    encoding='utf-8')

    def count(self):
        path = self.root / 'count'
        return len(path.read_text()) if path.exists() else 0

    def stress(self, *args, name='test_counted'):
        try:
            return subprocess.run([sys.executable, '-B', str(TOOL), '--dir', str(self.root), *args, name],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as error:
            # 止まった道具は subprocess.run が止める(負荷の子は、親が変わったのを見て 1 秒ほどで終わる)。
            self.fail(f'道具が 60 秒で終わらない: {error.stdout!r}')

    def test_all_runs_pass_exits_zero_and_runs_the_given_number_of_times(self):
        self.counting()
        result = self.stress('--repeat', '5', '--jobs', '3')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(5, self.count())
        self.assertRegex(result.stdout.splitlines()[-1], r'^結果: 合格 5 回・失敗 0 回\(5 回中\) / ')
        self.assertNotIn('最初に落ちた回', result.stdout)

    def test_one_failed_run_exits_one_and_shows_its_output(self):
        self.counting(fail_at=3)
        result = self.stress('--repeat', '4', '--jobs', '2')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertEqual(4, self.count())
        self.assertRegex(result.stdout.splitlines()[-1], r'^結果: 合格 3 回・失敗 1 回\(4 回中\) / ')
        self.assertRegex(result.stdout, r'最初に落ちた回: [1-4] 回目\(終了コード 1\)')
        self.assertIn('AssertionError: COUNTED-FAIL-3', result.stdout)

    def test_an_error_is_a_failed_run(self):
        self.counting(fail_at=1, error=True)
        result = self.stress('--repeat', '2', '--jobs', '1')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, self.count())
        self.assertRegex(result.stdout.splitlines()[-1], r'^結果: 合格 1 回・失敗 1 回\(2 回中\) / ')
        self.assertIn('最初に落ちた回: 1 回目(終了コード 1)', result.stdout)
        self.assertIn('RuntimeError: COUNTED-ERROR-1', result.stdout)

    def test_load_children_do_not_remain_after_the_end(self):
        self.counting()
        result = self.stress('--repeat', '2', '--jobs', '2', '--load', '2')
        started = re.search(r'^負荷: CPU を使い続ける子 2 本を、群 ([0-9]+) で動かす$', result.stdout, re.M)
        self.assertIsNotNone(started, result.stdout + result.stderr)
        pgid = int(started.group(1))
        try:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(2, self.count())
            self.assertIn(f'負荷: 群 {pgid} を止めて回収した', result.stdout)
            # 道具が終わった時点で、負荷の群は無い(EPERM を無いと読み替えない)。
            self.assertEqual('absent', support_proc.probe(os.killpg, pgid), f'負荷の群 {pgid} が残った')
        finally:
            # 道具が負荷の子を止めなかったとき(壊した版)も、この検査が起動したプロセスを残さない。
            if support_proc.probe(os.killpg, pgid) != 'absent':
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                support_proc.wait_absent(pgid, timeout=10, send=os.killpg)

    def test_a_run_past_the_time_limit_fails_and_its_group_does_not_remain(self):
        (self.root / 'test_sleeping.py').write_text(SLEEPING_TEST, encoding='utf-8')
        result = self.stress('--repeat', '1', '--jobs', '1', '--timeout', '1', name='test_sleeping')
        group = self.root / 'group'
        self.assertTrue(group.exists(), result.stdout + result.stderr)
        pgid = int(group.read_text())
        try:
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertRegex(result.stdout, r'(?m)^失敗: 1 回目\(時間切れ・1 秒・[0-9.]+ 秒\)$')
            self.assertIn('最初に落ちた回: 1 回目(時間切れ)', result.stdout)
            self.assertRegex(result.stdout.splitlines()[-1], r'^結果: 合格 0 回・失敗 1 回\(1 回中\) / ')
            # 回の群(テストと、同じ群の孫)は止めて回収した。孫は引き取った側が回収するので、少しだけ待つ。
            self.assertTrue(support_proc.wait_absent(pgid, timeout=10, send=os.killpg), f'回の群 {pgid} が残った')
        finally:
            # 道具が回の群を止めなかったとき(壊した版)も、この検査が起動したプロセスを残さない。
            if support_proc.probe(os.killpg, pgid) != 'absent':
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
                support_proc.wait_absent(pgid, timeout=10, send=os.killpg)


if __name__ == '__main__':
    unittest.main()
