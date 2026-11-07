#!/usr/bin/env python3
"""workflow リポジトリの一括検証。

検証内容:
  1. .claude-plugin/marketplace.json / plugin.json の JSON 構文と必須フィールド、
     配布メタの整合(version 3 箇所の一致・plugins[].source の実在・
     description の skill 件数の表記と実数の一致)
  2. 各 SKILL.md: frontmatter の存在と name / description 必須、name とディレクトリ名の一致、
     frontmatter に allowed-tools / disallowed-tools の鍵が無いこと(design.md §6)
  3. SKILL.md 500 行以下(design.md §6。論理行数 = 改行の数 + 末尾が改行で終わらなければ 1)
  4. description の長さ(規約 150〜500 字は ERROR / 上限 1024 字)
  5. 禁止パターン(design.md §5): 絶対パス・TeamCreate/TeamDelete・日付付きモデル ID・
     claude -p と、環境変数 WORKFLOW_PROJECT_NAMES に渡した周辺プロジェクト名
     (未設定なら 1 語も足さない)。行内に `<!-- validate-allow: 理由 -->` があれば
     その行だけ免除する(8 でも効く。7・11〜15 と委託の語の除外の不変条件では効かない)
  6. SKILL.md と references/*.md 内の相対リンク(references/ scripts/ templates/ 兄弟 skill)の
     存在。fragment 付き・タイトル付きも検査し、コードフェンスの中は除外する
  7. 委託の語(design.md §7-7): 全 skill の skill 直下(画像を除く)・references/ 配下の
     *.md・scripts/ 配下(画像を除く)を検査し、未移行 skill(許容リスト)と
     検査対象外ファイルを除外する
  8. ホスト CLI 語(design.md §7-7-1): 全 skill の skill 直下(画像を除く)・
     references/ 配下の *.md を大小無視で検査する(scripts/ は対象外。委託の語と同じ
     除外 2 本を共有する)。ランナー名・サンドボックスモード名・コマンド形・
     プラグイン/subagent 名・MCP サーバ名・ホストのツール引数名・
     ホストの質問の道具名と引数名の 7 系統。
     **5 と同じ行単位のマーカーで免除できる** —— MCP サーバ名は生成する設定の
     識別子そのもので、役割語へ書き換えて消せないため(design.md §7-7-1)
  9. 人が読む文の書き方の正本へのリンク(design.md §6・§5-25): 各 SKILL.md の `## 原則` の節
     (見出しの次の行から次の `## ` の前まで。コードフェンスの中は除く)に、6 と同じリンクの形で
     do-task/references/writing-for-people.md を指すものが 1 つ以上あること。リンク先は
     SKILL.md の位置から解決し、実パスで比べる。リンクにしていない言及・節の外・フェンスの中は数えない。
     マーカーでは免除しない
 10. SKILL.md の本文の行の長さ(design.md §6。WARN): frontmatter の後の、コードフェンスの外
     (判定は 6 と同じ _mask_code_fences)の行のうち、行頭の空白を除いて `|` で始まらない行
     (表の行を除く)について、文字数から、200 字を超えるインラインのコード(1 行の中の
     `[^`]+`)の長さを引いた数が 200 を超えたら WARN にする。ファイル:行番号と文字数を出す。
     マーカーでは免除しない
 11. 置き換える語(design.md §6): 「枡」「継承枠」「能力帯」「私有」「素通り」「塞ぐ」(塞が・塞ぎ・塞い・塞げ・塞ご。「閉塞」など前に漢字が付く語は除く)「git 出口」
     「写し」(写す・写さ・写せ・写そ と「複写」。前に漢字が付く語は「〜写す」「〜写し」などの形だけを探し、
     名詞のまま探すのは「複写」だけ。前に描・転・模・映・試・書・接・実・特・速・縮・透・活・投・連・盗・大 のどれかが付く語は除く。
     一覧に無い漢字が前に付く語は、意味を問わず探す。探したくない語が新しく出たら一覧に足す)
     「宛先」(宛て先も)
     「前置き」(「以前置き…」と、後ろが「置き換え」「置き場」の形は除く)
     「フル段階」「標準段階」「最小段階」「段階判定」「実行段階」「結末の行」(「段階」だけ・「結末」だけは探さない)
     太字の「**フル**」「**標準**」「**最小**」と、「フル / 標準」「標準・最小」のような並び(「フル」か「標準」の後に
     / ／ ・ と「標準」か「最小」が続く形だけ。「標準出力」などは探さない)
     「サイレント」(カタカナの字面)
     「レビュー役」「実装役」「まとめ役」「検証役」「全回帰」(後ろが「テスト」の形は除く)「試験」「直読み」「峻別」
     「一時物」「子起動」「非ファイルタスク」「退行」「追随」「起票」(どれも字面だけを探す)
     「担保」「打ち直」「読込」「書込」「陽性対照」「陰性対照」「正常対照」(間に「の」が入る形も)(どれも字面だけを探す)
     「打つ」(打た・打ち・打っ・打て・打と。コマンドを実行する意味。探すのは「打」の後ろが た・つ・て・っ・と の形と、
     「打ち」の後ろが 切・消・合・返・直 以外の形。「打ち切る」「打ち消す」「打ち合わせ」「打ち返す」「打ち直す」と、
     前が 壁・抜き・決め の「壁打ち」「抜き打ち」「決め打ち」は探さない。字面だけを探すので、別の意味の「打ち明ける」
     「釘を打つ」なども見つける限界がある)
     「倒す」「倒れる」(「に倒す」「へ倒す」「に倒れる」の形と、間に「しか」「必ず」「も」「は」が入る形だけ。「を倒す」などは探さない。
     「に」「へ」の後に続く形は、字どおりの意味〈「後ろには倒さない」など〉でも見つける限界がある。そのときは「傾ける」「横にする」などに言い換える)
     「素の」(「要素の」など前に漢字が付く語は除く)
     「止めの印」「守る値」「対話点」「報告点」を skill の文書(SKILLS_DIR 配下の *.md と
     templates/ 配下)と docs/design.md から探し、見つけたら言い換え先を添えて ERROR にする。
     除外は `do-task/references/writing-for-people.md`(言い換え表)と、docs/design.md の
     「- **文書の中で置き換える語**」「- **置き換える語**」で始まる定義の行だけ。定義の行で除くのは、
     その行で定義する語の組だけ(「- **置き換える語**」の行は止めの印・守る値・対話点・報告点、
     「- **文書の中で置き換える語**」の行はそのほかの語)。定義の行が design にちょうど 1 行ずつでなければ
     ERROR にする。免除の印は効かない
 12. 番号だけの理由(design.md §6): skill の文書(SKILLS_DIR 配下の *.md と templates/ 配下)、
     docs/design.md・README.md の結果の行と保留の行の例で、理由・停止条件が番号だけなら ERROR。
     D・S・U・G・M・H の番号、決定 N、軸 N、①〜⑳・条件 ③、§N-N を対象にする。
     コードフェンス・インラインのコードも検査する。参照や手順の番号は検査しない。
     免除の印は効かない。scripts・決定録・CHANGELOG は対象にしない
 13. 27 語の初出の言い換え(design.md §6): 各 skill 直下の SKILL.md と references/**/*.md の
     本文で、語を初めて出す所に決まった説明があること。語と説明は design §6 の表から読む。
     表の欠落・重複・空欄・語数の誤りも ERROR。Markdown は markdown-it-py で解釈する。
 14. 同じファイルの本文で、同じ語の言い換えが 2 か所以上なら ERROR。
 15. 名前でもコードでもない「縮退」は ERROR。意味に合う言い換えかどうかはレビューする。
     13〜15 の名前と報告の型の除外は scripts/wording-exceptions.json に限定する。
     定義元・許す字面の実在、使うファイル、型・重複を検査する。免除の印は効かない。
     除外文書は do-task/references/writing-for-people.md の正確なパスだけ。
     templates・design 本文・README・決定録・スクリプトには広げない。

終了コード: ERROR があれば 1。WARN のみなら 0。

使い回し: 同じプロセスで何度も流す呼び出し元は、main(file_results=<表>) で共有の表を渡すと、
11・12・13〜15・委託の語・ホスト CLI 語のファイルごとの結果を、その 1 回の実行の間だけ使い回せる。
表を渡さない CLI の stdout と終了コードは変わらない。
"""

import hashlib
import json
import os
import re
import shlex
import stat
import struct
import subprocess
import sys
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "plugins" / "dev-workflow" / "skills"

ERRORS: list[str] = []
WARNS: list[str] = []

# ファイルごとの検査の結果を使い回す表(同じプロセスの中だけ)。
# キーは (検査器のコードのダイジェスト, 検査の名前, リポジトリ相対のパス, 本文のダイジェスト, 結果を左右するほかの入力)。
# 値は、その検査がそのファイルについて足した ERRORS と WARNS。
# 既定は None(使い回さない)。表は main(file_results=<表>) で渡した 1 回の実行の間だけ使い、抜けるときに前の値へ戻す。
# CLI・表を渡さない main()・検査の関数の直接の呼び出しは、表を引かず・入れず・キーも作らない(今と同じ処理)。
# 表を渡しても、1 回の実行では同じ検査が同じファイルを 2 回は見ないので、表に当たるのは 2 回目以降の実行だけ。
_FILE_RESULTS: Optional[dict] = None
_VALIDATOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _reuse_file_result(check, f, text, inputs, run):
    """1 つのファイルについての検査 `run()` の結果を、表から使うか、流して表に入れる。

    `run()` は `text` と `inputs` だけから ERRORS・WARNS へ足す(ファイルを読み直さない・ほかのファイルを見ない)。
    """
    if _FILE_RESULTS is None:
        run()
        return
    key = (_VALIDATOR_SHA256, check, f.relative_to(REPO).as_posix(),
           hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest(), inputs)
    hit = _FILE_RESULTS.get(key)
    if hit is not None:
        ERRORS.extend(hit[0])
        WARNS.extend(hit[1])
        return
    errors, warns = len(ERRORS), len(WARNS)
    run()
    _FILE_RESULTS[key] = (tuple(ERRORS[errors:]), tuple(WARNS[warns:]))

# 画像拡張子(禁止パターン検査・委託の語検査の両方が走査から除外する)
_IMAGE_EXTS = {".png", ".jpg"}

# 固有名のハードコード検出(ユーザー環境の絶対パス・周辺プロジェクト名の混入)
FORBIDDEN_PATTERNS = [
    (rf"{re.escape(str(Path.home()))}(?!/dev/workflow\b)", "ユーザー環境の絶対パス"),
    (r"TeamCreate|TeamDelete", "廃止 API(Agent + SendMessage を使う)"),
    (r"claude-(?:fable|mythos|opus|sonnet|haiku)-[0-9][-0-9a-z]*", "日付付き/版数付きモデル ID(エイリアスを使う)"),
    (r"(?:Fable|Mythos|Opus|Sonnet|Haiku)\s*[0-9]", "モデル版数のハードコード(エイリアスを使う)"),
    (r"claude\s+-p\b|claude\s+--print", "claude -p の Bash 起動(禁止・別課金)"),
]

# 禁止パターン検査の免除マーカー(design.md §5)。`<!-- validate-allow: 理由 -->` の形で、
# **理由の記述が必須**(`<!-- validate-allow -->` だけでは免除しない)。行単位で効き、
# コードフェンスの内外を問わない。**効くのは禁止パターン検査とホスト CLI 語検査の 2 つ** ——
# 委託の語・除外の不変条件は免除規則を持たない(そちらは役割語へ書き換えて消せるため)。
# ホスト CLI 語に効かせるのは、MCP サーバ名のように**生成する設定の識別子そのもので、
# 書き換えて消せない**語が実在するから(design.md §7-7-1)。
# 以前は「禁止・使わない・しない・廃止・ではなく」を含む行を一律に免除していたが、
# 語の偶然の一致(普通の文に「〜しない」が入っているだけ)で混入が素通りしていた。
# 理由は**最初のコメント終端まで**を取る —— `:\s*\S.*?-->` のように貪欲さを抑えるだけでは、
# `<!-- validate-allow: --> <!-- 別のコメント -->` が後ろのコメントの終端まで飲み込んで
# 「理由あり」に化ける(実測)。
_ALLOW_MARKER_RE = re.compile(r"<!--\s*validate-allow\s*:((?:(?!-->).)*)-->")


def _has_allow_marker(line_text: str) -> bool:
    """行に**理由付きの**免除マーカーがあるか。理由が空白だけのものは免除しない。"""
    return any(m.group(1).strip() for m in _ALLOW_MARKER_RE.finditer(line_text))

