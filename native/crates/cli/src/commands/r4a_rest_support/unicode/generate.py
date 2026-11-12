#!/usr/bin/env python3
"""Python 3.10 (Unicode 13.0.0) の文字の性質の表を unicode13.rs へ書き出す生成器。

使い方: python3.10 generate.py > unicode13.rs
本家と同じ中身の CPython 3.10.x(unicodedata.unidata_version == 13.0.0)で流す。
"""
import hashlib
import sys
import unicodedata

assert unicodedata.unidata_version == "13.0.0", unicodedata.unidata_version
assert sys.version_info[:2] == (3, 10), sys.version


def ranges(predicate):
    out, start = [], None
    for code in range(0x110000):
        hit = predicate(chr(code))
        if hit and start is None:
            start = code
        elif not hit and start is not None:
            out.append((start, code - 1))
            start = None
    if start is not None:
        out.append((start, 0x10FFFF))
    return out


def decimal_zeros():
    zeros = []
    for code in range(0x110000):
        ch = chr(code)
        if unicodedata.decimal(ch, None) == 0:
            for step in range(10):
                assert unicodedata.decimal(chr(code + step), None) == step
            zeros.append(code)
    return zeros


def table(name, items, doc):
    body = ", ".join(f"({a:#x}, {b:#x})" for a, b in items)
    return f"/// {doc}\npub const {name}: [(u32, u32); {len(items)}] = [{body}];\n"


nonprintable = ranges(lambda c: not c.isprintable())
spaces = ranges(lambda c: c.isspace())
zeros = decimal_zeros()
digest = hashlib.sha256(repr((nonprintable, spaces, zeros)).encode()).hexdigest()
print("// 自動生成。native/crates/cli/src/commands/r4a_rest_support/unicode/generate.py")
print("// 生成元: CPython 3.10 / Unicode 13.0.0。手で編集しない。")
print(f'pub const UNICODE_VERSION: &str = "{unicodedata.unidata_version}";')
print(f'pub const TABLE_SHA256: &str = "{digest}";')
print(table("NON_PRINTABLE", nonprintable, "`str.isprintable()` が偽の範囲(サロゲートを含む)。"))
print(table("SPACE", spaces, "`str.isspace()` が真の範囲(`int()` の前後除去と正規表現の `\\s`)。"))
zero_body = ", ".join(f"{z:#x}" for z in zeros)
print(f"/// `unicodedata.decimal` が 0 になる文字(各 10 文字が連続する)。\npub const DECIMAL_ZEROS: [u32; {len(zeros)}] = [{zero_body}];")
