#!/usr/bin/env python3
"""workflow リポジトリの一括検証。

検証内容:
  1. .claude-plugin/marketplace.json / plugin.json の JSON 構文と必須フィールド
  2. 各 SKILL.md: frontmatter の存在と name / description 必須、name とディレクトリ名の一致
  3. SKILL.md 500 行以下(design.md §6)
  4. description の長さ(規約 150〜500 字 / 上限 1024 字)
  5. 禁止パターン(design.md §5): 特定プロジェクトへのハードコード・絶対パス・
     TeamCreate/TeamDelete・日付付きモデル ID・claude -p
  6. SKILL.md 内の相対リンク(references/ scripts/ templates/)の存在

終了コード: ERROR があれば 1。WARN のみなら 0。
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO / "plugins" / "dev-workflow" / "skills"

ERRORS: list[str] = []
WARNS: list[str] = []

# 固有名のハードコード検出(ユーザー環境の絶対パス・周辺プロジェクト名の混入)
FORBIDDEN_PATTERNS = [
    (rf"{re.escape(str(Path.home()))}(?!/dev/workflow\b)", "ユーザー環境の絶対パス"),
    (r"TeamCreate|TeamDelete", "廃止 API(Agent + SendMessage を使う)"),
    (r"claude-(?:fable|mythos|opus|sonnet|haiku)-[0-9][-0-9a-z]*", "日付付き/版数付きモデル ID(エイリアスを使う)"),
    (r"(?:Fable|Mythos|Opus|Sonnet|Haiku)\s*[0-9]", "モデル版数のハードコード(エイリアスを使う)"),
    (r"claude\s+-p\b|claude\s+--print", "claude -p の Bash 起動(禁止・別課金)"),
]

# 周辺プロジェクト名の動的検査: 実行環境の ~/dev 配下ディレクトリ名を禁止語として追加する
# (skill の還元時に固有プロジェクト名が紛れ込むのを防ぐ。一般語のディレクトリは除外)
_GENERIC_DIR_NAMES = {"workflow", "demo", "memo", "resume", "test", "tmp", "sandbox"}
_dev_dir = Path.home() / "dev"
if _dev_dir.is_dir():
    _names = sorted(
        re.escape(p.name)
        for p in _dev_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name.lower() not in _GENERIC_DIR_NAMES
    )
    if _names:
        FORBIDDEN_PATTERNS.append((rf"\b(?:{'|'.join(_names)})\b", "周辺プロジェクト固有名の混入"))

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#]+)\)")


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
        except Exception:
            pass


def check_skills():
    if not SKILLS_DIR.exists():
        ERRORS.append(f"{SKILLS_DIR}: 存在しない")
        return
    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skill_dirs:
        ERRORS.append("skills が 1 つもない")
    for d in skill_dirs:
        md = d / "SKILL.md"
        if not md.exists():
            ERRORS.append(f"{d.name}: SKILL.md がない")
            continue
        text = md.read_text(encoding="utf-8")
        lines = text.count("\n") + 1
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
                WARNS.append(f"{d.name}/SKILL.md: description {len(desc)} 字(規約 150〜500。トリガー語句を足す)")
            elif len(desc) > 500:
                WARNS.append(f"{d.name}/SKILL.md: description {len(desc)} 字(規約 150〜500)")

        # 禁止パターン(SKILL.md と references/ scripts/ templates/ 全ファイル)
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.suffix in {".png", ".jpg"}:
                continue
            body = f.read_text(encoding="utf-8", errors="replace")
            body_lines = body.splitlines()
            for pat, why in FORBIDDEN_PATTERNS:
                for m in re.finditer(pat, body):
                    line = body.count("\n", 0, m.start()) + 1
                    line_text = body_lines[line - 1] if line <= len(body_lines) else ""
                    # 「〜は禁止」「〜を使わない」等、規約としての言及は許容する
                    if re.search(r"禁止|使わない|しない|廃止|ではなく", line_text):
                        continue
                    ERRORS.append(f"{f.relative_to(REPO)}:{line}: 禁止パターン [{why}] -> {m.group(0)!r}")

        # 相対リンクの存在
        for m in LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (d / target).exists():
                line = text.count("\n", 0, m.start()) + 1
                ERRORS.append(f"{d.name}/SKILL.md:{line}: リンク切れ -> {target}")


def main() -> int:
    check_json_files()
    check_skills()
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
