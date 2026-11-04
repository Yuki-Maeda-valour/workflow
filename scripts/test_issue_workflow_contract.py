"""Issue の後続工程の手順と、既存の外部本文機構との接続を確かめる。

skill は実行手順そのものなので、入口・停止条件・工程間の引渡しを検査する。
本文の最終化と index の計算は製品の関数で確かめる。外部コマンドは起動しない。
"""

import base64
import importlib.util
from pathlib import Path
import re
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

    def test_execution_boundary_comes_from_success_or_human_approval_not_body_dates(self):
        execution = section(self.contract, "## 5. do-task の読み替え")
        self.contains(execution, "前回の実行用の取得に成功したとき", "人が本文を承認したとき",
                      "`--edits-since <保持した last_edited_at>`", "本文の設計レビュー行の日付からは決めない",
                      "保持値が無い新しい開始", "全履歴を調べる", "境界と同じ日時も含める",
                      "承認待ちで停止した取得値や書き込みの直後の結果で、前の境界を先に進めない")

    def test_repeat_approval_shows_delta_and_intervening_editors(self):
        execution = section(self.contract, "## 5. do-task の読み替え")
        self.contains(execution, "2回目以降は、前に承認した本文との差分と、その間の編集者の一覧を示す",
                      "その間の編集者が実行者だけであることを確かめて示す",
                      "他人の編集があれば、その人と変更箇所を明記して採用を確認する")

    def test_post_write_receipt_defers_history_check_and_cannot_be_reused(self):
        update = section(self.contract, "## 3. 本文を取得して書き換える共通手順")
        self.contains(update, "最終編集者と取得時の実行者が書き込みの前の実行者と同じ場合だけ成功",
                      "書き込みの直後は編集履歴を取得・検査しない", "次の通常の `snapshot`",
                      "`history_checked` は false", "更新用の控えに使わず")

    def test_repository_mismatch_is_checked_before_branch_and_again_before_push(self):
        phase = section(self.ship, "## Phase 0: 前提と作業ブランチ")
        before_branch = phase.split("3. **作業ツリーの清潔性**:", 1)[1].split("4. **作業ブランチ**:", 1)[0]
        for text in (before_branch, self.shipping.split("### 続行判定と実装\n", 1)[0]):
            self.contains(text, "`--no-pr`・`--design-only` を除き、`preflight` の直後・ブランチ作成の前",
                          "公開する root", "`origin-repo.py`", "Issue の `repo` を比べる",
                          "不一致・算出不能なら", "止めるか `--no-pr` で続けるかを質問で確かめ")
        self.contains(self.shipping, "新規 Issue でも省略しない", "回答前はブランチ作成・設計・実装へ進まない",
                      "Phase 5 の再確認も残す", "不一致・算出不能なら push より前に停止する")

    def test_do_task_handoff_and_failed_finalization_do_not_commit(self):
        phase = section(self.ship, "## Phase 3: 実装(/do-task)")
        self.contains(phase, "/do-task --issue=<番号>", "保持した対象・R", "stage・commit・文書同期へ進まない")
        self.contains(self.shipping, "外部の実装担当への承認は求めず", "本文のコピーを `--task-md` に渡さない",
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
                      "権威参照ファイルが定める形に一致する、節内の最後の該当記録で判定する",
                      "その形に使用スキルの指定が無ければ、`do-task` の記載は必須にしない",
                      "`task_store: github-issue` の経路には、この例外を当てない",
                      "do-task の手順4の完了記録と `finalize` の検証器記録を受け付ける")
        mode = section(self.doc, "## 2 つの実行モード")
        self.contains(mode, "`github-issue` は完了ステータスも必須", "権威参照ファイルの運用では、その運用の記録の形も受け付ける")

    def test_suspension_before_commit_verification_invalidates_completion(self):
        common = self.completion.split("- `task_store: github-issue` の経路:", 1)[0]
        self.contains(common, "実装 commit の検証より前に止まったものだけ、完了を取り消して停止する",
                      "`finalize` 適用後の commit の照合・commit の失敗を含む",
                      "日付順に並べ替えず、節の中の記載順で判断する",
                      "中断・保留がどちらの時点か分からなければ止めて人に確かめる")
        self.contains(self.shipping, "実装 commit の検証前に中断: <理由>",
                      "`/do-task --issue=<番号>` で検証をやり直す")

    def test_suspension_after_verified_commit_keeps_completion(self):
        common = self.completion.split("- `task_store: github-issue` の経路:", 1)[0]
        for stage in ("文書同期", "公開"):
            with self.subTest(stage=stage):
                record = f"実装 commit <sha> は検証済み。{stage}で停止"
                self.contains(common, record, "実装の完了を取り消さず受け付ける")
                self.contains(self.shipping, record)
        mode = section(self.doc, "## 2 つの実行モード")
        self.contains(mode, "実装 commit の検証前の中断では停止する",
                      "検証後の文書同期・公開の停止は完了を取り消さない")

    def test_interruption_records_take_priority_over_any_completion_shape(self):
        common = self.completion.split("- `task_store: github-issue` の経路:", 1)[0]
        self.contains(common, "§6 の中断・停止の記録(`に中断:`・`で停止:` を含む行)と保留の記録は、",
                      "完了の形に当たっても完了記録として数えず、中断として先に判定する")
        self.assertLess(common.index("中断として先に判定する"), common.index("do-task の手順4の完了記録"))
        self.assertLess(common.index("中断として先に判定する"), common.index("`finalize`"))

    def test_publication_retry_before_push_keeps_body_state_and_hashes(self):
        stop = self.shipping.split("- 中断の記録は", 1)[1].split("### 中断の記録を書いた後の再開", 1)[0]
        self.contains(stop, "push より前の失敗は、まず Issue へは書かず、会話で理由を報告する",
                      "同じ state と保持値のまま Phase 5 をやり直せるのは、push より前の失敗だけ",
                      "同じセッションで Issue に記録する前に限る",
                      "push より前の失敗では、人がやめると決めたとき、またはセッションを終えるときだけ",
                      "実装 commit <sha> は検証済み。公開で停止: <理由>",
                      "本文・対象リポジトリ・commit の再確認は Phase 5 と同じ",
                      "やり直す前に push・PR の成否を確かめ", "既に作成された PR を重複作成しない",
                      "検査を省いて続けない")
        table = section(self.ship, "## 停止条件のまとめ")
        row = next(line for line in table.splitlines() if line.startswith("| Issue 方式の push より前の公開失敗。"))
        self.contains(row, "まず会話で報告し、人がやめると決めたとき・セッション終了時だけ Issue に記録する",
                      "push・PR の成否を確かめ、push より前の失敗だけ、同じセッションで Issue に記録する前なら",
                      "同じ state と保持値で Phase 5 の再確認を通して再試行できる")
        report = table.split("Issue 方式の停止報告は", 1)[1]
        self.contains(report, "やり直す前に push・PR の成否を確かめる",
                      "同じ state と保持値のまま Phase 5 をやり直せるのは、push より前の失敗だけ",
                      "同じセッションで Issue に記録する前に限る")
        self.assertNotIn("同じセッションでやり直せる間は", stop + table)

    def test_post_push_failures_record_stop_before_remaining_manual_operations(self):
        stop = self.shipping.split("- 中断の記録は", 1)[1].split("### 中断の記録を書いた後の再開", 1)[0]
        table = section(self.ship, "## 停止条件のまとめ")
        row = next(line for line in table.splitlines() if line.startswith("| Issue 方式の push 以後の失敗"))
        report = table.split("Issue 方式の停止報告は", 1)[1]
        stop_line = next(line for line in stop.splitlines() if line.startswith("  - push 以後の失敗"))
        report_line = next(line for line in report.splitlines() if line.startswith("- push 以後の失敗"))
        for text in (stop_line, row, report_line):
            self.contains(text, "push 以後の失敗(SHA の照合・追跡設定・PR 作成・作成後の照合)",
                          "Phase 5 をやり直さ", "Issue に記録してから、push・PR の成否を確かめ、",
                          "残った操作だけを手で打つ形で示す")
            self.assertLess(text.index("Phase 5 をやり直さ"), text.index("Issue に記録してから"))
        for text in (stop_line, report_line):
            self.contains(text, "`実装 commit <sha> は検証済み。公開で停止: <理由>` と Issue に記録")
        self.contains(stop, "`--set-upstream` の追跡設定で git 設定のダイジェストが変わり")
        self.assertNotIn("公開(push・PR)で止まったときは、まず Issue へは書かず", stop)

    def test_post_push_pr_failure_uses_stdin_and_checks_created_or_existing_pr(self):
        resume = self.shipping.split("### 中断の記録を書いた後の再開\n", 1)[1]
        self.contains(resume, "push と追跡設定が済んで PR の作成だけが失敗した場合は、push を繰り返さず、",
                      "`gh pr create -R <repo> --base <デフォルトブランチ> --head <ブランチ名> --title <題名> --body-file - < <PR本文ファイル>`",
                      "本文・対象リポジトリの再確認と、保持した `pr-evidence`・`Closes #<番号>` を含む PR 本文の準備を先に並べる",
                      "作成後は `gh api --hostname <host> repos/<owner>/<repo>/pulls/<番号>`",
                      "head と base のブランチ名(`head.ref`・`base.ref`)と head の SHA(`head.sha`)が、保持したレビュー済みの値と一致する",
                      "作成済みなら重複作成せず、この照合を行う",
                      "不一致・取得失敗なら公開完了とは扱わない")
        self.assertLess(resume.index("gh pr create -R"), resume.index("gh api --hostname"))

    def test_manual_pr_command_requires_exact_remote_reviewed_sha(self):
        resume = self.shipping.split("### 中断の記録を書いた後の再開\n", 1)[1]
        before_create = resume.split("`gh pr create -R", 1)[0]
        self.contains(before_create,
                      "「push 済み」は、リモートの `<ブランチ名>` の先頭が、保持したレビュー済みの `<SHA>` と一致すること",
                      "PR 作成のコマンドを示す直前に、Git の共通の安全用のオプションを付けて",
                      "`git push --dry-run --porcelain --no-follow-tags --recurse-submodules=no origin <SHA>:refs/heads/<ブランチ名>`",
                      "`publish-guard.py` の `remote_checked` と同じ条件",
                      "コマンドが成功し、標準出力のタブ区切りの行が1件だけで3列あり、1列目が `=`、2列目が `<SHA>:refs/heads/<ブランチ名>` と一致する場合だけ",
                      "不一致・確認不能なら PR 作成のコマンドを示さずに停止し、人に原因を確かめてもらう")

    def test_manual_pr_matching_checks_repositories_owner_url_and_number(self):
        resume = self.shipping.split("### 中断の記録を書いた後の再開\n", 1)[1]
        before_create, after_create = resume.split("`gh pr create -R", 1)
        self.contains(before_create,
                      "PR が既に作成済みかを探すときも、下の作成後の照合と同じ条件を使う",
                      "作成済みなら重複作成せず、この照合を行う")
        self.contains(after_create,
                      "`publish-guard.py` の `created_pr_checked` と同じ項目",
                      "head と base のリポジトリ(`head.repo.full_name`・`base.repo.full_name`)が `<owner>/<repo>`",
                      "head の所有者(`head.repo.owner.login`)が `<owner>`",
                      "PR の URL が `https://<host>/<owner>/<repo>/pull/<正の整数>` の形で1件だけ",
                      "API の `html_url` と `number` がその URL と番号に一致する")

    def test_documented_gh_body_file_commands_use_stdin(self):
        commands = []
        for path in SKILLS.rglob("*.md"):
            for command in re.findall(r"`(gh\b[^`\n]*)`", path.read_text()):
                if "--body-file" in command:
                    commands.append((path, command))
        self.assertTrue(commands)
        for path, command in commands:
            with self.subTest(path=path.relative_to(ROOT), command=command):
                self.assertRegex(command, r"--body-file\s+-(?:\s|$)")

    def test_recorded_publication_stop_requires_manual_commands_and_saved_evidence(self):
        resume = self.shipping.split("### 中断の記録を書いた後の再開\n", 1)[1]
        self.contains(resume, "検査つきの Phase 5 はやり直せない",
                      "`--task` は Phase 2 から始まる入口", "Phase 4・5 から再開する入口ではない",
                      "過去の全文の保持値を黙って更新して", "`pr-evidence` を更新後の Issue 本文から作り直さない",
                      "人が手で打つ push・PR のコマンドの並びを示す",
                      "§3・§5 で Issue 本文を取り直し、中断の追記とそれ以外の変更を確かめる",
                      "`origin-repo.py` の `repo` と、保持した Issue の `repo` が同じ",
                      "不一致・取得不能なら公開しない", "保持した `pr-evidence` の内容を PR の検証欄に使い",
                      "`Closes #<番号>`", "Issue の記録から検証根拠を復元しない")
        table = section(self.ship, "## 停止条件のまとめ")
        self.contains(table, "記録を書いた後は検査つきの Phase 5 をやり直せない",
                      "保持した `pr-evidence` と `Closes #<番号>`", "本文と対象リポジトリを確かめ直す手順")
        self.assertNotIn("Phase 5 を再開する手順", self.shipping + table)

    def test_recorded_document_stop_runs_update_doc_then_manual_commit_and_publication(self):
        resume = self.shipping.split("### 中断の記録を書いた後の再開\n", 1)[1]
        self.contains(resume, "文書同期での停止報告には `/update-doc --task=#<番号>`",
                      "単独の update-doc で文書を直す",
                      "文書レビューが APPROVED になった後の commit と公開は、人が手で打つコマンドの並びを示す",
                      "後から作る文書 commit を以前の検証根拠で検証済みとは扱わない")
        table = section(self.ship, "## 停止条件のまとめ")
        self.contains(table, "実装 commit の検証前後を区別する",
                      "記録後の文書同期は `/update-doc --task=#<番号>`", "その後の commit・公開は手動のコマンド列を示す")

    def test_authority_completion_rejects_incomplete_even_with_approved_result(self):
        self.contains(self.authority_completion, "「完了」の部分一致で「未完了」の記録を受け付けない")
        instructions = (ROOT / "AGENTS.md").read_text()
        shape = "`- <YYYY-MM-DD> ` で始まり、「未完了」「中断」「停止」を含まず、「完了」と「結果 APPROVED」を含む行"
        self.contains(instructions, shape)
        self.contains(self.design, shape)
        # 手順の条件を対照へ当てる。AGENTS.md の先頭の形を使い、必須語と除外語も別々に確かめる。
        declared = re.search(r"`(- <YYYY-MM-DD> )`", instructions).group(1)
        prefix = re.escape(declared).replace(re.escape("<YYYY-MM-DD>"), r"\d{4}-\d{2}-\d{2}")
        pattern = prefix + r"(?![^\n]*(?:未完了|中断|停止))(?=[^\n]*完了)(?=[^\n]*結果 APPROVED)[^\n]*"
        accepted = [
            "- 2026-11-04 完了(反復 5 回): 結果 APPROVED",
            "- 2026-11-04 do-task 完了(反復 5 回): 結果 APPROVED",
            "- 2026-11-04 前半を完了(反復 5 回): 結果 APPROVED。後半は別途",
            "- 2026-11-04 後半を完了(反復 5 回): 結果 APPROVED",
        ]
        rejected = [
            "- 2026-11-04 未完了(反復 5 回): 結果 APPROVED",
            "- 2026-11-04 do-task 未完了(反復 5 回): 結果 APPROVED",
            "- 2026-11-04 レビュー: 結果 APPROVED",
            "- 2026-11-04 完了(反復 5 回): 結果 未完了",
            "- 完了(反復 5 回): 結果 APPROVED",
            "- 2026-11-05 実装 commit の検証前に中断: 完了記録の適用後に commit の照合が不一致(実装レビューは結果 APPROVED)",
            "- 2026-11-05 実装 commit " + "a" * 40 + " は検証済み。公開で停止: 完了記録の適用後に PR 作成が失敗(結果 APPROVED)",
        ]
        for line in accepted:
            with self.subTest(accepted=line):
                self.assertIsNotNone(re.fullmatch(pattern, line))
        for line in rejected:
            with self.subTest(rejected=line):
                self.assertIsNone(re.fullmatch(pattern, line))
        self.contains(self.authority_completion, "権威参照ファイルが定める形に一致する、節内の最後の該当記録で判定する")
        records = accepted + rejected
        self.assertEqual(accepted[-1], [line for line in records if re.fullmatch(pattern, line)][-1])

    def test_authority_must_define_its_own_completion_shape(self):
        self.contains(self.authority_completion, "権威参照ファイルも完了記録の形を定めていなければ、止めて人に形を確かめる",
                      "配布文書の例からプロジェクトの形を補わない")
        self.assertNotIn("2026-11-04 完了", self.contract)
        self.contains(self.design.split("22. **リポジトリ自身の Issue 運用**:", 1)[1].split("\n23. ", 1)[0],
                      "例: `- 2026-11-04 完了(反復 5 回): 結果 APPROVED`")

    def test_completion_is_selected_by_record_shape_not_caller(self):
        self.contains(self.completion, "単独実行か ship-task 経由かでは分けない")
        self.assertNotIn("単独 do-task の記録は", self.syncing)
        self.assertNotIn("ship-task 経由では", self.syncing)

    def test_stopping_table_covers_issue_specific_failures(self):
        table = section(self.ship, "## 停止条件のまとめ")
        self.contains(table, "Issue 方式のリポジトリ不一致・算出不能", "完了後の本文と保持値の不一致",
                      "update-doc の完了検査の失敗", "他人の Issue の再確認",
                      "`task_store: github-issue` のステータス行の欠落・重複")

    def test_design_uses_profile_and_keeps_legacy_route_separate(self):
        policy = self.design.split("22. **リポジトリ自身の Issue 運用**:", 1)[1].split("\n23. ", 1)[0]
        self.contains(policy, "`github-issue.md` §6・§7 を使う", "`AGENTS.md` が定める形は、`- <YYYY-MM-DD> ` で始まり",
                      "`.claude/project-profile.yml` の `task_store: github-issue` を選ぶ",
                      "`task_store: github-issue` では、template の完了ステータス行と do-task の完了記録が引き続き必須",
                      "権威参照ファイルによる Issue 運用でも停止する",
                      "現在の profile の経路へ例外を広げない", "過去の記録に不足があれば自動で完了とせず",
                      "設定はこの1項目だけ", "無人ループは本リポジトリの `task_store` を読んで起動時に停止")
        self.assertNotIn("本リポジトリの profile を新設しない", policy)
        self.assertNotIn("未対応", policy)
        protocol = (SKILLS / "do-task/references/review-protocol.md").read_text()
        self.contains(protocol, "または権威参照ファイルが Issue 等を正本にする場合")

    def test_repository_profile_and_minimal_legacy_instructions(self):
        profile = (ROOT / ".claude/project-profile.yml").read_text()
        self.assertEqual("task_store: github-issue\n", profile)
        instructions = section((ROOT / "AGENTS.md").read_text(), "## このリポジトリの Issue 運用")
        self.contains(instructions, "task_store: github-issue", "do-task/references/github-issue.md",
                      "古い版でも、作業記録の正本は GitHub Issue 本文", "ローカルのタスク成果物は新設しません",
                      "現在の版では、Issue 方式の契約が定めるステータス行と完了記録が必須")
        self.assertNotIn("profile へ移すまでの暫定", instructions)
        for old_procedure in ("基準コミット", "gh issue view", "設計レビューの行の `本文`"):
            self.assertNotIn(old_procedure, instructions)

    def test_open_issue_migration_requires_header_and_preserves_design_digest(self):
        policy = self.design.split("22. **リポジトリ自身の Issue 運用**:", 1)[1].split("\n23. ", 1)[0]
        instructions = section((ROOT / "AGENTS.md").read_text(), "## このリポジトリの Issue 運用")
        for document in (policy, instructions):
            self.contains(document, "開いた Issue も", "`> **ステータス**: 🚧 進行中` の行が1行必要",
                          "ship-task は実装前に停止", "do-task を単独で完了しても、その後の update-doc が停止",
                          "コードフェンスの外の最初の `## ` より前", "上の1行を行頭から追加",
                          "task-digest.py", "既存の設計レビュー行と値が一致していた本文",
                          "この1行だけを追加しても値は変わ", "設計レビュー行はそのまま使え",
                          "Issue 本文の再取得と承認")

    def test_repo_preflight_measurement_keeps_only_official_recheck_unconfirmed(self):
        facts = self.design.split("### 7-3.", 1)[1].split("### 7-4.", 1)[0]
        permission = next(line for line in facts.splitlines()
                          if line.startswith("| **gh** の `repo view <repo> --json name,isPrivate,viewerPermission`"))
        self.contains(permission, "2026-11-04・gh 2.103.3", "テスト用の [Issue #331]", "team-lead からの報告",
                      "`preflight`", "`github.com/Yuki-Maeda-valour/workflow`・公開・`ADMIN` を取得",
                      "公式本文の再確認は未確認")
        self.assertNotIn("実機確認は未確認", permission)

    def test_github_write_measurement_records_observations_and_limits(self):
        facts = self.design.split("### 7-3.", 1)[1].split("### 7-4.", 1)[0]
        write = next(line for line in facts.splitlines() if line.startswith("| **gh** の `issue create/edit --body-file -`"))
        self.contains(write, "2026-11-04・gh 2.103.3", "テスト用の [Issue #331]", "team-lead からの報告",
                      "`create --approve-public`", "`snapshot --for-execution`", "`check-checkboxes`",
                      "`update --checkbox-only`", "`update`(追記)", "古い控えでの `update` は書かずに停止")
        self.assertNotIn("未確認", write)
        history = next(line for line in facts.splitlines() if line.startswith("| GraphQL の Issue の `userContentEdits`"))
        self.contains(history, "履歴2件(作成時の版と編集)", "最終編集と一致", "`--edits-since`",
                      "100件を超える履歴のページ送りと、他の条件での反映時期は未確認",
                      "履歴の反映が遅れる可能性", "偽の `gh`")
        unedited = next(line for line in facts.splitlines() if line.startswith("| 未編集の Issue"))
        self.contains(unedited, "#331", "作成直後は両方 null、編集履歴は0件")

    def test_changelog_issue_release_has_final_behavior(self):
        changelog = (ROOT / "CHANGELOG.md").read_text()
        self.assertEqual(1, changelog.count("## v4.37.0\n"))
        self.assertLess(changelog.index("## v4.37.0\n"), changelog.index("## v4.36.0\n"))
        release = changelog.split("## v4.37.0\n", 1)[1].split("\n## v", 1)[0]
        self.assertNotIn("未対応", release)
        self.contains(release, "ship-task", "update-doc", "create-task", "do-task")
        self.contains(release, "push より前の失敗だけ、Issue に記録する前は、同じセッションの state と保持値で公開をやり直せる",
                      "人がやめると決めたとき・セッション終了時だけ Issue に記録する",
                      "push 以後の失敗(SHA の照合・追跡設定・PR 作成・作成後の照合)では、Phase 5 をやり直さない",
                      "公開停止を Issue に記録してから、push・PR の成否を確かめ、残った操作だけを手で打つ形で示す",
                      "`--body-file - < <PR本文ファイル>` で標準入力から本文を渡す",
                      "リモートのブランチの先頭が保持したレビュー済みの SHA と一致することを確かめる",
                      "不一致・確認不能なら PR 作成のコマンドを示さず止める",
                      "作成後は `gh api` で head・base のブランチ名・head の SHA・両方のリポジトリ・head の所有者・PR の URL と番号を照らす",
                      "作成済みの PR を探すときも同じ条件を使い、重複作成しない",
                      "公開停止を記録するまで各工程の内訳を会話に残す",
                      "中断・停止・保留の記録は、完了の形に当たっても完了記録に数えない",
                      "中断の記録を書いた後は、検査つきの Phase 5 をやり直せない",
                      "文書同期は `/update-doc --task=#<番号>` で再開し、その後の commit・公開は手動のコマンド列を示す")
        self.assertNotIn("止まった工程から再開できる", release)

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
        for record in ("- 2026-11-04 中断: commit の照合に失敗\n", "- **保留**: 公開に失敗\n",
                       "- 実装 commit " + "a" * 40 + " は検証済み。文書同期で停止: 未承認\n",
                       "- 実装 commit " + "a" * 40 + " は検証済み。公開で停止: push 失敗\n"):
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
