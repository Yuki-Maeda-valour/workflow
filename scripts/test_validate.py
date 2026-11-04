#!/usr/bin/env python3
"""scripts/validate.py の回帰テスト。

各検査について「**わざと壊した写しで ERROR になる**」「**正しい写しでは ERROR にならない**」の
2 方向を見る(片方だけだと、検査を外しても・広げすぎても緑のままになってしまう)。
写しは setUp が毎回作り直すので、テスト同士は干渉しない。
"""

import contextlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE_REPO = Path(__file__).resolve().parent.parent

# 周辺プロジェクト名を渡す環境変数(validate.py の _PROJECT_NAMES_ENV と対)
PROJECT_NAMES_ENV = "WORKFLOW_PROJECT_NAMES"

# 書き込み先。禁止パターン・委託の語の検査対象は scripts/ 配下の非画像ファイル、
# リンク検査の対象は skill 直下と references/ 配下の *.md —— 検査ごとに対象が違うので
# 書き先を選べるようにする。
CHECKED_PY = "plugins/dev-workflow/skills/update-doc/scripts/check_links.py"
CHECKED_MD = "plugins/dev-workflow/skills/tool-check/references/validate-test.md"
SKILLS_REL = "plugins/dev-workflow/skills"
SKILL_COUNT_RE = re.compile(r"skills?\s*(\d+)\s*種|(\d+)\s*skills?")
MARKETPLACE_JSON = ".claude-plugin/marketplace.json"
PLUGIN_JSON = "plugins/dev-workflow/.claude-plugin/plugin.json"
# 人が読む文の書き方の正本(validate.py の _WRITING_RULES と対。SKILLS_REL からの相対パス)
WRITING_RULES_REL = "do-task/references/writing-for-people.md"
WRITING_LINK_MISSING = "writing-for-people.md へのリンクが無い"
# 行の長さの WARN の文に必ずある字面(validate.py の check_line_length() と対)
LINE_LENGTH_WARN = "200 字以下にする"


def writing_rules_line(skill):
    """各 SKILL.md の `## 原則` に置く、書き方の正本を指す 1 行。リンクは SKILL.md の位置からの
    相対パス(正本を持つ do-task だけ `references/…`)。"""
    link = "references/writing-for-people.md" if skill == "do-task" else f"../{WRITING_RULES_REL}"
    return (
        "- **人が読む文(報告・質問・PR と Issue の本文・作る文書・コミットメッセージ)を書く前に "
        f"[{link}]({link}) を読み、それに従う**(わかりやすさの決まり・言い換え表・字面を変えない行と語・"
        "口調の決め方。このファイルに届かないときは、"
        "権威参照ファイル(AI への指示をまとめたプロジェクトのファイル)の「応答の書き方」節と、"
        "口調の決まりを書いた節に従い、届かないことを報告に書く)"
    )