# 周辺プロジェクト名の検査(design.md §5): 環境変数 WORKFLOW_PROJECT_NAMES に
# 「名前の一覧」(カンマ区切り)を渡したときだけ、その名前を禁止語として追加する。
# **未設定なら 1 語も足さない**。実行環境のディレクトリを列挙しないので、検査の結果は
# 渡した値だけで決まり、その PC のファイルシステムに依らない(同じリポジトリなら再現する)。
# 一般語は渡されても除外する(現状の挙動に合わせる。明示的に渡した一般語も黙って捨てる)。
_GENERIC_DIR_NAMES = {"workflow", "demo", "memo", "resume", "test", "tmp", "sandbox", "base"}
_PROJECT_NAMES_ENV = "WORKFLOW_PROJECT_NAMES"


def _name_boundary_pattern(name: str) -> str:
    r"""名前 1 語を、**名前の端が英数字・`_` のときだけ**境界を付けた正規表現にする。

    `\b` は日本語と `_` を単語文字として扱うため、`customer-portalに配線する` のような
    日本語直結を取りこぼす。かといって固定で両側に `(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])`
    を付けると、ハイフンで終わる名前(`Proj-`)で境界の意味が反転し、`\b` が捕まえていた
    `Proj-x` / `Proj-2` を取りこぼす(退行)。名前の端の文字を見て付け外しすると、
    `\b` の完全な上位互換になる(端が集合外なら境界を要求しない = 必ず緩い)。
    プロジェクト名は小文字の一般語を含みうるので、除外集合は `Agent` 側(`[A-Za-z]`)より
    広い `[A-Za-z0-9_]` を使う —— この非対称の理由は design.md §7-7-1。"""
    left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", name) else ""
    right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]\Z", name) else ""
    return f"{left}{re.escape(name)}{right}"


_names = sorted(
    {
        n
        for n in (s.strip() for s in os.environ.get(_PROJECT_NAMES_ENV, "").split(","))
        if n and n.lower() not in _GENERIC_DIR_NAMES
    }
)
if _names:
    FORBIDDEN_PATTERNS.append(
        (
            "(?:" + "|".join(_name_boundary_pattern(n) for n in _names) + ")",
            "周辺プロジェクト固有名の混入",
        )
    )

# 検査語彙としてのモデルエイリアス(スナップショット)。§5-4 の「固定リストとして扱わない」は
# 実行時に指定できるエイリアス集合の話で、こちらは skill 本文に書いてはいけない語。
# 世代交代のときに同時に更新する 4 箇所と順序は design.md §7-7。
_MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku")

# 委託の語(design.md §7-7・測定コマンドの語彙に一致させる)。ホスト固有の委託機構名 5 語 +
# モデルエイリアス。エイリアスは _MODEL_ALIASES だけを参照し、ここで独自に列挙しない。
_DELEGATION_WORDS = [
    r"(?<![A-Za-z])Agent(?![A-Za-z])",
    "SendMessage",
    "ListAgents",
    "Explore",
    "general-purpose",
] + list(_MODEL_ALIASES)

# 移行の許容リスト(design.md §7-7)。委託の語検査から除外する未移行 skill の名前。
# 移行のたびにここから削る。許容リストが空の状態でこの検査が通った時点が v4.0.0(design.md §7-7)。
_MIGRATION_ALLOWLIST: set[str] = set()   # v4.0.0 到達(2026-09-10)。空でも検査は動き続ける

# 検査対象外ファイル(design.md §7-7 の「検査対象外ファイル」が正本)。値は SKILLS_DIR からの相対パス。
_DELEGATION_MAP = "do-task/references/delegation-map.md"
_EXEMPT_FILES = {_DELEGATION_MAP, "do-task/references/external-runners.md"}

def _host_cli_boundary_pattern(pattern: str) -> str:
    """通常のホスト CLI 語だけに ASCII 英数字・_・- の境界を付ける。"""
    # IGNORECASE の [A-Za-z] が İ / ı / ſ / K へ広がらないよう、境界だけ大小を区別。
    # 語本体の大小無視とコマンド形の Unicode 空白(\s+)は呼出し元の契約を維持する。
    return rf"(?<!(?-i:[A-Za-z0-9_-]))(?:{pattern})(?!(?-i:[A-Za-z0-9_-]))"


# ホスト CLI 語(design.md §7-7-1)。委託の語とは別カテゴリで、scripts/ は対象外。
# 7 系統 14 語の語本体は大小無視(check_host_cli_words() の re.IGNORECASE)。
# 通常 12 語は ASCII 境界により別識別子内の誤検出を避け、日本語直結は検出する。
# Unicode の \b は助詞直結を取りこぼすので使わない。周辺プロジェクト名の
# _name_boundary_pattern() はハイフンを境界に含めない別契約なので共有しない。
# MCP 2 語だけは部分一致を維持し、未列挙の接頭・接尾の派生名も検出する。
# MCP 語を含む別語の誤検出は受容する。正当な行は理由付き validate-allow で免除する。
# 通常語の Gemini2 / gemini_bot / cursor-agent-wrapper は非検出となる限界がある。
# コマンド形の \s+ は全角空白・改行も許容し、跨行の一致は開始行で診断・免除する。
# 単独 codex・製品名(Codex / Cursor / Claude Code)・read-only は従来どおり対象外
# (設定パス .codex/・正当な散文・一般語との衝突を避ける)。
# ホストの質問の道具名と引数名は、ERROR の案内だけがほかの語と違う(解決表の質問の節を指す)。
# 案内は一致した字面ではなく、このリストのパターンに一致したかで選ぶ —— 大小無視の照合は
# ſ(ロングエス)にも一致し、ſ は lower() でも s に戻らないため(AſkUserQueſtion)。
_HOST_CLI_QUESTION_WORDS = [
    _host_cli_boundary_pattern(r"AskUserQuestion"),
    _host_cli_boundary_pattern(r"multiSelect"),
]
_HOST_CLI_WORDS = [
    # ランナー名
    _host_cli_boundary_pattern(r"cursor-agent"),
    _host_cli_boundary_pattern(r"gemini"),
    # サンドボックスモード名
    _host_cli_boundary_pattern(r"workspace-write"),
    _host_cli_boundary_pattern(r"danger-full-access"),
    # コマンド形(単独の codex は入れない)
    _host_cli_boundary_pattern(r"codex\s+exec"),
    _host_cli_boundary_pattern(r"codex\s+review"),
    _host_cli_boundary_pattern(r"codex\s+mcp"),
    # プラグイン・subagent 名
    _host_cli_boundary_pattern(r"codex-plugin-cc"),
    _host_cli_boundary_pattern(r"codex-rescue"),
    # MCP サーバ名(派生名を列挙せず部分一致にする)
    r"claude-in-chrome",
    r"chrome-devtools",
    # ホストのツール引数名(委託機構そのものではないが、ホストに結合する)
    _host_cli_boundary_pattern(r"run_in_background"),
    # ホストの質問の道具名と引数名
    *_HOST_CLI_QUESTION_WORDS,
]

# ホスト CLI 語の ERROR の案内(括弧の中)。質問の道具名と引数名だけ、移し先が違う。
_HOST_CLI_GUIDANCE = (
    "(役割語に書き換えるか、CLI の手順の契約として"
    " do-task/references/external-runners.md へ移す。"
    " delegation-map.md は役割語→機構の解決表で CLI 名を持たないため"
    " 移し先にならず、他の references/*.md はこの検査の対象内なので"
    " 移しても解消しない)"
)
_HOST_CLI_QUESTION_GUIDANCE = (
    "(「質問で確認する」「複数選択の質問」などのホストに依らない言い方に書き換える。"
    "ホストの道具への対応づけは do-task/references/delegation-map.md §8)"
)

# 配布メタの skill 件数の表記(「skills 12 種」/「12 skills」の両形)。
_SKILL_COUNT_RE = re.compile(r"skills?\s*(\d+)\s*種|(\d+)\s*skills?")

# Markdown の相対リンク(design.md §5)。旧形 `\[[^\]]*\]\(([^)\s#]+)\)` は
# fragment 付き(`](path#sec)`)とタイトル付き(`](path "title")`)にそもそも一致せず、
# 切れたリンクを検査せず素通りさせていた。パスだけを group(1) に取り、`#` 以降は
# 呼び出し側で落とす。括弧を含むパスは扱わない(現物に無い)。
LINK_RE = re.compile(r"""\[[^\]]*\]\(\s*([^)\s]+?)\s*(?:"[^"]*"|'[^']*')?\s*\)""")


def _mask_code_fences(body: str) -> str:
    """コードフェンス(``` / ~~~)の中身を空行に置き換えた写しを返す(行番号は保つ)。

    **リンク検査にだけ掛ける**。禁止パターン検査には掛けない —— フェンスを免除の単位に
    すると、行単位のマーカーに絞った免除が一気に広がるため(design.md §5-24)。

    CommonMark の規則のうち、**解析が同期を失うと以降のリンクが黙って検査されなくなる**
    3 点に従う(いずれも実測で再現した穴):
      - 開きフェンスの字下げは 3 空白まで(4 以上はインデントコードブロックで、フェンスを開かない)
      - 閉じは開きと**同じ文字・同じ長さ以上**で、info string を持たない
        (長さを見ないと ```` の中の ``` が外側を閉じ、以降の内外が反転する)
      - バッククォートの開きフェンスの info string に ` は入らない
        (行頭のインラインコード ```x``` をフェンスと誤認すると、そこから下が丸ごと検査されなくなる)
    """
    out = []
    fence = ""
    for line in body.split("\n"):
        m = re.match(r"[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
        marker, info = (m.group(1), m.group(2)) if m else ("", "")
        if not fence:
            if marker and not (marker[0] == "`" and "`" in info):
                fence = marker
                out.append("")
                continue
        else:
            if marker and marker[0] == fence[0] and len(marker) >= len(fence) and not info.strip():
                fence = ""
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def yaml_loader():
    """検証に必須の parser を取得する。未導入・破損を成功へ読み替えない。"""
    try:
        import yaml  # type: ignore

        loader = getattr(yaml, "safe_load", None)
        if not callable(loader):
            raise TypeError("yaml.safe_load が呼び出せない")
        return loader
    except Exception as exc:
        command = (f"{shlex.quote(sys.executable)} -m pip install -r "
                   f"{shlex.quote(str(REPO / 'requirements-dev.txt'))}")
        ERRORS.append(f"PyYAML を利用できない（未導入または読み込みの失敗）: {exc!r}。"
                      f"検証用の依存を導入・修復してください: {command}")
        return None


def parse_frontmatter(text: str, path: Path):
    if not text.startswith("---"):
        ERRORS.append(f"{path}: frontmatter がない")
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        ERRORS.append(f"{path}: frontmatter が閉じていない")
        return {}
    block = text[4:end]
    loader = yaml_loader()
    if loader is None:
        return {}
    try:
        data = loader(block) or {}
        if not isinstance(data, dict):
            ERRORS.append(f"{path}: frontmatter が辞書でない")
            return {}
        return data
    except Exception as e:  # yaml parse error
        ERRORS.append(f"{path}: frontmatter YAML パース失敗: {e}")
        return {}


def check_json_files():
    mp = REPO / ".claude-plugin" / "marketplace.json"
    pj = REPO / "plugins" / "dev-workflow" / ".claude-plugin" / "plugin.json"
    for p, required in [(mp, ["name", "plugins"]), (pj, ["name"])]:
        if not p.exists():
            ERRORS.append(f"{p}: 存在しない")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            ERRORS.append(f"{p}: JSON 構文エラー: {e}")
            continue
        for k in required:
            if k not in data:
                ERRORS.append(f"{p}: 必須フィールド '{k}' がない")
    if mp.exists() and pj.exists():
        try:
            mp_data = json.loads(mp.read_text(encoding="utf-8"))
            pj_data = json.loads(pj.read_text(encoding="utf-8"))
            entries = {pl.get("name") for pl in mp_data.get("plugins", [])}
            if pj_data.get("name") not in entries:
                ERRORS.append("marketplace.json の plugins に plugin.json の name が載っていない")

            # 版は 3 箇所にあり、1 箇所だけ上げると配布が壊れる
            # (marketplace の metadata.version / plugins[].version、plugin.json の version)
            versions = {
                "marketplace.json metadata.version": (mp_data.get("metadata") or {}).get("version"),
                "plugin.json version": pj_data.get("version"),
            }
            for pl in mp_data.get("plugins", []):
                versions[f"marketplace.json plugins[{pl.get('name')}].version"] = pl.get("version")
            # None が混じると set の要素数が 1 になり「全部欠けている」が一致として通る
            if None in versions.values() or len(set(versions.values())) > 1:
                detail = " / ".join(f"{k}={v!r}" for k, v in sorted(versions.items()))
                ERRORS.append(f"配布メタの version が一致しない(または欠けている): {detail}")

            # source が `./` 始まりならリポジトリ相対のパスとして実在を見る
            for pl in mp_data.get("plugins", []):
                src = pl.get("source")
                if isinstance(src, str) and src.startswith("./") and not (REPO / src).exists():
                    ERRORS.append(f"marketplace.json plugins[{pl.get('name')}].source が実在しない -> {src}")
        except Exception:
            pass


_NATIVE_BINARIES = (
    "linux-x86_64/dev-workflow",
    "linux-arm64/dev-workflow",
    "macos-universal/dev-workflow",
)
_NATIVE_LIMIT = 8 * 1024 * 1024
_NATIVE_REL = Path("plugins/dev-workflow/bin/dev-workflow")
_NATIVE_TARGETS = (
    "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin", "aarch64-apple-darwin",
)
# base-commit.md / publish-guard.py と同じ安全用のオプション。
_NATIVE_SAFE_GIT = (
    "--no-pager", "--no-replace-objects",
    "-c", "core.quotePath=false", "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null", "-c", "core.ignoreCase=false",
    "-c", "core.splitIndex=false", "-c", "core.ignoreStat=false",
    "-c", "commit.gpgSign=false", "-c", "push.gpgSign=false",
    "-c", "filter.lfs.smudge=", "-c", "filter.lfs.clean=",
    "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
)


def _native_git_modes(path, expected, problems):
    """このリポジトリ内だけ index のモードを調べる。成果物の一時領域は対象外。"""
    try:
        relative = path.relative_to(REPO)
    except ValueError:
        return
    if not os.path.lexists(REPO / ".git"):
        return
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), *_NATIVE_SAFE_GIT, "--literal-pathspecs",
             "ls-files", "--stage", "-z", "--", relative.as_posix()],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=15, check=True,
        )
        entries = {}
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, filename = record.split(b"\t", 1)
            mode, _oid, stage = metadata.decode("ascii").split()
            name = Path(os.fsdecode(filename)).relative_to(relative).as_posix()
            if stage != "0" or name in entries:
                problems.append(f"{name}: Git の記録が未解決または重複している")
            entries[name] = mode
        for name, executable in expected.items():
            mode = "100755" if executable else "100644"
            if entries.get(name) != mode:
                problems.append(f"{name}: Git のモードは {mode} が必要(実際: {entries.get(name)!r})")
        for name in sorted(entries.keys() - expected.keys()):
            problems.append(f"{name}: Git の記録に余分な項目がある")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        problems.append(f"Git のモードを読み取れない: {exc}")


