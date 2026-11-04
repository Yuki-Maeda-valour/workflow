"""Issue の後続工程の手順と、既存の外部本文機構との接続を確かめる。

skill は実行手順そのものなので、入口・停止条件・工程間の引渡しを検査する。
本文の最終化と index の計算は製品の関数で確かめる。外部コマンドは起動しない。
"""

import base64
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins/dev-workflow/skills"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def section(text, heading):
    """H2 の節だけを返し、別の工程にある説明で合格させない。"""
    return text.split(heading + "\n", 1)[1].split("\n## ", 1)[0]


class IssueWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ship = (SKILLS / "ship-task/SKILL.md").read_text()
        cls.doc = (SKILLS / "update-doc/SKILL.md").read_text()
        cls.contract = (SKILLS / "do-task/references/github-issue.md").read_text()
        cls.shipping = section(cls.contract, "## 6. ship-task の読み替え")
        cls.syncing = section(cls.contract, "## 7. update-doc の読み替え")

    def contains(self, text, *required):
        for value in required:
            with self.subTest(required=value):
                self.assertIn(value, text)

    def test_routing_keeps_authority_override_and_rejects_unattended_early(self):
        for text in (self.ship, self.doc):
            entrance = section(text, "## タスクの置き場の検査")
            self.contains(entrance, "不正な値は停止", "権威参照ファイル", "Issue", "正本",
                          "ファイルの保存先は解決・作成しない", "--unattended",
                          "task-store-unsupported", "対象選択・本文取得", "失敗扱いで停止")
            self.assertNotIn("Issue 方式は未対応", text)

    def test_issue_target_and_branch_are_bound_before_implementation(self):
        self.contains(self.shipping, "--task=#<番号>", "--task=<Issue の URL>", "正の整数",
                      "ファイルのパス・別リポジトリの URL・不正な番号は停止",
                      "ブランチ作成より前", "snapshot --for-execution",
                      "repo`・番号・URL をセッションで保持", "task/issue-<番号>",
                      "`--design-only`・`--no-pr` でも必要")
        self.contains(section(self.contract, "## 2. 対象と書き込み前の確認"),
                      "ホスト・所有者・リポジトリ", "不一致なら止める")

    def test_new_issue_is_fetched_only_after_creation_and_existing_after_precheck(self):
        self.contains(self.shipping, "手順2′ では `--task` の形式と併用引数だけを検査",
                      "手順3の安全な事前検査後", "`preflight` 後かつブランチ作成より前",
                      "Phase 0 で既存 Issue を選択・取得しない", "新規 Issue を作成し、その直後")
        phase = section(self.ship, "## Phase 0: 前提と作業ブランチ")
        before_status = phase.split("3. **作業ツリーの清潔性**:", 1)[0]
        self.assertNotIn("snapshot --for-execution", before_status)
        between = phase.split("3. **作業ツリーの清潔性**:", 1)[1].split("4. **作業ブランチ**:", 1)[0]
        self.contains(between, "安全な事前検査後", "preflight", "snapshot --for-execution", "Phase 1 の作成後")

    def test_phase_two_uses_fresh_body_and_strict_digest(self):
        phase = section(self.ship, "## Phase 2: 続行判定ゲート(唯一の自動停止判断)")
        self.contains(phase, "再取得した本文で6条件", "R として保持", "ファイル方式だけ")
        self.contains(self.shipping, "最後の設計レビュー行", "本文` が無い・行が無い・算出不能・不一致なら停止",
                      "close だけを完了の根拠にしない", "現在の本文から R を作り直すことはしない")

    def test_do_task_handoff_and_failed_finalization_do_not_commit(self):
        phase = section(self.ship, "## Phase 3: 実装(/do-task)")
        self.contains(phase, "/do-task --issue=<番号>", "保持した対象・R", "stage・commit・文書同期へ進まない")
        self.contains(self.shipping, "外部の実装役への承認は求めず", "本文のコピーを `--task-md` に渡さない",
                      "finalize` の期待本文を共通の `update`", "新たな実装 commit は作らない")

    def test_commit_reads_external_body_instead_of_staging_a_task(self):
        self.contains(self.shipping, "--task-id issue-<番号> --task-body <取得本文> --expect-task-body-sha256",
                      "--task-dir` は省く", "タスク MD の stage・旧パスの削除を行わない",
                      "取得本文・取得JSON・期待本文も commit に含めない",
                      "EXPECTED_TASK_SHA256", "--task-transition complete",
                      "verify --mode pre-stage", "--mode stage", "--mode commit",
                      "commit 内の MD を読む代わりに Issue を再取得", "task_digest` = R")

    def test_document_phase_starts_after_implementation_with_its_own_state(self):
        phase = section(self.ship, "## Phase 4: ドキュメント同期(/update-doc --task)")
        self.contains(phase, "完了本文を取り直して検査", "別 `start`", "/update-doc --task=#<番号>")
        self.contains(self.shipping, "doc 用の新しい `start`", "take --phase doc", "実装 state を流用せず",
                      "task 遷移なし", "文書変更が無ければ doc commit は作らず",
                      "未承認の文書を commit せず")

    def test_publication_binds_issue_repo_and_keeps_direct_evidence(self):
        phase = section(self.ship, "## Phase 5: PR 作成")
        self.contains(phase, "保持した Issue の `repo` が同じ", "不一致なら push 前に止める", "Closes #<番号>")
        self.contains(self.shipping, "push・PR 直前にも Issue を取り直し", "管理ルートとソースの root が別",
                      "publish-guard.py --repo <repo>", "gh pr create -R <repo>",
                      "Issue の記録から検証結果やレビュー返答を復元しない")

    def test_update_doc_chooses_session_issue_or_asks_and_allows_audit(self):
        self.contains(self.syncing, "このセッションで do-task が完了にした Issue が1件なら使う",
                      "複数なら対象を質問", "番号または URL を質問", "全量監査も選べる",
                      "更新日時・一覧の先頭・close の順から自動選択しない",
                      "完了_*.md` は探さない", "--task` と `--analyze-only` は併用しない",
                      "既存の全量監査へ進む")

    def test_completion_requires_both_status_and_execution_record(self):
        self.contains(self.syncing, "> **ステータス**: ✅ 完了(YYYY-MM-DD)", "ステータス行が1つ",
                      "## 追加修正記録", "フェンス内の例・コメント・設計レビュー行・保留の行だけでは満たさない",
                      "使用スキル `do-task`・品質確認結果・独立レビュー結果・実動確認の状態",
                      "- 検証器記録: APPROVED / 根拠 sha256:<64桁> / review:<64桁>",
                      "ステータスか記録が欠ける・未完了・取得不能なら停止")

    def test_update_doc_rechecks_input_without_mutating_issue(self):
        self.contains(self.syncing, "preflight` と `snapshot --for-execution`", "--yes` でも本文採用の確認を省かない",
                      "文書の適用前・レビュー前・最終報告前", "全文の一致", "入力を黙って差し替えない",
                      "Issue 本文・コメント・close・ラベルは変更しない", "レビュー判定:", "未完了")


class ExternalBodyIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = load("issue_workflow_guard", SKILLS / "ship-task/scripts/review-guard.py")

    def setUp(self):
        # 外部本文の純粋な変換だけを試す。Git/gh を誤って呼ぶ変更も失敗させる。
        self.no_process = mock.patch.object(self.guard.subprocess, "run", side_effect=AssertionError("外部コマンド禁止"))
        self.no_process.start()
        self.addCleanup(self.no_process.stop)
        self.body = ("# 設計\n> **ステータス**: 🚧 進行中\n\n## 実装\n- [x] 実装する\n\n"
                     "## 追加修正記録\n- **設計レビュー**(create-task・2026-11-04): APPROVED\n").encode()
        self.saved = {
            "phase": "implementation", "task_path": "@external:issue-320",
            "task_source": {"task_id": "issue-320", "task_dir": None},
            "task_original": base64.b64encode(self.body).decode(),
            "task_body_digest": self.guard.body_digest(self.body),
            "review_binding_sha256": "a" * 64,
        }
        self.proof = {"verdict": "APPROVED", "record_date": "2026-11-04"}

    def test_finalize_emits_the_completion_record_consumed_by_update_doc(self):
        final = self.guard.final_bytes(self.saved, self.proof, "complete")
        self.assertIn(b"\n- [x] ", final)
        self.assertIn("> **ステータス**: ✅ 完了(2026-11-04)", final.decode())
        self.assertRegex(final.decode(), r"\n- 検証器記録: APPROVED / 根拠 sha256:[a-f0-9]{64} / review:[a-f0-9]{64}\n")
        self.assertEqual(self.guard.body_digest(final), self.saved["task_body_digest"])
        self.assertNotEqual(self.guard.sha256(final), self.guard.sha256(self.body))
        self.assertEqual(self.guard.new_task_path(self.saved, "complete"), "@external:issue-320")

    def test_contract_digest_alone_cannot_detect_status_or_record_changes(self):
        final = self.guard.final_bytes(self.saved, self.proof, "complete")
        for revised in (final.replace("✅ 完了(2026-11-04)".encode(), "🚧 進行中".encode()),
                        final.replace(b"review:" + b"a" * 64, b"review:" + b"b" * 64)):
            with self.subTest(revised=revised):
                self.assertEqual(self.guard.body_digest(revised), self.guard.body_digest(final))
                self.assertNotEqual(self.guard.sha256(revised), self.guard.sha256(final))

    def test_external_completion_adds_no_task_file_to_commit_index(self):
        source = {"path": "source.py", "kind": "file", "mode": "100644", "oid": "b" * 40}
        saved = {**self.saved, "root": "/unused", "task_root": "/unused",
                 "commit_paths": ["source.py"], "worktree": [source]}
        result = self.guard.expected_index(saved, {"worktree": [source]}, "complete")
        self.assertEqual(set(result), {"source.py"})
        self.assertEqual(result["source.py"]["oid"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
