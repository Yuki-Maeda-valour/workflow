#!/usr/bin/env python3
"""Print the body digest of a task MD (the definition lives in create-task/references/task-template.md)."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


# 定義(コードフェンス・対象・正規化・値・算出できない場合)の正本は task-template.md の記法の規約。
# ここはそれを実装するだけで、規則を足さない
RECORD_HEADING = re.compile(r"^## 追加修正記録$")
H2 = re.compile(r"^## ")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# 除外 1: 最初の `## ` の見出しより前で、do-task・人が承認の後に書き換える 3 種の行
MUTABLE_HEADER = re.compile(r"^> \*\*(?:ステータス|基準コミット|無人実行)\*\*:")
CHECKBOX = re.compile(r"^(\s*)- \[[xX]\]")


class DigestError(Exception):
    pass


def _fence_flags(lines: list[str]) -> list[bool]:
    # 開きの行・閉じの行もフェンスの中に数える。閉じないまま末尾に達したら、そこまでがフェンスの中
    flags: list[bool] = []
    closing: re.Pattern[str] | None = None
    for line in lines:
        if closing is None:
            opened = FENCE_OPEN.match(line)
            if opened:
                marker = opened.group(1)
                closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$")
            flags.append(bool(opened))
            continue
        flags.append(True)
        if closing.match(line):
            closing = None
    return flags


def body_lines(text: str) -> list[str]:
    if text.startswith("\ufeff"):
        text = text[1:]
    # 改行を LF に揃え、各行の末尾の空白(半角スペースとタブ)を削る
    lines = [line.rstrip(" \t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    fenced = _fence_flags(lines)
    headings = [i for i, line in enumerate(lines) if not fenced[i] and H2.match(line)]

    records = [i for i in headings if RECORD_HEADING.match(lines[i])]
    if len(records) != 1:
        raise DigestError(f"コードフェンスの外の `## 追加修正記録` の見出し行が {len(records)} 個ある(ちょうど 1 個が必要)")
    start = records[0]
    # 除外 2 は見出し行から次の `## ` の見出しの前まで。中の行はフェンスの内外を問わず外す
    end = next((i for i in headings if i > start), len(lines))
    first_heading = headings[0]

    kept: list[str] = []
    for i, line in enumerate(lines):
        if start <= i < end:
            continue
        if fenced[i]:
            # フェンスの中は、除外 1 にも当たらず、空行も落とさず、チェックボックスも書き換えない
            kept.append(line)
            continue
        if i < first_heading and MUTABLE_HEADER.match(line):
            continue
        if line == "":
            continue
        kept.append(CHECKBOX.sub(r"\1- [ ]", line))
    return kept


def digest(text: str) -> str:
    # 残った行を LF で連結し(末尾に改行を付けない)、UTF-8 の sha256 の先頭 16 桁を取る
    normalized = "\n".join(body_lines(text))
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("path", help="タスク MD のパス。`-` なら stdin を読む")
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read() if args.path == "-" else Path(args.path).read_bytes()
        result = digest(data.decode("utf-8"))
    except OSError as exc:
        print(f"算出できない: ファイルを読めない: {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(f"算出できない: UTF-8 として読めない: {exc}", file=sys.stderr)
        return 1
    except DigestError as exc:
        print(f"算出できない: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
