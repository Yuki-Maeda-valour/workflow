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
        cls.completion = cls.syncing.split("### 完了記録の形\n", 1)[1]
        cls.template_completion, cls.authority_completion = cls.completion.split(
            "- `task_store: github-issue` の経路: ", 1)[1].split(
            "- 権威参照ファイルによる Issue 運用の経路: ", 1)
        cls.design = (ROOT / "docs/design.md").read_text()

    def contains(self, text, *required):
        for value in required:
            with self.subTest(required=value):
                self.assertIn(value, text)

    def test_routing_keeps_authority_override_and_rejects_unattended_early(self):
        for text, route in ((self.ship, "§6 で各 Phase を読み替える"),
                            (self.doc, "§7 で入力を選ぶ")):
            entrance = section(text, "## タスクの置き場の検査")
            self.contains(entrance, "不正な値は停止する。ファイル方式へ切り替えて続けない",
                          "`github-issue`、または権威参照ファイル", route,
                          "ファイルの保存先は解決・作成しない", "--unattended",
                          "task-store-unsupported", "対象選択・本文取得", "失敗扱いで停止")
            self.assertNotIn("Issue 方式は未対応", text)

    def test_unattended_references_reject_authority_issue_before_work(self):
        for path, heading, before in (
                ("unattended-mode.md", "## 1. 前提と分担", "対象選択・本文取得・ブランチ作成・文書更新"),
                ("discover-mode.md", "## 1. 引数と最初の Read", "調査・候補選択・本文取得・ブランチ作成・書き込み")):
            with self.subTest(path=path):
                text = section((SKILLS / "ship-task/references" / path).read_text(), heading)
                self.contains(text, "`task_store` が `file` 以外なら `task-store-unsupported`",
                              "`file` でも権威参照ファイル", "Issue を正本にする運用なら、",
                              "`task-store-unsupported` で失敗扱いにする", "環境の検査直後、", before + "より前に止める")

    def test_loop_only_detects_profile_store(self):
        text = (SKILLS / "ship-task/references/loop.md").read_text()
        profile = text.split("### profile(`features.loop`)と実効値\n", 1)[1].split("\n## ", 1)[0]
        self.contains(profile, "`loop.sh` が置き場の判定で見るのは `task_store` だけ",
                      "ループは起動するが、毎周の ship-task が `task-store-unsupported` で止まる",
                      "権威参照ファイルの運用はループ起動時には判定しない")

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

    def test_template_status_is_required_before_implementation_but_not_authority(self):
        phase = section(self.ship, "## Phase 2: 続行判定ゲート(唯一の自動停止判断)")
        self.contains(phase, "`task_store: github-issue` はヘッダのステータス行もちょうど1つ必要",
                      "欠落・重複なら実装前に設計の修正", "権威参照ファイルによる Issue 運用では必須にしない")
        continuation = self.shipping.split("### 続行判定と実装\n", 1)[1].split("- Phase 3 は", 1)[0]
        self.contains(continuation, "最初の `## ` より前に `> **ステータス**:` の行がちょうど1つ",
                      "0行・重複なら実装の前に止め", "create-task で設計の書式を直す",
                      "権威参照ファイルによる Issue 運用では、この行を必須にしない")

    def test_foreign_issue_approval_count_matches_do_task_and_stop_table(self):
        do_task = section(self.contract, "## 5. do-task の読み替え")
        self.contains(do_task, "承認を自分の更新後の本文へ引き継がない", "最大 `2 + N` 回",
                      "途中で実行用の再取得を追加した場合も、未承認の本文なら1回増える")
        entrance = self.shipping.split("### 続行判定と実装\n", 1)[0]
        self.contains(entrance, "Phase 0 で §5 の確認の回数と理由を予告する",
                      "最大 `2 + N` 回", "最後のチェック更新後", "`finalize` 適用後の最初の入口",
                      "本文が変わらなければ、同じ会話の承認値を渡して再確認を増やさない")
        principle = section(self.ship, "## 原則").split("2. **既定は走り切る**", 1)[1].split("3. **", 1)[0]
        self.contains(principle, "他人が作った Issue", "本文が変わった後の実行用の再取得", "Phase 0 で予告")
        for heading in ("## Phase 0: 前提と作業ブランチ", "## 停止条件のまとめ"):
            self.contains(section(self.ship, heading), "最大 `2 + N` 回", "最後のチェック更新後",
                          "`finalize` 適用後", "未承認の本文ごとに1回増える")

    def test_repository_mismatch_is_checked_before_branch_and_again_before_push(self):
        phase = section(self.ship, "## Phase 0: 前提と作業ブランチ")
        before_branch = phase.split("3. **作業ツリーの清潔性**:", 1)[1].split("4. **作業ブランチ**:", 1)[0]
        for text in (before_branch, self.shipping.split("### 続行判定と実装\n", 1)[0]):
            self.contains(text, "`--no-pr` を除き、`preflight` の直後・ブランチ作成の前",
                          "公開する root", "`origin-repo.py`", "Issue の `repo` を比べる",
                          "不一致・算出不能なら", "止めるか `--no-pr` で続けるかを質問で確かめ")
        self.contains(self.shipping, "新規 Issue でも省略しない", "回答前はブランチ作成・設計・実装へ進まない",
                      "Phase 5 の再確認も残す", "不一致・算出不能なら push より前に停止する")

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
        self.contains(self.template_completion, "> **ステータス**: ✅ 完了(YYYY-MM-DD)", "ステータス行が1つ",
                      "do-task の手順4の完了記録か `finalize` の検証器記録のどちらか",
                      "ステータスか記録が欠ける・未完了・取得不能なら停止")
        self.assertNotIn("ステータス行は必須にせず", self.template_completion)
        self.assertNotIn("結果 APPROVED", self.template_completion)
        self.contains(self.completion, "`## 追加修正記録` の節内の最後の完了記録を使う",
                      "フェンス内の例・コメント・設計レビュー行・保留の行だけでは満たさない",
                      "使用スキル `do-task`・品質確認結果・独立レビュー結果・実動確認の状態",
                      "- 検証器記録: APPROVED / 根拠 sha256:<64桁> / review:<64桁>")

    def test_authority_completion_accepts_existing_approved_line_without_status(self):
        self.contains(self.authority_completion, "ステータス行は必須にせず、その運用が定める完了記録の形も受け付ける",
                      "日付・「完了」・「結果 APPROVED」を含む行", "節内の最後の該当行で判定する",
                      "`- 2026-11-04 完了(反復 5 回): 結果 APPROVED`",
                      "この行だけでも受け付け、使用スキル `do-task` の記載は必須にしない",
                      "`task_store: github-issue` の経路には、この例外を当てない",
                      "do-task の手順4の完了記録と `finalize` の検証器記録を受け付ける")
        mode = section(self.doc, "## 2 つの実行モード")
        self.contains(mode, "`github-issue` は完了ステータスも必須", "権威参照ファイルの運用では、その運用の記録の形も受け付ける")

    def test_later_suspension_invalidates_last_completion_for_both_routes(self):
        common = self.completion.split("- `task_store: github-issue` の経路:", 1)[0]
        self.contains(common, "最後の完了記録より後に中断・保留の記録があれば、完了と扱わず停止する",
                      "`finalize` 適用後に commit の照合・commit・公開で失敗した記録も含む",
                      "日付順に並べ替えず、節の中の記載順で判断する")

    def test_completion_is_selected_by_record_shape_not_caller(self):
        self.contains(self.completion, "単独実行か ship-task 経由かでは分けない")
        self.assertNotIn("単独 do-task の記録は", self.syncing)
        self.assertNotIn("ship-task 経由では", self.syncing)

    def test_stopping_table_covers_issue_specific_failures(self):
        table = section(self.ship, "## 停止条件のまとめ")
        self.contains(table, "Issue 方式のリポジトリ不一致・算出不能", "完了後の本文と保持値の不一致",
                      "update-doc の完了検査の失敗", "他人の Issue の再確認",
                      "`task_store: github-issue` のステータス行の欠落・重複")

    def test_design_keeps_routes_separate_and_defers_profile_migration(self):
        policy = self.design.split("22. **リポジトリ自身の Issue 運用**:", 1)[1].split("\n23. ", 1)[0]
        self.contains(policy, "`github-issue.md` §6・§7 を使う", "日付・「完了」・「結果 APPROVED」を含む行",
                      "ステータス行と使用スキル `do-task` の記載は必須にしない",
                      "`task_store: github-issue` では、template の完了ステータス行と do-task の完了記録が引き続き必須",
                      "権威参照ファイルによる Issue 運用でも停止する", "profile への移行と init-project の質問・雛形は3本目")
        self.assertNotIn("この1本目では", policy)
        self.assertNotIn("未対応", policy)
        changelog = (ROOT / "CHANGELOG.md").read_text().split("## (次の次の版)\n", 1)[1].split("## (次の版)", 1)[0]
        self.assertNotIn("版の数字は merge の順に付ける", changelog)

    def test_update_doc_rechecks_input_without_mutating_issue(self):
        self.contains(self.syncing, "preflight` と `snapshot --for-execution`", "--yes` でも本文採用の確認を省かない",
                      "文書の適用前・レビュー前・最終報告前", "全文の一致", "入力を黙って差し替えない",
                      "Issue 本文・コメント・close・ラベルは変更しない",
                      "入力の不備でレビュー前に止まった場合は `未完了` とする")


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

    def test_finalize_without_status_preserves_authority_record_and_adds_proof(self):
        legacy = "- 2026-11-04 完了(反復 5 回): 結果 APPROVED\n".encode()
        body = self.body.replace("> **ステータス**: 🚧 進行中\n".encode(), b"") + legacy
        saved = {**self.saved, "task_original": base64.b64encode(body).decode(),
                 "task_body_digest": self.guard.body_digest(body)}
        final = self.guard.final_bytes(saved, self.proof, "complete")
        self.assertNotIn("> **ステータス**:".encode(), final)
        self.assertIn(legacy, final)
        self.assertRegex(final.decode(), r"\n- 検証器記録: APPROVED / 根拠 sha256:[a-f0-9]{64} / review:[a-f0-9]{64}\n")
        self.assertEqual(self.guard.body_digest(final), saved["task_body_digest"])

    def test_finalize_rejects_duplicate_header_status(self):
        body = self.body.replace(b"\n\n## ", "\n> **ステータス**: 🚧 進行中\n\n## ".encode(), 1)
        saved = {**self.saved, "task_original": base64.b64encode(body).decode()}
        with self.assertRaisesRegex(self.guard.GuardError, "ステータス行が重複"):
            self.guard.final_bytes(saved, self.proof, "complete")

    def test_later_interruption_is_invisible_to_contract_digest(self):
        final = self.guard.final_bytes(self.saved, self.proof, "complete")
        for record in ("- 2026-11-04 中断: commit の照合に失敗\n", "- **保留**: 公開に失敗\n"):
            with self.subTest(record=record):
                interrupted = final + record.encode()
                self.assertEqual(self.guard.body_digest(interrupted), self.guard.body_digest(final))
                self.assertNotEqual(self.guard.sha256(interrupted), self.guard.sha256(final))

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
