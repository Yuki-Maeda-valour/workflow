#!/usr/bin/env python3
"""macOS(Apple Libc)の `strerror` の本文の表を errno_macos.rs へ書き出す生成器。

元は Apple が公開している Libc のソース `gen/errlst.c` の `sys_errlist`。
CI の macos-15 に合う版は Libc-1725.40.4(macOS 15.0 の公開ソース)。版は出力の 2 行目に残す。
使い方: python3 generate_errno_macos.py <errlst.c の写し> <版> > errno_macos.rs
範囲の外(負数・表の長さ以上)は、同じ Libc の strerror の `Unknown error: N` の形(Rust 側で作る)。
"""
import re
import sys

source, version = sys.argv[1], sys.argv[2]
text = open(source, encoding="utf-8").read()
body = text[text.index("sys_errlist[] = {") + len("sys_errlist[] = {"):text.index("};")]
body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
entries = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
assert all("\\" not in entry for entry in entries)
print("// 自動生成。native/crates/cli/src/commands/r4a_rest_support/unicode/generate_errno_macos.py")
print(f"// 生成元: apple-oss-distributions/Libc {version} の gen/errlst.c(手で編集しない)。")
print(f"pub static APPLE_STRERROR: [&str; {len(entries)}] = [")
for entry in entries:
    print(f'    "{entry}",')
print("];")
