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


if __name__ == "__main__":
    unittest.main()
