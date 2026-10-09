#!/usr/bin/env python3
"""無人ループのタスク本文解析。親は信頼コピーの本文を保持して -c で動かす。"""
import re
import sys

HOLD = re.compile(r"^- \*\*保留\*\*\(ship-task・[0-9]{4}-[0-9]{2}-[0-9]{2}\): ")


HOLD_CODE = re.compile(r"^- \*\*保留\*\*\(ship-task・[0-9]{4}-[0-9]{2}-[0-9]{2}\): ([SDUG][0-9]+)")


META = re.compile(r"^> \*\*無人実行\*\*: 可[ \t\r\v\f]*$")


CREATED = re.compile(r"^> \*\*作成日\*\*: ([0-9]{4}-[0-9]{2}-[0-9]{2})")


H2 = re.compile(r"^## ")


RECORD = re.compile(r"^## 追加修正記録$")


FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def read_text(path):
    data = sys.stdin.buffer.read() if path == "-" else open(path, "rb").read()
    text = data.decode("utf-8", "replace")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def fence_flags(lines):
    # task-template.md の記法の規約のコードフェンス(task-digest.py と同じ定義)
    flags, closing = [], None
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


def headings(lines, fenced):
    return [i for i, line in enumerate(lines) if not fenced[i] and H2.match(line)]


def header_lines(lines):
    fenced = fence_flags(lines)
    hs = headings(lines, fenced)
    end = hs[0] if hs else len(lines)
    return [lines[i] for i in range(end) if not fenced[i]]


def record_lines(lines):
    # 追加修正記録の節(見出しの次の行から次の `## ` の見出しの前まで)の、フェンスの外の行
    fenced = fence_flags(lines)
    hs = headings(lines, fenced)
    out = []
    for start in (i for i in hs if RECORD.match(lines[i])):
        end = next((i for i in hs if i > start), len(lines))
        out.extend(lines[i] for i in range(start + 1, end) if not fenced[i])
    return out


def cmd_taskinfo(path):
    lines = read_text(path)
    head = header_lines(lines)
    meta = any(META.match(line) for line in head)
    date = next((m.group(1) for m in (CREATED.match(line) for line in head) if m), "")
    print(f"meta={1 if meta else 0}")
    print(f"date={date}")


def cmd_holdcount(path):
    print(sum(1 for line in record_lines(read_text(path)) if HOLD.match(line)))


def cmd_holdcode(path):
    # 保留の行の照合パターンに一致する最後の行を先に選び、その行だけから対話点番号を取り出す(取り出せなければ空。
    # 前の行の番号を返さない — 古い G1 の行が残っていても、最後の行で判定する)
    holds = [line for line in record_lines(read_text(path)) if HOLD.match(line)]
    m = HOLD_CODE.match(holds[-1]) if holds else None
    print(m.group(1) if m else "")


COMMANDS = {
    "taskinfo": cmd_taskinfo, "holdcount": cmd_holdcount, "holdcode": cmd_holdcode,
}
try:
    COMMANDS[sys.argv[1]](*sys.argv[2:])
except SystemExit:
    raise
except Exception as exc:  # 想定外の失敗は 9(呼び出し側は「差分あり」や成功に読み替えない)
    print(f"補助の失敗: {exc!r}", file=sys.stderr)
    sys.exit(9)
