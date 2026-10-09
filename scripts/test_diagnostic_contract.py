"""診断の親保持・開始順序・停止条件を、公開する呼出文書で検査する。"""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'plugins/dev-workflow/skills'
REFS = SKILLS / 'do-task/references'


def section(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


class DiagnosticCallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task = (SKILLS / 'do-task/SKILL.md').read_text()
        cls.ship = (SKILLS / 'ship-task/SKILL.md').read_text()
        cls.ext = (REFS / 'external-runners.md').read_text()
        cls.ref = (REFS / 'diagnostic-git.md').read_text()
        cls.base = (REFS / 'base-commit.md').read_text()

    def test_start_is_held_before_branch_and_base_is_not_replaced(self):
        task = section(self.task, '3. 先に [references/base-commit.md]', '4. `.claude/grasp.md`')
        self.assertLess(task.index('開始時 `S`'), task.index('**ユーザーがブランチ作成'))
        self.assertIn('取り直さない', task)
        self.assertIn('基準 `B`', task)
        self.assertIn('resume-invocation', task)
        ship = section(self.ship, '3. **作業ツリーの清潔性**', '5. リモート')
        self.assertLess(ship.index('開始時 `S`'), ship.index('4. **作業ブランチ**'))
        for text in (task, ship, self.base):
            with self.subTest(text=text[:30]):
                self.assertIn('not-recorded', text)
                self.assertRegex(text, '実際に.*報告|今回作成して報告')
        for word in ('`S` は `B` を置き換えない', '5条件', '基準不明', '推定しない'):
            self.assertIn(word, self.base)

    def test_take_plan_carry_parent_report_then_external_launch(self):
        before = section(self.task, '3. **起動前のスナップショットと退避**', '4. **起動**')
        for word in ('take', 'STATE_DIR', 'MANIFEST_SHA256', 'SNAPSHOT_SHA256',
                     '`plan`', '--carry-plan', '全有効対象', '親が保持', '起動前報告',
                     '`B`', '`S`', '座標', '外部を起動せず停止'):
            self.assertIn(word, before)
        order = section(self.ext, '### 12-1a.', '### 12-2.')
        for first, second in [('`take` 成功後', '`plan`'),
                              ('`plan`', '親が保持・報告'),
                              ('親が保持・報告', '外部実装を起動')]:
            self.assertLess(order.index(first), order.index(second))
        for word in ('--context-start', '親の OID を使い回さない', '--exclude-root management/context',
                     '残存ファイルからhashを再算出して採用しない'):
            self.assertIn(word, order)

    def test_changed_settings_use_separate_diagnostic_without_bypassing_resume(self):
        route = section(self.ext, '設定を戻さずに調べる場合は', '**外部 / 内蔵 implementer の宛先識別**')
        for word in ('子孫の停止', '`extend-plan` → 新計画の親保持・報告 → `run`',
                     '`run` 失敗でも最新計画を保持', '応答不明', '旧計画による反復',
                     '残存hashの再採用', '人の判断待ち', '通常復帰条件は短絡しない'):
            self.assertIn(word, route)
        self.assertNotIn('戻さないまま人が git を打つ手順は定めない', self.ext)
        # 通常の復帰工程と復元の有限監督を削って新入口へ置換していない。
        for word in ('① `config-check`', '② `--precheck`', '③ `compare`', '④ `taskmd-diff`',
                     'restore_taskmd_supervised', 'TOUCHED=unknown / TEMP=unknown'):
            self.assertIn(word, self.ext)

    def test_another_session_has_stop_and_human_held_plan_not_discovery_trust(self):
        route = section(self.ext, '**関連する記録の再開手順**:', '**手動工程で一覧が変わった場合の再確認**:')
        self.assertLess(route.index('人が以前の外部処理'), route.index('最新計画のパス/hash'))
        self.assertLess(route.index('最新計画のパス/hash'), route.index('`extend-plan`'))
        for word in ('候補発見を承認とせず', '人の判断待ち', '旧計画不在・保持値喪失',
                     '現在profileから作り直さない', 'config-check→precheck→compare→taskmd-diff',
                     '全候補', 'acknowledged'):
            self.assertIn(word, route)
        old = section(self.ref, '## 9.', '## 10.')
        for word in ('context欠落', '起動前計画なし', '22', '補完しない', '通常内蔵5読取り', '削除しない'):
            self.assertIn(word, old)

    def test_new_plan_is_kept_on_run_failure_and_unknown_extension_stops(self):
        flow = section(self.ref, '## 5.', '## 6.')
        self.assertLess(flow.index('新しい計画のパスとhashを先に保持'), flow.index('`run` に渡す'))
        for word in ('最新派生計画を保持', '古い計画へ戻らない',
                     '旧計画で `run` や `extend-plan` を反復せず', '人の証拠照合',
                     'hashを取り直す', '自動で起動前計画を作り直す'):
            self.assertIn(word, flow)
        cleanup = section(self.ext, '診断用の最新計画パス/hashも', '**受け入れた限界**')
        for word in ('元の保護記録・入力計画・公開済み派生計画を削除しない',
                     '診断成功を自動削除の条件にしない', 'Phase 7'):
            self.assertIn(word, cleanup)

    def test_usual_builtin_five_operations_do_not_require_new_plan(self):
        doc = (REFS / 'implementation-git.md').read_text()
        for word in ('既存5操作には診断用の計画を必須にしない', '内蔵実装・検証のみ',
                     '比較不能から `prepare` / `run` を呼び直して診断の基準にはしない'):
            self.assertIn(word, doc)
        for operation in ('status', 'diff', 'log', 'show', 'ls-files'):
            self.assertRegex(doc, r'\| `' + operation + r'` \|')

    def test_plan_failure_keeps_evidence_and_is_excluded_from_normal_cleanup(self):
        failure_name = '診断計画の作成失敗'
        before = section(self.task, '3. **起動前のスナップショットと退避**', '4. **起動**')
        task_cleanup = section(self.task, '8. **報告(サイレント縮退禁止)**', '比較不能の診断は')
        ext_cleanup = section(self.ext, '**保持と削除(', '**縮退(')
        preparation = section(self.ref, '## 2.', '## 3.')
        external_preparation = section(self.ext, '### 12-1a.', '### 12-2.')
        for label, text in (('起動前', before), ('Phase 3 の削除', task_cleanup),
                            ('外部手順の削除', ext_cleanup), ('診断の準備', preparation),
                            ('外部手順の準備', external_preparation)):
            with self.subTest(route=label):
                self.assertIn(failure_name, text)
                self.assertRegex(text, '保護記録と残存計画を保持|保護領域と残存計画を削除せず')
                self.assertIn('人の判断', text)
        for text in (task_cleanup, ext_cleanup):
            self.assertIn('診断計画の作成失敗を除く', text)
            self.assertIn('Phase 7', text)
            self.assertIn('implement-guard.sh cleanup --state', text)
            self.assertIn('正常終了・引き継ぎ・縮退', text)
            self.assertNotIn('消さないのは比較不能とタスク MD の判断待ちだけ', text)
        for word in ('失敗・途絶・不正応答', 'PyYAML', '不正なprofile', '出力先の競合'):
            self.assertIn(word, preparation)
        self.assertIn('残存ファイルからhashを取り直して採用しない', preparation)

    def test_schema_and_each_variant_are_public_not_only_names(self):
        schema = section(self.ref, '## 10.', '## 11.')
        for key in ('state_binding', 'context_id', 'management_binding', 'parent_plan_sha256',
                    'prepared_request', 'source_checks', 'policies', 'default_policy',
                    'identity_chain', 'relative_prefix_b64', 'history_origin', 'branch_provenance',
                    'commit_oid', 'profile_blob_oid', 'profile_sha256', 'root_kind',
                    'resolved_revisions', 'current_profile', 'symbolic_target_b64',
                    'observed-current', 'observed-head', 'observed-ref'):
            self.assertIn(key, schema)
        public = section(self.ref, '## 11.', '## 12.')
        variants = {
            'status-v1': ('staged_comparison', 'worktree_comparison', 'stages'),
            'files-v1': ('tracked', 'endpoint', 'index_flags'),
            'diff-v1': ('left,right', 'comparison', 'patch_b64'),
            'blob-v1': ('path_b64', 'endpoint', 'bytes_b64'),
            'history-v1': ('format', 'limit', 'unborn'),
            'refs-v1': ('name_b64', 'symbolic_target_b64', 'resolution'),
            'stashes-v1': ('bytes_b64', 'limit'),
        }
        for variant, fields in variants.items():
            row = next(line for line in public.splitlines() if line.startswith('| `' + variant + '` |'))
            with self.subTest(variant=variant):
                for field in fields:
                    self.assertIn(field, row)
        for value in ('280MiB', '1GiB', '200000', '2000', '64KiB', '4096',
                      'sparse-not-materialized', 'preparation-stale', 'resume_authorized:false'):
            self.assertIn(value, self.ref)

    def test_public_plan_and_option_keys_match_fixed_cli_schema(self):
        # 公開契約側の期待値を固定する。動的importや入口実行は不要。
        def assignments(name):
            tree = ast.parse((SKILLS / 'do-task/scripts' / name).read_text())
            return {node.targets[0].id: ast.literal_eval(node.value)
                    for node in tree.body if isinstance(node, ast.Assign)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ('PLAN_KEYS', 'POLICY_KEYS', 'SOURCE_KEYS', 'OPTION_KEYS')}
        keys = assignments('diagnostic-plan.py')
        expected = {
            'PLAN_KEYS': 'version state_binding context_id management_binding parent_plan_sha256 prepared_request source_checks policies default_policy',
            'POLICY_KEYS': 'context_id root_kind root_prefix_b64 history_origin start_kind branch_provenance sources globs eres',
            'SOURCE_KEYS': 'role commit_oid profile_blob_oid profile_sha256 absent',
        }
        self.assertEqual({k: set(v.split()) for k, v in keys.items()},
                         {k: set(v.split()) for k, v in expected.items()})
        options = assignments('diagnostic-git.py')['OPTION_KEYS']
        self.assertEqual(options, {
            'status': 'path_b64 under_b64 untracked',
            'files': 'path_b64 under_b64 kind',
            'diff': 'path_b64 under_b64 left right format context',
            'show': 'path_b64 under_b64 rev format',
            'log': 'path_b64 under_b64 to limit format',
            'refs': '', 'stashes': 'limit',
        })
        for group in (*expected.values(), *options.values()):
            for key in group.split():
                self.assertIn(key, self.ref)

    def test_runtime_and_output_contract_preserve_normal_and_failure_paths(self):
        runtime = section((REFS / 'runtime-requirements.md').read_text(),
                          '## 設定を戻さずに使う診断', '## タスク本文の復元')
        for text in ('Python 3.10', 'POSIX', 'python3 -I', 'pthread_sigmask', 'GNU `grep`',
                     'すべて不在なら PyYAML を要求しない', 'macOS 実機は未確認',
                     'GNU `timeout` は診断の新しい必須条件にしない'):
            self.assertIn(text, runtime)
        for text in ('完全なJSONが届いても終了値が非0なら成功ではない',
                     '継承stdout/stderrのflagsは変更しない', '二度目の応答を出さない',
                     '正当な大きい出力', '通常pipeの読み手停止'):
            self.assertIn(text, self.ref)
        # 診断追加で復元側の既存監督関数を消さない。
        self.assertIn('restore_taskmd_supervised()', (REFS / 'runtime-requirements.md').read_text())


if __name__ == '__main__':
    unittest.main()
