#!/usr/bin/env python3
"""Python 3.10 (Unicode 13.0.0) の NFC の表を nfc13.rs へ書き出す生成器。

使い方: python3.10 generate_nfc.py > nfc13.rs
本家と同じ中身の CPython 3.10.x(unicodedata.unidata_version == 13.0.0)で流す。
Hangul の音節は規則(定数)で扱い、表には入れない。
"""
import hashlib
import sys
import unicodedata

assert unicodedata.unidata_version == "13.0.0", unicodedata.unidata_version
assert sys.version_info[:2] == (3, 10), sys.version

HANGUL = range(0xAC00, 0xAC00 + 11172)

decomp = []  # (code, mapping) 正規分解(互換分解を除く・1 段)
for code in range(0x110000):
    if code in HANGUL:
        continue
    text = unicodedata.decomposition(chr(code))
    if text and not text.startswith("<"):
        decomp.append((code, [int(x, 16) for x in text.split()]))

ccc = []  # (start, end, class)
run = None
for code in range(0x110000):
    value = unicodedata.combining(chr(code))
    if value and run and run[2] == value and run[1] == code - 1:
        run[1] = code
    elif value:
        run = [code, code, value]
        ccc.append(run)
ccc = [tuple(item) for item in ccc]

compose = []  # (first, second, composite) 合成除外を除く
for code, mapping in decomp:
    if len(mapping) == 2:
        if unicodedata.normalize("NFC", "".join(map(chr, mapping))) == chr(code):
            compose.append((mapping[0], mapping[1], code))
compose.sort()

digest = hashlib.sha256(repr((decomp, ccc, compose)).encode()).hexdigest()
print("// 自動生成。native/crates/cli/src/commands/r4a_rest_support/unicode/generate_nfc.py")
print("// 生成元: CPython 3.10 / Unicode 13.0.0。手で編集しない。")
print(f'pub const NFC_UNICODE_VERSION: &str = "{unicodedata.unidata_version}";')
print(f'pub const NFC_TABLE_SHA256: &str = "{digest}";')
print("/// 正規分解(互換分解を除く・1 段)。コードの昇順。")
body = ", ".join(f"({c:#x}, &[{', '.join(f'{x:#x}' for x in m)}])" for c, m in decomp)
print(f"pub static DECOMPOSITION: [(u32, &[u32]); {len(decomp)}] = [{body}];")
print("/// 結合クラス(開始, 終了, クラス)。")
body = ", ".join(f"({a:#x}, {b:#x}, {c})" for a, b, c in ccc)
print(f"pub static COMBINING_CLASS: [(u32, u32, u8); {len(ccc)}] = [{body}];")
print("/// 合成(先頭, 後続, 合成結果)。合成除外を含まない。(先頭, 後続)の昇順。")
body = ", ".join(f"({a:#x}, {b:#x}, {c:#x})" for a, b, c in compose)
print(f"pub static COMPOSITION: [(u32, u32, u32); {len(compose)}] = [{body}];")
