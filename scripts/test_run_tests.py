"""scripts/run-tests.py の回帰テスト。使い捨ての scripts/ に置いた小さなテストのファイルで動かす。"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
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

    def test_argument_errors_exit_2_without_running(self):
        self.write_test("test_many.py", MANY)
        for args in (("test_missing.py",), ("validate.py",), ("-j", "0")):
            with self.subTest(args=args):
                result = self.run_runner(*args)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
