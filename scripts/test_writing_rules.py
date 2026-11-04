#!/usr/bin/env python3
"""人が読む文の書き方の決まりの写しの回帰テスト。

決まりは 3 か所にある。正本の `do-task/references/writing-for-people.md`、init-project の
AGENTS.md の雛形の「応答の書き方」節、このリポジトリの AGENTS.md の「応答の書き方」節。
写しがずれたり欠けたりしたら落ちる。
完了報告の 7 項目と、各 skill の参照・出力の境界も検査する。

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

    # ---------------------------------------------------------- 番号だけの呼び方

    def test_number_rules_are_in_the_required_sections(self):
        phrases_by_section = {
            "## 4. ": (
                "人に出す報告・質問・選択肢・Issue と PR の本文",
                "番号が理由・判定・指摘そのものになるとき",
                "「番号: 中身」の形で一言添える",
                "G3: 無人の前提を欠く",
                "S4: 承認の後に本文が変わった",
                "S・D・U・G の中身は",
                "その周で当たった理由を一言で書く",
                "§4 の表の行を目安にする",
                "S4 なら、当たった続行判定の項目の名前から書く",
                "`6 本文の照合` → `承認の後に本文が変わった`",
                "参照先を示すだけの番号",
                "手順の本文・相互参照の番号は残してよい",
            ),
            "## 5. ": (
                "保留の行の停止条件と、結果の行の理由を番号で示すときは",
                "「番号: 中身」で書く",
                "番号を先に置き",
                "行頭・区切り・結末の値には何も足さない",
            ),
            "## 7. ": (
                "選択肢に番号だけを書かない",
                "「番号: 中身」で、選ぶ内容を一言添える",
            ),
        }
        for heading, phrases in phrases_by_section.items():
            lines = section_lines(WRITING_RULES, lambda line: line.startswith(heading))
            self.assertIsNotNone(lines, f"{heading}の節が無い")
            # 4 節は表の前、5 節は (i) の中に決まりを置く。
            if heading == "## 4. ":
                lines = lines[:next(i for i, line in enumerate(lines) if line.startswith("|"))]
            elif heading == "## 5. ":
                start = next(i for i, line in enumerate(lines) if line.startswith("- **(i)"))
                end = next(i for i, line in enumerate(lines) if line.startswith("- **(ii)"))
                lines = lines[start:end]
            text = "\n".join(lines)
            for phrase in phrases:
                with self.subTest(section=heading, phrase=phrase):
                    self.assertIn(phrase, text)

    # ------------------------------------------------------------------ 口調の部分

    def test_repo_tone_part_is_the_same_as_the_report_tone_template(self):
        tone = strip_trailing_blank(TONE_REPORT.read_text(encoding="utf-8").split("\n"))
        self.assertEqual(tone, self.repo_split()[1])


class CompletionReportTest(unittest.TestCase):
    """共有する報告の型と、呼び出し元が読む出力の境界を守る。"""

    @classmethod
    def setUpClass(cls):
        cls.report = "\n".join(section_lines(WRITING_RULES, lambda line: line == "## 8. 完了報告の型") or [])

    def skill(self, name):
        return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    def test_copy_block_has_seven_fields_in_order_without_tone_or_result_lines(self):
        blocks = re.findall(r"^```text\n(.*?)^```$", self.report, flags=re.M | re.S)
        self.assertEqual(1, len(blocks), "コピー用の型は正本の末尾に 1 つだけ置く")
        lines = blocks[0].splitlines()
        self.assertEqual(
            ["完了", "変えたもの", "確かめたこと", "動かした確認", "残っていること", "詳しい記録", "リンク"],
            [line.split(":", 1)[0] for line in lines],
        )
        self.assertNotRegex(blocks[0], r"(?m)^(是|否|告|解|答|提案|警告|問|依頼|経過|異常)。")
        for prefix in ("無人の周の結果:", "候補モードの結果:", "レビュー判定:"):
            self.assertNotIn(prefix, blocks[0])
        self.assertIn("未実施", blocks[0])
        self.assertIn("理由", blocks[0])
        self.assertIn("モデルの名前と版", blocks[0])

    def test_tone_table_and_tone_section_both_exempt_the_copy_block(self):
        scope = "\n".join(section_lines(WRITING_RULES, lambda line: line.startswith("## 1. ")))
        tone = "\n".join(section_lines(WRITING_RULES, lambda line: line.startswith("## 6. ")))
        self.assertRegex(scope, r"\| 完了報告のコピー用の枠.*\| 当てない。段落の印も付けない \|")
        self.assertIn("会話の口調を当てず、マーカーも付けない", tone)
        self.assertIn("枠の前のまとめと、後の依頼の段落", tone)

    def test_every_skill_chooses_the_common_report_or_explains_its_exception(self):
        exceptions = {"cleanup", "data-audit", "tool-check", "understand-project"}
        for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                report_lines = [line for line in text.splitlines() if "8. 完了報告の型" in line]
                self.assertEqual(1, len(report_lines), "報告手順で共通の型か例外の理由を示す")
                line = report_lines[0]
                links = re.findall(r"\]\(([^)]+writing-for-people\.md)\)", line)
                self.assertEqual(1, len(links))
                self.assertEqual(WRITING_RULES.resolve(), (path.parent / links[0]).resolve())
                if path.parent.name in exceptions:
                    self.assertIn("成果物なので", line)
                    self.assertRegex(line, r"使わ(?:ない|ず)")
                else:
                    self.assertNotRegex(line, r"使わ(?:ない|ず)")

    def test_ship_task_suppresses_child_blocks_but_keeps_their_results(self):
        self.assertIn("`/ship-task` の中から呼ばれた skill は、コピー用の枠を出さない", self.report)
        self.assertIn("必要な結果・記録の場所・停止理由を呼び出し元へ返し", self.report)
        self.assertIn("最後に `/ship-task` が枠を 1 つだけ出す", self.report)
        for name in ("create-task", "do-task", "update-doc"):
            with self.subTest(skill=name):
                self.assertRegex(self.skill(name), r"`/ship-task` から呼ばれたときは枠を出さず")
        ship = self.skill("ship-task")
        self.assertIn("最後にこの skill が 1 つだけ出す", ship)
        pr_body = ship.split("3. PR 本文", 1)[1].split("\n4. ", 1)[0]
        self.assertIn("作成前に用意し", pr_body)
        self.assertIn("実行した skill 名と結果(反復回数・判定)", pr_body)
        self.assertIn("Phase 2 の続行 / 停止と根拠", pr_body)
        report = ship.split("\n4. 完了報告", 1)[1].split("\n5. ", 1)[0]
        final_gate = ship.split("## 最終ゲート", 1)[1].split("\n## ", 1)[0]
        for text in (report, final_gate, self.report):
            with self.subTest(text=text):
                self.assertIn("作成後は本文を書き換えない", text)
                self.assertIn("発見の周と PR を作らなかったときは", text)
                self.assertIn("既存のタスクの記録か会話に内訳を残す", text)
        self.assertIn("手順 3 で本文に書いた場所を指す", report)

    def test_terminal_protocols_stay_outside_the_copy_block(self):
        self.assertIn("枠の外に置く", self.report)
        self.assertIn("報告の最後の行のまま", self.report)
        self.assertIn("決まった一覧と結果の行が続く出力", self.report)
        unattended = (SKILLS_DIR / "ship-task/references/unattended-mode.md").read_text(encoding="utf-8")
        self.assertIn("`無人の周の結果: <結末> — <理由か URL>`", unattended)
        self.assertIn("`^無人の周の結果: (PR|縮退|保留|失敗扱い|候補なし) — `", unattended)
        update = self.skill("update-doc")
        self.assertIn("`レビュー判定: <APPROVED | 未収束 | 未完了>`", update)
        self.assertIn("判定の行は省かず", update)
        self.assertIn("枠の外の最後の行に置く", update)
        analysis = update.split("## Phase 3:", 1)[1].split("\n## ", 1)[0]
        final_check = update.split("## Phase 6:", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`--analyze-only` はここで報告して終了", analysis)
        self.assertIn("差分報告そのものが成果物なので、完了報告の型を使わず", analysis)
        self.assertIn("レビューをしないので、`レビュー判定:` の行は書かない", analysis)
        self.assertNotIn("`--analyze-only`", final_check)
        for name in ("create-task", "data-audit"):
            with self.subTest(skill=name):
                self.assertRegex(self.skill(name), r"候補モードでは、完了報告の型を使わず|候補モードでも共通の枠は出さない")

    def test_short_reports_preserve_unknowns_failures_and_completion_requirements(self):
        for phrase in ("完了にできる条件と、止める条件は各 skill のまま変えない",
                       "名前や版を確かめられない場合は「未確認」",
                       "失敗の理由・未実施の確認・人に求める判断・再開の条件",
                       "10 行に収めるために省かない", "「番号: 中身」",
                       "無い項目は省かず「なし」", "新しい記録ファイルは作らない"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.report)


if __name__ == "__main__":
    unittest.main()
