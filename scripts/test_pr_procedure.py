#!/usr/bin/env python3
"""AGENTS.md の「PR から merge までの順序」「PR の前の手順」の字面を固定するテスト(#365・#374)。

固定するもの:
  - 2 つの節の見出しと並び、「PR の前の手順」の 5 つの小見出し
  - 順序の 6 つの手順の並びと、配るプラグインだけ版を上げる決まり、git の安全用のオプション、送り方
  - PR の本文の「全回帰テスト(merge する状態)」の欄の形(1 つのコマンド `run-all.py`・終了コード・結果の行・内訳の行)
  - 版を上げる 3 つのファイルの 4 か所の数
  - docs/design.md §8 の手順 2 が、2 つの節を正本として指すこと

3 つの確かめのコマンド(bash のコードブロック)を、使い捨てのリポジトリで実際に流し、結果を照らす。
節の切り出し(`section()`・`fences()`)は、3 つの確かめのコマンドの取り出しも使う。
"""

import os
import re
import subprocess
import tempfile
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

PLUGIN = "plugins/dev-workflow/.claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"
SRC = "plugins/dev-workflow/skills/demo/src.txt"   # 配るプラグインの下のファイル
ICON = "plugins/dev-workflow/skills/demo/icon.bin"
TOOL = "scripts/tool.py"   # 配るプラグインの外のファイル
SNAPSHOT = "plugins/dev-workflow/skills/do-task/scripts/diff-snapshot.sh"   # 確かめのコマンドが --precheck で使う
PROFILES = "plugins/dev-workflow/skills/do-task/scripts/secret-profiles.py"   # 取り込みの確かめが、機密のパスの集合を作るのに使う
MARK = "marker-written"   # 作業ツリーのスクリプトが動いたら書かれる印
MERGE_HEAD_LINE = "reviewed=<レビュー済み SHA> bash -s <<'EOF'"
VERSION_HEAD_LINE = "checked=<1 で確かめた先頭の SHA> bash -s <<'EOF'"
RECORD_HEAD_LINE = "first=<レビュー済み SHA(1 回目)> ps='<PR の前の commit の SHA>' last=<3 で確かめた先頭の SHA> bash -s <<'EOF'"

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


def scripts():
    """「PR の前の手順」の 3 つの bash のコードブロックの中身(取り込みの確かめ・版の確かめ・記録の確かめ)。"""
    blocks = [body for info, body in fences(section(read(AGENTS), H_BEFORE)) if info == "bash"]
    assert len(blocks) == 3, "「PR の前の手順」の節の bash のコードブロックが 3 つでない"
    assert [b[0] for b in blocks] == [MERGE_HEAD_LINE, VERSION_HEAD_LINE, RECORD_HEAD_LINE], [b[0] for b in blocks]
    assert all(b[-1] == "EOF" for b in blocks)
    return ["\n".join(b[1:-1]) + "\n" for b in blocks]


def review_input_commands():
    """「PR の前の手順」の 2 の、diff-snapshot.sh を取り出すコマンドと、版の変更のスナップショットのコマンド(文の中の `…`)。"""
    before = section(read(AGENTS), H_BEFORE)
    two = before.split("### 2. 版を上げて、レビューに通す", 1)[1].split("### 3. 版を確かめる", 1)[0]
    codes = re.findall(r"`([^`]+)`", two)
    fetch = [c for c in codes if "archive origin/main plugins/dev-workflow/skills" in c]
    snap = [c for c in codes if re.search(r'diff-snapshot\.sh"? --cwd \. --base HEAD ', c)]
    assert len(fetch) == 1 and len(snap) == 1, (fetch, snap)
    return fetch[0], snap[0]


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

    def test_result_lines_name_the_full_regression_command(self):
        self.check_regression_commands()
        text_fences = [body for info, body in fences(self.before)
                       if info == "text" and any(line.startswith("- 流した commit:") for line in body)]
        self.assertEqual(len(text_fences), 1)
        body = text_fences[0]
        self.assertEqual(len(body), 5)
        self.assertEqual(body[0], "- 流した commit: `<SHA>`(取り込んだ main: `<SHA>`・版 <X.Y.Z>)")
        self.assertEqual(body[1], "- 流した環境: <`git --version` の出力> / <`python3 --version` の出力>")
        self.assertEqual(body[2], "- `run-all.py`: 終了コード <数> / <最後の `結果:` の行>")
        self.assertEqual(body[3], "- 内訳: <`結果(` で始まる行>")
        self.assertEqual(body[4], "- 流し直し: なし")
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
            "上限は 3600 秒(入口の `--timeout` の既定)",
            "上限は既定の 600 秒で足りる",
            "「最終 doc の `start` HEAD は検証済み実装 HEAD でなければならない」は、この 2 では、取り込みの commit",
            "1 の最後の行が `不合格 ⓪:`(1 の続け方の 2 つの行は除く)でなく(取り込みで衝突したとき・`違う:` のとき・その 2 つの行のときは、2 で作業の差分の全体をレビューし直した後)、3 の最後の行が `合格:` で、`run-all.py` が終了コード 0 で、5 の記録の確かめ ④ の最後の行が `合格:` のときだけ、PR を開く",
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
            "`bash scripts/run-all-container.sh --commit <3 で確かめた先頭の SHA>`",
            "頭に `P ` を付ける",
            "配るプラグインを変えない PR: ② 版が main と同じで、③ `CHANGELOG.md` が main と同じ",
            "`docs/testing.md` の時間の記録",
            "全回帰テスト(「検証」の節の `run-all.py`)を、人が流す",
            "`run-all.py` は、止められると最後の `結果:` の行を出さない",
            "「内訳」は、`run-all.py` が出す `結果(` で始まる 6 行を、出た順に 1 行ずつ `- 内訳: ` を付けて、字面を変えずに書く",
        ])
        # #375 で足した字面(3 つの確かめのコマンドの説明)。1 つずつ数える
        for phrase in (
            "`\"version\"` の行を消してから比べる",
            "`--no-ext-diff --no-textconv --no-color --no-renames -U3`",
            "ファイルの種類と実行権限も、中身とは別に比べる",
            "空どうしを比べずに `不合格 ⓪:` で止まる",
            "`GIT_NO_LAZY_FETCH=1`",
            "`diff-snapshot.sh --cwd . --precheck`",
            "git の物を読まずに設定で確かめる",
            "作業ツリーの `diff-snapshot.sh` は、そのまま流さない",
            "まだ確かめていない commit(`reviewed`・`checked`・`first`)のものも流さない",
            "中身を読まずにパスだけを並べて `不合格 ⓪:` で止まる",
            "`secret-profiles.py` と `diff-snapshot.sh --print-exclude-ere`",
            "版の 2 つのファイルと `CHANGELOG.md` は、ファイルの種類と実行権限も",
            "取り込みの commit に版の変更を混ぜると、「違う」になる",
            "レビュー済み SHA の後の取り込みを含む",
            "使った正規表現は `機密の除外: ` の行に出す",
            "やり直しの回も、前の回までの指定が集合から消えない(人が控える物は無い)",
            "その親すべて〈作業ブランチが main から分かれた所と、過去に取り込んだ main〉",
            "main が版を変えていないときは、自分の節の見出しも、レビュー済み SHA と同じかを見る",
            "全体が main と同じことを確かめる",
            "`@@` の行は `@@` だけにして、行番号と関数の見出しは比べない",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.before)
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

    def check_regression_commands(self):
        """「検証」の節に、全回帰テストの 2 つのコマンドと、詳しい説明へのリンクがあることを確かめる。"""
        verify = self.verify
        for command in ("python3 -B scripts/run-all.py", "bash scripts/run-all-container.sh"):
            self.assertIn(command, verify)
        self.assertIn("[docs/testing.md](docs/testing.md)", verify)
        self.assertIn("最後の行が `結果: 合格 / <秒> 秒` で、終了コードが 0", verify)
        # 時間の表は、docs/testing.md へ移した(AGENTS.md には置かない)。
        self.assertNotIn("| 流し方 |", verify)

    def test_scripts_stop_lazy_fetch_before_reading_objects(self):
        """3 つのスクリプトのそれぞれで、GIT_NO_LAZY_FETCH と promisor の確かめが、git の物を読むより前にある。"""
        for name, body in zip(("取り込み", "版", "記録"), scripts()):
            with self.subTest(script=name):
                lines = body.split("\n")
                exports = [i for i, l in enumerate(lines) if l == "export GIT_NO_LAZY_FETCH=1"]
                self.assertEqual(1, len(exports))
                calls = [i for i, l in enumerate(lines)
                         if not l.startswith("g() {") and re.search(r"(^|[\s;(|&!{])g [a-z-]", l)]
                pre = [i for i, l in enumerate(lines) if "--cwd . --precheck" in l]
                fetch = [i for i, l in enumerate(lines) if "cat-file blob" in l and "/diff-snapshot.sh\"" in l]
                self.assertEqual(1, len(pre))
                self.assertEqual(1, len(fetch))
                self.assertLess(exports[0], calls[0])
                self.assertLess(exports[0], pre[0])
                self.assertIn("config --get-regexp '^(extensions\\.partialclone|remote\\..*\\.promisor)$'", lines[calls[0]])
                self.assertLess(calls[0], fetch[0])
                self.assertLess(fetch[0], pre[0])
                self.assertTrue(lines[pre[0]].startswith('bash "$t/ds.sh" --cwd . --precheck'), lines[pre[0]])

    def test_secret_stop_lines_match_the_command(self):
        """続けてよい 2 つの止まり方は、1 のコマンドが出す行と全体で同じ字面(正規表現を作れないときは続けない)。"""
        merge = scripts()[0]
        for line in ("⓪: 機密のパス(上の行)に変更がある。中身を読まずに止めた", "⓪: 機密のパスの集合を作れない"):
            with self.subTest(line=line):
                self.assertIn('fail "' + line + '"', merge)
                self.assertIn("`不合格 " + line + "`", self.before)
        self.assertIn('fail "⓪: 機密のパスの正規表現を作れない"', merge)

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


