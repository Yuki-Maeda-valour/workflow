#!/usr/bin/env python3
"""Collect the finding keys of a task directory, or check one candidate MD (read-only).

The contract lives in create-task/references/candidate-mode.md, and the header format of
`候補_` in create-task/references/task-template.md.
"""

from __future__ import annotations

import sys

# プラグインルート(周の worktree の外)に __pycache__ を作らない。周の Bash では
# PYTHONDONTWRITEBYTECODE を渡せないので、兄弟の script を取り込む前にここで止める
sys.dont_write_bytecode = True

import argparse
import importlib.util
import json
import os
import re
import unicodedata
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"{filename} を読み込めない")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 状態名の列は resolve-task-dir.py の STATE_FILE、コードフェンスの定義は task-digest.py(本文ダイジェスト)の
# ものを取り込む(2 か所に置かない)。書式と照合パターンの正本は task-template.md の記法の規約
STATE_FILE = _load("resolve_task_dir", "resolve-task-dir.py").STATE_FILE
_fence_flags = _load("task_digest", "task-digest.py")._fence_flags

SOURCES = ("data-audit", "refactor", "stack-research", "reflect-decisions")
H2 = re.compile(r"^## ")
# メタ行の照合パターン(grep -E と同じ字面)。見るのはヘッダ(最初の `## ` の見出しより前・フェンスの外)だけ
SOURCE = re.compile(r"^> \*\*発見元\*\*: (data-audit|refactor|stack-research|reflect-decisions)( |\(|$)")
KEY = re.compile(r"^> \*\*指摘キー\*\*: (.+)$")
SKIP = re.compile(r"^> \*\*見送り\*\*: ([0-9]{4}-[0-9]{2}-[0-9]{2} — .+)$")
SOURCE_PREFIX = "> **発見元**:"
KEY_PREFIX = "> **指摘キー**:"
SKIP_PREFIX = "> **見送り**:"
UNATTENDED_PREFIX = "> **無人実行**:"
RECORD_HEADING = re.compile(r"^## 追加修正記録")
# チェックボックス形の行はフェンスの中も数える(do-task の集計と同じ)
CHECKBOX = re.compile(r"^\s*- \[( |x|X)\]")


def normalize_key(value: str) -> str:
    # 機械で行える正規化だけ(NFC・連続する空白を 1 つに・前後の空白を削る)。ほかは書き手の規則(candidate-mode.md)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()


