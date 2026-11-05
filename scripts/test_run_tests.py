"""scripts/run-tests.py の回帰テスト。使い捨ての scripts/ に置いた小さなテストのファイルで動かす。"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parent / "run-tests.py"

PASSING = '''
import unittest

class Passing(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertEqual(2, 1 + 1)

    @unittest.skip("理由")
    def test_skipped(self):
        pass
'''

OTHER = '''
import unittest

class Other(unittest.TestCase):
    def test_a(self):
        pass

    def test_b(self):
        pass
'''

FAILING = '''
import unittest

class Failing(unittest.TestCase):
    def test_fails(self):
        self.assertEqual("期待", "実際の値")

    def test_passes(self):
        pass
'''

BROKEN_IMPORT = '''
raise ImportError("読み込めないモジュール")
'''

DIES = '''
import os
import unittest

class Dies(unittest.TestCase):
    def test_exit_without_report(self):
        os._exit(3)
'''

# 流れたテストの名前を、環境変数で渡したファイルへ 1 行ずつ足す(分けたプロセスの重なりと漏れを見る)。
MANY = '''
import os
import unittest

class Many(unittest.TestCase):
    def setUp(self):
        with open(os.environ["RUN_TESTS_LOG"], "a", encoding="utf-8") as stream:
            stream.write(self.id() + "\\n")

''' + "".join(f"    def test_{index}(self):\n        pass\n\n" for index in range(7))


# 自分と、自分が起動した孫のプロセスの pid を書いてから眠る(止めたときに両方が消えるかを見る)。
SLOW = '''
import os
import subprocess
import tempfile
import time
import unittest

class Slow(unittest.TestCase):
    def test_sleep(self):
        # 後片付けで消える一時フォルダ(止め方が SIGINT なら、子は終わる前に消せる)。
        self.held = tempfile.TemporaryDirectory(prefix="held-")
        self.addCleanup(self.held.cleanup)
        child = subprocess.Popen(["sleep", "60"])
        marks = os.environ["RUN_TESTS_MARKS"]
        name = os.path.join(marks, os.path.basename(__file__))
        with open(name + ".tmp", "w", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()} {child.pid}\\n")
        os.replace(name + ".tmp", name + ".pids")
        time.sleep(60)
'''


# 入れ子の実行: テストが「真ん中」のプロセスを同じグループで起動し、真ん中は自分の子を別のグループで起動する。
# 真ん中は SIGINT・SIGTERM・SIGHUP のどれを受けても、少し後片付けをしてから自分の子を止めて回収し、終わる
# (入れ子の run-tests.py と同じ形)。外側が真ん中を途中で SIGKILL すると、別のグループの子が残る。
MIDDLE = r"""
import os, signal, subprocess, sys, time
child = subprocess.Popen(["sleep", "60"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def stop(signum, frame):
    time.sleep(0.5)
    child.terminate()
    child.wait()
    sys.exit(128 + signum)
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, stop)
with open(sys.argv[1] + ".tmp", "w", encoding="utf-8") as stream:
    stream.write(f"{os.getpid()} {child.pid}\n")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(60)
"""

NESTED = '''
import os
import subprocess
import sys
import time
import unittest

class Nested(unittest.TestCase):
    def test_nested(self):
        marks = os.environ["RUN_TESTS_MARKS"]
        # 出力を親の管と分ける(管を持ったままだと、外側が子孫の終わりを管の閉じで待ってしまい、残りを見逃す)。
        subprocess.Popen([sys.executable, "-c", os.environ["RUN_TESTS_MIDDLE"], os.path.join(marks, "nested.pids")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(60)
'''

# SIGINT と SIGTERM を無視する子孫(テストと同じグループ)。2 つの猶予の後の SIGKILL でしか止まらない。
IGNORING = '''
import os
import subprocess
import sys
import time
import unittest

CODE = (
    "import os, signal, sys, time\\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\\n"
    "time.sleep(60)\\n"
)

class Ignoring(unittest.TestCase):
    def test_ignoring(self):
        marks = os.environ["RUN_TESTS_MARKS"]
        subprocess.Popen([sys.executable, "-c", CODE, os.path.join(marks, "ignoring.pids")])
        time.sleep(60)
'''


def process_gone(pid):
    """pid のプロセスが無いか、ゾンビ(親が回収する前の終わったプロセス)なら True。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


class RunTestsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.runner = self.scripts / "run-tests.py"
        shutil.copy2(RUNNER, self.runner)
        self.log = self.root / "ran.txt"

    def write_test(self, name, text):
        (self.scripts / name).write_text(textwrap.dedent(text), encoding="utf-8")

    def set_shards(self, shards):
        """写しの SHARDS を置き換える(本物の表は変えない)。"""
        text = self.runner.read_text(encoding="utf-8")
        start = text.index("SHARDS = {")
        end = text.index("}\n", start) + 2
        self.runner.write_text(text[:start] + f"SHARDS = {shards!r}\n" + text[end:], encoding="utf-8")

    def run_runner(self, *args, flags=("-B",)):
        env = dict(os.environ, RUN_TESTS_LOG=str(self.log))
        return subprocess.run([sys.executable, *flags, str(self.runner), *args], cwd=self.root,
                              capture_output=True, text=True, env=env, timeout=120)

    def test_all_files_pass_and_counts_add_up(self):
        self.write_test("test_passing.py", PASSING)
        self.write_test("test_other.py", OTHER)
        result = self.run_runner("-j", "2")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("2 ファイル・2 プロセス", result.stdout)
        self.assertIn("結果: 5 件 / 失敗 0 件 / エラー 0 件 / skip 1 件", result.stdout)
        self.assertNotIn("異常:", result.stdout)
        # 親に -B を付けたときは、子も __pycache__ を作らない。
        self.assertEqual([], list(self.root.rglob("__pycache__")))

    def test_children_never_write_bytecode_even_without_parent_b(self):
        # テストのファイルが scripts/ の下のモジュールを読み込んでも、子は .pyc を書かない。
        (self.scripts / "helper_module.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.write_test("test_imports.py", '''
import unittest
import helper_module

class Imports(unittest.TestCase):
    def test_value(self):
        self.assertEqual(1, helper_module.VALUE)
''')
        result = self.run_runner(flags=())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("結果: 1 件 / 失敗 0 件", result.stdout)
        self.assertEqual([], list(self.root.rglob("__pycache__")))
        self.assertEqual([], list(self.root.rglob("*.pyc")))

    def test_failure_import_error_and_missing_report_exit_1(self):
        self.write_test("test_passing.py", PASSING)
        self.write_test("test_failing.py", FAILING)
        self.write_test("test_broken.py", BROKEN_IMPORT)
        self.write_test("test_dies.py", DIES)
        result = self.run_runner()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        # 失敗したプロセスの出力を最後にまとめて出す。成功したファイルの出力は出さない。
        self.assertIn("===== test_failing.py(終了コード 1)=====", result.stdout)
        self.assertIn("'期待' != '実際の値'", result.stdout)
        self.assertIn("読み込めないモジュール", result.stdout)
        self.assertIn("異常: test_dies.py: 結果の報告が無い(終了コード 3)", result.stdout)
        self.assertIn("異常: test_broken.py: 失敗(終了コード 1)", result.stdout)
        self.assertNotIn("===== test_passing.py", result.stdout)
        self.assertIn("失敗 1 件 / エラー 1 件", result.stdout)

    def test_shards_run_every_test_exactly_once(self):
        self.write_test("test_many.py", MANY)
        self.set_shards({"test_many.py": 3})
        result = self.run_runner("-j", "3")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for index in range(1, 4):
            self.assertIn(f"test_many.py [{index}/3]", result.stdout)
        ran = self.log.read_text(encoding="utf-8").split()
        self.assertEqual(7, len(ran), ran)
        self.assertEqual(sorted(f"test_many.Many.test_{index}" for index in range(7)), sorted(ran))
        self.assertIn("結果: 7 件 / 失敗 0 件", result.stdout)

    def test_shard_count_mismatch_is_reported(self):
        # 分けたプロセスが 1 つ欠けた状態(配った件数の合計が総数より少ない)を、写しで作る。
        self.write_test("test_many.py", MANY)
        self.set_shards({"test_many.py": 3})
        text = self.runner.read_text(encoding="utf-8")
        old = "for index in range(SHARDS.get(name, 1))]"
        self.assertEqual(1, text.count(old))
        self.runner.write_text(text.replace(old, "for index in range(SHARDS.get(name, 1) - 1)]"), encoding="utf-8")
        result = self.run_runner()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("異常: test_many.py: 分けたプロセスの件数が合わない", result.stdout)

    def test_only_named_files_run(self):
        self.write_test("test_passing.py", PASSING)
        self.write_test("test_failing.py", FAILING)
        result = self.run_runner("test_passing.py", "scripts/test_passing.py")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("1 ファイル・1 プロセス", result.stdout)
        self.assertNotIn("test_failing.py", result.stdout)

    def test_interrupt_stops_running_processes_and_skips_queued_ones(self):
        for signum, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(signal=signum.name):
                marks = self.root / f"marks-{signum.name}"
                marks.mkdir()
                # 親の run-tests-* と、子の held-* の置き場。止めた後に空なら、どちらも片付いている。
                temporary = self.root / f"tmp-{signum.name}"
                temporary.mkdir()
                for name in ("test_slow_a.py", "test_slow_b.py", "test_slow_c.py"):
                    self.write_test(name, SLOW)
                env = dict(os.environ, RUN_TESTS_MARKS=str(marks), TMPDIR=str(temporary))
                runner = subprocess.Popen([sys.executable, "-B", str(self.runner), "-j", "1"], cwd=self.root,
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 60
                    while not list(marks.glob("*.pids")) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    started = list(marks.glob("*.pids"))
                    self.assertEqual(["test_slow_a.py.pids"], [path.name for path in started])
                    pids = [int(pid) for pid in started[0].read_text(encoding="utf-8").split()]
                    runner.send_signal(signum)
                    output, _ = runner.communicate(timeout=60)
                finally:
                    if runner.poll() is None:
                        runner.kill()
                        runner.communicate()
                self.assertEqual(code, runner.returncode, output)
                self.assertIn("中断: 動いていた 1 個のプロセスを止め、まだ始めていない 2 個を始めずに捨てた", output)
                self.assertNotIn("結果:", output)
                # 待ち行列の 2 つは始まっていない。動いていたプロセスと、その孫も消えている。
                self.assertEqual(["test_slow_a.py.pids"], [path.name for path in marks.glob("*.pids")])
                deadline = time.monotonic() + 10
                while not all(process_gone(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(all(process_gone(pid) for pid in pids), pids)
                self.assertEqual([], sorted(path.name for path in temporary.iterdir()))

    def interrupt_and_collect_pids(self, test_file, text, mark, signum=signal.SIGTERM):
        """1 つのテストのファイルを流し、mark のファイルが出たら止める。終了コード・出力・pid を返す。

        止めた後に、一時フォルダの置き場(TMPDIR)が空であることも確かめる。
        """
        marks = self.root / "marks"
        marks.mkdir()
        temporary = self.root / "tmp"
        temporary.mkdir()
        self.write_test(test_file, text)
        env = dict(os.environ, RUN_TESTS_MARKS=str(marks), RUN_TESTS_MIDDLE=MIDDLE, TMPDIR=str(temporary))
        runner = subprocess.Popen([sys.executable, "-B", str(self.runner)], cwd=self.root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pids = []
        try:
            deadline = time.monotonic() + 60
            while not (marks / mark).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((marks / mark).exists(), "印のファイルが出ない")
            pids = [int(pid) for pid in (marks / mark).read_text(encoding="utf-8").split()]
            runner.send_signal(signum)
            output, _ = runner.communicate(timeout=60)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.communicate()
            # 落ちたときに子孫を残さない(試すための後片付け)。
            for pid in pids:
                if not process_gone(pid):
                    os.kill(pid, signal.SIGKILL)
        self.assertEqual([], sorted(path.name for path in temporary.iterdir()))
        return runner.returncode, output, pids

    def test_interrupt_waits_for_nested_runner_to_stop_its_own_group(self):
        started = time.monotonic()
        code, output, pids = self.interrupt_and_collect_pids("test_nested.py", NESTED, "nested.pids",
                                                             signum=signal.SIGHUP)
        # 子孫が眠る 60 秒より前に終わる(止めたのであって、眠りが終わるのを待ったのではない)。
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(129, code, output)
        self.assertIn("中断: 動いていた 1 個のプロセスを止め", output)
        # 真ん中と、真ん中が別のグループで起動した子が、止めた直後に残っていない。
        self.assertEqual(2, len(pids), pids)
        self.assertEqual([], [pid for pid in pids if not process_gone(pid)], pids)

    def test_interrupt_kills_descendants_that_ignore_sigint_and_sigterm(self):
        started = time.monotonic()
        code, output, pids = self.interrupt_and_collect_pids("test_ignoring.py", IGNORING, "ignoring.pids",
                                                             signum=signal.SIGINT)
        self.assertEqual(130, code, output)
        self.assertEqual(1, len(pids), pids)
        self.assertEqual([], [pid for pid in pids if not process_gone(pid)], pids)
        # SIGINT と SIGTERM を無視する子孫がいる間は、2 つの猶予(5 秒ずつ)を待ってから SIGKILL で止める。
        self.assertGreaterEqual(time.monotonic() - started, 10)

    def test_argument_errors_exit_2_without_running(self):
        self.write_test("test_many.py", MANY)
        for args in (("test_missing.py",), ("validate.py",), ("-j", "0")):
            with self.subTest(args=args):
                result = self.run_runner(*args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
