import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DIGEST = REPO_ROOT / "plugins/dev-workflow/skills/create-task/scripts/task-digest.py"

# ヘッダにフェンスを置くのは、除外 1 がフェンスの外だけに効くことを確かめるため
BASE = """# サンプル

> **ステータス**: 🚧 進行中
> **作成日**: 2026-09-23
> **品質維持契約**: checker・設計レビューを省略しない

~~~text
> **ステータス**: 期待出力の中
~~~

## 概要

本文の段落。

## 実装タスク

- [ ] **Task 1**: 作る
  - [ ] 子の項目

## 完了条件

1. `echo ok` を実行し、次が出る
   ```text
   - [x] done

   2 行目
   ```
2. `ls` を実行し、エラーが出ない

## 追加修正記録

- **設計レビュー**(create-task・2026-09-23): APPROVED — checker: 指摘 0 / 反復 1 回 / 編成: checker / 本文: sha256:0123456789abcdef / needs-user: なし
"""

# 見出し(`# `)の無い本文(Issue 本文の形)
ISSUE_BODY = """> **ステータス**: 🚧 設計中
> **作成日**: 2026-09-18

## 概要

本文。

## 完了条件

1. 確かめる

## 追加修正記録

- 記録
"""


class TaskDigestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_digest(self, content, *args):
        path = self.root / "進行中_サンプル.md"
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        command = [sys.executable, str(DIGEST), *(args or (str(path),))]
        return subprocess.run(command, capture_output=True, check=False)

    def value(self, content):
        completed = self.run_digest(content)
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        return completed.stdout.decode("utf-8").strip()

    def assert_same(self, changed):
        self.assertEqual(self.value(BASE), self.value(changed))

    def assert_differs(self, changed):
        self.assertNotEqual(BASE, changed)
        self.assertNotEqual(self.value(BASE), self.value(changed))

    def assert_cannot_compute(self, completed, reason):
        stderr = completed.stderr.decode("utf-8", "replace")
        self.assertEqual(1, completed.returncode, stderr)
        self.assertEqual(b"", completed.stdout)
        self.assertIn(reason, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_output_format(self):
        completed = self.run_digest(BASE)
        self.assertEqual(0, completed.returncode)
        self.assertRegex(completed.stdout.decode("utf-8"), r"\Asha256:[0-9a-f]{16}\n\Z")
        self.assertEqual(b"", completed.stderr)

    def test_checkbox_marks_give_the_same_value(self):
        for mark in ("x", "X"):
            with self.subTest(mark=mark):
                changed = BASE.replace("- [ ] **Task 1**", f"- [{mark}] **Task 1**")
                changed = changed.replace("  - [ ] 子の項目", f"  - [{mark}] 子の項目")
                self.assert_same(changed)

    def test_mutable_header_lines_are_excluded(self):
        cases = {
            "ステータスの変更": BASE.replace("> **ステータス**: 🚧 進行中", "> **ステータス**: ✅ 完了(2026-09-24)"),
            "基準行の追加": BASE.replace(
                "> **作成日**: 2026-09-23",
                "> **作成日**: 2026-09-23\n> **基準コミット**: " + "a" * 40 + " / 未追跡一覧: " + "b" * 64,
            ),
            "メタ行の追加": BASE.replace("> **作成日**: 2026-09-23", "> **作成日**: 2026-09-23\n> **無人実行**: 可"),
            "空行を挟んだ基準行": BASE.replace(
                "> **品質維持契約**: checker・設計レビューを省略しない",
                "> **品質維持契約**: checker・設計レビューを省略しない\n\n> **基準コミット**: " + "c" * 40,
            ),
            "空行を挟んだメタ行": BASE.replace("# サンプル\n", "# サンプル\n\n> **無人実行**: 可\n\n"),
        }
        for name, changed in cases.items():
            with self.subTest(name):
                self.assert_same(changed)

    def test_blank_lines_outside_fences_are_dropped(self):
        self.assert_same(BASE.replace("本文の段落。\n", "本文の段落。\n\n\n  \n"))
        self.assert_same(BASE.replace("## 概要\n\n本文の段落。", "## 概要\n本文の段落。"))

    def test_blank_line_inside_fence_is_kept(self):
        self.assert_differs(BASE.replace("   - [x] done\n\n   2 行目", "   - [x] done\n   2 行目"))

    def test_checkbox_inside_fence_is_not_normalized(self):
        self.assert_differs(BASE.replace("   - [x] done", "   - [ ] done"))
        # フェンスの外の同じ変更では同じ値
        self.assert_same(BASE.replace("  - [ ] 子の項目", "  - [x] 子の項目"))

    def test_mutable_header_inside_fence_is_not_excluded(self):
        self.assert_differs(BASE.replace("> **ステータス**: 期待出力の中", "> **ステータス**: 書き換えた期待出力"))

    def test_other_header_lines_are_covered(self):
        self.assert_differs(BASE.replace("checker・設計レビューを省略しない", "省略してよい"))
        self.assert_differs(BASE.replace("> **品質維持契約**: checker・設計レビューを省略しない\n", ""))
        self.assert_differs(BASE.replace("> **作成日**: 2026-09-23", "> **作成日**: 2026-09-24"))

    def test_issue_body_without_title(self):
        before = self.value(ISSUE_BODY)
        added = ISSUE_BODY.replace(
            "> **作成日**: 2026-09-18\n",
            "> **作成日**: 2026-09-18\n> **基準コミット**: " + "d" * 40 + "\n",
        )
        self.assertEqual(before, self.value(added))
        self.assertEqual(before, self.value(ISSUE_BODY.replace("> **作成日**: 2026-09-18\n", "> **作成日**: 2026-09-18\n\n> **無人実行**: 可\n")))
        self.assertNotEqual(before, self.value(ISSUE_BODY.replace("1. 確かめる", "1. 確かめない")))

    def test_appending_to_record_section(self):
        self.assert_same(BASE + "- **保留**(ship-task・2026-09-24): needs-user が残る — 本文を直す\n")
        self.assert_same(
            BASE
            + "\n### 2026-09-24 do-task\n\n| 項目 | 結果 |\n|---|---|\n| 品質ゲート | 緑 |\n\n"
            # 貼った `git status -sb` の `## ` の行で、節が途中で切れないこと
            + "- 状態:\n\n```text\n## main...origin/main\n M src/a.py\n```\n\n- 以上\n"
        )

    def test_whitespace_and_line_endings(self):
        cases = {
            "CRLF": BASE.replace("\n", "\r\n"),
            "行末の空白": BASE.replace("本文の段落。\n", "本文の段落。 \t\n").replace("## 概要\n", "## 概要  \n"),
            "末尾の空行": BASE + "\n\n\n",
            "BOM": "\ufeff" + BASE,
        }
        for name, changed in cases.items():
            with self.subTest(name):
                self.assert_same(changed)

    def test_changing_a_completion_criterion_changes_the_value(self):
        self.assert_differs(BASE.replace("2. `ls` を実行し、エラーが出ない", "2. `ls` を実行する"))

    def test_section_after_record_section_is_covered(self):
        self.assert_differs(BASE + "\n## 補足\n\n完了条件を読み替える\n")

    def test_no_record_heading(self):
        missing = BASE.replace("## 追加修正記録\n", "## 記録\n")
        self.assert_cannot_compute(self.run_digest(missing), "`## 追加修正記録`")

    def test_two_record_headings(self):
        twice = BASE.replace("## 完了条件\n", "## 追加修正記録\n\n- 偽の記録\n\n## 完了条件\n")
        self.assert_cannot_compute(self.run_digest(twice), "`## 追加修正記録`")

    def test_record_heading_inside_fence_is_not_counted(self):
        fenced = BASE.replace("本文の段落。\n", "本文の段落。\n\n```markdown\n## 追加修正記録\n```\n")
        completed = self.run_digest(fenced)
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        self.assertNotEqual(self.value(BASE), completed.stdout.decode("utf-8").strip())

    def test_known_value(self):
        # 正規化後の文字列を明示し、SHA-256・先頭 16 桁・連結の末尾に改行を付けない、の 3 点を縛る
        source = (
            "# t\n\n> **ステータス**: 🚧 進行中\n> **作成日**: 2026-09-23\n\n## 概要\n\n- [x] 済み\n\n"
            "```text\n- [x] 見本\n\n```\n\n## 追加修正記録\n\n- 記録\n"
        )
        normalized = "# t\n> **作成日**: 2026-09-23\n## 概要\n- [ ] 済み\n```text\n- [x] 見本\n\n```"
        expected = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(expected, self.value(source))

    def test_mutable_header_lines_after_first_heading_are_covered(self):
        added = BASE.replace("本文の段落。\n", "本文の段落。\n> **基準コミット**: " + "e" * 40 + "\n")
        self.assert_differs(added)
        with_status = BASE.replace("本文の段落。\n", "本文の段落。\n> **ステータス**: 🚧 進行中\n")
        self.assert_differs(with_status)
        changed = with_status.replace("本文の段落。\n> **ステータス**: 🚧 進行中", "本文の段落。\n> **ステータス**: ✅ 完了")
        self.assertNotEqual(with_status, changed)
        self.assertNotEqual(self.value(with_status), self.value(changed))

    def fence_probe(self, opener_and_closer, mark):
        return (
            "# t\n\n## 概要\n\n" + opener_and_closer + f"\n- [{mark}] 後ろ\n"
            "```\n\n## 追加修正記録\n\n- 記録\n"
        )

    def test_backtick_fence_is_not_closed_by_other_lines(self):
        # 閉じない行の後ろのチェックボックスはフェンスの中に残るので、印を変えると値が変わる
        for closer in ("~~~", "``", "```text"):
            with self.subTest(closer=closer):
                opened = "```\n本文\n" + closer
                self.assertNotEqual(self.value(self.fence_probe(opened, "x")), self.value(self.fence_probe(opened, " ")))

    def test_longer_closing_fence_closes(self):
        closed = "# t\n\n## 概要\n\n```\n本文\n````\n- [{}] 後ろ\n\n## 追加修正記録\n\n- 記録\n"
        self.assertEqual(self.value(closed.format("x")), self.value(closed.format(" ")))

    def test_nested_shorter_fence_does_not_close(self):
        # ```` で開いたフェンスは、中の ``` の行では閉じない(閉じの行は開きの長さ以上)
        nested = "# t\n\n## 概要\n\n````markdown\n```\n- [{}] 後ろ\n````\n\n## 追加修正記録\n\n- 記録\n"
        self.assertNotEqual(self.value(nested.format("x")), self.value(nested.format(" ")))

    def test_unclosed_fence_extends_to_end(self):
        unclosed = "# t\n\n## 概要\n\n本文\n\n## 追加修正記録\n\n- 記録\n\n## 補足\n\n```text\n1 行目\n\n2 行目\n"
        self.assertNotEqual(self.value(unclosed), self.value(unclosed.replace("1 行目\n\n2 行目", "1 行目\n2 行目")))

    def test_fence_indentation(self):
        four = "# t\n\n## 概要\n\n    ```\n- [{}] 後ろ\n\n## 追加修正記録\n\n- 記録\n"
        self.assertEqual(self.value(four.format("x")), self.value(four.format(" ")))
        three = "# t\n\n## 概要\n\n   ```\n- [{}] 後ろ\n   ```\n\n## 追加修正記録\n\n- 記録\n"
        self.assertNotEqual(self.value(three.format("x")), self.value(three.format(" ")))

    def test_cr_only_line_endings(self):
        self.assert_same(BASE.replace("\n", "\r"))

    def test_record_heading_needs_exact_match(self):
        prefixed = BASE.replace("## 完了条件\n", "## 追加修正記録xyz\n\n- 別の節\n\n## 完了条件\n")
        self.assert_differs(prefixed)
        self.assert_cannot_compute(self.run_digest(BASE.replace("## 追加修正記録\n", "## 追加修正記録xyz\n")), "`## 追加修正記録`")

    def test_invalid_inputs(self):
        self.assert_cannot_compute(self.run_digest(BASE.encode("utf-8") + b"\xff\xfe"), "UTF-8")
        completed = self.run_digest(BASE, str(self.root / "無い.md"))
        self.assert_cannot_compute(completed, "読めない")
        completed = subprocess.run([sys.executable, str(DIGEST)], capture_output=True, check=False)
        self.assertEqual(2, completed.returncode)
        self.assertEqual(b"", completed.stdout)

    def test_stdin(self):
        completed = subprocess.run(
            [sys.executable, str(DIGEST), "-"], input=BASE.encode("utf-8"), capture_output=True, check=False
        )
        self.assertEqual(0, completed.returncode)
        self.assertEqual(self.value(BASE), completed.stdout.decode("utf-8").strip())
        self.assertRegex(completed.stdout.decode("utf-8"), re.compile(r"\Asha256:[0-9a-f]{16}\n\Z"))


if __name__ == "__main__":
    unittest.main()
