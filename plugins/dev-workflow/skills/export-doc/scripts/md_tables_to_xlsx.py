#!/usr/bin/env python3
"""Markdown 文書内の表を xlsx(または CSV)へ変換する(export-doc 用)。

使い方:
  python3 md_tables_to_xlsx.py <input.md> [<input2.md> ...] -o <output.xlsx>

- 1 表 1 シート。シート名は「文書名 + 直前の見出し」(Excel の 31 文字制約に収める)
- openpyxl が無い環境では、出力名と同名のディレクトリへ CSV(BOM 付き UTF-8)で縮退出力する
- 入力は読み取りのみ(変更しない)。同名出力は上書き(冪等)
- --source-root <dir> で元の正本を保護する(繰返し可。省略時は入力の親)
- 正本への出力・最終symlink・複数hardlink・特殊ファイルは書込前に拒否する
- セル内の Markdown 装飾(**強調**・`コード`・[リンク](url)・<br>)はプレーンテキスト化する

終了コード: 0 = 出力あり / 1 = 表が見つからない / 2 = 入力エラー・出力先の拒否
"""

import argparse
import csv
import re
import stat
import sys
from pathlib import Path

CELL_CLEAN = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"<br\s*/?>", re.IGNORECASE), "\n"),
]


def clean_cell(s: str) -> str:
    s = s.strip()
    for pat, rep in CELL_CLEAN:
        s = pat.sub(rep, s)
    return s


def split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    parts = re.split(r"(?<!\\)\|", line)
    return [clean_cell(p.replace("\\|", "|")) for p in parts]


def is_separator(line: str) -> bool:
    body = line.strip().strip("|")
    cells = [c.strip() for c in body.split("|")]
    filled = [c for c in cells if c]
    return bool(filled) and all(re.fullmatch(r":?-{3,}:?", c) for c in filled)


def extract_tables(text: str, doc_name: str) -> list[tuple[str, list[list[str]]]]:
    tables = []
    lines = text.splitlines()
    heading = ""
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            heading = m.group(2).strip()
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and is_separator(lines[i + 1]):
            rows = [split_row(line)]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            tables.append((f"{doc_name} {heading}".strip(), rows))
            continue
        i += 1
    return tables


def unique_sheet_name(name: str, used: set) -> str:
    name = re.sub(r"[\\/*?\[\]:]", " ", name).strip() or "Sheet"
    cand = name[:31]
    n = 2
    while cand in used:
        suffix = f"_{n}"
        cand = name[: 31 - len(suffix)] + suffix
        n += 1
    used.add(cand)
    return cand


class OutputError(ValueError):
    """出力前に判明した、正本または出力先を壊しうる配置。"""


def physical_directory(path: Path) -> Path:
    """既存の親を検査し、未作成の末尾だけを物理パスへ付加する。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if path == path.parent:
            raise OutputError(f"出力先の親を解決できない: {path}")
        return (physical_directory(path.parent) / path.name).resolve()
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise OutputError(f"出力先の親がディレクトリでない: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise OutputError(f"出力先の親がディレクトリでない: {path}")
    return resolved


def output_paths(paths, *, inputs=(), source_roots=()) -> list[Path]:
    """全件を変更前に検査する。同一利用者の並行差替えは保証対象外。"""
    try:
        input_paths = [Path(p) for p in inputs]
        inputs = [p.resolve(strict=True) for p in input_paths]
        roots = [Path(p).resolve(strict=True) for p in source_roots]
        if not roots:
            roots = [p.parent.resolve(strict=True) for p in input_paths]
            roots.extend(p.parent for p in inputs)
        if any(not root.is_dir() for root in roots):
            raise OutputError("正本ディレクトリが通常のディレクトリでない")
        checked = []
        for path in paths:
            path = Path(path).absolute()
            dest = physical_directory(path.parent) / path.name
            try:
                info = dest.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISREG(info.st_mode):
                    raise OutputError(f"出力先が通常ファイルでない（リンクも拒否）: {path}")
                if info.st_nlink > 1:
                    raise OutputError(f"出力先に複数のhardlinkがある: {path}")
                if any(dest.samefile(p) for p in inputs):
                    raise OutputError(f"出力先が入力ファイルと同じ: {path}")
            for parent in dest.parents:
                # samefile は macOS の大文字小文字等の別名も検出する。
                if any(parent == root or (parent.exists() and parent.samefile(root)) for root in roots):
                    raise OutputError(f"出力先が正本ディレクトリ内にある: {path}")
            checked.append(dest)
        return checked
    except (OSError, RuntimeError) as exc:
        raise OutputError(f"出力先または正本を解決できない: {exc}") from exc


def write_csv_fallback(tables, out: Path, *, inputs=(), source_roots=()) -> None:
    outdir = out.with_suffix("")
    used = set()
    planned = []
    for hint, rows in tables:
        name = unique_sheet_name(hint, used)
        planned.append((outdir / f"{name}.csv", rows))
    checked = output_paths([path for path, _ in planned], inputs=inputs, source_roots=source_roots)
    for path, (_, rows) in zip(checked, planned):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(rows)
    print(f"FALLBACK openpyxl 未導入のため CSV で出力: {outdir}/ (pip install openpyxl で xlsx 化可)")


def write_xlsx(tables, out: Path, *, inputs=(), source_roots=()) -> None:
    checked_out, = output_paths([out], inputs=inputs, source_roots=source_roots)
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    for hint, rows in tables:
        ws = wb.create_sheet(unique_sheet_name(hint, used))
        for r in rows:
            ws.append(r)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for col in range(1, ws.max_column + 1):
            width = 8
            for row in range(1, ws.max_row + 1):
                v = ws.cell(row=row, column=col).value or ""
                first_line = str(v).split("\n")[0]
                w = sum(2 if ord(ch) > 0x7F else 1 for ch in first_line)
                width = max(width, min(w + 2, 60))
            ws.column_dimensions[get_column_letter(col)].width = width
    checked_out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(checked_out)
    print(f"OK {out} にシート {len(wb.sheetnames)} 件を出力")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", help="入力 Markdown ファイル")
    ap.add_argument("-o", "--output", required=True, help="出力 xlsx パス")
    ap.add_argument("--source-root", action="append", default=[],
                    help="保護する元の正本ディレクトリ（繰返し可。省略時は入力の親）")
    args = ap.parse_args()

    tables = []
    missing = []
    inputs = []
    for p in args.inputs:
        path = Path(p)
        if not path.is_file():
            missing.append(p)
            continue
        tables += extract_tables(path.read_text(encoding="utf-8"), path.stem)
        inputs.append(path)
    for p in missing:
        print(f"WARN 入力が存在しない: {p}", file=sys.stderr)
    if missing and not tables:
        return 2
    if not tables:
        print("表が見つからなかった(出力なし)")
        return 1

    out = Path(args.output)
    try:
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            write_csv_fallback(tables, out, inputs=inputs, source_roots=args.source_root)
        else:
            write_xlsx(tables, out, inputs=inputs, source_roots=args.source_root)
    except OutputError as exc:
        print(f"ERROR 出力を拒否: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
