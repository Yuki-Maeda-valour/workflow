#!/usr/bin/env python3
"""workflow リポジトリの一括検証。

検証内容:
  1. .claude-plugin/marketplace.json / plugin.json の JSON 構文と必須フィールド、
     配布メタの整合(version 3 箇所の一致・plugins[].source の実在・
     description の skill 件数の表記と実数の一致)
  2. 各 SKILL.md: frontmatter の存在と name / description 必須、name とディレクトリ名の一致
  3. SKILL.md 500 行以下(design.md §6。論理行数 = 改行の数 + 末尾が改行で終わらなければ 1)
  4. description の長さ(規約 150〜500 字は ERROR / 上限 1024 字)
  5. 禁止パターン(design.md §5): 絶対パス・TeamCreate/TeamDelete・日付付きモデル ID・
     claude -p と、環境変数 WORKFLOW_PROJECT_NAMES に渡した周辺プロジェクト名
     (未設定なら 1 語も足さない)。行内に `<!-- validate-allow: 理由 -->` があれば
     その行だけ免除する —— **この検査にだけ効く**(7・8・委託の語の除外の不変条件は
     免除規則を持たない)
  6. SKILL.md と references/*.md 内の相対リンク(references/ scripts/ templates/ 兄弟 skill)の
     存在。fragment 付き・タイトル付きも検査し、コードフェンスの中は除外する
  7. 委託の語(design.md §7-7): 全 skill の skill 直下(画像を除く)・references/ 配下の
     *.md・scripts/ 配下(画像を除く)を検査し、未移行 skill(許容リスト)と
     検査対象外ファイルを除外する
  8. ホスト CLI 語(design.md §7-7-1): 全 skill の skill 直下(画像を除く)・
     references/ 配下の *.md を大小無視で検査する(scripts/ は対象外。委託の語と同じ
     除外 2 本を共有する)。ランナー名・サンドボックスモード名・コマンド形・
     プラグイン/subagent 名の 4 系統

終了コード: ERROR があれば 1。WARN のみなら 0。
"""

import json
import os
import re
import sys
from pathlib import Path

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
# コードフェンスの内外を問わない。**効くのはこの禁止パターン検査だけ** —— 委託の語・
# ホスト CLI 語・除外の不変条件は免除規則を持たない(そちらは役割語へ書き換えて消す)。
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

# ホスト CLI 語(design.md §7-7-1)。_DELEGATION_WORDS(委託機構の語)とは別カテゴリ
# ——スコープが違う(design.md §7-7-1 は scripts/ を対象外にする。scripts/ は正当に CLI 名を持つ)
# ため、同じ定数に混ぜると scripts/ 配下が一斉に ERROR になる。ランナー名・サンドボックスモード名・
# コマンド形・プラグイン/subagent 名の 4 系統。**大小を無視する**(check_host_cli_words() が
# re.IGNORECASE を付ける。既存の _DELEGATION_WORDS は大小を区別したまま — `Codex に実装を委託する`
# のような大文字始まりが最も混入しやすい書き方なのに、既存の検査は大小を区別するため素通りする)。
# **単語境界(\b)を付けない(部分一致にする)**。§7-7 の「語彙の限界」がすでに明文で否定した
# 設計だからで、_DELEGATION_WORDS 側の `Agent` も \b はやめて英字だけの否定先読み・後読み
# ((?<![A-Za-z])Agent(?![A-Za-z]))に替えた。**`Agent` は部分一致ではない** ——
# 以下の「部分一致を選ぶ」は _HOST_CLI_WORDS の側の話
# ——日本語文字が \w に含まれるため \b を付けると助詞が直接続く形(`Geminiに実装を委託する` /
# `Codex execで委託`)を取りこぼす(偽陰性 = 到達条件をすり抜ける危険側)一方、誤検出は ERROR
# で落ちる安全側なので部分一致を選ぶ(実測 2026-09-17: \b 付きだとこの 2 例は MISS、空白区切りの
# `gemini を使う` だけが HIT した)。コマンド形は「codex exec」のように空白を含むため \s+ で
# 繋ぐだけで、前後に \b は付けない。⚠ 部分一致の副作用: 日本語隣接の取りこぼしは無くなるが、
# `my-cursor-agent-wrapper` のようなハイフン隣接語では誤検出しうる——誤検出は ERROR で落ちる
# 安全側なので許容し、出たらその語を書き換えるか除外集合に足す(§7-7 と同じ方針)。単独の
# `codex`・`read-only`・単独の製品名(`Codex`/`Cursor`/`Claude Code`)は対象外(既知の限界。
# 設定パス `.codex/` や一般語・`init-project` の正当な散文用例と衝突するため
# — 衝突を避ける方針は「コマンド形だけを検査語にしてパス形と衝突させない」を既定にし、
# 「検出前に `.codex/` 等のパス形をマスクする」方式は将来 codex 単独を検査したくなった場合の
# 拡張余地として残す。その場合 Python の re は固定長後読みしか許さない点に注意)。
_HOST_CLI_WORDS = [
    # ランナー名
    r"cursor-agent",
    r"gemini",
    # サンドボックスモード名
    r"workspace-write",
    r"danger-full-access",
    # コマンド形(単独の codex は入れない)
    r"codex\s+exec",
    r"codex\s+review",
    r"codex\s+mcp",
    # プラグイン・subagent 名
    r"codex-plugin-cc",
    r"codex-rescue",
]

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
    レイアウト外のファイルに置くと到達条件をすり抜けるため。design.md §7-7-1(決定 24))。
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
    この差は design.md §7-7-1 に明記する。"""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        for f in sorted(d.rglob("*")):
            if not _in_host_cli_scope(f):
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            for pat in _HOST_CLI_WORDS:
                for m in re.finditer(pat, body, flags=re.IGNORECASE):
                    line = body.count("\n", 0, m.start()) + 1
                    ERRORS.append(
                        f"{f.relative_to(REPO)}:{line}: ホスト固有の CLI 語 -> {m.group(0)!r}"
                        "(役割語に書き換えるか、CLI の手順の契約として"
                        " do-task/references/external-runners.md へ移す。"
                        " delegation-map.md は役割語→機構の解決表で CLI 名を持たないため"
                        " 移し先にならず、他の references/*.md はこの検査の対象内なので"
                        " 移しても解消しない)"
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