def _problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _lines(text: str) -> list[str]:
    # 本文ダイジェストと同じく、BOM を除き、改行を LF に揃え、各行の末尾の空白を削る
    if text.startswith("﻿"):
        text = text[1:]
    return [line.rstrip(" \t") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


class Parsed:
    def __init__(self, text: str):
        lines = _lines(text)
        fenced = _fence_flags(lines)
        header: list[str] = []
        for i, line in enumerate(lines):
            if fenced[i]:
                continue
            if H2.match(line):
                break
            header.append(line)

        self.source_lines = [line for line in header if line.startswith(SOURCE_PREFIX)]
        self.sources = [m.group(1) for m in map(SOURCE.match, self.source_lines) if m]
        self.key_lines = [line for line in header if line.startswith(KEY_PREFIX)]
        keys: list[str] = []
        for m in map(KEY.match, self.key_lines):
            key = normalize_key(m.group(1)) if m else ""
            if key and key not in keys:
                keys.append(key)
        self.keys = keys
        skip_lines = [line for line in header if line.startswith(SKIP_PREFIX)]
        self.skipped = [m.group(1) for m in map(SKIP.match, skip_lines) if m]
        self.malformed_skips = [line for line in skip_lines if not SKIP.match(line)]
        self.unattended = [line for line in header if line.startswith(UNATTENDED_PREFIX)]
        self.record = any(not fenced[i] and RECORD_HEADING.match(line) for i, line in enumerate(lines))
        self.checkboxes = [i + 1 for i, line in enumerate(lines) if CHECKBOX.match(line)]

    def malformed_problem(self) -> dict[str, str]:
        return _problem(
            "skipped-malformed",
            f"見送りの書式違いの行が {len(self.malformed_skips)} 行ある"
            "(`> **見送り**: YYYY-MM-DD — <理由>`。区切りは em ダッシュ `—` だけ)",
        )


def _read(path: str) -> str:
    data = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    return data.decode("utf-8")


def collect(task_dir: Path) -> tuple[int, dict]:
    files: list[dict] = []
    names: set[str] = set()
    code = 0
    if not task_dir.exists() and not task_dir.is_symlink():
        # 最初の候補(保存先がまだ無い)
        return 0, {"files": files, "names": []}
    try:
        with os.scandir(task_dir) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        print(f"収集できない: ディレクトリを読めない: {exc}", file=sys.stderr)
        return 1, {"files": files, "names": []}
    for entry in entries:
        # 直下の通常ファイルで、名が状態名の正規表現に全体一致するものだけ(symlink は除く)
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        if not STATE_FILE.fullmatch(entry.name):
            continue
        state, _, rest = entry.name.partition("_")
        name = rest[: -len(".md")]
        names.add(name)
        item: dict = {"file": entry.name, "state": state, "name": name, "keys": [], "skipped": None, "problems": []}
        try:
            parsed = Parsed(_read(entry.path))
        except (OSError, UnicodeDecodeError) as exc:
            # 黙って飛ばすと、既知のキーが欠けて重複が混ざる
            item["problems"].append(_problem("unreadable", f"読めない: {exc}"))
            code = 1
        else:
            item["keys"] = parsed.keys
            item["skipped"] = parsed.skipped[0] if parsed.skipped else None
            if parsed.malformed_skips:
                item["problems"].append(parsed.malformed_problem())
        files.append(item)
    return code, {"files": files, "names": sorted(names)}


def check(path: str, expected: str | None) -> tuple[int, dict]:
    try:
        parsed = Parsed(_read(path))
    except (OSError, UnicodeDecodeError) as exc:
        problem = _problem("unreadable", f"読めない: {exc}")
        return 1, {"ok": False, "source": None, "keys": [], "problems": [problem]}

    problems: list[dict[str, str]] = []
    source = parsed.sources[0] if len(parsed.source_lines) == 1 and len(parsed.sources) == 1 else None
    if source is None:
        problems.append(
            _problem(
                "source-count",
                f"ヘッダの発見元の行が {len(parsed.source_lines)} 行"
                f"(照合パターンに合うもの {len(parsed.sources)} 行)。照合パターンに合う行がちょうど 1 行必要",
            )
        )
    elif expected is not None and source != expected:
        problems.append(_problem("source-mismatch", f"発見元が {source}(期待は {expected})"))
    if len(parsed.key_lines) != 1 or len(parsed.keys) != 1:
        problems.append(
            _problem(
                "key-count",
                f"ヘッダの指摘キーの行が {len(parsed.key_lines)} 行(値のあるもの {len(parsed.keys)} 個)。ちょうど 1 行が必要",
            )
        )
    if parsed.unattended:
        problems.append(_problem("unattended-meta", "ヘッダに無人実行のメタ行がある(候補には書かない)"))
    if parsed.skipped:
        problems.append(_problem("skipped", "ヘッダに見送りの行がある"))
    if parsed.malformed_skips:
        problems.append(parsed.malformed_problem())
    if parsed.record:
        problems.append(_problem("record-section", "`## 追加修正記録` の見出しがある(候補には書かない)"))
    if parsed.checkboxes:
        lines = ", ".join(str(n) for n in parsed.checkboxes)
        problems.append(_problem("checkbox", f"チェックボックス形の行がある(フェンスの中も数える。行 {lines})"))
    payload = {"ok": not problems, "source": source, "keys": parsed.keys, "problems": problems}
    return (0 if not problems else 1), payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--task-dir")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("path", nargs="?", help="--check で検査する候補のパス。`-` なら stdin を読む")
    args = parser.parse_args()

    if args.check:
        if args.task_dir is not None or args.path is None:
            print("使い方: candidate-keys.py --check [--source=<発見元>] <パス | ->", file=sys.stderr)
            return 2
        if args.source is not None and args.source not in SOURCES:
            print(f"使い方: --source は {' / '.join(SOURCES)} のどれか", file=sys.stderr)
            return 2
        code, payload = check(args.path, args.source)
    else:
        if args.task_dir is None or args.path is not None or args.source is not None:
            print("使い方: candidate-keys.py --task-dir=<ディレクトリ>", file=sys.stderr)
            return 2
        task_dir = Path(args.task_dir)
        if (task_dir.exists() or task_dir.is_symlink()) and not task_dir.is_dir():
            print(f"使い方: --task-dir がディレクトリでない: {args.task_dir}", file=sys.stderr)
            return 2
        code, payload = collect(task_dir)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
