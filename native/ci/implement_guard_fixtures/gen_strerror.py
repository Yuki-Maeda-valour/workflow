#!/usr/bin/env python3
"""S3I-A: glibc の strerror の表(Rust の生成物 errno_glibc.rs)を作る・照らす。

基準の容器(Ubuntu 24.04・glibc 2.39)で、本家の CPython 3.10.11 を使い、環境を LC_ALL=C にして
LANGUAGE を消して流す(旧版は LC_ALL=C の英語の文を出す)。os.strerror は C の strerror をそのまま呼ぶ。
- `--write <errno_glibc.rs>`: 0 から最大の定義済みの番号までの文を、Rust のソースとして書く(手で書き写さない)。
- `--check <errno_glibc.rs>`: その生成物が、この機械の strerror の結果から作ったものとバイト単位で同じかを照らす。
どちらも、最大の番号より後ろ(256 まで)と負の番号が `Unknown error <番号>` であることを確かめる。
"""
import os
import sys

LIMIT = 256
HEADER = (
    '// 生成物。native/ci/implement_guard_fixtures/gen_strerror.py が基準の容器(Ubuntu 24.04・glibc 2.39)の\n'
    '// strerror から作った。手で直さない(直すときはスクリプトを流し直す)。\n'
)


def environment_problem():
    if os.environ.get('LC_ALL') != 'C':
        return 'LC_ALL=C で流してください'
    if 'LANGUAGE' in os.environ:
        return 'LANGUAGE を消して流してください'
    return None


def table():
    rows = [(number, os.strerror(number)) for number in range(LIMIT + 1)]
    defined = [number for number, text in rows if text != f'Unknown error {number}']
    last = max(defined)
    for number, text in rows[last + 1:]:
        if text != f'Unknown error {number}':
            raise SystemExit(f'番号 {number} の文が想定と違う: {text!r}')
    for number in (-1, -2, 4095, 65536):
        if os.strerror(number) != f'Unknown error {number}':
            raise SystemExit(f'番号 {number} の文が想定と違う: {os.strerror(number)!r}')
    return [text for _, text in rows[:last + 1]]


def render(texts):
    lines = [HEADER, f'pub const LINUX: [&str; {len(texts)}] = [\n']
    for text in texts:
        lines.append('    "' + text.replace('\\', '\\\\').replace('"', '\\"') + '",\n')
    lines.append('];\n')
    return ''.join(lines)


def main(argv):
    if len(argv) != 2 or argv[0] not in ('--write', '--check'):
        print('使い方: gen_strerror.py --write|--check <errno_glibc.rs>', file=sys.stderr)
        return 2
    problem = environment_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    text = render(table())
    if argv[0] == '--write':
        with open(argv[1], 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        return 0
    with open(argv[1], encoding='utf-8', newline='') as handle:
        fixed = handle.read()
    if fixed != text:
        print('errno_glibc.rs がこの機械の strerror と違う。この機械から作った表:', file=sys.stderr)
        print(text, file=sys.stderr, end='')
        return 1
    print('errno_glibc.rs はこの機械の strerror と一致した')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
