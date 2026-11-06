#!/usr/bin/env python3
"""AGENTS.md の「PR から merge までの順序」「PR の前の手順」の字面を固定するテスト(#365・#374)。

固定するもの:
  - 2 つの節の見出しと並び、「PR の前の手順」の 5 つの小見出し
  - 順序の 6 つの手順の並びと、配るプラグインだけ版を上げる決まり、git の安全用のオプション、送り方
  - PR の本文の「全回帰テスト(merge する状態)」の欄の形(6 本の名前・終了コード・結果の行)
  - 版を上げる 3 つのファイルの 4 か所の数
  - docs/design.md §8 の手順 2 が、2 つの節を正本として指すこと

3 つの確かめのコマンド(取り込み・版・記録)の字面と動きのテストは、#375 でこのファイルに足す。
節の切り出し(`section()`・`fences()`)は、#375 が使うので関数にしてある。
"""

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AGENTS = REPO / "AGENTS.md"
DESIGN = REPO / "docs" / "design.md"
PLUGIN_JSON = REPO / "plugins" / "dev-workflow" / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO / ".claude-plugin" / "marketplace.json"

H_VERIFY = "## 検証(実装終了時に必ず実行)"
H_ORDER = "## PR から merge までの順序"
H_BEFORE = "## PR の前の手順"
H_ISSUE = "## このリポジトリの Issue 運用"

FENCE_OPEN = re.compile(r"^([`~]{3,})")


def read(path):
    return path.read_text(encoding="utf-8")


def outside_fence_lines(text):
    """(行番号, 行, フェンスの外か) を返す。フェンスは行頭の ` か ~ が 3 個以上の行。"""
    result = []
    fence = None
    for number, line in enumerate(text.split("\n")):
        match = FENCE_OPEN.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                result.append((number, line, False))
                continue
            result.append((number, line, True))
        else:
            result.append((number, line, False))
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                    and line.strip() == match.group(1):
                fence = None
    return result


def fences(text):
    """フェンスごとの (開く行の文字列, 中の行のリスト) を順に返す。"""
    result = []
    fence = None
    body = []
    info = ""
    for line in text.split("\n"):
        match = FENCE_OPEN.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                info = line[len(fence):].strip()
                body = []
        elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                and line.strip() == match.group(1):
            result.append((info, body))
            fence = None
        else:
            body.append(line)
    return result


def h2_headings(text):
    return [line for _, line, outside in outside_fence_lines(text)
            if outside and line.startswith("## ")]


def section(text, heading):
    """H2 の節を返す。見出しの次の行から、フェンスの外の次の `## ` の行の前まで。"""
    lines = outside_fence_lines(text)
    start = None
    for index, (_, line, outside) in enumerate(lines):
        if start is None:
            if outside and line == heading:
                start = index + 1
        elif outside and line.startswith("## "):
            return "\n".join(line for _, line, _ in lines[start:index])
    if start is None:
        raise AssertionError(f"見出しが無い: {heading}")
    return "\n".join(line for _, line, _ in lines[start:])


def outside_lines(text):
    return [line for _, line, outside in outside_fence_lines(text) if outside]


