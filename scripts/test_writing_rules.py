#!/usr/bin/env python3
"""人が読む文の書き方の決まりの写しの回帰テスト。

決まりは 3 か所にある。正本の `do-task/references/writing-for-people.md`、init-project の
AGENTS.md の雛形の「応答の書き方」節、このリポジトリの AGENTS.md の「応答の書き方」節。
写しがずれたり欠けたりしたら落ちる。

取り出す範囲:
  - 正本: `## 2. ` の見出しの次の行から次の `## ` の前まで
  - 雛形: `## 応答の書き方` の見出しの次の行から次の `## ` の前まで。固定の文は
    `{{RESPONSE_STYLE}}` の行の前まで
  - このリポジトリの AGENTS.md: `## 応答の書き方` の見出しの次の行から次の `## ` の前まで。
    固定の文は節の中の最初の `### ` の行の前まで。口調の部分はその `### ` の行から節の終わりまで
"""

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "plugins" / "dev-workflow" / "skills"
WRITING_RULES = SKILLS_DIR / "do-task" / "references" / "writing-for-people.md"
AGENTS_TEMPLATE = SKILLS_DIR / "init-project" / "templates" / "AGENTS.md.template"
TONE_REPORT = SKILLS_DIR / "init-project" / "templates" / "response-tone" / "tone-report.md.template"
REPO_AGENTS = REPO / "AGENTS.md"

SECTION_HEADING = "## 応答の書き方"
PLACEHOLDER = "{{RESPONSE_STYLE}}"
ITEM_RE = re.compile(r"^\s*([1-8])\. (.*)$")

# 固定の文に必ずある字面(優先順位・コミットメッセージ・用語集・字面を変えない行)
REQUIRED_PHRASES = (
    "決まった書式 > 正確さ > 簡潔さ > 口調",
    "コミットメッセージとタイトルは",
    "用語集",
    "同じ行に何も足さない",
)


def section_lines(path, is_heading):
    """`is_heading` に当たる最初の行の次の行から、次の `## ` の行の前までを返す。
    見出しが無ければ None。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(lines):
        if not is_heading(line):
            continue
        out = []
        for rest in lines[i + 1 :]:
            if rest.startswith("## "):
                break
            out.append(rest)
        return out
    return None


def strip_trailing_blank(lines):
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    return out


def items(lines):
    """行頭が `[1-8]. ` の行を (番号, 番号の後ろの文) の並びで返す。"""
    return [(m.group(1), m.group(2)) for m in (ITEM_RE.match(line) for line in lines) if m]


class WritingRulesTest(unittest.TestCase):
    # ------------------------------------------------------------------ 取り出し

    def rules_section(self):
        lines = section_lines(WRITING_RULES, lambda line: line.startswith("## 2. "))
        self.assertIsNotNone(lines, f"{WRITING_RULES.relative_to(REPO)} に `## 2. ` の見出しが無い")
        return lines

    def template_section(self):
        lines = section_lines(AGENTS_TEMPLATE, lambda line: line == SECTION_HEADING)
        self.assertIsNotNone(lines, f"{AGENTS_TEMPLATE.relative_to(REPO)} に `{SECTION_HEADING}` が無い")
        return lines

    def template_fixed(self):
        """雛形の固定の文(`{{RESPONSE_STYLE}}` の行の前まで。末尾の空行は除く)。"""
        lines = self.template_section()
        marks = [i for i, line in enumerate(lines) if line.strip() == PLACEHOLDER]
        self.assertEqual(1, len(marks), f"雛形の節に `{PLACEHOLDER}` の行がちょうど 1 行ない")
        return strip_trailing_blank(lines[: marks[0]])

    def repo_section(self):
        lines = section_lines(REPO_AGENTS, lambda line: line == SECTION_HEADING)
        self.assertIsNotNone(lines, f"AGENTS.md に `{SECTION_HEADING}` が無い")
        return lines

    def repo_split(self):
        """このリポジトリの AGENTS.md の節を、固定の文と口調の部分に分ける(どちらも末尾の空行は除く)。"""
        lines = self.repo_section()
        heads = [i for i, line in enumerate(lines) if line.startswith("### ")]
        self.assertTrue(heads, "AGENTS.md の「応答の書き方」節に `### ` の行(口調の部分の始まり)が無い")
        return strip_trailing_blank(lines[: heads[0]]), strip_trailing_blank(lines[heads[0] :])

    # ------------------------------------------------------------------ 8 項目

    def test_rules_have_exactly_eight_items(self):
        found = items(self.rules_section())
        self.assertEqual([str(n) for n in range(1, 9)], [n for n, _ in found], found)

    def test_eight_items_are_the_same_in_three_places(self):
        rules = items(self.rules_section())
        template = items(self.template_fixed())
        repo = items(self.repo_split()[0])
        self.assertEqual(rules, template, "正本と雛形の 8 項目が一字一句同じでない")
        self.assertEqual(rules, repo, "正本とこのリポジトリの AGENTS.md の 8 項目が一字一句同じでない")

    # ------------------------------------------------------------------ 固定の文

    def test_placeholder_is_the_last_non_blank_line_of_the_template_section(self):
        non_blank = [line for line in self.template_section() if line.strip()]
        self.assertTrue(non_blank, "雛形の「応答の書き方」節が空")
        self.assertEqual(PLACEHOLDER, non_blank[-1].strip())

    def test_template_fixed_text_has_no_subheading_and_eight_items(self):
        fixed = self.template_fixed()
        self.assertEqual([], [line for line in fixed if line.startswith("### ")])
        self.assertEqual(8, sum(1 for line in fixed if ITEM_RE.match(line)), fixed)

    def test_fixed_text_has_the_required_phrases(self):
        # 雛形とこのリポジトリの両方で見る(両方から同じ行を消すと、一致の試験だけでは気づけない)
        for label, fixed in (("雛形", self.template_fixed()), ("AGENTS.md", self.repo_split()[0])):
            text = "\n".join(fixed)
            for phrase in REQUIRED_PHRASES:
                with self.subTest(where=label, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_repo_fixed_text_is_the_same_as_the_template(self):
        self.assertEqual(self.template_fixed(), self.repo_split()[0])

    # ------------------------------------------------------------------ 口調の部分

    def test_repo_tone_part_is_the_same_as_the_report_tone_template(self):
        tone = strip_trailing_blank(TONE_REPORT.read_text(encoding="utf-8").split("\n"))
        self.assertEqual(tone, self.repo_split()[1])


if __name__ == "__main__":
    unittest.main()
