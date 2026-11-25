#!/usr/bin/env python3
"""Linux(glibc)の `strerror` の本文の表を errno_linux.rs へ書き出す生成器。

Rust の Linux 版は musl の静的リンクで、`strerror` の本文が glibc と違う errno がある
(例: ELOOP)。旧 Python は glibc の本文を出すので、固定の表で合わせる。
使い方: python3 generate_errno.py > errno_linux.rs(Linux の glibc 上で流す)
"""
import os
import platform
import sys

assert platform.system() == "Linux", platform.system()
print("// 自動生成。native/crates/cli/src/commands/r4a_rest_support/unicode/generate_errno.py")
print("// 生成元: glibc の strerror(Linux)。手で編集しない。")
print("pub static GLIBC_STRERROR: [&str; 134] = [")
for number in range(134):
    text = os.strerror(number)
    assert '"' not in text and "\\" not in text
    print(f'    "{text}",')
print("];")