# Issue #101・#148 の期待語彙。検証器の定数を参照せず、欠落や分類違いも検知する。
HOST_CLI_BOUNDED_WORDS = (
    "cursor-agent", "gemini", "workspace-write", "danger-full-access",
    "codex exec", "codex review", "codex mcp", "codex-plugin-cc",
    "codex-rescue", "run_in_background", "AskUserQuestion", "multiSelect",
)
# 質問の道具名と引数名。ERROR の案内がほかの語と違い、解決表の質問の節を指す。
HOST_CLI_QUESTION_WORDS = ("AskUserQuestion", "multiSelect")
HOST_CLI_QUESTION_GUIDANCE = "delegation-map.md §8"
HOST_CLI_OTHER_GUIDANCE = "external-runners.md へ移す"
HOST_CLI_MCP_WORDS = ("claude-in-chrome", "chrome-devtools")


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "workflow"
        shutil.copytree(
            SOURCE_REPO,
            self.repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        self.home = self.root / "home"
        # 偽 home に「いかにも拾いそうな」周辺プロジェクトを置く。V3 の核心は
        # **これが在っても環境変数を渡さない限り検査しない**こと —— この 1 行が無いと、
        # `~/dev` の列挙を復活させる実装でもテストが全部通ってしまう。
        (self.home / "dev" / "customer-portal").mkdir(parents=True)

    # ------------------------------------------------------------------ ヘルパ

    def run_validator(self, env=None):
        """写しの validate.py を走らせる。`env` で渡した環境変数だけが見える状態にする
        (実行環境の WORKFLOW_PROJECT_NAMES を持ち込まない)。"""
        status, namespace, output = self.run_validator_namespace(env)
        return status, namespace["ERRORS"], output

    def run_validator_namespace(self, env=None):
        """run_validator() と同じく走らせ、ERRORS・WARNS を持つ名前空間ごと返す。"""
        script = self.repo / "scripts" / "validate.py"
        environ = {k: v for k, v in os.environ.items() if k != PROJECT_NAMES_ENV}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch(
            "pathlib.Path.home", return_value=self.home
        ):
            namespace = runpy.run_path(str(script), run_name="validate_under_test")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = namespace["main"]()
        return status, namespace, output.getvalue()

    def append_to_checked_file(self, text, relpath=CHECKED_PY, prefix="# "):
        target = self.repo / relpath
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"\n{prefix}{text}\n")

    def write_file(self, relpath, text):
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_repository_task_store_does_not_change_validation(self):
        # 配布物の検査は利用先の設定に依存しない。コピー内だけで有無と値を変える。
        profile = self.repo / ".claude/project-profile.yml"
        profile.unlink(missing_ok=True)
        expected = self.run_validator()
        self.assertEqual(expected[0], 0, expected[1])
        for value in ("file", "github-issue", "invalid"):
            with self.subTest(task_store=value):
                self.write_file(".claude/project-profile.yml", f"task_store: {value}\n")
                self.assertEqual(self.run_validator(), expected)

    def write_skill_md(self, skill, total_lines, trailing_newline=True):
        """SKILL.md を frontmatter を残したまま**論理行数ちょうど**に作り替える。
        `## 原則` の見出しと書き方の正本を指す 1 行は残す(無いと原則の節の検査で ERROR になり、
        行数の境界の検証にならない)。"""
        target = self.repo / "plugins" / "dev-workflow" / "skills" / skill / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        end = text.index("\n---", 3) + len("\n---\n")
        front = text[:end]
        kept = ["## 原則", writing_rules_line(skill)]
        filler_count = total_lines - front.count("\n") - len(kept)
        self.assertGreater(filler_count, 0, "frontmatter と原則の節だけで指定行数を超えている")
        out = front + "\n".join(kept + ["本文"] * filler_count)
        if trailing_newline:
            out += "\n"
        target.write_text(out, encoding="utf-8")
        # validate.py と同じ数え方で、狙った論理行数になったことを確かめてから使う
        logical = out.count("\n") + (0 if out.endswith("\n") else 1)
        self.assertEqual(total_lines, logical)

    def set_description(self, skill, length):
        target = self.repo / "plugins" / "dev-workflow" / "skills" / skill / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        new, n = re.subn(
            r"^description: .*$", "description: " + "あ" * length, text, count=1, flags=re.MULTILINE
        )
        self.assertEqual(1, n, "description 行が 1 行見つからない")
        target.write_text(new, encoding="utf-8")

    def add_frontmatter_line(self, skill, line):
        """SKILL.md の frontmatter の name の行の直後に 1 行を足す。"""
        target = self.repo / "plugins" / "dev-workflow" / "skills" / skill / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        new, n = re.subn(r"^(name: .*)$", lambda m: m.group(1) + "\n" + line, text, count=1, flags=re.MULTILINE)
        self.assertEqual(1, n, "name 行が 1 行見つからない")
        target.write_text(new, encoding="utf-8")

    def skill_md(self, skill):
        return self.repo / SKILLS_REL / skill / "SKILL.md"

    def replace_in_skill_md(self, skill, old, new):
        """SKILL.md の中の `old` を 1 か所だけ `new` に置き換える(見つからなければ試験を落とす)。"""
        target = self.skill_md(skill)
        text = target.read_text(encoding="utf-8")
        self.assertEqual(1, text.count(old), f"{skill}/SKILL.md に置き換え元がちょうど 1 か所ない")
        target.write_text(text.replace(old, new), encoding="utf-8")

    def writing_rules_line_of(self, skill):
        """SKILL.md の原則の節から、書き方の正本へのリンクを持つ行を取る。

        完了報告の手順にも同じファイルへのリンクがある。検査 9 が対象にする
        原則の節だけから、試験で置き換える行を選ぶ。
        """
        text = self.skill_md(skill).read_text(encoding="utf-8")
        principles = re.search(r"^## 原則\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
        self.assertIsNotNone(principles, f"{skill}/SKILL.md に原則の節が無い")
        lines = [
            line
            for line in principles.group(1).split("\n")
            if "writing-for-people.md](" in line
        ]
        self.assertEqual(1, len(lines), lines)
        return lines[0]

    def rewrite_skill_body(self, skill, body_lines):
        """SKILL.md の frontmatter を残し、本文を `body_lines` だけにする。"""
        target = self.skill_md(skill)
        text = target.read_text(encoding="utf-8")
        end = text.index("\n---", 3) + len("\n---\n")
        target.write_text(text[:end] + "\n".join(body_lines) + "\n", encoding="utf-8")
        # 本文を最小にした試験では、消した名前の参照形も一覧から外す。
        # 残る文書の除外と、定義・参照の実在検査はそのまま保つ。
        def keep_existing_forms(data):
            for kind in ("names", "reports"):
                for entry in data[kind]:
                    bodies = [(self.repo / path).read_text(encoding="utf-8") for path in entry["files"]]
                    entry["forms"] = [form for form in entry["forms"] if any(form in body for body in bodies)]
                data[kind] = [entry for entry in data[kind] if entry["forms"]]
        self.patch_json("scripts/wording-exceptions.json", keep_existing_forms)

    def patch_json(self, relpath, mutate):
        target = self.repo / relpath
        data = json.loads(target.read_text(encoding="utf-8"))
        mutate(data)
        target.write_text(json.dumps(data, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")

    def assert_clean(self, env=None):
        status, errors, _ = self.run_validator(env)
        self.assertEqual(0, status, errors)
        self.assertEqual([], errors)

    def assert_error(self, needles, env=None):
        """ERROR で落ち、**その本文に該当の理由が出る**ことまで見る。"""
        status, errors, _ = self.run_validator(env)
        self.assertEqual(1, status, errors)
        self.assertTrue(
            any(all(needle in error for needle in needles) for error in errors), errors
        )
        return errors

    # -------------------------------------------------------------- 写しの健全性

    def test_pristine_copy_is_clean(self):
        """壊していない写しは ERROR 0(各テストの「ERROR にならない」判定の土台)。"""
        self.assert_clean()

    def test_wording_check_runs_in_full_validator(self):
        self.write_file(CHECKED_MD, "照合する。\n")
        self.assert_error([f"{CHECKED_MD}:1:", "言い換えの初出", "照合"])

    # ---------------------------------------------------------- 番号だけの理由

    def test_number_only_outcome_reasons_are_errors(self):
        for number in ("D1", "S4", "U2", "G3", "M4", "H39", "決定 51", "軸 3", "③", "条件 ③", "§12-2"):
            with self.subTest(number=number):
                self.assert_appended_line_flagged(
                    f"無人の周の結果: 失敗扱い — {number}", ["番号だけの理由", repr(number), "番号: 中身"]
                )

    def test_number_only_hold_reason_is_error(self):
        for date in ("2026-11-04", "YYYY-MM-DD"):
            with self.subTest(date=date):
                self.assert_appended_line_flagged(
                    f"- **保留**(ship-task・{date}): G3 — 前提を確かめる",
                    ["番号だけの理由", "'G3'"],
                )

    def test_number_only_outcome_before_punctuation_is_error(self):
        for suffix in (")", "、", ",", "*", "]"):
            with self.subTest(suffix=suffix):
                self.assert_appended_line_flagged(
                    f"(例: 無人の周の結果: 保留 — S4{suffix}",
                    ["番号だけの理由", "'S4'"],
                )

    def test_number_only_hold_at_end_of_example_is_error(self):
        for suffix in ("", "`", ")"):
            with self.subTest(suffix=suffix):
                self.assert_appended_line_flagged(
                    f"- **保留**(ship-task・YYYY-MM-DD): S4{suffix}",
                    ["番号だけの理由", "'S4'"],
                )

    def test_number_only_reasons_in_document_examples_are_errors(self):
        cases = (
            ("plugins/dev-workflow/skills/tool-check/SKILL.md", "無人の周の結果: 保留 — S4"),
            (f"{SKILLS_REL}/{WRITING_RULES_REL}", "`無人の周の結果: 保留 — S4`"),
            ("docs/design.md", "例: `無人の周の結果: 失敗扱い — G3`。"),
            ("README.md", "| 例 | `無人の周の結果: 保留 — S4` |"),
            (f"{SKILLS_REL}/init-project/templates/AGENTS.md.template",
             "- **保留**(ship-task・YYYY-MM-DD): G1 — 許可を確かめる"),
        )
        for path, line in cases:
            with self.subTest(path=path):
                self.assert_appended_line_flagged(line, ["番号だけの理由"], relpath=path)

    def test_number_only_reason_in_code_fence_is_error(self):
        target = self.repo / self.REPLACED_TARGET
        line_no = target.read_text(encoding="utf-8").count("\n") + 3
        self.append_to_checked_file(
            "```markdown\n無人の周の結果: 保留 — S4\n```", relpath=self.REPLACED_TARGET, prefix=""
        )
        self.assert_error([f"{self.REPLACED_TARGET}:{line_no}:", "番号だけの理由", "'S4'"])

    def test_number_only_reason_with_empty_explanation_or_allow_marker_is_error(self):
        for line in (
            "無人の周の結果: 保留 — S4: ",
            "- **保留**(ship-task・YYYY-MM-DD): G1: — 許可を確かめる",
            "無人の周の結果: 保留 — S4 <!-- validate-allow: 例示 -->",
        ):
            with self.subTest(line=line):
                self.assert_appended_line_flagged(line, ["番号だけの理由"])

    def test_explained_reasons_and_reference_numbers_are_allowed(self):
        for line in (
            "無人の周の結果: 失敗扱い — G3: 無人の前提を欠く",
            "`無人の周の結果: 保留 — S4: 承認の後に本文が変わった`",
            "- **保留**(ship-task・2026-11-04): G1: 許可の要る操作が拒否された — 許可を確かめる",
            "無人の周の結果: PR — https://example.invalid/pr/1",
            "無人の周の結果: 保留 — <理由>",
            "- **保留**(ship-task・YYYY-MM-DD): <停止条件> — <人が次にすること>",
            "無人の周の結果: 失敗扱い — G3 無人の前提を欠く",
            "(例: 無人の周の結果: 保留 — S4: 承認の後に本文が変わった)",
            "`- **保留**(ship-task・YYYY-MM-DD): S4: 承認の後に本文が変わった`",
            "- **保留**(ship-task・YYYY-MM-DD): S4 承認の後に本文が変わった",
            "正本は loop.md §4。#107 の H48。手順は D1・S4・G3・M4・決定 51・軸 3・条件 ③・§12-2。",
            "レビュー判定: APPROVED",
        ):
            with self.subTest(line=line):
                self.assert_appended_line_flagged(line, [], flagged=False)

    def test_number_only_reasons_outside_documents_are_not_scanned(self):
        line = "無人の周の結果: 保留 — S4"
        self.append_to_checked_file(line, relpath="plugins/dev-workflow/skills/ship-task/scripts/loop.sh")
        self.append_to_checked_file(line, relpath="CHANGELOG.md", prefix="")
        self.write_file("docs/minutes/example.md", line + "\n")
        self.assert_clean()

    # ------------------------------------------------------- 置き換える語(枡・継承枠)

    REPLACED_TARGET = "plugins/dev-workflow/skills/do-task/references/review-protocol.md"

    def test_replaced_word_masu_is_error_with_guidance(self):
        self.append_to_checked_file("固定枡の説明", relpath=self.REPLACED_TARGET, prefix="")
        self.assert_error(["置き換える語", "'枡'", "持ち場"])

    def test_replaced_word_keishoowaku_is_error_in_design(self):
        self.append_to_checked_file("継承枠の説明", relpath="docs/design.md", prefix="")
        self.assert_error(["docs/design.md", "置き換える語", "'継承枠'"])

    def test_replaced_word_is_error_in_template(self):
        self.append_to_checked_file(
            "継承枠", relpath="plugins/dev-workflow/skills/init-project/templates/project-profile.yml.template"
        )
        self.assert_error(["置き換える語", "'継承枠'"])

    def test_replaced_word_allowed_in_writing_for_people(self):
        self.append_to_checked_file(
            "枡 -> 持ち場",
            relpath="plugins/dev-workflow/skills/do-task/references/writing-for-people.md",
            prefix="",
        )
        self.assert_clean()

    def test_replaced_word_marker_does_not_exempt_in_skill_docs(self):
        # 免除の印は禁止パターン検査とホスト CLI 語検査の 2 つだけに効く
        self.append_to_checked_file(
            "枡 <!-- validate-allow: 置き換える語の定義の例示 -->", relpath=self.REPLACED_TARGET, prefix=""
        )
        self.assert_error(["置き換える語", "'枡'"])

    def test_replaced_word_in_skill_md_is_error(self):
        self.append_to_checked_file("枡", relpath="plugins/dev-workflow/skills/tool-check/SKILL.md", prefix="")
        self.assert_error(["tool-check/SKILL.md", "置き換える語", "'枡'"])

    def test_replaced_word_in_skill_scripts_is_not_scanned(self):
        self.append_to_checked_file("枡", relpath="plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh")
        self.assert_clean()

    def test_replaced_word_in_non_utf8_file_is_still_found(self):
        target = self.repo / self.REPLACED_TARGET
        target.write_bytes(target.read_bytes() + b"\n\xff\xfe \xe6\x9e\xa1\n")
        self.assert_error(["置き換える語", "'枡'"])

    def edit_definition_line(self, prefix, add="", new_prefix=None):
        """docs/design.md の定義の行(行頭が prefix のちょうど 1 行)の末尾に add を足すか、行頭を new_prefix に
        変える。その行の番号を返す(元の中身は setUp のコピーごと捨てる)。"""
        target = self.repo / "docs" / "design.md"
        lines = target.read_text(encoding="utf-8").split("\n")
        found = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        self.assertEqual(1, len(found), found)
        line = lines[found[0]] + add
        if new_prefix is not None:
            line = new_prefix + line[len(prefix):]
        lines[found[0]] = line
        target.write_text("\n".join(lines), encoding="utf-8")
        return found[0] + 1

    def test_replaced_word_definition_line_in_design_is_exempt(self):
        # 「文書の中で置き換える語」の定義の行は、その行の組の語(枡・フル段階など)を除く
        self.edit_definition_line("- **文書の中で置き換える語**", "例として枡・フル段階・結末の行を書く")
        self.assert_clean()

    def test_replaced_word_other_design_line_is_error(self):
        self.append_to_checked_file(
            "- 文書の中で置き換える語: 枡 <!-- validate-allow: 例示 -->", relpath="docs/design.md", prefix=""
        )
        self.assert_error(["docs/design.md", "置き換える語", "'枡'"])

    def test_replaced_word_noryokutai_is_error_with_guidance(self):
        self.append_to_checked_file("能力帯の異なる2体", relpath=self.REPLACED_TARGET, prefix="")
        self.assert_error(["置き換える語", "'能力帯'", "性能の段"])

    def test_replaced_word_shiyu_and_sudoori_are_errors(self):
        self.append_to_checked_file("私有の領域", relpath=self.REPLACED_TARGET, prefix="")
        self.assert_error(["置き換える語", "'私有'", "専用の"])
        self.append_to_checked_file("検査が素通りする", relpath="docs/design.md", prefix="")
        self.assert_error(["docs/design.md", "置き換える語", "'素通り'"])

    def assert_appended_line_flagged(self, text, needles, relpath=None, flagged=True):
        """1 行を書き足し、**その行の番号の ERROR**(flagged=False なら ERROR 0)を確かめ、元の中身へ戻す。
        subTest をまたいで前の行の ERROR が残り、後の判定を通してしまうのを防ぐ。"""
        relpath = relpath or self.REPLACED_TARGET
        target = self.repo / relpath
        original = target.read_bytes()
        try:
            line_no = original.decode("utf-8").count("\n") + 2  # 書き足す前の改行の後ろの空行の次
            self.append_to_checked_file(text, relpath=relpath, prefix="")
            if flagged:
                self.assert_error([f"{relpath}:{line_no}:", *needles])
            else:
                self.assert_clean()
        finally:
            target.write_bytes(original)

    def test_replaced_word_fusagu_conjugations_are_errors(self):
        for form in ("塞ぐ", "塞がない", "塞ぎます", "塞ぎ、", "塞いだ", "塞げる", "塞ごう"):
            with self.subTest(form=form):
                self.assert_appended_line_flagged(f"経路を{form}", ["置き換える語", "塞ぐ", "防ぐ"])

    def test_replaced_word_fusagu_after_kanji_is_not_flagged(self):
        for word in ("閉塞が起きる", "要塞がある", "梗塞が", "閉塞げ", "閉塞ぎ", "閉塞いだ", "閉塞ごう", "閉塞ぐ"):
            with self.subTest(word=word):
                self.assert_appended_line_flagged(f"{word}の説明", [], flagged=False)

    def test_replaced_word_git_deguchi_is_error(self):
        for text in ("git 出口の規律", "git の出口は ship-task", "Git 出口", "GIT の出口", "git出口", "gitの出口", "git  出口", "Git　出口", "git の 出口", "git　の　出口"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'git 出口'", "commit・push・PR"])

    def test_plain_deguchi_alone_is_not_flagged(self):
        self.assert_appended_line_flagged("非常口と出口の案内を書く", [], flagged=False)

    def test_replaced_word_utsushi_forms_are_errors(self):
        for text in ("一時的な写し", "写しを作る", "行を写す", "節は写さない", "写した先", "写して使う",
                     "依頼文へ写せる", "写そうとする", "汎用知識の書き写し", "内容を複写しない", "複写を採らない",
                     "丸写しする", "全文写しを渡す", "筆写する"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'写し(", "コピー"])
        self.assert_appended_line_flagged("わざと壊した写しで", ["'写し("], relpath="docs/design.md")

    def test_replaced_word_utsushi_lookalikes_are_not_flagged(self):
        # 決まった漢字が前に付く語(描写・転写・模写・映写・試写・接写・活写・投写・連写・盗写・大写しなど)と、
        # 写真・写像・写る は探さない。
        # 前に漢字が付く語は「〜写す」「〜写し」などの形だけを探すので、名詞の「筆写」も探さない
        for text in ("現状を描写する", "転写した", "模写する", "映写する", "試写する", "接写する",
                     "活写する", "投写する", "連写する", "盗写する", "大写しにする",
                     "写真を撮る", "写像を定義する", "記録に写りうる", "筆写の手間"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_word_atesaki_is_error(self):
        for text in ("宛先として識別する", "宛先が失われる", "`ssh -G <宛先>`", "宛て先の名前"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'宛先'", "送る相手"])
        self.assert_appended_line_flagged("宛先識別", ["'宛先'"], relpath="docs/design.md")

    def test_replaced_word_atesaki_lookalikes_are_not_flagged(self):
        for text in ("宛名を書く", "手紙を宛てる", "送り先と接続先"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_word_maeoki_is_error(self):
        for text in ("Git の共通安全前置き", "safe Git 前置きを付ける", "`git -C <dir> <前置き> ls-files`",
                     "前置きの mount", "予約語の前置き", "素朴な前置き比較"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'前置き'", "安全用のオプション"])
        self.assert_appended_line_flagged("公開経路の安全前置き", ["'前置き'"], relpath="docs/design.md")

    def test_replaced_word_maeoki_lookalikes_are_not_flagged(self):
        # 「以前置き…」と、後ろが「置き換え」「置き場」になる形と、「前置詞」「前置される」は探さない
        for text in ("以前置きました", "以前置き換えた", "直前置き場に置く", "事前置き換えの手順", "前置詞", "空白だけが前置される位置"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_words_of_316_are_errors(self):
        # #316: やり取りの範囲の 3 つの環境の名前と、その判定・結果の行
        cases = (
            ("フル段階では同じ name へ再依頼する", "'フル段階'", "双方向でやり取りできる環境"),
            ("標準段階では新規に起動する", "'標準段階'", "一方向だけの環境"),
            ("最小段階では直列に進める", "'最小段階'", "別の AI を使えない環境"),
            ("標準・最小段階では M1 を省略する", "'最小段階'", "別の AI を使えない環境"),
            ("解決順・段階判定の手段", "'段階判定・実行段階'", "やり取りの範囲の判定"),
            ("6. **実行段階の判定**", "'段階判定・実行段階'", "やり取りの範囲の判定"),
            ("完了報告の最後に結末の行を書く", "'結末の行'", "結果の行"),
            ("失敗(結末の行が無い)", "'結末の行'", "無人の周の結果"),
        )
        for text, word, guidance in cases:
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", word, guidance])
        self.assert_appended_line_flagged("17. **実行環境の段階判定と縮退プロトコル**", ["'段階判定・実行段階'"], relpath="docs/design.md")
        self.assert_appended_line_flagged("    - **フル**(双方向の委託が可能な段階)。標準段階と同じ", ["'標準段階'"], relpath="docs/design.md")

    def test_replaced_words_of_316_list_and_bold_names_are_errors(self):
        # #316: 3 つの環境の古い名前の並び(「フル / 標準 / 最小」など)と太字の名前
        list_needles = ["置き換える語", "'フル / 標準 / 最小(並び)'", "双方向でやり取りできる環境"]
        bold_needles = ["置き換える語", "'**フル**・**標準**・**最小**(太字の名前)'", "一方向だけの環境"]
        for text in ("フル / 標準 / 最小 のどれで走るか", "実行形態: {フル / 標準 / 最小(段階を下げたとき)}",
                     "フル/標準", "標準・最小の環境", "標準／最小", "フル・最小"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, list_needles)
        for text in ("    - **フル**(双方向の委託が可能な段階): ", "- **標準**: 委託は一方向", "**最小**(サブエージェント機構なし)"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, bold_needles)
        self.assert_appended_line_flagged("    - **標準**(一方向の委託のみ)", ["'**フル**・**標準**・**最小**(太字の名前)'"],
                                          relpath="docs/design.md")

    def test_replaced_words_of_316_list_and_bold_lookalikes_are_not_flagged(self):
        # 区切りの無い「標準」・ほかの意味の並び・太字の中のほかの語は探さない
        for text in ("標準出力に出す", "標準の性能の段", "最上位 / 上位 / 標準", "報告調 / 標準 / プロジェクトの指示ファイルに口調を入れない",
                     "標準・軽量のどちらで実行したか", "標準・導入 bash の両方", "{quick | 標準 | area:{名} | deep}",
                     "モード: {フル / --quick / --layer=X}", "口調の型(標準・報告調)", "16. **図の標準**", "**標準ロジック**",
                     "**最小構成**", "フルパス", "最小 / 最大"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_words_of_318_are_errors(self):
        # #318: 置き換える語 4 語(名前・プレースホルダも探す)
        cases = (
            ("止めの印を置いて exit 20", "'止めの印'", "止める印"),
            ("`--expect=<守る値>`", "'守る値'", "控える値"),
            ("## 7. 周の中で守る値と照合(改竄ガード)", "'守る値'", "控える値"),
            ("## 4. 対話点の表", "'対話点'", "人に確かめる場面"),
            ("`<対話点番号で始まる理由>`", "'対話点'", "人に確かめる場面の番号"),
            ("停止ではなく報告点であり", "'報告点'", "判断を仰ぐ点"),
        )
        for text, word, guidance in cases:
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", word, guidance])
        self.assert_appended_line_flagged("update-doc の報告点など", ["'報告点'"], relpath="docs/design.md")

    def test_replaced_words_of_316_318_lookalikes_are_not_flagged(self):
        # 「段階」だけ・「結末」だけ(結末の値・`- 結末: …` の行)と、新しい語は探さない
        for text in ("段階を下げて続ける", "使えるのに下の段階で回さない", "実行の段階で決める", "段階的に実施", "第2段階で照合",
                     "要求がまだ調査段階", "スコープ縮小を設計段階で殺す", "結末: `PR`", "- 結末: `縮退` — x", "結末の値",
                     "結果の行を書く", "止める印を置く", "`--expect=<控える値>`", "人に確かめる場面の番号", "判断を仰ぐ点",
                     "双方向でやり取りできる環境", "一方向だけの環境", "別の AI を使えない環境", "やり取りの範囲の判定",
                     "フル監査", "最上位 / 上位 / 標準", "最小構成", "報告の点数", "対話の点"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_word_okikaeru_go_definition_line_in_design_is_exempt(self):
        # 「置き換える語」の定義の行は、その行の 4 語(守る値・対話点・報告点・止めの印)を除く
        self.edit_definition_line("- **置き換える語**", "例: 守る値・対話点番号・報告点・止めの印")
        self.assert_clean()

    def test_replaced_word_definition_line_does_not_exempt_other_group(self):
        # 定義の行で除くのは、その行で定義する語の組だけ。ほかの組の語は、その行でも ERROR にする
        cases = (
            ("- **置き換える語**", "例: 枡", "'枡'"),
            ("- **置き換える語**", "例: フル段階", "'フル段階'"),
            ("- **置き換える語**", "例: 結末の行", "'結末の行'"),
            ("- **文書の中で置き換える語**", "例: 守る値", "'守る値'"),
            ("- **文書の中で置き換える語**", "例: 止めの印", "'止めの印'"),
            ("- **置き換える語**", "例: サイレント", "'サイレント'"),
        )
        for prefix, add, word in cases:
            with self.subTest(prefix=prefix, add=add):
                target = self.repo / "docs" / "design.md"
                original = target.read_bytes()
                try:
                    no = self.edit_definition_line(prefix, add)
                    self.assert_error([f"docs/design.md:{no}:", "置き換える語", word])
                finally:
                    target.write_bytes(original)

    def test_replaced_word_definition_line_must_be_exactly_one(self):
        # 定義の行は design にちょうど 1 行ずつ。2 行以上・0 行は ERROR にする
        for prefix in ("- **置き換える語**", "- **文書の中で置き換える語**"):
            target = self.repo / "docs" / "design.md"
            original = target.read_bytes()
            with self.subTest(prefix=prefix, count=2):
                try:
                    self.append_to_checked_file(f"{prefix}: 二つ目の定義", relpath="docs/design.md", prefix="")
                    self.assert_error(["docs/design.md:", f"定義の行「{prefix}」が 2 行", "ちょうど 1 行"])
                finally:
                    target.write_bytes(original)
            with self.subTest(prefix=prefix, count=0):
                try:
                    self.edit_definition_line(prefix, new_prefix="- **言い換えの定義**")
                    self.assert_error(["docs/design.md:", f"定義の行「{prefix}」が 0 行", "ちょうど 1 行"])
                finally:
                    target.write_bytes(original)

    def test_replaced_word_okikaeru_go_prefix_elsewhere_is_error(self):
        # 行頭でない・字下げした・skill の文書の同じ字面は除かない
        for relpath, text in (
            ("docs/design.md", "  - **置き換える語**: 守る値"),
            ("docs/design.md", "注: - **置き換える語**: 守る値"),
            (self.REPLACED_TARGET, "- **置き換える語**: 守る値"),
        ):
            with self.subTest(relpath=relpath, text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'守る値'"], relpath=relpath)

    def test_replaced_word_silent_is_error(self):
        # #317: 「サイレント」(規則の名前・太字の項目名の中も探す)
        for text in ("8. **報告(サイレント縮退禁止)**: どちらの担い手で実装したか", "7. **サイレントスキップ禁止**。",
                     "明記した(サイレントスキップしていない)", "(サイレント切り捨て禁止)", "(サイレント無視禁止。design §7-2)",
                     "サイレント続行も、無意味な確認も避ける", "引き継ぐ(サイレント禁止)", "サイレントに内蔵へ切り替える"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, ["置き換える語", "'サイレント'", "黙って"])
        self.assert_appended_line_flagged("理由を報告に明記する(サイレント縮退禁止)", ["'サイレント'", "知らせずに"],
                                          relpath="docs/design.md")
        self.assert_appended_line_flagged("- [ ] 部分実施を報告に明記した(サイレントスキップ禁止)", ["'サイレント'"],
                                          relpath="plugins/dev-workflow/skills/create-task/SKILL.md")

    def test_replaced_word_silent_lookalikes_are_not_flagged(self):
        # 新しい言い方と、「サイレン」・コマンドの英字のフラグは探さない
        for text in ("黙って内蔵に切り替えない", "黙って代わりの手段に切り替えない", "黙って飛ばさない", "黙って切り捨てない",
                     "黙って無視しない", "知らせずに続ける", "黙って生成をやめない", "サイレンが鳴る", "`curl --silent`"):
            with self.subTest(text=text):
                self.assert_appended_line_flagged(text, [], flagged=False)

    def test_replaced_word_silent_definition_line_in_design_is_exempt(self):
        # 「文書の中で置き換える語」の定義の行は、その行の組の語(サイレントを含む)を除く
        self.edit_definition_line("- **文書の中で置き換える語**", "例: サイレント縮退禁止・サイレントスキップ禁止")
        self.assert_clean()

    # ----------------------------------------------- V1: 禁止パターンの免除マーカー

    def test_v1_exemption_word_alone_does_not_exempt(self):
        # 旧実装は「しない」を含む行を一律に免除していた
        self.append_to_checked_file(f"{self.home / 'private'} は使用しない")
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_marker_with_reason_exempts_the_line(self):
        self.append_to_checked_file(
            f"{self.home / 'private'} <!-- validate-allow: 規約の説明のための例示 -->"
        )
        self.assert_clean()

    def test_v1_marker_without_reason_does_not_exempt(self):
        self.append_to_checked_file(f"{self.home / 'private'} <!-- validate-allow -->")
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_empty_reason_followed_by_another_comment_does_not_exempt(self):
        # 理由を「最初のコメント終端まで」で取らないと、後ろのコメントの `-->` まで
        # 飲み込んで「理由あり」に化ける
        self.append_to_checked_file(
            f"{self.home / 'private'} <!-- validate-allow: --> <!-- 別のコメント -->"
        )
        self.assert_error(["ユーザー環境の絶対パス"])

    def test_v1_marker_does_not_exempt_delegation_words(self):
        # マーカーが救うのは禁止パターン検査だけ。委託の語は免除規則を持たない
        self.append_to_checked_file(
            "claude-opus-5 は使わない <!-- validate-allow: モデル ID の例示 -->"
        )
        status, errors, _ = self.run_validator()
        self.assertEqual(1, status, errors)
        self.assertTrue(any("委託の語" in e and "'opus'" in e for e in errors), errors)
        self.assertFalse(any("日付付き" in e for e in errors), errors)

    def test_v1_code_fence_does_not_exempt(self):
        # フェンス除外はリンク検査だけに掛ける(禁止パターンは行単位のマーカーだけ)
        self.write_file(CHECKED_MD, f"# t\n\n```\n{self.home / 'private'}\n```\n")
        self.assert_error(["ユーザー環境の絶対パス"])

    # ------------------------------------------------------ V2: SKILL.md の行数

    def test_v2_500_lines_with_trailing_newline_passes(self):
        self.write_skill_md("tool-check", 500, trailing_newline=True)
        self.assert_clean()

    def test_v2_501_lines_with_trailing_newline_is_error(self):
        self.write_skill_md("tool-check", 501, trailing_newline=True)
        self.assert_error(["tool-check/SKILL.md", "501 行"])

    def test_v2_500_lines_without_trailing_newline_passes(self):
        self.write_skill_md("tool-check", 500, trailing_newline=False)
        self.assert_clean()

    def test_v2_501_lines_without_trailing_newline_is_error(self):
        # `wc -l` はこれを 500 と数える —— そちらに揃えると規約違反を見逃す
        self.write_skill_md("tool-check", 501, trailing_newline=False)
        self.assert_error(["tool-check/SKILL.md", "501 行"])

    # -------------------------------------------- V3: 周辺プロジェクト名(環境変数)

    def test_v3_without_env_no_project_name_is_checked(self):
        # 偽 home に dev/customer-portal が在っても、環境変数が無ければ検査しない
        # (実行環境のファイルシステムを読まないことの判別機)
        self.assertTrue((self.home / "dev" / "customer-portal").is_dir())
        self.append_to_checked_file("customer-portal に配線する")
        self.assert_clean()

    def test_v3_with_env_project_name_is_detected(self):
        self.append_to_checked_file("customer-portal に配線する")
        self.assert_error(
            ["周辺プロジェクト固有名の混入", "customer-portal"],
            env={PROJECT_NAMES_ENV: "customer-portal"},
        )

    def test_v3_generic_name_is_excluded_even_when_passed(self):
        self.append_to_checked_file("base に配線する")
        self.assert_clean(env={PROJECT_NAMES_ENV: "base"})

    def test_v3_japanese_adjacent_is_detected(self):
        # `\b` は日本語を単語文字として扱うので、旧実装はこの 2 形を取りこぼしていた
        self.append_to_checked_file("customer-portalに配線する")
        self.append_to_checked_file("のcustomer-portalを見る")
        errors = self.assert_error(
            ["周辺プロジェクト固有名の混入"], env={PROJECT_NAMES_ENV: "customer-portal"}
        )
        self.assertEqual(2, sum("周辺プロジェクト固有名の混入" in e for e in errors), errors)

    def test_v3_alnum_and_underscore_adjacent_is_not_detected(self):
        # 除外集合を `[A-Za-z]` だけにすると、この 3 形で誤検出が出る
        for text in ("customer-portal123", "_customer-portal", "customer-portals"):
            self.append_to_checked_file(text)
        self.assert_clean(env={PROJECT_NAMES_ENV: "customer-portal"})

    def test_v3_hyphen_terminated_name_keeps_old_hits(self):
        # 境界を固定で両側に付ける実装はここで取りこぼす(`\b` からの退行)
        self.append_to_checked_file("Proj-x")
        self.append_to_checked_file("Proj-2")
        errors = self.assert_error(
            ["周辺プロジェクト固有名の混入", "Proj-"], env={PROJECT_NAMES_ENV: "Proj-"}
        )
        self.assertEqual(2, sum("周辺プロジェクト固有名の混入" in e for e in errors), errors)

    # ---------------------------------------------------- V4: description の字数

    def test_v4_too_short_description_is_error(self):
        self.set_description("tool-check", 149)
        self.assert_error(["tool-check/SKILL.md", "description 149 字"])

    def test_v4_too_long_description_is_error(self):
        self.set_description("tool-check", 501)
        self.assert_error(["tool-check/SKILL.md", "description 501 字"])

    def test_v4_boundary_descriptions_pass(self):
        for length in (150, 500):
            with self.subTest(length=length):
                self.set_description("tool-check", length)
                self.assert_clean()

    # ------------------------------ V9: frontmatter の allowed-tools / disallowed-tools

    def test_v9_allowed_tools_in_frontmatter_is_error(self):
        self.add_frontmatter_line("tool-check", "allowed-tools: Read, Grep")
        self.assert_error(["tool-check/SKILL.md", "frontmatter に allowed-tools がある", "design.md §6"])

    def test_v9_disallowed_tools_in_frontmatter_is_error(self):
        self.add_frontmatter_line("tool-check", "disallowed-tools: Edit, NotebookEdit")
        self.assert_error(["tool-check/SKILL.md", "frontmatter に disallowed-tools がある", "design.md §6"])

    def test_v9_both_keys_are_errors(self):
        self.add_frontmatter_line("tool-check", "allowed-tools: Read")
        self.add_frontmatter_line("tool-check", "disallowed-tools: Edit")
        errors = self.assert_error(["tool-check/SKILL.md", "frontmatter に allowed-tools がある"])
        self.assertTrue(any("frontmatter に disallowed-tools がある" in e for e in errors), errors)

    def test_v9_without_keys_passes_and_body_mention_is_not_checked(self):
        # 鍵が無ければ通る。本文でこの語に触れるだけ(frontmatter の外)は検査しない
        target = self.repo / "plugins" / "dev-workflow" / "skills" / "tool-check" / "SKILL.md"
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\nfrontmatter に allowed-tools・disallowed-tools を付けない。\n")
        self.assert_clean()

    # ------------------------------ 原則の節の、人が読む文の書き方の正本へのリンク

    def test_writing_link_in_principles_passes(self):
        # 写しの実ファイルに頼らず、原則の節にリンクを置いた最小の本文で通ることを見る。
        # do-task は正本と同じ skill なので、リンクの形が `references/…` になる
        for skill in ("tool-check", "do-task"):
            with self.subTest(skill=skill):
                self.rewrite_skill_body(
                    skill,
                    [f"# {skill}", "", "## 原則", "", "- 推測で進めない", writing_rules_line(skill),
                     "", "## 1. 手順", "", "本文"],
                )
                self.assert_clean()

    def test_writing_link_missing_is_error(self):
        line = self.writing_rules_line_of("tool-check")
        self.replace_in_skill_md("tool-check", line + "\n", "")
        self.assert_error(["tool-check/SKILL.md", WRITING_LINK_MISSING])

    def test_writing_link_without_principles_section_is_error(self):
        # 原則の節そのものが無いときも ERROR(リンクは H1 の下の段落に残る)
        self.replace_in_skill_md("tool-check", "\n## 原則\n", "\n")
        self.assertIn("writing-for-people.md](", self.skill_md("tool-check").read_text(encoding="utf-8"))
        self.assert_error(["tool-check/SKILL.md", "`## 原則` の節が無い"])

    def test_writing_link_only_outside_principles_is_error(self):
        # リンクは実在するのでリンク切れにはならない。原則の節の外にあるから ERROR になる。
        # 節の前(H1 の下)と、節の後(ファイルの末尾。別の `## ` の節の中)の両方を見る
        line = self.writing_rules_line_of("tool-check")
        original = self.skill_md("tool-check").read_text(encoding="utf-8")
        for where in ("前", "後"):
            with self.subTest(where=where):
                self.skill_md("tool-check").write_text(original, encoding="utf-8")
                self.replace_in_skill_md("tool-check", line + "\n", "")
                if where == "前":
                    self.replace_in_skill_md("tool-check", "\n## 原則\n", f"\n{line}\n\n## 原則\n")
                else:
                    with self.skill_md("tool-check").open("a", encoding="utf-8") as stream:
                        stream.write(f"\n{line}\n")
                errors = self.assert_error(["tool-check/SKILL.md", WRITING_LINK_MISSING])
                self.assertFalse(any("リンク切れ" in e for e in errors), errors)

    def test_writing_link_inside_code_fence_is_not_counted(self):
        line = self.writing_rules_line_of("tool-check")
        for opening, closing in (("```", "```"), ("~~~", "~~~")):
            with self.subTest(fence=opening):
                self.replace_in_skill_md("tool-check", line, f"{opening}\n{line}\n{closing}")
                self.assert_error(["tool-check/SKILL.md", WRITING_LINK_MISSING])
                # 次の subTest のために戻す
                self.replace_in_skill_md("tool-check", f"{opening}\n{line}\n{closing}", line)

    def test_writing_link_plain_mention_is_error(self):
        # リンクの形でない素の言及は数えない
        line = self.writing_rules_line_of("tool-check")
        for mention in (
            "- writing-for-people.md に従う",
            f"- `../{WRITING_RULES_REL}` を読み、それに従う",
        ):
            with self.subTest(mention=mention):
                self.replace_in_skill_md("tool-check", line, mention)
                self.assert_error(["tool-check/SKILL.md", WRITING_LINK_MISSING])
                self.replace_in_skill_md("tool-check", mention, line)

    def test_writing_link_to_another_file_with_the_same_name_is_error(self):
        # 名前だけでなく、SKILL.md の位置から解決した実パスで比べる
        self.write_file(f"{SKILLS_REL}/tool-check/writing-for-people.md", "# 別のファイル\n")
        line = self.writing_rules_line_of("tool-check")
        self.replace_in_skill_md(
            "tool-check", line, "- [writing-for-people.md](writing-for-people.md) を読み、それに従う"
        )
        errors = self.assert_error(["tool-check/SKILL.md", WRITING_LINK_MISSING])
        self.assertFalse(any("リンク切れ" in e for e in errors), errors)

    # ------------------------------------------- 検査 10: SKILL.md の本文の行の長さ

    def write_length_body(self, lines):
        """tool-check の本文を、原則の節(書き方の正本への短いリンク)と `lines` だけにする。
        リンクの行を短くするのは、試験で見たい行のほかに長い行を置かないため。"""
        link = f"../{WRITING_RULES_REL}"
        self.rewrite_skill_body(
            "tool-check",
            ["# tool-check", "", "## 原則", "", f"- [文を書くときの決まり]({link}) に従う", "", "## 1. 手順", ""]
            + lines,
        )

    def line_number_of(self, text):
        """写しの tool-check/SKILL.md で、行全体が `text` の行の番号(1 始まり)。"""
        lines = self.skill_md("tool-check").read_text(encoding="utf-8").split("\n")
        self.assertEqual(1, lines.count(text))
        return lines.index(text) + 1

    def line_length_warns(self):
        """写しの validate.py を走らせ、tool-check/SKILL.md の行の長さの WARN だけを返す。
        WARN だけでは落ちない(ERROR 0・終了コード 0)ことも確かめる。"""
        status, namespace, _ = self.run_validator_namespace()
        self.assertEqual([], namespace["ERRORS"])
        self.assertEqual(0, status)
        prefix = f"{SKILLS_REL}/tool-check/SKILL.md:"
        return [w for w in namespace["WARNS"] if w.startswith(prefix) and LINE_LENGTH_WARN in w]

    def assert_one_line_length_warn(self, line, length):
        warns = self.line_length_warns()
        self.assertEqual(1, len(warns), warns)
        self.assertIn(
            f"{SKILLS_REL}/tool-check/SKILL.md:{self.line_number_of(line)}: 行が {length} 字", warns[0]
        )

    def test_line_length_200_chars_passes(self):
        for line in ("あ" * 200, "- " + "あ" * 198, "  - " + "a" * 196):
            with self.subTest(line=line[:6]):
                self.write_length_body([line])
                self.assertEqual([], self.line_length_warns())

    def test_line_length_201_chars_is_warn(self):
        for line in ("あ" * 201, "- " + "あ" * 199, "1. " + "a" * 198):
            with self.subTest(line=line[:6]):
                self.write_length_body([line])
                self.assert_one_line_length_warn(line, 201)

    def test_line_length_excluded_lines_are_not_counted(self):
        long = "あ" * 250
        cases = (
            ("``` のフェンス", ["```", long, "```"]),
            ("info string 付きの ``` のフェンス", ["```bash", long, "```"]),
            ("~~~ のフェンス", ["~~~", long, "~~~"]),
            ("表の行", [f"| {long} |"]),
            ("字下げした表の行", [f"  | {long} |", f"\t| {long} |"]),
        )
        for name, lines in cases:
            with self.subTest(case=name):
                self.write_length_body(lines)
                self.assertEqual([], self.line_length_warns())
        with self.subTest(case="frontmatter"):
            self.write_length_body([])
            self.set_description("tool-check", 500)
            self.assertIn("\ndescription: " + "あ" * 500 + "\n", self.skill_md("tool-check").read_text(encoding="utf-8"))
            self.assertEqual([], self.line_length_warns())

    def test_line_length_counts_again_after_fence_closes(self):
        # フェンスが閉じた後の行は数える(閉じを見落とすと、以降の長い行が黙って通る)
        line = "あ" * 201
        for opening, closing in (("```", "```"), ("~~~", "~~~")):
            with self.subTest(fence=opening):
                self.write_length_body([opening, "あ" * 250, closing, "", line])
                self.assert_one_line_length_warn(line, 201)

    def test_line_length_long_inline_code_alone_passes(self):
        # 200 字を超えるインラインのコード(コマンドの字面)は分けられないので数えない
        code = "`" + "a" * 250 + "`"
        for line in (code, "- " + code, "- 次を打つ: " + code, f"- {code} と {code}"):
            with self.subTest(line=line[:8]):
                self.write_length_body([line])
                self.assertEqual([], self.line_length_warns())

    def test_line_length_long_inline_code_with_201_chars_of_text_is_warn(self):
        code = "`" + "a" * 250 + "`"
        cases = (
            "あ" * 201 + code,
            code + "あ" * 201,
            "あ" * 100 + code + "あ" * 101,
            # 200 字ちょうどのインラインのコードは数えから引かない(引くのは 200 字を超えるものだけ)
            "あ" + "`" + "a" * 198 + "`",
        )
        for line in cases:
            with self.subTest(line=line[:8]):
                self.write_length_body([line])
                self.assert_one_line_length_warn(line, 201)

    # ------------------------------------------------------------ V5: リンク検査

    def test_v5_broken_link_with_fragment_is_error(self):
        self.write_file(CHECKED_MD, "# t\n\n[x](nope.md#sec)\n")
        self.assert_error(["リンク切れ", "nope.md#sec"])

    def test_v5_broken_link_with_title_is_error(self):
        self.write_file(CHECKED_MD, '# t\n\n[x](nope.md "t")\n')
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_broken_link_inside_code_fence_is_ignored(self):
        for opening, closing in (("```", "```"), ("~~~", "~~~")):
            with self.subTest(fence=opening):
                self.write_file(
                    CHECKED_MD, f"# t\n\n{opening}\n[x](nope.md)\n{closing}\n"
                )
                self.assert_clean()

    def test_v5_fence_is_not_closed_by_a_different_character(self):
        # ~~~ で開いたブロックは ``` では閉じない(閉じたと誤認すると、以降の内外が反転する)
        self.write_file(CHECKED_MD, "# t\n\n~~~\n```\n[x](nope.md)\n~~~\n")
        self.assert_clean()

    def test_v5_longer_fence_is_not_closed_by_a_shorter_one(self):
        # ```` の中の ``` は閉じフェンスにならない(CommonMark)
        self.write_file(CHECKED_MD, "# t\n\n````\n```\n[x](nope.md)\n```\n````\n")
        self.assert_clean()

    def test_v5_closing_fence_must_not_have_an_info_string(self):
        # info string を持つ行は閉じフェンスにならない(CommonMark)。これを見落とすと
        # そこで閉じたことになり、以降の内外が反転して検査が黙って止まる
        self.write_file(CHECKED_MD, "# t\n\n~~~\n~~~text\n[x](nope.md)\n~~~\n")
        self.assert_clean()

    def test_v5_indented_four_spaces_does_not_open_a_fence(self):
        # 4 空白以上の字下げはインデントコードブロックで、フェンスを開かない(CommonMark)。
        # ここで開いてしまうと、以降の実リンクが検査されなくなる
        self.write_file(CHECKED_MD, "# t\n\n    ```\n\n[x](nope.md)\n")
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_inline_code_at_line_start_is_not_a_fence(self):
        # 行頭のインラインコードをフェンス開始と誤認すると、そこから下が丸ごと
        # 検査されなくなる(取りこぼし側の退行)
        self.write_file(CHECKED_MD, "# t\n\n```x``` を使う\n[y](nope.md)\n")
        self.assert_error(["リンク切れ", "nope.md"])

    def test_v5_existing_link_with_fragment_passes(self):
        self.write_file(CHECKED_MD, "# t\n\n[x](ok.md#sec)\n")
        self.write_file(str(Path(CHECKED_MD).parent / "ok.md"), "# ok\n")
        self.assert_clean()

    def test_v5_existing_link_with_title_passes(self):
        # タイトルをパスに含めて「実在しない」と誤判定しないこと
        self.write_file(CHECKED_MD, '# t\n\n[x](ok.md "t")\n')
        self.write_file(str(Path(CHECKED_MD).parent / "ok.md"), "# ok\n")
        self.assert_clean()

    # -------------------------------------------------------------- V6: 配布メタ

    def test_v6_plugin_version_mismatch_is_error(self):
        self.patch_json(PLUGIN_JSON, lambda d: d.__setitem__("version", "9.9.9"))
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_marketplace_metadata_version_mismatch_is_error(self):
        self.patch_json(MARKETPLACE_JSON, lambda d: d["metadata"].__setitem__("version", "9.9.9"))
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_missing_source_is_error(self):
        self.patch_json(
            MARKETPLACE_JSON, lambda d: d["plugins"][0].__setitem__("source", "./nope")
        )
        self.assert_error(["source が実在しない", "./nope"])

    def test_v6_skill_count_claim_mismatch_is_error(self):
        self.patch_json(
            MARKETPLACE_JSON,
            lambda d: d["plugins"][0].__setitem__(
                "description", d["plugins"][0]["description"].replace("skills 13 種", "skills 99 種")
            ),
        )
        self.assert_error(["skill 件数の表記 99 件", "実数 13 件"])

    def test_v6_plugin_json_skill_count_mismatch_is_error(self):
        # marketplace.json 側だけを壊すと、plugin.json 側の検査を消しても気づけない
        self.patch_json(
            PLUGIN_JSON,
            lambda d: d.__setitem__("description", d["description"].replace("13 skills", "99 skills")),
        )
        self.assert_error(["plugin.json", "skill 件数の表記 99 件", "実数 13 件"])

    def test_v6_all_versions_missing_is_error(self):
        # 3 箇所とも欠けていると set の要素数が 1 になり、空虚に「一致」して通ってしまう
        self.patch_json(PLUGIN_JSON, lambda d: d.pop("version", None))
        self.patch_json(
            MARKETPLACE_JSON,
            lambda d: (d["metadata"].pop("version", None), d["plugins"][0].pop("version", None)),
        )
        self.assert_error(["配布メタの version が一致しない"])

    def test_v6_intact_distribution_meta_passes(self):
        # 「何も壊さない写し」。版 3 箇所・source・件数の表記が実際に揃っていることを
        # 直接確かめてから、検査が通ることを見る(無改変の写しを見るだけでは、
        # この検査が動いていなくても緑になる)
        mp = json.loads((self.repo / MARKETPLACE_JSON).read_text(encoding="utf-8"))
        pj = json.loads((self.repo / PLUGIN_JSON).read_text(encoding="utf-8"))
        versions = {mp["metadata"]["version"], pj["version"]} | {
            pl["version"] for pl in mp["plugins"]
        }
        self.assertEqual(1, len(versions), versions)
        self.assertTrue((self.repo / mp["plugins"][0]["source"]).exists())
        actual = len([d for d in (self.repo / SKILLS_REL).iterdir() if d.is_dir()])
        for desc in (mp["plugins"][0]["description"], pj["description"]):
            found = {int(m.group(1) or m.group(2)) for m in SKILL_COUNT_RE.finditer(desc)}
            self.assertEqual({actual}, found, desc[:60])
        self.assert_clean()

    # ------------------------------------------------------------- V8: 委託の語

    def test_v8_japanese_after_agent_is_detected(self):
        self.append_to_checked_file("Agentに委託する")
        self.assert_error(["委託の語", "'Agent'"])

    def test_v8_japanese_before_agent_is_detected(self):
        # 後方だけの否定先読みでは取りこぼす形
        self.append_to_checked_file("委託はAgentで")
        self.assert_error(["委託の語", "'Agent'"])

    def test_v8_alpha_adjacent_words_are_not_detected(self):
        # 既存の検査語(ListAgents)を含まないダミー語を選ぶ。
        # `SubAgent` は**前に英字が続く**形で、否定後読みが無いと誤検出になる
        for text in ("Agentworks", "SubAgent"):
            self.append_to_checked_file(text)
        status, errors, _ = self.run_validator()
        self.assertEqual(0, sum("委託の語" in e for e in errors), errors)
        self.assertEqual(0, status, errors)

    # ------------------------------------------------------ ホスト CLI 語の検査

    def assert_host_cli_result(self, text, detected, relpath=CHECKED_MD, line=3, keep_body=False):
        """1 ケースずつ実際の検証器へ渡し、他カテゴリの ERROR による偽陽性も防ぐ。"""
        if keep_body:
            # 範囲の試験で既存文書を使うときは、名前の定義と参照形を残す。
            original = (self.repo / relpath).read_text(encoding="utf-8")
            line = original.count("\n") + 2
            self.write_file(relpath, f"{original}\n{text}\n")
        else:
            # 用例の「委託」は先に説明する。検査対象の行番号 3 は保つ。
            self.write_file(relpath, f"# t\n委託(作業を別の AI に任せること)。\n{text}\n")
        status, errors, _ = self.run_validator()
        hits = [e for e in errors if "ホスト固有の CLI 語" in e]
        self.assertEqual(hits, errors, errors)
        self.assertEqual(int(detected), len(hits), errors)
        self.assertEqual(int(detected), status, errors)
        if detected:
            self.assertIn(f"{relpath}:{line}: ホスト固有の CLI 語", hits[0])
        return hits

    def test_host_cli_issue_101_examples(self):
        cases = (
            ("P1", "Geminiに実装を委託する", True),
            ("P2", "Codex execで委託", True),
            ("P3", "cursor-agentで実装", True),
            ("N1", "my-cursor-agent-wrapper", False),
            ("N2", "geminibot", False),
            ("N3", "workspace-writer", False),
            ("N4", "x-codex-rescue-y", False),
            ("P4", "chrome-devtools-mcp", True),
        )
        for case, text, detected in cases:
            with self.subTest(case=case, text=text):
                self.assert_host_cli_result(text, detected)

    def test_host_cli_all_words_and_boundaries(self):
        for word in HOST_CLI_BOUNDED_WORDS + HOST_CLI_MCP_WORDS:
            cases = (
                word, word[0].upper() + word[1:], f"設定は{word}で", f"{word}で", f"設定は{word}",
                f" {word} ", f"、{word}。", f"'{word}'", f'"{word}"',
                f"/{word}/", f".{word}.", f"@{word}@",
            )
            for text in cases:
                with self.subTest(word=word, text=text):
                    self.assert_host_cli_result(text, True)

    def test_host_cli_ascii_adjacent_left_is_not_detected(self):
        for word in HOST_CLI_BOUNDED_WORDS:
            for adjacent in ("x", "Z", "2", "_", "-"):
                with self.subTest(word=word, adjacent=adjacent):
                    self.assert_host_cli_result(adjacent + word, False)

    def test_host_cli_ascii_adjacent_right_is_not_detected(self):
        for word in HOST_CLI_BOUNDED_WORDS:
            for adjacent in ("x", "Z", "2", "_", "-"):
                with self.subTest(word=word, adjacent=adjacent):
                    self.assert_host_cli_result(word + adjacent, False)

    def test_host_cli_non_ascii_adjacent_is_detected(self):
        # IGNORECASE の [A-Z] は İ / ı / ſ / K にも一致するが、境界は ASCII だけ。
        for word in HOST_CLI_BOUNDED_WORDS:
            for adjacent in ("に", "é", "İ", "ı", "ſ", "K"):
                for text in (adjacent + word, word + adjacent):
                    with self.subTest(word=word, text=text):
                        self.assert_host_cli_result(text, True)

    def test_host_cli_mcp_derivatives_are_detected(self):
        for text in (
            "chrome-devtools-mcp@latest", "CHROME-DEVTOOLS-MCPに接続",
            "claude-in-chrome-helper", "my-chrome-devtools-wrapper",
        ):
            with self.subTest(text=text):
                self.assert_host_cli_result(text, True)
        # MCP は左右とも境界なし。片側だけの派生も独立に固定する。
        for word in HOST_CLI_MCP_WORDS:
            for adjacent in ("x", "2", "_", "-"):
                for text in (adjacent + word, word + adjacent):
                    with self.subTest(text=text):
                        hits = self.assert_host_cli_result(text, True)
                        self.assertIn(repr(word), hits[0])

    def test_host_cli_commands_keep_unicode_whitespace(self):
        for command in ("exec", "review", "mcp"):
            for space in (" ", "   ", "\t", "\u3000", "\n"):
                with self.subTest(command=command, space=space):
                    self.assert_host_cli_result(f"codex{space}{command}で委託", True)

    def test_host_cli_multiline_command_marker_uses_start_line(self):
        marker = "<!-- validate-allow: 生成する設定の識別子 -->"
        self.assert_host_cli_result(f"{marker} codex\nexecで委託", False)
        self.assert_host_cli_result(f"codex\nexecで委託 {marker}", True)

    def test_host_cli_marker_reason_and_mcp_derivatives(self):
        for word in ("gemini", "chrome-devtools-mcp", "claude-in-chrome-helper"):
            for marker, detected in (
                ("<!-- validate-allow: 生成する設定の識別子 -->", False),
                ("<!-- validate-allow -->", True),
                ("<!-- validate-allow: -->", True),
                ("<!-- validate-allow:   -->", True),
            ):
                with self.subTest(word=word, marker=marker):
                    self.assert_host_cli_result(f"{word} {marker}", detected)
            with self.subTest(word=word, marker="別行"):
                self.assert_host_cli_result(
                    f"<!-- validate-allow: 生成する設定の識別子 -->\n{word}", True, line=4
                )

    def test_host_cli_product_names_and_config_path_are_not_detected(self):
        for text in ("Codex", "Cursor", "Claude Code", "read-only", ".codex/config.toml"):
            with self.subTest(text=text):
                self.assert_host_cli_result(text, False)

    def test_host_cli_scan_scope(self):
        cases = (
            ("tool-check/references/validate-test.md", True),
            ("tool-check/references/nested/validate-test.md", True),
            ("tool-check/README.md", True),
            ("tool-check/validate-test.txt", True),
            ("tool-check/scripts/validate-test.py", False),
            ("tool-check/templates/validate-test.md", False),
            ("tool-check/references/validate-test.txt", False),
            ("tool-check/validate-test.png", False),
            ("tool-check/validate-test.jpg", False),
            ("do-task/references/delegation-map.md", False),
            ("do-task/references/external-runners.md", False),
        )
        for path, detected in cases:
            with self.subTest(path=path):
                relpath = f"{SKILLS_REL}/{path}"
                target = self.repo / relpath
                original = target.read_bytes() if target.exists() else None
                try:
                    self.assert_host_cli_result("gemini", detected, relpath=relpath, keep_body=original is not None)
                finally:
                    if original is None:
                        target.unlink()
                    else:
                        target.write_bytes(original)

    def assert_host_cli_question_guidance(self, hit):
        """質問の道具名と引数名の ERROR は、案内が解決表の質問の節を指し、ほかの語の案内を持たない。"""
        self.assertIn(HOST_CLI_QUESTION_GUIDANCE, hit)
        self.assertNotIn(HOST_CLI_OTHER_GUIDANCE, hit)

    def test_host_cli_question_guidance(self):
        for word in HOST_CLI_QUESTION_WORDS:
            with self.subTest(word=word):
                hits = self.assert_host_cli_result(word, True)
                # 文の頭の字面は、ほかの語と同じ。括弧の中の案内だけが違う
                self.assertTrue(
                    hits[0].startswith(f"{CHECKED_MD}:3: ホスト固有の CLI 語 -> {word!r}("), hits
                )
                self.assert_host_cli_question_guidance(hits[0])
        # 対照: ほかの語の案内は external-runners.md を指し、解決表の質問の節を指さない
        with self.subTest(word="gemini"):
            hits = self.assert_host_cli_result("gemini", True)
            self.assertIn(HOST_CLI_OTHER_GUIDANCE, hits[0])
            self.assertNotIn("§8", hits[0])

    def test_host_cli_question_case_variants(self):
        # 末尾の変種は s をロングエス(ſ)にした形。大小無視の照合では s に一致するが、
        # 字面の lower() では s に戻らない —— 案内を字面から選ぶと、ほかの語の案内になる
        for text in (
            "askuserquestion", "ASKUSERQUESTION", "multiselect", "MULTISELECT", "MultiSelect",
            "AſkUserQueſtion",
        ):
            with self.subTest(text=text):
                hits = self.assert_host_cli_result(text, True)
                self.assertIn(repr(text), hits[0])
                self.assert_host_cli_question_guidance(hits[0])

    def test_host_cli_question_marker(self):
        reason = "<!-- validate-allow: 質問の道具の名前を例として示す -->"
        for word in HOST_CLI_QUESTION_WORDS:
            for marker, detected in (
                (reason, False),
                ("<!-- validate-allow -->", True),
                ("<!-- validate-allow: -->", True),
            ):
                with self.subTest(word=word, marker=marker):
                    hits = self.assert_host_cli_result(f"{word} {marker}", detected)
                    if detected:
                        self.assert_host_cli_question_guidance(hits[0])
            # マーカーは、その行だけに効く
            with self.subTest(word=word, marker="別行"):
                hits = self.assert_host_cli_result(f"{reason}\n{word}", True, line=4)
                self.assert_host_cli_question_guidance(hits[0])

    def test_host_cli_question_scope(self):
        cases = (
            ("tool-check/README.md", True),
            ("tool-check/references/validate-test.md", True),
            ("tool-check/scripts/validate-test.py", False),
            ("do-task/references/delegation-map.md", False),
            ("do-task/references/external-runners.md", False),
        )
        for word in HOST_CLI_QUESTION_WORDS:
            for path, detected in cases:
                with self.subTest(word=word, path=path):
                    relpath = f"{SKILLS_REL}/{path}"
                    target = self.repo / relpath
                    original = target.read_bytes() if target.exists() else None
                    try:
                        self.assert_host_cli_result(word, detected, relpath=relpath, keep_body=original is not None)
                    finally:
                        if original is None:
                            target.unlink()
                        else:
                            target.write_bytes(original)

    def test_host_cli_question_adjacent(self):
        # 日本語の直結は検出し、英字が続く別の語は検出しない
        for text, detected in (
            ("AskUserQuestionで", True),
            ("multiSelectで", True),
            ("AskUserQuestions", False),
            ("multiSelected", False),
        ):
            with self.subTest(text=text):
                self.assert_host_cli_result(text, detected)

    def test_host_cli_words_are_detected(self):
        # 書き先は references/*.md に固定する —— CHECKED_PY は scripts/ 配下で
        # _in_host_cli_scope() の対象外なので、そこに書くと ERROR 0 になり空虚に真になる
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(CHECKED_MD, f"# t\n\n{word} を使う\n")
                self.assert_error(["ホスト固有の CLI 語", word])

    def test_host_cli_words_are_not_detected_in_scripts_dir(self):
        # 上のテストの「書き先を固定する」理由を固定する(scripts/ は対象外)
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            self.append_to_checked_file(word)
        self.assert_clean()

    def test_host_cli_words_are_exempted_by_marker(self):
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(
                    CHECKED_MD,
                    f"# t\n\n{word} を使う <!-- validate-allow: 生成する設定の識別子 -->\n",
                )
                self.assert_clean()

    def test_host_cli_marker_exempts_only_its_own_line(self):
        # 免除は**その行だけ**に効く。ファイル単位にすると、一度マーカーを置いた
        # ファイルでは以後どれだけ混入しても捕まらなくなる(設計がファイル全体の
        # 除外を退けた理由そのもの)
        self.write_file(
            CHECKED_MD,
            "# t\n\nchrome-devtools <!-- validate-allow: 生成する設定の識別子 -->\n"
            "chrome-devtools を素で足す\n",
        )
        errors = self.assert_error(["ホスト固有の CLI 語", "chrome-devtools"])
        # **1 件だけ**であることまで見る。件数を見ないと、配線を丸ごと外して
        # 2 件になった場合(免除が一切効かない退行)を素通りさせる
        hits = [e for e in errors if "ホスト固有の CLI 語" in e]
        self.assertEqual(1, len(hits), hits)

    def test_host_cli_marker_without_reason_does_not_exempt(self):
        for word in ("run_in_background", "claude-in-chrome", "chrome-devtools"):
            with self.subTest(word=word):
                self.write_file(CHECKED_MD, f"# t\n\n{word} を使う <!-- validate-allow -->\n")
                self.assert_error(["ホスト固有の CLI 語", word])

    def test_marker_does_not_exempt_delegation_words(self):
        # マーカーが効くのは禁止パターン検査とホスト CLI 語検査の 2 つだけ。
        # 委託の語は役割語へ書き換えて消すので免除規則を持たない
        self.write_file(
            CHECKED_MD, "# t\n\nAgentに委託する <!-- validate-allow: 理由 -->\n"
        )
        self.assert_error(["委託の語", "'Agent'"])

    def test_marker_does_not_exempt_delegation_map_invariant(self):
        target = self.repo / "plugins" / "dev-workflow" / "skills" / "do-task" / "references" / "delegation-map.md"
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\nopus を使う <!-- validate-allow: 理由 -->\n")
        self.assert_error(["除外の不変条件", "'opus'"])

    # ---------------------------------------------- 既存テスト(V3 の方式に追従)

    def test_base_directory_is_treated_as_generic(self):
        self.append_to_checked_file("base")
        self.assert_clean(env={PROJECT_NAMES_ENV: "base"})

    def test_project_specific_directory_name_is_detected(self):
        project_name = "customer-portal"
        self.append_to_checked_file(project_name)
        self.assert_error(
            ["周辺プロジェクト固有名の混入", project_name], env={PROJECT_NAMES_ENV: project_name}
        )

    def test_user_absolute_path_is_detected(self):
        absolute_path = self.home / "private" / "notes"
        self.append_to_checked_file(str(absolute_path))
        self.assert_error(["ユーザー環境の絶対パス", str(self.home)])


class YamlDependencyTest(unittest.TestCase):
    """依存なし・壊れた依存でも検証成功へ切り替わらないことを確認する。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        shutil.copytree(SOURCE_REPO, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.script = self.repo / "scripts/validate.py"
        self.skill = self.repo / SKILLS_REL / "tool-check/SKILL.md"
        self.original = self.skill.read_text(encoding="utf-8")

    def malformed(self, line):
        self.skill.write_text(self.original.replace("name: tool-check\n", "name: tool-check\n" + line + "\n", 1), encoding="utf-8")

    def run_process(self, *, isolated=False, fake_yaml=None):
        code = "import runpy,sys; "
        args = []
        if fake_yaml is not None:
            package = self.root / "fake"
            package.mkdir(exist_ok=True)
            (package / "yaml.py").write_text(fake_yaml, encoding="utf-8")
            code += "sys.path.insert(0, sys.argv[2]); "
            args.append(str(package))
        code += ("ns=runpy.run_path(sys.argv[1],run_name='validate_under_test'); "
                 "print('MODULE_LOADED'); raise SystemExit(ns['main']())")
        env = {k: v for k, v in os.environ.items() if k != PROJECT_NAMES_ENV}
        return subprocess.run([sys.executable, *(['-I', '-S'] if isolated else []),
                               '-c', code, str(self.script), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def assert_dependency_failure(self, result):
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('MODULE_LOADED', result.stdout)
        self.assertEqual(1, len([line for line in result.stdout.splitlines() if line.startswith('ERROR ')]), result.stdout)
        self.assertIn('PyYAML', result.stdout)
        self.assertIn('-m pip install -r', result.stdout)
        self.assertIn('requirements-dev.txt', result.stdout)
        self.assertIn('結果: ERROR 1 件 / WARN 0 件', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('frontmatter に name がない', result.stdout)

    def test_missing_dependency_rejects_normal_and_malformed_in_subprocess(self):
        for line in (None, 'broken: [unclosed', 'broken: "unclosed', 'broken: {a: b'):
            with self.subTest(line=line):
                if line:
                    self.malformed(line)
                self.assert_dependency_failure(self.run_process(isolated=True))

    def test_broken_import_and_api_reject_in_subprocess(self):
        for fake in ("raise ImportError('yaml internal dependency unavailable')\n",
                     "broken = [\n", "raise RuntimeError('yaml initialization failed')\n",
                     "unrelated = 1\n", "safe_load = None\n"):
            with self.subTest(fake=fake):
                self.assert_dependency_failure(self.run_process(isolated=True, fake_yaml=fake))

    def test_installed_yaml_accepts_normal_and_rejects_malformed(self):
        result = self.run_process()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('結果: ERROR 0 件 / WARN 0 件', result.stdout)
        for line in ('broken: [unclosed', 'broken: "unclosed', 'broken: {a: b'):
            with self.subTest(line=line):
                self.malformed(line)
                result = self.run_process()
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn('frontmatter YAML パース失敗', result.stdout)

    def test_direct_parser_rejects_missing_dependency(self):
        with mock.patch.dict(sys.modules, {'yaml': None}):
            ns = runpy.run_path(str(self.script), run_name='validate_under_test')
            value = ns['parse_frontmatter']('---\nname: test\nbroken: [unclosed\n---\n', Path('fixture.md'))
        self.assertEqual({}, value)
        self.assertEqual(1, len(ns['ERRORS']))
        self.assertIn('PyYAML', ns['ERRORS'][0])

    def test_parser_import_error_never_falls_back(self):
        import types
        fake = types.SimpleNamespace(safe_load=mock.Mock(side_effect=ImportError('loader unavailable')))
        with mock.patch.dict(sys.modules, {'yaml': fake}):
            ns = runpy.run_path(str(self.script), run_name='validate_under_test')
            value = ns['parse_frontmatter']('---\nname: test\n---\n', Path('fixture.md'))
        self.assertEqual({}, value)
        self.assertEqual(1, len(ns['ERRORS']))
        self.assertIn('frontmatter YAML パース失敗', ns['ERRORS'][0])

    def test_import_base_exception_is_not_swallowed(self):
        for exception in ('KeyboardInterrupt', 'SystemExit(37)'):
            with self.subTest(exception=exception):
                result = self.run_process(isolated=True, fake_yaml='raise ' + exception + '\n')
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('PyYAML', result.stdout)
                if exception.startswith('SystemExit'):
                    self.assertEqual(37, result.returncode)


class WordingTest(unittest.TestCase):
    """初出を既存本文に左右されない、小さな文書で確かめる。"""

    reference = f"{SKILLS_REL}/wording-test/references/space name.md"
    exceptions = "scripts/wording-exceptions.json"
    explanation = "照らし合わせて確かめること"
    first_error = "言い換えの初出"
    duplicate_error = "言い換えの重複"
    reduced_error = "名前でもコードでもない「縮退」"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.write("scripts/validate.py", (SOURCE_REPO / "scripts/validate.py").read_text(encoding="utf-8"))
        self.write("docs/design.md", (SOURCE_REPO / "docs/design.md").read_text(encoding="utf-8"))
        self.write(self.reference, "本文。\n")
        self.write_exceptions()

    def write(self, relative, text):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_exceptions(self, names=None, reports=None):
        self.write(self.exceptions, json.dumps({
            "version": 1, "names": names or [], "reports": reports or [],
        }, ensure_ascii=False))

    def check(self, text=None):
        if text is not None:
            self.write(self.reference, text)
        namespace = runpy.run_path(str(self.repo / "scripts/validate.py"), run_name="wording_under_test")
        with contextlib.redirect_stdout(io.StringIO()):
            namespace["check_wording"]()
        self.assertEqual([], namespace["WARNS"])
        return namespace["ERRORS"]

    def assert_wording(self, text, expected, relative=None):
        """expected は (診断の種類, 語, 行番号)。件数と各診断の出典を確かめる。"""
        errors = self.check(text)
        self.assertEqual(len(expected), len(errors), errors)
        for kind, word, line in expected:
            self.assertTrue(any(
                kind in error and word in error
                and f"{relative or self.reference}:{line}:" in error
                for error in errors
            ), errors)
        return errors

    def name_entry(self, name="照合パターン", form="「照合パターン」"):
        self.write("docs/wording-names.md", f"# {name}\n")
        self.write(self.reference, form + "\n")
        return {
            "name": name,
            "definition": {"path": "docs/wording-names.md", "text": name},
            "files": [self.reference], "forms": [form],
            "reason": "別の文書が指す名前。",
        }

    def assert_dictionary_error(self):
        errors = self.check()
        self.assertTrue(errors, "壊れた除外一覧を受け付けた")
        self.assertTrue(all("言葉の除外一覧" in error for error in errors), errors)
        return errors

    def test_first_occurrence_is_reported_once_at_original_line(self):
        self.assert_wording("# 見出し\n\n照合する。\n再び照合する。\n", [
            (self.first_error, "照合", 3),
        ])

    def test_all_27_terms_use_the_design_table(self):
        design = (self.repo / "docs/design.md").read_text(encoding="utf-8")
        table = design.split("| 語 | 言い換えの字面 |\n|---|---|\n", 1)[1].split("\n\n", 1)[0]
        pairs = [line.strip("| ").split(" | ") for line in table.splitlines()]
        self.assertEqual(27, len(pairs))
        for label, explanation in pairs:
            # 「X(Y)」の語は X と Y の両方を確かめる(表の字面から分ける)。
            alias = re.fullmatch(r"([^()]+)\(([^()]+)\)", label)
            terms = alias.groups() if alias else (label,)
            for term in terms:
                with self.subTest(term=term):
                    self.assert_wording(f"{term}({explanation})。\n", [])
                    self.assert_wording(f"{term}。\n", [(self.first_error, term[:3], 1)])

    def test_explanation_is_read_from_changed_design_table(self):
        path = self.repo / "docs/design.md"
        design = path.read_text(encoding="utf-8")
        old = f"| 照合 | {self.explanation} |"
        self.assertEqual(1, design.count(old))
        path.write_text(design.replace(old, "| 照合 | 新しく決めた説明 |"), encoding="utf-8")
        self.assert_wording("照合(新しく決めた説明)。\n", [])
        self.assert_wording(f"照合({self.explanation})。\n", [(self.first_error, "照合", 1)])

    def test_37_markdown_controls(self):
        explained = f"照合({self.explanation})"
        cases = (
            ("code-link-tail", "`表示](先` 照合)。\n", 1),
            ("comment-link-tail", "<!-- ]( -->照合)。\n", 1),
            ("fence-in-comment", "<!--\n```\n-->\n照合。\n", 4),
            ("comment-with-fence", "<!-- ``` -->\n照合。\n", 2),
            ("double-backtick", "`` `照合` ``\n", None),
            ("reverse", f"{self.explanation}(照合)。\n", None),
            ("reverse-loose", f"{self.explanation}など(照合)。\n", 1),
            ("reverse-noclose", f"{self.explanation}(照合。\n", 1),
            ("forward-noclose", f"照合({self.explanation}。\n", 1),
            ("forward-mismatch", f"照合({self.explanation}〉。\n", 1),
            ("forward-supplement", f"照合({self.explanation}。補足)。\n", None),
            ("forward-nested", f"照合({self.explanation}。補足(内側))。\n", None),
            ("nakaguro", f"照合・確認({self.explanation})。\n", 1),
            ("indented-heading", "   # 照合\n", None),
            ("setext", "照合\n===\n", None),
            ("quote-setext", "> 照合\n> ===\n", None),
            ("quote-setext2", "> 照合\n> ---\n", None),
            ("list-setext", "- 照合\n  ===\n", None),
            ("hr-after-list", f"- {explained}\n---\n", None),
            ("hr-after-blank", f"{explained}\n\n---\n", None),
            ("multi-comment", "前<!--\n照合\n-->照合。\n", 3),
            ("code-comment-start", f"`<!--` {explained}。\n", None),
            ("bt-in-comment", f"<!-- ` --> {explained}。\n", None),
            ("link-paren", "[表示](https://example.test/照合(a))\n", None),
            ("link-paren-space", '[表示](https://example.test/照合(a) "題")\n', None),
            ("refdef", "[表示][r]\n\n[r]: https://example.test/照合\n", None),
            ("refdef-nospace", "[表示][r]\n\n[r]:https://example.test/照合\n", None),
            ("refdef-quote", "> [表示][r]\n>\n> [r]: https://example.test/照合\n", None),
            ("refdef-list", "- [表示][r]\n\n  [r]: https://example.test/照合\n", None),
            ("refdef-escape", "[表示][r\\]]\n\n[r\\]]: https://example.test/照合\n", None),
            ("link-multiline", "[表示](\nhttps://example.test/照合\n)\n", None),
            ("link-angle", "[表示](<https://example.test/照合>)\n", None),
            ("link-space", "[表示](<some path/照合>)\n", None),
            ("link-simple", "[表示](https://example.test/照合)\n", None),
            ("link-in-code", "`[照合](https://example.test)`\n", None),
            # 下線形式の見出しにならない形。frontmatter を除かないと、リストの項目として数える。
            ("frontmatter", "---\ntags:\n- 照合\n---\n本文。\n", None),
            ("table", f"| 項目 |\n|---|\n| {explained} |\n", None),
        )
        self.assertEqual(37, len(cases))
        for name, text, line in cases:
            with self.subTest(name=name):
                self.assert_wording(text, [] if line is None else [(self.first_error, "照合", line)])

    def test_markdown_labels_are_body_and_destinations_are_not(self):
        for text, line in (
            ("[照合](https://example.test)\n", 1),
            ("[照合][ref]\n\n[ref]: https://example.test\n", 1),
            ("| 項目 |\n|---|\n| 照合 |\n", 3),
            ("> 説明\n> 照合。\n", 2),
            ("- 説明\n  照合。\n", 2),
            ("<!-- 非表示 --> 照合。\n", 1),
        ):
            with self.subTest(text=text):
                self.assert_wording(text, [(self.first_error, "照合", line)])

    def test_image_alt_text_is_body_and_frontmatter_is_not(self):
        # 画像の説明は本文として数え、初出の言い換えにもなる。
        self.assert_wording("![照合](image.png)\n", [(self.first_error, "照合", 1)])
        self.assert_wording(f"![照合({self.explanation})](image.png)\n\n照合。\n", [])
        # frontmatter は、見出しにならない形(リスト)でも数えない。行番号は元のまま。
        self.assert_wording("---\ntags:\n- 照合\n---\n照合する。\n", [(self.first_error, "照合", 5)])
        self.assert_wording(f"---\ntags:\n- 照合({self.explanation})\n---\n照合する。\n", [
            (self.first_error, "照合", 5),
        ])

    def test_empty_frontmatter_does_not_consume_later_horizontal_rule(self):
        for ending in ("---", "..."):
            with self.subTest(ending=ending):
                self.assert_wording(f"---\n{ending}\n\n照合\n\n---\n", [
                    (self.first_error, "照合", 4),
                ])

    def test_html_comments_in_attributes_and_unclosed_comments(self):
        self.assert_wording('<div title="<!--">照合</div>\n', [
            (self.first_error, "照合", 1),
        ])
        self.assert_wording("<!-- コメント 照合\n", [])

    def test_unclosed_comment_does_not_depend_on_parser_close(self):
        import html.parser

        def old_close(parser):
            # 古い Python の close() は、閉じていないコメントの後ろを本文として渡す。
            parser.handle_data(parser.rawdata)
            parser.rawdata = ""

        with mock.patch.object(html.parser.HTMLParser, "close", old_close):
            self.assert_wording("<!-- コメント 照合\n", [])
            self.assert_wording("<div>\n本文。<!-- コメント\n照合\n</div>\n", [])
            self.assert_wording("<div>\n照合<!-- コメント\n</div>\n", [(self.first_error, "照合", 2)])
            # 属性の値の中の <!-- はコメントの始まりではない。
            self.assert_wording('<div title="<!--">照合</div>\n', [(self.first_error, "照合", 1)])

    def test_entity_newline_keeps_separate_visible_words_separate(self):
        self.assert_wording("照&#10;合\n", [])

    def test_html_named_character_references_can_explain_a_term(self):
        self.assert_wording(f"<div>照合&lpar;{self.explanation}&rpar;</div>\n", [])
        self.assert_wording("<div>照&NewLine;合</div>\n", [])

    def test_headings_and_code_do_not_provide_a_first_explanation(self):
        for prefix in (
            f"# 照合({self.explanation})\n",
            f"照合({self.explanation})\n===\n",
            f"```text\n照合({self.explanation})\n```\n",
            f"~~~text\n照合({self.explanation})\n~~~\n",
            f"`照合({self.explanation})`\n",
            f"<!-- 照合({self.explanation}) -->\n",
        ):
            with self.subTest(prefix=prefix):
                self.assert_wording(prefix + "\n照合。\n", [
                    (self.first_error, "照合", prefix.count("\n") + 2),
                ])

    def test_multiline_inline_code_is_not_excluded(self):
        self.assert_wording("`始まり\n照合\n終わり`\n", [(self.first_error, "照合", 2)])

    def test_role_words_respect_ascii_boundaries_and_single_word_code(self):
        roles = {
            "team-lead": "作業を進め、結果を確かめる側の AI",
            "implementer": "実装を受け持つ AI", "reviewer": "変更を確かめる AI",
            "checker": "設計書を点検する AI", "researcher": "調べものを受け持つ AI",
        }
        for role, explanation in roles.items():
            with self.subTest(role=role):
                self.assert_wording(f"{role}({explanation})。\n", [])
                self.assert_wording(f"`{role}`〈{explanation}〉。\n", [])
                self.assert_wording(f"``{role}``({explanation})。\n", [])
                self.assert_wording(f"{explanation}(`{role}`)。\n", [])
                self.assert_wording(f"`{role}`。\n", [(self.first_error, role, 1)])
                self.assert_wording(f"日本語{role}です。\n", [(self.first_error, role, 1)])
                for character in ("a", "Z", "1", "_", "-"):
                    self.assert_wording(f"x{character}{role} {role}{character}x\n", [])
                self.assert_wording(f"`run {role}` `some-{role}`\n", [])

    def test_parentheses_and_eight_character_suffix_boundary(self):
        for text in (
            f"照合〈{self.explanation}〉。",
            f"{self.explanation}〈照合〉。",
            f"**照合**({self.explanation})。",
            f"照合甲乙丙丁戊己庚辛({self.explanation})。",
            f"照合テストA1甲乙丙({self.explanation})。",
        ):
            with self.subTest(text=text):
                self.assert_wording(text + "\n", [])
        for text in (
            f"照合甲乙丙丁戊己庚辛壬({self.explanation})。",
            f"照合する({self.explanation})。",
            f"照合 ({self.explanation})。",
            f"照合({self.explanation}\n)。",
            f"照合({self.explanation}。補足(内側)。",
            f"{self.explanation} (照合)。",
            f"照合(照らし合わせて確かめる)。",
        ):
            with self.subTest(text=text):
                self.assert_wording(text + "\n", [(self.first_error, "照合", 1)])

    def test_explanation_must_open_the_bracket(self):
        # 既にある括弧に入れるときは、その頭に置く(design §6 の添え方)。途中や後ろは数えない。
        self.assert_wording(f"照合(補足。{self.explanation})。\n", [(self.first_error, "照合", 1)])
        self.assert_wording(f"照合〈補足:{self.explanation}〉。\n", [(self.first_error, "照合", 1)])
        self.assert_wording(f"照合({self.explanation}。補足)。\n", [])

    def test_missing_first_explanation_reports_later_explanation_line(self):
        errors = self.assert_wording(f"照合する。\n\n照合({self.explanation})。\n", [
            (self.first_error, "照合", 1),
        ])
        self.assertRegex(errors[0], r"(?:3\s*行|:3\b)")

    def test_duplicate_explanations_are_reported_at_second_occurrence(self):
        explanation = f"照合({self.explanation})"
        self.assert_wording(f"{explanation}。\n通常の照合。\n{explanation}。\n", [
            (self.duplicate_error, "照合", 3),
        ])
        self.assert_wording(f"{explanation}。{explanation}。\n", [
            (self.duplicate_error, "照合", 1),
        ])
        self.assert_wording(f"{explanation}。\n照合。\n", [])

    def test_scope_and_exact_writing_rules_exclusion(self):
        for relative in (
            f"{SKILLS_REL}/wording-test/templates/template.md",
            f"{SKILLS_REL}/wording-test/scripts/example.md",
            f"{SKILLS_REL}/wording-test/README.md",
            "README.md", "docs/minutes/example.md", "scripts/example.md",
            f"{SKILLS_REL}/{WRITING_RULES_REL}",
        ):
            self.write(relative, "照合。縮退する。\n")
        self.assert_wording("本文。\n", [])
        included = (
            f"{SKILLS_REL}/wording-test/SKILL.md",
            f"{SKILLS_REL}/wording-test/references/nested/child.md",
            f"{SKILLS_REL}/wording-test/references/writing-for-people.md",
        )
        for relative in included:
            with self.subTest(relative=relative):
                path = self.write(relative, "照合。\n")
                self.assert_wording(None, [(self.first_error, "照合", 1)], relative)
                path.unlink()

    def test_each_file_needs_its_own_explanation(self):
        self.write(f"{SKILLS_REL}/wording-test/SKILL.md", f"照合({self.explanation})。\n")
        self.assert_wording("照合。\n", [(self.first_error, "照合", 1)])

    def test_validate_allow_does_not_hide_wording_errors(self):
        self.assert_wording("照合。縮退する。 <!-- validate-allow: 試験のため -->\n", [
            (self.first_error, "照合", 1), (self.reduced_error, "縮退", 1),
        ])

    def test_reduced_term_is_allowed_only_in_code_or_registered_names(self):
        self.assert_wording("`縮退`。\n```text\n縮退\n```\n<!-- 縮退 -->\n# 縮退判定\n", [])
        self.assert_wording("# 縮退判定\n\n縮退判定を行う。\n", [(self.reduced_error, "縮退", 3)])
        entry = self.name_entry("縮退判定", "「縮退判定」")
        self.write_exceptions(names=[entry])
        self.assert_wording("「縮退判定」を使う。\n", [])
        self.assert_wording("「縮退判定」の後に縮退する。\n", [(self.reduced_error, "縮退", 1)])

    def test_names_exclude_only_registered_form_in_registered_file(self):
        entry = self.name_entry()
        self.write_exceptions(names=[entry])
        self.assert_wording("「照合パターン」を使う。\n", [])
        self.assert_wording("「照合パターン」の後に照合する。\n", [(self.first_error, "照合", 1)])
        self.assert_wording("「照合パターン」を使う。\n照合パターンを使う。\n", [
            (self.first_error, "照合", 2),
        ])
        other = f"{SKILLS_REL}/wording-test/references/other.md"
        self.write(other, "「照合パターン」を使う。\n")
        self.assert_wording("「照合パターン」を使う。\n", [(self.first_error, "照合", 1)], other)

    def short_entry(self, form, name="32 縮退", marks=("32", "比較不能"),
                    definition="**0** 正常終了 / **32** 縮退 / **33** 比較不能"):
        """short(短い呼び名と印)を明示した項目。名前の字面を含まない形を許す。"""
        entry = self.name_entry(name, form)
        self.write("docs/wording-names.md", definition + "\n")
        entry["definition"]["text"] = definition
        entry["short"] = {"text": "縮退", "marks": list(marks)}
        return entry

    def test_short_name_forms_are_local_and_need_a_mark(self):
        other = f"{SKILLS_REL}/wording-test/references/other.md"
        entry = self.short_entry("縮退の 32")
        self.write_exceptions(names=[entry])
        self.assert_wording("状態は縮退の 32。\n", [])
        self.assert_wording("状態は縮退の 32。縮退する。\n", [(self.reduced_error, "縮退", 1)])
        self.write(other, "状態は縮退の 32。\n")
        self.assert_wording("状態は縮退の 32。\n", [(self.reduced_error, "縮退", 1)], other)
        (self.repo / other).unlink()
        # 印(終了値の番号・同じ表のほかの状態名)の無い形は登録できない。数字の印は前後の数字を見る。
        for form in ("正常(縮退)", "縮退の 332", "縮退の 320"):
            with self.subTest(form=form):
                self.write_exceptions(names=[self.short_entry(form)])
                errors = self.assert_dictionary_error()
                self.assertIn("印が無い", errors[0])
        self.write_exceptions(names=[self.short_entry("「比較不能」は縮退ではない")])
        self.assert_wording(None, [])
        entry = self.short_entry("縮退")
        self.write_exceptions(names=[entry])
        self.assert_dictionary_error()

    def test_name_string_alone_does_not_allow_forms_without_the_name(self):
        # 名前の字面(30 縮退・32 縮退)では分岐しない。short の無い項目は、許す形に名前を含める。
        for name, form in (("30 縮退", "exit 30 は縮退"), ("32 縮退", "縮退の 32")):
            with self.subTest(name=name):
                entry = self.name_entry(name, form)
                self.write("docs/wording-names.md", f"**{name.split()[0]}** 縮退\n")
                entry["definition"]["text"] = f"**{name.split()[0]}** 縮退"
                self.write_exceptions(names=[entry])
                errors = self.assert_dictionary_error()
                self.assertIn("名前の字面がない", errors[0])

    def test_short_text_must_be_a_checked_word_in_the_name(self):
        # 呼び名の数・印・動詞の判定はすべて通る形で、short の text の判定だけを確かめる。
        # 「照合」は検査語だが、名前「32 縮退」には含まれない。
        entry = self.short_entry("照合の 32")
        entry["short"] = {"text": "照合", "marks": ["32"]}
        self.write_exceptions(names=[entry])
        self.assertIn("short の text は", self.assert_dictionary_error()[0])

    def test_short_field_is_validated(self):
        # 印は定義の字面がある行に在るものだけ。同じファイルの別の行に在っても認めない。
        entry = self.short_entry("縮退の 34", marks=("34",))
        self.write("docs/wording-names.md", entry["definition"]["text"] + "\n34 は別の行\n")
        self.write_exceptions(names=[entry])
        self.assertIn("印が定義の行に無い", self.assert_dictionary_error()[0])
        # 印に検査語を入れて、ほかの語を隠すことはできない。
        entry = self.short_entry("縮退と照合パターン", marks=("照合パターン",),
                                 definition="**32** 縮退 / 照合パターン")
        self.write_exceptions(names=[entry])
        self.assertIn("印に検査語", self.assert_dictionary_error()[0])
        for short in ("縮退", {"text": "縮退"}, {"text": "照合", "marks": ["32"]},
                      {"text": "状態", "marks": ["32"]}, {"text": "縮退", "marks": []},
                      {"text": "縮退", "marks": ["32", "32"]}, {"text": "縮退", "marks": "32"},
                      {"text": "縮退", "marks": ["32"], "extra": 1}):
            with self.subTest(short=short):
                entry = self.short_entry("縮退の 32")
                entry["short"] = short
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()

    def test_one_character_status_name_is_not_a_mark(self):
        # 定義の行の ` / ` の並びにある状態名でも、1 字なら印にできない。同じ行の 2 字以上の状態名は通る。
        definition = "**32** 縮退 / 可 / 比較不能"
        entry = self.short_entry("縮退と可", marks=("可",), definition=definition)
        self.write_exceptions(names=[entry])
        self.assertIn("印は", self.assert_dictionary_error()[0])
        entry = self.short_entry("縮退と比較不能", marks=("比較不能",), definition=definition)
        self.write_exceptions(names=[entry])
        self.assert_wording(None, [])

    def test_short_marks_are_limited_to_numbers_circled_numbers_and_status_names(self):
        # 印は、数字だけの終了値の番号・①〜⑳・定義の行で **…** か ` / ` で区切った状態名(2 字以上)に限る。
        # 下の印はどれも定義の行に在るが、種類が違うので認めない(1 字の「の」で縮退を隠せないように)。
        definition = "**32** 縮退 / **33** 比較不能 | ふつうの語の方言と正常終了の 3a ㉑"
        for mark in ("の", "方言", "正常終了", "比較", "3a", "㉑"):
            with self.subTest(mark=mark):
                entry = self.short_entry(f"縮退と{mark}", marks=(mark,), definition=definition)
                self.write_exceptions(names=[entry])
                self.assertIn("印は", self.assert_dictionary_error()[0])
        for definition, mark in (("**32** 縮退。次は **比較不能**", "比較不能"), ("**32** 縮退 ⑳", "⑳"),
                                 ("**32** 縮退 / 120 / ほか", "120")):
            with self.subTest(mark=mark):
                entry = self.short_entry(f"縮退と{mark}", marks=(mark,), definition=definition)
                self.write_exceptions(names=[entry])
                self.assert_wording(None, [])

    def test_verb_forms_are_rejected_even_when_the_form_has_the_whole_name(self):
        # 名前全体を含む形でも、短い呼び名の直後の動詞の形は登録できない。
        for form in ("32 縮退する", "**32** 縮退して続ける", "exit 32 は縮退せず"):
            with self.subTest(form=form):
                self.write_exceptions(names=[self.short_entry(form)])
                self.assertIn("動詞", self.assert_dictionary_error()[0])
        # short の無い名前でも、末尾の「縮退」の直後の動詞の形は登録できない。
        entry = self.name_entry("解決できないときの縮退", "解決できないときの縮退する")
        self.write_exceptions(names=[entry])
        self.assertIn("動詞", self.assert_dictionary_error()[0])

    def test_body_forms_respect_digit_boundaries(self):
        # 数字で始まる・終わる許す形は、本文でも外側が数字でない所だけを隠す(登録の印と同じ扱い)。
        self.write_exceptions(names=[self.short_entry("縮退の 32")])
        self.assert_wording("縮退の 32。\n", [])
        for text in ("縮退の 320。\n", "縮退の 32**0**。\n", "縮退の 32`0`。\n"):
            with self.subTest(text=text):
                self.assert_wording(text, [(self.reduced_error, "縮退", 1)])
        for form, texts in (("32 縮退", ("132 縮退。\n", "1**32 縮退**。\n")),
                            ("**32** 縮退", ("1**32** 縮退。\n",))):
            self.write_exceptions(names=[self.short_entry(form)])
            self.assert_wording(f"{form}。\n", [])
            for text in texts:
                with self.subTest(text=text):
                    self.assert_wording(text, [(self.reduced_error, "縮退", 1)])

    def test_digit_boundary_ignores_any_number_of_emphasis_marks(self):
        # 強調・コードの印の字数によらず、前後の数字を見る(1***32 縮退*** は表示が「132 縮退」)。
        self.write_exceptions(names=[self.short_entry("32 縮退")])
        self.assert_wording("32 縮退。***32 縮退***。\n", [])
        for text in ("1***32 縮退***。\n", "1****32 縮退。\n", "1`**32 縮退。\n"):
            with self.subTest(text=text):
                self.assert_wording(text, [(self.reduced_error, "縮退", 1)])
        self.write_exceptions(names=[self.short_entry("縮退の 32")])
        self.assert_wording("縮退の 32***0***。\n", [(self.reduced_error, "縮退", 1)])

    def test_whole_name_match_respects_digit_boundaries(self):
        # 名前全体を含むかの判定にも数字の境目を当てる。short の有無によらず「132 縮退」は登録できない。
        self.write_exceptions(names=[self.short_entry("132 縮退")])
        self.assertIn("印が無い", self.assert_dictionary_error()[0])
        entry = self.name_entry("32 縮退", "132 縮退")
        self.write_exceptions(names=[entry])
        self.assertIn("名前の字面がない", self.assert_dictionary_error()[0])
        self.write_exceptions(names=[self.name_entry("32 縮退", "32 縮退")])
        self.assert_wording("32 縮退。132 縮退。\n", [(self.reduced_error, "縮退", 1)])

    def test_links_character_references_and_html_are_not_hidden(self):
        # 登録: 表示の字面が変わる書き方(リンク・文字参照・HTML)は許す形に使えない。
        for form in ("32 縮退[する](x)", "32 縮退&#12377;る", "32 縮退<b>する</b>", "[32 縮退](x)"):
            with self.subTest(form=form):
                self.write_exceptions(names=[self.short_entry(form)])
                self.assertIn("リンク・文字参照・HTML", self.assert_dictionary_error()[0])
        # 本文: 許す形の隣にそれらがあれば、隠さずに検出する側に倒す。
        self.write_exceptions(names=[self.short_entry("32 縮退")])
        self.assert_wording("32 縮退。32 縮退(終了値)。\n", [])
        for text in ("32 縮退[する](x)。\n", "32 縮退&#12377;る。\n", "32 縮退<b>する</b>。\n",
                     "32 縮退**[する](x)**。\n", "[32 縮退](x)する。\n",
                     "&#49;32 縮退。\n", "<b>1</b>32 縮退。\n", "[1](x)32 縮退。\n"):
            with self.subTest(text=text):
                self.assert_wording(text, [(self.reduced_error, "縮退", 1)])
        self.write_exceptions(names=[self.short_entry("縮退の 32")])
        for text in ("縮退の 32&#48;。\n", "縮退の 32<b>0</b>。\n"):
            with self.subTest(text=text):
                self.assert_wording(text, [(self.reduced_error, "縮退", 1)])

    def test_short_name_forms_cannot_use_reduced_as_a_verb(self):
        # 名前の字面を含まない形でも、design §6 が書き換えを求める動詞の形は登録できない。
        for form in ("32 で縮退する", "縮退して続ける(32)", "exit 32 で縮退させる", "**縮退**せず 32"):
            with self.subTest(form=form):
                self.write_exceptions(names=[self.short_entry(form)])
                errors = self.assert_dictionary_error()
                self.assertIn("動詞", errors[0])

    def test_short_name_forms_need_exactly_one_short_name(self):
        for form in ("縮退・縮退(32)", "縮退の 32 と縮退"):
            with self.subTest(form=form):
                self.write_exceptions(names=[self.short_entry(form)])
                errors = self.assert_dictionary_error()
                self.assertIn("ちょうど 1 つ", errors[0])

    def test_registered_form_ending_in_reduced_does_not_hide_a_verb(self):
        # 項目ごとに定義のファイルを書き直すので、使う直前に作る。
        for make in (lambda: self.short_entry("exit 32 は縮退"), lambda: self.short_entry("**32** 縮退"),
                     lambda: self.name_entry("解決できないときの縮退", "解決できないときの縮退")):
            entry = make()
            form = entry["forms"][0]
            with self.subTest(form=form):
                self.write_exceptions(names=[entry])
                self.assert_wording(f"{form}。\n", [])
                self.assert_wording(f"{form}(終了値)。\n", [])
                for verb in ("する。", "して続ける。", "させる。", "**する**。"):
                    self.assert_wording(f"{form}。\n{form}{verb}\n", [(self.reduced_error, "縮退", 2)])

    def test_report_explanations_are_excluded_but_body_still_needs_one(self):
        entry = self.name_entry("照合", f"**照合({self.explanation})**")
        entry["definition"]["text"] = f"照合({self.explanation})"
        self.write("docs/wording-names.md", entry["definition"]["text"] + "\n")
        self.write_exceptions(reports=[entry])
        report = entry["forms"][0]
        self.assert_wording(f"照合({self.explanation})。\n{report}\n{report}\n", [])
        self.assert_wording(f"{report}\n照合する。\n", [(self.first_error, "照合", 2)])
        self.assert_wording(f"{report} 照合する。\n", [(self.first_error, "照合", 1)])
        self.assert_wording(f"{report}\n照合({self.explanation})。\n照合({self.explanation})。\n", [
            (self.duplicate_error, "照合", 3),
        ])

    def test_design_table_missing_duplicate_and_missing_row_are_errors(self):
        path = self.repo / "docs/design.md"
        original = path.read_text(encoding="utf-8")
        row = f"| 照合 | {self.explanation} |\n"
        table = "| 語 | 言い換えの字面 |\n|---|---|\n" + row
        for design in (
            original.replace("| 語 | 言い換えの字面 |", "| 別の表 | 字面 |"),
            original.replace("## 7.", table + "\n## 7.", 1),
            original.replace(row, ""),
            original.replace(row, row + row),
            original.replace(row, "| 照合 | |\n"),
            # 別々の語に同じ言い換え。語数は 27 のまま。
            original.replace("| 正本 | ほかが合わせる元 |", f"| 正本 | {self.explanation} |"),
            # 括弧のある語は「X(Y)」の形だけ。
            original.replace("| 差し戻し(差し戻す) |", "| 差し戻し(差し戻す |"),
        ):
            with self.subTest(design=design[-120:]):
                path.write_text(design, encoding="utf-8")
                errors = self.check()
                self.assertTrue(errors)
                self.assertTrue(all("27 語の表" in error for error in errors), errors)

    def test_word_shapes_come_from_the_design_table(self):
        # 英字の語と「X(Y)」の語の数え方は、検査の中の語の一覧ではなく、表の字面から導く。
        path = self.repo / "docs/design.md"
        design = path.read_text(encoding="utf-8")
        for old, new in (("| checker | 設計書を点検する AI |", "| inspector | 設計書を点検する AI |"),
                         ("| 差し戻し(差し戻す) |", "| 取り下げ(取り下げる) |")):
            self.assertEqual(1, design.count(old))
            design = design.replace(old, new)
        path.write_text(design, encoding="utf-8")
        self.assert_wording("`inspector`。\n", [(self.first_error, "inspector", 1)])
        self.assert_wording("xinspector inspector-x `run inspector`\n", [])
        self.assert_wording("inspector(設計書を点検する AI)。\n", [])
        self.assert_wording("`checker`。checker。差し戻す。\n", [])
        self.assert_wording("取り下げる。\n", [(self.first_error, "取り下げ", 1)])
        self.assert_wording("取り下げ。\n", [(self.first_error, "取り下げ", 1)])
        self.assert_wording("取り下げる(相手に返して、やり直してもらうこと)。取り下げ。\n", [])

    def test_definition_path_cannot_leave_through_dotdot_or_symlink(self):
        # docs/../x.md は、解決した先がリポジトリの中でも、.. を含むので受け付けない。
        self.write("x.md", "# 照合パターン\n")
        entry = self.name_entry()
        entry["definition"]["path"] = "docs/../x.md"
        self.write_exceptions(names=[entry])
        self.assertIn("定義元のパスが範囲外", self.assert_dictionary_error()[0])
        # リポジトリの外を指す symlink は、解決した先で受け付けない。
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "names.md"
        target.write_text("# 照合パターン\n", encoding="utf-8")
        (self.repo / "docs/link.md").symlink_to(target)
        entry = self.name_entry()
        entry["definition"]["path"] = "docs/link.md"
        self.write_exceptions(names=[entry])
        self.assertIn("定義元がリポジトリの外", self.assert_dictionary_error()[0])

    def test_exception_definition_must_exist(self):
        for field, value in (("path", "docs/missing.md"), ("text", "存在しない定義")):
            with self.subTest(field=field):
                entry = self.name_entry()
                entry["definition"][field] = value
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()

    def test_exception_form_must_exist_in_registered_files(self):
        # 登録したファイルに無い字面は、許す形にできない。
        for form in ("「照合パターン」の表", "照合パターン一覧"):
            with self.subTest(form=form):
                entry = self.name_entry()
                entry["forms"].append(form)
                self.write_exceptions(names=[entry])
                errors = self.assert_dictionary_error()
                self.assertIn("許す形が存在しない", errors[0])
        # ほかのファイルにだけある字面も、登録したファイルに無ければ同じ。
        entry = self.name_entry()
        self.write(f"{SKILLS_REL}/wording-test/references/other.md", "「照合パターン」の表\n")
        entry["forms"] = ["「照合パターン」の表"]
        self.write_exceptions(names=[entry])
        errors = self.assert_dictionary_error()
        self.assertIn("許す形が存在しない", errors[0])

    def test_same_form_cannot_be_registered_by_two_entries(self):
        self.write("docs/wording-names.md", "# 照合パターン\n# 照合パターン表\n")
        self.write(self.reference, "「照合パターン表」\n")

        def entry(name):
            return {
                "name": name, "definition": {"path": "docs/wording-names.md", "text": name},
                "files": [self.reference], "forms": ["「照合パターン表」"], "reason": "試験。",
            }

        for names, reports in (
            ([entry("照合パターン"), entry("照合パターン表")], []),
            ([entry("照合パターン表")], [entry("照合パターン表")]),
        ):
            with self.subTest(names=len(names), reports=len(reports)):
                self.write_exceptions(names=names, reports=reports)
                errors = self.assert_dictionary_error()
                self.assertIn("許す形の重複", errors[0])
        self.write_exceptions(names=[entry("照合パターン表")])
        self.assert_wording(None, [])

    def test_each_registered_form_hides_only_its_own_words(self):
        """実際の一覧の、ファイルと許す形の組ごとに確かめる(名前を足す変更の試験)。

        許す形だけなら検出しない。許す形の直後に守る語を言い換えなしで続けると検出する。
        """
        data = json.loads((SOURCE_REPO / self.exceptions).read_text(encoding="utf-8"))
        design = (self.repo / "docs/design.md").read_text(encoding="utf-8")
        table = design.split("| 語 | 言い換えの字面 |\n|---|---|\n", 1)[1].split("\n\n", 1)[0]
        labels = {}
        for row in table.splitlines():
            label = row.strip("| ").split(" | ")[0]
            alias = re.fullmatch(r"([^()]+)\(([^()]+)\)", label)
            for word in alias.groups() if alias else (label,):
                labels[word] = label
        # 英字の語は、表の字面から「英字と - だけの語」として導く。
        roles = {word for word in labels if re.fullmatch(r"[A-Za-z][A-Za-z-]*", word)}

        def protected(name):
            literal = re.sub(r"[`*_]", "", name)
            return [
                word for word in [*labels, "縮退"]
                if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_-])" if word in roles
                             else re.escape(word), literal)
            ]

        namespace = runpy.run_path(str(self.repo / "scripts/validate.py"), run_name="wording_under_test")
        pairs = 0
        for kind in ("names", "reports"):
            for entry in data[kind]:
                words = protected(entry["name"])
                self.assertTrue(words, entry["name"])
                # 定義元は、検査しない docs/ に置き換える(定義の実在は ValidateTest の全体の検査で見る)。
                # short の印は定義の行で探すので、実際の定義元から定義のある行をそのまま使う。
                source = (SOURCE_REPO / entry["definition"]["path"]).read_text(encoding="utf-8")
                line = next(row for row in source.splitlines() if entry["definition"]["text"] in row)
                self.write("docs/wording-names.md", line + "\n")
                for relative in entry["files"]:
                    for form in entry["forms"]:
                        pairs += 1
                        single = dict(entry, files=[relative], forms=[form], definition={
                            "path": "docs/wording-names.md", "text": entry["definition"]["text"],
                        })
                        self.write_exceptions(**{kind: [single]})
                        cases = [(f"{form}。\n", None)] + [(f"{form}{word}。\n", word) for word in words]
                        for text, word in cases:
                            with self.subTest(name=entry["name"], file=relative, form=form, word=word):
                                shutil.rmtree(self.repo / SKILLS_REL, ignore_errors=True)
                                self.write(relative, text)
                                namespace["ERRORS"].clear()
                                with contextlib.redirect_stdout(io.StringIO()):
                                    namespace["check_wording"]()
                                errors = list(namespace["ERRORS"])
                                if word is None:
                                    self.assertEqual([], errors)
                                    continue
                                self.assertEqual(1, len(errors), errors)
                                kind_text = self.reduced_error if word == "縮退" else self.first_error
                                self.assertIn(f"{relative}:1:", errors[0])
                                self.assertIn(kind_text, errors[0])
                                if word != "縮退":
                                    self.assertIn(repr(labels[word]), errors[0])
        # 一覧が空になって試験が何も確かめない、を防ぐ。
        self.assertGreaterEqual(pairs, len(data["names"]) + len(data["reports"]))

    def test_exception_must_name_a_complete_defined_phrase(self):
        entry = self.name_entry("縮", "縮")
        self.write(self.reference, "縮退する。\n")
        self.write_exceptions(names=[entry])
        self.assert_dictionary_error()

        entry = self.name_entry()
        self.write(self.reference, "「照合パターン」\n")
        self.write("docs/wording-names.md", "定義だけの文\n")
        entry["definition"]["text"] = "定義だけの文"
        self.write_exceptions(names=[entry])
        self.assert_dictionary_error()

        entry = self.name_entry()
        entry["forms"] = ["照合"]
        self.write(self.reference, "照合する。\n")
        self.write_exceptions(names=[entry])
        self.assert_dictionary_error()

    def test_exception_form_cannot_include_other_body_violations(self):
        for form in ("「照合パターン」と照合する", "「照合パターン」と縮退する"):
            with self.subTest(form=form):
                entry = self.name_entry(form=form)
                self.write(self.reference, form + "。\n")
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()

    def test_exception_paths_cannot_escape_or_exclude_whole_files(self):
        for path in ("../outside.md", "/tmp/outside.md", "docs/design.md", "*", f"{SKILLS_REL}/**/*.md"):
            with self.subTest(path=path):
                entry = self.name_entry()
                entry["files"] = [path]
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()
        for form in ("", "縮退", "\n"):
            with self.subTest(form=form):
                entry = self.name_entry("縮退判定", form)
                self.write_exceptions(names=[entry])
                self.write(self.reference, form + "\n")
                self.assert_dictionary_error()
        entry = self.name_entry("縮退", "「縮退」")
        self.write_exceptions(names=[entry])
        self.write(self.reference, "「縮退」\n")
        self.assert_dictionary_error()
        for form in ("〈縮退〉", '"縮退"', "(縮退)"):
            with self.subTest(form=form):
                entry = self.name_entry(form, form)
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()
                entry = self.name_entry("30 縮退", form)
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()

    def test_exception_empty_and_invalid_types_are_rejected(self):
        for field, value in (
            ("name", ""), ("name", 3), ("reason", ""), ("reason", None),
            ("definition", []), ("files", []), ("files", self.reference),
            ("forms", []), ("forms", "照合パターン"),
        ):
            with self.subTest(field=field, value=value):
                entry = self.name_entry()
                entry[field] = value
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()
        for data in ({}, [], {"version": 2, "names": [], "reports": []},
                     {"version": 1, "names": {}, "reports": []}):
            with self.subTest(data=data):
                self.write(self.exceptions, json.dumps(data))
                self.assert_dictionary_error()

    def test_exception_duplicate_keys_and_entries_are_rejected(self):
        self.write(self.exceptions, '{"version":1,"version":1,"names":[],"reports":[]}')
        self.assert_dictionary_error()
        entry = self.name_entry()
        self.write(self.reference, "「照合パターン」\n")
        self.write_exceptions(names=[entry, entry])
        self.assert_dictionary_error()
        for field in ("forms", "files"):
            with self.subTest(field=field):
                entry = self.name_entry()
                entry[field] *= 2
                self.write_exceptions(names=[entry])
                self.assert_dictionary_error()

    def test_missing_exception_file_is_an_error(self):
        (self.repo / self.exceptions).unlink()
        self.assert_dictionary_error()

    def test_malformed_exception_json_is_an_error(self):
        self.write(self.exceptions, '{"version":1,')
        self.assert_dictionary_error()

    def test_missing_markdown_dependency_fails_with_install_guidance(self):
        with mock.patch.dict(sys.modules, {"markdown_it": None}):
            errors = self.check()
        self.assertEqual(1, len(errors), errors)
        self.assertIn("markdown", errors[0].lower())
        self.assertIn("requirements-dev.txt", errors[0])


if __name__ == "__main__":
    unittest.main()
