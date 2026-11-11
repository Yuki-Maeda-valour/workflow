#!/usr/bin/env python3
"""S2E(#470)用の Unicode 表と固定期待を、Python 3.10(Unicode 13.0.0)から作る。

出力:
  --rust <パス>     unicode_tables.rs(空白・十進数字・表示不能・結合クラス・正準分解・合成の表)
  --expect <フォルダ> unicode_expect.txt(全文字番号の判定の範囲)と nfc_cases.txt(文字の並びの固定期待)
  --check           --rust と --expect の既存の出力が、いまの Python から作った内容と同じかを確かめる

表は Python の unicodedata と str の判定だけから作る。Python 3.10 以外では止める。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from pathlib import Path

if sys.version_info[:2] != (3, 10) or unicodedata.unidata_version != "13.0.0":
    raise SystemExit("Python 3.10(Unicode 13.0.0)で実行する: " + sys.version.split()[0] + " / " + unicodedata.unidata_version)

LAST = 0x110000


def ranges(predicate):
    """predicate が真の文字番号を (開始, 終了) の連続範囲にする。"""
    result = []
    start = None
    for code in range(LAST):
        if predicate(code):
            if start is None:
                start = code
        elif start is not None:
            result.append((start, code - 1))
            start = None
    if start is not None:
        result.append((start, LAST - 1))
    return result


def valued_ranges(value_of):
    """値が同じ連続範囲を (開始, 終了, 値) にする。None の範囲は含めない。"""
    result = []
    start = None
    current = None
    for code in range(LAST):
        value = value_of(code)
        if value != current:
            if current is not None:
                result.append((start, code - 1, current))
            start, current = code, value
    if current is not None:
        result.append((start, LAST - 1, current))
    return result


def space(code):
    return chr(code).isspace()


def decimal(code):
    return unicodedata.decimal(chr(code), None)


def printable(code):
    return chr(code).isprintable()


def combining(code):
    return unicodedata.combining(chr(code)) or None


def nfc_changes(code):
    return unicodedata.normalize("NFC", chr(code)) != chr(code)


def decimal_starts():
    starts = []
    code = 0
    while code < LAST:
        value = decimal(code)
        if value == 0:
            if [decimal(code + k) for k in range(10)] != list(range(10)):
                raise SystemExit("十進数字が 10 個ずつの連続でない: %x" % code)
            starts.append(code)
            code += 10
        else:
            if value is not None:
                raise SystemExit("0 以外から始まる十進数字がある: %x" % code)
            code += 1
    return starts


def canonical_decompositions():
    result = []
    for code in range(LAST):
        text = unicodedata.decomposition(chr(code))
        if text and not text.startswith("<"):
            result.append((code, [int(part, 16) for part in text.split()]))
    return result


def compositions(decomps):
    result = []
    for code, parts in decomps:
        if len(parts) == 2 and unicodedata.normalize("NFC", chr(code)) == chr(code):
            result.append((parts[0], parts[1], code))
    return sorted(result)


def rust_source():
    out = []
    w = out.append
    w("#![cfg_attr(rustfmt, rustfmt::skip)]\n")
    w("// このファイルは native/ci/s2e_fixtures/gen_unicode_tables.py が Python 3.10(Unicode 13.0.0)から作る。手で直さない。\n")
    w("// 直すときはスクリプトを直して作り直し、--check で一致を確かめる。\n")
    w("\n")

    def pair_table(name, items, doc):
        w("/// %s\n" % doc)
        w("pub const %s: &[(u32, u32)] = &[\n" % name)
        for a, b in items:
            w("    (0x%x, 0x%x),\n" % (a, b))
        w("];\n\n")

    pair_table("SPACE", ranges(space), "`str.isspace()` が真の文字番号の範囲。")
    w("/// `unicodedata.decimal` が 0 になる文字番号。各 10 個が連続し、値は 0 から 9。\n")
    w("pub const DECIMAL_STARTS: &[u32] = &[\n")
    for start in decimal_starts():
        w("    0x%x,\n" % start)
    w("];\n\n")
    pair_table("NONPRINTABLE", ranges(lambda c: not printable(c)), "`str.isprintable()` が偽の文字番号の範囲。")
    w("/// 結合クラスが 0 でない文字番号の範囲と値。\n")
    w("pub const COMBINING: &[(u32, u32, u8)] = &[\n")
    for a, b, v in valued_ranges(combining):
        w("    (0x%x, 0x%x, %d),\n" % (a, b, v))
    w("];\n\n")
    decomps = canonical_decompositions()
    w("/// 正準分解(1 段。互換分解を含まない)。ハングルの音節は式で分解するので含まない。\n")
    w("pub const DECOMPOSITION: &[(u32, &[u32])] = &[\n")
    for code, parts in decomps:
        w("    (0x%x, &[%s]),\n" % (code, ", ".join("0x%x" % p for p in parts)))
    w("];\n\n")
    w("/// 合成する組(先頭, 後ろ, 合成後)。合成除外と分解が 1 文字の物は含まない。\n")
    w("pub const COMPOSITION: &[(u32, u32, u32)] = &[\n")
    for a, b, c in compositions(decomps):
        w("    (0x%x, 0x%x, 0x%x),\n" % (a, b, c))
    w("];\n")
    return "".join(out)


def errno_rust_source():
    """glibc の `strerror` の文面(Python の os.strerror)。Linux の製品は musl の静的ビルドで、文面が違うため固定する。"""
    import os

    out = ["#![cfg_attr(rustfmt, rustfmt::skip)]\n",
           "// このファイルは native/ci/s2e_fixtures/gen_unicode_tables.py が Python 3.10 の os.strerror(glibc)から作る。手で直さない。\n\n",
           "/// errno の番号 0 から 140 までの `strerror` の文面(glibc)。\n",
           "pub const GLIBC_STRERROR: &[&str] = &[\n"]
    for number in range(0, 141):
        text = os.strerror(number)
        out.append("    %s,\n" % json.dumps(text, ensure_ascii=False))
    out.append("];\n")
    return "".join(out)


def expect_text():
    lines = ["# gen_unicode_tables.py が作る。全文字番号の判定の範囲(Python 3.10 / Unicode 13.0.0)。"]
    for a, b in ranges(space):
        lines.append("space %x %x" % (a, b))
    for a, b, v in valued_ranges(decimal):
        lines.append("decimal %x %x %d" % (a, b, v))
    for a, b in ranges(lambda c: not printable(c)):
        lines.append("nonprintable %x %x" % (a, b))
    for a, b, v in valued_ranges(combining):
        lines.append("combining %x %x %d" % (a, b, v))
    for a, b in ranges(nfc_changes):
        lines.append("nfc_changes %x %x" % (a, b))
    return "\n".join(lines) + "\n"


def nfc_cases():
    """文字の並びの固定期待。入力と NFC の出力を 16 進で並べる。"""
    rng = random.Random(13)
    decomps = canonical_decompositions()
    combining_codes = [c for c in range(LAST) if unicodedata.combining(chr(c))]
    composite = [c for c, parts in decomps]
    jamo = list(range(0x1100, 0x1113)) + list(range(0x1161, 0x1176)) + list(range(0x11A8, 0x11C3))
    hangul = [0xAC00, 0xAC01, 0xB098, 0xB099, 0xD7A3, 0xC7B0, 0xC7B1]
    exclusions = [0x0958, 0x0959, 0x095A, 0x095B, 0x095C, 0x095D, 0x095E, 0x095F, 0x09DC, 0x09DD, 0x09DF, 0x0A33, 0x0A36,
                  0x0A59, 0x0B5C, 0x0B5D, 0x0F43, 0x0F4D, 0x0F52, 0x0F57, 0x0F5C, 0x0F69, 0x0F73, 0x0F75, 0x0F76, 0x0F78,
                  0x0F81, 0x0F93, 0x0F9D, 0x0FA2, 0x0FA7, 0x0FAC, 0x0FB9, 0x2ADC, 0xFB1D, 0xFB1F, 0xFB2A, 0xFB4E, 0x1D15E,
                  0x1D1BB, 0x1D1C0, 0x2126, 0x212A, 0x212B, 0x0340, 0x0341, 0x0343, 0x0344, 0x0374, 0x037E, 0x0387, 0x1F71]
    maybe = [0x0300, 0x0301, 0x0302, 0x0308, 0x0323, 0x0327, 0x0345, 0x093C, 0x09BE, 0x0B3E, 0x0BBE, 0x3099, 0x309A, 0x110BA, 0x11127,
             0x1161, 0x11A8, 0x0653, 0x0654, 0x0655, 0x064B, 0x1D165, 0x1D16E]
    base = [0x41, 0x61, 0x45, 0x65, 0x4F, 0x6F, 0x55, 0x75, 0x49, 0x69, 0x0391, 0x03B1, 0x0627, 0x0915, 0x0B47, 0x0CC6, 0x1100, 0x3053,
            0x3042, 0x4E00, 0x1D158, 0x1D157, 0x1100, 0x110A5, 0x11131]
    surrogates = [0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0xDC80]
    inputs = [
        [], [0x41], [0x41, 0x300], [0xC0], [0x41, 0x300, 0x301], [0x65, 0x301, 0x323], [0x65, 0x323, 0x301],
        [0x1100, 0x1161], [0x1100, 0x1161, 0x11A8], [0xAC00, 0x11A8], [0xAC01, 0x11A8], [0x1100, 0x1161, 0x11A8, 0x11A8],
        [0x11A8], [0x1161], [0x1100, 0x1100, 0x1161], [0xD7A3, 0x11A8], [0xAC00, 0x1161],
        [0x0958], [0x0915, 0x093C], [0x2126], [0x03A9], [0x212B], [0x00C5], [0x41, 0x30A], [0x1E9B, 0x323], [0x1E9B, 0x0323],
        [0x0344], [0x0308, 0x0301], [0x0F73], [0x0F71, 0x0F72], [0x1D15E], [0x1D157, 0x1D165], [0x0B47, 0x0B3E], [0x0B4B],
        [0x09C7, 0x09BE], [0x09CB], [0x0BC6, 0x0BBE], [0x0BCA], [0x11099, 0x110BA], [0x1109A], [0x110A5, 0x110BA], [0x1109B],
        [0x41, 0x0327, 0x0300], [0x41, 0x0300, 0x0327], [0x1F71], [0x03AC], [0x0301, 0x0041], [0x0020, 0x0308], [0x00A8],
        [0xFB1D], [0x05D9, 0x05B4], [0x1100, 0x1161, 0x0301], [0x0061, 0x0345, 0x0301], [0x1FBE], [0x03B9], [0x0345],
        [0xD800, 0x41, 0x300], [0xDC80, 0x300], [0x41, 0xDC80, 0x300], [0x10FFFF], [0x41, 0x10FFFF, 0x300],
    ]
    pool = (composite, combining_codes, jamo, hangul, exclusions, maybe, base, surrogates, list(range(0x20, 0x7F)))
    weights = [8, 8, 4, 2, 3, 3, 4, 1, 2]
    for _ in range(9000):
        length = rng.randint(1, 7)
        value = []
        for _ in range(length):
            group = rng.choices(pool, weights)[0]
            value.append(rng.choice(group))
        inputs.append(value)
    # 全合成文字の単独・基底文字と結合文字の組も入れる。
    for code in composite:
        inputs.append([code])
    for code, parts in decomps:
        inputs.append(parts)
        inputs.append(parts[:1] + [0x0323] + parts[1:])
    lines = ["# gen_unicode_tables.py が作る。入力;NFC の出力(Python 3.10 / Unicode 13.0.0)。16 進の文字番号を空白で区切る。"]
    for value in inputs:
        text = "".join(chr(c) for c in value)
        out = unicodedata.normalize("NFC", text)
        lines.append("%s;%s" % (" ".join("%x" % c for c in value), " ".join("%x" % ord(c) for c in out)))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rust")
    parser.add_argument("--errno-rust")
    parser.add_argument("--expect")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = {}
    if args.rust:
        outputs[Path(args.rust)] = rust_source()
    if args.errno_rust:
        outputs[Path(args.errno_rust)] = errno_rust_source()
    if args.expect:
        folder = Path(args.expect)
        outputs[folder / "unicode_expect.txt"] = expect_text()
        outputs[folder / "nfc_cases.txt"] = nfc_cases()
    if not outputs:
        parser.error("--rust か --errno-rust か --expect が要る")
    status = 0
    for path, text in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print("不一致: " + str(path))
                status = 1
        else:
            path.write_text(text, encoding="utf-8")
    if args.check and status == 0:
        print("一致: " + ", ".join(str(p) for p in outputs))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
