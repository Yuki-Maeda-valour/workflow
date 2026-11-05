"""#346: implementation-git.py の引数の誤りの文言・検査の順序・help の出力を固定する特性テスト。

git を呼ぶ前に止まる入力だけを使う。本体を実プロセスで起動し、rc・stdout・stderr を照らす。
help の期待値は Python 3.10.12・COLUMNS=80 で取った。
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/dev-workflow/skills/do-task/scripts/implementation-git.py"
HASH = "a" * 64
M_MODE = "ERROR [input] prepare または run と読取り操作を指定する"
M_CWD_EMPTY = "ERROR [input] cwd が空"
M_CWD_DIR = "ERROR [input] cwd は存在するディレクトリでなければならない"
M_HASH = "ERROR [input] expect-context-sha256 は小文字16進64桁が必要"
M_DIFF = "ERROR [input] diff の範囲または組合せが不正"
M_LIMIT = "ERROR [input] limit は1〜1000が必要"
M_SHOW = "ERROR [input] show --file と --format は併用できない"
M_REV = "ERROR [input] revision が不正"
M_PATH = "ERROR [input] path はルートから外れない相対パスが必要"
ARGPARSE = "ERROR [input] 引数が不正: "
BAD_REVS = ("-x", "a\nb", "a\rb", "x" * 1025)

HELP = {
    "--help": "使い方: implementation-git.py [-h] {prepare,run} ...\n\n承認済みフィルターを無効化する、5操作専用の Git 読取り入口。\n\nオプション:\n  -h, --help     使い方を表示して終了\n\n入口:\n  {prepare,run}\n    prepare      委託直前の確認と承認情報の JSON 出力\n    run          承認情報を照合して指定した操作を実行\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "prepare --help": "使い方: implementation-git.py prepare [-h] --cwd CWD [--accept ACCEPT]\n\nオプション:\n  -h, --help       使い方を表示して終了\n  --cwd CWD        本体リポジトリのルート\n  --accept ACCEPT  最後に利用者が承認した一覧の値\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run --help": "使い方: implementation-git.py run [-h] --cwd CWD [--accept ACCEPT]\n                                 --expect-context-sha256 EXPECT_CONTEXT_SHA256\n                                 {status,diff,log,show,ls-files} ...\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --cwd CWD             本体リポジトリのルート\n  --accept ACCEPT       最後に利用者が承認した一覧の値\n  --expect-context-sha256 EXPECT_CONTEXT_SHA256\n                        委託元から受け取った64桁の照合値\n\n読取り操作:\n  {status,diff,log,show,ls-files}\n    status              変更したファイルを表示\n    diff                作業ツリー・index・履歴の差分を表示\n    log                 履歴を表示（既定: HEAD の先頭20件）\n    show                1 commit またはその1ファイルを表示\n    ls-files            記録済みか未記録のファイルを表示\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run status --help": "使い方: implementation-git.py run status [-h] [--format {short,porcelain}]\n                                        [--untracked {normal,all,no}]\n                                        [--path PATH]\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --format {short,porcelain}\n                        表示形式（既定: short）\n  --untracked {normal,all,no}\n                        未追跡の表示（既定: normal）\n  --path PATH           ルート相対のリテラルパス。反復可能\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run diff --help": "使い方: implementation-git.py run diff [-h] [--cached] [--base BASE]\n                                      [--target TARGET]\n                                      [--format {patch,stat,name-only,name-status}]\n                                      [--context CONTEXT] [--path PATH]\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --cached              index と比較\n  --base BASE           比較元の commit\n  --target TARGET       比較先の commit（--base が必要）\n  --format {patch,stat,name-only,name-status}\n                        差分の形式（既定: patch）\n  --context CONTEXT     前後の行数 0〜100（既定: 3）\n  --path PATH           ルート相対のリテラルパス。反復可能\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run log --help": "使い方: implementation-git.py run log [-h] [--from FROM_REV] [--to TO_REV]\n                                     [--limit LIMIT]\n                                     [--format {oneline,fuller}] [--path PATH]\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --from FROM_REV       表示から除く側の commit\n  --to TO_REV           履歴をたどる起点（既定: HEAD）\n  --limit LIMIT         表示件数 1〜1000（既定: 20）\n  --format {oneline,fuller}\n                        履歴の形式（既定: oneline）\n  --path PATH           ルート相対のリテラルパス。反復可能\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run show --help": "使い方: implementation-git.py run show [-h] [--rev REV]\n                                      [--format {patch,stat,name-only}]\n                                      [--file FILE]\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --rev REV             表示する commit（既定: HEAD）\n  --format {patch,stat,name-only}\n                        表示形式（既定: patch。--file と併用不可）\n  --file FILE           commit 内の単一ファイル。リンクは辿らない\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n",
    "run ls-files --help": "使い方: implementation-git.py run ls-files [-h] [--mode {tracked,untracked}]\n                                          [--path PATH]\n\nオプション:\n  -h, --help            使い方を表示して終了\n  --mode {tracked,untracked}\n                        対象（既定: tracked。untracked は ignore 済みを除く）\n  --path PATH           ルート相対のリテラルパス。反復可能\n\n終了コード: 成功 0、入力不正 2、起動・解析・Git の失敗 20、承認情報の不一致・事前検査の拒否 22。事前検査の 2/20/22 は維持します。\n"
}


class InputCharacterization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="impl-git-input-346-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.dir = str(Path(cls.tmp.name).resolve())
        cls.file = str(Path(cls.dir) / "plain-file")
        Path(cls.file).write_text("x")
        cls.missing = str(Path(cls.dir) / "missing")

    def call(self, *args):
        env = dict(os.environ, COLUMNS="80")
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, stdin=subprocess.DEVNULL, timeout=60)

    def run_args(self, *rest, cwd=None, digest=HASH):
        return ["run", "--cwd", self.dir if cwd is None else cwd,
                "--expect-context-sha256", digest, *rest]

    def assert_error(self, args, message, prefix=False):
        r = self.call(*args)
        self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
        self.assertEqual(r.stdout, "", args)
        self.assertEqual(len(r.stderr.splitlines()), 1, (args, r.stderr))
        if prefix:
            self.assertTrue(r.stderr.startswith(message), (args, r.stderr))
        else:
            self.assertEqual(r.stderr, message + "\n", args)

    def test_missing_entry_or_operation(self):
        self.assert_error([], M_MODE)
        self.assert_error(self.run_args(), M_MODE)

    def test_cwd_errors(self):
        for cwd, message in (("", M_CWD_EMPTY), (self.missing, M_CWD_DIR), (self.file, M_CWD_DIR)):
            with self.subTest(cwd=cwd):
                self.assert_error(["prepare", "--cwd", cwd], message)
                self.assert_error(self.run_args("status", cwd=cwd), message)

    def test_expected_hash_shape(self):
        for value in ("", "a" * 63, "A" * 64, "g" * 64):
            with self.subTest(value=value):
                self.assert_error(self.run_args("status", digest=value), M_HASH)

    def test_diff_range_and_combination(self):
        for rest in (("diff", "--context=-1"), ("diff", "--context=101"),
                     ("diff", "--target=HEAD"),
                     ("diff", "--cached", "--base=HEAD", "--target=HEAD")):
            with self.subTest(rest=rest):
                self.assert_error(self.run_args(*rest), M_DIFF)
        self.assert_error(self.run_args("diff", "--context=101", "--base=-x"), M_DIFF)

    def test_log_limit_and_show_combination(self):
        for n in ("0", "1001", "-1"):
            self.assert_error(self.run_args("log", "--limit=" + n), M_LIMIT)
        self.assert_error(self.run_args("show", "--file=a", "--format=patch"), M_SHOW)

    def test_revision_errors(self):
        for rev in BAD_REVS:
            with self.subTest(rev=rev):
                for rest in (("diff", "--base=" + rev), ("diff", "--base=HEAD", "--target=" + rev),
                             ("log", "--from=" + rev), ("log", "--to=" + rev),
                             ("show", "--rev=" + rev)):
                    self.assert_error(self.run_args(*rest), M_REV)

    def test_path_errors(self):
        for bad in ("", "/absolute", "..", "a/../b"):
            for op in ("status", "diff", "log", "ls-files"):
                with self.subTest(op=op, path=bad):
                    self.assert_error(self.run_args(op, "--path=" + bad), M_PATH)
            self.assert_error(self.run_args("show", "--file=" + bad), M_PATH)

    def test_duplicates_and_argparse_errors(self):
        self.assert_error(self.run_args("status", "--format=short", "--format=short"),
                          "ERROR [input] オプションが重複: --format")
        self.assert_error(self.run_args("diff", "--cached", "--cached"),
                          "ERROR [input] オプションが重複: --cached")
        self.assert_error(self.run_args("diff", "--base=HEAD", "--base", "HEAD"),
                          "ERROR [input] オプションが重複: --base")
        self.assert_error(["run", "--cwd", self.dir, "--cwd", self.dir,
                           "--expect-context-sha256", HASH, "status"],
                          "ERROR [input] オプションが重複: --cwd")
        for args in (["bogus"], ["prepare"], ["prepare", "--cwd", self.dir, "status"],
                     ["prepare", "--cwd", self.dir, "--expect-context-sha256", HASH],
                     ["run", "--cwd", self.dir, "status"],
                     self.run_args("bogus"), self.run_args("status", "--format", "patch"),
                     self.run_args("diff", "--context", "x"), self.run_args("log", "--limit", "x"),
                     self.run_args("status", "--output=x"), self.run_args("show", "--path", "a"),
                     self.run_args("status", "--untracked", "bad")):
            with self.subTest(args=args):
                self.assert_error(args, ARGPARSE, prefix=True)

    def test_check_order(self):
        # 文字 → 解析 → 入口 → cwd → 照合値の形 → diff → log → show → rev → path → file
        bad_path = "--path=/abs"
        cases = [
            (self.run_args("status", cwd=self.missing, digest="x"), M_CWD_DIR),
            (self.run_args("diff", "--context=101", cwd=self.missing, digest="x"), M_CWD_DIR),
            (self.run_args("diff", "--base=-x", cwd=self.file), M_CWD_DIR),
            (self.run_args("show", "--rev=-x", cwd=""), M_CWD_EMPTY),
            (self.run_args("status", bad_path, cwd=self.file), M_CWD_DIR),
            (self.run_args(cwd=self.missing), M_MODE),
            (["run", "--cwd", "", "--expect-context-sha256", "x"], M_MODE),
            (self.run_args("status", cwd=self.file, digest="x"), M_CWD_DIR),
            (self.run_args("status", cwd="", digest="x"), M_CWD_EMPTY),
            (self.run_args("log", "--limit=0", digest="x"), M_HASH),
            (self.run_args("show", "--file=a", "--format=patch", digest="x"), M_HASH),
            (self.run_args("diff", "--context=101", digest="x"), M_HASH),
            (self.run_args("diff", "--context=101", "--base=-x"), M_DIFF),
            (self.run_args("diff", "--target=-x"), M_DIFF),
            (self.run_args("diff", "--base=-x", bad_path), M_REV),
            (self.run_args("log", "--limit=0", "--from=-x"), M_LIMIT),
            (self.run_args("show", "--file=a", "--format=patch", "--rev=-x"), M_SHOW),
            (self.run_args("show", "--rev=-x", "--file=/abs"), M_REV),
            (self.run_args("status", bad_path, "--path=.."), M_PATH),
            (self.run_args("log", "--from=-x", "--to=-y"), M_REV),
            (self.run_args("diff", "--base=-x", "--target=-y"), M_REV),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                self.assert_error(args, message)

    def test_help_output(self):
        for key, expected in HELP.items():
            with self.subTest(args=key):
                r = self.call(*key.split(" "))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(r.stdout, expected)


if __name__ == "__main__":
    unittest.main()
