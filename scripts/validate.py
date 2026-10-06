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
     その行だけ免除する(8 でも効く。7 と委託の語の除外の不変条件では効かない)
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
     SKILL.md の位置から解決し、実パスで比べる。素の言及・節の外・フェンスの中は数えない。
     マーカーでは免除しない
 10. SKILL.md の本文の行の長さ(design.md §6。WARN): frontmatter の後の、コードフェンスの外
     (判定は 6 と同じ _mask_code_fences)の行のうち、行頭の空白を除いて `|` で始まらない行
     (表の行を除く)について、文字数から、200 字を超えるインラインのコード(1 行の中の
     `[^`]+`)の長さを引いた数が 200 を超えたら WARN にする。ファイル:行番号と文字数を出す。
     マーカーでは免除しない

終了コード: ERROR があれば 1。WARN のみなら 0。
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "plugins" / "dev-workflow" / "skills"

ERRORS: list[str] = []
WARNS: list[str] = []

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


def parse_frontmatter(text: str, path: Path):
    if not text.startswith("---"):
        ERRORS.append(f"{path}: frontmatter がない")
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        ERRORS.append(f"{path}: frontmatter が閉じていない")
        return {}
    block = text[4:end]
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block) or {}
        if not isinstance(data, dict):
            ERRORS.append(f"{path}: frontmatter が辞書でない")
            return {}
        return data
    except ImportError:
        # PyYAML が無い環境向けの簡易パース(トップレベルの key: value のみ)
        data = {}
        current_key = None
        for line in block.splitlines():
            m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
            if m:
                current_key = m.group(1)
                data[current_key] = m.group(2).strip().strip('"')
            elif current_key and line.startswith(("  ", "\t")):
                data[current_key] = str(data.get(current_key, "")) + " " + line.strip()
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
    リンク・リンクの形でない素の言及・節の外のリンク・コードフェンスの中のリンクは数えない。
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
                "(正本を指す 1 行をリンクの形で置く。素の言及・節の外・コードフェンスの中は数えない"
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
    SKILL.md 欠落時に打つ continue を継承しないため)。既存の禁止パターンループ
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


def main() -> int:
    check_json_files()
    check_skill_count_claims()
    check_skills()
    check_writing_rules_link()
    check_line_length()
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
