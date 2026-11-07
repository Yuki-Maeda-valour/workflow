"""ship-task の Phase 4.5(取り込みと品質ゲートの流し直し)の手順の字面を確かめる。

手順は文書なので、結果と理由の値・停止の条件・起動の字面の必須の引数・公開の前の照合の条件を、
決まった語だけで照らす(言い回し全体は照らさない)。外部コマンドは起動しない。
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHIP = ROOT / "plugins/dev-workflow/skills/ship-task"

# integrate-guard.py のサブコマンドごとの必須の引数(`integrate-guard.py <サブコマンド> --` を含む字面が持つ)。
REQUIRED_ARGS = {
    "merge": ("--dir", "--branch", "--reviewed-sha", "--base", "--repo", "--exclude-ere"),
    "check": ("--dir", "--sha", "--reviewed-sha", "--base-sha", "--exclude-ere"),
    "stage-resolved": ("--dir", "--branch", "--reviewed-sha", "--base-sha", "--exclude-ere",
                       "--merge-result", "--expect-merge-result-sha256"),
    "commit": ("--dir", "--branch", "--reviewed-sha", "--base-sha", "--exclude-ere",
               "--merge-result", "--expect-merge-result-sha256"),
    "worktree": ("--dir", "--branch", "--head", "--exclude-ere"),
    "exclude": ("--dir", "--reviewed-sha", "--base-sha", "--previous", "--expect-previous-sha256"),
}
CALL = re.compile(r"integrate-guard\.py\s+(" + "|".join(REQUIRED_ARGS) + r")\s+--")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
INLINE = re.compile(r"`([^`\n]+)`")


def section(text, heading):
    """H2 の節だけを返し、別の節にある説明で合格させない。"""
    return text.split(heading + "\n", 1)[1].split("\n## ", 1)[0]


def subsection(text, heading):
    """H3 の小節を、次の `\\n### ` か `\\n## ` の先に来るほうまで切り出す。"""
    body = text.split(heading + "\n", 1)[1]
    return re.split(r"\n#{2,3} ", body, maxsplit=1)[0]


def code_literals(text):
    """コードフェンスの中身と、インラインのコードの字面を返す。"""
    fenced = [match[2] for match in FENCE.finditer(text)]
    return fenced + INLINE.findall(FENCE.sub("", text))


PHASE_HEADINGS = (
    "## Phase 4: ドキュメント同期(/update-doc --task)",
    "## Phase 4.5: 取り込みと品質ゲートの流し直し",
    "## Phase 5: PR 作成",
)


def phase_headings_in_order(text):
    """Phase 4・4.5・5 の見出しがこの順に 1 回ずつあるかを返す。"""
    positions = [text.find("\n" + heading + "\n") for heading in PHASE_HEADINGS]
    return all(position >= 0 for position in positions) and positions == sorted(positions) \
        and all(text.count("\n" + heading + "\n") == 1 for heading in PHASE_HEADINGS)


class ShipIntegrationContractTest(unittest.TestCase):
    def contains(self, text, *required):
        for value in required:
            with self.subTest(required=value):
                self.assertIn(value, text)

    def test_integration_reference_has_contract_terms(self):
        text = (SHIP / "references/integration.md").read_text()
        self.contains(
            text,
            "`merged`", "`up-to-date`", "`needs-review`", "`resolved`", "`committed`",
            "`conflict`", "`diff-changed`", "`multiple-merge-bases`", "`secret-overlap`",
            "binary_conflicts", "index_sha256", "PR の前の手順", "人が行う手順",
            "run-checks の上限:", "verify --mode pre-stage",
            "integrate-guard.py check", "integrate-guard.py worktree",
            "merge.conflictStyle=diff3", "rerere.enabled=false", "--cleanup=strip", "--text",
            "--exclude-ere", "取り込みを始められない", "implementation-git.md", "prepare", "モデル:",
            "取り込んだ後の profile", "取り込みの結果(`up-to-date` を含む)を問わず",
            "--allow-path", "--baseline", "段 3", "先頭の 100 件", "レビューを求める終了コード",
            "core.filemode=true", "作業ツリーの中身が HEAD と違う", "exclude_ere",
            "integrate-guard.py exclude", "元の ERE の列", "exclude_globs", "added_globs",
            "LFS の実体を取り出したリポジトリ")

    def test_integrate_guard_examples_have_required_args(self):
        found = {name: 0 for name in REQUIRED_ARGS}
        for relative in ("SKILL.md", "references/integration.md"):
            for literal in code_literals((SHIP / relative).read_text()):
                starts = list(CALL.finditer(literal))
                for index, match in enumerate(starts):
                    end = starts[index + 1].start() if index + 1 < len(starts) else len(literal)
                    call = literal[match.start():end]
                    found[match[1]] += 1
                    for arg in REQUIRED_ARGS[match[1]]:
                        with self.subTest(file=relative, subcommand=match[1], arg=arg):
                            self.assertRegex(call, re.escape(arg) + r"(?![\w-])")
        self.assertGreaterEqual(found["merge"], 1)
        self.assertGreaterEqual(found["check"], 1)

    def test_publish_check_without_p_uses_new_record(self):
        text = (SHIP / "references/integration.md").read_text()
        without_p = [line for line in text.splitlines() if "P が無く、手順 6 の状態の記録があれば:" in line]
        without_record = [line for line in text.splitlines()
                          if "Phase 4.5 の状態の記録(手順 6)も P も無ければ: 最後の commit の状態の記録で" in line]
        self.assertEqual(1, len(without_p))
        self.assertEqual(1, len(without_record))
        self.contains(without_p[0], "verify --mode pre-stage", "節の検査のコマンドだけを流したとき",
                      "新しい機密の glob だけが足されたとき")
        self.assertNotIn("--mode commit", without_p[0])
        self.contains(without_record[0], "verify --mode commit")

    def skill(self):
        return (SHIP / "SKILL.md").read_text()

    def test_phase_45_heading_is_between_phase_4_and_5(self):
        self.assertTrue(phase_headings_in_order(self.skill()))

    def test_phase_45_section_has_required_terms(self):
        body = section(self.skill(), "## Phase 4.5: 取り込みと品質ゲートの流し直し")
        self.contains(
            body, "[references/integration.md](references/integration.md)", "無人のタスクのイテレーション",
            "--discover", "--no-pr", "parent-child", "origin-repo.py",
            "python3 {ship-task の}scripts/integrate-guard.py merge", "rebase は使わない",
            "needs-review", "stage-resolved", "integrate-guard.py commit", "take --phase doc",
            "--allow-empty", "人が行う手順", "解決し直して",
            "取り込みの結果(`up-to-date` を含む)を問わず", "新しい機密の glob",
            "この枠の 4(`needs-review`)を通ったとき", "手順 8", "節の手順は行えない手順として扱う",
            "理由が `conflict` のときは", "どの理由でも `integrate-guard.py commit` で M を作る")

    def test_phase_5_names_the_published_sha(self):
        body = section(self.skill(), "## Phase 5: PR 作成")
        self.contains(body, "公開する SHA", "references/integration.md §3 の手順 1",
                      "Phase 4.5 の状態の記録も P も無いとき(最後の commit の状態の記録で。Phase 4.5 を行わなかったときを含む)", "references/integration.md §7 の照合を行い")

    def test_stop_table_has_integration_rows(self):
        body = section(self.skill(), "## 停止条件のまとめ")
        self.contains(body, "衝突を解けない", "人が行う手順", "機密の除外に当たるファイルを両方が変えた",
                      "事前検査で承認が要るもの", "節の確かめ(履歴を読む確かめ・P を前提にする確かめ)が落ちた",
                      "節が無いときの、PR を作らない・parent-child・`repo` が null")

    def test_review_protocol_mentions_phase_45(self):
        text = (ROOT / "plugins/dev-workflow/skills/do-task/references/review-protocol.md").read_text()
        body = subsection(text, "### 文書、保留、公開")
        self.contains(body, "Phase 4.5", "--allow-empty",
                      "Phase 4.5 の状態の記録も、取り込みの後の commit(P)も無いときは",
                      "最終 doc の `start` HEAD は検証済み実装 HEAD でなければならない",
                      "その記録では `pr-evidence` を取らない")
        self.assertLess(body.index("`pr-evidence` に同じ state"), body.index("Phase 4.5 の状態の記録があって P が無いときは"))

    def test_github_issue_mentions_phase_45(self):
        text = (ROOT / "plugins/dev-workflow/skills/do-task/references/github-issue.md").read_text()
        body = section(text, "## 6. ship-task の読み替え")
        self.contains(body, "Phase 4.5", "M か P が作業ブランチにあれば", "Phase 4.5 を行わないとき",
                      "取り込みの commit(M)", "取り込みの後のレビューの commit(P)", "実装 commit の検査の直後の出力(doc commit があるとき)")

    def test_unattended_table_has_s14_row(self):
        text = (SHIP / "references/unattended-mode.md").read_text()
        rows = [line for line in text.splitlines() if line.startswith("| S14 | ship-task Phase 4.5")]
        self.assertEqual(1, len(rows))
        self.assertIn("起きない", rows[0])

    def test_merge_direction_is_distinguished(self):
        text = self.skill()
        for heading in ("## 原則", "## 最終ゲート(完了報告前セルフチェック)"):
            with self.subTest(section=heading):
                self.assertIn("取り込み先(PR の base)への merge はしない", section(text, heading))

    def test_unattended_publish_command_is_single(self):
        literals = {literal for literal in code_literals(self.skill())
                    if literal.startswith("python3 {ship-task の}scripts/publish-guard.py ")}
        self.assertEqual(1, len(literals))

    def test_heading_order_check_detects_missing_or_moved_phase_45(self):
        p4, p45, p5 = PHASE_HEADINGS
        ordered = "\n" + "\n\n".join(f"{h}\n本文" for h in (p4, p45, p5)) + "\n"
        self.assertTrue(phase_headings_in_order(ordered))
        removed = ordered.replace("\n" + p45 + "\n", "\n")
        moved = "\n" + "\n\n".join(f"{h}\n本文" for h in (p4, p5, p45)) + "\n"
        self.assertFalse(phase_headings_in_order(removed))
        self.assertFalse(phase_headings_in_order(moved))
        self.assertFalse(phase_headings_in_order(self.skill().replace("\n" + p45 + "\n", "\n")))


if __name__ == "__main__":
    unittest.main()
