"""既存テストの入力と、実装から計算しない正規化本文の期待値。"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

INDENT = "対応外の字下げのコードフェンスがある(半角スペースは0〜3個、タブは不可)"
UNCLOSED = "閉じていないコードフェンスがある"


def headings_error(count):
    return f"コードフェンスの外の `## 追加修正記録` の見出し行が {count} 個ある(ちょうど 1 個が必要)"


BASE_BODY = """# サンプル
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
2. `ls` を実行し、エラーが出ない"""
ISSUE_NORMALIZED = "> **作成日**: 2026-09-18\n## 概要\n本文。\n## 完了条件\n1. 確かめる"


@dataclass(frozen=True)
class Case:
    name: str
    data: bytes
    body: str | None
    error: str | None
    digest: str | None
    origin: str | None = None

    @property
    def record(self):
        return {"normalized_hex": self.body.encode().hex() if self.body is not None else None,
                "digest": self.digest, "error": self.error}

    @property
    def cli(self):
        if self.error is None:
            return 0, (self.digest + "\n").encode(), b""
        return 1, b"", ("算出できない: " + self.error + "\n").encode()


def load_existing(root: Path):
    path = root / "scripts/test_task_digest.py"
    sys.path.insert(0, str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("task_digest_existing_cases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {node.name for parent in tree.body if isinstance(parent, ast.ClassDef)
               and parent.name == "TaskDigestTest" for node in parent.body
               if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}
    if len(methods) != 37:
        raise AssertionError(f"既存テストの件数が変わった: {len(methods)}、対応表を更新する")
    fixed = None
    for parent in ast.walk(tree):
        if isinstance(parent, ast.FunctionDef) and parent.name == "test_supported_indentation_keeps_fixed_values":
            for node in parent.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "expected" for t in node.targets):
                    fixed = ast.literal_eval(node.value)
    if fixed is None or sum(map(len, fixed.values())) != 16:
        raise AssertionError("既存16固定値を取得できない")
    return module, methods, fixed


def cases(root: Path):
    old, methods, fixed = load_existing(root)
    base, issue = old.BASE, old.ISSUE_BODY
    result = []
    covered = set()

    def add(name, source, body=None, error=None, digest=None, origin=None):
        if isinstance(source, str):
            source = source.encode()
        if (body is None) == (error is None):
            raise AssertionError(f"期待本文またはエラーを1個指定する: {name}")
        if body is not None:
            calculated = "sha256:" + hashlib.sha256(body.encode()).hexdigest()[:16]
            if digest is not None and digest != calculated:
                raise AssertionError(f"固定本文と既存固定値が不一致: {name}")
            digest = calculated
        result.append(Case(name, source, body, error, digest, origin))
        if origin:
            covered.add("test_" + origin)

    def same(name, source, origin):
        add(name, source, BASE_BODY, digest="sha256:21eefa756f838e54", origin=origin)

    same("base", base, "output_format")
    for mark in ("x", "X"):
        same("checkbox-" + mark, base.replace("- [ ] **Task 1**", f"- [{mark}] **Task 1**").replace("  - [ ] 子の項目", f"  - [{mark}] 子の項目"), "checkbox_marks_give_the_same_value")
    for name, source in {
        "status": base.replace("> **ステータス**: 🚧 進行中", "> **ステータス**: ✅ 完了(2026-09-24)"),
        "base-header": base.replace("> **作成日**: 2026-09-23", "> **作成日**: 2026-09-23\n> **基準コミット**: " + "a" * 40 + " / 未追跡一覧: " + "b" * 64),
        "unattended": base.replace("> **作成日**: 2026-09-23", "> **作成日**: 2026-09-23\n> **無人実行**: 可"),
        "blank-base-header": base.replace("> **品質維持契約**: checker・設計レビューを省略しない", "> **品質維持契約**: checker・設計レビューを省略しない\n\n> **基準コミット**: " + "c" * 40),
        "blank-unattended": base.replace("# サンプル\n", "# サンプル\n\n> **無人実行**: 可\n\n"),
    }.items():
        same(name, source, "mutable_header_lines_are_excluded")
    same("blank-outside", base.replace("本文の段落。\n", "本文の段落。\n\n\n  \n"), "blank_lines_outside_fences_are_dropped")
    same("blank-heading", base.replace("## 概要\n\n本文の段落。", "## 概要\n本文の段落。"), "blank_lines_outside_fences_are_dropped")
    changes = [
        ("blank-inside", "   - [x] done\n\n   2 行目", "   - [x] done\n   2 行目", "blank_line_inside_fence_is_kept"),
        ("checkbox-inside", "   - [x] done", "   - [ ] done", "checkbox_inside_fence_is_not_normalized"),
        ("header-inside", "> **ステータス**: 期待出力の中", "> **ステータス**: 書き換えた期待出力", "mutable_header_inside_fence_is_not_excluded"),
        ("contract", "checker・設計レビューを省略しない", "省略してよい", "other_header_lines_are_covered"),
        ("remove-contract", "> **品質維持契約**: checker・設計レビューを省略しない\n", "", "other_header_lines_are_covered"),
        ("created", "> **作成日**: 2026-09-23", "> **作成日**: 2026-09-24", "other_header_lines_are_covered"),
        ("criterion", "2. `ls` を実行し、エラーが出ない", "2. `ls` を実行する", "changing_a_completion_criterion_changes_the_value"),
        ("post-header", "本文の段落。", "本文の段落。\n> **基準コミット**: " + "e" * 40, "mutable_header_lines_after_first_heading_are_covered"),
        ("post-status", "本文の段落。", "本文の段落。\n> **ステータス**: 🚧 進行中", "mutable_header_lines_after_first_heading_are_covered"),
    ]
    for name, before, after, origin in changes:
        add(name, base.replace(before, after), BASE_BODY.replace(before, after), origin=origin)
    for name, source in [("issue", issue), ("issue-base", issue.replace("> **作成日**: 2026-09-18\n", "> **作成日**: 2026-09-18\n> **基準コミット**: " + "d" * 40 + "\n")), ("issue-unattended", issue.replace("> **作成日**: 2026-09-18\n", "> **作成日**: 2026-09-18\n\n> **無人実行**: 可\n"))]:
        add(name, source, ISSUE_NORMALIZED, origin="issue_body_without_title")
    add("issue-change", issue.replace("1. 確かめる", "1. 確かめない"), ISSUE_NORMALIZED.replace("1. 確かめる", "1. 確かめない"), origin="issue_body_without_title")
    for index, extra in enumerate(["- **保留**(ship-task・2026-09-24): needs-user が残る — 本文を直す\n", "\n### 2026-09-24 do-task\n\n| 項目 | 結果 |\n|---|---|\n| 品質ゲート | 緑 |\n\n- 状態:\n\n```text\n## main...origin/main\n M src/a.py\n```\n\n- 以上\n"]):
        same(f"append-record-{index}", base + extra, "appending_to_record_section")
    for name, source in [("crlf", base.replace("\n", "\r\n")), ("trailing-space", base.replace("本文の段落。\n", "本文の段落。 \t\n").replace("## 概要\n", "## 概要  \n")), ("trailing-blank", base + "\n\n\n"), ("bom", "\ufeff" + base)]:
        same(name, source, "whitespace_and_line_endings")
    same("cr", base.replace("\n", "\r"), "cr_only_line_endings")
    add("after-record", base + "\n## 補足\n\n完了条件を読み替える\n", BASE_BODY + "\n## 補足\n完了条件を読み替える", origin="section_after_record_section_is_covered")
    add("missing-record", base.replace("## 追加修正記録\n", "## 記録\n"), error=headings_error(0), origin="no_record_heading")
    add("duplicate-record", base.replace("## 完了条件\n", "## 追加修正記録\n\n- 偽の記録\n\n## 完了条件\n"), error=headings_error(2), origin="two_record_headings")
    add("fenced-record", base.replace("本文の段落。\n", "本文の段落。\n\n```markdown\n## 追加修正記録\n```\n"), BASE_BODY.replace("本文の段落。", "本文の段落。\n```markdown\n## 追加修正記録\n```"), origin="record_heading_inside_fence_is_not_counted")
    known = "# t\n\n> **ステータス**: 🚧 進行中\n> **作成日**: 2026-09-23\n\n## 概要\n\n- [x] 済み\n\n```text\n- [x] 見本\n\n```\n\n## 追加修正記録\n\n- 記録\n"
    add("known", known, "# t\n> **作成日**: 2026-09-23\n## 概要\n- [ ] 済み\n```text\n- [x] 見本\n\n```", origin="known_value")
    for marker, values in fixed.items():
        for indent, value in enumerate(values):
            pad = " " * indent
            source = f"> **ステータス**: 進行中\n## 概要\n本文\n{pad}{marker}text\nalpha\n\nbeta\n{pad}{marker}{marker[0]} \t\n## 追加修正記録\n記録\n## 補足\n本文\n"
            body = f"## 概要\n本文\n{pad}{marker}text\nalpha\n\nbeta\n{pad}{marker}{marker[0]}\n## 補足\n本文"
            for ending in ("\n", "\r\n", "\r"):
                add(f"fixed-{marker}-{indent}-{ending!r}", source.replace("\n", ending), body, digest="sha256:" + value, origin="supported_indentation_keeps_fixed_values")
    for closer in ("~~~", "``", "```text"):
        for mark in ("x", " "):
            source = old.TaskDigestTest.fence_probe(None, "```\n本文\n" + closer, mark)
            body = f"# t\n## 概要\n```\n本文\n{closer}\n- [{mark}] 後ろ\n```"
            add(f"false-close-{closer}-{mark}", source, body, origin="backtick_fence_is_not_closed_by_other_lines")
    for mark in ("x", " "):
        add(f"long-close-{mark}", f"# t\n\n## 概要\n\n```\n本文\n````\n- [{mark}] 後ろ\n\n## 追加修正記録\n\n- 記録\n", "# t\n## 概要\n```\n本文\n````\n- [ ] 後ろ", origin="longer_closing_fence_closes")
        add(f"nested-{mark}", f"# t\n\n## 概要\n\n````markdown\n```\n- [{mark}] 後ろ\n````\n\n## 追加修正記録\n\n- 記録\n", f"# t\n## 概要\n````markdown\n```\n- [{mark}] 後ろ\n````", origin="nested_shorter_fence_does_not_close")
    for marker in ("```", "~~~"):
        for location, source in {
            "before": f"## 概要\n{marker}text\n本文\n## 追加修正記録\n- 記録",
            "inside": f"## 概要\n本文\n## 追加修正記録\n{marker}text\n- 記録",
            "after": f"## 追加修正記録\n- 記録\n## 補足\n{marker}text\n本文",
        }.items():
            for end in ("", "\n"):
                add(f"unclosed-{marker}-{location}-{end!r}", source + end, error=UNCLOSED, origin="unclosed_fence_is_rejected_inside_and_outside_record")
        for text in ("本文 A", "本文 B"):
            add(f"cannot-hide-{marker}-{text}", issue + f"\n{marker}text\n記録\n\n## 補足\n{text}\n", error=UNCLOSED, origin="unclosed_record_fence_cannot_hide_later_body_changes")
        for closer in (marker[:2], "~~~" if marker == "```" else "```", marker + "text", "    " + marker):
            add(f"invalid-close-{marker}-{closer}", issue + f"\n{marker}text\n記録\n{closer}\n## 補足\n本文\n", error=UNCLOSED, origin="invalid_closer_does_not_make_unclosed_fence_valid")
        for indent in range(4):
            source = issue + "\n## 補足\n本文 A\n"
            fence = f"{' ' * indent}{marker}text\n## 偽の見出し\n- [x] 記録\n\n{' ' * indent}{marker}{marker[0]} \t\n"
            source = source.replace("- 記録\n", fence)
            for text in ("本文 A", "本文 B"):
                add(f"closed-record-{marker}-{indent}-{text}", source.replace("本文 A", text), ISSUE_NORMALIZED + f"\n## 補足\n{text}", origin="closed_record_fence_preserves_exclusion_and_later_body")
    for indent in (" " * 4, " " * 5, " " * 8, "\t", " \t", "\t ", "  \t  "):
        for marker in ("```", "~~~"):
            for closed in (False, True):
                fence = f"{indent}{marker}text\n{indent}first\n\n{indent}- [x] second\n"
                if closed:
                    fence += f"{indent}{marker}\n"
                for changed in (fence, fence.replace("\n\n", "\n"), fence.replace("[x]", "[ ]")):
                    for location, source in {"body": f"## 概要\n{changed}## 追加修正記録\n記録\n", "record": f"## 概要\n本文\n## 追加修正記録\n{changed}", "after": f"## 追加修正記録\n記録\n## 補足\n{changed}"}.items():
                        add(f"indent-{len(result)}-{location}", source, error=INDENT, origin="unsupported_fence_indentation_is_rejected_everywhere")
            for outer in ("```", "~~~"):
                for outer_indent in range(4):
                    pad = " " * outer_indent
                    source = f"## 概要\n{pad}{outer}text\n{indent}{marker}literal\n\n{indent}- [x] expected\n{pad}{outer}\n## 追加修正記録\n記録\n"
                    body = f"## 概要\n{pad}{outer}text\n{indent}{marker}literal\n\n{indent}- [x] expected\n{pad}{outer}"
                    add(f"literal-{len(result)}", source, body, origin="indented_fence_strings_inside_supported_fences_are_preserved")
        for literal in ("`", "``", "~", "~~", "`~`", "~`~", "text ```", "text ~~~"):
            for mark in ("x", " "):
                # 行末の半角スペースだけは除く。
                body = f"## 概要\n{indent}{literal}\n- [ ] 後ろ"
                add(f"short-{len(result)}", f"## 概要\n{indent}{literal}\n- [{mark}] 後ろ\n## 追加修正記録\n記録\n", body, origin="short_or_non_immediate_markers_do_not_trigger_indentation_rejection")
    add("prefix-record", base.replace("## 完了条件\n", "## 追加修正記録xyz\n\n- 別の節\n\n## 完了条件\n"), BASE_BODY.replace("## 完了条件", "## 追加修正記録xyz\n- 別の節\n## 完了条件"), origin="record_heading_needs_exact_match")
    add("only-prefix-record", base.replace("## 追加修正記録\n", "## 追加修正記録xyz\n"), error=headings_error(0), origin="record_heading_needs_exact_match")
    for source in ("", "\ufeff"):
        add(f"empty-{len(result)}", source, error=headings_error(0))
    add("record-only", "## 追加修正記録", "", digest="sha256:e3b0c44298fc1c14")
    for prefix in ("", "\ufeff", "\ufeff\ufeff"):
        add(f"bom-{len(result)}", prefix + "x\ufeffy\n## 追加修正記録", prefix[1:] + "x\ufeffy" if prefix else "x\ufeffy")
    for c in "\t\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u200a\u2028\u2029\u202f\u205f\u3000":
        add(f"unicode-space-{ord(c):x}", f"{c}- [x]text{c}\n## 追加修正記録", f"{c}- [ ]text{c}".rstrip(" \t"))
    for source, body in [("a\0b", "a\0b"), ("é", "é"), ("e\u0301", "e\u0301"), ("\u200b- [x]", "\u200b- [x]"), ("\ufeff- [x]", "- [ ]"), ("x" * 131073, "x" * 131073)]:
        add(f"boundary-{len(result)}", source + "\n## 追加修正記録", body)
    # CLI/ファイル専用の7観点は比較器の該当段で必ず実行した後に完了を照合する。
    external = {"test_invalid_inputs", "test_stdin", "test_non_regular_paths_are_rejected_without_waiting", "test_regular_file_and_symlink_keep_the_known_value", "test_replacement_before_open_is_rejected_without_waiting", "test_replacement_after_open_reads_the_opened_regular_file"}
    if covered | external != methods:
        raise AssertionError(f"既存テストの対応不足: {sorted(methods - covered - external)}、余分: {sorted(covered | external - methods)}")
    if not result or len({case.name for case in result}) != len(result):
        raise AssertionError("入力0件またはケース名重複")
    return result, external


# CPython3.10の実測と整数0の規則から固定した期待。実装から生成しない。
UNBUFFERED_CASES = (
    ("unset", None, False), ("empty", b"", False),
    ("zero", b"0", False), ("zeros", b"00", False),
    ("plus-zero", b"+0", False), ("minus-zero", b"-0", False),
    ("long-zero", b"0" * 4096, False),
    ("one", b"1", True), ("leading-zero-one", b"01", True),
    ("yes", b"yes", True), ("false", b"false", True),
    ("negative", b"-1", True), ("int-min", b"-2147483648", True),
    ("int-underflow", b"-2147483649", True), ("int-max", b"2147483647", True),
    ("int-overflow", b"2147483648", True), ("long-overflow", b"9223372036854775808", True),
    ("long-underflow", b"-9223372036854775809", True),
    ("space", b" ", True), ("tab", b"\t", True), ("lf", b"\n", True),
    ("cr", b"\r", True), ("vt", b"\x0b", True), ("ff", b"\x0c", True),
    ("space-zero", b" 0", False), ("tab-zero", b"\t0", False),
    ("lf-zero", b"\n0", False), ("cr-zero", b"\r0", False),
    ("vt-zero", b"\x0b0", False), ("ff-zero", b"\x0c0", False),
    ("spaces-signed-zeros", b" \t\n\r\x0b\x0c-00", False),
    ("zero-space", b"0 ", True), ("zero-tab", b"0\t", True),
    ("zero-lf", b"0\n", True), ("zero-cr", b"0\r", True),
    ("zero-vt", b"0\x0b", True), ("zero-ff", b"0\x0c", True),
    ("plus", b"+", True), ("minus", b"-", True), ("hex", b"0x0", True),
    ("decimal", b"0.0", True), ("underscore", b"0_0", True),
    ("raw-word", b"false\xff", True), ("raw", b"\xff", True),
    ("unicode-zero", "０".encode(), True), ("unicode-one", "١".encode(), True),
    ("nbsp-zero", "\u00a00".encode(), True), ("em-space-zero", "\u20030".encode(), True),
    ("zero-nbsp", "0\u00a0".encode(), True),
)
# 実stdoutと実stderrでも確認する代表値。全境界のflag検査とは別に数える。
UNBUFFERED_STREAM_IDS = ("unset", "empty", "zero", "spaces-signed-zeros", "long-zero",
                         "one", "yes", "false", "zero-space", "negative", "raw")
