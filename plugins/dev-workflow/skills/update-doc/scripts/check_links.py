#!/usr/bin/env python3
"""Markdown の相対リンク・記載パスの存在チェック(汎用・依存なし・読み取り専用)。

使い方:
    python3 check_links.py <ファイルまたはディレクトリ>...

ディレクトリを渡すと配下の *.md を再帰走査する(.git / node_modules 等は除外)。
検査対象:
  1. [text](relative/path) 形式の相対リンク(http(s):, mailto:, #アンカーのみは対象外)
  2. `path/to/file.ext` 形式のインラインコード中の相対パスらしき文字列(存在すれば OK、
     無ければ WARN — コード片の可能性があるため error にはしない)
終了コード: リンク切れ(ERROR)が 1 件以上あれば 1、なければ 0。
"""

import re
import sys
from pathlib import Path

EXCLUDE_DIRS = {".git", "node_modules", "dist", "build", ".next", "vendor", "__pycache__"}
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
CODEPATH_RE = re.compile(r"`([A-Za-z0-9_.@/-]+/[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})`")


def iter_md_files(targets):
    for t in targets:
        p = Path(t)
        if p.is_dir():
            for f in sorted(p.rglob("*.md")):
                if not any(part in EXCLUDE_DIRS for part in f.parts):
                    yield f
        elif p.is_file():
            yield p
        else:
            print(f"WARN: 対象が見つかりません: {t}")


def strip_link(target: str) -> str:
    target = target.split("#", 1)[0]
    return target.strip()


def main(argv):
    if not argv:
        print(__doc__)
        return 2

    errors = []
    warns = []
    checked = 0

    for md in iter_md_files(argv):
        text = md.read_text(encoding="utf-8", errors="replace")
        base = md.parent
        checked += 1

        for m in LINK_RE.finditer(text):
            raw = m.group(1)
            if raw.startswith(("http://", "https://", "mailto:", "#", "tel:")):
                continue
            target = strip_link(raw)
            if not target:
                continue
            line = text.count("\n", 0, m.start()) + 1
            resolved = (base / target).resolve() if not target.startswith("/") else Path(target)
            if not resolved.exists():
                errors.append(f"{md}:{line}: リンク切れ -> {raw}")

        for m in CODEPATH_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http", "npm", "pnpm", "yarn")):
                continue
            line = text.count("\n", 0, m.start()) + 1
            resolved = (base / target).resolve() if not target.startswith("/") else Path(target)
            # doc/ 配下の文書はプロジェクトルート基準で書かれることが多いため上位も試す
            alt = None
            for up in list(base.parents)[:3]:
                if (up / target).exists():
                    alt = up / target
                    break
            if not resolved.exists() and alt is None:
                warns.append(f"{md}:{line}: 記載パスが見つからない(コード片なら無視可) -> {target}")

    print(f"検査ファイル数: {checked}")
    for w in warns:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"結果: ERROR {len(errors)} 件 / WARN {len(warns)} 件")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
