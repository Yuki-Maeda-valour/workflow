"""init-project の質問・生成の条件と、雛形の置換の契約を確かめる。

skill 本文が実行手順なので、各工程の中で条件と出力を検査する。
実際の初期設定や GitHub への接続は行わない。
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "plugins/dev-workflow/skills/init-project"


def section(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


class InitTaskStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (INIT / "SKILL.md").read_text()
        cls.questions = section(cls.skill, "## Phase 2: 対話確認", "## Phase 3: 生成")
        cls.generation = section(cls.skill, "## Phase 3: 生成", "## Phase 3.5:")
        cls.profile = (INIT / "templates/project-profile.yml.template").read_text()
        cls.agents = (INIT / "templates/AGENTS.md.template").read_text()

    def contains(self, text, *required):
        for value in required:
            with self.subTest(required=value):
                self.assertIn(value, text)

    def test_task_store_question_is_last_and_has_both_choices(self):
        order = next(line for line in self.questions.splitlines() if "**質問の固定の順**" in line)
        self.assertTrue(order.endswith("デザイン連携 MCP → タスクの置き場"))
        row = next(line for line in self.questions.splitlines() if line.startswith("| タスクの置き場"))
        self.contains(row, "origin が GitHub で `gh` があるときだけ", "ファイル(推奨) / GitHub Issue")
        self.contains(self.questions, "タスクの設計と作業記録をどこに置きますか?",
                      "設計書をプロジェクト内のファイルに保存する", "TASK_STORE=file",
                      "設計と作業記録を Issue 本文に保存する", "TASK_STORE=github-issue")

    def test_detection_and_unavailable_conditions_do_not_create_issues(self):
        detection = section(self.skill, "**8. タスク保存先の解決**", "## Phase 2:")
        self.contains(detection, "`gh` の有無", "origin-repo.py --dir <root>",
                      "`repo` が `github.com/` で始まるか", "検出不能・非 Git・origin なし")
        self.contains(self.questions, "条件を満たすときだけ聞く。それ以外は聞かずに `file` にする",
                      "この初期設定では Issue を作成・更新しない", "利用時に認証と権限を確かめる",
                      "無人ループ・発見の実行・候補モードは使えない")

    def test_yes_and_rerun_keep_existing_choice(self):
        arguments = section(self.skill, "## 引数", "## Phase 1:")
        self.contains(arguments, "タスクの置き場は `file`。既存の有効な `task_store` は変えない")
        self.contains(self.questions, "既存の明示値(`file` / `github-issue`)があれば質問せず保持",
                      "条件不足でも `file` に切り替えず不足を報告", "`has_code: false` のときもタスクの置き場は同じ条件で聞く")
        detection = section(self.skill, "**8. タスク保存先の解決**", "## Phase 2:")
        self.contains(detection, "§1 で既存の `task_store` を検査する。不正なら停止する")

    def test_invalid_existing_profile_stops_before_generation(self):
        detection = section(self.skill, "**5. 既存構成の有無**", "**6. MCP の利用可否**")
        task_store = section(self.skill, "**8. タスク保存先の解決**", "## Phase 2:")
        generation = section(self.generation, "**2. .claude/project-profile.yml**", "**3. doc/")
        final = section(self.skill, "## 最終ゲート", "## 関連 skill")
        for phase in (detection, task_store, generation, final):
            with self.subTest(phase=phase.splitlines()[0]):
                self.contains(phase, "値に `{{...}}` が残る場合", "YAML が読めない場合",
                              "停止", "先に人が手で直してから再実行")
        self.contains(detection, "YAML として読む前にテキスト", "コメント行(`#` 始まり)の例示は除く",
                      "項目名と直す場所を報告して停止", "`source_of_truth`・`task_store`・`task_dir` を含む全項目",
                      "差分提案では埋めない", "読めない理由と `.claude/project-profile.yml`")
        self.contains(generation, "差分提案では埋めず")
        self.contains(final, "差分提案では埋めず")
        rerun = section(self.skill, "**1. 新規 / 既存 / 再実行の判定**", "**2. 技術スタック")
        self.contains(rerun, "再実行でも Phase 1-5 で停止する")
        self.contains(task_store, "`task_dir` の値に `{{...}}` が残る場合も停止する", "未指定として検出へ進まず")
        directory = (ROOT / "plugins/dev-workflow/skills/create-task/references/task-directory.md").read_text()
        self.contains(directory, "init-project も停止し、先に人が手で直してから再実行")
        for obsolete in ("報告だけで停止はしない", "報告自体では停止せず", "未設定項目とみなして提案に含める"):
            self.assertNotIn(obsolete, self.skill)
        self.assertNotIn("例外として init-project だけは", directory)

    def test_directory_resolution_happens_only_after_file_selection(self):
        detection = section(self.skill, "**8. タスク保存先の解決**", "## Phase 2:")
        self.contains(detection, "保存先の解決は Phase 2 の選択後、Phase 3 の生成前に行う",
                      "`github-issue` なら解決・作成せず、`TASK_DIR` も使わない。`file` だけ次を行う",
                      "task-directory.md", "`task_dir` はホスト側で型を検証", "`TASK_DIR` として")
        directory = section(self.generation, "**4. タスク保存先**", "**5.")
        self.contains(directory, "`github-issue` ではこの手順を行わず、タスク用ディレクトリ・`.gitkeep`・タスク MD を作らない",
                      "Phase 1-8 で確定した保存先に `.gitkeep` を作る")
        self.assertLess(directory.index("`github-issue`"), directory.index("保存先に `.gitkeep` を作る"))

    def test_profile_placeholder_and_file_only_directory(self):
        self.assertEqual(1, self.profile.count("task_store: {{TASK_STORE}}\n"))
        self.contains(self.profile, "file(既定) | github-issue", "無い・null・空文字は file",
                      "それ以外の値や型は停止する", "github-issue では解決・作成しない")
        generation = section(self.generation, "**2. .claude/project-profile.yml**", "**3. doc/")
        self.contains(generation, "`{{TASK_STORE}}` は選択した `file` / `github-issue` に置換する",
                      "`github-issue` の新規生成では `task_dir` の行とその直前のコメント行を消す",
                      "既存の `task_dir` は変えず、使わない")
        directory_line = self.profile.splitlines().index("task_dir: '{{TASK_DIR}}'")
        self.assertTrue(self.profile.splitlines()[directory_line - 1].startswith("# file の保存先"))
        # 雛形に項目を足したのに置換の一覧へ足し忘れる変更を検出する。
        replacements = section(generation, "- 置換:", "- Phase 1-7")
        for placeholder in set(re.findall(r"\{\{[A-Z_]+\}\}", self.profile)):
            self.contains(replacements, placeholder)

    def test_legacy_notice_is_conditional_and_existing_files_are_preserved(self):
        self.assertEqual(1, self.agents.count("{{TASK_STORE_NOTICE}}"))
        generation = section(self.generation, "**1. 権威参照ファイル", "**2. .claude/project-profile.yml**")
        self.contains(generation, "`{{TASK_STORE_NOTICE}}` は `github-issue` のとき", "`file` なら行だけを消す",
                      "見出しと表の間の空行は1つ残す",
                      "既存ファイルには、同じ行が無いときだけ追記を提案する",
                      "タスクの置き場は GitHub Issue(`.claude/project-profile.yml` の `task_store`)",
                      "この設定を読まない古い版の skill では、ローカルのタスク MD を作らない")
        checks = section(self.skill, "**2. 妥当性の自己確認**", "**3. 初期コミット")
        self.contains(checks, "`{{TASK_STORE_NOTICE}}` が残らず", "既存のファイル用の案内も、上書きせず修正を提案")

    def test_issue_selection_warns_about_existing_local_tasks(self):
        detection = section(self.skill, "**5. 既存構成の有無**", "**6. MCP の利用可否**")
        self.contains(detection, "状態名 MD を見つけたら、保存先ごとの件数を控える")
        self.contains(self.questions, "Issue を選び、Phase 1-5 で状態名 MD を見つけていたら、件数と保存先を示す",
                      "Issue 方式の create-task・do-task では読まれず", "自動で移す仕組みも無い",
                      "ファイル方式のまま終えてから切り替えるか、手で Issue に移すよう案内")

    def test_generated_guidance_uses_issue_without_task_path(self):
        self.contains(self.generation, "設計書を GitHub Issue 本文に作成(影響範囲調査込み)",
                      "Issue 本文を使って実装・検証・レビュー", "`TASK_DIR` は埋めない",
                      "「一時的な TODO は GitHub Issue へ」に置換する")
        operations = (INIT / "templates/doc/05_operations.md.template").read_text()
        self.contains(operations, "一時的な TODO は `{{TASK_DIR}}/` へ")
        self.contains(self.agents, "| `/create-task` |", "| `/do-task` |", "`{{TASK_DIR}}/`")

    def test_skill_stays_within_line_limit(self):
        self.assertLessEqual(len(self.skill.splitlines()), 500)


if __name__ == "__main__":
    unittest.main()