class ProcedureTextTest(unittest.TestCase):
    # 節は使う所で切り出す(節が無い版では、その節を使うテストだけが落ちる)。
    @property
    def agents(self):
        return read(AGENTS)

    @property
    def order(self):
        return section(self.agents, H_ORDER)

    @property
    def before(self):
        return section(self.agents, H_BEFORE)

    @property
    def both(self):
        return self.order + "\n" + self.before

    @property
    def verify(self):
        return section(self.agents, H_VERIFY)

    def assertContainsAll(self, text, phrases):
        for phrase in phrases:
            self.assertIn(phrase, text, f"字面が無い: {phrase}")

    def test_sections_appear_once_between_verification_and_issue_rules(self):
        headings = h2_headings(self.agents)
        for heading in (H_VERIFY, H_ORDER, H_BEFORE, H_ISSUE):
            self.assertEqual(headings.count(heading), 1, heading)
        start = headings.index(H_VERIFY)
        self.assertEqual(headings[start:start + 4], [H_VERIFY, H_ORDER, H_BEFORE, H_ISSUE])
        h3 = [line for line in outside_lines(self.before) if line.startswith("### ")]
        self.assertEqual(h3, [
            "### 1. 取り込みを確かめる",
            "### 2. 版を上げて、レビューに通す",
            "### 3. 版を確かめる",
            "### 4. 全回帰テストを流す",
            "### 5. PR の本文の欄を書く",
        ])

    def test_order_has_six_steps_in_the_decided_order(self):
        numbered = [line for line in outside_lines(self.order) if re.match(r"^\d+\. ", line)]
        self.assertEqual([int(line.split(".", 1)[0]) for line in numbered], [1, 2, 3, 4, 5, 6])
        expected = [
            "文書の commit まで済ませる",
            "main を取り込む",
            "「PR の前の手順」の節を行う",
            "PR を開く",
            "merge の直前",
            "merge しない",
        ]
        for line, phrase in zip(numbered, expected):
            self.assertIn(phrase, line)
        both = self.both
        self.assertContainsAll(self.order, [
            "`## (次の版)`",
            "`verify --mode commit`",
            "`pr-evidence`",
            "すべて解いて取り込む",
            "作業の差分の全体をレビューし直す",
            "`publish-guard.py` の `--sha`",
            "`--no-pr`",
            "先に merge した PR が、その版を使う",
            "「review/commit 照合(PR の前の変更)」の欄は、回ごとに足す",
            "`\"version\"` の行の衝突は、main の側の値を取って解く",
            "§6 の、送る直前の `verify --mode commit` と `pr-evidence`(最後の commit の状態の記録で行う)は、PR の前の commit の状態の記録で行う",
            "`gh pr view <番号> -R <リポジトリ> --json headRefOid -q .headRefOid`",
            "PR の前に main を取り込む工程(Phase 4.5)を持つかで分ける",
            "持つ版は、`--no-pr` を付けずに走らせる",
            "全回帰テスト(人が行う手順)の前で、PR を開かずに止まる",
            "報告の値(公開する SHA・PR の本文の下書きのパス・保持した `pr-evidence`)",
            "持たない版は、`--no-pr` を付けて走らせる",
            # 配るプラグインだけ版を上げる決まり
            "配るプラグイン(`plugins/` の下・`.claude-plugin/marketplace.json`・プラグインを使わない導入の `setup.sh`)を変えた PR だけ",
            "変えない PR は、版も `CHANGELOG.md` も変えない",
            # 手で打つ git と送り方
            "Git の共通の安全用のオプション",
            "`git <安全用のオプション> merge --no-ff",
            "`gh pr merge <番号> -R <リポジトリ> --merge --match-head-commit <全回帰テストを流した commit の SHA>`",
            "--push-only --push-url-digest <origin-repo.py の push_url_sha256>",
            # やり直しの送る前の確かめ
            "4 と同じ確かめをする: Issue 方式の契約 §6 の確かめ",
            "5 の記録の確かめ ④ を流し直し",
            "その状態の記録で `verify --mode commit` と `pr-evidence` をやり直す",
            # 2 回目のレビューで足した字面
            "`git <安全用のオプション> diff --name-only --diff-filter=U`",
            "`git <安全用のオプション> merge-base --is-ancestor origin/main HEAD`",
            "この 2 つの節で打つ git のコマンドには、すべて Git の共通の安全用のオプション",
            "文書の変更が無く、文書の commit を作らなかったときは、実装の commit",
            "文書の commit を作ったときは、main を取り込む前に",
            "ship-task の Phase 4.5 を行った回(状態の記録〈`integration.md` の手順 6〉がある回)は、送る直前の照合を、P があってもなくても `integration.md` §7 のとおりに行う",
            "古い記録の `verify --mode commit` と `pr-evidence` はしない",
            "`integrate-guard.py check`(`--sha=<M> --reviewed-sha=<D> --base-sha=<B> --exclude-ere=<今の列>`)が終了コード 0",
            "P があれば、手順 6 の記録で `verify --mode commit`",
            "`integrate-guard.py worktree` と、その記録の `verify --mode pre-stage`",
            "Phase 4.5 を行わなかった回は、次のとおり",
            "作らなかったら、4 の分かれ道のとおりに代える",
            "先頭がレビュー済み SHA(1 回目)なら、その状態の記録で `verify --mode commit` と `pr-evidence` を行う",
            "先頭が取り込みの commit なら、照合は通らない",
            # 3 回目のレビューで足した字面
            "review-guard の状態の記録を取らずに作った PR(ship-task も do-task も使わずに作った PR。例: #339・#342・#363・#369)では",
            "状態の記録を失った PR には当てない",
            "順序の 4 と 6 の `verify --mode commit` と `pr-evidence`)を行わない",
            "`<レビュー済み SHA>`(1 回目)と記録の確かめの `first` には、取り込む前の作業ブランチの先頭を渡す",
            "ふつうのレビューは、`diff-snapshot.sh` の `--base HEAD` と `--base origin/main` のスナップショットをレビュアーに渡し、全員の判定を APPROVED にすること",
            "「状態の記録なし」と書き、並べてレビュアーのモデルの名前と版と判定を書く",
            "PR の前の commit として記録の確かめの `ps` に入れる",
            "全回帰テストを流した commit(記録の確かめ ④ の `last`)と同じ",
            "「PR の前の手順」の 1 の `同じ:` の行と、その先頭を `last` に渡して合格した記録の確かめ ④ の行で代え",
            # 4 回目のレビューで足した字面
            "`--exclude-glob` で 1 つずつ渡す",
        ])
        for phrase in ("git merge --abort", "持つときは", "PR を開くため",
                       "git push --dry-run", "`publish-guard.py` と同じ付け方"):
            self.assertNotIn(phrase, both, f"入れてはいけない字面: {phrase}")

    def test_result_lines_name_the_full_regression_commands_in_order(self):
        names = self.regression_command_names()
        self.assertEqual(len(names), 6)
        text_fences = [body for info, body in fences(self.before)
                       if info == "text" and any(line.startswith("- 流した commit:") for line in body)]
        self.assertEqual(len(text_fences), 1)
        body = text_fences[0]
        self.assertEqual(len(body), 9)
        self.assertEqual(body[0], "- 流した commit: `<SHA>`(取り込んだ main: `<SHA>`・版 <X.Y.Z>)")
        self.assertEqual(body[1], "- 流した環境: <`git --version` の出力> / <`python3 --version` の出力>")
        self.assertEqual(body[-1], "- 流し直し: なし")
        written = []
        for line in body:
            match = re.match(r"^- `([^`]+)`: 終了コード <数> / <最後の `結果:` の行>$", line)
            if match:
                written.append(match.group(1))
        self.assertEqual(written, names)
        both = self.both
        self.assertContainsAll(self.before, [
            "見出し `## 全回帰テスト(merge する状態)` の欄",
            "見出し `## レビュー差分の外で PR に入る commit` の欄",
            "見出し `## review/commit 照合(PR の前の変更・<回> 回目)` の欄",
            "字面を変えずに書く",
            "件数はこの行にあり、省かない",
            "`- 流し直し <回>: ",
            "人が流す(人が行う手順。ship-task は流さない)",
            "取り込んだ後の作業ツリーにあるこの節",
            "1 本ごとの上限は 3600 秒",
            "上限は既定の 600 秒で足りる",
            "「最終 doc の `start` HEAD は検証済み実装 HEAD でなければならない」は、この 2 では、取り込みの commit",
            "1 の最後の行が `不合格 ⓪:`(1 の続け方の 2 つの行は除く)でなく(取り込みで衝突したとき・`違う:` のとき・その 2 つの行のときは、2 で作業の差分の全体をレビューし直した後)、3 の最後の行が `合格:` で、6 本が終了コード 0 で、5 の記録の確かめ ④ の最後の行が `合格:` のときだけ、PR を開く",
            "2 の commit があるときだけ",
            "reset せずに人に知らせる",
            "節を消す変更を PR の前の commit として通し、記録の確かめ ④ の `ps` に入れる",
            "前の回に 2 を行わなかったときは、前の回のレビュー済み SHA をそのまま使う",
            "初回の値から変えない",
            "`<3 で確かめた先頭の SHA>` には、最後の回の 3 の結果の行の「先頭」の SHA を渡す",
            "状態の記録を取らずに作った PR は、取り込む前の作業ブランチの先頭",
            "`終了コード <数> / 結果の行なし(<理由>)`",
            "(取り込んだときは取り込みの commit、取り込まなかったときはレビュー済み SHA)",
            "- レビュー済み SHA(1 回目): `<SHA>`",
            "2 の commit を作った回だけ書く",
            "文書の commit を作ったときだけ書く",
            "`git log --no-show-signature …` の出力。取り込みの commit も含む",
            "`bash \"$d/plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh\" --cwd . --base HEAD --out",
            "取り込み先の main の版を一時フォルダに取り出して流す",
            "archive origin/main plugins/dev-workflow/skills | tar -x -C",
            "`python3 -B \"$d/plugins/dev-workflow/skills/ship-task/scripts/review-guard.py\"`",
            "1 の続け方(読めない profile で止まったとき)と同じ作り方の正規表現を使う",
            "最後の行が `不合格 ⓪: 機密のパス(上の行)に変更がある。中身を読まずに止めた` のときは、① の代わりに、2 で作業の差分の全体をレビューし直す",
            "版の行がレビュー済み SHA の値と同じかは、1 の ① が確かめる",
            "PR を開かず、人に知らせる(最後の行が、下の続け方の 2 つの行のどちらかと全体で同じときを除く)",
            "ほかの `不合格 ⓪:`(`不合格 ⓪: 機密のパスの正規表現を作れない` を含む)は、PR を開かず、人に知らせる",
            "`git <安全用のオプション> rev-list --first-parent --parents HEAD ^origin/main` が出す SHA",
            "取り込みの commit(取り込まなかったときは、レビュー済み SHA の commit。つまり作業ブランチの先頭)を渡す",
            "1 の出力の `機密の除外: ` の行の正規表現を、もう 1 つの `--exclude-ere` として足す",
            "共通の祖先の設定にだけ残る機密の指定も、やり直しの回で外れない",
            "最後の行が `不合格 ⓪: 機密のパスの集合を作れない`(作業ブランチの途中の commit の profile が読めない)ときも同じく進める",
            "`.claude-plugin/` の中のほかのファイルだけを変えた PR は、配るプラグインを変えていない",
            "ただし、次の場合を除く",
            "版の 2 つのファイルと `CHANGELOG.md` の種類と実行権限が main と同じこと",
            "取り込みの 2 つ目の親の側にある commit も数える",
            "`take --phase doc`",
            "`git <安全用のオプション> commit --allow-empty",
            "同じコマンドの `--base origin/main`",
            "`git diff` は、追跡していない新しいファイルを出さず",
            "`seal` → `verify --mode pre-stage` → レビュー",
            "§6(review/commit の全入口)のとおり、Issue 本文を取り直して R と比べてから",
            "`LOOP_SELFTEST_TIMEOUT=6000 RUN_TIMEOUT=600 timeout 3600 python3 -B scripts/run-loop-selftest.py`",
            "頭に `P ` を付ける",
            "配るプラグインを変えない PR: ② 版が main と同じで、③ `CHANGELOG.md` が main と同じ",
            "「検証」の節の時間の表",
            "`timeout 3600`",
        ])
        # 3 の箇条の並び
        position = -1
        for phrase in ("  - ③ 版が main の版の次で",
                       "- 配るプラグインを変えない PR: ②",
                       "- どちらの PR でも、版の 2 つのファイルと `CHANGELOG.md`",
                       "ただし、次の場合を除く",
                       "  - 配るものを変えない PR が `CHANGELOG.md` に節を足していたとき(③)"):
            found = self.before.find(phrase, position + 1)
            self.assertGreater(found, position, f"3 の箇条の並びが違う: {phrase}")
            position = found
        for phrase in ("run-checks の上限", "--first-parent --merges", "git diff --binary HEAD",
                       "`git diff --binary origin/main`", "- 文書の commit: "):
            self.assertNotIn(phrase, both, f"入れてはいけない字面: {phrase}")

    def regression_command_names(self):
        """「検証」の節の手順 2 から、6 本の名前を行の順に集める。"""
        verify = self.verify
        marker = "全回帰テスト(merge の前)は、次の手順で流す。"
        self.assertIn(marker, verify)
        lines = verify.split(marker, 1)[1].split("\n")
        start = next(i for i, line in enumerate(lines) if re.match(r"^2\. 次の (\d+) つを同時に始める。", line))
        count = int(re.match(r"^2\. 次の (\d+) つを同時に始める。", lines[start]).group(1))
        names = []
        for line in lines[start + 1:]:
            if not line.startswith("   - "):
                break
            py = re.search(r"python3 -B scripts/([\w-]+\.py)", line)
            if py:
                names.append(py.group(1))
                continue
            selftest = re.match(r"^   - ([\w・-]+) の selftest\(", line)
            if selftest:
                for base in selftest.group(1).split("・"):
                    names.append(f"{base}-selftest.sh")
                    self.assertIn(
                        f"bash plugins/dev-workflow/skills/do-task/scripts/{base}-selftest.sh", verify)
        self.assertEqual(len(names), count)
        return names

    def test_version_places_match_the_files(self):
        plugin = [line for line in read(PLUGIN_JSON).split("\n") if re.match(r'^\s*"version": "', line)]
        market = [line for line in read(MARKETPLACE_JSON).split("\n") if re.match(r'^\s*"version": "', line)]
        self.assertEqual(len(plugin), 1)
        self.assertEqual(len(market), 2)
        self.assertContainsAll(self.before, [
            "`plugins/dev-workflow/.claude-plugin/plugin.json` の `\"version\"`(1 か所)",
            "`.claude-plugin/marketplace.json` の `\"version\"`(2 か所)",
            "main の版から新しい版への 3 行(`plugin.json` 1 行・`marketplace.json` 2 行)",
        ])

    def test_design_section_8_points_to_the_sections(self):
        design = read(DESIGN)
        heading = "## 8. このリポジトリへの還元フロー"
        self.assertEqual(design.count("\n" + heading + "\n"), 1)
        part = design.split(heading + "\n", 1)[1].split("\n### ", 1)[0]
        self.assertContainsAll(part, [
            "[AGENTS.md](../AGENTS.md) の「PR から merge までの順序」「PR の前の手順」の節が正本",
            "上げる(配るプラグインを変えた PR だけ)",
        ])
        position = -1
        for phrase in ("main を取り込む", "全回帰テスト", "PR を開く"):
            found = part.find(phrase, position + 1)
            self.assertGreater(found, position, phrase)
            position = found


if __name__ == "__main__":
    unittest.main()
