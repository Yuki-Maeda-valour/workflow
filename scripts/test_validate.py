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
        "口調の決め方。このファイルに届かないときは、権威参照ファイルの「応答の書き方」節と、"
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
        """写しの SKILL.md から、書き方の正本へのリンクを持つ行(行全体)を取る。"""
        lines = [
            line
            for line in self.skill_md(skill).read_text(encoding="utf-8").split("\n")
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
            ["# tool-check", "", "## 原則", "", f"- [書き方の正本]({link}) に従う", "", "## 1. 手順", ""]
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
                "description", d["plugins"][0]["description"].replace("skills 12 種", "skills 99 種")
            ),
        )
        self.assert_error(["skill 件数の表記 99 件", "実数 12 件"])

    def test_v6_plugin_json_skill_count_mismatch_is_error(self):
        # marketplace.json 側だけを壊すと、plugin.json 側の検査を消しても気づけない
        self.patch_json(
            PLUGIN_JSON,
            lambda d: d.__setitem__("description", d["description"].replace("12 skills", "99 skills")),
        )
        self.assert_error(["plugin.json", "skill 件数の表記 99 件", "実数 12 件"])

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

    def assert_host_cli_result(self, text, detected, relpath=CHECKED_MD, line=3):
        """1 ケースずつ実際の検証器へ渡し、他カテゴリの ERROR による偽陽性も防ぐ。"""
        self.write_file(relpath, f"# t\n\n{text}\n")
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
                    self.assert_host_cli_result("gemini", detected, relpath=relpath)
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
                        self.assert_host_cli_result(word, detected, relpath=relpath)
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


if __name__ == "__main__":
    unittest.main()