def _native_files(path, expected, problems):
    """固定の木だけを nofollow で読む。FIFO・リンク・上限超過は開く前にも拒否する。"""
    contents = {}
    directories = {str(Path(name).parent) for name in expected if "/" in name}

    def visit(directory_fd, prefix):
        names = {name[len(prefix):].split("/", 1)[0] for name in expected if name.startswith(prefix)}
        actual = set(os.listdir(directory_fd))
        for name in sorted(actual - names):
            problems.append(f"{prefix}{name}: 余分な項目がある")
        for name in sorted(names):
            relative = prefix + name
            if name not in actual:
                problems.append(f"{relative}: 存在しない")
                continue
            child_fd = None
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                is_directory = relative in directories
                if stat.S_ISLNK(before.st_mode):
                    raise ValueError("シンボリックリンクは禁止")
                if is_directory:
                    if not stat.S_ISDIR(before.st_mode):
                        raise ValueError("通常のフォルダでない")
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    visit(child_fd, relative + "/")
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("通常ファイルでない")
                if before.st_nlink != 1:
                    raise ValueError("ハードリンクは禁止")
                if before.st_size > _NATIVE_LIMIT:
                    raise ValueError("8 MiB を超えている")
                child_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=directory_fd)
                current = os.fstat(child_fd)
                if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink, current.st_size) != (
                        before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size):
                    raise ValueError("読み取り前にファイルが変わった")
                if bool(current.st_mode & 0o111) != expected[relative]:
                    problems.append(f"{relative}: 実行ビットが不正")
                chunks = []
                total = 0
                while total <= _NATIVE_LIMIT:
                    chunk = os.read(child_fd, min(65536, _NATIVE_LIMIT + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > _NATIVE_LIMIT:
                    raise ValueError("8 MiB を超えている")
                data = b"".join(chunks)
                if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    raise ValueError("Git LFS のポインタは禁止")
                contents[relative] = data
            except (OSError, ValueError) as exc:
                problems.append(f"{relative}: {exc}")
            finally:
                if child_fd is not None:
                    os.close(child_fd)

    directory_fd = None
    try:
        # 配布フォルダより上の、リポジトリ内のリンクも拒否する。
        if path.is_relative_to(REPO):
            for parent in reversed(path.relative_to(REPO).parents):
                if stat.S_ISLNK((REPO / parent).lstat().st_mode):
                    raise ValueError(f"{parent}: シンボリックリンクは禁止")
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        visit(directory_fd, "")
    except (OSError, ValueError) as exc:
        problems.append(f"{path}: 配布フォルダを読み取れない: {exc}")
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return contents


def _native_binary_format(name, data, problems):
    """ELF と universal Mach-O のヘッダを読む。静的リンク・署名・起動は CI の責務。"""
    if name.startswith("linux-"):
        machine = 62 if name == _NATIVE_BINARIES[0] else 183
        if (len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"
                or struct.unpack_from("<H", data, 18)[0] != machine):
            problems.append(f"{name}: 対象 CPU の 64-bit little-endian ELF でない")
        return
    # fat_header / fat_arch(_64)。CPU 一覧だけの偽装を避け、各 slice のヘッダも読む。
    formats = {b"\xca\xfe\xba\xbe": (">", "IIIII"), b"\xbe\xba\xfe\xca": ("<", "IIIII"),
               b"\xca\xfe\xba\xbf": (">", "IIQQII"), b"\xbf\xba\xfe\xca": ("<", "IIQQII")}
    try:
        if len(data) < 8 or data[:4] not in formats:
            raise ValueError("universal Mach-O でない")
        endian, fields = formats[data[:4]]
        count = struct.unpack_from(endian + "I", data, 4)[0]
        entry_size = struct.calcsize(endian + fields)
        table_end = 8 + count * entry_size
        if count != 2 or table_end > len(data):
            raise ValueError("Mach-O の CPU は x86_64 と arm64 の 2 つが必要")
        cpus, ranges = set(), []
        for index in range(count):
            cpu, subtype, offset, size, align, *reserved = struct.unpack_from(
                endian + fields, data, 8 + index * entry_size)
            if cpu not in (0x01000007, 0x0100000c) or cpu in cpus:
                raise ValueError("Mach-O の CPU が不正または重複")
            if (size < 32 or offset < table_end or offset + size > len(data)
                    or align > 31 or offset % (1 << align) or any(reserved)
                    or any(offset < end and start < offset + size for start, end in ranges)):
                raise ValueError("Mach-O の CPU ごとの範囲が不正")
            if (data[offset:offset + 4] != b"\xcf\xfa\xed\xfe"
                    or struct.unpack_from("<II", data, offset + 4) != (cpu, subtype)):
                raise ValueError("Mach-O の中身と CPU の一覧が一致しない")
            cpus.add(cpu)
            ranges.append((offset, offset + size))
    except (ValueError, struct.error) as exc:
        problems.append(f"{name}: {exc}")


def _native_build_info(raw, hashes, problems):
    """build-info.json v1: targets[target]={linker,sdk}, ci={run_id,run_url},
    artifacts=[{path,sha256}, ...]。Linux の sdk は null を認める。
    追加の道具の版などは許すが、全階層で重複キーと JSON 外の定数は拒否する。
    """
    def error(message):
        problems.append(f"build-info.json: {message}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重複キー: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"JSON に使えない値: {value}")

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def sha256(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        error(f"JSON を読み取れない: {exc}")
        return
    if not isinstance(data, dict):
        error("JSON の最上位はオブジェクトが必要")
        return
    for key in ("schema_version", "source_commit", "source_input_sha256", "cargo_lock_sha256",
                "rustc", "targets", "ci", "artifacts"):
        if key not in data:
            error(f"必須キー {key} がない")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        error("schema_version は整数の 1 が必要")
    commit = data.get("source_commit")
    if not isinstance(commit, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is None:
        error("source_commit は完全な小文字の Git SHA が必要")
    for key in ("source_input_sha256", "cargo_lock_sha256"):
        if not sha256(data.get(key)):
            error(f"{key} は小文字の SHA256 が必要")
    if not text(data.get("rustc")):
        error("rustc は空でない文字列が必要")
    targets = data.get("targets")
    if not isinstance(targets, dict) or set(targets) != set(_NATIVE_TARGETS):
        error("targets は固定の 4 対象が必要")
    if isinstance(targets, dict):
        for target in _NATIVE_TARGETS:
            settings = targets.get(target)
            if not isinstance(settings, dict) or not text(settings.get("linker")):
                error(f"targets.{target}.linker は空でない文字列が必要")
            if (not isinstance(settings, dict) or "sdk" not in settings
                    or not (text(settings["sdk"]) or (target.endswith("linux-musl") and settings["sdk"] is None))):
                error(f"targets.{target}.sdk がないか不正")
    ci = data.get("ci")
    if not isinstance(ci, dict):
        error("ci は run_id と run_url を持つオブジェクトが必要")
    else:
        run_id = ci.get("run_id")
        if not ((type(run_id) is int and run_id > 0)
                or (isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]*", run_id))):
            error("ci.run_id は正の実行番号が必要")
        try:
            url = urlsplit(ci.get("run_url") if isinstance(ci.get("run_url"), str) else "")
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("https の絶対 URL が必要")
        except ValueError:
            error("ci.run_url は https の絶対 URL が必要")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        error("artifacts は 3 成果物の配列が必要")
        return
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            error("artifacts の項目は path と sha256 を持つオブジェクトが必要")
            continue
        path = artifact.get("path")
        if not isinstance(path, str) or path not in _NATIVE_BINARIES or path in seen:
            error(f"artifacts の path が不正または重複: {path!r}")
            continue
        seen.add(path)
        value = artifact.get("sha256")
        if not sha256(value):
            error(f"artifacts の {path} の sha256 が不正")
        elif path in hashes and hashes[path] != value:
            error(f"artifacts の {path} の SHA256 が成果物と一致しない")
    if seen != set(_NATIVE_BINARIES) or len(artifacts) != 3:
        error("artifacts は固定の 3 成果物がちょうど 1 個ずつ必要")


def check_native_bundle(path: Path, mode: str) -> list[str]:
    """未確認の配布物を実行せずに検査し、問題の文を返す(空なら合格)。

    artifact は CI の 5 ファイル、distribution は launch.sh を加えた 6 ファイル。
    このリポジトリ内では Git のモードも調べる。外の一時領域では実行ビットを調べる。
    現在の native/ のソースとは比較しない。ファイルと権限は変更しない。
    """
    if mode not in ("artifact", "distribution"):
        return [f"検査の mode が不正: {mode!r}"]
    path = Path(os.path.abspath(path))
    problems = []
    expected = {name: True for name in _NATIVE_BINARIES}
    expected.update({"SHA256SUMS": False, "build-info.json": False})
    if mode == "distribution":
        expected["launch.sh"] = True
    contents = _native_files(path, expected, problems)
    _native_git_modes(path, expected, problems)
    hashes = {}
    for name in _NATIVE_BINARIES:
        if name in contents:
            data = contents[name]
            hashes[name] = hashlib.sha256(data).hexdigest()
            _native_binary_format(name, data, problems)
    if "SHA256SUMS" in contents:
        pattern = b"".join(rb"[0-9a-f]{64}  " + re.escape(name.encode()) + b"\n"
                           for name in _NATIVE_BINARIES)
        data = contents["SHA256SUMS"]
        if re.fullmatch(pattern, data) is None:
            problems.append("SHA256SUMS: 固定の 3 行・小文字 SHA256・空白 2 個・LF の形でない")
        else:
            for name, line in zip(_NATIVE_BINARIES, data.splitlines()):
                if name in hashes and line[:64].decode("ascii") != hashes[name]:
                    problems.append(f"SHA256SUMS: {name} の SHA256 が成果物と一致しない")
    if "build-info.json" in contents:
        _native_build_info(contents["build-info.json"], hashes, problems)
    return problems


def check_native_distribution():
    """未配布なら無出力。リンク切れを『未配布』として見逃さない。"""
    path = REPO
    for part in _NATIVE_REL.parts:
        path /= part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            ERRORS.append(f"{path}: 配布フォルダを読み取れない: {exc}")
            return
        if not stat.S_ISDIR(mode):
            ERRORS.append(f"{path}: 配布フォルダはリンクでない通常のフォルダが必要")
            return
    ERRORS.extend(f"{_NATIVE_REL}: {problem}" for problem in check_native_bundle(path, "distribution"))


def check_skill_count_claims():
    """配布メタの skill 件数の表記が実数と一致するかを検査する(design.md §5)。

    対象は `marketplace.json` の `plugins[].description` と `plugin.json` の
    `description` だけ。`docs/design.md` は**対象にしない** —— そこの「12 skill」は
    測定の記録で、skill を増やすと正しい記録が ERROR になってしまう。
    件数の表記が見つからない場合は WARN(見つかったときだけ実数と突き合わせる)。
    1 検査 = 1 関数の構成規約に従い、JSON 以外を読みうるこの検査は
    check_json_files() に相乗りしない。"""
    if not SKILLS_DIR.is_dir():
        return
    actual = len([d for d in SKILLS_DIR.iterdir() if d.is_dir()])
    mp = REPO / ".claude-plugin" / "marketplace.json"
    pj = REPO / "plugins" / "dev-workflow" / ".claude-plugin" / "plugin.json"
    claims: list[tuple[str, str]] = []
    try:
        mp_data = json.loads(mp.read_text(encoding="utf-8"))
        for pl in mp_data.get("plugins", []):
            claims.append((f"marketplace.json plugins[{pl.get('name')}].description", str(pl.get("description") or "")))
    except Exception:
        pass
    try:
        pj_data = json.loads(pj.read_text(encoding="utf-8"))
        claims.append(("plugin.json description", str(pj_data.get("description") or "")))
    except Exception:
        pass
    for label, desc in claims:
        # 「skills 12 種」と「12 skills」の両方を拾う。素朴に最初の \d+ を取ると
        # plugin.json 側の「1 コマンド」を拾って偽 ERROR になる(実測)。
        found = {int(m.group(1) or m.group(2)) for m in _SKILL_COUNT_RE.finditer(desc)}
        if not found:
            WARNS.append(f"{label}: skill 件数の表記が見つからない(実数 {actual} 件)")
            continue
        for n in sorted(found):
            if n != actual:
                ERRORS.append(f"{label}: skill 件数の表記 {n} 件が実数 {actual} 件と一致しない")


def check_skills():
    if not SKILLS_DIR.exists():
        ERRORS.append(f"{SKILLS_DIR}: 存在しない")
        return
    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skill_dirs:
        ERRORS.append("skills が 1 つもない")
    for d in skill_dirs:
        md = d / "SKILL.md"
        md_ok = md.is_file()
        if not md_ok:
            ERRORS.append(f"{d.name}: SKILL.md がない")
        else:
            text = md.read_text(encoding="utf-8", errors="replace")
            # 論理行数 = 改行の数 + 末尾が改行で終わらなければ 1(design.md §6)。
            # 旧形の `count("\n") + 1` は末尾改行ありの 500 行を 501 行と数えて誤検出し、
            # `wc -l` に揃えると末尾改行なしの 501 行を 500 と数えて見逃す。
            lines = text.count("\n") + (0 if text.endswith("\n") else 1)
            if lines > 500:
                ERRORS.append(f"{d.name}/SKILL.md: {lines} 行(500 行以下の規約違反)")

            fm = parse_frontmatter(text, md)
            name = fm.get("name")
            desc = str(fm.get("description", "") or "")
            if not name:
                ERRORS.append(f"{d.name}/SKILL.md: frontmatter に name がない")
            elif name != d.name:
                ERRORS.append(f"{d.name}/SKILL.md: name '{name}' がディレクトリ名と不一致")
            if not desc:
                ERRORS.append(f"{d.name}/SKILL.md: frontmatter に description がない")
            else:
                if len(desc) > 1024:
                    ERRORS.append(f"{d.name}/SKILL.md: description {len(desc)} 字(上限 1024)")
                elif len(desc) < 150:
                    ERRORS.append(f"{d.name}/SKILL.md: description {len(desc)} 字(規約 150〜500。トリガー語句を足す)")
                elif len(desc) > 500:
                    ERRORS.append(f"{d.name}/SKILL.md: description {len(desc)} 字(規約 150〜500)")
            # skill の frontmatter でツールを制限・許可しない(design.md §6)。この鍵は skill を呼んだ後の
            # ターンの残りにも効き、呼び出し側の skill を止めるか、確認を飛ばす許可を足しうる(§7-3)
            for key in ("allowed-tools", "disallowed-tools"):
                if key in fm:
                    ERRORS.append(
                        f"{d.name}/SKILL.md: frontmatter に {key} がある(呼んだ後のターンの残りにも効き、"
                        "呼び出し側を止めるか確認を飛ばす許可を足しうる — design.md §6・§7-3。"
                        "ツールの制約は本文の原則で書く)"
                    )

        # 禁止パターン(SKILL.md と references/ scripts/ templates/ 全ファイル。
        # SKILL.md の有無に関わらず走る)
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.suffix in _IMAGE_EXTS:
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            body_lines = body.splitlines()
            for pat, why in FORBIDDEN_PATTERNS:
                for m in re.finditer(pat, body):
                    line = body.count("\n", 0, m.start()) + 1
                    line_text = body_lines[line - 1] if line <= len(body_lines) else ""
                    # 明示のマーカーがある行だけ免除する(design.md §5)。
                    # コードフェンスの内外は問わない(フェンスは免除の単位にしない)。
                    if _has_allow_marker(line_text):
                        continue
                    ERRORS.append(f"{f.relative_to(REPO)}:{line}: 禁止パターン [{why}] -> {m.group(0)!r}")

        # 相対リンクの存在(SKILL.md が使える場合はそれと、references/ 配下の md を検査する。
        # 禁止パターンと同じく SKILL.md の有無に関わらず走る。リンクは「そのファイルの位置」から
        # 解決する)
        md_files = ([md] if md_ok else []) + sorted(f for f in d.rglob("*.md") if f != md and f.is_file())
        for f in md_files:
            body = f.read_text(encoding="utf-8", errors="replace")
            scanned = _mask_code_fences(body)
            for m in LINK_RE.finditer(scanned):
                # fragment(`#sec`)を落としてパスだけを見る。落として空になるもの
                # (同一文書内アンカー `[x](#見出し)`)は検査しない。
                target = m.group(1).split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (f.parent / target).exists():
                    line = scanned.count("\n", 0, m.start()) + 1
                    ERRORS.append(f"{f.relative_to(REPO)}:{line}: リンク切れ -> {m.group(1)}")


# 人が読む文の書き方の正本(design.md §5-25)。値は SKILLS_DIR からの相対パス。
# 各 SKILL.md の `## 原則` の節が、ここをリンクで指す(design.md §6)。
_WRITING_RULES = "do-task/references/writing-for-people.md"
_PRINCIPLES_HEADING_RE = re.compile(r"^## 原則\s*$")


def _principles_section(body: str) -> Optional[str]:
    """SKILL.md の `## 原則` の節を返す。範囲は見出しの次の行から次の `## ` の行の前まで。
    節が無ければ None。

    コードフェンスの中身を空行にした写し(_mask_code_fences)から取る。フェンスの中の
    見出しは節の境目にならず、フェンスの中のリンクは数えない。"""
    lines = _mask_code_fences(body).split("\n")
    for i, line in enumerate(lines):
        if not _PRINCIPLES_HEADING_RE.match(line):
            continue
        section = []
        for rest in lines[i + 1 :]:
            if rest.startswith("## "):
                break
            section.append(rest)
        return "\n".join(section)
    return None


def check_writing_rules_link():
    """各 SKILL.md の `## 原則` の節に、人が読む文の書き方の正本(_WRITING_RULES)への
    リンクが 1 つ以上あるかを検査する(design.md §6)。

    リンクの取り出しはリンク検査(check_skills())と同じ LINK_RE を使い、fragment を落とした
    パスを SKILL.md の位置から解決して、正本の実パスと比べる。同じ名前の別のファイルを指す
    リンク・リンクの形でない言及・節の外のリンク・コードフェンスの中のリンクは数えない。
    SKILL.md の欠けは check_skills() が ERROR にするので、ここでは飛ばす。"""
    if not SKILLS_DIR.exists():
        return
    want = (SKILLS_DIR / _WRITING_RULES).resolve()
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        section = _principles_section(md.read_text(encoding="utf-8", errors="replace"))
        if section is None:
            ERRORS.append(
                f"{d.name}/SKILL.md: `## 原則` の節が無い(人が読む文の書き方の正本 {_WRITING_RULES} への"
                "リンクを置く節 — design.md §6)"
            )
            continue
        targets = (m.group(1).split("#", 1)[0] for m in LINK_RE.finditer(section))
        if not any(t and (md.parent / t).resolve() == want for t in targets):
            ERRORS.append(
                f"{d.name}/SKILL.md: `## 原則` の節に {_WRITING_RULES} へのリンクが無い"
                "(正本を指す 1 行をリンクの形で置く。リンクにしていない言及・節の外・コードフェンスの中は数えない"
                " — design.md §6)"
            )


# SKILL.md の本文の行の長さ(design.md §6)。上限を超える行は WARN にする(独自の skill を持つ fork を
# 止めない)。インラインのコードは 1 行の中の `…`(改行をまたがない)で、上限を超える長さのものだけを
# 数えから引く(コマンドの字面は分けられないため)。
_LINE_LENGTH_LIMIT = 200
_INLINE_CODE_RE = re.compile(r"`[^`]+`")


def _measured_line_length(line: str) -> int:
    """行の文字数から、_LINE_LENGTH_LIMIT を超えるインラインのコードの長さを引いた数を返す。"""
    long_code = sum(
        len(m.group(0)) for m in _INLINE_CODE_RE.finditer(line) if len(m.group(0)) > _LINE_LENGTH_LIMIT
    )
    return len(line) - long_code


def check_line_length():
    """各 SKILL.md の本文の行の長さを検査する(design.md §6)。WARN だけを出す。

    本文は frontmatter の閉じの行(parse_frontmatter() と同じく、2 行目以降で最初に `---` で
    始まる行)の次の行から。コードフェンスの中(_mask_code_fences が空行にする行)と、
    行頭の空白(半角の空白・タブ)を除いて `|` で始まる行(表の行)は数えない。
    行番号は SKILL.md の先頭からの番号。frontmatter が無い・閉じていない SKILL.md は
    check_skills() が ERROR にするので、ここでは飛ばす。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        lines = md.read_text(encoding="utf-8", errors="replace").split("\n")
        if not lines[0].startswith("---"):
            continue
        close = next((i for i in range(1, len(lines)) if lines[i].startswith("---")), None)
        if close is None:
            continue
        body_start = close + 1
        body = _mask_code_fences("\n".join(lines[body_start:])).split("\n")
        for offset, line in enumerate(body):
            if line.lstrip(" \t").startswith("|"):
                continue
            length = _measured_line_length(line)
            if length > _LINE_LENGTH_LIMIT:
                WARNS.append(
                    f"{md.relative_to(REPO)}:{body_start + offset + 1}: 行が {length} 字"
                    f"({_LINE_LENGTH_LIMIT} 字以下にする。{_LINE_LENGTH_LIMIT} 字を超えるインラインのコードは"
                    "数えない。1 文 1 つの下位の箇条書きに分ける — design.md §6)"
                )


def _in_delegation_scope(f: Path) -> bool:
    """委託の語検査の走査範囲を 1 式で判定する。skill 直下のファイル(画像以外)、または
    `references/` 配下の *.md(再帰)、または `scripts/` 配下の画像以外(再帰)で、除外 2 本
    (_EXEMPT_FILES)でない、の論理積。拡張子の絞り方が references/ と scripts/ で非対称な
    理由は design.md §7-7。許容リスト(①)は含めない —
    check_migration_allowlist_staleness() が①抜きで再利用するため。"""
    if not f.is_file():
        return False
    try:
        rel = f.relative_to(SKILLS_DIR)
    except ValueError:
        return False
    rest = rel.parts[1:]
    if not rest:
        return False
    is_skill_root = len(rest) == 1 and f.suffix not in _IMAGE_EXTS
    is_references_md = len(rest) > 1 and rest[0] == "references" and f.suffix == ".md"
    is_scripts_any = len(rest) > 1 and rest[0] == "scripts" and f.suffix not in _IMAGE_EXTS
    return (is_skill_root or is_references_md or is_scripts_any) and rel.as_posix() not in _EXEMPT_FILES


def _is_delegation_target(f: Path) -> bool:
    """委託の語検査の対象かどうかを 1 式で判定する(対象集合はこの述語だけで決まり、
    他の場所に追加の絞り込みを置かない)。①skill 名が許容リストに無く、かつ ②③(_in_delegation_scope)。"""
    try:
        skill = f.relative_to(SKILLS_DIR).parts[0]
    except (ValueError, IndexError):
        return False
    return skill not in _MIGRATION_ALLOWLIST and _in_delegation_scope(f)


def check_delegation_words():
    """design.md §7-7 の委託の語検査。check_skills() のループには相乗りせず、
    自前で SKILLS_DIR.iterdir() から skill ディレクトリを列挙する(check_skills() が
    SKILL.md 欠落時に行う continue を継承しないため)。既存の禁止パターンループ
    (免除規則・templates/ 走査)も流用せず、免除規則は持たない。
    ⚠ 既存の禁止パターン検査の挙動変更は別論点として扱い、この検査だけが同じ穴を
       継承しないようにする(スコープ外)。
    """
    if not SKILLS_DIR.exists():
        return
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        for f in sorted(d.rglob("*")):
            if not _is_delegation_target(f):
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            _reuse_file_result("delegation_words", f, body, (), lambda: _delegation_words_in(f, body))


def _delegation_words_in(f, body):
    for pat in _DELEGATION_WORDS:
        for m in re.finditer(pat, body):
            line = body.count("\n", 0, m.start()) + 1
            ERRORS.append(f"{f.relative_to(REPO)}:{line}: 委託の語 -> {m.group(0)!r}")


def _in_host_cli_scope(f: Path) -> bool:
    """ホスト CLI 語検査の走査範囲を 1 式で判定する(design.md §7-7-1)。
    `_in_delegation_scope()` と同じ判定のうち **`scripts/` だけを対象外にする**
    (scripts/ はモデル名を `--model` 引数等で外から受け取る実行層のアダプタで、
    CLI 名を持つことが仕事であるため。この差分だけが既存とのズレ)。skill 直下は
    既存と同じ「非画像ファイル全部」に揃える(`*.md` に絞らない — `README.md` のような
    レイアウト外のファイルに置くと到達条件をすり抜けるため。design.md §7-7-1(2026-09-17 決定 24))。
    除外 2 本(_EXEMPT_FILES)は委託の語検査と共有する。未移行 skill の許容リストは
    このカテゴリには存在しない(新設のため移行対象が無い)ので参照しない。"""
    if not f.is_file():
        return False
    try:
        rel = f.relative_to(SKILLS_DIR)
    except ValueError:
        return False
    rest = rel.parts[1:]
    if not rest:
        return False
    is_skill_root = len(rest) == 1 and f.suffix not in _IMAGE_EXTS
    is_references_md = len(rest) > 1 and rest[0] == "references" and f.suffix == ".md"
    return (is_skill_root or is_references_md) and rel.as_posix() not in _EXEMPT_FILES


def check_host_cli_words():
    """design.md §7-7-1 のホスト CLI 語検査。`check_delegation_words()` の
    構成(定数 → スコープ判定 → 検査関数)を踏襲するが、対象語(_HOST_CLI_WORDS)・
    スコープ(_in_host_cli_scope。scripts/ を含まない)が委託の語検査とは別である。
    **大小を無視する**(re.IGNORECASE)— _DELEGATION_WORDS 側は大小を区別したままで、
    この差は design.md §7-7-1 に明記する。
    **禁止パターン検査と同じ行単位のマーカー(_has_allow_marker)で免除できる**。
    委託の語検査・除外の不変条件は免除規則を持たない(この 2 つとの違い)。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        for f in sorted(d.rglob("*")):
            if not _in_host_cli_scope(f):
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            _reuse_file_result("host_cli_words", f, body, (), lambda: _host_cli_words_in(f, body))


def _host_cli_words_in(f, body):
    body_lines = body.splitlines()
    for pat in _HOST_CLI_WORDS:
        guidance = (
            _HOST_CLI_QUESTION_GUIDANCE if pat in _HOST_CLI_QUESTION_WORDS
            else _HOST_CLI_GUIDANCE
        )
        for m in re.finditer(pat, body, flags=re.IGNORECASE):
            line = body.count("\n", 0, m.start()) + 1
            # 正当に具体名を持つ行だけ、明示マーカーで免除する(design.md §5-24)。
            # ファイルごと除外にしないのは、その先で新しく混入しても捕まらなくなるため。
            if _has_allow_marker(body_lines[line - 1] if line <= len(body_lines) else ""):
                continue
            ERRORS.append(
                f"{f.relative_to(REPO)}:{line}: ホスト固有の CLI 語 -> {m.group(0)!r}"
                + guidance
            )


def check_delegation_map_invariant():
    """design.md §7-7 の除外の不変条件: 解決表(_DELEGATION_MAP)はモデルエイリアス名だけは
    自ら 0 件に保つ。語彙(_MODEL_ALIASES)と対象(_DELEGATION_MAP)は他の関数と共通の定義を
    参照し、ここで再列挙しない。分類できない(対象ファイルが無い)場合も PASS に倒さず
    ERROR にする。"""
    path = SKILLS_DIR / _DELEGATION_MAP
    if not path.is_file():
        ERRORS.append(f"{path.relative_to(REPO)}: 除外の不変条件の対象ファイルが無い")
        return
    body = path.read_text(encoding="utf-8", errors="replace")
    for word in _MODEL_ALIASES:
        for m in re.finditer(word, body):
            line = body.count("\n", 0, m.start()) + 1
            ERRORS.append(f"{path.relative_to(REPO)}:{line}: 除外の不変条件 -> {m.group(0)!r}")


def check_migration_allowlist_staleness():
    """design.md §7-7 の許容リストの陳腐化検出(逆検査)。
    (a) 許容リストに載っているが委託の語が実際は 0 件の skill → WARN(移行済みなのに残っている)
    (b) 許容リストに載っているが skill ディレクトリが実在しない → WARN(タイプミス・リネーム・
        削除の取り残し。許容リスト側から回さないと (b) はループに一度も現れない)
    (a) の件数は _is_delegation_target と同じ範囲判定(_in_delegation_scope。②③)を再利用し、
    独自の走査や除外の再実装はしない。"""
    for name in sorted(_MIGRATION_ALLOWLIST):
        d = SKILLS_DIR / name
        if not d.is_dir():
            WARNS.append(f"{name}: 許容リストの skill が実在しない")
            continue
        count = 0
        for f in sorted(d.rglob("*")):
            if not _in_delegation_scope(f):
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            for pat in _DELEGATION_WORDS:
                count += len(re.findall(pat, body))
        if count == 0:
            WARNS.append(f"{name}: 許容リストの skill に委託の語が無い")


# 置き換える語(design.md §6)。(検出する正規表現, 表示する語, 言い換え先)。
# 「枡」は「固定枡」などの中でも見つける。「出口」は一般語なので「git 出口」「git の出口」だけを探す。
# 「写し」は動詞「写す」の活用(写さ・写し・写す・写せ・写そ)と「複写」を探す。「丸写し」「筆写する」のような
# コピーの意味の複合語も探す。前に漢字が付く語は「〜写す」「〜写し」などの形だけを探し、名詞のまま
# 探すのは「複写」だけ(「筆写の手間」は探さない)。
# 前に _UTSUSHI_OTHER_KANJI(描・転・模・映・試・書・接・実・特・速・縮・透・活・投・連・盗・大)のどれかが付く語
# (一般の語として使う決まった語。描写・転写・模写・活写・大写しなど)と、「写真」「写像」「写る」は探さない。
# 一覧に無い漢字が前に付く語は、意味を問わず探す。探したくない語が新しく出たら一覧に足す。
# 「前置き」は「以前置き…」と、後ろが「置き換え」「置き場」になる形(「直前置き場」など)を探さない。
# 「フル段階」「標準段階」「最小段階」「段階判定」「実行段階」「結末の行」は、その字面だけを探す。「段階」だけ(「段階を下げて続ける」など)・
# 「実行の段階」・「結末」だけ(結末の値・`- 結末: …` の行)は、ふつうの日本語の語として探さない。
# 3 つの環境の古い名前は、太字の「**フル**」「**標準**」「**最小**」と、並び(「フル」か「標準」の後に区切り / ／ ・ と
# 「標準」か「最小」が続く形)も探す。区切りの無い「標準」(標準出力・標準の・標準・軽量など)は探さない。
# 「止めの印」「守る値」「対話点」「報告点」も字面だけを探す(「止める印」「控える値」は別の字面なので掛からない)。
# 「サイレント」はカタカナの字面だけを探す(サイレント縮退禁止・サイレントスキップ禁止などの規則の名前も含む)。
# #333 の語(レビュー役・実装役・まとめ役・検証役・試験・直読み・峻別・一時物・子起動・非ファイルタスク・退行・追随・起票)も
# 字面だけを探す。「全回帰」は、後ろが「テスト」の形(全回帰テスト)を探さない。
# 「検証器」は決まった行 `- 検証器記録:`(review-guard.py が書く)があるので探さない。
# #337 の語のうち、担保・打ち直・読込・書込・陽性対照・陰性対照・正常対照(間に「の」が入る形も)は字面だけを探す。
# #426 の「打つ」も字面だけを探す(打ち切る・打ち消す・打ち合わせ・打ち返す・打ち直す・壁打ち・抜き打ち・決め打ちは探さない)。
# 「倒す」は、判断や結果を寄せる比喩の形(「〜側に倒す」「保留に倒す」「33 に倒れる」「安全側にしか倒れない」「どちらにも倒れない」「保留には倒さない」など)だけを探し、
# 「を倒す」「が倒れる」のような、ふつうの意味の形は探さない。「素の」は前に漢字が付く語(要素の・酸素の・平素の)を探さない。
# 「群」「対照」(単独)「変異」「回帰」は、同じ字のふつうの意味や、決まった語(観点群・回帰の疑い・回帰検知条件)・
# 自己テストの出力の名前(変異 `x`)と字面で分けられないので探さない(design.md §6)。
_UTSUSHI_OTHER_KANJI = "描転模映試書接実特速縮透活投連盗大"
# 4 つ目の要素は、その語を定義する docs/design.md §6 の行(語の組)。定義の行で除くのは、その行の組の語だけ
_DOC = "- **文書の中で置き換える語**"
_FOUR = "- **置き換える語**"
_REPLACED_WORDS = (
    ("継承枠", "継承枠", "「継承の持ち場」または「モデルを指定しない持ち場」", _DOC),
    ("枡", "枡", "「持ち場」", _DOC),
    ("能力帯", "能力帯", "「性能の段」", _DOC),
    ("私有", "私有", "「専用の」", _DOC),
    ("素通り", "素通り", "「見逃される」「検査をすり抜ける」など文脈に合う言い方", _DOC),
    ("(?<![一-鿿])塞[ぐがぎげごい]", "塞ぐ(塞が・塞ぎ・塞い・塞げ・塞ご)", "「防ぐ」(経路が塞がるは「使えなくなる」、受理側を塞ぐは「受け付けないようにする」)", _DOC),
    ("(?i)git[ \t　]*(?:の[ \t　]*)?出口", "git 出口", "「commit・push・PR の受け持ち」", _DOC),
    (f"(?<![{_UTSUSHI_OTHER_KANJI}])写[さしすせそ]|複写", "写し(写す・写さ・写せ・写そ・複写)", "「コピー」「コピーする」(テストのためにわざと壊したものは「わざと壊したコピー」)", _DOC),
    ("宛て?先", "宛先", "「送る相手」「送る相手の名前」(改名先・ssh の接続先・push の送り先なら「改名先」「接続先」「送り先」)", _DOC),
    ("(?<!以)前置き(?![換場])", "前置き", "git に付けるものは「安全用のオプション」(コマンドの先頭に付ける `cd` は「先頭に付ける `cd`」、前に付く予約語なども意味に合う言い方)", _DOC),
    ("フル段階", "フル段階", "「双方向でやり取りできる環境」", _DOC),
    ("標準段階", "標準段階", "「一方向だけの環境」", _DOC),
    ("最小段階", "最小段階", "「別の AI を使えない環境」", _DOC),
    ("段階判定|実行段階", "段階判定・実行段階", "「やり取りの範囲の判定」", _DOC),
    ("結末の行", "結末の行", "「結果の行」(ファイルごとに初めて出す所に `無人のイテレーションの結果: …` の形を添える)", _DOC),
    (r"(?:フル|標準)[ \t]*[/／・][ \t]*(?:標準|最小)", "フル / 標準 / 最小(並び)",
     "「双方向でやり取りできる環境 / 一方向だけの環境 / 別の AI を使えない環境」", _DOC),
    (r"\*\*(?:フル|標準|最小)\*\*", "**フル**・**標準**・**最小**(太字の名前)",
     "「**双方向でやり取りできる環境**」「**一方向だけの環境**」「**別の AI を使えない環境**」", _DOC),
    ("サイレント", "サイレント",
     "「黙って〜しない」「知らせずに〜」の形で、何をしないかが分かる言い方(サイレント縮退禁止は「黙って内蔵に切り替えない」など、"
     "サイレントスキップ禁止は「黙って飛ばさない」、サイレント続行は「知らせずに続ける」)", _DOC),
    ("レビュー役", "レビュー役", "「レビュアー」", _DOC),
    ("実装役", "実装役", "「実装担当」", _DOC),
    ("まとめ役", "まとめ役", "「team-lead」(作業を進め、結果を確かめる側の AI)", _DOC),
    ("検証役", "検証役", "結果を確かめる側なら「team-lead」、変更をレビューする側なら「レビュアー」", _DOC),
    ("全回帰(?!テスト)", "全回帰", "「全回帰テスト」(初めて出す所に「回帰テストをすべて流すこと」を添える)", _DOC),
    ("試験", "試験",
     "自動のテストは「テスト」、実行時の小さな試しは「試し」、人が実機で確かめた実験は「実機での確認」、"
     "試験用の Issue は「テスト用の Issue」", _DOC),
    ("直読み", "直読み", "「(import を通さず)直接読む」(例: 「`AGENTS.md` を直接読めない環境」)", _DOC),
    ("峻別", "峻別", "「はっきり分ける」", _DOC),
    ("一時物", "一時物", "「一時ファイル」(フォルダも含むなら「一時ファイルやフォルダ」)", _DOC),
    ("子起動", "子起動", "「子プロセスとしての起動」", _DOC),
    ("非ファイルタスク", "非ファイルタスク", "「ファイルを持たないタスク」", _DOC),
    ("退行", "退行", "「後戻り」か、文脈に合う言い方(「使える機能が減る」「守りを外した版」など)", _DOC),
    ("追随", "追随", "「合わせる」", _DOC),
    ("起票", "起票", "「書き起こす」", _DOC),
    ("担保", "担保",
     "「保証する」「確かめる」「守らせる」「保つ」など文脈に合う言い方(読み取り専用を指示で担保する →「指示で守らせる」、"
     "品質を担保する →「品質を確かめる」)", _DOC),
    ("[にへ](?:しか|必ず|も|は)?倒[さしすせそれ]", "倒す・倒れる(〜に倒す・〜へ倒す・〜に倒れる)",
     "「〜にする」「〜と判定する」「〜と見なす」「〜になる」(保守側に倒す →「安全な側と判定する」、保留に倒す →「保留にする」)", _DOC),
    ("(?<![一-鿿])素の", "素の",
     "何が付いていないかを書く(素の `cd` →「`CDPATH=` を付けない `cd`」など、素の `reviewer` →「持ち場名を付けない `reviewer`」、"
     "素の言及 →「リンクにしていない言及」)", _DOC),
    ("打ち?直", "打ち直す", "「もう一度実行する」(名詞は「再実行」)", _DOC),
    ("(?<!壁)(?<!抜き)(?<!決め)打(?:[たつてっと]|ち(?![切消合返直]))", "打つ(打た・打ち・打っ・打て・打と)",
     "「実行する」(人が引数を書く意味は「入力する」、文の形に合わないときは「行う」)", _DOC),
    ("読込|書込", "読込・書込", "「読み込み」「書き込み」(送り仮名を省かない)", _DOC),
    ("(?:陽性|陰性|正常)(?:の)?対照", "陽性対照・陰性対照・正常対照",
     "「比べるための起動」「比べるためのケース」など文脈に合う言い方(効くはずのものが効くかを見る起動・拒否されるはずの構成・"
     "止めてはいけない正常な例)", _DOC),
    ("止めの印", "止めの印", "「止める印」", _FOUR),
    ("守る値", "守る値", "「控える値」", _FOUR),
    ("対話点", "対話点", "「人に確かめる場面」(対話点番号は「人に確かめる場面の番号」)", _FOUR),
    ("報告点", "報告点", "「判断を仰ぐ点」", _FOUR),
)
# docs/design.md の §6 で語を定義する行の行頭。それぞれちょうど 1 行であること(0 行・2 行以上は ERROR)
_REPLACED_WORDS_DEFINITION_PREFIXES = (_DOC, _FOUR)
_REPLACED_WORDS_EXEMPT = SKILLS_DIR / "do-task" / "references" / "writing-for-people.md"


def check_replaced_words():
    """design.md §6 の置き換える語(枡・継承枠・能力帯・私有・素通り・塞ぐ・git 出口・写し・宛先・前置き・
    フル段階・標準段階・最小段階・段階判定・実行段階・結末の行・サイレント・レビュー役・実装役・まとめ役・検証役・
    全回帰・試験・直読み・峻別・一時物・子起動・非ファイルタスク・退行・追随・起票・担保・倒す・素の・打ち直す・打つ・読込・書込・
    陽性対照・陰性対照・正常対照・止めの印・守る値・対話点・報告点)の検査。
    skill の文書と docs/design.md が対象。
    言い換え表(writing-for-people.md)と、docs/design.md の定義の行だけを除く(免除の印は使わない)。
    定義の行で除くのは、その行で定義する語の組だけ。定義の行が design にちょうど 1 行ずつでなければ ERROR。"""
    targets = []
    if SKILLS_DIR.exists():
        for f in sorted(SKILLS_DIR.rglob("*")):
            if not f.is_file() or f == _REPLACED_WORDS_EXEMPT:
                continue
            rel = f.relative_to(SKILLS_DIR).parts
            if f.suffix == ".md" or (len(rel) > 2 and rel[1] == "templates"):
                targets.append(f)
    design = REPO / "docs" / "design.md"
    if design.is_file():
        targets.append(design)
    for f in targets:
        text = f.read_text(encoding="utf-8", errors="replace")
        _reuse_file_result("replaced_words", f, text, (), lambda: _replaced_words_in(f, text, f == design))


def _replaced_words_in(f, text, is_design):
    lines = text.split("\n")
    if is_design:
        for prefix in _REPLACED_WORDS_DEFINITION_PREFIXES:
            found = [no for no, line in enumerate(lines, 1) if line.startswith(prefix)]
            if len(found) != 1:
                where = "・".join(str(no) for no in found) or "なし"
                ERRORS.append(
                    f"{f.relative_to(REPO)}: 置き換える語の定義の行「{prefix}」が {len(found)} 行(行: {where})。"
                    "ちょうど 1 行にする(design.md §6)"
                )
    for no, line in enumerate(lines, 1):
        own = None
        if is_design:
            own = next((p for p in _REPLACED_WORDS_DEFINITION_PREFIXES if line.startswith(p)), None)
        hits = []
        rest = line
        for pattern, word, repl, group in _REPLACED_WORDS:
            if group == own:
                # 定義の行では、その行で定義する語だけを除く(ほかの組の語は探す)
                rest = re.sub(pattern, "", rest)
                continue
            if re.search(pattern, rest):
                hits.append(f"{word!r} -> {repl}")
                rest = re.sub(pattern, "", rest)
        for h in hits:
            ERRORS.append(f"{f.relative_to(REPO)}:{no}: 置き換える語 {h}(design.md §6)")


# 結果の行・保留の行の理由欄だけを調べる。参照の番号へ検査を広げない(#314)。
_REPORT_NUMBER = r"(?:[DSUGMH][0-9]+|(?:決定|軸)[ \t]*[0-9]+|(?:条件[ \t]*)?[①-⑳]|§[0-9]+(?:-[0-9]+)*)"
_BARE_REPORT_REASON = (
    re.compile(
        r"無人のイテレーションの結果: (?:PR|縮退|保留|失敗扱い|候補なし) — "
        rf"(?P<number>{_REPORT_NUMBER})(?:[ \t]*[:：])?[ \t]*(?=$|[`\"')）」』。、,*\]|]|<!--)"
    ),
    re.compile(
        r"- \*\*保留\*\*\(ship-task・(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|YYYY-MM-DD)\): "
        rf"(?P<number>{_REPORT_NUMBER})(?:[ \t]*[:：])?"
        r"(?:[ \t]+—(?:[ \t]|$)|[ \t]*(?=$|[`)]))"
    ),
)


def check_number_only_reasons():
    """人に出す定型行の例で、理由・停止条件を番号だけで済ませていないかを確かめる。"""
    targets = []
    if SKILLS_DIR.exists():
        for f in sorted(SKILLS_DIR.rglob("*")):
            rel = f.relative_to(SKILLS_DIR).parts
            if f.is_file() and (f.suffix == ".md" or (len(rel) > 2 and rel[1] == "templates")):
                targets.append(f)
    targets.extend(f for f in (REPO / "docs" / "design.md", REPO / "README.md") if f.is_file())
    for f in targets:
        text = f.read_text(encoding="utf-8", errors="replace")
        _reuse_file_result("number_only_reasons", f, text, (), lambda: _number_only_reasons_in(f, text))


def _number_only_reasons_in(f, text):
    for no, line in enumerate(text.split("\n"), 1):
        for pattern in _BARE_REPORT_REASON:
            for match in pattern.finditer(line):
                ERRORS.append(
                    f"{f.relative_to(REPO)}:{no}: 番号だけの理由 -> {match['number']!r}。"
                    "結果の行の理由・保留の行の停止条件は「番号: 中身」で書く(design.md §6)"
                )


# 検査 13〜15。語彙は design §6 の表だけに置く。名前と報告の型は JSON で限定する。
_WORDING_EXEMPT = Path('do-task/references/writing-for-people.md')
_WORDING_GAP = '\ufffc'
# 表の語の形。英字と - だけの語は前後の英数字を見て数え、「X(Y)」の語は X と Y のどちらにも当てる。
# 語そのものは表から読み、ここには持たない。
_WORDING_ASCII = re.compile(r'[A-Za-z][A-Za-z-]*')
_WORDING_ALIAS = re.compile(r'([^()]+)\(([^()]+)\)')
# 語の直後のし・す・さ・せ・そは、動詞として使う形(縮退する・縮退して続ける など)。
_WORDING_VERB = '[しすさせそ]'
# リンク・文字参照・HTML の書き方の字。登録では拒み、本文で許す形の隣にあれば隠さない(検出する側に倒す)。
_WORDING_MARKUP = r'[\[\]&<>]'
# 動詞を探すときに挟んでよい改行。同じ段落・同じ項目の中の 1 つまで(1 文を途中で改行した書き方)。
# 空行は改行が 2 つなので越えない。次の行の印のうち `-`・`+`・`1.`・`#` は読み飛ばす字でないので、そこで止まる。
# 読み飛ばす字と重なる `*` の箇条書きと、検出する側の字 `>` の引用だけを、ここで別の項目とみなす(字下げも見る)。
_WORDING_NEXT_LINE = r'(?:\r?\n(?![ \t]*(?:\*[ \t]|>)))?'


def wording_targets():
    """skill 直下の SKILL.md と references の Markdown だけを列挙する。"""
    return sorted(
        f for f in SKILLS_DIR.rglob('*.md') if f.is_file()
        and f.relative_to(SKILLS_DIR) != _WORDING_EXEMPT
        and (len(f.relative_to(SKILLS_DIR).parts) == 2 and f.name == 'SKILL.md'
             or len(f.relative_to(SKILLS_DIR).parts) >= 3
             and f.relative_to(SKILLS_DIR).parts[1] == 'references')
    )


def read_wording_terms():
    """27 語の表が一意で、全行が有効なときだけ語と言い換えを返す。"""
    path = REPO / 'docs/design.md'
    try:
        body = path.read_text(encoding='utf-8')
        sections = re.findall(r'^## 6\. [^\n]*\n(.*?)(?=^## |\Z)', body, re.M | re.S)
        if len(sections) != 1:
            raise ValueError('§6 はちょうど 1 節必要')
        tables = re.findall(r'^\| 語 \| 言い換えの字面 \|\n\|[-| :]+\|\n((?:\|[^\n]*\|\n?)+)', sections[0], re.M)
        if len(tables) != 1:
            raise ValueError('表はちょうど 1 つ必要')
        terms = {}
        for row in tables[0].splitlines():
            cells = [cell.strip() for cell in row.strip('|').split('|')]
            if len(cells) != 2 or not all(cells):
                raise ValueError('語と言い換えの空欄・列数の誤り')
            word, explanation = cells
            if ('(' in word or ')' in word) and not _WORDING_ALIAS.fullmatch(word):
                raise ValueError(f'括弧のある語は「X(Y)」の形にする: {word}')
            if word in terms or explanation in terms.values():
                raise ValueError(f'語または言い換えの重複: {word}')
            terms[word] = explanation
        if len(terms) != 27:
            raise ValueError(f'27 語必要(実際は {len(terms)} 語)')
        return terms
    except (OSError, UnicodeError, ValueError) as exc:
        ERRORS.append(f'docs/design.md: 27 語の表が不正: {exc}')
        return None


def _wording_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'JSON のキーの重複: {key}')
        result[key] = value
    return result


def _wording_literal(text):
    """一覧の字面を比較するときだけコードと強調の印を除く。"""
    return text.replace('`', '').replace('*', '').replace('_', '')


def _wording_bare_reduced(text):
    # 括弧や引用符を付けただけの単独語も、名前として登録できない。
    return _wording_literal(text).strip(' \t\r\n\'"「」『』〈〉《》【】（）()[]') == '縮退'


def _wording_pattern(word):
    """表の字面から語の型を作る(語のコピーを持たない)。"""
    alias = _WORDING_ALIAS.fullmatch(word)
    if alias:
        parts = sorted(alias.groups(), key=len, reverse=True)
        return '(?:' + '|'.join(_wording_pattern(part.strip()) for part in parts) + ')'
    if _WORDING_ASCII.fullmatch(word):
        return rf'(?<![A-Za-z0-9_-]){re.escape(word)}(?![A-Za-z0-9_-])'
    return re.escape(word)


def _wording_mark(mark):
    # 数字の印(終了値の番号)は、前後が数字でない所だけに当てる(32 が 332 に当たらない)。
    return rf'(?<![0-9]){re.escape(mark)}(?![0-9])'


def _wording_status_names(line):
    """定義の行の状態名: `**…**` に囲まれた語と、` / ` で区切った並びの各項目(先頭の番号を除く)。"""
    names = {_wording_literal(bold).strip() for bold in re.findall(r'\*\*(.+?)\*\*', line)}
    for cell in line.split('|'):
        if ' / ' in cell:
            names.update(re.sub(r'^[0-9]+\s*', '', _wording_literal(item).strip()).strip()
                         for item in cell.split(' / '))
    return names


def _wording_mark_kind(mark, definition_line):
    """印の種類を絞る: 数字だけの終了値の番号・①〜⑳・定義の行の状態名(2 字以上)。"""
    if re.fullmatch('[0-9]+', mark) or re.fullmatch('[①-⑳]', mark):
        return True
    return len(mark) >= 2 and mark in _wording_status_names(definition_line)


def read_wording_exceptions(targets, terms):
    """全ファイル免除や正規表現を受け付けず、実在する字面だけを許す。"""
    path = REPO / 'scripts/wording-exceptions.json'
    allowed = {f.relative_to(REPO).as_posix() for f in targets}
    result = {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_wording_json_pairs)
        if not isinstance(data, dict) or set(data) != {'version', 'names', 'reports'} or type(data['version']) is not int or data['version'] != 1:
            raise ValueError('version: 1 と names・reports の配列が必要')
        seen_names, seen_forms = set(), set()
        for kind in ('names', 'reports'):
            if not isinstance(data[kind], list):
                raise ValueError(f'{kind} は配列が必要')
            for entry in data[kind]:
                required = {'name', 'definition', 'files', 'forms', 'reason'}
                if not isinstance(entry, dict) or not required <= set(entry) <= required | {'short'}:
                    raise ValueError(f'{kind}: name・definition・files・forms・reason が必要(short は任意)')
                for key in ('name', 'reason'):
                    if not isinstance(entry[key], str) or not entry[key].strip():
                        raise ValueError(f'{key} は空でない文字列が必要')
                name = entry['name']
                literal_name = _wording_literal(name)
                if _wording_bare_reduced(name) or (kind, literal_name) in seen_names:
                    raise ValueError(f'単独の縮退または名前の重複: {name}')
                seen_names.add((kind, literal_name))
                protected = [word for word in [*terms, '縮退']
                             if re.search(_wording_pattern(word), literal_name)]
                if not protected:
                    raise ValueError(f'{name}: 名前は検査する語の全文を含める')
                definition = entry['definition']
                if not isinstance(definition, dict) or set(definition) != {'path', 'text'}:
                    raise ValueError(f'{name}: 定義元の path・text が必要')
                source = definition['path']
                if (not isinstance(source, str) or not (
                        source in allowed | {'plugins/dev-workflow/skills/do-task/references/writing-for-people.md'}
                        or source.startswith('docs/') and Path(source).suffix == '.md')
                        or '..' in Path(source).parts):
                    raise ValueError(f'{name}: 定義元のパスが範囲外')
                definition_path = REPO / source
                if not definition_path.resolve().is_relative_to(REPO.resolve()):
                    raise ValueError(f'{name}: 定義元がリポジトリの外')
                literal = definition['text']
                # 定義の実在は字面の部分一致で見る。見出し・太字かは見ない(限界。design §6)。
                source_text = definition_path.read_text(encoding='utf-8')
                if not isinstance(literal, str) or not literal.strip() or literal not in source_text:
                    raise ValueError(f'{name}: 定義の字面が存在しない')
                if literal_name not in _wording_literal(literal):
                    raise ValueError(f'{name}: 定義に名前の字面がない')
                start = source_text.index(literal)
                end = source_text.find('\n', start + len(literal))
                definition_line = source_text[source_text.rfind('\n', 0, start) + 1:None if end < 0 else end]
                short = entry.get('short')
                if short is not None:
                    # 名前の字面を含まない許す形を、項目ごとに明示して認める(design §6)。
                    # 短い呼び名は名前の中の検査語 1 つ。印は定義の行に在る終了値の番号・①〜⑳・状態名だけ。
                    if not isinstance(short, dict) or set(short) != {'text', 'marks'}:
                        raise ValueError(f'{name}: short は text・marks が必要')
                    short_text, marks = short['text'], short['marks']
                    if short_text not in [*terms, '縮退'] or not re.search(_wording_pattern(short_text), literal_name):
                        raise ValueError(f'{name}: short の text は、名前に含まれる検査語 1 つにする')
                    if (not isinstance(marks, list) or not marks
                            or any(not isinstance(m, str) or not m.strip() or '\n' in m or '\r' in m for m in marks)
                            or len(marks) != len(set(marks))):
                        raise ValueError(f'{name}: short の marks は空・改行・重複のない文字列の配列が必要')
                    for mark in marks:
                        if not _wording_mark_kind(mark, definition_line):
                            raise ValueError(f'{name}: 印は終了値の番号・①〜⑳・定義の行の状態名(2 字以上)に限る: {mark}')
                        if not re.search(_wording_mark(mark), _wording_literal(definition_line)):
                            raise ValueError(f'{name}: 印が定義の行に無い: {mark}')
                        if any(re.search(_wording_pattern(word), mark) for word in [*terms, '縮退']):
                            raise ValueError(f'{name}: 印に検査語がある: {mark}')
                for key in ('files', 'forms'):
                    values = entry[key]
                    if (not isinstance(values, list) or not values
                            or any(not isinstance(v, str) or not v.strip() or '\n' in v or '\r' in v for v in values)
                            or len(values) != len(set(values))):
                        raise ValueError(f'{name}: {key} は空・改行・重複のない文字列の配列が必要')
                if any(f not in allowed for f in entry['files']):
                    raise ValueError(f'{name}: 使うファイルが検査の範囲外または存在しない')
                contents = {f: (REPO / f).read_text(encoding='utf-8') for f in entry['files']}
                for form in entry['forms']:
                    literal_form = _wording_literal(form)
                    if _wording_bare_reduced(form):
                        raise ValueError(f'{name}: 縮退だけを許す形は不可')
                    # 表示の字面が元の字面と変わる書き方は、隣の字を見る検査をすり抜けるので登録しない。
                    if re.search(_WORDING_MARKUP, form):
                        raise ValueError(f'{name}: 許す形にリンク・文字参照・HTML の書き方(`[` `]` `&` `<` `>`)は使えない: {form}')
                    # 動詞の使い方(縮退する・縮退して続ける)は design §6 が書き換えを求めるので登録しない。
                    # 名前全体を含む形でも見る: short の呼び名の直後と、末尾が「縮退」の名前の直後。
                    verb_before = [re.escape(literal_name)] if literal_name.endswith('縮退') else []
                    if short is not None:
                        verb_before.append(_wording_pattern(short_text))
                    # 空白・全角空白を挟んだ動詞(32 縮退 する)も同じに扱う。
                    if any(re.search(before + r'[\s　]*' + _WORDING_VERB, literal_form) for before in verb_before):
                        raise ValueError(f'{name}: 短い呼び名や名前の末尾の語を動詞で使う形は不可: {form}')
                    # 名前全体との一致にも数字の境目を当てる(32 縮退 が 132 縮退 に当たらない)。
                    name_pattern = _wording_mark(literal_name)
                    if re.search(name_pattern, literal_form):
                        rest = re.sub(name_pattern, _WORDING_GAP, literal_form)
                    elif short is not None:
                        # 短い呼び名の形: 呼び名をちょうど 1 つと、印のどれかを含める。
                        short_pattern = _wording_pattern(short_text)
                        if len(re.findall(short_pattern, literal_form)) != 1:
                            raise ValueError(f'{name}: 短い呼び名をちょうど 1 つ含める: {form}')
                        if not any(re.search(_wording_mark(mark), literal_form) for mark in marks):
                            raise ValueError(f'{name}: 短い呼び名の形に印が無い: {form}')
                        rest = re.sub(short_pattern, _WORDING_GAP, literal_form)
                    else:
                        raise ValueError(f'{name}: 許す形に名前の字面がない: {form}')
                    if any(re.search(_wording_pattern(word), rest) for word in [*terms, '縮退']):
                        raise ValueError(f'{name}: 許す形に名前以外の検査語がある: {form}')
                    if not any(form in body for body in contents.values()):
                        raise ValueError(f'{name}: 許す形が存在しない: {form}')
                    for f in entry['files']:
                        if (f, form) in seen_forms:
                            raise ValueError(f'{name}: 許す形の重複: {f}: {form}')
                        seen_forms.add((f, form))
                        result.setdefault(f, {})[form] = protected
        return result
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        ERRORS.append(f'scripts/wording-exceptions.json: 言葉の除外一覧が不正: {exc}')
        return None


def wording_markdown():
    """Markdown の構文を解釈し、各 inline token に元の行を残す。"""
    try:
        from markdown_it import MarkdownIt
        from markdown_it.parser_inline import _rules
        md = MarkdownIt('commonmark')
        # 強調の処理後に text を結合すると、改行をまたぐリンクの後の行番号を失う。
        md.inline.ruler2.disable('fragments_join')
        md.core.ruler.disable('text_join')

        def located(rule):
            def wrapped(state, silent):
                start, count = state.pos, len(state.tokens)
                if not rule(state, silent):
                    return False
                if not silent:
                    if state.pending:
                        state.pushPending()
                    for token in state.tokens[count:]:
                        token.meta.setdefault('wording_line', state.src.count('\n', 0, start))
                        if token.type == 'code_inline':
                            n = len(token.markup)
                            token.meta.setdefault('wording_raw', state.src[start + n:state.pos - n])
                return True
            return wrapped

        for name, rule in _rules:
            md.inline.ruler.at(name, located(rule))
        return md
    except Exception as exc:
        command = (f'{shlex.quote(sys.executable)} -m pip install -r '
                   f'{shlex.quote(str(REPO / "requirements-dev.txt"))}')
        ERRORS.append(f'Markdown の解析に必要な markdown-it-py を利用できない: {exc!r}。'
                      f'検証用の依存を導入・修復してください: {command}')
        return None


def wording_body(body, md, roles):
    """本文の (行番号, 表示文字列) を返す。見出し・コード・リンク先は数えない。"""
    # frontmatter は改行を残して除く。references でも同じ形だけを扱う。
    body = re.sub(r'\A---[ \t]*\n.*?^(?:---|\.\.\.)[ \t]*(?:\n|$)',
                  lambda m: '\n' * m[0].count('\n'), body, count=1, flags=re.M | re.S)
    result = {}

    def add_children(children, base):
        for token in children:
            line = base + token.meta.get('wording_line', 0)
            if token.type == 'code_inline':
                raw = token.meta['wording_raw']
                text = raw if '\n' in raw else token.content if token.content in roles else _WORDING_GAP
            elif token.type in ('text', 'text_special'):
                # 文字参照の生成改行は、元ファイルの行を増やさず語の間を分ける。
                text = token.content.replace('\n', _WORDING_GAP)
            elif token.type == 'html_inline':
                text = _WORDING_GAP if token.content.startswith('<!--') else ''
            elif token.type == 'image':
                # 画像の説明は本文。リンク先だけを除く。
                add_children(token.children or [], line)
                continue
            else:
                continue
            for offset, part in enumerate(text.split('\n')):
                result.setdefault(line + offset, []).append(part)

    env = {}
    tokens = md.parse(body, env)
    heading = False
    for token in tokens:
        if token.type == 'heading_open':
            heading = True
        elif token.type == 'heading_close':
            heading = False
        elif token.type == 'inline' and not heading:
            add_children(token.children or [], token.map[0] + 1)
        elif token.type == 'html_block':
            # 属性内の <!-- と本当のコメントを区別する。コメントの後半の本文は残す。
            class BodyHTML(HTMLParser):
                def handle_data(self, data):
                    start = token.map[0] + self.getpos()[0]
                    for offset, part in enumerate(data.split('\n')):
                        result.setdefault(start + offset, []).append(part)

                def handle_comment(self, data):
                    start = token.map[0] + self.getpos()[0]
                    result.setdefault(start, []).append(_WORDING_GAP)

                def handle_entityref(self, name):
                    self.handle_data(unescape(f'&{name};').replace('\n', _WORDING_GAP))

                def handle_charref(self, name):
                    self.handle_data(unescape(f'&#{name};').replace('\n', _WORDING_GAP))

            content = token.content
            cut = _wording_unclosed_comment(content)
            if cut is not None:
                # 閉じていないコメントの後ろを、こちらで空白に置き換える。
                # close() がその後ろを本文として渡すかは Python の版で違うため、parser に任せない。
                content = content[:cut] + _WORDING_GAP + re.sub(r'[^\n]', ' ', content[cut + 1:])
            parser = BodyHTML(convert_charrefs=False)
            parser.feed(content)
            parser.close()
    return [(line, ''.join(parts)) for line, parts in sorted(result.items())]


def _wording_unclosed_comment(content):
    """後ろに --> が無い <!-- のうち、タグの外にある最初の位置を返す。"""
    end = content.rfind('-->')
    start = content.find('<!--', end + 3 if end >= 0 else 0)
    while start >= 0:
        # 手前だけを読ませ、全部を読み終えたならタグの外(属性の値の中ではない)。
        probe = HTMLParser(convert_charrefs=False)
        probe.feed(content[:start])
        line, column = probe.getpos()
        if (line, column) == (content.count('\n', 0, start) + 1, start - content.rfind('\n', 0, start) - 1):
            return start
        start = content.find('<!--', start + 1)
    return None


def _wording_close(text, start):
    pairs = {'(': ')', '〈': '〉'}
    stack = []
    for pos in range(start, len(text)):
        char = text[pos]
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return pos
    return None


def _wording_explained(text, match, explanation):
    tail = re.match(r'[一-鿿々ァ-ヶーA-Za-z0-9]{0,8}([(〈])', text[match.end():])
    if tail:
        start = match.end() + tail.end() - 1
        close = _wording_close(text, start)
        if close is not None and text[start + 1:close].startswith(explanation):
            return True
    # 逆順は「説明(語)」だけ。間の空白・閉じ忘れ・種類違いは認めない。
    if match.start() and text[match.start() - 1] in '(〈':
        start = match.start() - 1
        close = _wording_close(text, start)
        if close == match.end() and text[:start].endswith(explanation):
            return True
    return False


def _wording_form_pattern(form):
    """本文で許す形を探す型。後ろの境目を見る(前の境目は _wording_form_starts_ok)。

    Markdown の元の字面で隣の字を見る。強調・コードの印はいくつ挟んでも飛ばす。
    リンク・文字参照・HTML の書き方が隣にあれば、表示の字面が分からないので隠さない。
    """
    literal = _wording_literal(form)
    pattern = re.escape(form)
    # 数字で終わる形は、後ろが数字でない所だけを隠す(縮退の 32 が縮退の 320 に当たらない)。
    if re.search('[0-9]$', literal):
        pattern += r'(?![*_`]*(?:[0-9]|' + _WORDING_MARKUP + '))'
    # 末尾が「縮退」の形は、直後に動詞の活用が続く所(縮退する など)を隠さない。
    if literal.endswith('縮退'):
        # 強調・コードの印・空白・全角空白と、同じ段落・同じ項目の中の改行 1 つを挟んだ動詞も見る。
        # 数字の先読みには足さない(32 0 は別の数のため)。
        pattern += (r'(?![*_`\t 　]*' + _WORDING_NEXT_LINE + r'[*_`\t 　]*(?:'
                    + _WORDING_VERB + '|' + _WORDING_MARKUP + '))')
    return pattern


def _wording_form_starts_ok(form, match):
    """数字で始まる形は、前が数字でない所だけを隠す(32 縮退 が 132 縮退 に当たらない)。

    前の強調・コードの印は字数によらず飛ばす。文字参照・HTML・リンクの終わり(`;` `>` `)` `]`)が前にあれば隠さない。
    """
    if not re.match('[0-9]', _wording_literal(form)):
        return True
    before = match.string[:match.start()].rstrip('*_`')
    return not re.search(r'[0-9;>)\]]$', before)


# 引用の行頭: 字下げ → 箇条書きの印(あれば)→ `>` と後ろの空白(0〜3 字)の並び。`>` の数が引用の深さ。
_WORDING_QUOTE_LINE = re.compile(r'[ \t]*(?:(?:[-*+]|[0-9]+[.)])[ \t]+)*((?:>[ \t]{0,3})+)')
# 次の行は箇条書きの印を許さない(印があれば別の項目)。
_WORDING_QUOTE_NEXT = re.compile(r'[ \t]*((?:>[ \t]{0,3})+)')


def _wording_quote_verb_follows(form, match):
    """引用の中の許す形は、同じか浅い深さの次の行の頭を読み飛ばし、同じ段落の続きの動詞を探す(design §6)。

    許す形のある行が引用(箇条書きの項目の中の引用 `- > …`・`1. > …` も含む)のときだけ見る。
    次の行の引用の深さ(`>` の数)が同じか浅いときは越える(浅い行は Markdown では印を省いた同じ段落の続き)。
    深い行(新しい入れ子の引用が段落を区切る)・箇条書きの印がある行・`>` だけの行(引用の中の空行)・
    引用の中の `*` の箇条書きは、別の段落・項目として越えない。
    """
    if not _wording_literal(form).endswith('縮退'):
        return False
    text = match.string
    here = _WORDING_QUOTE_LINE.match(text, text.rfind('\n', 0, match.start()) + 1, match.start())
    after = re.match(r'[*_`\t 　]*\r?\n', text[match.end():])
    if not here or not after:
        return False
    following = _WORDING_QUOTE_NEXT.match(text, match.end() + after.end())
    if not following or following[1].count('>') > here[1].count('>'):
        return False
    rest = text[following.end():]
    if re.match(r'[ \t]*\*[ \t]', rest):
        return False
    return bool(re.match(r'[*_`\t 　]*(?:' + _WORDING_VERB + '|' + _WORDING_MARKUP + ')', rest))


def check_wording():
    """初出の説明・説明の重複・名前以外の縮退を ERROR にする。"""
    terms = read_wording_terms()
    targets = wording_targets()
    if terms is None:
        return
    exceptions = read_wording_exceptions(targets, terms)
    md = wording_markdown()
    if exceptions is None or md is None:
        return
    # 英字の語は、その語だけのインラインのコードの中も数える(design §6)。語は表から導く。
    roles = {word for word in terms if _WORDING_ASCII.fullmatch(word)}
    for f in targets:
        body = f.read_text(encoding='utf-8', errors='replace')
        forms = exceptions.get(f.relative_to(REPO).as_posix(), {})
        # 結果を左右する入力: 語と言い換え・このファイルの許す形と隠す語・Markdown の解析器・英字の語
        inputs = (tuple(terms.items()), tuple((form, tuple(words)) for form, words in forms.items()),
                  type(md), tuple(sorted(roles)))
        _reuse_file_result('wording', f, body, inputs,
                           lambda: _wording_in(f, body, terms, forms, md, roles))


def _wording_in(f, body, terms, forms, md, roles):
    rel = f.relative_to(REPO).as_posix()
    # 許す形の字面だけを隠す。後ろに足された本文や同じ行の別の語は残す。
    if forms:
        def exclude(match):
            if not _wording_form_starts_ok(match[0], match) or _wording_quote_verb_follows(match[0], match):
                return match[0]
            pattern = '|'.join(_wording_pattern(word) for word in forms[match[0]])
            return re.sub(pattern, lambda m: _WORDING_GAP * len(m[0]), match[0])
        body = re.sub('|'.join(_wording_form_pattern(form) for form in sorted(forms, key=len, reverse=True)),
                      exclude, body)
    lines = wording_body(body, md, roles)
    for word, explanation in terms.items():
        pattern = _wording_pattern(word)
        found = [(no, _wording_explained(line, m, explanation))
                 for no, line in lines for m in re.finditer(pattern, line)]
        if not found:
            continue
        explained = [no for no, ok in found if ok]
        if not found[0][1]:
            later = f'。説明のある行: {", ".join(map(str, explained))} 行' if explained else ''
            ERRORS.append(f'{rel}:{found[0][0]}: 言い換えの初出 {word!r} -> ({explanation}){later}(design.md §6)')
        for no in explained[1:]:
            ERRORS.append(f'{rel}:{no}: 言い換えの重複 {word!r}。本文では 1 か所にする(最初の説明: {explained[0]} 行、design.md §6)')
    for no, line in lines:
        for _ in re.finditer('縮退', line):
            ERRORS.append(f'{rel}:{no}: 名前でもコードでもない「縮退」。意味に合う言葉へ変える(design.md §6)')


def main(*, file_results=None) -> int:
    # 同じプロセスで何度も流す呼び出し元は、共有の表を渡して、ファイルごとの検査の結果を使い回す。
    # 表は、この呼び出しの間だけ使い、抜けるとき(例外のときも)に前の値へ戻す。
    global _FILE_RESULTS
    previous = _FILE_RESULTS
    # 表を渡さない呼び出しは、外の表(表を入れた実行の中の main())を引き継がず、使い回さない。
    _FILE_RESULTS = file_results
    try:
        return _main()
    finally:
        _FILE_RESULTS = previous


def _main() -> int:
    # 文書ごとの必須項目不足にせず、検証環境の失敗を1回だけ報告する。
    if yaml_loader() is not None:
        check_json_files()
        check_native_distribution()
        check_skill_count_claims()
        check_skills()
        check_writing_rules_link()
        check_line_length()
        check_replaced_words()
        check_number_only_reasons()
        check_wording()
        check_delegation_words()
        check_host_cli_words()
        check_delegation_map_invariant()
        check_migration_allowlist_staleness()
    skills = sorted(d.name for d in SKILLS_DIR.iterdir() if d.is_dir()) if SKILLS_DIR.exists() else []
    print(f"skills: {len(skills)} 件 — {', '.join(skills)}")
    for w in WARNS:
        print(f"WARN  {w}")
    for e in ERRORS:
        print(f"ERROR {e}")
    print(f"結果: ERROR {len(ERRORS)} 件 / WARN {len(WARNS)} 件")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