class ScratchRepo(unittest.TestCase):
    """使い捨てのリポジトリで、3 つの確かめのコマンドを流す準備。test_ で始まるメソッドを持たない。"""

    @classmethod
    def setUpClass(cls):
        cls.merge_script, cls.version_script, cls.record_script = scripts()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wf-pr-procedure-")
        self.repo = Path(os.path.realpath(self.tmp.name)) / "repo"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        scratch_tmp = self.repo.parent / "tmp"   # コマンドの mktemp -d がここに作り、tearDown で一緒に消える
        scratch_tmp.mkdir()
        self.env.update(HOME=str(self.repo.parent), GIT_CONFIG_NOSYSTEM="1", LC_ALL="C.UTF-8", TMPDIR=str(scratch_tmp))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "tester")
        self.git("config", "user.email", "tester@example.invalid")
        self.write(PLUGIN, '{\n\t"name": "dev-workflow",\n\t"version": "1.0.0",\n\t"displayName": "Dev Workflow",\n'
                           '\t"homepage": "h",\n\t"license": "MIT",\n\t"description": "d"\n}\n')
        self.write(MARKET, '{\n\t"metadata": {\n\t\t"version": "1.0.0"\n\t},\n\t"plugins": [\n\t\t{\n'
                           '\t\t\t"name": "dev-workflow",\n\t\t\t"version": "1.0.0",\n\t\t\t"author": "a"\n\t\t}\n\t]\n}\n')
        self.write("CHANGELOG.md", "# Changelog\n\nv4.2.0 以前は commit 履歴を参照。\n\n## v1.0.0\n\n### 変更\n\n- 最初\n")
        self.write(SRC, "".join(f"line {i}\n" for i in range(1, 11)))
        self.write(TOOL, "print('tool')\n")
        self.write("setup.sh", "#!/bin/sh\necho setup\n")
        self.write(SNAPSHOT, (REPO / SNAPSHOT).read_text(encoding="utf-8"))
        self.write(PROFILES, (REPO / PROFILES).read_text(encoding="utf-8"))
        self.commit("base")
        self.publish_main()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, check=True):
        return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], cwd=self.repo,
                              env=self.env, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read(self, path):
        return (self.repo / path).read_text(encoding="utf-8")

    def replace(self, path, old, new, count=1):
        text = self.read(path)
        self.assertEqual(count, text.count(old), (path, old))
        self.write(path, text.replace(old, new))

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def drop_object(self, rev_path):
        """git の物の 1 つ(緩い物)を消して、読めなくする。"""
        oid = self.git("rev-parse", rev_path).stdout.strip()
        obj = Path(self.git("rev-parse", "--git-path", f"objects/{oid[:2]}/{oid[2:]}").stdout.strip())
        obj = obj if obj.is_absolute() else self.repo / obj
        obj.chmod(0o644)
        obj.unlink()

    def plant(self, mark):
        """作業ツリーの diff-snapshot.sh の 2 行目に、動いたら印のファイルを書く行を入れる(commit するかは呼ぶ側が決める)。"""
        self.write(SNAPSHOT, self.read(SNAPSHOT).replace("\n", f"\necho x > '{mark}'\n", 1))

    def publish_main(self):
        self.git("update-ref", "refs/remotes/origin/main", "refs/heads/main")

    def set_version(self, old, new):
        self.replace(PLUGIN, f'"version": "{old}"', f'"version": "{new}"')
        self.replace(MARKET, f'"version": "{old}"', f'"version": "{new}"', count=2)

    def block(self, heading, item):
        return f"## {heading}\n\n### 変更\n\n- {item}\n\n"

    def work(self, touch_plugin=False):
        """作業ブランチで、レビューする変更(実装と文書)を作る。文書の commit の SHA を返す。"""
        self.git("switch", "-q", "-c", "task/x")
        self.replace(SRC, "line 8\n", "line 8 changed\n")
        (self.repo / ICON).write_bytes(b"\x00\x01reviewed\x00")
        if touch_plugin:
            self.replace(PLUGIN, '"description": "d"', '"description": "work"')
        self.commit("impl")
        self.replace("CHANGELOG.md", "## v1.0.0\n", self.block("(次の版)", "この PR") + "## v1.0.0\n")
        self.checked = self.commit("docs")
        return self.checked

    def work_tool(self):
        """配るプラグインを変えない PR(scripts/ だけ。CHANGELOG.md も版も変えない)。文書の commit に当たる SHA を返す。"""
        self.git("switch", "-q", "-c", "task/x")
        self.replace(TOOL, "print('tool')\n", "print('tool changed')\n")
        self.checked = self.commit("test only")
        return self.checked

    def main_moves(self, old, new, top, item):
        """別の PR が main に merge された形を作る(版を上げ、CHANGELOG に節を足す)。"""
        self.git("switch", "-q", "main")
        if old == "1.0.0":
            self.replace(SRC, "line 1\n", "line 0\nline 1\n")
        else:
            self.write(f"other-{new}.txt", "x\n")
        self.replace("CHANGELOG.md", f"## v{top}\n", self.block(f"v{new}", item) + f"## v{top}\n")
        self.set_version(old, new)
        self.commit(f"main {new}")
        self.publish_main()
        self.git("switch", "-q", "task/x")

    def merge(self, mine, theirs_top, extra=None):
        """main を取り込む。CHANGELOG.md は「自分の節を上に」、版のファイルは main の側の値で衝突を解く(この土台では、版のファイルで衝突するのは版の行だけ)。衝突したファイルを返す。"""
        done = self.git("merge", "-q", "--no-ff", "-m", "merge main", "origin/main", check=False)
        conflicts = self.git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if done.returncode:
            for path in (PLUGIN, MARKET):
                if path in conflicts:
                    self.git("checkout", "--theirs", "--", path)
            changelog = self.git("show", "origin/main:CHANGELOG.md").stdout
            self.write("CHANGELOG.md", changelog.replace(f"## v{theirs_top}\n", mine + f"## v{theirs_top}\n", 1))
        if extra:
            extra()
        self.git("add", "-A")
        if done.returncode:
            self.git("commit", "-q", "--no-edit")
        elif extra:
            self.git("commit", "-q", "--amend", "--no-edit")   # 衝突しなかった取り込みの commit に、手を加える
        self.assertEqual("", self.git("status", "--porcelain").stdout)
        self.checked = self.git("rev-parse", "HEAD").stdout.strip()   # 取り込みの確かめで「先頭」に出る SHA
        return conflicts

    def past_our_version(self):
        """1.0.2 を付けた後に、main が 2 つの PR で 1.0.3 まで進み、版の行も衝突する取り込み。"""
        reviewed = self.normal()
        verified = self.bump("1.0.1", "1.0.2")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "先に merge した PR")
        self.main_moves("1.0.2", "1.0.3", "1.0.2", "その次に merge した PR")
        conflicts = self.merge(self.block("v1.0.2", "この PR"), "1.0.3")
        self.assertEqual(sorted([PLUGIN, MARKET, "CHANGELOG.md"]), sorted(conflicts))
        self.assertIn('"version": "1.0.3"', self.read(PLUGIN))
        return reviewed, verified

    def bump(self, old, new, heading_from="## (次の版)\n", heading=True):
        """版の 3 か所と、先頭にある自分の節の見出しを変える(PR の前の手順の 2 の commit)。"""
        self.set_version(old, new)
        if heading:
            text = self.read("CHANGELOG.md")
            self.assertEqual(text.index(heading_from), text.index("\n## ") + 1)
            self.write("CHANGELOG.md", text.replace(heading_from, f"## v{new}\n", 1))
        return self.commit(f"chore: 版を {new} にする")

    def run_script(self, script, reviewed=None, checked=None, first=None, ps=None, last=None):
        env = dict(self.env)
        if last is not None:
            env["last"] = last
        if first is not None:
            env["first"] = first
        if ps is not None:
            env["ps"] = ps
        if reviewed is not None:
            env["reviewed"] = reviewed
        if checked is not None:
            env["checked"] = checked
        done = subprocess.run(["bash", "-s"], input=script, cwd=self.repo, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return done.returncode, done.stdout, done.stderr

    def normal(self, **kwargs):
        reviewed = self.work(touch_plugin=kwargs.get("touch_plugin", False))
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge(self.block("(次の版)", "この PR"), "1.0.1", extra=kwargs.get("extra"))
        return reviewed

    def assert_merge(self, reviewed, rc, head):
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(rc, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith(head), out + err)

    def assert_version(self, rc, head, checked=None):
        code, out, err = self.run_script(self.version_script, checked=checked or self.checked)
        self.assertEqual(rc, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith(head), out + err)
        return out


class MergeCheckTest(ScratchRepo):
    """「PR の前の手順」の 1(取り込みの確かめ)。"""

    def test_clean_resolution_is_same(self):
        reviewed = self.normal()
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(0, code, out + err)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        main = self.git("rev-parse", "origin/main").stdout.strip()
        self.assertEqual(f"同じ: 取り込みの後も、レビューした作業の差分は同じ(レビュー済み {reviewed}・先頭 {head}・main {main})",
                         out.splitlines()[-1])

    def test_reviewed_merge_commit_fails_0(self):
        self.normal()
        merged = self.git("rev-parse", "HEAD").stdout.strip()
        self.assert_merge(merged, 1, "不合格 ⓪:")

    def test_nonplugin_pr_is_same(self):
        reviewed = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.assertEqual([], self.merge("", "1.0.1"))
        self.assert_merge(reviewed, 0, "同じ: ")

    def test_nonplugin_pr_changelog_changed_in_merge_is_different(self):
        reviewed = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1", extra=lambda: self.replace("CHANGELOG.md", "- ほかの PR\n", "- ほかの PR(書き換えた)\n"))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_no_merge_needed_is_same(self):
        reviewed = self.work()
        self.assert_merge(reviewed, 0, "同じ: ")

    def test_redo_after_another_pr_used_the_same_version_is_same(self):
        self.normal()
        verified = self.bump("1.0.1", "1.0.2")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "先に merge した PR")
        self.merge(self.block("v1.0.2", "この PR"), "1.0.2")
        self.assert_merge(verified, 0, "同じ: ")

    def test_redo_compared_with_the_document_commit_is_same(self):
        reviewed = self.normal()
        self.bump("1.0.1", "1.0.2")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "先に merge した PR")
        self.merge(self.block("v1.0.2", "この PR"), "1.0.2")
        self.assert_merge(reviewed, 0, "同じ: ")

    def test_changing_own_changelog_section_in_the_merge_is_different(self):
        reviewed = self.normal(extra=lambda: self.replace("CHANGELOG.md", "- この PR\n", "- この PR(書き換えた)\n"))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_redo_after_main_moved_past_our_version_is_same(self):
        reviewed, verified = self.past_our_version()
        self.assert_merge(verified, 0, "同じ: ")
        self.assert_merge(reviewed, 0, "同じ: ")

    def test_line_moved_in_the_merge_is_different(self):
        def move():
            self.replace(SRC, "line 8 changed\n", "")
            self.write(SRC, self.read(SRC) + "line 8 changed\n")
        self.assert_merge(self.normal(extra=move), 2, "違う: ")

    def test_main_change_next_to_the_work_is_different(self):
        """main が、作業の行から 2 行上(前後 3 行の文脈の中)を変えると、衝突しなくても「違う」(安全な側)。"""
        reviewed = self.work()
        self.git("switch", "-q", "main")
        self.replace(SRC, "line 6\n", "line 6 main\n")
        self.commit("main changes line 6")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.assertEqual([], self.merge(self.block("(次の版)", "この PR"), "1.0.0"))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_work_changing_description_is_same(self):
        self.assert_merge(self.normal(touch_plugin=True), 0, "同じ: ")

    def test_binary_change_hidden_by_textconv_is_different(self):
        """2 進のファイルを大きさだけのテキストに変換する設定があっても、同じ大きさの書き換えを見逃さない。"""
        info = Path(self.git("rev-parse", "--git-path", "info").stdout.strip())
        info = info if info.is_absolute() else self.repo / info
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("*.bin diff=size\n", encoding="utf-8")
        self.git("config", "diff.size.textconv", "wc -c <")
        reviewed = self.normal(extra=lambda: (self.repo / ICON).write_bytes(b"\x00\x01sneaked!\x00"))
        # そのままでは、diff-snapshot.sh --precheck がテキストへの変換の設定を見つけて止める。
        self.assert_merge(reviewed, 1, "不合格 ⓪: diff-snapshot.sh --precheck が止めた")
        # --precheck を外しても、--no-textconv で見逃さないこと(守りの 2 段目)を確かめる。
        lines = self.merge_script.split("\n")
        pre = [l for l in lines if l.startswith('bash "$t/ds.sh" --cwd . --precheck') or l.startswith('[ ! -s "$t/pre" ]')]
        self.assertEqual(2, len(pre), pre)
        code, out, err = self.run_script("\n".join(l for l in lines if l not in pre), reviewed)
        self.assertEqual(2, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("違う: "), out + err)

    def test_replace_ref_does_not_hide_change_is_different(self):
        """取り込みの commit を、変更を隠さない commit で置き換える参照(git replace)を置いても、実際の変更を見逃さない。"""
        reviewed = self.normal()
        clean = self.git("rev-parse", "HEAD").stdout.strip()
        self.replace(SRC, "line 3\n", "line 3 sneaked\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "--no-edit")
        bad = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("replace", bad, clean)
        self.assertNotIn("sneaked", self.git("show", "HEAD:" + SRC).stdout)   # 置き換えの参照が効いている
        self.assert_merge(reviewed, 2, "違う: ")

    def test_version_file_mode_change_in_merge_is_different(self):
        reviewed = self.normal(extra=lambda: (self.repo / PLUGIN).chmod(0o755))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_changelog_mode_change_in_merge_is_different(self):
        reviewed = self.normal(extra=lambda: (self.repo / "CHANGELOG.md").chmod(0o755))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_missing_object_with_promisor_does_not_start_uploadpack_fails_0(self):
        """欠けた物を自動で取りに行く構成でも、uploadpack のコマンドを動かさずに止まる。"""
        reviewed = self.normal()
        trace = self.repo.parent / "uploadpack-trace"
        stub = self.repo.parent / "uploadpack.sh"
        stub.write_text(f"#!/bin/sh\necho \"uploadpack $*\" >> '{trace}'\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
        for key, value in (("core.repositoryformatversion", "1"), ("extensions.partialClone", "origin"),
                           ("remote.origin.promisor", "true"), ("remote.origin.url", str(self.repo.parent / "nosuchremote")),
                           ("remote.origin.uploadpack", f"sh {stub}")):
            self.git("config", key, value)
        self.drop_object("HEAD:" + ICON)
        self.drop_object("origin/main:" + SNAPSHOT)   # 一時フォルダに取り出す main のスクリプトの物も欠けている(どの commit も同じ物)
        self.git("show", "HEAD:" + ICON, check=False)   # 止める設定が無いと、uploadpack が動く構成であることを確かめる
        self.assertTrue(trace.exists())
        trace.unlink()
        self.assert_merge(reviewed, 1, "不合格 ⓪:")
        self.assertFalse(trace.exists())

    def test_snapshot_script_hidden_in_merge_is_not_run(self):
        """取り込みで diff-snapshot.sh に印を書く行を隠しても、作業ツリーのスクリプトは動かない。"""
        mark = self.repo.parent / MARK
        reviewed = self.normal(extra=lambda: self.plant(mark))
        self.assert_merge(reviewed, 2, "違う: ")
        self.assertFalse(mark.exists())

    def test_secret_file_hidden_in_merge_fails_0_without_content(self):
        """既定の機密のパス(.env)の変更は、中身を読まずにパスだけを出して止まる。"""
        def hide():
            self.write(".env", "TOKEN=dummy-secret-0123456789\n")
            self.write("sub/.Env.local", "TOKEN=dummy-secret-upper-9876543210\n")   # 大文字を含む名前も当たる(grep -i)

        reviewed = self.normal(extra=hide)
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        lines = out.splitlines()
        hit = lines[[i for i, line in enumerate(lines) if line.startswith("機密の除外: ")][0] + 1:-1]   # 除外の行の後ろ、最後の行の前
        self.assertIn(".env", hit, out)
        self.assertIn("sub/.Env.local", hit, out)
        for value in ("dummy-secret-0123456789", "dummy-secret-upper-9876543210"):
            self.assertNotIn(value, out + err)

    def test_profile_secret_path_changed_in_work_fails_0_without_content(self):
        """profile の secret_paths に当たるパスの変更も、中身を読まずに止まる。"""
        self.write(".claude/project-profile.yml", "secret_paths:\n  - conf/private.txt\n")
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.write("conf/private.txt", "PASSWORD=dummy-after-0123\n")
        reviewed = self.commit("work changes the private file")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertIn("conf/private.txt", out)
        self.assertNotIn("dummy-", out + err)

    def test_secret_path_only_in_base_profile_fails_0_without_content(self):
        """main から分かれた所の profile にだけ残る機密の指定も集合に入れ、中身を読まずに止まる。"""
        self.write(".claude/project-profile.yml", "secret_paths:\n  - conf/private.txt\n")
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.write(".claude/project-profile.yml", "secret_paths: []\n")
        self.write("conf/private.txt", "PASSWORD=dummy-after-0123\n")
        reviewed = self.commit("work drops the secret path and changes the file")
        self.git("switch", "-q", "main")
        self.write(".claude/project-profile.yml", "secret_paths: []\n")
        self.commit("main also drops the secret path")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.merge("", "1.0.0", extra=lambda: self.write("conf/private.txt", "PASSWORD=dummy-hidden-0123\n"))
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertIn("conf/private.txt", out)
        self.assertNotIn("dummy-", out + err)

    def test_version_bump_hidden_in_merge_without_main_bump_is_different(self):
        """main が版を上げずに進んだ取り込みの commit に、版の変更を混ぜると「違う」(3 と ④ だけでは通る経路)。"""
        reviewed = self.work()
        self.git("switch", "-q", "main")
        self.write("other.txt", "x\n")
        self.commit("main without bump")
        self.publish_main()
        self.git("switch", "-q", "task/x")

        def hide():
            self.set_version("1.0.0", "1.0.1")
            self.replace("CHANGELOG.md", "## (次の版)\n", "## v1.0.1\n")
        self.assertEqual([], self.merge("", "1.0.0", extra=hide))
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(2, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("違う: "), out + err)
        self.assertIn("取り込みの commit の版の行が", out)
        self.assert_version(0, "合格: 版 1.0.0 から 1.0.1")

    def test_version_lines_bumped_in_merge_without_main_bump_is_different(self):
        """main が版を上げずに進んだ取り込みの commit で、見出しは変えず、JSON の版の 3 行だけを次の版にすると「違う」。"""
        reviewed = self.work()
        self.git("switch", "-q", "main")
        self.write("other.txt", "x\n")
        self.commit("main without bump")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.assertEqual([], self.merge("", "1.0.0", extra=lambda: self.set_version("1.0.0", "1.0.1")))
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(2, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("違う: "), out + err)
        self.assertIn("取り込みの commit の版の行が", out)
        self.assertNotIn("自分の節の見出しが変わった", out)

    def test_changelog_heading_bumped_in_merge_without_main_bump_is_different(self):
        """main が版を上げずに進んだ取り込みの commit で、JSON の版は変えず、自分の節の見出しだけを次の版にすると「違う」。"""
        reviewed = self.work()
        self.git("switch", "-q", "main")
        self.write("other.txt", "x\n")
        self.commit("main without bump")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.assertEqual([], self.merge("", "1.0.0", extra=lambda: self.replace("CHANGELOG.md", "## (次の版)\n", "## v1.0.1\n")))
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(2, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("違う: "), out + err)
        self.assertIn("自分の節の見出しが変わった", out)

    def test_secret_path_only_in_base_profile_review_input_has_no_value(self):
        """1 が出す機密の除外の正規表現を 2 のスナップショットに渡すと、共通の祖先の設定にだけ残る機密のファイルの値が出ない。"""
        self.write(".claude/project-profile.yml", "secret_paths:\n  - conf/private.txt\n")
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.write(".claude/project-profile.yml", "secret_paths: []\n")
        self.write("conf/private.txt", "PASSWORD=dummy-after-0123\n")
        reviewed = self.commit("work drops the secret path and changes the file")
        self.git("switch", "-q", "main")
        self.write(".claude/project-profile.yml", "secret_paths: []\n")
        self.commit("main also drops the secret path")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.merge("", "1.0.0", extra=lambda: self.write("conf/private.txt", "PASSWORD=dummy-hidden-0123\n"))
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        eres = [l[len("機密の除外: "):] for l in out.splitlines() if l.startswith("機密の除外: ")]
        self.assertEqual(1, len(eres), out)
        fetch, snap = review_input_commands()
        outs = [self.repo.parent / "snap-head.md", self.repo.parent / "snap-main.md"]
        lines = ["set -e", "export GIT_NO_LAZY_FETCH=1", fetch.replace("<安全用のオプション>", "--no-pager --no-replace-objects")]
        for base, o in zip(("HEAD", "origin/main"), outs):
            lines.append(snap.replace("--base HEAD", "--base " + base).replace(".claude/reviews/<名>-pr-before.md", str(o))
                         .replace("<take と同じ ERE>", "'^\\.claude/reviews/'") + " --exclude '" + eres[0] + "'")
        done = subprocess.run(["bash", "-c", "\n".join(lines) + "\n"], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        for o in outs:
            text = o.read_text(encoding="utf-8")
            self.assertNotIn("dummy-", text)
        self.assertIn("conf/private.txt", outs[1].read_text(encoding="utf-8"))

    def second_round_secret(self, work_profile, main_profile, pr_before):
        """機密の指定を変える 1 回目の取り込みと、P の後の 2 回目の取り込み(取り込みの commit で値を書き換える)。2 回目の 1 の結果を返す。"""
        self.write(".claude/project-profile.yml", main_profile[0])
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.write(".claude/project-profile.yml", work_profile)
        self.write("conf/private.txt", "PASSWORD=dummy-after-0123\n")
        reviewed = self.commit("work changes the private file")
        self.git("switch", "-q", "main")
        self.write(".claude/project-profile.yml", main_profile[1])
        self.write("other-1.txt", "x\n")
        self.commit("main 1")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.merge("", "1.0.0")
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        p = pr_before()   # 2 で作業の差分の全体をレビューし直した P
        self.git("switch", "-q", "main")
        self.write("other-2.txt", "x\n")
        self.commit("main 2")
        self.publish_main()
        self.git("switch", "-q", "task/x")
        self.merge("", "1.0.0", extra=lambda: self.write("conf/private.txt", "PASSWORD=dummy-round2-0123\n"))
        return self.run_script(self.merge_script, p)

    def test_secret_path_only_in_base_profile_second_round_fails_0_without_content(self):
        """共通の祖先だけにある機密の指定は、P を reviewed にした 2 回目でも外れない(作業ブランチが main から分かれた所の profile)。"""
        def empty_commit():
            self.git("commit", "-q", "--allow-empty", "-m", "chore: 取り込みの後の作業の差分をレビューし直す")
            return self.git("rev-parse", "HEAD").stdout.strip()
        code, out, err = self.second_round_secret("secret_paths: []\n", ("secret_paths:\n  - conf/private.txt\n", "secret_paths: []\n"), empty_commit)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertNotIn("dummy-", out + err)

    def test_secret_path_removed_in_pr_before_commit_second_round_fails_0_without_content(self):
        """作業が足した機密の指定を P で消しても、2 回目の集合から外れない(作業ブランチの commit の profile)。"""
        def drop_entry():
            self.write(".claude/project-profile.yml", "secret_paths: []\n")
            return self.commit("chore: 機密の指定を消す")
        code, out, err = self.second_round_secret("secret_paths:\n  - conf/private.txt\n", ("secret_paths: []\n", "secret_paths: []\n"), drop_entry)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertNotIn("dummy-", out + err)

    def merge_ours(self, message, extra=None):
        """origin/main を取り込む。profile が衝突したら、作業の側を取る(機密の指定を残さない解き方)。"""
        done = self.git("merge", "-q", "--no-ff", "-m", message, "origin/main", check=False)
        if done.returncode:
            self.git("checkout", "--ours", "--", ".claude/project-profile.yml")
        if extra:
            extra()
        self.git("add", "-A")
        self.git("commit", "-q", "--no-edit") if done.returncode else (extra and self.git("commit", "-q", "--amend", "--no-edit"))
        return done.returncode

    def test_secret_path_only_in_past_main_third_round_fails_0_without_content(self):
        """main の側だけにあった機密の指定を、衝突の解き方と後の main の両方が消しても、3 回目の集合に残る(過去に取り込んだ main の profile)。"""
        profile = ".claude/project-profile.yml"
        self.write(profile, "task_store: x\n")
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.write(profile, "task_store: work\n")
        self.write("conf/private.txt", "PASSWORD=dummy-after-0123\n")
        reviewed = self.commit("work")

        def main_commit(text, path, name):
            self.git("switch", "-q", "main")
            self.write(path, text)
            self.commit(name)
            self.publish_main()
            self.git("switch", "-q", "task/x")

        def pr_before(name):
            self.git("commit", "-q", "--allow-empty", "-m", name)
            return self.git("rev-parse", "HEAD").stdout.strip()
        main_commit("secret_paths:\n  - conf/private.txt\n", profile, "M1")
        self.assertNotEqual(0, self.merge_ours("H1"))
        for rev in (reviewed,):
            code, out, err = self.run_script(self.merge_script, rev)
            self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        p1 = pr_before("P1")
        main_commit("task_store: x\n", profile, "M2")
        self.merge_ours("H2")
        code, out, err = self.run_script(self.merge_script, p1)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        p2 = pr_before("P2")
        main_commit("x\n", "other-3.txt", "M3")
        self.merge_ours("H3", extra=lambda: self.write("conf/private.txt", "PASSWORD=dummy-round3-0123\n"))
        code, out, err = self.run_script(self.merge_script, p2)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertNotIn("dummy-", out + err)

    def test_unreadable_profile_in_middle_commit_fails_0(self):
        """作業ブランチの途中の commit の profile が読めないと、後の commit で直しても、1 は機密のパスの集合を作れずに止まる。"""
        self.git("switch", "-q", "-c", "task/x")
        self.write(".claude/project-profile.yml", "secret_paths: conf/x.txt\n")
        self.replace(TOOL, "print('tool')\n", "print('tool changed')\n")
        self.commit("broken profile")
        self.write(".claude/project-profile.yml", "secret_paths:\n  - conf/x.txt\n")
        reviewed = self.commit("fix profile")
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertEqual("不合格 ⓪: 機密のパスの集合を作れない", out.splitlines()[-1])
        self.assertIn("配列でなければならない", err)

    def two_merges_without_pr_before_commit(self, broken=False):
        """M1 だけが機密の指定を持ち、P を作らずに取り込み、M2 が指定を消した後に、取り込みの commit で値を書き換える。"""
        profile = ".claude/project-profile.yml"
        self.write(profile, "task_store: x\n")
        self.write("conf/private.txt", "PASSWORD=dummy-before-0123\n")
        self.commit("profile")
        self.publish_main()
        self.git("switch", "-q", "-c", "task/x")
        self.replace(TOOL, "print('tool')\n", "print('tool changed')\n")
        bad = None
        if broken:
            self.write(profile, "secret_paths: conf/x.txt\n")
            bad = self.commit("broken profile")
            self.write(profile, "task_store: x\n")
        reviewed = self.commit("work")
        for name, text in (("M1", "secret_paths:\n  - conf/private.txt\n"), ("M2", "task_store: x\n")):
            self.git("switch", "-q", "main")
            self.write(profile, text)
            self.commit(name)
            self.publish_main()
            self.git("switch", "-q", "task/x")
            if name == "M1":
                self.merge("", "1.0.0")   # P を作らない回(reviewed は据え置き)
        self.merge("", "1.0.0", extra=lambda: self.write("conf/private.txt", "PASSWORD=dummy-hidden-0123\n"))
        return reviewed, bad

    def test_secret_path_from_merged_main_without_pr_before_commit_fails_0_without_content(self):
        """P を作らなかった回に取り込んだ main の指定も、次の回の集合に残る(先頭からたどる)。"""
        reviewed, _ = self.two_merges_without_pr_before_commit()
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.assertNotIn("dummy-", out + err)

    def test_unreadable_profile_continuation_keeps_merged_main_secret_out_of_review_input(self):
        """読めない profile で止まった後、続け方のとおり(読めない commit を外した ref と、人が読んだ要素と既定の 3 つ)に作った正規表現で、レビュー入力に値が出ない。"""
        reviewed, bad = self.two_merges_without_pr_before_commit(broken=True)
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual("不合格 ⓪: 機密のパスの集合を作れない", out.splitlines()[-1])
        fetch, snap = review_input_commands()
        d = Path(tempfile.mkdtemp(dir=self.repo.parent))
        archive = subprocess.run(["git", "archive", "origin/main", "plugins/dev-workflow/skills"], cwd=self.repo, env=self.env, check=True, stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(d)], input=archive.stdout, check=True)
        sd = d / "plugins/dev-workflow/skills/do-task/scripts"
        rev = lambda *a: self.git(*a).stdout.split()
        refs = rev("rev-list", "--first-parent", "--parents", "HEAD", "^origin/main") + rev("rev-parse", "origin/main", "HEAD", reviewed)
        refs += rev("merge-base", reviewed, "origin/main")
        args = [x for r in dict.fromkeys(refs) if r != bad for x in ("--ref", r)]
        globs = subprocess.run(["python3", "-B", str(sd / "secret-profiles.py"), "--cwd", ".", *args], cwd=self.repo, env=self.env,
                               check=True, stdout=subprocess.PIPE).stdout.split(b"\0")[:-1]
        globs = [g.decode() for g in globs] + ["conf/x.txt", ".env", ".env.*", ".dev.vars"]   # 人が読んだ要素と既定の 3 つ
        ere = subprocess.run(["bash", str(sd / "diff-snapshot.sh"), "--print-exclude-ere", *[x for g in globs for x in ("--exclude-glob", g)]],
                             cwd=self.repo, env=self.env, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        outs = [self.repo.parent / "c-head.md", self.repo.parent / "c-main.md"]
        lines = ["set -e", "export GIT_NO_LAZY_FETCH=1", "d=" + str(d)]
        for base, o in zip(("HEAD", "origin/main"), outs):
            lines.append(snap.replace("--base HEAD", "--base " + base).replace(".claude/reviews/<名>-pr-before.md", str(o))
                         .replace("<take と同じ ERE>", "'^\\.claude/reviews/'") + " --exclude '" + ere + "'")
        done = subprocess.run(["bash", "-c", "\n".join(lines) + "\n"], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        for o in outs:
            self.assertNotIn("dummy-", o.read_text(encoding="utf-8"))
        self.assertIn("conf/private.txt", outs[1].read_text(encoding="utf-8"))

    def test_snapshot_script_mixed_in_merge_is_not_run_until_fixed(self):
        """取り込みに混ぜた diff-snapshot.sh の行は、1・2 のスナップショット・P で直した後の 3 のどれでも動かない。"""
        mark = self.repo.parent / MARK
        reviewed = self.normal(extra=lambda: self.plant(mark))
        merged = self.checked
        self.assert_merge(reviewed, 2, "違う: ")
        self.assertFalse(mark.exists())
        fetch, snap = review_input_commands()
        out = self.repo.parent / "pr-before.md"
        script = ("set -e\nexport GIT_NO_LAZY_FETCH=1\n"
                  + fetch.replace("<安全用のオプション>", "--no-pager --no-replace-objects") + "\n"
                  + snap.replace(".claude/reviews/<名>-pr-before.md", str(out)).replace("<take と同じ ERE>", "'^\\.claude/reviews/'") + "\n")
        done = subprocess.run(["bash", "-c", script], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertTrue(out.exists())
        self.assertFalse(mark.exists())
        self.write(SNAPSHOT, self.git("show", "origin/main:" + SNAPSHOT).stdout)   # レビューで見つけ、P で直す
        self.bump("1.0.1", "1.0.2")
        self.assert_version(0, "合格: 版 1.0.1 から 1.0.2", checked=merged)
        self.assertFalse(mark.exists())

    def test_reviewed_merge_commit_does_not_run_its_snapshot_script_fails_0(self):
        """reviewed に取り込みの commit を渡しても、その commit の diff-snapshot.sh は動かさずに ⓪ で止まる。"""
        mark = self.repo.parent / MARK
        self.normal(extra=lambda: self.plant(mark))
        self.assert_merge(self.git("rev-parse", "HEAD").stdout.strip(), 1, "不合格 ⓪:")
        self.assertFalse(mark.exists())

    def test_profile_helper_changed_by_the_work_is_not_run(self):
        """作業が secret-profiles.py を変えても、取り込みの確かめは main の版を動かす。"""
        mark = self.repo.parent / MARK
        self.git("switch", "-q", "-c", "task/x")
        self.replace(PROFILES, "from __future__ import annotations\n",
                     f"from __future__ import annotations\nopen({str(mark)!r}, 'w').write('x')\n")
        reviewed = self.commit("work changes the helper")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        self.assert_merge(reviewed, 0, "同じ: ")
        self.assertFalse(mark.exists())

    def test_unreadable_object_fails_0(self):
        """両方の git diff が失敗しても、空どうしを比べて「同じ」にしない。"""
        reviewed = self.normal()
        self.drop_object("HEAD:" + ICON)
        self.assert_merge(reviewed, 1, "不合格 ⓪: 比べる中身を取れない")

    def test_unreadable_changelog_fails_0(self):
        reviewed = self.normal()
        self.drop_object("HEAD:CHANGELOG.md")
        self.assert_merge(reviewed, 1, "不合格 ⓪: 比べる中身を取れない")

    def test_second_merge_without_pr_before_commit_keeps_reviewed_is_same(self):
        """2 を行わなかった回の次は、前の回のレビュー済み SHA をそのまま使う(取り込みの commit を渡すと ⓪)。"""
        reviewed = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        first_merge = self.checked
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "その次の PR")
        self.merge("", "1.0.2")
        self.assert_merge(reviewed, 0, "同じ: ")
        self.assert_merge(first_merge, 1, "不合格 ⓪:")

    def test_change_hidden_in_version_file_is_different(self):
        reviewed = self.normal(extra=lambda: self.replace(PLUGIN, '"license": "MIT"', '"license": "GPL"'))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_change_hidden_in_the_merge_is_different(self):
        self.assert_merge(self.normal(extra=lambda: self.replace(SRC, "line 3\n", "line 3 sneaked\n")), 2, "違う: ")

    def test_mode_change_hidden_in_the_merge_is_different(self):
        self.assert_merge(self.normal(extra=lambda: (self.repo / SRC).chmod(0o755)), 2, "違う: ")

    def test_binary_change_hidden_in_the_merge_is_different(self):
        reviewed = self.normal(extra=lambda: (self.repo / ICON).write_bytes(b"\x00\x01sneaked\x00"))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_dropping_the_main_changelog_section_is_different(self):
        reviewed = self.normal(extra=lambda: self.replace("CHANGELOG.md", self.block("v1.0.1", "ほかの PR"), ""))
        self.assert_merge(reviewed, 2, "違う: ")

    def test_squashed_branch_without_the_reviewed_commit_fails_0(self):
        reviewed = self.normal()
        self.git("reset", "-q", "--soft", "origin/main")
        self.commit("squash")
        self.assert_merge(reviewed, 1, "不合格 ⓪:")


class VersionCheckTest(ScratchRepo):
    """「PR の前の手順」の 3(版の確かめ)。"""

    def test_commit_added_before_round_fails_0(self):
        self.normal()
        checked = self.checked
        self.replace(SRC, "line 9\n", "line 9 later\n")
        self.commit("1 の後に足した commit")
        self.bump("1.0.1", "1.0.2")
        self.assert_version(1, "不合格 ⓪:", checked=checked)

    def nonplugin_merged(self):
        self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")

    def test_setup_sh_pr_with_bump_passes(self):
        """setup.sh(プラグインを使わない導入)だけを変えた PR も、配るものを変えた PR として版を上げる(#359 と同じ)。"""
        self.git("switch", "-q", "-c", "task/x")
        self.replace("setup.sh", "echo setup\n", "echo setup changed\n")
        self.commit("setup.sh")
        self.replace("CHANGELOG.md", "## v1.0.0\n", self.block("(次の版)", "setup.sh") + "## v1.0.0\n")
        self.commit("docs")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge(self.block("(次の版)", "setup.sh"), "1.0.1")
        self.bump("1.0.1", "1.0.2")
        self.assertIn("版 1.0.1 から 1.0.2", self.assert_version(0, "合格: "))

    def test_nonplugin_pr_without_bump_passes(self):
        self.nonplugin_merged()
        self.assertIn("版は 1.0.1 のまま", self.assert_version(0, "合格: 配るプラグインを変えていないので"))

    def test_checked_head_after_secret_stop_without_merge_passes(self):
        """取り込む物が無い回でも、作業が機密のパスを変えていれば 1 は止まる。P の後の 3 は、作業ブランチの先頭を checked にして合格。"""
        self.git("switch", "-q", "-c", "task/x")
        self.replace(SRC, "line 8\n", "line 8 changed\n")
        self.write(".env.example", "TOKEN=\n")
        self.commit("impl")
        self.replace("CHANGELOG.md", "## v1.0.0\n", self.block("(次の版)", "この PR") + "## v1.0.0\n")
        reviewed = self.commit("docs")
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.bump("1.0.0", "1.0.1")
        self.assert_version(0, "合格: 版 1.0.0 から 1.0.1", checked=reviewed)

    def test_claude_plugin_doc_only_pr_passes(self):
        """.claude-plugin/ の中の marketplace.json でない文書だけを変えた PR は、版を上げずに合格する。"""
        self.git("switch", "-q", "-c", "task/x")
        self.write(".claude-plugin/README.md", "notes\n")
        self.checked = self.commit("doc in .claude-plugin")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        out = self.assert_version(0, "合格: 配るプラグインを変えていないので")
        self.assertIn("版は 1.0.1 のまま", out)

    def test_snapshot_script_in_work_tree_is_not_run(self):
        """作業ツリーの diff-snapshot.sh を書き換えても、版の確かめはそれを動かさない。"""
        self.normal()
        self.bump("1.0.1", "1.0.2")
        mark = self.repo.parent / MARK
        self.plant(mark)
        self.assert_version(0, "合格: 版 1.0.1 から 1.0.2")
        self.assertFalse(mark.exists())

    def test_checked_merge_commit_after_secret_stop_passes(self):
        """作業が機密のパスに当たるファイルを正しく変えた PR: 1 は機密のパスで止まり、P の後の 3 は取り込みの commit を checked にして合格。"""
        self.git("switch", "-q", "-c", "task/x")
        self.replace(SRC, "line 8\n", "line 8 changed\n")
        self.write(".env.example", "TOKEN=\n")
        self.commit("impl")
        self.replace("CHANGELOG.md", "## v1.0.0\n", self.block("(次の版)", "この PR") + "## v1.0.0\n")
        reviewed = self.commit("docs")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge(self.block("(次の版)", "この PR"), "1.0.1")
        merged = self.checked
        code, out, err = self.run_script(self.merge_script, reviewed)
        self.assertEqual(1, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith("不合格 ⓪: 機密のパス"), out + err)
        self.bump("1.0.1", "1.0.2")   # 2 で作業の差分の全体をレビューし直し、版を上げた P
        self.assert_version(0, "合格: 版 1.0.1 から 1.0.2", checked=merged)

    def test_nonplugin_pr_with_bump_fails_2(self):
        self.nonplugin_merged()
        self.set_version("1.0.1", "1.0.2")
        self.commit("chore: 版を 1.0.2 にする")
        self.assert_version(1, "不合格 ②:")

    def test_nonplugin_pr_with_changelog_fails_3(self):
        self.nonplugin_merged()
        self.replace("CHANGELOG.md", "## v1.0.1\n", self.block("(次の版)", "テストだけ") + "## v1.0.1\n")
        self.commit("docs: CHANGELOG")
        self.assert_version(1, "不合格 ③:")

    def test_version_file_mode_change_fails_2(self):
        self.normal()
        self.bump("1.0.1", "1.0.2")
        (self.repo / PLUGIN).chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "--no-edit")
        self.assert_version(1, "不合格 ②: 版のファイルか CHANGELOG.md の種類か実行権限が変わった")

    def test_changelog_mode_change_fails_2(self):
        self.nonplugin_merged()
        (self.repo / "CHANGELOG.md").chmod(0o755)
        self.commit("CHANGELOG.md の実行権限だけを変える")
        self.assert_version(1, "不合格 ②: 版のファイルか CHANGELOG.md の種類か実行権限が変わった")

    def test_unreadable_version_file_fails_0(self):
        self.normal()
        self.bump("1.0.1", "1.0.2")
        self.drop_object("HEAD:" + PLUGIN)
        self.assert_version(1, "不合格 ⓪: 比べる中身を取れない")

    def test_bump_passes(self):
        self.normal()
        self.bump("1.0.1", "1.0.2")
        self.assertIn("版 1.0.1 から 1.0.2", self.assert_version(0, "合格: "))

    def test_redo_bump_after_collision_passes(self):
        self.normal()
        self.bump("1.0.1", "1.0.2")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "先に merge した PR")
        self.merge(self.block("v1.0.2", "この PR"), "1.0.2")
        self.bump("1.0.2", "1.0.3", heading_from="## v1.0.2\n")
        self.assertEqual(["## v1.0.3", "## v1.0.2", "## v1.0.1", "## v1.0.0"],
                         [l for l in self.read("CHANGELOG.md").splitlines() if l.startswith("## ")])
        self.assertIn("版 1.0.2 から 1.0.3", self.assert_version(0, "合格: "))

    def test_redo_bump_after_version_conflict_passes(self):
        self.past_our_version()
        self.bump("1.0.3", "1.0.4", heading_from="## v1.0.2\n")
        self.assertEqual(["## v1.0.4", "## v1.0.3", "## v1.0.2", "## v1.0.1", "## v1.0.0"],
                         [l for l in self.read("CHANGELOG.md").splitlines() if l.startswith("## ")])
        self.assertIn("版 1.0.3 から 1.0.4", self.assert_version(0, "合格: "))

    def test_work_changing_description_passes(self):
        self.normal(touch_plugin=True)
        self.bump("1.0.1", "1.0.2")
        self.assertIn("版 1.0.1 から 1.0.2", self.assert_version(0, "合格: "))

    def test_one_version_line_left_behind_fails_2(self):
        self.normal()
        self.bump("1.0.1", "1.0.2")
        self.write(MARKET, self.read(MARKET).replace('"version": "1.0.2"', '"version": "1.0.1"', 1))   # 2 つのうち 1 つを戻す
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "--no-edit")   # 版の commit のまま(親は取り込みの commit)
        self.assert_version(1, "不合格 ②:")

    def test_missing_bump_fails_2(self):
        self.normal()
        self.assert_version(1, "不合格 ②:")

    def test_version_that_is_not_the_next_fails_3(self):
        self.normal()
        self.bump("1.0.1", "1.0.5")
        self.assert_version(1, "不合格 ③:")

    def test_changelog_heading_left_unnumbered_fails_3(self):
        self.normal()
        self.bump("1.0.1", "1.0.2", heading=False)
        self.assert_version(1, "不合格 ③:")

    def test_branch_without_main_fails_0(self):
        self.work()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.bump("1.0.0", "1.0.1")
        self.assert_version(1, "不合格 ⓪:")



class RecordCheckTest(ScratchRepo):
    """「PR の前の手順」の 5(記録の確かめ ④)。"""

    def assert_record(self, first, ps, rc, head, last=None):
        last = last or self.git("rev-parse", "HEAD").stdout.strip()
        code, out, err = self.run_script(self.record_script, first=first, ps=ps, last=last)
        self.assertEqual(rc, code, out + err)
        self.assertTrue(out.splitlines()[-1].startswith(head), out + err)
        return out

    def test_pr_before_commit_only_passes(self):
        first = self.normal()
        p = self.bump("1.0.1", "1.0.2")
        out = self.assert_record(first, p, 0, "合格: ")
        self.assertIn(p + " chore: 版を 1.0.2 にする", out)

    def test_no_document_and_no_pr_before_commit_passes(self):
        first = self.work_tool()   # 文書の commit も PR の前の commit も作らない(1 回目は実装の commit)
        self.assert_record(first, "", 0, "合格: ")

    def test_nonplugin_merge_only_passes(self):
        first = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        self.assert_record(first, "", 0, "合格: ")

    def test_commit_hidden_after_merge_fails_4(self):
        """取り込みに変更を隠し、その上に普通の commit S を足して reviewed=S で流すと、取り込みの確かめは「同じ」になる。記録の確かめが止める。"""
        first = self.normal(extra=lambda: self.replace(SRC, "line 3\n", "line 3 sneaked\n"))
        self.replace(TOOL, "print('tool')\n", "print('S')\n")
        s = self.commit("S")
        self.assert_merge(s, 0, "同じ: ")
        self.checked = s
        p = self.bump("1.0.1", "1.0.2")
        self.assert_version(0, "合格: ")
        self.assert_record(first, p, 1, "不合格 ④:")

    def test_snapshot_script_in_work_tree_is_not_run(self):
        """作業ツリーの diff-snapshot.sh を書き換えても、記録の確かめはそれを動かさない。"""
        first = self.normal()
        p = self.bump("1.0.1", "1.0.2")
        mark = self.repo.parent / MARK
        self.plant(mark)
        self.assert_record(first, p, 0, "合格: ")
        self.assertFalse(mark.exists())

    def test_snapshot_script_changed_by_the_work_is_not_run(self):
        """作業が commit した diff-snapshot.sh の変更も、記録の確かめは動かさない(main の版を動かす)。"""
        mark = self.repo.parent / MARK
        self.git("switch", "-q", "-c", "task/x")
        self.plant(mark)
        first = self.commit("work changes diff-snapshot.sh")
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        self.assert_record(first, "", 0, "合格: ")
        self.assertFalse(mark.exists())

    def test_missing_pr_before_commit_in_list_fails_4(self):
        first = self.normal()
        self.bump("1.0.1", "1.0.2")
        self.assert_record(first, "", 1, "不合格 ④:")

    def test_first_not_in_branch_fails_4(self):
        self.git("switch", "-q", "-c", "side")
        self.replace(TOOL, "print('tool')\n", "print('side')\n")
        side = self.commit("side")
        self.git("switch", "-q", "main")
        self.normal()
        self.assert_record(side, "", 1, "不合格 ④: 作業ブランチの先頭が、レビュー済み SHA(1 回目)を含まない")

    def test_unreviewed_branch_merged_after_round_fails_4(self):
        """3 と ④ が合格した P の上に、main でない別のブランチを取り込むと、先頭が 3 で確かめた先頭でなくなる。"""
        first = self.normal()
        p = self.bump("1.0.1", "1.0.2")
        self.assert_record(first, p, 0, "合格: ")
        self.git("switch", "-q", "-c", "evil", "origin/main")
        self.write("plugins/dev-workflow/skills/demo/evil.txt", "evil\n")
        self.commit("evil")
        self.git("switch", "-q", "task/x")
        self.git("merge", "-q", "--no-ff", "-m", "merge evil", "evil")
        self.assert_record(first, p, 1, "不合格 ④: 作業ブランチの先頭が、3 で確かめた先頭でない", last=p)

    def test_hidden_merge_after_round_fails_4(self):
        """回の後に main が進み、1〜3 をやり直さずに変更を隠して取り込み直すと、④ が止める。"""
        first = self.normal()
        p = self.bump("1.0.1", "1.0.2")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "先に merge した PR")
        self.merge(self.block("v1.0.2", "この PR"), "1.0.2", extra=lambda: self.replace(SRC, "line 3\n", "line 3 sneaked\n"))
        self.assert_record(first, p, 1, "不合格 ④: 作業ブランチの先頭が、3 で確かめた先頭でない", last=p)

    def test_second_merge_without_pr_before_commit_passes(self):
        first = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        self.main_moves("1.0.1", "1.0.2", "1.0.1", "その次の PR")
        self.merge("", "1.0.2")
        self.assert_version(0, "合格: 配るプラグインを変えていないので")
        self.assert_record(first, "", 0, "合格: ")

    def test_unreviewed_commit_merged_from_side_fails_4(self):
        """取り込みの 2 つ目の親の側にある、レビューしていない commit S も数える(① と ③ は通る)。"""
        first = self.work_tool()
        self.main_moves("1.0.0", "1.0.1", "1.0.0", "ほかの PR")
        self.merge("", "1.0.1")
        self.git("switch", "-q", "-c", "side")
        self.replace(TOOL, "print('tool changed')\n", "print('unreviewed')\n")
        side = self.commit("S")
        self.git("switch", "-q", "task/x")
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.assert_merge(side, 0, "同じ: ")
        self.checked = self.git("rev-parse", "HEAD").stdout.strip()
        self.assert_version(0, "合格: 配るプラグインを変えていないので")
        self.assert_record(first, "", 1, "不合格 ④: main にもレビュー済み SHA(1 回目)にも無い")


if __name__ == "__main__":
    unittest.main()
